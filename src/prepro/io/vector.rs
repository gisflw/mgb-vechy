//! Narrow vector adapters shared by ROI and aggregation.
use crate::prepro::{
    execution::{CACHE_BYTES, MIB, publish},
    model::{RoiAttributes, SourceId},
};
use anyhow::{Context, Result, bail, ensure};
use gdal::{
    Dataset, DriverManager,
    spatial_ref::{AxisMappingStrategy, CoordTransform, SpatialRef},
    vector::{
        Feature, FieldDefn, FieldValue, Geometry, Layer, LayerAccess, LayerOptions, OGRFieldType,
    },
};
use geographiclib_rs::{Geodesic, InverseGeodesic, PolygonArea, Winding};
use geos::{Geom, Geometry as GeosGeometry, GeometryTypes};
use serde::Serialize;
use std::{
    fs::{self, File},
    path::{Path, PathBuf},
    thread,
};

pub(crate) const ROI_FIELDS: [&str; 9] = [
    "id",
    "id_down",
    "sub",
    "strahler_order",
    "unit_length",
    "upstream_length",
    "unit_area",
    "upstream_area",
    "water_course",
];

pub(crate) struct Provider {
    dataset: Dataset,
    name: Option<String>,
    pub crs: String,
    pub geometry_type: u32,
}
impl Provider {
    pub fn open(path: &Path, name: Option<&str>, override_crs: Option<&str>) -> Result<Self> {
        let dataset =
            Dataset::open(path).with_context(|| format!("Open vector {}", path.display()))?;
        let layer = match name {
            Some(name) => dataset.layer_by_name(name)?,
            None => {
                ensure!(
                    dataset.layer_count() == 1,
                    "Select a layer for {}",
                    path.display()
                );
                dataset.layer(0)?
            }
        };
        let crs = if let Some(text) = override_crs {
            parse_crs(text)?
        } else {
            layer
                .spatial_ref()
                .context("Vector must declare a CRS or supply a source-CRS override")?
        };
        ensure!(
            crs.is_geographic() || crs.is_projected(),
            "Vector CRS must be geographic or projected"
        );
        Ok(Self {
            crs: crs.to_wkt()?,
            geometry_type: layer.defn().geometry_type(),
            name: name.map(str::to_owned),
            dataset,
        })
    }
    pub fn layer(&self) -> Result<Layer<'_>> {
        Ok(match &self.name {
            Some(name) => self.dataset.layer_by_name(name)?,
            None => self.dataset.layer(0)?,
        })
    }
}

pub(crate) fn parse_crs(text: &str) -> Result<SpatialRef> {
    let mut crs = SpatialRef::from_definition(text).context("Invalid CRS")?;
    ensure!(
        crs.is_projected() || crs.is_geographic(),
        "CRS must be geographic or projected"
    );
    crs.set_axis_mapping_strategy(AxisMappingStrategy::TraditionalGisOrder);
    Ok(crs)
}

pub(crate) fn field(layer: &Layer<'_>, requested: &str) -> Result<usize> {
    let fields: Vec<_> = layer.defn().fields().map(|f| f.name()).collect();
    if let Some(index) = fields.iter().position(|name| name == requested) {
        return Ok(index);
    }
    let matches: Vec<_> = fields
        .iter()
        .enumerate()
        .filter(|(_, name)| name.to_lowercase() == requested.to_lowercase())
        .map(|(i, _)| i)
        .collect();
    ensure!(
        matches.len() == 1,
        "Missing or ambiguous required field: {requested}"
    );
    Ok(matches[0])
}

pub(crate) fn id_type(layer: &Layer<'_>, index: usize) -> Result<u32> {
    let ty = layer
        .defn()
        .fields()
        .nth(index)
        .context("Missing ID field")?
        .field_type();
    ensure!(
        [
            OGRFieldType::OFTInteger,
            OGRFieldType::OFTInteger64,
            OGRFieldType::OFTReal,
            OGRFieldType::OFTString
        ]
        .contains(&ty),
        "IDs must be integer, real, or string values"
    );
    Ok(ty)
}

