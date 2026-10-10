//! Aligned raster preparation, canonical mini ownership, and matching drainage.
use super::{
    execution::{CACHE_BYTES, CacheBudget, MIB},
    io::{self, BLOCK, vector},
    model::{Grid, SourceId, Window, topological_order},
};
use anyhow::{Context, Result, bail, ensure};
use gdal::{
    Dataset, DriverManager, Metadata,
    raster::{Buffer, GdalDataType, GdalType, MergeAlgorithm, RasterizeOptions, rasterize},
    vector::{FieldValue, Geometry, LayerAccess, OGRFieldType},
};
use geos::{Geom, Geometry as GeosGeometry, STRtree, SpatialIndex};
use serde::Serialize;
use std::{
    collections::{BTreeMap, HashMap, VecDeque},
    fs,
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
        mpsc,
    },
    thread,
};

const GEOMETRY_CACHE_BYTES: usize = 8 * MIB;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum RasterKind {
    Continuous,
    Categorical,
}

#[derive(Debug, Clone, Serialize)]
pub struct NamedRaster {
    pub name: String,
    pub path: PathBuf,
    pub kind: RasterKind,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, clap::ValueEnum)]
#[serde(rename_all = "lowercase")]
pub enum D8Encoding {
    Canonical,
    Esri,
}

#[derive(Debug, Clone, Serialize)]
pub struct PreparationSpec {
    pub dem: PathBuf,
    pub mini_catchments: PathBuf,
    pub mini_segments: PathBuf,
    pub output_dir: PathBuf,
    pub rasters: Vec<NamedRaster>,
    pub d8: Option<PathBuf>,
    pub d8_encoding: Option<D8Encoding>,
    pub dem_scale: f64,
    pub workers: usize,
    /// Application allocation budget in MiB, not a hard RSS ceiling.
    pub memory_limit_mb: usize,
    pub overwrite: bool,
    pub io_slots: usize,
}

#[derive(Debug)]
pub struct PreparationReport {
    pub dem: PathBuf,
    pub rasters: BTreeMap<String, PathBuf>,
    pub d8: Option<PathBuf>,
    pub grid_catchments: PathBuf,
    pub grid_segments: PathBuf,
    pub manifest: PathBuf,
    pub mini_count: usize,
    pub workers_used: usize,
    pub timings: super::execution::StageTimings,
}

struct Mini {
    #[cfg(test)]
    wkb: Vec<u8>,
    fid: u64,
    geometry_bytes: usize,
    bounds: [f64; 4],
    downstream: Option<i32>,
}

fn integer(value: Option<FieldValue>) -> Result<Option<i32>> {
    match value {
        None => Ok(None),
        Some(FieldValue::IntegerValue(v)) => Ok(Some(v)),
        Some(FieldValue::Integer64Value(v)) => {
            Ok(Some(v.try_into().context("Mini ID exceeds int32")?))
        }
        _ => bail!("Mini IDs and downstream targets must be integers"),
    }
}

fn read_minis(
    path: &Path,
    polygon: bool,
    budget: usize,
    retained: &mut usize,
) -> Result<(String, Vec<Mini>)> {
    let provider = vector::Provider::open(path, None, None)?;
    let mut layer = provider.layer()?;
    let id_col = vector::field(&layer, "id")?;
    let down_col = if polygon {
        None
    } else {
        Some(vector::field(&layer, "id_down")?)
    };
    for col in std::iter::once(id_col).chain(down_col) {
        let ty = vector::id_type(&layer, col)?;
        let subtype = unsafe {
            gdal_sys::OGR_Fld_GetSubType(gdal_sys::OGR_FD_GetFieldDefn(
                layer.defn().c_defn(),
                col.try_into()?,
            ))
        };
        ensure!(
            [OGRFieldType::OFTInteger, OGRFieldType::OFTInteger64].contains(&ty) && subtype == 0,
            "Mini IDs and downstream targets must be integer fields"
        );
    }
    let mut rows = BTreeMap::new();

    for feature in layer.features() {
        let id = integer(vector::value(&feature, id_col)?)?.context("Null mini ID")?;
        ensure!(
            id > 0 && !rows.contains_key(&id),
            "Mini IDs must be positive and unique"
        );
        let downstream = down_col
            .map(|col| integer(vector::value(&feature, col)?))
            .transpose()?
            .flatten();
        ensure!(
            !unsafe { gdal_sys::OGR_F_GetGeometryRef(feature.c_feature()) }.is_null(),
            "Missing mini geometry"
        );
        let geometry = feature.geometry().context("Missing mini geometry")?;
        ensure!(!geometry.is_empty(), "Empty mini geometry");
        let wkb = geometry.wkb()?;
        *retained = retained
            .checked_add(256)
            .context("Mini metadata overflow")?;
        ensure!(
            *retained < budget,
            "Mini metadata exceeds application memory budget"
        );
        vector::validate_geometry(&wkb, polygon)?;
        let e = geometry.envelope();
        let bounds = [e.MinX, e.MinY, e.MaxX, e.MaxY];
        ensure!(
            bounds.iter().all(|v| v.is_finite()),
            "Mini geometry has non-finite bounds"
        );
        rows.insert(
            id,
            Mini {
                #[cfg(test)]
                wkb: Vec::new(),
                fid: feature.fid().context("Mini has no feature ID")?,
                geometry_bytes: wkb.len(),
                bounds,
                downstream,
            },
        );
    }
    ensure!(
        !rows.is_empty() && rows.keys().copied().eq(1..=i32::try_from(rows.len())?),
        "Mini IDs must be dense integers 1..N"
    );
    Ok((provider.crs, rows.into_values().collect()))
}

fn graph(segments: &[Mini]) -> Result<Vec<Option<usize>>> {
    let downstream: Vec<_> = segments
        .iter()
        .map(|row| match row.downstream {
            None | Some(-1) => Ok(None),
            Some(id) if id > 0 && id as usize <= segments.len() => Ok(Some(id as usize - 1)),
            Some(id) => bail!("Missing mini downstream target {id}"),
        })
        .collect::<Result<_>>()?;
    let ids: Vec<_> = (1..=segments.len())
        .map(|id| SourceId::Integer(id as i64))
        .collect();
    topological_order(&ids, &downstream)?;
    Ok(downstream)
}

