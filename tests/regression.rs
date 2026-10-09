#[path = "support/jacui/mod.rs"]
mod jacui;

use anyhow::Result;
use gdal::{
    DriverManager, Metadata,
    raster::{Buffer, GdalType, RasterCreationOptions},
    spatial_ref::SpatialRef,
    vector::{
        Feature, FieldDefn, Geometry, LayerAccess, LayerOptions, OGRFieldType, OGRwkbGeometryType,
    },
};
use jacui::{
    Network, Stage,
    compare::{compare_csv, compare_raster, compare_vector},
    runner::{RunOptions, invocation, protect_output, run},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
};

fn fixture() -> tempfile::TempDir {
    let temp = tempfile::tempdir().unwrap();
    fs::copy(
        jacui::fixture_root().join("config.json"),
        temp.path().join("config.json"),
    )
    .unwrap();
    for network in ["bhae", "tdxhydro"] {
        let input = temp.path().join("input").join(network);
        let expected = temp.path().join("expected").join(network);
        fs::create_dir_all(&input).unwrap();
        fs::create_dir_all(&expected).unwrap();
        for name in ["catchments.fgb", "segments.fgb"] {
            fs::write(input.join(name), b"fixture").unwrap();
        }
        for name in Stage::All.products() {
            fs::write(expected.join(name), b"fixture").unwrap();
        }
    }
    for name in ["dem.tif", "hru.tif"] {
        fs::write(temp.path().join("input").join(name), b"fixture").unwrap();
    }
    temp
}

fn options(output: PathBuf) -> RunOptions {
    RunOptions {
        network: Network::Bhae,
        stage: Stage::SampleMinis,
        output_dir: output,
        command: "/bin/true".into(),
        command_arg: vec![],
        workers: 4,
        memory_limit_mb: 4096,
    }
}

#[test]
fn inventory_detects_missing_size_checksum_and_unsafe_paths() {
    let temp = tempfile::tempdir().unwrap();
    fs::write(temp.path().join("data"), b"abc").unwrap();
    let hash = format!("{:x}", Sha256::digest(b"abc"));
    let inventory = json!({"files":{"data":{"bytes":3,"sha256":hash}}});
    fs::write(temp.path().join("inventory.json"), inventory.to_string()).unwrap();
    jacui::verify(temp.path()).unwrap();
    fs::write(temp.path().join("data"), b"abd").unwrap();
    assert!(
        jacui::verify(temp.path())
            .unwrap_err()
            .to_string()
            .contains("Checksum")
    );
    fs::write(temp.path().join("data"), b"ab").unwrap();
    assert!(
        jacui::verify(temp.path())
            .unwrap_err()
            .to_string()
            .contains("Size")
    );
    fs::remove_file(temp.path().join("data")).unwrap();
    assert!(
        jacui::verify(temp.path())
            .unwrap_err()
            .to_string()
            .contains("Missing")
    );
    fs::write(
        temp.path().join("inventory.json"),
        json!({"files":{"../data":{"bytes":3,"sha256":hash}}}).to_string(),
    )
    .unwrap();
    assert!(
        jacui::verify(temp.path())
            .unwrap_err()
            .to_string()
            .contains("Unsafe")
    );
}

#[test]
fn commands_use_prepro_and_correct_upstream_and_outlet_order() {
    let temp = fixture();
    let mut opts = options(temp.path().join("runs/candidate"));
    let single = invocation(temp.path(), &opts, Stage::SampleMinis).unwrap();
    assert_eq!(single.args[0..2], ["prepro", "sample-minis"]);
    assert!(
        single
            .inputs
            .iter()
            .all(|path| path.starts_with(temp.path().join("expected/bhae")))
    );
    opts.stage = Stage::All;
    let full = invocation(temp.path(), &opts, Stage::SampleMinis).unwrap();
    assert!(
        full.inputs
            .iter()
            .all(|path| path.starts_with(&opts.output_dir))
    );
    for stage in Stage::All.stages() {
        assert_eq!(
            invocation(temp.path(), &opts, stage).unwrap().args[1],
            stage.name()
        );
    }
    opts.network = Network::Tdxhydro;
    let roi = invocation(temp.path(), &opts, Stage::DefineRoi).unwrap();
    let outlets: Vec<_> = roi
        .args
        .windows(2)
        .filter(|pair| pair[0] == "--outlet-id")
        .map(|pair| pair[1].to_string_lossy().to_string())
        .collect();
    assert_eq!(outlets, ["640538827", "640543432", "640538824"]);
    assert!(roi.args.iter().any(|arg| arg == "--segments-source-crs"));
    let aggregate = invocation(temp.path(), &opts, Stage::Aggregate).unwrap();
    for flag in [
        "--roi-catchments",
        "--roi-segments",
        "--uparea-min",
        "--lmin",
        "--workers",
        "--memory-limit-mb",
    ] {
        assert!(aggregate.args.iter().any(|arg| arg == flag));
    }
    let prepare = invocation(temp.path(), &opts, Stage::Prepare).unwrap();
    let flags: Vec<_> = prepare
        .args
        .iter()
        .map(|arg| arg.to_string_lossy())
        .collect();
    assert!(
        flags
            .windows(3)
            .any(|args| args[0] == "--categorical-raster"
                && args[1] == "hru"
                && args[2].ends_with("input/hru.tif"))
    );
    assert!(
        prepare
            .inputs
            .contains(&opts.output_dir.join("mini_catchments.fgb"))
    );
    assert!(
        flags
            .windows(2)
            .any(|args| args[0] == "--dem-scale" && args[1] == "0.01")
    );
    assert!(invocation(temp.path(), &opts, Stage::All).is_err());
}

