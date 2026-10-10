use anyhow::Result;
use gdal::{
    Dataset, DriverManager, Metadata,
    raster::{Buffer, GdalType},
    spatial_ref::SpatialRef,
    vector::{
        Feature, FieldDefn, Geometry, LayerAccess, LayerOptions, OGRFieldType, OGRwkbGeometryType,
    },
};
use mgb::prepro::{
    D8Encoding, DirectionSource, NamedRaster, PreparationSpec, RasterKind, TerrainSpec,
    create_terrain_dataset, prepare_dataset,
};
use serde_json::Value;
use std::{fs, path::Path};

fn vector(path: &Path, rows: &[(i64, Option<i64>, &str)], polygon: bool) -> Result<()> {
    let mut ds = DriverManager::get_driver_by_name("FlatGeobuf")?.create_vector_only(path)?;
    let crs = SpatialRef::from_epsg(3857)?;
    let layer = ds.create_layer(LayerOptions {
        name: "minis",
        srs: Some(&crs),
        ty: OGRwkbGeometryType::wkbUnknown,
        options: Some(&["SPATIAL_INDEX=NO"]),
    })?;
    FieldDefn::new("id", OGRFieldType::OFTInteger64)?.add_to_layer(&layer)?;
    if !polygon {
        FieldDefn::new("id_down", OGRFieldType::OFTInteger64)?.add_to_layer(&layer)?;
    }
    for &(id, down, wkt) in rows {
        let mut f = Feature::new(layer.defn())?;
        f.set_field_integer64(0, id)?;
        if !polygon {
            if let Some(id) = down {
                f.set_field_integer64(1, id)?;
            } else {
                f.set_field_null(1)?;
            }
        }
        f.set_geometry(Geometry::from_wkt(wkt)?)?;
        f.create(&layer)?;
    }
    ds.flush_cache()?;
    Ok(())
}
fn raster<T: Copy + GdalType>(
    path: &Path,
    width: usize,
    height: usize,
    values: Vec<T>,
    mask: Option<Vec<u8>>,
) -> Result<()> {
    let mut ds = DriverManager::get_driver_by_name("GTiff")?
        .create_with_band_type::<T, _>(path, width, height, 1)?;
    ds.set_geo_transform(&[0., 1., 0., height as f64, 0., -1.])?;
    ds.set_spatial_ref(&SpatialRef::from_epsg(3857)?)?;
    let mut band = ds.rasterband(1)?;
    band.write(
        (0, 0),
        (width, height),
        &mut Buffer::new((width, height), values),
    )?;
    if let Some(mask) = mask {
        band.create_mask_band(true)?;
        band.open_mask_band()?.write(
            (0, 0),
            (width, height),
            &mut Buffer::new((width, height), mask),
        )?;
    }
    ds.flush_cache()?;
    Ok(())
}
fn fixture() -> Result<(tempfile::TempDir, PreparationSpec)> {
    let root = tempfile::tempdir()?;
    let spec = PreparationSpec {
        overwrite: false,
        io_slots: 2,
        dem: root.path().join("source.tif"),
        mini_catchments: root.path().join("catchments.fgb"),
        mini_segments: root.path().join("segments.fgb"),
        output_dir: root.path().join("prepared"),
        rasters: vec![],
        d8: None,
        d8_encoding: None,
        dem_scale: 1.,
        workers: 4,
        memory_limit_mb: 256,
    };
    raster(
        &spec.dem,
        2,
        2,
        vec![1200_f32, -9999., 3400., 5600.],
        Some(vec![255, 0, 255, 255]),
    )?;
    vector(
        &spec.mini_catchments,
        &[(1, None, "POLYGON ((0 0,2 0,2 2,0 2,0 0))")],
        true,
    )?;
    vector(
        &spec.mini_segments,
        &[(1, None, "LINESTRING (0.5 0.5,1.5 0.5)")],
        false,
    )?;
    Ok((root, spec))
}
fn values<T: Copy + GdalType>(path: &Path) -> Result<Vec<T>> {
    let ds = Dataset::open(path)?;
    let size = ds.raster_size();
    Ok(ds
        .rasterband(1)?
        .read_as::<T>((0, 0), size, size, None)?
        .data()
        .to_vec())
}
fn mask(path: &Path) -> Result<Vec<u8>> {
    let ds = Dataset::open(path)?;
    let size = ds.raster_size();
    Ok(ds
        .rasterband(1)?
        .open_mask_band()?
        .read_as::<u8>((0, 0), size, size, None)?
        .data()
        .to_vec())
}
fn empty_output(spec: &PreparationSpec) -> Result<()> {
    assert!(!spec.output_dir.exists() || fs::read_dir(&spec.output_dir)?.next().is_none());
    Ok(())
}

