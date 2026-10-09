//! Mini-basin sampling: inputs, validation, native statistics, and stage coordination.
use super::{
    execution::{CACHE_BYTES, CacheBudget, MIB, publish},
    io::{self, read, spatial_ref, windows},
    model::{ATTRIBUTES, Attributes, Grid, Window},
};
use anyhow::{Context, Result, bail, ensure};
use gdal::{
    Dataset, Metadata,
    spatial_ref::{AxisMappingStrategy, CoordTransform, SpatialRef},
    vector::{LayerAccess, OGRFieldType},
};
use geos::{Geom, Geometry, GeometryTypes};
use serde::Serialize;
use std::{
    collections::BTreeMap,
    fs::{self, File},
    path::{Path, PathBuf},
    thread,
};
/// Explicit inputs to mini sampling. Raster values are already in metres.
#[derive(Debug, Clone, Serialize)]
pub struct SamplingSpec {
    pub mini_catchments: PathBuf,
    pub mini_segments: PathBuf,
    pub dem: PathBuf,
    pub grid_catchments: PathBuf,
    pub grid_segments: PathBuf,
    pub hand: PathBuf,
    pub ltnd: PathBuf,
    pub hru: PathBuf,
    pub output_dir: PathBuf,
    /// Maximum concurrent workers; actual concurrency may be lower to fit memory.
    pub workers: usize,
    /// Application allocation budget in MiB, not a hard process RSS ceiling.
    pub memory_limit_mb: usize,
}

impl SamplingSpec {
    pub(crate) fn rasters(&self) -> [&Path; 6] {
        [
            &self.dem,
            &self.grid_catchments,
            &self.grid_segments,
            &self.hand,
            &self.ltnd,
            &self.hru,
        ]
    }
}

/// Paths and counts produced by a successful sampling run.
#[derive(Debug)]
pub struct SamplingReport {
    pub sampled_minis: PathBuf,
    pub manifest: PathBuf,
    pub nodata_reports: Vec<PathBuf>,
    pub mini_count: usize,
    pub workers_used: usize,
}

pub(crate) const NODATA_NAMES: [&str; 5] = ["dem", "grid_segments", "hand", "ltnd", "hru"];
const WORKER_BYTES: usize = 32 * MIB;

#[derive(Debug)]
pub(crate) struct Mini {
    pub attributes: Attributes,
    pub longitude: f64,
    pub latitude: f64,
    pub reach_length: f64,
    pub window: Window,
    pub owned_cells: usize,
}

impl Mini {
    pub fn id(&self) -> i64 {
        self.attributes.integers[0]
    }
}

pub(crate) struct Statistics {
    pub reach_slope: f64,
    pub reach_elevation: f64,
    pub tributary_length: f64,
    pub tributary_slope: f64,
    pub hru_counts: [u64; 100],
    pub flooded_area: [f64; 100],
}