#[test]
fn protected_outputs_include_other_networks_ancestors_and_nonempty_directories() {
    let temp = fixture();
    for path in [
        temp.path().to_owned(),
        temp.path().join("input/new"),
        temp.path().join("expected/tdxhydro/new"),
    ] {
        assert!(protect_output(temp.path(), &path).is_err());
    }
    let safe = temp.path().join("runs/new");
    assert_eq!(protect_output(temp.path(), &safe).unwrap(), safe);
    fs::create_dir_all(&safe).unwrap();
    fs::write(safe.join("old.log"), b"old").unwrap();
    assert!(protect_output(temp.path(), &safe).is_err());
}

#[cfg(unix)]
#[test]
fn protected_outputs_resolve_symlinks_even_for_missing_children() {
    let temp = fixture();
    std::os::unix::fs::symlink(temp.path().join("expected"), temp.path().join("alias")).unwrap();
    assert!(protect_output(temp.path(), &temp.path().join("alias/new/deeper")).is_err());
    std::os::unix::fs::symlink(temp.path().join("input"), temp.path().join("input-alias")).unwrap();
    assert!(
        protect_output(
            temp.path(),
            &temp.path().join("input-alias/../expected/new")
        )
        .is_err()
    );
}

#[cfg(unix)]
#[test]
fn full_pipeline_records_stage_sequence_prefix_logs_and_benchmarks() {
    let temp = fixture();
    let mock = temp.path().join("candidate.sh");
    fs::write(
        &mock,
        r#"set -eu
test "$1" = prepro
stage=$2
shift 2
while [ "$#" -gt 0 ]; do
  if [ "$1" = --output-dir ]; then out=$2; fi
  shift
done
echo "candidate $stage"
case "$stage" in
  define-roi) files='roi_catchments.fgb roi_segments.fgb' ;;
  aggregate) files='mini_catchments.fgb mini_segments.fgb source_to_mini.csv' ;;
  prepare) files='dem.tif hru.tif grid_catchments.tif grid_segments.tif' ;;
  terrain-products) files='hand.tif ltnd.tif undrained_cells.csv' ;;
  sample-minis) files='sampled_minis.csv' ;;
  *) exit 12 ;;
esac
for file in $files; do : > "$out/$file"; done
"#,
    )
    .unwrap();
    let mut opts = options(temp.path().join("runs/pipeline"));
    opts.stage = Stage::All;
    opts.command = "/bin/sh".into();
    opts.command_arg = vec![mock.into_os_string()];
    let report = run(temp.path(), &opts).unwrap();
    let measurements = report["measurements"].as_array().unwrap();
    assert_eq!(measurements.len(), 5);
    for (measurement, stage) in measurements.iter().zip(Stage::All.stages()) {
        assert_eq!(measurement["stage"], stage.name());
        assert_eq!(measurement["exit_code"], 0);
        assert!(measurement["wall_seconds"].as_f64().unwrap() >= 0.);
        #[cfg(target_os = "linux")]
        assert!(measurement["max_process_rss_kib"].as_i64().unwrap() > 0);
        assert!(
            fs::read_to_string(opts.output_dir.join(format!("{}.log", stage.name())))
                .unwrap()
                .contains(stage.name())
        );
    }
    let saved: Value =
        serde_json::from_slice(&fs::read(opts.output_dir.join("benchmark.json")).unwrap()).unwrap();
    assert_eq!(saved, report);
    assert!(report["revision"].as_str().unwrap().len() == 40);
}