fn grid(dem: &Dataset, crs: &str, rows: impl Iterator<Item = [f64; 4]>) -> Result<Grid> {
    ensure!(
        dem.raster_count() == 1 && dem.spatial_ref()? == io::spatial_ref(crs)?,
        "DEM must be single-band with the authoritative CRS"
    );
    let source = io::canonical_grid(dem)?;
    let bounds = rows.fold(
        [
            f64::INFINITY,
            f64::INFINITY,
            f64::NEG_INFINITY,
            f64::NEG_INFINITY,
        ],
        |mut a, b| {
            a[0] = a[0].min(b[0]);
            a[1] = a[1].min(b[1]);
            a[2] = a[2].max(b[2]);
            a[3] = a[3].max(b[3]);
            a
        },
    );
    let t = source.transform;
    let snap = |v: f64| {
        if (v - v.round()).abs() <= 1e-7 {
            v.round()
        } else {
            v
        }
    };
    let x0 = snap((bounds[0] - t[0]) / t[1]).floor();
    let y0 = snap((bounds[3] - t[3]) / t[5]).floor();
    let x1 = snap((bounds[2] - t[0]) / t[1]).ceil();
    let y1 = snap((bounds[1] - t[3]) / t[5]).ceil();
    ensure!(
        x0 >= 0. && y0 >= 0. && x1 <= source.width as f64 && y1 <= source.height as f64,
        "DEM does not cover the mini polygon/segment domain"
    );
    let x0 = (x0 - 1.).max(0.) as usize;
    let y0 = (y0 - 1.).max(0.) as usize;
    let x1 = (x1 + 1.).min(source.width as f64) as usize;
    let y1 = (y1 + 1.).min(source.height as f64) as usize;
    ensure!(x1 > x0 && y1 > y0, "Empty prepared grid");
    Ok(Grid {
        transform: [
            t[0] + x0 as f64 * t[1],
            t[1],
            0.,
            t[3] + y0 as f64 * t[5],
            0.,
            t[5],
        ],
        width: x1 - x0,
        height: y1 - y0,
        wkt: crs.to_owned(),
    })
}

fn source_offset(source: &Dataset, grid: &Grid) -> Result<(usize, usize)> {
    ensure!(
        source.raster_count() == 1 && source.spatial_ref()? == io::spatial_ref(&grid.wkt)?,
        "Source raster must be single-band with the authoritative CRS"
    );
    let t = io::canonical_grid(source)?.transform;
    let close = |a: f64, b: f64| (a - b).abs() <= 1e-9 * a.abs().max(b.abs());
    ensure!(
        close(t[1], grid.transform[1]) && close(t[5], grid.transform[5]),
        "Source resolution differs from DEM"
    );
    let x = (grid.transform[0] - t[0]) / t[1];
    let y = (grid.transform[3] - t[3]) / t[5];
    let aligned = |v: f64| (v - v.round()).abs() <= 1e-7_f64.max(1e-9 * v.abs());
    ensure!(
        aligned(x) && aligned(y),
        "Source origin is not aligned to DEM grid"
    );
    let (width, height) = source.raster_size();
    ensure!(
        x.round() >= 0.
            && y.round() >= 0.
            && x.round() + grid.width as f64 <= width as f64
            && y.round() + grid.height as f64 <= height as f64,
        "Source raster does not cover prepared grid"
    );
    Ok((x.round() as usize, y.round() as usize))
}

struct Index {
    tree: STRtree<usize>,
    _geometries: Vec<GeosGeometry>,
    exact_geometries: Vec<Option<GeosGeometry>>,
    exact_wkb: Vec<Option<Vec<u8>>>,
    burns: Vec<Geometry>,
    ids: Vec<usize>,
}

struct GeometryCache {
    entries: HashMap<(bool, u64), Arc<[u8]>>,
    order: VecDeque<(bool, u64)>,
    bytes: usize,
    limit: usize,
}

impl GeometryCache {
    fn new(limit: usize) -> Self {
        Self {
            entries: HashMap::new(),
            order: VecDeque::new(),
            bytes: 0,
            limit,
        }
    }

    fn get(&mut self, polygon: bool, fid: u64) -> Option<Arc<[u8]>> {
        let key = (polygon, fid);
        let geometry = self.entries.get(&key)?.clone();
        if let Some(position) = self.order.iter().position(|&cached| cached == key) {
            self.order.remove(position);
        }
        self.order.push_back(key);
        Some(geometry)
    }

    fn insert(&mut self, polygon: bool, fid: u64, wkb: Vec<u8>) -> Arc<[u8]> {
        if let Some(geometry) = self.get(polygon, fid) {
            return geometry;
        }
        let size = wkb.len().saturating_add(64);
        if size > self.limit {
            return Arc::from(wkb);
        }
        while self.bytes.saturating_add(size) > self.limit {
            let Some(oldest) = self.order.pop_front() else {
                break;
            };
            if let Some(geometry) = self.entries.remove(&oldest) {
                self.bytes -= geometry.len().saturating_add(64);
            }
        }
        let geometry: Arc<[u8]> = Arc::from(wkb);
        self.bytes += size;
        let key = (polygon, fid);
        self.order.push_back(key);
        self.entries.insert(key, geometry.clone());
        geometry
    }
}

fn geometry_wkb(
    polygon: bool,
    fid: u64,
    layer: &mut gdal::vector::Layer<'_>,
    slots: &super::execution::IoSlots,
    cache: &Mutex<GeometryCache>,
) -> Result<Arc<[u8]>> {
    if let Some(geometry) = cache
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .get(polygon, fid)
    {
        return Ok(geometry);
    }
    let _permit = slots.acquire()?;
    let feature = layer
        .feature(fid)
        .context("Mini disappeared while preparing raster window")?;
    let wkb = feature.geometry().context("Missing mini geometry")?.wkb()?;
    drop(_permit);
    Ok(cache
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .insert(polygon, fid, wkb))
}

impl Index {
    #[cfg(test)]
    fn new(rows: &[Mini], polygon: bool) -> Result<Self> {
        let mut tree = STRtree::with_capacity(10)?;
        let mut geometries = Vec::with_capacity(rows.len());
        let mut exact_wkb = Vec::with_capacity(rows.len());
        let mut burns = Vec::with_capacity(rows.len());
        for (i, row) in rows.iter().enumerate() {
            let b = row.bounds;
            let geometry = GeosGeometry::create_rectangle(b[0], b[1], b[2], b[3])?;
            tree.insert(&geometry, i);
            geometries.push(geometry);
            exact_wkb.push(polygon.then(|| row.wkb.clone()));
            burns.push(Geometry::from_wkb(&row.wkb)?);
        }
        Ok(Self {
            tree,
            _geometries: geometries,
            exact_geometries: std::iter::repeat_with(|| None).take(rows.len()).collect(),
            exact_wkb,
            burns,
            ids: (0..rows.len()).collect(),
        })
    }
    fn hits(&self, query: &GeosGeometry) -> Vec<usize> {
        let mut hits = Vec::new();
        self.tree.query(query, |id| hits.push(*id));
        hits.sort_unstable();
        hits
    }

