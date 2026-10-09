//! Evolving mini-basin aggregation with separate reach and catchment assignments.
use super::{
    execution::CacheBudget,
    io::{
        spatial_ref,
        vector::{self, Provider, ROI_FIELDS, VectorBudget},
    },
    model::{ATTRIBUTES, Attributes, RoiAttributes, topological_order},
};
use anyhow::{Context, Result, ensure};
use gdal::{
    spatial_ref::CoordTransform,
    vector::{FieldValue, LayerAccess, OGRFieldType},
};
use geos::{Geom, Geometry};
use serde::Serialize;
use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    fs,
    path::PathBuf,
};

#[derive(Debug, Clone, Serialize)]
pub struct AggregationSpec {
    pub roi_catchments: PathBuf,
    pub roi_segments: PathBuf,
    pub output_dir: PathBuf,
    /// Minimum eligible upstream area in square kilometres.
    pub uparea_min: f64,
    /// Minimum evolving reach length in kilometres.
    pub lmin: f64,
    pub workers: usize,
    /// Conservative application allocation budget, not a hard process RSS ceiling.
    pub memory_limit_mb: usize,
}

#[derive(Debug)]
pub struct AggregationReport {
    pub catchments: PathBuf,
    pub segments: PathBuf,
    pub source_to_mini: PathBuf,
    pub manifest: PathBuf,
    pub source_count: usize,
    pub mini_count: usize,
    pub workers_used: usize,
}

struct Sources {
    rows: Vec<RoiAttributes>,
    geometry: Vec<Vec<u8>>,
    crs: String,
    ty: u32,
}
struct Plan {
    attributes: Vec<Attributes>,
    catchment: Vec<usize>,
    reach: Vec<Option<usize>>,
}