#[cfg(unix)]
#[test]
fn failures_keep_measurements_and_missing_inputs_fail_before_outputs() {
    let temp = fixture();
    let mut opts = options(temp.path().join("runs/failure"));
    opts.command = "/bin/false".into();
    opts.stage = Stage::All;
    assert!(
        run(temp.path(), &opts)
            .unwrap_err()
            .to_string()
            .contains("exit code")
    );
    let report: Value =
        serde_json::from_slice(&fs::read(opts.output_dir.join("benchmark.json")).unwrap()).unwrap();
    assert_eq!(report["measurements"].as_array().unwrap().len(), 1);
    assert_eq!(report["measurements"][0]["exit_code"], 1);
    opts.output_dir = temp.path().join("runs/missing-input");
    fs::remove_file(temp.path().join("input/bhae/catchments.fgb")).unwrap();
    assert!(
        run(temp.path(), &opts)
            .unwrap_err()
            .to_string()
            .contains("fixture missing")
    );
    assert!(!opts.output_dir.exists());
    opts.stage = Stage::SampleMinis;
    opts.command = "/missing/mgb".into();
    assert!(
        run(temp.path(), &opts)
            .unwrap_err()
            .to_string()
            .contains("executable missing")
    );
    opts.command = "nonexistent-mgb-candidate-17".into();
    assert!(
        run(temp.path(), &opts)
            .unwrap_err()
            .to_string()
            .contains("Cannot start candidate")
    );
    let report: Value =
        serde_json::from_slice(&fs::read(opts.output_dir.join("benchmark.json")).unwrap()).unwrap();
    assert!(
        report["measurements"][0]["error"]
            .as_str()
            .unwrap()
            .contains("Cannot start")
    );
}

#[test]
fn csv_checks_types_integer_ids_order_nulls_and_tolerances() {
    let temp = tempfile::tempdir().unwrap();
    let reference = temp.path().join("reference.csv");
    let actual = temp.path().join("actual.csv");
    fs::write(
        &reference,
        "id,value,missing\n640538827,1.1,\n640543432,2.2,\n",
    )
    .unwrap();
    fs::write(
        &actual,
        "id,value,missing\n640543432,2.2,\n640538827,1.10000000005,\n",
    )
    .unwrap();
    compare_csv(&actual, &reference, true).unwrap();
    assert!(compare_csv(&actual, &reference, false).is_err());
    for data in [
        "id,value,missing\n640538828,1.1,\n640543432,2.2,\n",
        "id,value,missing\n640538827,1.11,\n640543432,2.2,\n",
        "id,value,missing\n640538827,1,\n640543432,2,\n",
        "id,value,missing\n640538827,1.1,\n640538827,2.2,\n",
        "value,id,missing\n1.1,640538827,\n2.2,640543432,\n",
    ] {
        fs::write(&actual, data).unwrap();
        assert!(compare_csv(&actual, &reference, true).is_err(), "{data}");
    }
}

fn raster<T: Copy + GdalType>(
    path: &Path,
    values: Vec<T>,
    mask: Vec<u8>,
    index: &str,
    units: &str,
    origin: f64,
) -> Result<()> {
    let mut mem =
        DriverManager::get_driver_by_name("MEM")?.create_with_band_type::<T, _>("", 2, 1, 1)?;
    mem.set_geo_transform(&[origin, 0.01, 0., -20., 0., -0.01])?;
    mem.set_spatial_ref(&SpatialRef::from_epsg(4326)?)?;
    mem.set_metadata_item("mini_index", index, "")?;
    mem.set_metadata_item("units", units, "")?;
    let mut band = mem.rasterband(1)?;
    band.write((0, 0), (2, 1), &mut Buffer::new((2, 1), values))?;
    band.create_mask_band(true)?;
    band.open_mask_band()?
        .write((0, 0), (2, 1), &mut Buffer::new((2, 1), mask))?;
    let options = RasterCreationOptions::from_iter(["BLOCKSIZE=512", "COMPRESS=DEFLATE"]);
    mem.create_copy(&DriverManager::get_driver_by_name("COG")?, path, &options)?
        .flush_cache()?;
    Ok(())
}