pub(crate) fn source_id(value: Option<FieldValue>) -> Result<Option<SourceId>> {
    Ok(match value {
        None => None,
        Some(FieldValue::IntegerValue(v)) => Some(SourceId::Integer(v.into())),
        Some(FieldValue::Integer64Value(v)) => Some(SourceId::Integer(v)),
        Some(FieldValue::RealValue(v)) => {
            ensure!(v.is_finite(), "Source ID must be finite");
            Some(SourceId::Real(if v == 0. { 0 } else { v.to_bits() }))
        }
        Some(FieldValue::StringValue(v)) if v.trim().is_empty() => None,
        Some(FieldValue::StringValue(v)) => Some(SourceId::Text(v)),
        _ => bail!("Unsupported source ID type"),
    })
}

pub(crate) fn value(feature: &Feature<'_>, index: usize) -> Result<Option<FieldValue>> {
    // FlatGeobuf represents absent properties as unset; GDAL's typed getters turn these into zero.
    let present =
        unsafe { gdal_sys::OGR_F_IsFieldSetAndNotNull(feature.c_feature(), index.try_into()?) };
    if present == 0 {
        return Ok(None);
    }
    Ok(feature.field(index)?)
}

pub(crate) fn number(feature: &Feature<'_>, index: usize) -> Result<Option<f64>> {
    value(feature, index)?
        .map(|value| match value {
            FieldValue::IntegerValue(value) => Ok(f64::from(value)),
            FieldValue::Integer64Value(value) => Ok(value as f64),
            FieldValue::RealValue(value) => Ok(value),
            FieldValue::StringValue(value) => {
                value.parse().context("Invalid numeric vector attribute")
            }
            _ => bail!("Invalid numeric vector attribute type"),
        })
        .transpose()
}

pub(crate) fn integer(feature: &Feature<'_>, index: usize) -> Result<i64> {
    match value(feature, index)?.context("Null ROI integer attribute")? {
        FieldValue::IntegerValue(value) => Ok(value.into()),
        FieldValue::Integer64Value(value) => Ok(value),
        FieldValue::RealValue(value)
            if value.is_finite()
                && value.fract() == 0.
                && value >= i64::MIN as f64
                && value < i64::MAX as f64 =>
        {
            Ok(value as i64)
        }
        _ => bail!("Invalid ROI integer attribute"),
    }
}

pub(crate) fn outlet(text: &str, ty: u32) -> Result<SourceId> {
    source_id(Some(match ty {
        OGRFieldType::OFTInteger | OGRFieldType::OFTInteger64 => {
            FieldValue::Integer64Value(text.parse().context("Invalid integer outlet ID")?)
        }
        OGRFieldType::OFTReal => {
            FieldValue::RealValue(text.parse().context("Invalid real outlet ID")?)
        }
        _ => FieldValue::StringValue(text.to_owned()),
    }))?
    .context("Outlet ID must not be empty")
}

pub(crate) fn id_value(id: &SourceId, ty: u32) -> Result<FieldValue> {
    Ok(match id {
        SourceId::Integer(v) if ty == OGRFieldType::OFTInteger => {
            FieldValue::IntegerValue((*v).try_into()?)
        }
        SourceId::Integer(v) => FieldValue::Integer64Value(*v),
        SourceId::Real(v) => FieldValue::RealValue(f64::from_bits(*v)),
        SourceId::Text(v) => FieldValue::StringValue(v.clone()),
    })
}

pub(crate) fn roi_values(row: &RoiAttributes, ty: u32) -> Result<Vec<Option<FieldValue>>> {
    let mut fields = vec![
        Some(id_value(&row.id, ty)?),
        row.id_down
            .as_ref()
            .map(|id| id_value(id, ty))
            .transpose()?,
        Some(FieldValue::Integer64Value(row.sub)),
        Some(FieldValue::Integer64Value(row.strahler_order)),
    ];
    fields.extend(row.metrics.iter().map(|v| Some(FieldValue::RealValue(*v))));
    fields.push(Some(id_value(&row.water_course, ty)?));
    Ok(fields)
}

pub(crate) fn roi_schema(ty: u32) -> Vec<(&'static str, u32)> {
    ROI_FIELDS
        .iter()
        .enumerate()
        .map(|(i, name)| {
            (
                *name,
                match i {
                    0 | 1 | 8 => ty,
                    2 | 3 => OGRFieldType::OFTInteger64,
                    _ => OGRFieldType::OFTReal,
                },
            )
        })
        .collect()
}