/// Aggregate a normalized ROI and publish ordered mini vectors and source mapping.
/// Existing products are never overwritten; invalid inputs publish no products.
pub fn aggregate_roi_dataset(spec: &AggregationSpec) -> Result<AggregationReport> {
    let mut spec = spec.clone();
    let mut budget = VectorBudget::new(spec.memory_limit_mb, spec.workers)?;
    ensure!(
        [spec.uparea_min, spec.lmin]
            .iter()
            .all(|v| v.is_finite() && *v >= 0.),
        "Aggregation thresholds must be finite and non-negative"
    );
    spec.roi_catchments = vector::absolute_input(&spec.roi_catchments)?;
    spec.roi_segments = vector::absolute_input(&spec.roi_segments)?;
    spec.output_dir = vector::output_paths(
        &spec.output_dir,
        &[
            "mini_catchments.fgb",
            "mini_segments.fgb",
            "source_to_mini.csv",
            "manifest-aggregate.json",
        ],
    )?;
    let _cache = CacheBudget::new()?;
    let segments = read_roi(&spec.roi_segments, &mut budget)?;
    let catchments = read_roi(&spec.roi_catchments, &mut budget)?;
    ensure!(
        spatial_ref(&segments.crs)? == spatial_ref(&catchments.crs)?,
        "ROI CRS mismatch"
    );
    ensure!(
        segments.ty == catchments.ty
            && segments.rows.len() == catchments.rows.len()
            && segments
                .rows
                .iter()
                .zip(&catchments.rows)
                .all(|(a, b)| a.id == b.id),
        "ROI source IDs or ID types differ"
    );
    let plan = plan(&segments.rows, &catchments.rows, spec.uparea_min, spec.lmin)?;
    let count = plan.attributes.len();
    let mut catchment_groups = vec![Vec::new(); count];
    let mut reach_groups = vec![Vec::new(); count];
    for (i, target) in plan.catchment.iter().enumerate() {
        catchment_groups[*target].push(i);
    }
    for (i, target) in plan.reach.iter().enumerate() {
        if let Some(target) = target {
            reach_groups[*target].push(i);
        }
    }
    let largest = catchment_groups
        .iter()
        .map(|members| {
            members
                .iter()
                .map(|i| catchments.geometry[*i].len())
                .sum::<usize>()
        })
        .chain(reach_groups.iter().map(|members| {
            members
                .iter()
                .map(|i| segments.geometry[*i].len())
                .sum::<usize>()
        }))
        .max()
        .unwrap_or(0);
    let workers = budget.workers(largest, count)?;
    let results = vector::parallel(count, workers, |worker, workers| {
        let source = spatial_ref(&catchments.crs)?;
        let target = vector::parse_crs("EPSG:4326")?;
        let transform = CoordTransform::new(&source, &target)?;
        let mut results = Vec::new();
        for mini in (worker..count).step_by(workers) {
            let mut mapping = Vec::new();
            let mut polygons = Vec::new();
            for i in &catchment_groups[mini] {
                let geometry = vector::validate_geometry(&catchments.geometry[*i], true)?;
                let centroid = geometry.get_centroid()?;
                let mut x = [centroid.get_x()?];
                let mut y = [centroid.get_y()?];
                transform.transform_coords(&mut x, &mut y, &mut [])?;
                ensure!(
                    x[0].is_finite() && y[0].is_finite(),
                    "Non-finite source centroid"
                );
                mapping.push((*i, x[0], y[0]));
                polygons.push(geometry);
            }
            let polygon = Geometry::create_geometry_collection(polygons)?.unary_union()?;
            let lines = reach_groups[mini]
                .iter()
                .map(|i| vector::validate_geometry(&segments.geometry[*i], false))
                .collect::<Result<Vec<_>>>()?;
            let line = Geometry::create_geometry_collection(lines)?.unary_union()?;
            results.push((
                mini,
                polygon.to_wkb()?.to_vec(),
                line.to_wkb()?.to_vec(),
                mapping,
            ));
        }
        Ok(results)
    })?;
    let mut polygons = vec![Vec::new(); count];
    let mut lines = vec![Vec::new(); count];
    let mut coordinates = vec![[0.; 2]; catchments.rows.len()];
    for (mini, polygon, line, mapping) in results.into_iter().flatten() {
        vector::validate_geometry(&polygon, true)?;
        vector::validate_geometry(&line, false)?;
        polygons[mini] = polygon;
        lines[mini] = line;
        for (i, x, y) in mapping {
            coordinates[i] = [x, y];
        }
    }
    // Excluded sources do not contribute reach geometry, but their inputs must still be valid.
    for (i, mini) in plan.reach.iter().enumerate() {
        if mini.is_none() {
            vector::validate_geometry(&segments.geometry[i], false)?;
        }
    }
    fs::create_dir_all(&spec.output_dir)?;
    spec.output_dir = fs::canonicalize(&spec.output_dir)?;
    let staging = tempfile::tempdir_in(&spec.output_dir)?;
    let schema: Vec<_> = ATTRIBUTES
        .iter()
        .enumerate()
        .map(|(i, name)| {
            (
                *name,
                if i < 4 {
                    OGRFieldType::OFTInteger64
                } else {
                    OGRFieldType::OFTReal
                },
            )
        })
        .collect();
    for (name, geometries) in [
        ("mini_catchments.fgb", &polygons),
        ("mini_segments.fgb", &lines),
    ] {
        vector::write_vector(
            &staging.path().join(name),
            &spatial_ref(&catchments.crs)?,
            &schema,
            gdal::vector::OGRwkbGeometryType::wkbUnknown,
            false,
            plan.attributes
                .iter()
                .zip(geometries)
                .map(|(row, geometry)| {
                    let fields = row
                        .integers
                        .iter()
                        .map(|v| Some(FieldValue::Integer64Value(*v)))
                        .chain(row.metrics.iter().map(|v| Some(FieldValue::RealValue(*v))))
                        .collect();
                    Ok((fields, geometry.as_slice()))
                }),
        )?;
    }
    let mut mapping = csv::Writer::from_path(staging.path().join("source_to_mini.csv"))?;
    mapping.write_record(["id", "mini_id", "sub", "longitude", "latitude"])?;
    for (i, row) in catchments.rows.iter().enumerate() {
        mapping.write_record([
            row.id.key(),
            (plan.catchment[i] + 1).to_string(),
            row.sub.to_string(),
            format!("{:?}", coordinates[i][0]),
            format!("{:?}", coordinates[i][1]),
        ])?;
    }
    mapping.flush()?;
    drop(mapping);
    let manifest = vector::finish(
        staging.path(),
        &spec.output_dir,
        "aggregate",
        &spec,
        &[
            "mini_catchments.fgb",
            "mini_segments.fgb",
            "source_to_mini.csv",
        ],
        workers,
    )?;
    Ok(AggregationReport {
        catchments: spec.output_dir.join("mini_catchments.fgb"),
        segments: spec.output_dir.join("mini_segments.fgb"),
        source_to_mini: spec.output_dir.join("source_to_mini.csv"),
        manifest,
        source_count: catchments.rows.len(),
        mini_count: count,
        workers_used: workers,
    })
}