#[test]
fn scaling_only_dem_types_masks_units_index_and_manifest() -> Result<()> {
    let (root, mut spec) = fixture()?;
    let land = root.path().join("land.tif");
    let d8 = root.path().join("directions.tif");
    raster(&land, 2, 2, vec![1_i16, 2, 3, 4], None)?;
    raster(&d8, 2, 2, vec![64_u8, 128, 1, 0], None)?;
    spec.dem_scale = 0.01;
    spec.rasters = vec![
        NamedRaster {
            name: "land".into(),
            path: land,
            kind: RasterKind::Categorical,
        },
        NamedRaster {
            name: "other".into(),
            path: spec.dem.clone(),
            kind: RasterKind::Continuous,
        },
    ];
    spec.d8 = Some(d8);
    spec.d8_encoding = Some(D8Encoding::Esri);
    let report = prepare_dataset(&spec)?;
    assert_eq!(values::<f32>(&report.dem)?, vec![12., 0., 34., 56.]);
    assert_eq!(mask(&report.dem)?, vec![255, 0, 255, 255]);
    assert_eq!(
        values::<f32>(&report.rasters["other"])?,
        vec![1200., 0., 3400., 5600.]
    );
    assert_eq!(values::<i32>(&report.rasters["land"])?, vec![1, 2, 3, 4]);
    assert_eq!(values::<u8>(report.d8.as_ref().unwrap())?, vec![1, 2, 3, 0]);
    assert_eq!(values::<i32>(&report.grid_catchments)?, vec![1, 1, 1, 1]);
    assert_eq!(values::<i32>(&report.grid_segments)?, vec![0, 0, 1, 1]);
    assert_eq!(mask(&report.grid_catchments)?, mask(&report.grid_segments)?);
    let ds = Dataset::open(&report.dem)?;
    assert_eq!(ds.metadata_item("units", "").as_deref(), Some("m"));
    assert_eq!(ds.rasterband(1)?.unit(), "m");
    assert_eq!(ds.metadata_item("dem_scale", "").as_deref(), Some("0.01"));
    for path in [
        &report.dem,
        &report.grid_catchments,
        &report.grid_segments,
        &report.rasters["land"],
        report.d8.as_ref().unwrap(),
    ] {
        let ds = Dataset::open(path)?;
        let band = ds.rasterband(1)?;
        assert_eq!(
            ds.metadata_item("LAYOUT", "IMAGE_STRUCTURE").as_deref(),
            Some("COG")
        );
        assert_eq!(band.block_size(), (512, 512));
        assert!(band.mask_flags()?.is_per_dataset());
        assert_eq!(band.no_data_value(), None);
        assert!(!Path::new(&format!("{}.msk", path.display())).exists());
    }
    let ds = Dataset::open(&report.grid_catchments)?;
    let index: Value = serde_json::from_str(&ds.metadata_item("mini_index", "").unwrap())?;
    assert_eq!(index, serde_json::json!([[1, 0., 0., 2., 2.]]));
    let manifest: Value = serde_json::from_slice(&fs::read(&report.manifest)?)?;
    assert_eq!(manifest["step"], "prepare");
    assert_eq!(manifest["parameters"]["workers_used"], 1);
    assert!(manifest["elapsed_seconds"].as_f64().unwrap().is_finite());
    assert_eq!(
        manifest["parameters"]["dem"],
        fs::canonicalize(&spec.dem)?.to_str().unwrap()
    );
    assert_eq!(fs::read_dir(&spec.output_dir)?.count(), 7);
    Ok(())
}