    fn bounds(rows: &[Mini]) -> Result<Self> {
        let mut tree = STRtree::with_capacity(10)?;
        let mut geometries = Vec::with_capacity(rows.len());
        for (i, row) in rows.iter().enumerate() {
            let b = row.bounds;
            let geometry = GeosGeometry::create_rectangle(b[0], b[1], b[2], b[3])?;
            tree.insert(&geometry, i);
            geometries.push(geometry);
        }
        Ok(Self {
            tree,
            _geometries: geometries,
            exact_geometries: std::iter::repeat_with(|| None).take(rows.len()).collect(),
            exact_wkb: std::iter::repeat_with(|| None).take(rows.len()).collect(),
            burns: Vec::new(),
            ids: (0..rows.len()).collect(),
        })
    }

    fn exact_geometry(&mut self, id: usize) -> Result<&GeosGeometry> {
        if self.exact_geometries[id].is_none() {
            let bytes = self.exact_wkb[id]
                .as_deref()
                .context("Missing exact geometry for ownership collision")?;
            self.exact_geometries[id] = Some(GeosGeometry::new_from_wkb(bytes)?);
        }
        Ok(self.exact_geometries[id].as_ref().unwrap())
    }
}

fn block_grid(grid: &Grid, w: Window) -> Grid {
    let t = grid.transform;
    Grid {
        transform: [
            t[0] + w.x as f64 * t[1],
            t[1],
            0.,
            t[3] + w.y as f64 * t[5],
            0.,
            t[5],
        ],
        width: w.width,
        height: w.height,
        wkt: grid.wkt.clone(),
    }
}

fn burn<T: GdalType + Copy>(
    index: &Index,
    hits: &[usize],
    grid: &Grid,
    touched: bool,
    occupancy: bool,
) -> Result<Vec<T>> {
    let mut ds = DriverManager::get_driver_by_name("MEM")?.create_with_band_type::<T, _>(
        "",
        grid.width,
        grid.height,
        1,
    )?;
    ds.set_geo_transform(&grid.transform)?;
    let geometries: Vec<_> = hits.iter().rev().map(|&i| index.burns[i].clone()).collect();
    let labels: Vec<_> = hits
        .iter()
        .rev()
        .map(|&i| {
            if occupancy {
                1.
            } else {
                (index.ids[i] + 1) as f64
            }
        })
        .collect();
    if !hits.is_empty() {
        rasterize(
            &mut ds,
            &[1],
            &geometries,
            &labels,
            Some(RasterizeOptions {
                all_touched: touched,
                merge_algorithm: if occupancy {
                    MergeAlgorithm::Add
                } else {
                    MergeAlgorithm::Replace
                },
                ..Default::default()
            }),
        )?;
    }
    Ok(ds
        .rasterband(1)?
        .read_as::<T>(
            (0, 0),
            (grid.width, grid.height),
            (grid.width, grid.height),
            None,
        )?
        .data()
        .to_vec())
}

fn ownership(index: &mut Index, hits: &[usize], grid: &Grid) -> Result<Vec<i32>> {
    let mut owners = burn::<i32>(index, hits, grid, false, false)?;
    let occupancy = burn::<u32>(index, hits, grid, false, true)?;
    for (cell, &count) in occupancy.iter().enumerate() {
        if count <= 1 && (count == 0 || owners[cell] != 0) {
            continue;
        }
        let row = cell / grid.width;
        let col = cell % grid.width;
        let t = grid.transform;
        let point = GeosGeometry::new_from_wkt(&format!(
            "POINT ({} {})",
            t[0] + (col as f64 + 0.5) * t[1],
            t[3] + (row as f64 + 0.5) * t[5]
        ))?;
        let mut interior = None;
        let mut boundary = None;
        for i in index.hits(&point) {
            if index.exact_geometry(i)?.contains(&point)? {
                ensure!(
                    interior.is_none(),
                    "Mini catchments overlap at raster cell {row},{col}"
                );
                interior = Some(index.ids[i] as i32 + 1);
            } else if index.exact_geometry(i)?.covers(&point)? {
                boundary = Some(boundary.map_or(index.ids[i] as i32 + 1, |v: i32| {
                    v.min(index.ids[i] as i32 + 1)
                }));
            }
        }
        owners[cell] = if let Some(id) = interior.or(boundary) {
            id
        } else {
            let mut counts = BTreeMap::<i32, usize>::new();
            for y in row.saturating_sub(1)..=(row + 1).min(grid.height - 1) {
                for x in col.saturating_sub(1)..=(col + 1).min(grid.width - 1) {
                    let id = owners[y * grid.width + x];
                    if id > 0 {
                        *counts.entry(id).or_default() += 1;
                    }
                }
            }
            counts
                .into_iter()
                .max_by_key(|&(id, count)| (count, std::cmp::Reverse(id)))
                .context("Rasterized cell has no defensible owner")?
                .0
        };
    }
    Ok(owners)
}

fn winner(values: &[usize], downstream: &[Option<usize>]) -> usize {
    *values
        .iter()
        .filter(|&&id| {
            let mut current = downstream[id];
            while let Some(next) = current {
                if values.binary_search(&next).is_ok() {
                    return false;
                }
                current = downstream[next];
            }
            true
        })
        .min()
        .expect("Acyclic contenders have a downstream survivor")
}

fn drainage(
    index: &Index,
    hits: &[usize],
    grid: &Grid,
    downstream: &[Option<usize>],
    workspace: usize,
) -> Result<Vec<i32>> {
    let mut ids = burn::<i32>(index, hits, grid, true, false)?;
    let occupancy = burn::<u32>(index, hits, grid, true, true)?;
    let cells: Vec<_> = occupancy
        .iter()
        .enumerate()
        .filter_map(|(i, &count)| (count > 1).then_some(i))
        .collect();
    let batch_cells = (workspace / hits.len().max(1) / 16).max(1);
    for batch in cells.chunks(batch_cells) {
        let mut contenders = vec![Vec::new(); batch.len()];
        for &id in hits {
            let mask = burn::<u8>(index, &[id], grid, true, true)?;
            for (list, &cell) in contenders.iter_mut().zip(batch) {
                if mask[cell] != 0 {
                    list.push(index.ids[id]);
                }
            }
        }
        for (&cell, choices) in batch.iter().zip(contenders) {
            ids[cell] = winner(&choices, downstream) as i32 + 1;
        }
    }
    Ok(ids)
}

