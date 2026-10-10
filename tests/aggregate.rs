use anyhow::Result;
use gdal::{
    Dataset, DriverManager,
    spatial_ref::SpatialRef,
    vector::{
        Feature, FieldDefn, FieldValue, Geometry, LayerAccess, LayerOptions, OGRFieldType,
        OGRwkbGeometryType,
    },
};
use mgb::prepro::{AggregationSpec, aggregate_roi_dataset, define_roi_dataset};
use std::path::Path;

#[path = "support/vector.rs"]
mod vector;
use vector::{fixture, integer, sources, spec, values};

#[path = "support/jacui/mod.rs"]
#[allow(dead_code)]
mod jacui;

#[test]
fn geometry_union_centroids_and_worker_determinism() -> Result<()> {
    let (temp, spec) = fixture()?;
    let roi = define_roi_dataset(&spec)?;
    let mut aggregation = AggregationSpec {
        overwrite: false,
        io_slots: 2,
        batch_size: 10000,
        roi_catchments: roi.catchments,
        roi_segments: roi.segments,
        output_dir: temp.path().join("minis"),
        uparea_min: 0.,
        lmin: 0.,
        workers: 4,
        memory_limit_mb: 256,
    };
    let minis = aggregate_roi_dataset(&aggregation)?;
    assert_eq!(minis.source_count, 4);
    assert_eq!(minis.mini_count, 3);
    let manifest: serde_json::Value =
        serde_json::from_reader(std::fs::File::open(&minis.manifest)?)?;
    assert!(
        manifest["runtime"]["elapsed_time"]["total"]
            .as_f64()
            .unwrap()
            .is_finite()
    );
    assert!(
        !Dataset::open(&minis.catchments)?
            .layer(0)?
            .has_capability(gdal::vector::LayerCaps::OLCFastSpatialFilter)
    );
    assert_eq!(
        values(&minis.segments)?
            .iter()
            .map(|row| (
                row[0].clone(),
                row[1].clone(),
                row[2].clone(),
                row[3].clone()
            ))
            .collect::<Vec<_>>(),
        [
            (
                Some(integer(1)),
                Some(integer(3)),
                Some(integer(1)),
                Some(integer(1))
            ),
            (
                Some(integer(2)),
                Some(integer(3)),
                Some(integer(2)),
                Some(integer(1))
            ),
            (
                Some(integer(3)),
                Some(integer(-1)),
                Some(integer(2)),
                Some(integer(2))
            )
        ]
    );
    let mut reader = csv::Reader::from_path(&minis.source_to_mini)?;
    let mapping = reader
        .records()
        .collect::<std::result::Result<Vec<_>, _>>()?;
    assert_eq!(
        mapping.iter().map(|row| &row[0]).collect::<Vec<_>>(),
        ["1", "2", "3", "4"]
    );
    for (i, row) in mapping.iter().enumerate() {
        assert_eq!(row[3].parse::<f64>()?, i as f64 + 0.5);
        assert_eq!(row[4].parse::<f64>()?, 0.5);
    }
    aggregation.memory_limit_mb = 32;
    aggregation.output_dir = temp.path().join("serial-minis");
    let serial = aggregate_roi_dataset(&aggregation)?;
    assert_eq!(serial.workers_used, 1);
    jacui::compare::compare_vector(&serial.catchments, &minis.catchments)?;
    jacui::compare::compare_vector(&serial.segments, &minis.segments)?;
    assert_eq!(
        std::fs::read(serial.source_to_mini)?,
        std::fs::read(minis.source_to_mini)?
    );
    Ok(())
}

#[test]
fn accepts_normalized_source_identifier_types() -> Result<()> {
    for ty in [
        OGRFieldType::OFTInteger,
        OGRFieldType::OFTInteger64,
        OGRFieldType::OFTReal,
        OGRFieldType::OFTString,
    ] {
        let temp = tempfile::tempdir()?;
        let mut spec = spec(temp.path());
        let id = match ty {
            OGRFieldType::OFTInteger => FieldValue::IntegerValue(10),
            OGRFieldType::OFTInteger64 => integer(9_007_199_254_740_993),
            OGRFieldType::OFTReal => FieldValue::RealValue(10.5),
            _ => FieldValue::StringValue("010".into()),
        };
        spec.outlet_ids = vec![
            match ty {
                OGRFieldType::OFTReal => "10.5",
                OGRFieldType::OFTString => "010",
                OGRFieldType::OFTInteger64 => "9007199254740993",
                _ => "10",
            }
            .into(),
        ];
        let rows = [(id.clone(), None, Some(1.))];
        sources(&spec.catchments, true, ty, &rows, None)?;
        sources(&spec.segments, false, ty, &rows, None)?;
        let roi = define_roi_dataset(&spec)?;
        let aggregation = AggregationSpec {
            overwrite: false,
            io_slots: 2,
            batch_size: 10000,
            roi_catchments: roi.catchments,
            roi_segments: roi.segments,
            output_dir: temp.path().join("minis"),
            uparea_min: 0.,
            lmin: 0.,
            workers: 1,
            memory_limit_mb: 256,
        };
        assert_eq!(aggregate_roi_dataset(&aggregation)?.mini_count, 1);
    }
    Ok(())
}

