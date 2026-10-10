//! Upstream outlet selection, source-ellipsoid metrics, and normalized ROI vectors.
use super::{
    execution::CacheBudget,
    io::{
        spatial_ref,
        vector::{self, GeodesicMetric, Provider, VectorBudget},
    },
    model::{RoiAttributes, SourceId, topological_order},
};
use anyhow::{Context, Result, ensure};
use gdal::{spatial_ref::CoordTransform, vector::LayerAccess};
use serde::Serialize;
use std::{
    collections::{HashMap, HashSet},
    fs,
    path::PathBuf,
};

/// Explicit raw network inputs. Ordered outlets determine overlapping subdomains.
#[derive(Debug, Clone, Serialize)]
pub struct RoiSpec {
    pub catchments: PathBuf,
    pub segments: PathBuf,
    pub output_dir: PathBuf,
    pub crs: String,
    pub outlet_ids: Vec<String>,
    pub id_col: String,
    pub id_down_col: String,
    pub strahler_order_col: String,
    pub catchments_layer: Option<String>,
    pub segments_layer: Option<String>,
    pub catchments_source_crs: Option<String>,
    pub segments_source_crs: Option<String>,
    pub workers: usize,
    /// Conservative application allocation budget, not a hard process RSS ceiling.
    pub memory_limit_mb: usize,
    pub overwrite: bool,
    pub io_slots: usize,
    pub batch_size: usize,
}

#[derive(Debug)]
pub struct RoiReport {
    pub catchments: PathBuf,
    pub segments: PathBuf,
    pub manifest: PathBuf,
    pub source_count: usize,
    pub workers_used: usize,
    pub timings: super::execution::StageTimings,
}

struct Raw {
    id: SourceId,
    down: Option<SourceId>,
    order: f64,
}

/// Select the upstream union and publish normalized, spatially indexed vectors.
/// Replacements require `overwrite`; invalid inputs publish no products.
pub fn define_roi_dataset(spec: &RoiSpec) -> Result<RoiReport> {
    define_roi_dataset_with_progress(spec, &|_| {})
}