pub(crate) struct MiniResult {
    pub id: i64,
    pub total_cells: usize,
    pub nodata: [usize; 5],
    pub statistics: Option<Statistics>,
    pub failures: Vec<String>,
}
/// Sample explicit mini, terrain, and HRU inputs into deterministic CSV products.
///
/// Existing sampling products are never overwritten. A completed scan missing
/// required statistics publishes only nodata reports and returns an error.
/// The application budget is conservative, not a hard RSS limit. During the
/// run GDAL's process-wide block-cache limit is capped and restored on exit.
pub fn sample_minibasins(spec: &SamplingSpec) -> Result<SamplingReport> {
    ensure!(
        spec.workers > 0 && spec.memory_limit_mb > 0,
        "Workers and memory limit must be positive"
    );
    let budget = spec
        .memory_limit_mb
        .checked_mul(MIB)
        .context("Memory budget overflow")?;
    ensure!(
        budget >= CACHE_BYTES + WORKER_BYTES,
        "Memory budget needs at least 48 MiB for GIS cache and raster windows"
    );
    check_collisions(&spec.output_dir)?;
    let mut spec = spec.clone();
    for path in [
        &mut spec.mini_catchments,
        &mut spec.mini_segments,
        &mut spec.dem,
        &mut spec.grid_catchments,
        &mut spec.grid_segments,
        &mut spec.hand,
        &mut spec.ltnd,
        &mut spec.hru,
    ] {
        *path = fs::canonicalize(&*path)
            .with_context(|| format!("Input unavailable: {}", path.display()))?;
    }
    spec.output_dir = std::path::absolute(&spec.output_dir)?;
    let _cache = CacheBudget::new()?;
    let (grid, minis) = inspect(&spec)?;
    let largest = minis.iter().try_fold(0, |maximum, mini| -> Result<usize> {
        let bytes = mini
            .owned_cells
            .checked_mul(24)
            .and_then(|v| v.checked_add(WORKER_BYTES))
            .context("Mini allocation size overflow")?;
        Ok(maximum.max(bytes))
    })?;
    let coordinator = minis
        .len()
        .checked_mul(8192)
        .and_then(|v| v.checked_add(CACHE_BYTES))
        .context("Coordinator allocation size overflow")?;
    let available = budget
        .checked_sub(coordinator)
        .context("Memory budget cannot hold mini metadata")?;
    ensure!(
        largest <= available,
        "Oversized mini requires at least {} MiB including buffers; increase --memory-limit-mb",
        (largest + coordinator).div_ceil(MIB)
    );
    let workers = spec.workers.min(minis.len()).min(available / largest);
    let mut results = thread::scope(|scope| -> Result<Vec<MiniResult>> {
        let mut handles = Vec::with_capacity(workers);
        for worker in 0..workers {
            let spec = &spec;
            let grid = &grid;
            let minis = &minis;
            handles.push(scope.spawn(move || -> Result<Vec<MiniResult>> {
                let datasets = open_rasters(spec)?;
                let areas = io::CellAreas::new(grid)?;
                minis
                    .iter()
                    .skip(worker)
                    .step_by(workers)
                    .map(|mini| sample_mini(mini, &datasets, &areas))
                    .collect()
            }));
        }
        let mut results = Vec::with_capacity(minis.len());
        let mut error = None;
        for handle in handles {
            match handle
                .join()
                .map_err(|_| anyhow::anyhow!("Sampling worker panicked"))
                .and_then(|r| r)
            {
                Ok(values) => results.extend(values),
                Err(e) => {
                    if error.is_none() {
                        error = Some(e);
                    }
                }
            }
        }
        if let Some(error) = error {
            return Err(error);
        }
        Ok(results)
    })?;
    results.sort_unstable_by_key(|r| r.id);
    let failures: Vec<_> = results.iter().flat_map(|r| r.failures.iter()).collect();
    fs::create_dir_all(&spec.output_dir)?;
    spec.output_dir = fs::canonicalize(&spec.output_dir)?;
    check_collisions(&spec.output_dir)?;
    let staging = tempfile::tempdir_in(&spec.output_dir)?;
    let mut files = write_nodata(staging.path(), &results)?;
    if !files.is_empty() {
        eprintln!(
            "Warning: Nodata cells were found in {}",
            files
                .iter()
                .map(|name| name.trim_start_matches("nodata_").trim_end_matches(".csv"))
                .collect::<Vec<_>>()
                .join(", ")
        );
    }
    if !failures.is_empty() {
        publish(staging.path(), &spec.output_dir, &files)?;
        let message = failures
            .into_iter()
            .map(String::as_str)
            .collect::<Vec<_>>()
            .join("; ");
        if files.is_empty() {
            bail!("{message}");
        }
        bail!(
            "{message}. Nodata reports saved in {}",
            spec.output_dir.display()
        );
    }
    write_sampled(staging.path(), &minis, &results)?;
    let mut parameters = serde_json::to_value(&spec)?;
    parameters["workers_used"] = serde_json::json!(workers);
    serde_json::to_writer_pretty(
        File::create(staging.path().join("manifest-sample-minis.json"))?,
        &serde_json::json!({"step": "sample-minis", "parameters": parameters}),
    )?;
    let nodata_reports = files
        .iter()
        .map(|name| spec.output_dir.join(name))
        .collect();
    files.extend([
        "sampled_minis.csv".into(),
        "manifest-sample-minis.json".into(),
    ]);
    publish(staging.path(), &spec.output_dir, &files)?;
    Ok(SamplingReport {
        sampled_minis: spec.output_dir.join("sampled_minis.csv"),
        manifest: spec.output_dir.join("manifest-sample-minis.json"),
        nodata_reports,
        mini_count: minis.len(),
        workers_used: workers,
    })
}