#[test]
fn wide_scaling_nodata_metadata_and_nonfinite_continuous_values() -> Result<()> {
    for (value, scale, expected) in [(1e40, 1e-5, 1e35_f32), (1e35, 1e-45, 1e-10_f32)] {
        let (_root, mut spec) = fixture()?;
        fs::remove_file(&spec.dem)?;
        raster(&spec.dem, 2, 2, vec![value; 4], None)?;
        spec.dem_scale = scale;
        let report = prepare_dataset(&spec)?;
        assert_eq!(values::<f32>(&report.dem)?, vec![expected; 4]);
    }
    let (_root, spec) = fixture()?;
    fs::remove_file(&spec.dem)?;
    raster(
        &spec.dem,
        2,
        2,
        vec![1_f64, f64::NAN, f64::INFINITY, -9999.],
        None,
    )?;
    {
        let mut ds = Dataset::open_ex(
            &spec.dem,
            gdal::DatasetOptions {
                open_flags: gdal::GdalOpenFlags::GDAL_OF_UPDATE,
                ..Default::default()
            },
        )?;
        let mut band = ds.rasterband(1)?;
        band.set_no_data_value(Some(-9999.))?;
        band.set_scale(100.)?;
        band.set_offset(1000.)?;
        ds.flush_cache()?;
    }
    let report = prepare_dataset(&spec)?;
    assert_eq!(values::<f32>(&report.dem)?, vec![1., 0., 0., 0.]);
    assert_eq!(mask(&report.dem)?, vec![255, 0, 0, 0]);
    Ok(())
}

#[test]
fn stream_overlay_halo_tight_bounds_and_disconnected_terrain() -> Result<()> {
    let (_root, spec) = fixture()?;
    fs::remove_file(&spec.dem)?;
    raster(&spec.dem, 10, 10, vec![10_f32; 100], None)?;
    fs::remove_file(&spec.mini_catchments)?;
    fs::remove_file(&spec.mini_segments)?;
    vector(
        &spec.mini_catchments,
        &[
            (2, None, "POLYGON ((5 4,6 4,6 6,5 6,5 4))"),
            (
                1,
                None,
                "MULTIPOLYGON (((2 2,4 2,4 4,2 4,2 2)),((7 7,8 7,8 8,7 8,7 7)))",
            ),
        ],
        true,
    )?;
    vector(
        &spec.mini_segments,
        &[
            (2, Some(-1), "LINESTRING (5.5 4.5,5.5 5.5)"),
            (1, None, "LINESTRING (0.5 0.5,8.5 0.5)"),
        ],
        false,
    )?;
    let report = prepare_dataset(&spec)?;
    let ds = Dataset::open(&report.grid_catchments)?;
    assert_eq!(ds.raster_size(), (10, 9));
    assert_eq!(ds.geo_transform()?, [0., 1., 0., 9., 0., -1.]);
    let cells = values::<i32>(&report.grid_catchments)?;
    let streams = values::<i32>(&report.grid_segments)?;
    assert_eq!(cells[8 * 10], 1);
    assert_eq!(cells[8 * 10 + 8], 1);
    assert_eq!(cells[10 + 7], 1);
    assert_eq!(cells[3 * 10 + 5], 2);
    for (&id, &owner) in streams.iter().zip(&cells) {
        assert!(id == 0 || id == owner);
    }
    assert_eq!(mask(&report.grid_catchments)?, mask(&report.grid_segments)?);
    let index: Value = serde_json::from_str(&ds.metadata_item("mini_index", "").unwrap())?;
    assert_eq!(
        index,
        serde_json::json!([[1, 0., 0., 9., 8.], [2, 5., 4., 6., 6.]])
    );
    let terrain = create_terrain_dataset(&TerrainSpec {
        overwrite: false,
        io_slots: 2,
        dem: report.dem,
        grid_catchments: report.grid_catchments,
        grid_segments: report.grid_segments,
        output_dir: spec.output_dir.join("terrain"),
        direction_source: DirectionSource::Dem,
        d8: None,
        write_flow_direction: false,
        agree_sharp: 80.,
        agree_smooth: 8.,
        agree_buffer: 4,
        workers: 2,
        memory_limit_mb: 256,
        routing_bytes_per_cell: 128,
    })?;
    assert!(terrain.undrained_count > 0);
    assert_eq!(mask(&terrain.hand)?[10 + 7], 0);
    Ok(())
}