pub(crate) fn write_vector<'a>(
    path: &Path,
    crs: &SpatialRef,
    schema: &[(&str, u32)],
    geometry_type: u32,
    indexed: bool,
    rows: impl Iterator<Item = Result<(Vec<Option<FieldValue>>, &'a [u8])>>,
) -> Result<()> {
    let mut dataset = DriverManager::get_driver_by_name("FlatGeobuf")?.create_vector_only(path)?;
    let layer = dataset.create_layer(LayerOptions {
        name: path
            .file_stem()
            .unwrap()
            .to_str()
            .context("Invalid vector filename")?,
        srs: Some(crs),
        ty: geometry_type,
        options: Some(&[if indexed {
            "SPATIAL_INDEX=YES"
        } else {
            "SPATIAL_INDEX=NO"
        }]),
    })?;
    for (name, ty) in schema {
        FieldDefn::new(name, *ty)?.add_to_layer(&layer)?;
    }
    for row in rows {
        let (values, bytes) = row?;
        let mut feature = Feature::new(layer.defn())?;
        for (i, value) in values.iter().enumerate() {
            match value {
                Some(value) => feature.set_field(i, value)?,
                None => feature.set_field_null(i)?,
            }
        }
        feature.set_geometry(Geometry::from_wkb(bytes)?)?;
        feature.create(&layer)?;
    }
    dataset.flush_cache()?;
    Ok(())
}

pub(crate) fn validate_geometry(bytes: &[u8], polygon: bool) -> Result<GeosGeometry> {
    let geometry = GeosGeometry::new_from_wkb(bytes)?;
    let allowed = if polygon {
        [GeometryTypes::Polygon, GeometryTypes::MultiPolygon]
    } else {
        [GeometryTypes::LineString, GeometryTypes::MultiLineString]
    };
    ensure!(
        !geometry.is_empty()?
            && geometry.is_valid()?
            && allowed.contains(&geometry.geometry_type()?),
        "Invalid, empty, or incorrectly typed vector geometry"
    );
    Ok(geometry)
}

pub(crate) struct GeodesicMetric {
    transform: CoordTransform,
    geodesic: Geodesic,
}
impl GeodesicMetric {
    pub fn new(source: &SpatialRef) -> Result<Self> {
        let mut geographic = source.geog_cs()?;
        let status = unsafe {
            gdal_sys::OSRSetAngularUnits(
                geographic.to_c_hsrs(),
                c"degree".as_ptr(),
                std::f64::consts::PI / 180.,
            )
        };
        ensure!(status == 0, "Cannot normalize source geographic units");
        geographic.set_axis_mapping_strategy(AxisMappingStrategy::TraditionalGisOrder);
        let a = source.semi_major()?;
        let b = source.semi_minor()?;
        ensure!(
            a.is_finite() && b.is_finite() && a > 0. && b > 0.,
            "Unusable source CRS ellipsoid"
        );
        Ok(Self {
            transform: CoordTransform::new(source, &geographic)?,
            geodesic: Geodesic::new(a, (a - b) / a),
        })
    }
    pub fn measure(&self, geometry: &Geometry, polygon: bool) -> Result<f64> {
        let geographic = geometry.transform(&self.transform)?;
        let value = self.parts(&geographic, polygon)?;
        let value = if polygon {
            value.abs() / 1e6
        } else {
            value / 1000.
        };
        ensure!(value.is_finite() && value >= 0., "Invalid geodesic metric");
        Ok(value)
    }
    fn parts(&self, geometry: &Geometry, polygon: bool) -> Result<f64> {
        if geometry.geometry_count() > 0 {
            let mut value = 0.;
            for i in 0..geometry.geometry_count() {
                value += self.parts(&geometry.get_geometry(i), polygon)?;
            }
            return Ok(value);
        }
        let points: Vec<_> = (0..geometry.point_count())
            .map(|i| geometry.get_point(i as i32))
            .collect();
        ensure!(
            points
                .iter()
                .all(|(x, y, _)| x.is_finite() && y.is_finite()),
            "Non-finite geometry coordinates"
        );
        if polygon {
            let mut area = PolygonArea::new(&self.geodesic, Winding::CounterClockwise);
            for (x, y, _) in points {
                area.add_point(y, x);
            }
            Ok(area.compute(true).1)
        } else {
            Ok(points
                .windows(2)
                .map(|p| -> f64 { self.geodesic.inverse(p[0].1, p[0].0, p[1].1, p[1].0) })
                .sum())
        }
    }
}