fn sample_mini(mini: &Mini, datasets: &[Dataset], areas: &io::CellAreas) -> Result<MiniResult> {
    let mut accumulator = Accumulator::new(mini.owned_cells);
    for window in windows(mini.window) {
        let blocks = datasets
            .iter()
            .map(|source| read(source, window))
            .collect::<Result<Vec<_>>>()?;
        let mut row_area = None;
        for cell in 0..window.width * window.height {
            if blocks[1].value(cell) != Some(mini.id() as f64) {
                continue;
            }
            let values = [0, 2, 3, 4, 5].map(|i| blocks[i].value(cell));
            let row = window.y + cell / window.width;
            let area = if values[2].is_some() {
                if areas.is_geographic() {
                    if row_area.is_none_or(|(r, _)| r != row) {
                        row_area = Some((row, areas.area(window.x, row)?));
                    }
                    row_area.unwrap().1
                } else {
                    areas.area(window.x + cell % window.width, row)?
                }
            } else {
                0.
            };
            accumulator.add(mini.id(), values, area)?;
        }
    }
    ensure!(
        accumulator.total_cells == mini.owned_cells,
        "Mini {} ownership changed during sampling",
        mini.id()
    );
    Ok(accumulator.finish(mini.id(), mini.reach_length))
}

fn check_collisions(output: &Path) -> Result<()> {
    for name in std::iter::once("sampled_minis.csv".to_owned())
        .chain(std::iter::once("manifest-sample-minis.json".to_owned()))
        .chain(NODATA_NAMES.map(|n| format!("nodata_{n}.csv")))
    {
        match fs::symlink_metadata(output.join(&name)) {
            Ok(_) => bail!(
                "Sampling product already exists: {}",
                output.join(name).display()
            ),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.into()),
        }
    }
    Ok(())
}

fn write_nodata(directory: &Path, results: &[MiniResult]) -> Result<Vec<String>> {
    let mut files = Vec::new();
    for (i, name) in NODATA_NAMES.iter().enumerate() {
        if !results.iter().any(|r| r.nodata[i] > 0) {
            continue;
        }
        let name = format!("nodata_{name}.csv");
        let mut writer = csv::Writer::from_path(directory.join(&name))?;
        writer.write_record([
            "mini_id",
            "nodata_cells",
            "total_cells",
            "percentage_nodata",
        ])?;
        for result in results.iter().filter(|r| r.nodata[i] > 0) {
            writer.write_record([
                result.id.to_string(),
                result.nodata[i].to_string(),
                result.total_cells.to_string(),
                float(100. * result.nodata[i] as f64 / result.total_cells as f64),
            ])?;
        }
        writer.flush()?;
        files.push(name);
    }
    Ok(files)
}

// Keep floating columns recognizably floating even when every value is integral.
fn float(value: f64) -> String {
    format!("{value:?}")
}

