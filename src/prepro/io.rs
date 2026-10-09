//! Shared raster windows, geometry-independent CRS transforms, and cell areas.
use super::model::{Grid, Window};
use anyhow::{Context, Result, bail, ensure};
use gdal::{
    Dataset, Metadata,
    raster::Buffer,
    spatial_ref::{AxisMappingStrategy, CoordTransform, SpatialRef},
};
use geographiclib_rs::{Geodesic, PolygonArea, Winding};
use std::path::Path;
pub(crate) const BLOCK: usize = 512;

pub(crate) fn spatial_ref(wkt: &str) -> Result<SpatialRef> {
    let mut crs = SpatialRef::from_wkt(wkt)?;
    crs.set_axis_mapping_strategy(AxisMappingStrategy::TraditionalGisOrder);
    Ok(crs)
}

pub(crate) fn windows(bounds: Window) -> impl Iterator<Item = Window> {
    (bounds.y..bounds.y + bounds.height)
        .step_by(BLOCK)
        .flat_map(move |y| {
            (bounds.x..bounds.x + bounds.width)
                .step_by(BLOCK)
                .map(move |x| Window {
                    x,
                    y,
                    width: BLOCK.min(bounds.x + bounds.width - x),
                    height: BLOCK.min(bounds.y + bounds.height - y),
                })
        })
}

pub(crate) struct RasterBlock {
    pub values: Buffer<f64>,
    pub mask: Buffer<u8>,
}
impl RasterBlock {
    pub fn value(&self, index: usize) -> Option<f64> {
        let value = self.values.data()[index];
        (self.mask.data()[index] != 0 && !value.is_nan()).then_some(value)
    }
}

pub(crate) fn read(source: &Dataset, window: Window) -> Result<RasterBlock> {
    let band = source.rasterband(1)?;
    let position = (window.x as isize, window.y as isize);
    let size = (window.width, window.height);
    Ok(RasterBlock {
        values: band.read_as(position, size, size, None)?,
        mask: band.open_mask_band()?.read_as(position, size, size, None)?,
    })
}

pub(crate) struct CellAreas {
    transform: CoordTransform,
    geodesic: Geodesic,
    affine: [f64; 6],
    geographic: bool,
}
impl CellAreas {
    pub fn new(grid: &Grid) -> Result<Self> {
        let source = spatial_ref(&grid.wkt)?;
        let mut target = source.geog_cs()?;
        // Geodesic polygon coordinates must be degrees, even for source CRSs in grads.
        let status = unsafe {
            gdal_sys::OSRSetAngularUnits(
                target.to_c_hsrs(),
                c"degree".as_ptr(),
                std::f64::consts::PI / 180.,
            )
        };
        ensure!(
            status == 0,
            "Cannot normalize source geographic CRS to degrees"
        );
        target.set_axis_mapping_strategy(AxisMappingStrategy::TraditionalGisOrder);
        let a = source.semi_major()?;
        let b = source.semi_minor()?;
        ensure!(
            a.is_finite() && b.is_finite() && a > 0. && b > 0.,
            "Source CRS lacks a usable ellipsoid"
        );
        Ok(Self {
            transform: CoordTransform::new(&source, &target)?,
            geodesic: Geodesic::new(a, (a - b) / a),
            affine: grid.transform,
            geographic: source.is_geographic(),
        })
    }
    pub fn is_geographic(&self) -> bool {
        self.geographic
    }
    pub fn area(&self, column: usize, row: usize) -> Result<f64> {
        let t = self.affine;
        let x0 = t[0] + column as f64 * t[1];
        let x1 = x0 + t[1];
        let y0 = t[3] + row as f64 * t[5];
        let y1 = y0 + t[5];
        let mut x = [x0, x1, x1, x0];
        let mut y = [y0, y0, y1, y1];
        self.transform.transform_coords(&mut x, &mut y, &mut [])?;
        let mut polygon = PolygonArea::new(&self.geodesic, Winding::CounterClockwise);
        for i in 0..4 {
            polygon.add_point(y[i], x[i]);
        }
        let (_, area, _) = polygon.compute(true);
        let area = area.abs() / 1e6;
        if !area.is_finite() || area <= 0. {
            bail!("Raster grid produced invalid geodesic cell area");
        }
        Ok(area)
    }
}