#[test]
fn parallel_matches_serial_across_blocks_and_cli_accepts_repeated_rasters() -> Result<()> {
    let (root, mut spec) = fixture()?;
    fs::remove_file(&spec.dem)?;
    raster(
        &spec.dem,
        1030,
        12,
        (0..1030 * 12).map(|i| i as f32).collect(),
        None,
    )?;
    fs::remove_file(&spec.mini_catchments)?;
    fs::remove_file(&spec.mini_segments)?;
    vector(
        &spec.mini_catchments,
        &[(1, None, "POLYGON ((0 0,1030 0,1030 12,0 12,0 0))")],
        true,
    )?;
    vector(
        &spec.mini_segments,
        &[(1, None, "LINESTRING (0.25 0.25,512 6,1029.75 11.75)")],
        false,
    )?;
    spec.workers = 1;
    let serial = prepare_dataset(&spec)?;
    spec.workers = 4;
    spec.output_dir = root.path().join("parallel");
    let parallel = prepare_dataset(&spec)?;
    for (a, b) in [
        (&serial.dem, &parallel.dem),
        (&serial.grid_catchments, &parallel.grid_catchments),
        (&serial.grid_segments, &parallel.grid_segments),
    ] {
        assert_eq!(values::<f64>(a)?, values::<f64>(b)?);
        assert_eq!(mask(a)?, mask(b)?);
    }
    assert!((1..=3).contains(&parallel.workers_used));
    spec.memory_limit_mb = 130;
    spec.output_dir = root.path().join("limited");
    let limited = prepare_dataset(&spec)?;
    assert_eq!(limited.workers_used, 1);
    assert_eq!(
        values::<i32>(&limited.grid_segments)?,
        values::<i32>(&serial.grid_segments)?
    );
    spec.memory_limit_mb = 256;

    let output = std::process::Command::new(env!("CARGO_BIN_EXE_mgb"))
        .arg("prepro")
        .arg("prepare")
        .arg("--dem")
        .arg(&spec.dem)
        .arg("--mini-catchments")
        .arg(&spec.mini_catchments)
        .arg("--mini-segments")
        .arg(&spec.mini_segments)
        .arg("--continuous-raster")
        .arg("first")
        .arg(&spec.dem)
        .arg("--continuous-raster")
        .arg("second")
        .arg(&spec.dem)
        .arg("--categorical-raster")
        .arg("land")
        .arg(&spec.dem)
        .arg("--output-dir")
        .arg(root.path().join("cli"))
        .arg("--memory-limit-mb")
        .arg("256")
        .output()?;
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    for name in ["first", "second", "land"] {
        assert!(
            root.path()
                .join("cli")
                .join(format!("{name}.tif"))
                .is_file()
        );
    }
    Ok(())
}