#[test]
fn raster_checks_masks_metadata_dtype_grids_and_numeric_tolerances() {
    let temp = tempfile::tempdir().unwrap();
    let reference = temp.path().join("reference.tif");
    let actual = temp.path().join("actual.tif");
    let index = "[[1,0,0,2,1]]";
    raster(&reference, vec![10_f32, 20.], vec![255, 0], index, "m", 0.).unwrap();
    raster(
        &actual,
        vec![10.000005_f32, 999.],
        vec![255, 0],
        "[[1,0.0000000000005,0,2,1]]",
        "m",
        5e-13,
    )
    .unwrap();
    compare_raster(&actual, &reference).unwrap();
    for (values, mask, index, units, origin) in [
        (vec![11_f32, 20.], vec![255, 0], index, "m", 0.),
        (vec![10_f32, 20.], vec![255, 255], index, "m", 0.),
        (vec![10_f32, 20.], vec![255, 0], "[[2,0,0,2,1]]", "m", 0.),
        (vec![10_f32, 20.], vec![255, 0], index, "km", 0.),
        (vec![10_f32, 20.], vec![255, 0], index, "m", 1e-9),
    ] {
        fs::remove_file(&actual).unwrap();
        raster(&actual, values, mask, index, units, origin).unwrap();
        assert!(compare_raster(&actual, &reference).is_err());
    }
    fs::remove_file(&actual).unwrap();
    raster(&actual, vec![10_i32, 20], vec![255, 0], index, "m", 0.).unwrap();
    assert!(compare_raster(&actual, &reference).is_err());
    fs::remove_file(&reference).unwrap();
    raster(&reference, vec![10_i32, 20], vec![255, 0], index, "m", 0.).unwrap();
    compare_raster(&actual, &reference).unwrap();
    fs::remove_file(&actual).unwrap();
    raster(&actual, vec![11_i32, 20], vec![255, 0], index, "m", 0.).unwrap();
    assert!(compare_raster(&actual, &reference).is_err());
}

fn vector(path: &Path, ids: &[i64], reversed: bool, value_type: u32, offset: f64) -> Result<()> {
    let mut ds = DriverManager::get_driver_by_name("FlatGeobuf")?.create_vector_only(path)?;
    let srs = SpatialRef::from_epsg(4326)?;
    let layer = ds.create_layer(LayerOptions {
        name: "minis",
        srs: Some(&srs),
        ty: OGRwkbGeometryType::wkbPolygon,
        options: Some(&["SPATIAL_INDEX=NO"]),
    })?;
    FieldDefn::new("id", OGRFieldType::OFTInteger64)?.add_to_layer(&layer)?;
    FieldDefn::new("value", value_type)?.add_to_layer(&layer)?;
    for id in ids {
        let mut f = Feature::new(layer.defn())?;
        f.set_field_integer64(0, *id)?;
        if value_type == OGRFieldType::OFTReal {
            f.set_field_double(1, *id as f64 + 0.25 + offset)?;
        } else {
            f.set_field_string(1, "value")?;
        }
        let wkt = if reversed {
            "POLYGON ((0 0,0 1,1 1,1 0,0 0))"
        } else {
            "POLYGON ((0 0,1 0,1 1,0 1,0 0))"
        };
        f.set_geometry(Geometry::from_wkt(wkt)?)?;
        f.create(&layer)?;
    }
    ds.flush_cache()?;
    Ok(())
}

#[test]
fn vector_checks_semantic_geometry_schema_ids_and_mini_order() {
    let temp = tempfile::tempdir().unwrap();
    let reference = temp.path().join("reference.fgb");
    let actual = temp.path().join("mini_segments.fgb");
    vector(&reference, &[1, 2], false, OGRFieldType::OFTReal, 0.).unwrap();
    vector(&actual, &[1, 2], true, OGRFieldType::OFTReal, 1e-11).unwrap();
    compare_vector(&actual, &reference).unwrap();
    for (ids, ty, offset) in [
        (vec![2, 1], OGRFieldType::OFTReal, 0.),
        (vec![1, 3], OGRFieldType::OFTReal, 0.),
        (vec![1, 2], OGRFieldType::OFTString, 0.),
        (vec![1, 2], OGRFieldType::OFTReal, 0.1),
    ] {
        fs::remove_file(&actual).unwrap();
        vector(&actual, &ids, false, ty, offset).unwrap();
        assert!(compare_vector(&actual, &reference).is_err());
    }
    let roi = temp.path().join("roi_segments.fgb");
    vector(&roi, &[2, 1], true, OGRFieldType::OFTReal, 0.).unwrap();
    compare_vector(&roi, &reference).unwrap();
    let a = Geometry::from_wkt("POINT (0 0)").unwrap().wkb().unwrap();
    let b = Geometry::from_wkt("POINT (0 1)").unwrap().wkb().unwrap();
    assert!(!jacui::compare::geometry_equal(&a, &b).unwrap());
}