// shortcut: vector topology and selected WKB stay resident; add paging only for measured larger datasets.
pub(crate) struct VectorBudget {
    limit: usize,
    retained: usize,
    requested: usize,
}
impl VectorBudget {
    pub fn new(memory_mb: usize, workers: usize) -> Result<Self> {
        ensure!(
            workers > 0 && memory_mb > 0,
            "Workers and memory limit must be positive"
        );
        let limit = memory_mb
            .checked_mul(MIB)
            .context("Memory budget overflow")?;
        ensure!(
            limit >= CACHE_BYTES + 8 * MIB,
            "Vector memory budget needs at least 24 MiB"
        );
        Ok(Self {
            limit,
            retained: CACHE_BYTES,
            requested: workers,
        })
    }
    pub fn reserve(&mut self, bytes: usize) -> Result<()> {
        self.retained = self
            .retained
            .checked_add(bytes)
            .context("Vector allocation overflow")?;
        ensure!(
            self.retained <= self.limit - 8 * MIB,
            "Vector inputs exceed application memory budget; increase --memory-limit-mb"
        );
        Ok(())
    }
    pub fn geometry(&mut self, bytes: usize) -> Result<()> {
        self.reserve(
            bytes
                .checked_mul(4)
                .context("Geometry allocation overflow")?,
        )
    }
    pub fn workers(&self, largest: usize, jobs: usize) -> Result<usize> {
        let worker = largest
            .checked_mul(24)
            .and_then(|v| v.checked_add(8 * MIB))
            .context("Geometry workspace overflow")?;
        let available = self.limit - self.retained;
        ensure!(
            worker <= available,
            "Oversized geometry group exceeds memory budget; increase --memory-limit-mb"
        );
        Ok(self.requested.min(jobs).min(available / worker))
    }
}

