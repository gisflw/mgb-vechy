//! Shared raster windows, geometry-independent CRS transforms, and cell areas.
use super::model::{Grid, Window};
use anyhow::{Result, bail, ensure};
use gdal::{
    Dataset,
    raster::Buffer,
    spatial_ref::{AxisMappingStrategy, CoordTransform, SpatialRef},
};
use geographiclib_rs::{Geodesic, PolygonArea, Winding};
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