fn write_sampled(directory: &Path, minis: &[Mini], results: &[MiniResult]) -> Result<()> {
    let classes: Vec<_> = (0..100)
        .filter(|i| {
            results
                .iter()
                .any(|r| r.statistics.as_ref().unwrap().hru_counts[*i] > 0)
        })
        .collect();
    let mut headers: Vec<_> = ATTRIBUTES.iter().map(|name| (*name).to_owned()).collect();
    headers.extend(
        [
            "longitude",
            "latitude",
            "reach_slope",
            "reach_elevation",
            "tributary_length",
            "tributary_slope",
        ]
        .map(str::to_owned),
    );
    headers.extend(classes.iter().map(|id| format!("hru_{}", id + 1)));
    headers.extend((1..=100).map(|stage| format!("flooded_area_{stage}")));
    let mut writer = csv::Writer::from_path(directory.join("sampled_minis.csv"))?;
    writer.write_record(headers)?;
    for (mini, result) in minis.iter().zip(results) {
        ensure!(mini.id() == result.id, "Sampling result ID mismatch");
        let s = result
            .statistics
            .as_ref()
            .context("Incomplete sampling statistics")?;
        let mut row: Vec<_> = mini
            .attributes
            .integers
            .iter()
            .map(i64::to_string)
            .collect();
        row.extend(mini.attributes.metrics.iter().map(|v| float(*v)));
        row.extend(
            [
                mini.longitude,
                mini.latitude,
                s.reach_slope,
                s.reach_elevation,
                s.tributary_length,
                s.tributary_slope,
            ]
            .map(float),
        );
        let total = s.hru_counts.iter().sum::<u64>() as f64;
        row.extend(
            classes
                .iter()
                .map(|i| float(100. * s.hru_counts[*i] as f64 / total)),
        );
        row.extend(s.flooded_area.map(float));
        writer.write_record(row)?;
    }
    writer.flush()?;
    Ok(())
}

fn inspect(spec: &SamplingSpec) -> Result<(Grid, Vec<Mini>)> {
    let datasets = open_rasters(spec)?;
    let dem = &datasets[0];
    let grid = io::canonical_grid(dem)?;
    for (i, source) in datasets.iter().enumerate() {
        io::validate_raster(
            source,
            spec.rasters()[i],
            &grid,
            if matches!(i, 0 | 3 | 4) {
                "Float32"
            } else {
                "Int32"
            },
            matches!(i, 0 | 3 | 4),
        )?;
    }
    ensure!(
        datasets[4].metadata_item("distance_method", "").as_deref() == Some("geodesic"),
        "LTND must declare distance_method=geodesic"
    );
    let index = io::mini_windows(&datasets[1], &grid)?;
    let catchments = read_vectors(&spec.mini_catchments, &grid, false)?;
    let segments = read_vectors(&spec.mini_segments, &grid, true)?;
    ensure!(
        catchments.len() == index.len() && segments.len() == index.len(),
        "Vector IDs and mini_index differ"
    );
    let mut minis = Vec::with_capacity(index.len());
    for (id, window) in index {
        let catchment = catchments
            .get(&id)
            .with_context(|| format!("Missing catchment mini {id}"))?;
        let segment = segments
            .get(&id)
            .with_context(|| format!("Missing segment mini {id}"))?;
        ensure!(
            catchment.0.integers == segment.0.integers
                && catchment.0.metrics[1..] == segment.0.metrics[1..],
            "Mini {id} catchment and segment attributes differ"
        );
        minis.push(Mini {
            attributes: catchment.0.clone(),
            longitude: segment.1[0],
            latitude: segment.1[1],
            reach_length: segment.0.metrics[0],
            window,
            owned_cells: 0,
        });
    }
    validate_ownership(&datasets, &grid, &mut minis)?;
    Ok((grid, minis))
}

fn open_rasters(spec: &SamplingSpec) -> Result<Vec<Dataset>> {
    spec.rasters()
        .iter()
        .map(|path| Dataset::open(path).with_context(|| format!("Open raster {}", path.display())))
        .collect()
}