enum Values {
    Continuous(Vec<f32>),
    Categorical(Vec<i32>),
    D8(Vec<u8>),
}
struct Patch {
    values: Values,
    mask: Vec<u8>,
}
struct Block {
    window: Window,
    owners: Vec<i32>,
    segments: Vec<i32>,
    rasters: Vec<Patch>,
}

#[derive(Clone, Copy)]
enum Kind {
    Continuous,
    Categorical,
    D8(D8Encoding),
}
struct Source {
    name: String,
    path: PathBuf,
    kind: Kind,
}
struct Reader {
    dataset: Dataset,
    offset: (usize, usize),
    narrow: bool,
}
impl Reader {
    fn open(source: &Source, grid: &Grid) -> Result<Self> {
        let dataset = Dataset::open(&source.path)?;
        let offset =
            source_offset(&dataset, grid).with_context(|| source.path.display().to_string())?;
        let ty = dataset.rasterband(1)?.band_type();
        let narrow = (ty.is_integer() && ty.bits() <= 16) || ty == GdalDataType::Float32;
        Ok(Self {
            dataset,
            offset,
            narrow,
        })
    }
    fn patch(&self, source: &Source, w: Window, owners: &[i32], scale: f64) -> Result<Patch> {
        let input = io::read(
            &self.dataset,
            Window {
                x: w.x + self.offset.0,
                y: w.y + self.offset.1,
                ..w
            },
        )?;
        let mut mask: Vec<_> = input
            .mask
            .data()
            .iter()
            .zip(owners)
            .map(|(&m, &id)| if m != 0 && id > 0 { 255 } else { 0 })
            .collect();
        let raw = input.values.data();
        let values = match source.kind {
            Kind::Continuous => {
                let mut values = Vec::with_capacity(raw.len());
                for (i, &v) in raw.iter().enumerate() {
                    if mask[i] == 0 || !v.is_finite() {
                        mask[i] = 0;
                        values.push(0.);
                        continue;
                    }
                    let value = if source.name == "dem" {
                        // Match the reference's narrow float32 multiplication, scaling wide sources before conversion.
                        if self.narrow
                            && scale >= f32::MIN_POSITIVE as f64
                            && scale <= f32::MAX as f64
                        {
                            (v as f32) * (scale as f32)
                        } else {
                            (v * scale) as f32
                        }
                    } else {
                        v as f32
                    };
                    ensure!(
                        value.is_finite(),
                        "{} exceeds finite float32 values",
                        source.name
                    );
                    values.push(value);
                }
                Values::Continuous(values)
            }
            Kind::Categorical => {
                let mut values = Vec::with_capacity(raw.len());
                for (i, &v) in raw.iter().enumerate() {
                    if mask[i] == 0 {
                        values.push(0);
                        continue;
                    }
                    ensure!(
                        v.is_finite()
                            && v.fract() == 0.
                            && v >= i32::MIN as f64
                            && v <= i32::MAX as f64,
                        "Categorical raster {} contains invalid int32 values",
                        source.name
                    );
                    values.push(v as i32);
                }
                Values::Categorical(values)
            }
            Kind::D8(encoding) => {
                let mut values = Vec::with_capacity(raw.len());
                for (i, &v) in raw.iter().enumerate() {
                    values.push(if mask[i] == 0 {
                        0
                    } else {
                        normalize_d8(v, encoding)?
                    });
                }
                Values::D8(values)
            }
        };
        Ok(Patch { values, mask })
    }
}
fn normalize_d8(value: f64, encoding: D8Encoding) -> Result<u8> {
    ensure!(
        value.is_finite() && value.fract() == 0.,
        "Invalid D8 code {value}"
    );
    match encoding {
        D8Encoding::Canonical if (0. ..=8.).contains(&value) => Ok(value as u8),
        D8Encoding::Esri => match value as i64 {
            0 if value == 0. => Ok(0),
            1 => Ok(3),
            2 => Ok(4),
            4 => Ok(5),
            8 => Ok(6),
            16 => Ok(7),
            32 => Ok(8),
            64 => Ok(1),
            128 => Ok(2),
            _ => bail!("Invalid ESRI D8 code {value}"),
        },
        _ => bail!("Invalid canonical D8 code {value}"),
    }
}

fn write<T: GdalType + Copy>(ds: &mut Dataset, w: Window, values: Vec<T>) -> Result<()> {
    ds.rasterband(1)?.write(
        (w.x as isize, w.y as isize),
        (w.width, w.height),
        &mut Buffer::new((w.width, w.height), values),
    )?;
    Ok(())
}
fn write_mask(ds: &mut Dataset, w: Window, mask: Vec<u8>) -> Result<()> {
    ds.rasterband(1)?.open_mask_band()?.write(
        (w.x as isize, w.y as isize),
        (w.width, w.height),
        &mut Buffer::new((w.width, w.height), mask),
    )?;
    Ok(())
}

fn validate_spec(spec: &PreparationSpec) -> Result<usize> {
    ensure!(
        spec.workers > 0 && spec.memory_limit_mb > 0,
        "Workers and memory limit must be positive"
    );
    ensure!(
        spec.dem_scale.is_finite() && spec.dem_scale > 0.,
        "DEM scale must be finite and positive"
    );
    ensure!(
        spec.d8.is_some() == spec.d8_encoding.is_some(),
        "D8 and D8 encoding must be supplied together"
    );
    let mut names = std::collections::BTreeSet::new();
    for raster in &spec.rasters {
        let name = &raster.name;
        ensure!(
            name.as_bytes().first().is_some_and(u8::is_ascii_lowercase)
                && name.bytes().all(|b| b.is_ascii_lowercase()
                    || b.is_ascii_digit()
                    || b == b'_'
                    || b == b'-')
                && ![
                    "dem",
                    "d8",
                    "grid_catchments",
                    "grid_segments",
                    "cells",
                    "drainage"
                ]
                .contains(&name.as_str())
                && names.insert(name),
            "Named raster names must be unique, valid, and non-reserved"
        );
    }
    spec.memory_limit_mb
        .checked_mul(MIB)
        .context("Memory budget overflow")
}

