use anyhow::Result;
use gdal::{
    Dataset,
    vector::{FieldValue, LayerAccess, OGRFieldType},
};
use mgb::prepro::define_roi_dataset;
use std::path::Path;

#[path = "support/vector.rs"]
mod vector;
use vector::{fixture, integer, sources, spec, values};

#[path = "support/jacui/mod.rs"]
#[allow(dead_code)]
mod jacui;

#[test]
fn ordered_outlets_and_worker_determinism() -> Result<()> {
    let (temp, mut spec) = fixture()?;
    let roi = define_roi_dataset(&spec)?;
    assert_eq!(roi.source_count, 4);
    let ds = Dataset::open(&roi.segments)?;
    let mut layer = ds.layer(0)?;
    assert!(layer.has_capability(gdal::vector::LayerCaps::OLCFastSpatialFilter));
    let mut rows = Vec::new();
    for feature in layer.features() {
        rows.push((
            feature.field_as_integer64(0)?.unwrap(),
            feature.field_as_integer64(2)?.unwrap(),
            feature.field_as_integer64(8)?.unwrap(),
        ));
    }
    rows.sort();
    assert_eq!(rows, [(1, 2, 1), (2, 1, 2), (3, 2, 1), (4, 1, 2)]);
    assert_eq!(
        layer.defn().fields().nth(2).unwrap().field_type(),
        OGRFieldType::OFTInteger64
    );
    let manifest: serde_json::Value = serde_json::from_reader(std::fs::File::open(&roi.manifest)?)?;
    assert_eq!(manifest["step"], "define-roi");
    assert!(manifest["elapsed_seconds"].as_f64().unwrap().is_finite());
    assert!(Path::new(manifest["parameters"]["catchments"].as_str().unwrap()).is_absolute());
    spec.output_dir = temp.path().join("serial-roi");
    spec.memory_limit_mb = 32;
    let serial = define_roi_dataset(&spec)?;
    assert_eq!(serial.workers_used, 1);
    jacui::compare::compare_vector(&serial.catchments, &roi.catchments)?;
    jacui::compare::compare_vector(&serial.segments, &roi.segments)?;
    Ok(())
}

#[test]
fn source_identifier_types_and_filter_before_outlet_selection() -> Result<()> {
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
        let output_ty = if ty == OGRFieldType::OFTInteger {
            OGRFieldType::OFTInteger64
        } else {
            ty
        };
        assert_eq!(
            Dataset::open(&roi.segments)?
                .layer(0)?
                .defn()
                .fields()
                .next()
                .unwrap()
                .field_type(),
            output_ty
        );
        let output_id = if ty == OGRFieldType::OFTInteger {
            integer(10)
        } else {
            id.clone()
        };
        assert_eq!(values(&roi.segments)?[0][0], Some(output_id));
        let filtered = temp.path().join("filtered.fgb");
        sources(&filtered, false, ty, &[(id, None, None)], Some(0))?;
        spec.segments = filtered;
        spec.output_dir = temp.path().join("filtered-roi");
        assert!(define_roi_dataset(&spec).is_err());
        assert!(!spec.output_dir.exists());
    }
    Ok(())
}

#[test]
fn selected_only_validation_and_rejection_without_products() -> Result<()> {
    let temp = tempfile::tempdir()?;
    let mut spec = spec(temp.path());
    spec.outlet_ids = vec!["1".into()];
    let rows = [
        (integer(1), None, Some(1.)),
        (integer(99), None, Some(1.)),
        (integer(99), None, Some(1.)),
    ];
    sources(
        &spec.catchments,
        true,
        OGRFieldType::OFTInteger64,
        &rows,
        Some(1),
    )?;
    sources(
        &spec.segments,
        false,
        OGRFieldType::OFTInteger64,
        &rows[..2],
        Some(1),
    )?;
    assert_eq!(define_roi_dataset(&spec)?.source_count, 1);
    for (label, rows, catchment, invalid) in [
        (
            "duplicate",
            vec![(integer(1), None, Some(1.)), (integer(1), None, Some(1.))],
            true,
            None,
        ),
        ("missing", vec![(integer(99), None, Some(1.))], true, None),
        (
            "cycle",
            vec![
                (integer(1), Some(integer(2)), Some(1.)),
                (integer(2), Some(integer(1)), Some(1.)),
            ],
            false,
            None,
        ),
        (
            "fractional",
            vec![(integer(1), None, Some(1.5))],
            false,
            None,
        ),
        ("invalid", vec![(integer(1), None, Some(1.))], true, Some(0)),
    ] {
        let path = temp.path().join(format!("{label}.fgb"));
        sources(&path, catchment, OGRFieldType::OFTInteger64, &rows, invalid)?;
        let mut failed = spec.clone();
        failed.output_dir = temp.path().join(label);
        if catchment {
            failed.catchments = path;
        } else {
            failed.segments = path;
        }
        assert!(define_roi_dataset(&failed).is_err(), "{label}");
        assert!(
            !failed.output_dir.join("roi_segments.fgb").exists(),
            "{label}"
        );
    }
    Ok(())
}

#[test]
fn collisions_bad_fields_layers_crs_and_budget_preserve_existing_data() -> Result<()> {
    let (temp, mut spec) = fixture()?;
    std::fs::create_dir_all(&spec.output_dir)?;
    std::fs::write(spec.output_dir.join("roi_segments.fgb"), "existing")?;
    assert!(define_roi_dataset(&spec).is_err());
    assert_eq!(
        std::fs::read_to_string(spec.output_dir.join("roi_segments.fgb"))?,
        "existing"
    );
    for mode in ["field", "layer", "crs", "workers", "memory"] {
        let mut failed = spec.clone();
        failed.output_dir = temp.path().join(mode);
        match mode {
            "field" => failed.id_col = "missing".into(),
            "layer" => failed.segments_layer = Some("missing".into()),
            "crs" => failed.crs = "invalid-crs".into(),
            "workers" => failed.workers = 0,
            _ => failed.memory_limit_mb = 1,
        }
        assert!(define_roi_dataset(&failed).is_err(), "{mode}");
        assert!(!failed.output_dir.exists());
    }
    spec.output_dir = temp.path().join("good");
    spec.catchments_layer = Some("sources".into());
    spec.segments_layer = Some("sources".into());
    spec.catchments_source_crs = Some("EPSG:4326".into());
    spec.segments_source_crs = Some("EPSG:4326".into());
    let roi = define_roi_dataset(&spec)?;
    assert_eq!(roi.source_count, 4);
    Ok(())
}