fn read_vectors(
    path: &Path,
    grid: &Grid,
    segments: bool,
) -> Result<BTreeMap<i64, (Attributes, [f64; 2])>> {
    let dataset = Dataset::open(path).with_context(|| format!("Open vector {}", path.display()))?;
    ensure!(
        dataset.layer_count() == 1,
        "Mini vector must contain exactly one layer"
    );
    let mut layer = dataset.layer(0)?;
    let source = spatial_ref(&grid.wkt)?;
    let mut vector_crs = layer
        .spatial_ref()
        .context("Mini vector must declare a CRS")?;
    vector_crs.set_axis_mapping_strategy(AxisMappingStrategy::TraditionalGisOrder);
    ensure!(
        vector_crs == source,
        "Vector CRS differs from DEM: {}",
        path.display()
    );
    let schema: Vec<_> = layer
        .defn()
        .fields()
        .map(|f| (f.name(), f.field_type()))
        .collect();
    ensure!(
        schema.len() == ATTRIBUTES.len()
            && schema
                .iter()
                .enumerate()
                .all(|(i, (name, ty))| name == ATTRIBUTES[i]
                    && *ty
                        == if i < 4 {
                            OGRFieldType::OFTInteger64
                        } else {
                            OGRFieldType::OFTReal
                        }),
        "Invalid mini vector schema: {} ({schema:?})",
        path.display()
    );
    let mut target = SpatialRef::from_epsg(4326)?;
    target.set_axis_mapping_strategy(AxisMappingStrategy::TraditionalGisOrder);
    let transform = CoordTransform::new(&source, &target)?;
    let mut result = BTreeMap::new();
    for feature in layer.features() {
        let mut integers = [0; 4];
        let mut metrics = [0.; 4];
        for (i, integer) in integers.iter_mut().enumerate() {
            *integer = feature
                .field_as_integer64(i)?
                .context("Null mini integer attribute")?;
        }
        for (i, metric) in metrics.iter_mut().enumerate() {
            *metric = feature
                .field_as_double(i + 4)?
                .context("Null mini metric attribute")?;
        }
        let id = integers[0];
        ensure!(
            id > 0 && metrics.iter().all(|v| v.is_finite()),
            "Mini {id} has invalid attributes"
        );
        let ogr = feature.geometry().context("Mini has no geometry")?;
        let geometry = Geometry::new_from_wkb(&ogr.wkb()?)?;
        let allowed = if segments {
            [GeometryTypes::LineString, GeometryTypes::MultiLineString]
        } else {
            [GeometryTypes::Polygon, GeometryTypes::MultiPolygon]
        };
        ensure!(
            !geometry.is_empty()?
                && geometry.is_valid()?
                && allowed.contains(&geometry.geometry_type()?),
            "Mini {id} has invalid geometry"
        );
        let mut xy = [0.; 2];
        if segments {
            ensure!(
                metrics[0] > 0. && geometry.length()? > 0.,
                "Mini {id} has zero or invalid reach length"
            );
            let point = geometry.point_on_surface()?;
            let mut x = [point.get_x()?];
            let mut y = [point.get_y()?];
            transform.transform_coords(&mut x, &mut y, &mut [])?;
            xy = [x[0], y[0]];
            ensure!(
                xy.iter().all(|v| v.is_finite()),
                "Mini {id} has invalid representative coordinates"
            );
        }
        ensure!(
            result
                .insert(id, (Attributes { integers, metrics }, xy))
                .is_none(),
            "Duplicate mini ID {id}"
        );
    }
    ensure!(!result.is_empty(), "Mini vector is empty");
    Ok(result)
}

fn validate_ownership(datasets: &[Dataset], grid: &Grid, minis: &mut [Mini]) -> Result<()> {
    let ids: BTreeMap<_, _> = minis.iter().enumerate().map(|(i, m)| (m.id(), i)).collect();
    let mut bounds = vec![(usize::MAX, usize::MAX, 0, 0); minis.len()];
    for window in windows(Window {
        x: 0,
        y: 0,
        width: grid.width,
        height: grid.height,
    }) {
        let owners = read(&datasets[1], window)?;
        let segments = read(&datasets[2], window)?;
        for cell in 0..window.width * window.height {
            // Partial segment coverage is sampled and reported rather than rejected.
            let Some(owner) = owners.value(cell) else {
                ensure!(
                    segments.value(cell).is_none(),
                    "Segment validity exists outside catchment ownership"
                );
                continue;
            };
            let id = owner as i64;
            let index = *ids
                .get(&id)
                .with_context(|| format!("Ownership contains unknown mini {id}"))?;
            if let Some(segment) = segments.value(cell) {
                ensure!(
                    segment == 0. || segment == owner,
                    "Positive segment ownership mismatch for mini {id}"
                );
            }
            let x = window.x + cell % window.width;
            let y = window.y + cell / window.width;
            let mini = &mut minis[index];
            ensure!(
                x >= mini.window.x
                    && x < mini.window.x + mini.window.width
                    && y >= mini.window.y
                    && y < mini.window.y + mini.window.height,
                "Ownership lies outside mini {id} index bounds"
            );
            mini.owned_cells += 1;
            let b = &mut bounds[index];
            b.0 = b.0.min(x);
            b.1 = b.1.min(y);
            b.2 = b.2.max(x + 1);
            b.3 = b.3.max(y + 1);
        }
    }
    for (mini, bounds) in minis.iter().zip(bounds) {
        let w = mini.window;
        ensure!(
            mini.owned_cells > 0 && bounds == (w.x, w.y, w.x + w.width, w.y + w.height),
            "Mini {} index bounds are not tight ownership bounds",
            mini.id()
        );
    }
    Ok(())
}

