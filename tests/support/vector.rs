use anyhow::Result;
use gdal::{
    Dataset, DriverManager,
    spatial_ref::SpatialRef,
    vector::{
        Feature, FieldDefn, FieldValue, Geometry, LayerAccess, LayerOptions, OGRFieldType,
        OGRwkbGeometryType,
    },
};
use mgb::prepro::RoiSpec;
use std::path::Path;

pub fn sources(
    path: &Path,
    polygon: bool,
    ty: u32,
    rows: &[(FieldValue, Option<FieldValue>, Option<f64>)],
    invalid: Option<usize>,
) -> Result<()> {
    let mut ds = DriverManager::get_driver_by_name("FlatGeobuf")?.create_vector_only(path)?;
    let crs = SpatialRef::from_epsg(4326)?;
    let layer = ds.create_layer(LayerOptions {
        name: "sources",
        srs: Some(&crs),
        ty: OGRwkbGeometryType::wkbUnknown,
        options: Some(&["SPATIAL_INDEX=NO"]),
    })?;
    for (name, ty) in [
        ("ID", ty),
        ("ID_DOWN", ty),
        ("STRAHLER_ORDER", OGRFieldType::OFTReal),
    ] {
        FieldDefn::new(name, ty)?.add_to_layer(&layer)?;
    }
    for (i, (id, down, order)) in rows.iter().enumerate() {
        let mut feature = Feature::new(layer.defn())?;
        feature.set_field(0, id)?;
        if let Some(value) = down {
            feature.set_field(1, value)?;
        } else {
            feature.set_field_null(1)?;
        }
        if let Some(value) = order {
            feature.set_field_double(2, *value)?;
        } else {
            feature.set_field_null(2)?;
        }
        let x = i as f64;
        let text = if invalid == Some(i) {
            "POINT (0 0)".to_owned()
        } else if polygon {
            format!(
                "POLYGON (({x} 0, {} 0, {} 1, {x} 1, {x} 0))",
                x + 1.,
                x + 1.
            )
        } else {
            format!("LINESTRING ({x} 0, {} 0)", x + 1.)
        };
        feature.set_geometry(Geometry::from_wkt(&text)?)?;
        feature.create(&layer)?;
    }
    ds.flush_cache()?;
    Ok(())
}
pub fn integer(v: i64) -> FieldValue {
    FieldValue::Integer64Value(v)
}
fn network() -> Vec<(FieldValue, Option<FieldValue>, Option<f64>)> {
    vec![
        (integer(1), None, Some(2.)),
        (integer(2), Some(integer(1)), Some(1.)),
        (integer(3), Some(integer(1)), Some(1.)),
        (integer(4), Some(integer(2)), Some(1.)),
    ]
}
pub fn spec(root: &Path) -> RoiSpec {
    RoiSpec {
        overwrite: false,
        io_slots: 2,
        batch_size: 10000,
        catchments: root.join("catchments.fgb"),
        segments: root.join("segments.fgb"),
        output_dir: root.join("roi"),
        crs: "EPSG:4326".into(),
        outlet_ids: vec!["1".into(), "2".into()],
        id_col: "id".into(),
        id_down_col: "id_down".into(),
        strahler_order_col: "strahler_order".into(),
        catchments_layer: None,
        segments_layer: None,
        catchments_source_crs: None,
        segments_source_crs: None,
        workers: 4,
        memory_limit_mb: 256,
    }
}
pub fn fixture() -> Result<(tempfile::TempDir, RoiSpec)> {
    let temp = tempfile::tempdir()?;
    let spec = spec(temp.path());
    sources(
        &spec.catchments,
        true,
        OGRFieldType::OFTInteger64,
        &network(),
        None,
    )?;
    sources(
        &spec.segments,
        false,
        OGRFieldType::OFTInteger64,
        &network(),
        None,
    )?;
    Ok((temp, spec))
}
pub fn values(path: &Path) -> Result<Vec<Vec<Option<FieldValue>>>> {
    let ds = Dataset::open(path)?;
    let mut layer = ds.layer(0)?;
    layer
        .features()
        .map(|feature| {
            (0..feature.field_count())
                .map(|i| Ok(feature.field(i)?))
                .collect()
        })
        .collect()
}