#[test]
fn vector_schema_distinguishes_boolean_and_integer_fields() {
    let temp = tempfile::tempdir().unwrap();
    let reference = temp.path().join("reference.geojson");
    let actual = temp.path().join("actual.geojson");
    let feature = |flag: Value| {
        json!({
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {"id": 1, "flag": flag},
                "geometry": {"type": "Point", "coordinates": [0, 0]}}]
        })
    };
    fs::write(&reference, feature(json!(true)).to_string()).unwrap();
    fs::write(&actual, feature(json!(1)).to_string()).unwrap();
    let error = compare_vector(&actual, &reference).unwrap_err();
    assert!(error.to_string().contains("field names/types/order"));
}

#[test]
fn compare_requires_product_set_and_audit_envelope() {
    let temp = tempfile::tempdir().unwrap();
    let expected = temp.path().join("expected/bhae");
    let actual = temp.path().join("candidate");
    fs::create_dir_all(&expected).unwrap();
    fs::create_dir(&actual).unwrap();
    fs::write(expected.join("sampled_minis.csv"), "id,value\n1,1.1\n").unwrap();
    assert!(
        jacui::compare::compare(temp.path(), Network::Bhae, Stage::SampleMinis, &actual).is_err()
    );
    fs::copy(
        expected.join("sampled_minis.csv"),
        actual.join("sampled_minis.csv"),
    )
    .unwrap();
    assert!(
        jacui::compare::compare(temp.path(), Network::Bhae, Stage::SampleMinis, &actual).is_err()
    );
    fs::write(
        actual.join("manifest-sample-minis.json"),
        json!({"step":"sample-minis","parameters":{}}).to_string(),
    )
    .unwrap();
    jacui::compare::compare(temp.path(), Network::Bhae, Stage::SampleMinis, &actual).unwrap();
    fs::write(actual.join("unexpected.csv"), "id\n1\n").unwrap();
    assert!(
        jacui::compare::compare(temp.path(), Network::Bhae, Stage::SampleMinis, &actual).is_err()
    );
    fs::remove_file(actual.join("unexpected.csv")).unwrap();
    fs::write(
        actual.join("manifest-sample-minis.json"),
        json!({"step":"wrong","parameters":{}}).to_string(),
    )
    .unwrap();
    assert!(
        jacui::compare::compare(temp.path(), Network::Bhae, Stage::SampleMinis, &actual).is_err()
    );
}

#[test]
fn synthetic_references_are_readable_and_cover_planned_routing_cases() {
    let capture: Value =
        serde_json::from_str(include_str!("regression/synthetic/terrain.json")).unwrap();
    let cases = capture["cases"].as_array().unwrap();
    assert_eq!(
        capture["reference_revision"],
        "0e29ede1d2fbb729cbdffebb2eb5b13bebf231c0"
    );
    let names: std::collections::BTreeSet<_> =
        cases.iter().map(|c| c["name"].as_str().unwrap()).collect();
    assert_eq!(names.len(), cases.len());
    for name in [
        "raw-dem-hand-after-agree",
        "valley-before-ridge-breach",
        "drainable-flat-lowest-outlet",
        "multiple-drainages-owner-confinement",
        "d8-cycle",
        "d8-nodata",
        "agree-confinement-nodata",
    ] {
        assert!(names.contains(name));
    }
    assert_eq!(
        cases.iter().filter(|c| c["operation"] == "ltnd").count(),
        14
    );
}

#[test]
#[ignore = "requires local captured datasets and MGB_REGRESSION_COMMAND implementing scientific stages"]
fn jacui_candidate_scientific_regression() {
    let command = std::env::var_os("MGB_REGRESSION_COMMAND")
        .expect("Set MGB_REGRESSION_COMMAND to the candidate executable path");
    let stage = std::env::var("JACUI_STAGE").unwrap_or_else(|_| "all".into());
    let stage = <Stage as clap::ValueEnum>::from_str(&stage, false).expect("Valid JACUI_STAGE");
    for network in [Network::Bhae, Network::Tdxhydro] {
        let output = tempfile::tempdir().unwrap();
        let opts = RunOptions {
            network,
            stage,
            command: command.clone().into(),
            ..options(output.path().join(network.name()))
        };
        run(&jacui::fixture_root(), &opts).unwrap();
        jacui::compare::compare(&jacui::fixture_root(), network, stage, &opts.output_dir).unwrap();
    }
}