struct Accumulator {
    pub total_cells: usize,
    pub nodata: [usize; 5],
    dem: Vec<f64>,
    paired: Vec<(f64, f64)>,
    hru: [u64; 100],
    floods: [f64; 101],
    hand_count: usize,
}

impl Accumulator {
    pub fn new(cells: usize) -> Self {
        // shortcut: exact samples must fit in memory, add disk-backed samples when oversized minis are needed.
        Self {
            total_cells: 0,
            nodata: [0; 5],
            dem: Vec::with_capacity(cells),
            paired: Vec::with_capacity(cells),
            hru: [0; 100],
            floods: [0.; 101],
            hand_count: 0,
        }
    }

    /// Values are DEM, segment ID, HAND, LTND, HRU; absent means masked or NaN.
    pub fn add(&mut self, id: i64, values: [Option<f64>; 5], area: f64) -> Result<()> {
        self.total_cells += 1;
        for (i, value) in values.iter().enumerate() {
            if value.is_none() {
                self.nodata[i] += 1;
            }
            ensure!(
                value.is_none_or(f64::is_finite),
                "Mini {id} contains infinite sampled values"
            );
        }
        let [dem, segment, hand, ltnd, hru] = values;
        if let Some(segment) = segment {
            ensure!(
                segment == 0. || segment == id as f64,
                "Mini {id} has mismatched positive segment ownership"
            );
            if segment == id as f64
                && let Some(dem) = dem
            {
                self.dem.push(dem);
            }
        }
        if let (Some(hand), Some(ltnd)) = (hand, ltnd) {
            self.paired.push((hand, ltnd));
        }
        if let Some(hru) = hru {
            ensure!(
                (1. ..=100.).contains(&hru) && hru.fract() == 0.,
                "Mini {id} contains invalid HRU class {hru}"
            );
            self.hru[hru as usize - 1] += 1;
        }
        if let Some(hand) = hand {
            ensure!(
                area.is_finite() && area > 0.,
                "Mini {id} has invalid geodesic pixel area"
            );
            self.hand_count += 1;
            let bin = if hand <= 1. {
                0
            } else if hand > 100. {
                100
            } else {
                hand.ceil() as usize - 1
            };
            self.floods[bin] += area;
        }
        Ok(())
    }