#[test]
fn invalid_parameters_fail_before_publication() -> Result<()> {
    let (_root, spec) = fixture()?;
    for value in [0., -1., f64::NAN, f64::INFINITY] {
        let mut invalid = spec.clone();
        invalid.dem_scale = value;
        assert!(prepare_dataset(&invalid).is_err());
        empty_output(&invalid)?;
    }
    for name in [
        "dem",
        "d8",
        "grid_catchments",
        "grid_segments",
        "cells",
        "drainage",
        "../bad",
        "Upper",
        "",
        "a.b",
    ] {
        let mut invalid = spec.clone();
        invalid.rasters = vec![NamedRaster {
            name: name.into(),
            path: spec.dem.clone(),
            kind: RasterKind::Continuous,
        }];
        assert!(prepare_dataset(&invalid).is_err());
        empty_output(&invalid)?;
    }
    let mut invalid = spec.clone();
    invalid.rasters = vec![
        NamedRaster {
            name: "same".into(),
            path: spec.dem.clone(),
            kind: RasterKind::Continuous
        };
        2
    ];
    assert!(prepare_dataset(&invalid).is_err());
    empty_output(&invalid)?;
    for (workers, memory) in [(0, 256), (1, 0), (1, 16), (1, usize::MAX)] {
        let mut invalid = spec.clone();
        invalid.workers = workers;
        invalid.memory_limit_mb = memory;
        assert!(prepare_dataset(&invalid).is_err());
        empty_output(&invalid)?;
    }
    let mut invalid = spec.clone();
    invalid.d8 = Some(spec.dem.clone());
    assert!(prepare_dataset(&invalid).is_err());
    empty_output(&invalid)?;
    let mut invalid = spec.clone();
    invalid.d8_encoding = Some(D8Encoding::Canonical);
    assert!(prepare_dataset(&invalid).is_err());
    empty_output(&invalid)?;
    Ok(())
}

#[test]
fn worker_value_failures_clean_outputs_and_collisions_preserve_existing_files() -> Result<()> {
    for (value, kind) in [
        (0.5, RasterKind::Categorical),
        (f64::INFINITY, RasterKind::Categorical),
        (2147483648., RasterKind::Categorical),
        (1e40, RasterKind::Continuous),
    ] {
        let (root, mut spec) = fixture()?;
        let path = root.path().join("bad.tif");
        raster(&path, 2, 2, vec![value; 4], None)?;
        spec.rasters = vec![NamedRaster {
            name: "bad".into(),
            path,
            kind,
        }];
        assert!(prepare_dataset(&spec).is_err());
        empty_output(&spec)?;
    }
    let (_root, mut spec) = fixture()?;
    fs::remove_file(&spec.dem)?;
    raster(&spec.dem, 2, 2, vec![1e40_f64; 4], None)?;
    spec.dem_scale = 1e10;
    assert!(prepare_dataset(&spec).is_err());
    empty_output(&spec)?;
    let (_root, mut spec) = fixture()?;
    spec.d8 = Some(spec.dem.clone());
    spec.d8_encoding = Some(D8Encoding::Esri);
    assert!(prepare_dataset(&spec).is_err());
    empty_output(&spec)?;
    let (_root, spec) = fixture()?;
    fs::create_dir(&spec.output_dir)?;
    fs::write(spec.output_dir.join("dem.tif"), b"previous")?;
    assert!(prepare_dataset(&spec).is_err());
    assert_eq!(fs::read(spec.output_dir.join("dem.tif"))?, b"previous");
    assert_eq!(fs::read_dir(&spec.output_dir)?.count(), 1);
    Ok(())
}