pub(crate) fn canonical_grid(dem: &Dataset) -> Result<Grid> {
    let transform = dem.geo_transform()?;
    ensure!(
        transform.iter().all(|v| v.is_finite())
            && transform[1] > 0.
            && transform[5] < 0.
            && transform[2] == 0.
            && transform[4] == 0.,
        "DEM must have a north-up, unrotated grid"
    );
    let (width, height) = dem.raster_size();
    let crs = dem.spatial_ref().context("DEM must declare a CRS")?;
    let grid = Grid {
        transform,
        width,
        height,
        wkt: crs.to_wkt()?,
    };
    Ok(grid)
}

pub(crate) fn validate_raster(
    source: &Dataset,
    path: &Path,
    grid: &Grid,
    dtype: &str,
    metres: bool,
) -> Result<()> {
    let band = source.rasterband(1)?;
    ensure!(
        source.raster_count() == 1
            && source.raster_size() == (grid.width, grid.height)
            && source.geo_transform()? == grid.transform
            && source.spatial_ref()? == spatial_ref(&grid.wkt)?,
        "Raster grid/CRS mismatch: {}",
        path.display()
    );
    ensure!(
        source.metadata_item("LAYOUT", "IMAGE_STRUCTURE").as_deref() == Some("COG")
            && band.block_size() == (BLOCK, BLOCK),
        "Raster must be a COG with 512-pixel tiles: {}",
        path.display()
    );
    let mask = band.mask_flags()?;
    ensure!(
        mask.is_per_dataset()
            && !mask.is_nodata()
            && !mask.is_alpha()
            && !mask.is_all_valid()
            && band.no_data_value().is_none()
            && !Path::new(&format!("{}.msk", path.display())).exists(),
        "Raster must have an internal per-dataset mask and no nodata sentinel: {}",
        path.display()
    );
    ensure!(
        band.band_type().name() == dtype,
        "Raster dtype must be {dtype}: {}",
        path.display()
    );
    if metres {
        ensure!(
            source.metadata_item("units", "").as_deref() == Some("m") && band.unit() == "m",
            "Raster must declare metre units: {}",
            path.display()
        );
    }
    Ok(())
}

pub(crate) fn mini_windows(source: &Dataset, grid: &Grid) -> Result<Vec<(i64, Window)>> {
    let raw = source
        .metadata_item("mini_index", "")
        .context("Catchment grid lacks mini_index")?;
    let index: Vec<(i64, f64, f64, f64, f64)> =
        serde_json::from_str(&raw).context("Invalid mini_index")?;
    ensure!(!index.is_empty(), "mini_index is empty");
    let mut previous = 0;
    index
        .into_iter()
        .map(|(id, minx, miny, maxx, maxy)| {
            ensure!(
                id > previous && id <= i32::MAX as i64,
                "mini_index IDs must be positive, unique, ascending int32 IDs"
            );
            previous = id;
            ensure!(
                [minx, miny, maxx, maxy].iter().all(|v| v.is_finite())
                    && minx < maxx
                    && miny < maxy,
                "Mini {id} has invalid bounds"
            );
            let t = grid.transform;
            let pixel = |coordinate: f64, origin: f64, scale: f64, limit: usize| -> Result<usize> {
                let rounded = ((coordinate - origin) / scale).round();
                ensure!(
                    rounded >= 0.
                        && rounded <= limit as f64
                        && (coordinate - (origin + rounded * scale)).abs() <= 1e-12,
                    "Mini {id} bounds are outside the grid or not pixel edges"
                );
                Ok(rounded as usize)
            };
            let x = pixel(minx, t[0], t[1], grid.width)?;
            let right = pixel(maxx, t[0], t[1], grid.width)?;
            let y = pixel(maxy, t[3], t[5], grid.height)?;
            let bottom = pixel(miny, t[3], t[5], grid.height)?;
            ensure!(right > x && bottom > y, "Mini {id} has empty pixel bounds");
            Ok((
                id,
                Window {
                    x,
                    y,
                    width: right - x,
                    height: bottom - y,
                },
            ))
        })
        .collect()
}