    pub fn finish(mut self, id: i64, reach_length: f64) -> MiniResult {
        let mut failures = Vec::new();
        for (valid, name) in [
            (self.hru.iter().sum::<u64>() > 0, "HRU percentages"),
            (self.hand_count > 0, "HAND flooded areas"),
            (!self.dem.is_empty(), "DEM reach statistics"),
            (!self.paired.is_empty(), "paired HAND/LTND statistics"),
        ] {
            if !valid {
                failures.push(format!("Mini {id} has no valid data for {name}"));
            }
        }
        let maximum = self
            .paired
            .iter()
            .map(|(_, ltnd)| *ltnd)
            .fold(f64::NEG_INFINITY, f64::max);
        if !self.paired.is_empty() && maximum <= 0. {
            failures.push(format!("Mini {id} has non-positive maximum LTND"));
        }
        let mut statistics = if failures.is_empty() {
            self.dem.sort_unstable_by(f64::total_cmp);
            let mut sum = 0.;
            let mut count = 0;
            for (hand, ltnd) in self.paired {
                if (ltnd - maximum).abs() <= 1e-8 + 1e-5 * maximum.abs() {
                    sum += hand;
                    count += 1;
                }
            }
            let mut cumulative = 0.;
            let flooded_area = std::array::from_fn(|i| {
                cumulative += self.floods[i];
                cumulative
            });
            Some(Statistics {
                reach_elevation: percentile(&self.dem, 0.5),
                reach_slope: (percentile(&self.dem, 0.85) - percentile(&self.dem, 0.1))
                    / (0.75 * reach_length),
                tributary_length: maximum / 1000.,
                tributary_slope: (sum / count as f64) / (maximum / 1000.),
                hru_counts: self.hru,
                flooded_area,
            })
        } else {
            None
        };
        if statistics.as_ref().is_some_and(|s| {
            ![
                s.reach_slope,
                s.reach_elevation,
                s.tributary_length,
                s.tributary_slope,
            ]
            .into_iter()
            .chain(s.flooded_area)
            .all(f64::is_finite)
        }) {
            failures.push(format!("Mini {id} produced non-finite statistics"));
            statistics = None;
        }
        MiniResult {
            id,
            total_cells: self.total_cells,
            nodata: self.nodata,
            statistics,
            failures,
        }
    }
}

fn percentile(sorted: &[f64], fraction: f64) -> f64 {
    let position = (sorted.len() - 1) as f64 * fraction;
    let lower = position.floor() as usize;
    let upper = position.ceil() as usize;
    sorted[lower] + (sorted[upper] - sorted[lower]) * (position - lower as f64)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn exact_percentiles_ties_and_flood_thresholds() {
        assert_eq!(percentile(&[0., 10.], 0.85), 8.5);
        assert_eq!(percentile(&[7.], 0.1), 7.);
        let mut acc = Accumulator::new(4);
        for (dem, hand, ltnd, hru) in [
            (0., -1., 1000., 1.),
            (10., 1.1, 999.995, 2.),
            (20., 100., 2., 2.),
            (30., 101., 1., 2.),
        ] {
            acc.add(
                12,
                [Some(dem), Some(12.), Some(hand), Some(ltnd), Some(hru)],
                0.1,
            )
            .unwrap();
        }
        let stats = acc.finish(12, 2.).statistics.unwrap();
        assert_eq!(stats.reach_elevation, 15.);
        assert_eq!(stats.reach_slope, 15.);
        assert!((stats.tributary_slope - 0.05).abs() < 1e-15);
        assert_eq!(stats.hru_counts[..2], [1, 3]);
        assert_eq!(stats.flooded_area[0], 0.1);
        assert_eq!(stats.flooded_area[1], 0.2);
        assert!((stats.flooded_area[99] - 0.3).abs() < 1e-15);
    }
    #[test]
    fn missing_pairs_and_invalid_values() {
        let mut acc = Accumulator::new(2);
        acc.add(1, [Some(1.), Some(1.), None, Some(1.), Some(1.)], 1.)
            .unwrap();
        acc.add(1, [Some(1.), Some(1.), Some(2.), None, Some(1.)], 1.)
            .unwrap();
        let result = acc.finish(1, 1.);
        assert!(result.statistics.is_none());
        assert_eq!(result.nodata, [0, 0, 1, 1, 0]);
        assert!(result.failures[0].contains("paired"));
        for values in [
            [Some(f64::INFINITY), None, None, None, None],
            [None, None, None, None, Some(101.)],
            [None, Some(2.), None, None, None],
        ] {
            assert!(Accumulator::new(1).add(1, values, 1.).is_err());
        }
        let mut acc = Accumulator::new(2);
        for dem in [0., 10.] {
            acc.add(1, [Some(dem), Some(1.), Some(1.), Some(1.), Some(1.)], 1.)
                .unwrap();
        }
        let result = acc.finish(1, f64::MIN_POSITIVE);
        assert!(result.statistics.is_none());
        assert!(result.failures[0].contains("non-finite"));
    }
}