fn read_roi(path: &std::path::Path, budget: &mut VectorBudget) -> Result<Sources> {
    let provider = Provider::open(path, None, None)?;
    let mut layer = provider.layer()?;
    let fields: Vec<_> = ROI_FIELDS
        .iter()
        .map(|name| vector::field(&layer, name))
        .collect::<Result<_>>()?;
    let ty = vector::id_type(&layer, fields[0])?;
    for i in [1, 8] {
        ensure!(
            vector::id_type(&layer, fields[i])? == ty,
            "ROI reference ID types differ"
        );
    }
    let mut records = BTreeMap::new();
    for feature in layer.features() {
        budget.reserve(2048)?;
        let id = vector::source_id(vector::value(&feature, fields[0])?)?
            .context("Null ROI source ID")?;
        let mut metrics = [0.; 4];
        for (i, value) in metrics.iter_mut().enumerate() {
            *value = vector::number(&feature, fields[i + 4])?.context("Null ROI metric")?;
            ensure!(value.is_finite() && *value >= 0., "Invalid ROI metric");
        }
        let row = RoiAttributes {
            id: id.clone(),
            id_down: vector::source_id(vector::value(&feature, fields[1])?)?,
            sub: vector::integer(&feature, fields[2])?,
            strahler_order: vector::integer(&feature, fields[3])?,
            metrics,
            water_course: vector::source_id(vector::value(&feature, fields[8])?)?
                .context("Null ROI water course")?,
        };
        let geometry = feature
            .geometry()
            .context("ROI source has no geometry")?
            .wkb()?;
        budget.geometry(geometry.len())?;
        ensure!(
            records.insert(id, (row, geometry)).is_none(),
            "Duplicate ROI source ID"
        );
    }
    ensure!(!records.is_empty(), "ROI contains no sources");
    let mut ordered: Vec<_> = records.into_values().collect();
    ordered.sort_by_cached_key(|(row, _)| row.id.key());
    let (rows, geometry) = ordered.into_iter().unzip();
    Ok(Sources {
        rows,
        geometry,
        crs: provider.crs,
        ty,
    })
}

fn representative(members: impl Iterator<Item = usize>, rows: &[RoiAttributes]) -> usize {
    members
        .max_by(|a, b| {
            rows[*a].metrics[3]
                .total_cmp(&rows[*b].metrics[3])
                .then_with(|| rows[*a].metrics[0].total_cmp(&rows[*b].metrics[0]))
                .then_with(|| rows[*a].id.key().cmp(&rows[*b].id.key()))
        })
        .expect("non-empty mini group")
}
fn find(parent: &mut [usize], mut i: usize) -> usize {
    while parent[i] != i {
        parent[i] = parent[parent[i]];
        i = parent[i];
    }
    i
}