/// Prepare aligned inputs; replacements require `overwrite`.
pub fn prepare_dataset(request: &PreparationSpec) -> Result<PreparationReport> {
    prepare_dataset_with_progress(request, &|_| {})
}

pub fn prepare_dataset_with_progress(
    request: &PreparationSpec,
    progress: super::execution::ProgressCallback<'_>,
) -> Result<PreparationReport> {
    let mut reporter = super::execution::Reporter::new(progress);
    let io_slots = super::execution::IoSlots::new(request.io_slots)?;

    let budget = validate_spec(request)?;
    let mut spec = request.clone();
    spec.dem = vector::absolute_input(&spec.dem)?;
    spec.mini_catchments = vector::absolute_input(&spec.mini_catchments)?;
    spec.mini_segments = vector::absolute_input(&spec.mini_segments)?;
    for raster in &mut spec.rasters {
        raster.path = vector::absolute_input(&raster.path)?;
    }
    spec.d8 = spec.d8.as_deref().map(vector::absolute_input).transpose()?;
    let mut sources = vec![Source {
        name: "dem".into(),
        path: spec.dem.clone(),
        kind: Kind::Continuous,
    }];
    for raster in &spec.rasters {
        sources.push(Source {
            name: raster.name.clone(),
            path: raster.path.clone(),
            kind: match raster.kind {
                RasterKind::Continuous => Kind::Continuous,
                RasterKind::Categorical => Kind::Categorical,
            },
        });
    }
    if let Some(path) = &spec.d8 {
        sources.push(Source {
            name: "d8".into(),
            path: path.clone(),
            kind: Kind::D8(spec.d8_encoding.unwrap()),
        });
    }
    let mut names: Vec<_> = sources.iter().map(|s| format!("{}.tif", s.name)).collect();
    names.extend([
        "grid_catchments.tif".into(),
        "grid_segments.tif".into(),
        "manifest-prepare.json".into(),
    ]);
    let refs: Vec<_> = names.iter().map(String::as_str).collect();
    spec.output_dir = vector::output_paths(&spec.output_dir, &refs, spec.overwrite)?;
    let mut affected = names.clone();
    affected.extend(super::execution::preparation_optional(&spec.output_dir)?);
    super::execution::check_outputs(&spec.output_dir, &affected, spec.overwrite)?;
    let mut inputs = vec![
        spec.dem.as_path(),
        spec.mini_catchments.as_path(),
        spec.mini_segments.as_path(),
    ];
    inputs.extend(spec.rasters.iter().map(|r| r.path.as_path()));
    inputs.extend(spec.d8.as_deref());
    super::execution::protect_inputs(&spec.output_dir, &affected, &inputs)?;
    let _cache = CacheBudget::new()?;
    let mut retained = CACHE_BYTES + 32 * MIB;
    fs::create_dir_all(&spec.output_dir)?;
    let (crs, catchments) = read_minis(&spec.mini_catchments, true, budget, &mut retained)?;
    let (segment_crs, segments) = read_minis(&spec.mini_segments, false, budget, &mut retained)?;
    ensure!(
        io::spatial_ref(&crs)? == io::spatial_ref(&segment_crs)?
            && catchments.len() == segments.len(),
        "Mini vectors must have matching CRS and IDs"
    );
    let downstream = graph(&segments)?;
    let grid = grid(
        &Dataset::open(&spec.dem)?,
        &crs,
        catchments.iter().chain(&segments).map(|m| m.bounds),
    )?;
    for source in &sources {
        Reader::open(source, &grid)?;
    }
    let block_count = grid
        .width
        .div_ceil(BLOCK)
        .checked_mul(grid.height.div_ceil(BLOCK))
        .context("Block count overflow")?;
    let block_bytes = (sources.len() * 15 + 96) * BLOCK * BLOCK;
    let polygon_bounds = Index::bounds(&catchments)?;
    let line_bounds = Index::bounds(&segments)?;
    let mut largest_window_geometry = 0usize;
    let columns = grid.width.div_ceil(BLOCK);
    for ordinal in 0..block_count {
        let x = ordinal % columns * BLOCK;
        let y = ordinal / columns * BLOCK;
        let window = Window {
            x,
            y,
            width: BLOCK.min(grid.width - x),
            height: BLOCK.min(grid.height - y),
        };
        let local = block_grid(&grid, window);
        let t = local.transform;
        let query = GeosGeometry::create_rectangle(
            t[0],
            t[3] + local.height as f64 * t[5],
            t[0] + local.width as f64 * t[1],
            t[3],
        )?;
        let polygon_bytes = polygon_bounds
            .hits(&query)
            .into_iter()
            .try_fold(0usize, |bytes, id| {
                bytes.checked_add(catchments[id].geometry_bytes)
            })
            .context("Preparation geometry allocation overflow")?;
        let line_bytes = line_bounds
            .hits(&query)
            .into_iter()
            .try_fold(0usize, |bytes, id| {
                bytes.checked_add(segments[id].geometry_bytes)
            })
            .context("Preparation geometry allocation overflow")?;
        largest_window_geometry = largest_window_geometry.max(
            polygon_bytes
                .checked_add(line_bytes)
                .context("Preparation geometry allocation overflow")?,
        );
    }
    drop(polygon_bounds);
    drop(line_bounds);
    let index_bytes = catchments
        .len()
        .checked_add(segments.len())
        .and_then(|n| n.checked_mul(512))
        .context("Preparation spatial index allocation overflow")?;
    let worker_base = block_bytes
        .checked_add(32 * MIB)
        .and_then(|bytes| bytes.checked_add(index_bytes))
        .and_then(|bytes| bytes.checked_add(largest_window_geometry.checked_mul(16)?))
        .context("Preparation worker allocation overflow")?;
    let geometry_cache_bytes = (budget / 64).min(GEOMETRY_CACHE_BYTES);
    retained = retained
        .checked_add((8 + sources.len() * 5) * BLOCK * BLOCK)
        .and_then(|bytes| bytes.checked_add(geometry_cache_bytes))
        .context("Coordinator buffer overflow")?;
    let available = budget
        .checked_sub(retained)
        .context("Memory budget cannot hold preparation inputs")?;
    ensure!(
        worker_base <= available,
        "One preparation window requires about {} MiB; increase --memory-limit-mb",
        worker_base.div_ceil(MIB)
    );
    let workers = spec.workers.min(block_count).min(available / worker_base);
    ensure!(
        workers > 0,
        "Memory budget cannot hold one preparation worker"
    );
    let collision_workspace = available / workers - worker_base;
    let geometry_cache = Mutex::new(GeometryCache::new(geometry_cache_bytes));

    fs::create_dir_all(&spec.output_dir)?;
    spec.output_dir = fs::canonicalize(&spec.output_dir)?;
    let staging = tempfile::tempdir_in(&spec.output_dir)?;
    let mut outputs = Vec::new();
    for source in &sources {
        let name = format!("{}.tif", source.name);
        let mut ds = match source.kind {
            Kind::Continuous => io::staging_raster::<f32>(staging.path(), &name, &grid)?,
            Kind::Categorical => io::staging_raster::<i32>(staging.path(), &name, &grid)?,
            Kind::D8(_) => io::staging_raster::<u8>(staging.path(), &name, &grid)?,
        };
        ds.rasterband(1)?.create_mask_band(true)?;
        if source.name == "dem" {
            ds.set_metadata_item("units", "m", "")?;
            ds.set_metadata_item("dem_scale", &format!("{:?}", spec.dem_scale), "")?;
            ensure!(
                unsafe {
                    gdal_sys::GDALSetRasterUnitType(ds.rasterband(1)?.c_rasterband(), c"m".as_ptr())
                } == 0,
                "Cannot set DEM units"
            );
        }
        outputs.push(ds);
    }
    let mut cells = io::staging_raster::<i32>(staging.path(), "grid_catchments.tif", &grid)?;
    let mut streams = io::staging_raster::<i32>(staging.path(), "grid_segments.tif", &grid)?;
    cells.rasterband(1)?.create_mask_band(true)?;
    streams.rasterband(1)?.create_mask_band(true)?;
    let mut bounds = vec![[usize::MAX, usize::MAX, 0, 0]; catchments.len()];
    let next = AtomicUsize::new(0);
    let cancelled = AtomicBool::new(false);
    let active = AtomicUsize::new(0);
    let peak = AtomicUsize::new(0);
    reporter.enter("processing", "Preparing raster blocks");
    let mut completed = 0;
    thread::scope(|scope| -> Result<()> {
        let (sender, receiver) = mpsc::sync_channel(0);
        let mut handles = Vec::new();
        for _ in 0..workers {
            let sender = sender.clone();
            let (
                catchments,
                segments,
                grid,
                sources,
                downstream,
                next,
                cancelled,
                active,
                peak,
                geometry_cache,
            ) = (
                &catchments,
                &segments,
                &grid,
                &sources,
                &downstream,
                &next,
                &cancelled,
                &active,
                &peak,
                &geometry_cache,
            );
            let io_slots = &io_slots;
            let catchment_path = &spec.mini_catchments;
            let segment_path = &spec.mini_segments;
            let scale = spec.dem_scale;
            handles.push(scope.spawn(move || {
                let work = || -> Result<()> {
                    let polygons = Index::bounds(catchments)?;
                    let lines = Index::bounds(segments)?;
                    let polygon_provider = vector::Provider::open(catchment_path, None, None)?;
                    let line_provider = vector::Provider::open(segment_path, None, None)?;
                    let mut polygon_layer = polygon_provider.layer()?;
                    let mut line_layer = line_provider.layer()?;
                    let readers = sources
                        .iter()
                        .map(|s| Reader::open(s, grid))
                        .collect::<Result<Vec<_>>>()?;
                    while !cancelled.load(Ordering::Relaxed) {
                        let ordinal = next.fetch_add(1, Ordering::Relaxed);
                        if ordinal >= block_count {
                            break;
                        }
                        let columns = grid.width.div_ceil(BLOCK);
                        let x = ordinal % columns * BLOCK;
                        let y = ordinal / columns * BLOCK;
                        let window = Window {
                            x,
                            y,
                            width: BLOCK.min(grid.width - x),
                            height: BLOCK.min(grid.height - y),
                        };
                        peak.fetch_max(
                            active.fetch_add(1, Ordering::Relaxed) + 1,
                            Ordering::Relaxed,
                        );
                        let result = || -> Result<Block> {
                            let local = block_grid(grid, window);
                            let t = local.transform;
                            let query = GeosGeometry::create_rectangle(
                                t[0],
                                t[3] + local.height as f64 * t[5],
                                t[0] + local.width as f64 * t[1],
                                t[3],
                            )?;
                            let polygon_ids = polygons.hits(&query);
                            let line_ids = lines.hits(&query);
                            let mut local_polygons = selected_index(
                                catchments,
                                &polygon_ids,
                                true,
                                &mut polygon_layer,
                                io_slots,
                                geometry_cache,
                            )?;
                            let local_lines = selected_index(
                                segments,
                                &line_ids,
                                false,
                                &mut line_layer,
                                io_slots,
                                geometry_cache,
                            )?;
                            let polygon_hits: Vec<_> = (0..polygon_ids.len()).collect();
                            let line_hits: Vec<_> = (0..line_ids.len()).collect();
                            let mut owners = ownership(&mut local_polygons, &polygon_hits, &local)?;
                            let segments = drainage(
                                &local_lines,
                                &line_hits,
                                &local,
                                downstream,
                                collision_workspace,
                            )?;
                            for (owner, &id) in owners.iter_mut().zip(&segments) {
                                if id > 0 {
                                    *owner = id;
                                }
                            }
                            let _permit = io_slots.acquire()?;
                            let rasters = readers
                                .iter()
                                .zip(sources)
                                .map(|(r, s)| r.patch(s, window, &owners, scale))
                                .collect::<Result<_>>()?;
                            Ok(Block {
                                window,
                                owners,
                                segments,
                                rasters,
                            })
                        }();
                        active.fetch_sub(1, Ordering::Relaxed);
                        let failed = result.is_err();
                        if failed {
                            cancelled.store(true, Ordering::Relaxed);
                        }
                        if sender.send(result).is_err() || failed {
                            break;
                        }
                    }
                    Ok(())
                };
                if let Err(error) = work() {
                    cancelled.store(true, Ordering::Relaxed);
                    let _ = sender.send(Err(error));
                }
            }));
        }
        drop(sender);
        let mut failure = None;
        for result in receiver {
            let reduce = || -> Result<()> {
                let block = result?;
                if failure.is_some() {
                    return Ok(());
                }
                let w = block.window;
                for (i, &id) in block.owners.iter().enumerate() {
                    if id == 0 {
                        continue;
                    }
                    let x = w.x + i % w.width;
                    let y = w.y + i / w.width;
                    let b = &mut bounds[id as usize - 1];
                    b[0] = b[0].min(x);
                    b[1] = b[1].min(y);
                    b[2] = b[2].max(x + 1);
                    b[3] = b[3].max(y + 1);
                }
                let mask: Vec<_> = block
                    .owners
                    .iter()
                    .map(|&id| if id > 0 { 255 } else { 0 })
                    .collect();
                write(&mut cells, w, block.owners)?;
                write(&mut streams, w, block.segments)?;
                write_mask(&mut cells, w, mask.clone())?;
                write_mask(&mut streams, w, mask)?;
                for (ds, patch) in outputs.iter_mut().zip(block.rasters) {
                    match patch.values {
                        Values::Continuous(v) => write(ds, w, v)?,
                        Values::Categorical(v) => write(ds, w, v)?,
                        Values::D8(v) => write(ds, w, v)?,
                    }
                    write_mask(ds, w, patch.mask)?;
                }
                completed += 1;
                reporter.advance(completed, Some(block_count));
                Ok(())
            }();
            if let Err(error) = reduce {
                cancelled.store(true, Ordering::Relaxed);
                if failure.is_none() {
                    failure = Some(error);
                }
            }
        }
        for handle in handles {
            if handle.join().is_err() && failure.is_none() {
                failure = Some(anyhow::anyhow!("Preparation worker panicked"));
            }
        }
        if let Some(error) = failure {
            return Err(error);
        }
        Ok(())
    })?;
    reporter.enter("finalizing", "Compressing preparation COGs");
    let index = bounds
        .iter()
        .enumerate()
        .map(|(i, &b)| {
            ensure!(
                b[0] != usize::MAX,
                "Mini {} has no rasterized ownership cells",
                i + 1
            );
            let t = grid.transform;
            Ok((
                i + 1,
                t[0] + b[0] as f64 * t[1],
                t[3] + b[3] as f64 * t[5],
                t[0] + b[2] as f64 * t[1],
                t[3] + b[1] as f64 * t[5],
            ))
        })
        .collect::<Result<Vec<_>>>()?;
    cells.set_metadata_item("mini_index", &serde_json::to_string(&index)?, "")?;
    for (source, ds) in sources.iter().zip(outputs.iter_mut()) {
        ds.flush_cache()?;
        let name = format!("{}.tif", source.name);
        io::finish_cog(
            ds,
            &staging.path().join(&name),
            workers,
            Some(if matches!(source.kind, Kind::Continuous) {
                "BILINEAR"
            } else {
                "NEAREST"
            }),
        )?;
        let output = Dataset::open(staging.path().join(&name))?;
        io::validate_raster(
            &output,
            &staging.path().join(&name),
            &grid,
            match source.kind {
                Kind::Continuous => "Float32",
                Kind::Categorical => "Int32",
                Kind::D8(_) => "Byte",
            },
            source.name == "dem",
        )?;
    }
    for (name, ds) in [
        ("grid_catchments.tif", &mut cells),
        ("grid_segments.tif", &mut streams),
    ] {
        ds.flush_cache()?;
        io::finish_cog(ds, &staging.path().join(name), workers, Some("NEAREST"))?;
        io::validate_raster(
            &Dataset::open(staging.path().join(name))?,
            &staging.path().join(name),
            &grid,
            "Int32",
            false,
        )?;
    }
    let workers_used = peak.load(Ordering::Relaxed);
    let products: Vec<_> = refs
        .iter()
        .copied()
        .filter(|&name| name != "manifest-prepare.json")
        .collect();
    let manifest = vector::finish(
        staging.path(),
        &spec.output_dir,
        "prepare",
        &spec,
        &products,
        workers_used,
        reporter.elapsed_seconds(),
    )?;
    let timings = reporter.finish();
    Ok(PreparationReport {
        timings,
        dem: spec.output_dir.join("dem.tif"),
        rasters: spec
            .rasters
            .iter()
            .map(|r| {
                (
                    r.name.clone(),
                    spec.output_dir.join(format!("{}.tif", r.name)),
                )
            })
            .collect(),
        d8: spec.d8.map(|_| spec.output_dir.join("d8.tif")),
        grid_catchments: spec.output_dir.join("grid_catchments.tif"),
        grid_segments: spec.output_dir.join("grid_segments.tif"),
        manifest,
        mini_count: catchments.len(),
        workers_used,
    })
}