pub(crate) fn parallel<T: Send>(
    jobs: usize,
    workers: usize,
    work: impl Fn(usize, usize) -> Result<T> + Sync,
) -> Result<Vec<T>> {
    thread::scope(|scope| {
        let mut handles = Vec::new();
        let work = &work;
        for worker in 0..workers {
            handles.push(scope.spawn(move || work(worker, workers)));
        }
        let mut values = Vec::with_capacity(workers);
        let mut error = None;
        for handle in handles {
            match handle
                .join()
                .map_err(|_| anyhow::anyhow!("Vector worker panicked"))
                .and_then(|r| r)
            {
                Ok(value) => values.push(value),
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
        ensure!(jobs > 0, "No geometry jobs");
        Ok(values)
    })
}

pub(crate) fn output_paths(output: &Path, names: &[&str]) -> Result<PathBuf> {
    let output = std::path::absolute(output)?;
    for name in names {
        ensure!(
            !output.join(name).try_exists()?,
            "Output already exists: {}",
            output.join(name).display()
        );
    }
    Ok(output)
}

pub(crate) fn finish(
    staging: &Path,
    output: &Path,
    stage: &str,
    parameters: &impl Serialize,
    products: &[&str],
    workers_used: usize,
) -> Result<PathBuf> {
    let name = format!("manifest-{stage}.json");
    let mut parameters = serde_json::to_value(parameters)?;
    parameters["workers_used"] = serde_json::json!(workers_used);
    serde_json::to_writer_pretty(
        File::create(staging.join(&name))?,
        &serde_json::json!({"step":stage,"parameters":parameters}),
    )?;
    let names: Vec<_> = products
        .iter()
        .map(|s| s.to_string())
        .chain([name.clone()])
        .collect();
    publish(staging, output, &names)?;
    Ok(output.join(name))
}

pub(crate) fn absolute_input(path: &Path) -> Result<PathBuf> {
    fs::canonicalize(path).with_context(|| format!("Input unavailable: {}", path.display()))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn field_resolution_prefers_exact_names_and_rejects_ambiguity() -> Result<()> {
        let mut dataset = DriverManager::get_driver_by_name("Memory")?.create_vector_only("")?;
        let layer = dataset.create_layer(LayerOptions::default())?;
        for name in ["id", "ID"] {
            FieldDefn::new(name, OGRFieldType::OFTInteger64)?.add_to_layer(&layer)?;
        }
        assert_eq!(field(&layer, "id")?, 0);
        assert_eq!(field(&layer, "ID")?, 1);
        assert!(field(&layer, "Id").is_err());
        assert!(field(&layer, "missing").is_err());
        let mut feature = Feature::new(layer.defn())?;
        assert_eq!(number(&feature, 0)?, None);
        feature.set_field_integer64(0, 9_007_199_254_740_993)?;
        assert_eq!(integer(&feature, 0)?, 9_007_199_254_740_993);
        Ok(())
    }
    #[test]
    fn measurements_use_native_ellipsoid_degrees_and_signed_rings() -> Result<()> {
        let metric = GeodesicMetric::new(&parse_crs("EPSG:4326")?)?;
        let line = Geometry::from_wkt("LINESTRING (0 0, 1 0)")?;
        assert!((metric.measure(&line, false)? - 111.31949079327357).abs() < 1e-10);
        let polygon = Geometry::from_wkt("POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))")?;
        let area = metric.measure(&polygon, true)?;
        assert!((area - 12308.778361469452).abs() < 1e-8);
        let hole = Geometry::from_wkt(
            "POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0), (0.2 0.2, 0.2 0.8, 0.8 0.8, 0.8 0.2, 0.2 0.2))",
        )?;
        assert!(metric.measure(&hole, true)? < area);
        let projected = GeodesicMetric::new(&parse_crs("EPSG:3857")?)?;
        let line = Geometry::from_wkt("LINESTRING (0 0, 111319.49079327357 0)")?;
        assert!((projected.measure(&line, false)? - 111.31949079327357).abs() < 1e-10);
        let native =
            GeodesicMetric::new(&parse_crs("+proj=longlat +a=3396190 +b=3376200 +type=crs")?)?;
        let line = Geometry::from_wkt("LINESTRING (0 0, 1 0)")?;
        assert!(
            (native.measure(&line, false)? - 3396190. * std::f64::consts::PI / 180000.).abs()
                < 1e-10
        );
        let grads = GeodesicMetric::new(&parse_crs("EPSG:4807")?)?;
        let line = Geometry::from_wkt("LINESTRING (0 0, 1 0)")?;
        assert!(
            (grads.measure(&line, false)?
                - parse_crs("EPSG:4807")?.semi_major()? * std::f64::consts::PI / 200000.)
                .abs()
                < 1e-8
        );
        Ok(())
    }

    #[test]
    fn memory_rejects_overflow_resident_inputs_and_oversized_groups() -> Result<()> {
        assert!(VectorBudget::new(usize::MAX, 1).is_err());
        assert!(VectorBudget::new(24, 0).is_err());
        assert!(VectorBudget::new(1, 1).is_err());
        let mut budget = VectorBudget::new(64, 4)?;
        assert_eq!(budget.workers(1, 4)?, 4);
        assert!(budget.workers(3 * MIB, 4).is_err());
        assert!(budget.geometry(usize::MAX).is_err());
        assert!(budget.reserve(64 * MIB).is_err());
        Ok(())
    }

    #[test]
    fn source_id_types_nulls_and_string_keys() -> Result<()> {
        for (text, ty) in [
            ("10", OGRFieldType::OFTInteger64),
            ("10.0", OGRFieldType::OFTReal),
            ("010", OGRFieldType::OFTString),
        ] {
            assert_eq!(outlet(text, ty)?.key(), text);
        }
        assert_eq!(outlet("1e-7", OGRFieldType::OFTReal)?.key(), "1e-07");
        assert_eq!(outlet("1e-5", OGRFieldType::OFTReal)?.key(), "1e-05");
        assert_eq!(outlet("1e16", OGRFieldType::OFTReal)?.key(), "1e+16");
        assert_eq!(
            outlet("1e15", OGRFieldType::OFTReal)?.key(),
            "1000000000000000.0"
        );
        assert_eq!(source_id(None)?, None);
        assert_eq!(source_id(Some(FieldValue::StringValue(" ".into())))?, None);
        assert!(outlet("NaN", OGRFieldType::OFTReal).is_err());
        Ok(())
    }
}