#[test]
fn invalid_source_grids_and_insufficient_coverage() -> Result<()> {
    for transform in [
        [0., 1., 1., 2., 0., -1.],
        [0., 2., 0., 2., 0., -1.],
        [0.25, 1., 0., 2., 0., -1.],
        [1., 1., 0., 2., 0., -1.],
    ] {
        let (root, mut spec) = fixture()?;
        let path = root.path().join("land.tif");
        raster(&path, 2, 2, vec![1_i32; 4], None)?;
        {
            let mut ds = Dataset::open_ex(
                &path,
                gdal::DatasetOptions {
                    open_flags: gdal::GdalOpenFlags::GDAL_OF_UPDATE,
                    ..Default::default()
                },
            )?;
            ds.set_geo_transform(&transform)?;
            ds.flush_cache()?;
        }
        spec.rasters = vec![NamedRaster {
            name: "land".into(),
            path,
            kind: RasterKind::Categorical,
        }];
        assert!(prepare_dataset(&spec).is_err());
        empty_output(&spec)?;
    }
    for crs in ["", "EPSG:4326"] {
        let (_root, spec) = fixture()?;
        {
            let mut ds = Dataset::open_ex(
                &spec.dem,
                gdal::DatasetOptions {
                    open_flags: gdal::GdalOpenFlags::GDAL_OF_UPDATE,
                    ..Default::default()
                },
            )?;
            ds.set_projection(crs)?;
            ds.flush_cache()?;
        }
        assert!(prepare_dataset(&spec).is_err());
        empty_output(&spec)?;
    }
    let (_root, spec) = fixture()?;
    fs::remove_file(&spec.mini_segments)?;
    vector(
        &spec.mini_segments,
        &[(1, None, "LINESTRING (-1 0,1 1)")],
        false,
    )?;
    assert!(prepare_dataset(&spec).is_err());
    empty_output(&spec)?;
    Ok(())
}

#[test]
fn invalid_dense_ids_geometry_and_downstream_targets() -> Result<()> {
    for rows in [
        vec![(2, None, "POLYGON ((0 0,2 0,2 2,0 2,0 0))")],
        vec![(1, None, "POINT (0 0)")],
        vec![(1, None, "POLYGON ((0 0,2 2,2 0,0 2,0 0))")],
        vec![(1, None, "POLYGON EMPTY")],
        vec![
            (1, None, "POLYGON ((0 0,2 0,2 2,0 2,0 0))"),
            (1, None, "POLYGON ((0 0,2 0,2 2,0 2,0 0))"),
        ],
    ] {
        let (_root, spec) = fixture()?;
        fs::remove_file(&spec.mini_catchments)?;
        vector(&spec.mini_catchments, &rows, true)?;
        assert!(prepare_dataset(&spec).is_err());
        empty_output(&spec)?;
    }
    for down in [Some(1), Some(2), Some(0)] {
        let (_root, spec) = fixture()?;
        fs::remove_file(&spec.mini_segments)?;
        vector(
            &spec.mini_segments,
            &[(1, down, "LINESTRING (0 0,1 1)")],
            false,
        )?;
        assert!(prepare_dataset(&spec).is_err());
        empty_output(&spec)?;
    }
    let (root, spec) = fixture()?;
    fs::write(
        root.path().join("float.geojson"),
        r#"{"type":"FeatureCollection","crs":{"type":"name","properties":{"name":"EPSG:3857"}},"features":[{"type":"Feature","properties":{"id":1.0},"geometry":{"type":"Polygon","coordinates":[[[0,0],[2,0],[2,2],[0,2],[0,0]]]}}]}"#,
    )?;
    let mut invalid = spec.clone();
    invalid.mini_catchments = root.path().join("float.geojson");
    assert!(prepare_dataset(&invalid).is_err());
    empty_output(&invalid)?;
    Ok(())
}

#[test]
fn masked_invalid_categories_and_d8_do_not_fail() -> Result<()> {
    let (root, mut spec) = fixture()?;
    let path = root.path().join("land.tif");
    raster(
        &path,
        2,
        2,
        vec![1_f64, f64::INFINITY, 2., 3.],
        Some(vec![255, 0, 255, 255]),
    )?;
    spec.rasters = vec![NamedRaster {
        name: "land".into(),
        path,
        kind: RasterKind::Categorical,
    }];
    let path = root.path().join("d8.tif");
    raster(
        &path,
        2,
        2,
        vec![0_f64, f64::NAN, 8., 1.],
        Some(vec![255, 0, 255, 255]),
    )?;
    spec.d8 = Some(path);
    spec.d8_encoding = Some(D8Encoding::Canonical);
    let report = prepare_dataset(&spec)?;
    assert_eq!(values::<i32>(&report.rasters["land"])?, vec![1, 0, 2, 3]);
    assert_eq!(values::<u8>(report.d8.as_ref().unwrap())?, vec![0, 0, 8, 1]);
    assert_eq!(mask(&report.rasters["land"])?, vec![255, 0, 255, 255]);
    Ok(())
}