fn selected_index(
    rows: &[Mini],
    ids: &[usize],
    polygon: bool,
    layer: &mut gdal::vector::Layer<'_>,
    io_slots: &super::execution::IoSlots,
    geometry_cache: &Mutex<GeometryCache>,
) -> Result<Index> {
    let mut tree = STRtree::with_capacity(10)?;
    let mut geometries = Vec::with_capacity(ids.len());
    let mut exact_wkb = Vec::with_capacity(ids.len());
    let mut burns = Vec::with_capacity(ids.len());
    for (local_id, &id) in ids.iter().enumerate() {
        let wkb = geometry_wkb(polygon, rows[id].fid, layer, io_slots, geometry_cache)?;
        let b = rows[id].bounds;
        let geometry = GeosGeometry::create_rectangle(b[0], b[1], b[2], b[3])?;
        tree.insert(&geometry, local_id);
        geometries.push(geometry);
        exact_wkb.push(polygon.then(|| wkb.to_vec()));
        burns.push(Geometry::from_wkb(&wkb)?);
    }
    Ok(Index {
        tree,
        _geometries: geometries,
        exact_geometries: std::iter::repeat_with(|| None).take(ids.len()).collect(),
        exact_wkb,
        burns,
        ids: ids.to_vec(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mini(wkt: &str, downstream: Option<i32>) -> Result<Mini> {
        let geometry = Geometry::from_wkt(wkt)?;
        let e = geometry.envelope();
        Ok(Mini {
            #[cfg(test)]
            wkb: geometry.wkb()?,
            fid: 0,
            geometry_bytes: 0,
            bounds: [e.MinX, e.MinY, e.MaxX, e.MaxY],
            downstream,
        })
    }

    #[test]
    fn geometry_cache_is_bounded_and_keeps_recent_entries() {
        let mut cache = GeometryCache::new(136);
        cache.insert(true, 1, vec![1; 4]);
        cache.insert(true, 2, vec![2; 4]);
        assert_eq!(&*cache.get(true, 1).unwrap(), &[1; 4]);
        cache.insert(true, 3, vec![3; 4]);
        assert!(cache.get(true, 2).is_none());
        assert_eq!(&*cache.get(true, 1).unwrap(), &[1; 4]);
        assert_eq!(&*cache.get(true, 3).unwrap(), &[3; 4]);
        assert!(cache.bytes <= cache.limit);

        cache.insert(true, 4, vec![4; 73]);
        assert!(cache.get(true, 4).is_none());
        assert!(cache.bytes <= cache.limit);
        cache.insert(false, 1, vec![5; 4]);
        assert_eq!(&*cache.get(false, 1).unwrap(), &[5; 4]);
    }

    fn test_grid(width: usize, height: usize) -> Result<Grid> {
        Ok(Grid {
            transform: [0., 1., 0., height as f64, 0., -1.],
            width,
            height,
            wkt: gdal::spatial_ref::SpatialRef::from_epsg(3857)?.to_wkt()?,
        })
    }

    #[test]
    fn shared_boundaries_holes_and_true_overlap() -> Result<()> {
        let grid = test_grid(4, 2)?;
        let rows = vec![
            mini("POLYGON ((0 0,1.5 0,1.5 2,0 2,0 0))", None)?,
            mini("POLYGON ((1.5 0,4 0,4 2,1.5 2,1.5 0))", None)?,
        ];
        let mut index = Index::new(&rows, true)?;
        assert_eq!(
            ownership(&mut index, &[0, 1], &grid)?,
            vec![1, 1, 2, 2, 1, 1, 2, 2]
        );
        let rows = vec![
            mini("POLYGON ((0 0,3 0,3 2,0 2,0 0))", None)?,
            mini("POLYGON ((1 0,4 0,4 2,1 2,1 0))", None)?,
        ];
        let mut index = Index::new(&rows, true)?;
        assert!(
            ownership(&mut index, &[0, 1], &grid)
                .unwrap_err()
                .to_string()
                .contains("overlap")
        );
        let rows = vec![
            mini("POLYGON ((0 0,1.5 0,1.5 2,0 2,0 0))", None)?,
            mini("POLYGON ((1 0,4 0,4 2,1 2,1 0))", None)?,
        ];
        let mut index = Index::new(&rows, true)?;
        assert_eq!(
            ownership(&mut index, &[0, 1], &grid)?,
            vec![1, 2, 2, 2, 1, 2, 2, 2]
        );
        let grid = test_grid(3, 3)?;
        let rows = vec![mini(
            "POLYGON ((0 0,3 0,3 3,0 3,0 0),(1 1,1 2,2 2,2 1,1 1))",
            None,
        )?];
        let mut index = Index::new(&rows, true)?;
        assert_eq!(
            ownership(&mut index, &[0], &grid)?,
            vec![1, 1, 1, 1, 0, 1, 1, 1, 1]
        );
        Ok(())
    }

    #[test]
    fn collisions_follow_ancestry_and_lowest_id_with_noncontending_connectors() -> Result<()> {
        let rows = (0..3)
            .map(|_| mini("LINESTRING (0 0.5,2 0.5)", None))
            .collect::<Result<Vec<_>>>()?;
        let index = Index::new(&rows, false)?;
        let grid = test_grid(2, 1)?;
        for (downstream, expected) in [
            (vec![Some(1), Some(2), None], 3),
            (vec![None, None, None], 1),
            (vec![Some(2), None, None], 2),
        ] {
            assert_eq!(
                drainage(&index, &[0, 1, 2], &grid, &downstream, MIB)?,
                vec![expected, expected]
            );
        }
        assert_eq!(
            drainage(&index, &[0, 2], &grid, &[Some(1), Some(2), None], MIB)?,
            vec![3, 3]
        );
        assert_eq!(
            drainage(&index, &[0, 1, 2], &grid, &[None, None, None], 0)?,
            drainage(&index, &[0, 1, 2], &grid, &[None, None, None], usize::MAX)?
        );
        Ok(())
    }

    #[test]
    fn pixel_and_block_junctions_match_global_rasterization() -> Result<()> {
        let rows = vec![
            mini("LINESTRING (0.25 0.25,512 6,1029.75 11.75)", Some(2))?,
            mini("LINESTRING (512 0,512 12)", None)?,
            mini("LINESTRING (0 6,1030 6)", None)?,
        ];
        let index = Index::new(&rows, false)?;
        let downstream = graph(&rows)?;
        let grid = test_grid(1030, 12)?;
        let expected = drainage(&index, &[0, 1, 2], &grid, &downstream, 16 * MIB)?;
        let mut actual = vec![0; expected.len()];
        for w in io::windows(Window {
            x: 0,
            y: 0,
            width: grid.width,
            height: grid.height,
        }) {
            let local = block_grid(&grid, w);
            let t = local.transform;
            let query = GeosGeometry::create_rectangle(
                t[0],
                t[3] + local.height as f64 * t[5],
                t[0] + local.width as f64 * t[1],
                t[3],
            )?;
            let values = drainage(&index, &index.hits(&query), &local, &downstream, 16 * MIB)?;
            for row in 0..w.height {
                actual[row * grid.width + w.x..row * grid.width + w.x + w.width]
                    .copy_from_slice(&values[row * w.width..(row + 1) * w.width]);
            }
        }
        assert_eq!(actual, expected);
        Ok(())
    }

    #[test]
    fn sinks_missing_targets_cycles_and_d8_codes() -> Result<()> {
        let mut rows = vec![
            mini("LINESTRING (0 0,1 1)", Some(2))?,
            mini("LINESTRING (1 1,2 2)", None)?,
        ];
        assert_eq!(graph(&rows)?, vec![Some(1), None]);
        rows[1].downstream = Some(-1);
        assert_eq!(graph(&rows)?, vec![Some(1), None]);
        rows[1].downstream = Some(1);
        assert!(graph(&rows).is_err());
        rows[1].downstream = Some(7);
        assert!(graph(&rows).is_err());
        for i in 0..=8 {
            assert_eq!(normalize_d8(i as f64, D8Encoding::Canonical)?, i);
        }
        for (raw, expected) in [
            (0, 0),
            (1, 3),
            (2, 4),
            (4, 5),
            (8, 6),
            (16, 7),
            (32, 8),
            (64, 1),
            (128, 2),
        ] {
            assert_eq!(normalize_d8(raw as f64, D8Encoding::Esri)?, expected);
        }
        for value in [-1., 0.5, 9., f64::INFINITY, f64::NAN] {
            assert!(normalize_d8(value, D8Encoding::Canonical).is_err());
        }
        for value in [-1., 0.5, 3., 9., f64::INFINITY, f64::NAN] {
            assert!(normalize_d8(value, D8Encoding::Esri).is_err());
        }
        Ok(())
    }
}