fn plan(
    rows: &[RoiAttributes],
    catchments: &[RoiAttributes],
    uparea_min: f64,
    lmin: f64,
) -> Result<Plan> {
    let ids: Vec<_> = rows.iter().map(|row| row.id.clone()).collect();
    let lookup: HashMap<_, _> = ids
        .iter()
        .cloned()
        .enumerate()
        .map(|(i, id)| (id, i))
        .collect();
    let downstream: Vec<_> = rows
        .iter()
        .map(|row| row.id_down.as_ref().and_then(|id| lookup.get(id)).copied())
        .collect();
    topological_order(&ids, &downstream)?;
    let n = rows.len();
    let mut upstream = vec![Vec::new(); n];
    for (i, target) in downstream.iter().enumerate() {
        if let Some(target) = target {
            upstream[*target].push(i);
        }
    }
    let eligible: Vec<_> = rows
        .iter()
        .map(|row| row.metrics[3] >= uparea_min)
        .collect();
    ensure!(
        eligible.iter().any(|v| *v),
        "No segments satisfy uparea-min"
    );
    let same_domain = |a: usize, b: usize| {
        rows[a].sub == rows[b].sub && rows[a].water_course == rows[b].water_course
    };
    let mut reduced = vec![None; n];
    let mut counts = vec![0; n];
    for i in 0..n {
        if eligible[i] {
            let mut target = downstream[i];
            while let Some(j) = target {
                if eligible[j] {
                    break;
                }
                target = downstream[j];
            }
            reduced[i] = target;
            if let Some(target) = target {
                counts[target] += 1;
            }
        }
    }
    let mut parent: Vec<_> = (0..n).collect();
    for (i, target) in reduced.iter().copied().enumerate() {
        if let Some(target) = target
            && counts[target] == 1
            && same_domain(i, target)
        {
            let left = find(&mut parent, i);
            let right = find(&mut parent, target);
            parent[right] = left;
        }
    }
    let mut components: BTreeMap<usize, BTreeSet<usize>> = BTreeMap::new();
    for (i, yes) in eligible.iter().enumerate() {
        if *yes {
            components
                .entry(find(&mut parent, i))
                .or_default()
                .insert(i);
        }
    }
    let mut groups: BTreeMap<_, _> = components
        .into_values()
        .map(|members| (representative(members.iter().copied(), rows), members))
        .collect();
    let mut reach = vec![None; n];
    let mut lengths = BTreeMap::new();
    for (rep, members) in &groups {
        lengths.insert(
            *rep,
            members.iter().map(|i| rows[*i].metrics[0]).sum::<f64>(),
        );
        for i in members {
            reach[*i] = Some(*rep);
        }
    }
    loop {
        let mut neighbors: BTreeMap<usize, BTreeSet<usize>> =
            groups.keys().map(|i| (*i, BTreeSet::new())).collect();
        for (i, target) in reduced.iter().enumerate() {
            if let Some(target) = target
                && let (Some(a), Some(b)) = (reach[i], reach[*target])
                && a != b
                && same_domain(a, b)
            {
                neighbors.get_mut(&a).unwrap().insert(b);
                neighbors.get_mut(&b).unwrap().insert(a);
            }
        }
        let pair = groups
            .keys()
            .filter(|i| lengths[i] < lmin)
            .filter_map(|i| {
                neighbors[i]
                    .iter()
                    .min_by(|a, b| {
                        lengths[a]
                            .total_cmp(&lengths[b])
                            .then_with(|| ids[**a].key().cmp(&ids[**b].key()))
                    })
                    .map(|target| (*i, *target))
            })
            .min_by_key(|(i, _)| ids[*i].key());
        let Some((source, target)) = pair else {
            break;
        };
        let mut members = groups.remove(&source).unwrap();
        members.extend(groups.remove(&target).unwrap());
        let rep = representative([source, target].into_iter(), rows);
        let length = lengths.remove(&source).unwrap() + lengths.remove(&target).unwrap();
        for i in &members {
            reach[*i] = Some(rep);
        }
        groups.insert(rep, members);
        lengths.insert(rep, length);
    }
    let short: Vec<_> = groups
        .keys()
        .filter(|i| lengths[i] < lmin)
        .copied()
        .collect();
    for rep in short {
        for i in groups.remove(&rep).unwrap() {
            reach[i] = None;
        }
        lengths.remove(&rep);
    }
    ensure!(
        !groups.is_empty(),
        "Catchments have no surviving aggregation target in their sub"
    );
    let mut catchment = reach.clone();
    for strict in [true, false] {
        let mut seen = vec![false; n];
        for start in 0..n {
            if catchment[start].is_some() || seen[start] {
                continue;
            }
            let mut stack = vec![start];
            let mut component = BTreeSet::new();
            let mut candidates = BTreeSet::new();
            while let Some(i) = stack.pop() {
                if component.contains(&i)
                    || rows[i].sub != rows[start].sub
                    || (strict && !same_domain(i, start))
                {
                    continue;
                }
                component.insert(i);
                for j in upstream[i].iter().copied().chain(downstream[i]) {
                    if rows[j].sub != rows[start].sub || (strict && !same_domain(j, start)) {
                        continue;
                    }
                    if let Some(target) = reach[j] {
                        candidates.insert(target);
                    } else if !component.contains(&j) {
                        stack.push(j);
                    }
                }
            }
            for i in &component {
                seen[*i] = true;
            }
            if let Some(target) = candidates.iter().min_by(|a, b| {
                lengths[a]
                    .total_cmp(&lengths[b])
                    .then_with(|| ids[**a].key().cmp(&ids[**b].key()))
            }) {
                for i in component {
                    if catchment[i].is_none() {
                        catchment[i] = Some(*target);
                    }
                }
            }
        }
    }
    ensure!(
        catchment.iter().all(Option::is_some),
        "Catchment has no surviving aggregation target in its sub"
    );
    let representatives: Vec<_> = groups.keys().copied().collect();
    let mini_lookup: HashMap<_, _> = representatives
        .iter()
        .copied()
        .enumerate()
        .map(|(i, rep)| (rep, i))
        .collect();
    let mut mini_down = vec![None; groups.len()];
    for (mini, rep) in representatives.iter().enumerate() {
        let mut target = downstream[*rep];
        while let Some(i) = target {
            if groups[rep].contains(&i) {
                break;
            }
            if let Some(other) = reach[i]
                && other != *rep
            {
                mini_down[mini] = Some(mini_lookup[&other]);
                break;
            }
            target = downstream[i];
        }
    }
    let mini_ids: Vec<_> = representatives.iter().map(|i| ids[*i].clone()).collect();
    let order = topological_order(&mini_ids, &mini_down)?;
    let mut p_order = vec![1i64; groups.len()];
    for i in order {
        if let Some(target) = mini_down[i] {
            p_order[target] = p_order[target].max(p_order[i] + 1);
        }
    }
    let mut sorted: Vec<_> = (0..groups.len()).collect();
    sorted.sort_by(|a, b| {
        let left = representatives[*a];
        let right = representatives[*b];
        rows[left]
            .sub
            .cmp(&rows[right].sub)
            .then_with(|| p_order[*a].cmp(&p_order[*b]))
            .then_with(|| catchments[left].metrics[3].total_cmp(&catchments[right].metrics[3]))
            .then_with(|| ids[left].key().cmp(&ids[right].key()))
    });
    let mut dense = vec![0; groups.len()];
    for (i, mini) in sorted.iter().enumerate() {
        dense[*mini] = i;
    }
    let catchment: Vec<_> = catchment
        .into_iter()
        .map(|target| dense[mini_lookup[&target.unwrap()]])
        .collect();
    let reach: Vec<_> = reach
        .into_iter()
        .map(|target| target.map(|rep| dense[mini_lookup[&rep]]))
        .collect();
    let mut areas = vec![0.; groups.len()];
    for (i, mini) in catchment.iter().enumerate() {
        areas[*mini] += catchments[i].metrics[2];
    }
    let attributes = sorted
        .into_iter()
        .enumerate()
        .map(|(i, mini)| {
            let rep = representatives[mini];
            Attributes {
                integers: [
                    (i + 1) as i64,
                    mini_down[mini].map_or(-1, |target| (dense[target] + 1) as i64),
                    rows[rep].sub,
                    p_order[mini],
                ],
                metrics: [
                    groups[&rep].iter().map(|i| rows[*i].metrics[0]).sum(),
                    rows[rep].metrics[1],
                    areas[i],
                    catchments[rep].metrics[3],
                ],
            }
        })
        .collect();
    Ok(Plan {
        attributes,
        catchment,
        reach,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::prepro::model::SourceId;

    fn rows(
        ids: &[i64],
        down: &[Option<i64>],
        sub: &[i64],
        lengths: &[f64],
        areas: &[f64],
        courses: &[i64],
    ) -> Vec<RoiAttributes> {
        (0..ids.len())
            .map(|i| RoiAttributes {
                id: SourceId::Integer(ids[i]),
                id_down: down[i].map(SourceId::Integer),
                sub: sub[i],
                strahler_order: 1,
                metrics: [lengths[i], areas[i], ids[i] as f64, areas[i]],
                water_course: SourceId::Integer(courses[i]),
            })
            .collect()
    }

    #[test]
    fn confluence_boundaries_dense_topology_and_mapping() -> Result<()> {
        let rows = rows(
            &[1, 2, 3, 4],
            &[None, Some(1), Some(1), Some(2)],
            &[1; 4],
            &[1.; 4],
            &[10., 6., 4., 2.],
            &[1, 1, 3, 1],
        );
        let plan = plan(&rows, &rows, 0., 0.)?;
        assert_eq!(plan.catchment, [2, 1, 0, 1]);
        assert_eq!(
            plan.attributes
                .iter()
                .map(|a| a.integers)
                .collect::<Vec<_>>(),
            [[1, 3, 1, 1], [2, 3, 1, 1], [3, -1, 1, 2]]
        );
        assert_eq!(
            plan.attributes.iter().map(|a| a.metrics[2]).sum::<f64>(),
            10.
        );
        Ok(())
    }

    #[test]
    fn excluded_connectors_supply_catchment_but_not_reach() -> Result<()> {
        let rows = rows(
            &[1, 2, 3],
            &[None, Some(1), Some(2)],
            &[1; 3],
            &[5., 1., 4.],
            &[10., 2., 8.],
            &[1; 3],
        );
        let plan = plan(&rows, &rows, 5., 0.)?;
        assert_eq!(plan.catchment, [0, 0, 0]);
        assert_eq!(plan.reach, [Some(0), None, Some(0)]);
        assert_eq!(plan.attributes[0].metrics[0], 9.);
        Ok(())
    }

    #[test]
    fn chain_length_and_metric_provenance() -> Result<()> {
        let segments = rows(
            &[1, 2],
            &[None, Some(1)],
            &[1; 2],
            &[2., 3.],
            &[300., 200.],
            &[1; 2],
        );
        let mut catchments = segments.clone();
        catchments[0].metrics = [70., 700., 10., 30.];
        catchments[1].metrics = [80., 800., 20., 20.];
        let plan = plan(&segments, &catchments, 0., 1.)?;
        assert_eq!(plan.attributes[0].metrics, [5., 300., 30., 30.]);
        assert_eq!(plan.catchment, [0, 0]);
        Ok(())
    }

    #[test]
    fn short_groups_merge_using_evolving_lengths_and_string_ties() -> Result<()> {
        let rows = rows(
            &[1, 2, 10],
            &[None, Some(1), Some(1)],
            &[1; 3],
            &[1., 2., 2.],
            &[10., 6., 6.],
            &[1; 3],
        );
        let tied = plan(&rows, &rows, 0., 2.)?;
        assert_eq!(tied.catchment, [1, 0, 1]);
        assert_eq!(
            tied.attributes
                .iter()
                .map(|a| a.metrics[0])
                .collect::<Vec<_>>(),
            [2., 3.]
        );
        let plan = plan(&rows, &rows, 0., 3.)?;
        // The mouth first joins string ID "10". The remaining short head then joins the evolved group.
        assert_eq!(plan.catchment, [0, 0, 0]);
        assert_eq!(plan.attributes[0].metrics[0], 5.);
        Ok(())
    }

    #[test]
    fn removed_short_group_uses_connected_sub_fallback_without_cross_sub_merge() -> Result<()> {
        let rows = rows(
            &[1, 2, 3, 4],
            &[None, Some(1), Some(2), Some(1)],
            &[1, 1, 1, 2],
            &[3., 0.5, 3., 3.],
            &[10., 8., 6., 5.],
            &[1, 2, 1, 2],
        );
        let plan = plan(&rows, &rows, 0., 1.)?;
        assert_eq!(plan.catchment[1], plan.catchment[0]);
        assert_ne!(plan.catchment[2], plan.catchment[0]);
        assert_ne!(plan.catchment[3], plan.catchment[0]);
        assert_eq!(plan.reach[1], None);
        Ok(())
    }

    #[test]
    fn rejects_cycle_empty_eligibility_and_missing_sub_target() {
        let mut rows = rows(
            &[1, 2],
            &[None, Some(1)],
            &[1, 2],
            &[1., 0.1],
            &[10., 1.],
            &[1, 2],
        );
        assert!(plan(&rows, &rows, 20., 0.).is_err());
        assert!(plan(&rows, &rows, 0., 0.5).is_err());
        rows[0].id_down = Some(rows[1].id.clone());
        assert!(plan(&rows, &rows, 0., 0.).is_err());
    }
}