pub fn define_roi_dataset_with_progress(
    spec: &RoiSpec,
    progress: super::execution::ProgressCallback<'_>,
) -> Result<RoiReport> {
    let mut reporter = super::execution::Reporter::new(progress);
    let io_slots = super::execution::IoSlots::new(spec.io_slots)?;
    ensure!(spec.batch_size > 0, "Batch size must be positive");

    let mut spec = spec.clone();
    let mut budget = VectorBudget::new(spec.memory_limit_mb, spec.workers)?;
    ensure!(
        !spec.outlet_ids.is_empty(),
        "At least one outlet ID is required"
    );
    let target = vector::parse_crs(&spec.crs)?;
    spec.catchments = vector::absolute_input(&spec.catchments)?;
    spec.segments = vector::absolute_input(&spec.segments)?;
    spec.output_dir = vector::output_paths(
        &spec.output_dir,
        &[
            "roi_catchments.fgb",
            "roi_segments.fgb",
            "manifest-define-roi.json",
        ],
        spec.overwrite,
    )?;
    super::execution::protect_inputs(
        &spec.output_dir,
        &[
            "roi_catchments.fgb".into(),
            "roi_segments.fgb".into(),
            "manifest-define-roi.json".into(),
        ],
        &[&spec.catchments, &spec.segments],
    )?;
    let _cache = CacheBudget::new()?;
    let segments = Provider::open(
        &spec.segments,
        spec.segments_layer.as_deref(),
        spec.segments_source_crs.as_deref(),
    )?;
    let catchments = Provider::open(
        &spec.catchments,
        spec.catchments_layer.as_deref(),
        spec.catchments_source_crs.as_deref(),
    )?;
    let mut layer = segments.layer()?;
    let id_field = vector::field(&layer, &spec.id_col)?;
    let down_field = vector::field(&layer, &spec.id_down_col)?;
    let order_field = vector::field(&layer, &spec.strahler_order_col)?;
    let ty = vector::id_type(&layer, id_field)?;
    // OGR can avoid decoding geometry during the topology scan.
    let mut ignored = [c"OGR_GEOMETRY".as_ptr(), std::ptr::null()];
    let status = unsafe { gdal_sys::OGR_L_SetIgnoredFields(layer.c_layer(), ignored.as_mut_ptr()) };
    ensure!(status == 0, "Cannot select topology fields");
    let _output = super::execution::OutputDirectory::new(&spec.output_dir)?;
    let mut raw = Vec::new();
    let mut lookup = HashMap::new();
    for feature in layer.features() {
        let Some(order) = vector::number(&feature, order_field)? else {
            continue;
        };
        if !order.is_finite() || order < 1. {
            continue;
        }
        budget.reserve(1024)?;
        let id = vector::source_id(vector::value(&feature, id_field)?)?
            .context("Null retained segment ID")?;
        ensure!(
            lookup.insert(id.clone(), raw.len()).is_none(),
            "Duplicate retained segment ID: {}",
            id.key()
        );
        raw.push(Raw {
            id,
            down: vector::source_id(vector::value(&feature, down_field)?)?,
            order,
        });
    }
    // Restore the layer before reading selected geometries.
    let status = unsafe { gdal_sys::OGR_L_SetIgnoredFields(layer.c_layer(), std::ptr::null_mut()) };
    ensure!(status == 0, "Cannot restore geometry fields");
    ensure!(
        !raw.is_empty(),
        "No segments remain after Strahler filtering"
    );
    let mut upstream = vec![Vec::new(); raw.len()];
    for (i, row) in raw.iter().enumerate() {
        if let Some(target) = row.down.as_ref().and_then(|id| lookup.get(id)) {
            upstream[*target].push(i);
        }
    }
    let mut sub = vec![0; raw.len()];
    let mut outlets = HashSet::new();
    for (position, text) in spec.outlet_ids.iter().enumerate() {
        let id = vector::outlet(text, ty)?;
        let start = *lookup
            .get(&id)
            .with_context(|| format!("Outlet ID not found after Strahler filtering: {text}"))?;
        outlets.insert(start);
        let domain = i64::try_from(spec.outlet_ids.len() - position)?;
        let mut seen = HashSet::new();
        let mut stack = vec![start];
        while let Some(i) = stack.pop() {
            if seen.insert(i) {
                sub[i] = domain;
                stack.extend(&upstream[i]);
            }
        }
    }
    let mut selected: Vec<_> = raw
        .iter()
        .enumerate()
        .filter(|(i, _)| sub[*i] > 0)
        .map(|(i, _)| i)
        .collect();
    selected.sort_by_cached_key(|i| raw[*i].id.key());
    let mut rows = Vec::with_capacity(selected.len());
    for i in selected {
        let row = &raw[i];
        ensure!(
            row.order.fract() == 0. && row.order < i64::MAX as f64,
            "Selected Strahler orders must be integral int64 values"
        );
        ensure!(
            outlets.contains(&i)
                || row
                    .down
                    .as_ref()
                    .and_then(|id| lookup.get(id))
                    .is_some_and(|target| sub[*target] > 0),
            "Selected non-outlet does not connect to an outlet"
        );
        rows.push(RoiAttributes {
            id: row.id.clone(),
            id_down: row.down.clone(),
            sub: sub[i],
            strahler_order: row.order as i64,
            metrics: [0.; 4],
            water_course: row.id.clone(),
        });
    }
    drop(raw);
    drop(lookup);
    drop(upstream);
    drop(sub);
    drop(outlets);
    let ids: Vec<_> = rows.iter().map(|r| r.id.clone()).collect();
    let selected_lookup: HashMap<_, _> = ids
        .iter()
        .cloned()
        .enumerate()
        .map(|(i, id)| (id, i))
        .collect();
    let downstream: Vec<_> = rows
        .iter()
        .map(|r| {
            r.id_down
                .as_ref()
                .and_then(|id| selected_lookup.get(id))
                .copied()
        })
        .collect();
    let order = topological_order(&ids, &downstream)?;
    budget.retain_only(
        rows.len()
            .checked_mul(1024)
            .context("Selected topology allocation overflow")?,
    )?;
    budget.reserve(
        rows.len()
            .checked_mul(32)
            .context("Feature ID allocation overflow")?,
    )?;
    let segment_geometry = selected_geometry(&segments, id_field, &selected_lookup)?;
    let catchment_layer = catchments.layer()?;
    let catchment_field = vector::field(&catchment_layer, &spec.id_col)?;
    ensure!(
        vector::id_type(&catchment_layer, catchment_field)? == ty,
        "Source catchment and segment ID types differ"
    );
    let catchment_geometry = selected_geometry(&catchments, catchment_field, &selected_lookup)?;
    let largest = segment_geometry
        .bytes
        .iter()
        .chain(&catchment_geometry.bytes)
        .copied()
        .max()
        .unwrap_or(0);
    let workers = budget.workers(
        largest
            .checked_mul(4)
            .and_then(|n| n.checked_add(4 * super::execution::MIB))
            .context("Geometry worker allocation overflow")?,
        rows.len() * 2,
    )?;
    let jobs = rows.len() * 2;
    let row_count = rows.len();
    reporter.enter("processing", "Transforming selected geometry");
    let output_rows = std::sync::Mutex::new(rows);
    let next = std::sync::atomic::AtomicUsize::new(0);
    let completed = std::sync::atomic::AtomicUsize::new(0);
    let batch = spec.batch_size.min(jobs.div_ceil(workers * 4)).max(1);
    vector::parallel(jobs, workers, |_, _| {
        let segment_source = Provider::open(
            &spec.segments,
            spec.segments_layer.as_deref(),
            spec.segments_source_crs.as_deref(),
        )?;
        let catchment_source = Provider::open(
            &spec.catchments,
            spec.catchments_layer.as_deref(),
            spec.catchments_source_crs.as_deref(),
        )?;
        let mut segment_layer = segment_source.layer()?;
        let mut catchment_layer = catchment_source.layer()?;
        let metrics = [
            GeodesicMetric::new(&spatial_ref(&segments.crs)?)?,
            GeodesicMetric::new(&spatial_ref(&catchments.crs)?)?,
        ];
        loop {
            let start = next.fetch_add(batch, std::sync::atomic::Ordering::Relaxed);
            if start >= jobs {
                break;
            }
            for job in start..(start + batch).min(jobs) {
                let polygon = job >= row_count;
                let i = job % row_count;
                let (layer, fid) = if polygon {
                    (&mut catchment_layer, catchment_geometry.fids[i])
                } else {
                    (&mut segment_layer, segment_geometry.fids[i])
                };
                let _permit = io_slots.acquire()?;
                let feature = layer
                    .feature(fid)
                    .with_context(|| format!("Selected feature {} disappeared", ids[i].key()))?;
                drop(_permit);
                let bytes = feature
                    .geometry()
                    .context("Selected source has no geometry")?
                    .wkb()?;
                vector::validate_geometry(&bytes, polygon)
                    .with_context(|| format!("Invalid selected geometry at {}", ids[i].key()))?;
                let geometry = gdal::vector::Geometry::from_wkb(&bytes)?;
                let k = usize::from(polygon);
                let metric = metrics[k].measure(&geometry, polygon)?;
                output_rows
                    .lock()
                    .map_err(|_| anyhow::anyhow!("ROI row lock poisoned"))?[i]
                    .metrics[if polygon { 2 } else { 0 }] = metric;
                reporter.advance(
                    completed.fetch_add(1, std::sync::atomic::Ordering::Relaxed) + 1,
                    Some(jobs),
                );
            }
        }
        Ok(())
    })?;
    let mut rows = output_rows
        .into_inner()
        .map_err(|_| anyhow::anyhow!("ROI row lock poisoned"))?;
    for row in &mut rows {
        row.metrics[1] = row.metrics[0];
        row.metrics[3] = row.metrics[2];
    }
    for i in &order {
        if let Some(target) = downstream[*i] {
            rows[target].metrics[1] += rows[*i].metrics[1];
            rows[target].metrics[3] += rows[*i].metrics[3];
        }
    }
    let mut children = vec![Vec::new(); rows.len()];
    for (i, target) in downstream.iter().enumerate() {
        if let Some(target) = target
            && rows[i].sub == rows[*target].sub
        {
            children[*target].push(i);
        }
    }
    for i in order.iter().rev() {
        if let Some(main) = children[*i].iter().max_by(|a, b| {
            rows[**a].metrics[3]
                .total_cmp(&rows[**b].metrics[3])
                .then_with(|| rows[**a].metrics[0].total_cmp(&rows[**b].metrics[0]))
                .then_with(|| ids[**a].key().cmp(&ids[**b].key()))
        }) {
            rows[*main].water_course = rows[*i].water_course.clone();
        }
    }
    reporter.enter("finalizing", "Writing indexed ROI vectors");
    fs::create_dir_all(&spec.output_dir)?;
    spec.output_dir = fs::canonicalize(&spec.output_dir)?;
    let staging = tempfile::tempdir_in(&spec.output_dir)?;
    // Integer source IDs normalize to int64, matching the frozen ROI schema.
    let output_ty = if ty == gdal::vector::OGRFieldType::OFTInteger {
        gdal::vector::OGRFieldType::OFTInteger64
    } else {
        ty
    };
    let schema = vector::roi_schema(output_ty);
    for (name, provider, geometry_ids, geometry_type) in [
        (
            "roi_catchments.fgb",
            &catchments,
            &catchment_geometry,
            catchments.geometry_type,
        ),
        (
            "roi_segments.fgb",
            &segments,
            &segment_geometry,
            segments.geometry_type,
        ),
    ] {
        let layer = provider.layer()?;
        let source = spatial_ref(&provider.crs)?;
        let transform = CoordTransform::new(&source, &target)?;
        vector::write_vector(
            &staging.path().join(name),
            &target,
            &schema,
            geometry_type,
            true,
            rows.iter().enumerate().map(|(i, row)| {
                let _permit = io_slots.acquire()?;
                let feature = layer
                    .feature(geometry_ids.fids[i])
                    .with_context(|| format!("Selected feature {} disappeared", row.id.key()))?;
                drop(_permit);
                let bytes = feature
                    .geometry()
                    .context("Selected source has no geometry")?
                    .wkb()?;
                let transformed =
                    gdal::vector::Geometry::from_wkb(&bytes)?.transform(&transform)?;
                Ok((vector::roi_values(row, output_ty)?, transformed.wkb()?))
            }),
        )?;
    }
    let manifest = vector::finish(
        staging.path(),
        &spec.output_dir,
        "define-roi",
        &spec,
        &["roi_catchments.fgb", "roi_segments.fgb"],
        workers,
        reporter.elapsed_seconds(),
    )?;
    let timings = reporter.finish();
    Ok(RoiReport {
        timings,
        catchments: spec.output_dir.join("roi_catchments.fgb"),
        segments: spec.output_dir.join("roi_segments.fgb"),
        manifest,
        source_count: rows.len(),
        workers_used: workers,
    })
}