#[test]
fn rejects_multiple_bands_null_fields_and_mismatched_vector_ids_or_crs() -> Result<()> {
    let (_root, spec) = fixture()?;
    fs::remove_file(&spec.dem)?;
    let mut ds = DriverManager::get_driver_by_name("GTiff")?
        .create_with_band_type::<f32, _>(&spec.dem, 2, 2, 2)?;
    ds.set_spatial_ref(&SpatialRef::from_epsg(3857)?)?;
    ds.set_geo_transform(&[0., 1., 0., 2., 0., -1.])?;
    ds.flush_cache()?;
    drop(ds);
    assert!(prepare_dataset(&spec).is_err());
    empty_output(&spec)?;
    for (properties, crs) in [
        (serde_json::json!({"id":null,"id_down":-1}), "EPSG:3857"),
        (serde_json::json!({"id":true,"id_down":-1}), "EPSG:3857"),
        (serde_json::json!({"id":1}), "EPSG:3857"),
        (serde_json::json!({"id":1,"id_down":-1}), "EPSG:4326"),
    ] {
        let (root, mut spec) = fixture()?;
        let path = root.path().join("segments.geojson");
        fs::write(
            &path,
            serde_json::to_vec(
                &serde_json::json!({"type":"FeatureCollection","crs":{"type":"name","properties":{"name":crs}},"features":[{"type":"Feature","properties":properties,"geometry":{"type":"LineString","coordinates":[[0.5,0.5],[1.5,0.5]]}}]}),
            )?,
        )?;
        spec.mini_segments = path;
        assert!(prepare_dataset(&spec).is_err());
        empty_output(&spec)?;
    }
    let (_root, spec) = fixture()?;
    fs::remove_file(&spec.mini_segments)?;
    vector(
        &spec.mini_segments,
        &[
            (1, None, "LINESTRING (0 0,1 1)"),
            (2, None, "LINESTRING (1 0,2 1)"),
        ],
        false,
    )?;
    assert!(prepare_dataset(&spec).is_err());
    empty_output(&spec)?;
    Ok(())
}

#[test]
fn replacement_removes_previous_optional_rasters_and_preserves_inputs() -> Result<()> {
    let (_root, mut spec) = fixture()?;
    let raster_path = spec.dem.with_file_name("land-source.tif");
    raster(&raster_path, 2, 2, vec![1i32; 4], None)?;
    spec.rasters.push(NamedRaster {
        name: "land".into(),
        path: raster_path.clone(),
        kind: RasterKind::Categorical,
    });
    prepare_dataset(&spec)?;
    fs::write(spec.output_dir.join("unrelated.txt"), "keep")?;
    spec.overwrite = true;
    spec.rasters.clear();
    let events = std::sync::Mutex::new(Vec::new());
    let report = mgb::prepro::prepare_dataset_with_progress(&spec, &|event| {
        events.lock().unwrap().push(event)
    })?;
    assert!(!spec.output_dir.join("land.tif").exists());
    assert!(raster_path.exists());
    assert_eq!(
        fs::read_to_string(spec.output_dir.join("unrelated.txt"))?,
        "keep"
    );
    let events = events.into_inner().unwrap();
    for phase in ["preparing", "processing", "finalizing"] {
        assert!(events.iter().any(|e| e.phase == phase));
    }
    assert!(report.timings.total > 0.);
    spec.dem = report.dem;
    assert!(
        prepare_dataset(&spec)
            .unwrap_err()
            .to_string()
            .contains("input")
    );
    Ok(())
}