#[test]
fn rejects_invalid_thresholds_without_products() -> Result<()> {
    let (temp, spec) = fixture()?;
    let roi = define_roi_dataset(&spec)?;
    for threshold in [-1., f64::NAN, f64::INFINITY] {
        let aggregation = AggregationSpec {
            overwrite: false,
            io_slots: 2,
            batch_size: 10000,
            roi_catchments: roi.catchments.clone(),
            roi_segments: roi.segments.clone(),
            output_dir: temp.path().join("bad-threshold"),
            uparea_min: threshold,
            lmin: 0.,
            workers: 1,
            memory_limit_mb: 256,
        };
        assert!(aggregate_roi_dataset(&aggregation).is_err());
        assert!(!aggregation.output_dir.exists());
    }
    Ok(())
}

fn normalized(
    path: &Path,
    polygon: bool,
    crs: u32,
    rows: &[Vec<Option<FieldValue>>],
    invalid: bool,
) -> Result<()> {
    let mut ds = DriverManager::get_driver_by_name("FlatGeobuf")?.create_vector_only(path)?;
    let crs = SpatialRef::from_epsg(crs)?;
    let layer = ds.create_layer(LayerOptions {
        name: "roi",
        srs: Some(&crs),
        ty: OGRwkbGeometryType::wkbUnknown,
        options: Some(&["SPATIAL_INDEX=NO"]),
    })?;
    let names = [
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
    for (i, name) in names.iter().enumerate() {
        FieldDefn::new(
            name,
            if (4..8).contains(&i) {
                OGRFieldType::OFTReal
            } else {
                OGRFieldType::OFTInteger64
            },
        )?
        .add_to_layer(&layer)?;
    }
    for row in rows {
        let mut feature = Feature::new(layer.defn())?;
        for (i, value) in row.iter().enumerate() {
            if let Some(value) = value {
                feature.set_field(i, value)?;
            } else {
                feature.set_field_null(i)?;
            }
        }
        let wkt = if invalid {
            "POINT (0 0)"
        } else if polygon {
            "POLYGON ((0 0,1 0,1 1,0 1,0 0))"
        } else {
            "LINESTRING (0 0,1 0)"
        };
        feature.set_geometry(Geometry::from_wkt(wkt)?)?;
        feature.create(&layer)?;
    }
    ds.flush_cache()?;
    Ok(())
}

#[test]
fn geometric_union_preserves_summed_metrics_and_rejects_malformed_roi() -> Result<()> {
    let temp = tempfile::tempdir()?;
    let mut rows = Vec::new();
    for id in [1, 2] {
        rows.push(vec![
            Some(integer(id)),
            if id == 1 { None } else { Some(integer(1)) },
            Some(integer(1)),
            Some(integer(1)),
            Some(FieldValue::RealValue(5.)),
            Some(FieldValue::RealValue(10.)),
            Some(FieldValue::RealValue(2.)),
            Some(FieldValue::RealValue(if id == 1 { 4. } else { 2. })),
            Some(integer(1)),
        ]);
    }
    let catches = temp.path().join("catchments.fgb");
    let segments = temp.path().join("segments.fgb");
    normalized(&catches, true, 4326, &rows, false)?;
    normalized(&segments, false, 4326, &rows, false)?;
    let spec = AggregationSpec {
        overwrite: false,
        io_slots: 2,
        batch_size: 10000,
        roi_catchments: catches,
        roi_segments: segments,
        output_dir: temp.path().join("minis"),
        uparea_min: 0.,
        lmin: 0.,
        workers: 4,
        memory_limit_mb: 256,
    };
    let report = aggregate_roi_dataset(&spec)?;
    assert_eq!(report.mini_count, 1);
    let line_ds = Dataset::open(&report.segments)?;
    let mut line_layer = line_ds.layer(0)?;
    let line = line_layer.features().next().unwrap();
    assert_eq!(line.geometry().unwrap().length(), 1.);
    assert_eq!(line.field_as_double(4)?, Some(10.));
    let polygon_ds = Dataset::open(&report.catchments)?;
    let mut polygon_layer = polygon_ds.layer(0)?;
    let polygon = polygon_layer.features().next().unwrap();
    assert_eq!(polygon.geometry().unwrap().area(), 1.);
    assert_eq!(polygon.field_as_double(6)?, Some(4.));
    for label in [
        "duplicate",
        "ids",
        "crs",
        "numeric",
        "integer",
        "geometry",
        "cycle",
    ] {
        let mut altered = rows.clone();
        match label {
            "duplicate" => altered[1][0] = Some(integer(1)),
            "ids" => altered[1][0] = Some(integer(9)),
            "numeric" => altered[0][4] = Some(FieldValue::RealValue(f64::NAN)),
            "integer" => altered[0][2] = None,
            "cycle" => altered[0][1] = Some(integer(2)),
            _ => {}
        }
        let path = temp.path().join(format!("{label}.fgb"));
        normalized(
            &path,
            false,
            if label == "crs" { 3857 } else { 4326 },
            &altered,
            label == "geometry",
        )?;
        let mut failed = spec.clone();
        failed.roi_segments = path;
        failed.output_dir = temp.path().join(label);
        assert!(aggregate_roi_dataset(&failed).is_err(), "{label}");
        assert!(!failed.output_dir.join("mini_catchments.fgb").exists());
    }
    let mut collision = spec.clone();
    collision.output_dir = temp.path().join("collision");
    std::fs::create_dir(&collision.output_dir)?;
    std::fs::write(collision.output_dir.join("source_to_mini.csv"), "old")?;
    assert!(aggregate_roi_dataset(&collision).is_err());
    assert_eq!(
        std::fs::read_to_string(collision.output_dir.join("source_to_mini.csv"))?,
        "old"
    );
    assert!(!collision.output_dir.join("mini_catchments.fgb").exists());
    Ok(())
}