struct GeometryIds {
    fids: Vec<u64>,
    bytes: Vec<usize>,
}

fn selected_geometry(
    provider: &Provider,
    field: usize,
    lookup: &HashMap<SourceId, usize>,
) -> Result<GeometryIds> {
    let mut layer = provider.layer()?;
    let mut result = GeometryIds {
        fids: vec![u64::MAX; lookup.len()],
        bytes: vec![0; lookup.len()],
    };
    for feature in layer.features() {
        let Some(id) = vector::source_id(vector::value(&feature, field)?)? else {
            continue;
        };
        let Some(i) = lookup.get(&id) else {
            continue;
        };
        ensure!(
            result.fids[*i] == u64::MAX,
            "Duplicate selected catchment/segment ID: {}",
            id.key()
        );
        let geometry = feature
            .geometry()
            .context("Selected source has no geometry")?;
        // The feature owns this geometry for the lifetime of the WKB size query.
        let bytes = unsafe { gdal_sys::OGR_G_WkbSizeEx(geometry.c_geometry()) };
        result.fids[*i] = feature.fid().context("Selected source has no feature ID")?;
        result.bytes[*i] = bytes;
    }
    ensure!(
        result.fids.iter().all(|&fid| fid != u64::MAX),
        "Selected catchment/segment is missing"
    );
    Ok(result)
}
