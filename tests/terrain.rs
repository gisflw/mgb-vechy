use anyhow::Result;
use gdal::{
    Dataset, DriverManager, Metadata,
    raster::{Buffer, GdalType, RasterCreationOptions},
    spatial_ref::SpatialRef,
};
use mgb::prepro::{DirectionSource, TerrainSpec, create_terrain_dataset};
use std::{fs, path::Path};

const WIDTH: usize = 5;
const HEIGHT: usize = 4;
// Minis interleave and have overlapping bounding windows. Mini 1's last cell is disconnected.
const OWNERS: [i32; 20] = [1, 2, 1, 2, 0, 2, 1, 2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1];
const MASK: [u8; 20] = [
    255, 255, 255, 255, 0, 255, 255, 255, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 255,
];
const SEGMENTS: [i32; 20] = [1, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0];
const DEM: [f32; 20] = [
    10., 20., 5., 30., 0., 15., 25., 0., 40., 0., 0., 0., 0., 0., 0., 0., 0., 0., 0., 50.,
];
const D8: [u8; 20] = [7, 7, 6, 6, 0, 2, 8, 8, 8, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 255];

struct Fixture {
    _dir: tempfile::TempDir,
    spec: TerrainSpec,
}
impl Fixture {
    fn new() -> Result<Self> {
        let dir = tempfile::tempdir()?;
        let root = dir.path();
        let spec = TerrainSpec {
            overwrite: false,
            io_slots: 2,
            dem: root.join("dem.tif"),
            grid_catchments: root.join("grid_catchments.tif"),
            grid_segments: root.join("grid_segments.tif"),
            output_dir: root.join("out"),
            direction_source: DirectionSource::Dem,
            d8: None,
            write_flow_direction: true,
            agree_sharp: 80.,
            agree_smooth: 8.,
            agree_buffer: 4,
            workers: 4,
            memory_limit_mb: 256,
            routing_bytes_per_cell: 128,
        };
        raster(&spec.dem, &DEM, &MASK, true, None)?;
        raster(&spec.grid_segments, &SEGMENTS, &MASK, false, None)?;
        raster(
            &spec.grid_catchments,
            &OWNERS,
            &MASK,
            false,
            Some("[[1,0,0,5,4],[2,0,2,4,4]]"),
        )?;
        Ok(Self { _dir: dir, spec })
    }
    fn d8(&mut self, values: &[u8], mask: &[u8]) -> Result<()> {
        let path = self.spec.dem.with_file_name("d8.tif");
        raster(&path, values, mask, false, None)?;
        self.spec.d8 = Some(path);
        self.spec.direction_source = DirectionSource::D8;
        Ok(())
    }
}
fn raster<T: Copy + GdalType>(
    path: &Path,
    values: &[T],
    mask: &[u8],
    metres: bool,
    index: Option<&str>,
) -> Result<()> {
    if path.exists() {
        fs::remove_file(path)?;
    }
    let mut ds = DriverManager::get_driver_by_name("MEM")?
        .create_with_band_type::<T, _>("", WIDTH, HEIGHT, 1)?;
    ds.set_geo_transform(&[0., 1., 0., 4., 0., -1.])?;
    ds.set_spatial_ref(&SpatialRef::from_epsg(3857)?)?;
    if metres {
        ds.set_metadata_item("units", "m", "")?;
        assert_eq!(
            unsafe {
                gdal_sys::GDALSetRasterUnitType(ds.rasterband(1)?.c_rasterband(), c"m".as_ptr())
            },
            0
        );
    }
    if let Some(index) = index {
        ds.set_metadata_item("mini_index", index, "")?;
    }
    let mut band = ds.rasterband(1)?;
    band.write(
        (0, 0),
        (WIDTH, HEIGHT),
        &mut Buffer::new((WIDTH, HEIGHT), values.to_vec()),
    )?;
    band.create_mask_band(true)?;
    band.open_mask_band()?.write(
        (0, 0),
        (WIDTH, HEIGHT),
        &mut Buffer::new((WIDTH, HEIGHT), mask.to_vec()),
    )?;
    ds.create_copy(
        &DriverManager::get_driver_by_name("COG")?,
        path,
        &RasterCreationOptions::from_iter(["BLOCKSIZE=512", "COMPRESS=DEFLATE"]),
    )?
    .flush_cache()?;
    Ok(())
}
fn values(path: &Path) -> Result<(Vec<f64>, Vec<u8>)> {
    let ds = Dataset::open(path)?;
    let band = ds.rasterband(1)?;
    Ok((
        band.read_as::<f64>((0, 0), (WIDTH, HEIGHT), (WIDTH, HEIGHT), None)?
            .data()
            .to_vec(),
        band.open_mask_band()?
            .read_as::<u8>((0, 0), (WIDTH, HEIGHT), (WIDTH, HEIGHT), None)?
            .data()
            .to_vec(),
    ))
}
#[test]
fn products_preserve_ownership_masks_metadata_and_worker_determinism() -> Result<()> {
    let mut fixture = Fixture::new()?;
    for mode in [DirectionSource::Dem, DirectionSource::D8] {
        if mode == DirectionSource::D8 {
            fixture.d8(&D8, &MASK)?;
        }
        fixture.spec.routing_bytes_per_cell = 128;
        fixture.spec.output_dir = fixture.spec.dem.with_file_name(format!("{mode:?}-one"));
        fixture.spec.workers = 1;
        let first = create_terrain_dataset(&fixture.spec)?;
        assert_eq!(first.mini_count, 2);
        assert_eq!(first.undrained_count, 1);
        let metadata = Dataset::open(&first.hand)?;
        assert_eq!(
            metadata.metadata_item("agree_sharp", "").as_deref(),
            if mode == DirectionSource::Dem {
                Some("80.0")
            } else {
                None
            }
        );
        assert_eq!(
            metadata.metadata_item("agree_smooth", "").as_deref(),
            if mode == DirectionSource::Dem {
                Some("8.0")
            } else {
                None
            }
        );
        fixture.spec.output_dir = fixture.spec.dem.with_file_name(format!("{mode:?}-many"));
        fixture.spec.workers = 4;
        fixture.spec.routing_bytes_per_cell = 256;
        let second = create_terrain_dataset(&fixture.spec)?;
        assert_eq!(second.workers_used, 2);
        for (left, right) in [
            (&first.hand, &second.hand),
            (&first.ltnd, &second.ltnd),
            (
                first.flow_direction.as_ref().unwrap(),
                second.flow_direction.as_ref().unwrap(),
            ),
        ] {
            let (a, mask) = values(left)?;
            let (b, other_mask) = values(right)?;
            assert_eq!(mask, other_mask);
            for i in 0..20 {
                assert_eq!(mask[i], if MASK[i] != 0 && i != 19 { 255 } else { 0 });
                if mask[i] != 0 {
                    assert_eq!(a[i], b[i]);
                }
            }
            let ds = Dataset::open(left)?;
            assert_eq!(
                ds.metadata_item("LAYOUT", "IMAGE_STRUCTURE").as_deref(),
                Some("COG")
            );
            assert_eq!(ds.rasterband(1)?.block_size(), (512, 512));
            assert!(ds.rasterband(1)?.no_data_value().is_none());
            assert!(!Path::new(&format!("{}.msk", left.display())).exists());
            assert_eq!(
                ds.metadata_item("routing_source", "").as_deref(),
                Some(if mode == DirectionSource::Dem {
                    "dem"
                } else {
                    "d8"
                })
            );
            assert_eq!(
                ds.metadata_item("ownership", "").as_deref(),
                Some("strict aggregated mini catchments; no buffer")
            );
        }
        let (hand, mask) = values(&first.hand)?;
        assert!(hand[2] < 0. && hand[7] < 0.);
        assert_eq!(hand[0], 0.);
        assert_eq!(hand[1], 0.);
        assert_eq!(mask[19], 0);
        let ds = Dataset::open(&first.ltnd)?;
        assert_eq!(ds.metadata_item("units", "").as_deref(), Some("m"));
        assert_eq!(ds.rasterband(1)?.unit(), "m");
        assert_eq!(
            ds.metadata_item("distance_method", "").as_deref(),
            Some("geodesic")
        );
        let ds = Dataset::open(first.flow_direction.unwrap())?;
        assert_eq!(ds.rasterband(1)?.band_type().name(), "Byte");
        assert_eq!(
            ds.metadata_item("direction_codes", "").as_deref(),
            Some("0 drainage, 1-8 N NE E SE S SW W NW")
        );
        let csv = fs::read_to_string(first.undrained_cells)?;
        assert!(csv.contains("1,1,5,20.0"));
        assert_eq!(csv.lines().count(), 2);
        let manifest: serde_json::Value = serde_json::from_reader(fs::File::open(first.manifest)?)?;
        assert_eq!(manifest["step"], "terrain-products");
        assert_eq!(manifest["parameters"]["workers_used"], first.workers_used);
        assert_eq!(manifest["parameters"]["routing_bytes_per_cell"], 128);
        assert!(manifest["elapsed_seconds"].as_f64().unwrap().is_finite());
        assert!(Path::new(manifest["parameters"]["dem"].as_str().unwrap()).is_absolute());
        let second_manifest: serde_json::Value =
            serde_json::from_reader(fs::File::open(second.manifest)?)?;
        assert_eq!(second_manifest["parameters"]["routing_bytes_per_cell"], 256);
    }
    Ok(())
}
#[test]
fn optional_flow_and_header_only_undrained_report() -> Result<()> {
    let mut f = Fixture::new()?;
    f.spec.write_flow_direction = false;
    let mut segments = SEGMENTS;
    segments[19] = 1;
    raster(&f.spec.grid_segments, &segments, &MASK, false, None)?;
    let report = create_terrain_dataset(&f.spec)?;
    assert_eq!(report.undrained_count, 0);
    assert!(report.flow_direction.is_none());
    assert!(!f.spec.output_dir.join("flow_direction.tif").exists());
    assert_eq!(
        fs::read_to_string(report.undrained_cells)?.lines().count(),
        1
    );
    Ok(())
}
#[test]
fn rejects_invalid_inputs_before_publishing() -> Result<()> {
    for kind in [
        "missing-index",
        "malformed-index",
        "loose-index",
        "unknown-owner",
        "segment-id",
        "segment-mask",
        "dem-mask",
        "nonfinite-dem",
        "no-drainage",
        "grid",
        "units",
        "dtype",
        "mask-contract",
    ] {
        let f = Fixture::new()?;
        match kind {
            "missing-index" => raster(&f.spec.grid_catchments, &OWNERS, &MASK, false, None)?,
            "malformed-index" => {
                raster(&f.spec.grid_catchments, &OWNERS, &MASK, false, Some("oops"))?
            }
            "loose-index" => raster(
                &f.spec.grid_catchments,
                &OWNERS,
                &MASK,
                false,
                Some("[[1,0,0,5,4],[2,0,0,5,4]]"),
            )?,
            "unknown-owner" => {
                let mut v = OWNERS;
                v[0] = 3;
                raster(
                    &f.spec.grid_catchments,
                    &v,
                    &MASK,
                    false,
                    Some("[[1,0,0,5,4],[2,0,2,4,4]]"),
                )?;
            }
            "segment-id" => {
                let mut v = SEGMENTS;
                v[2] = 2;
                raster(&f.spec.grid_segments, &v, &MASK, false, None)?;
            }
            "segment-mask" => {
                let mut v = MASK;
                v[0] = 0;
                raster(&f.spec.grid_segments, &SEGMENTS, &v, false, None)?;
            }
            "dem-mask" => {
                let mut v = MASK;
                v[0] = 0;
                raster(&f.spec.dem, &DEM, &v, true, None)?;
            }
            "nonfinite-dem" => {
                let mut v = DEM;
                v[0] = f32::INFINITY;
                raster(&f.spec.dem, &v, &MASK, true, None)?;
            }
            "no-drainage" => raster(&f.spec.grid_segments, &[0i32; 20], &MASK, false, None)?,
            "units" => raster(&f.spec.dem, &DEM, &MASK, false, None)?,
            "dtype" => raster(&f.spec.dem, &DEM.map(|v| v as f64), &MASK, true, None)?,
            "grid" => {
                let mut ds = Dataset::open_ex(
                    &f.spec.dem,
                    gdal::DatasetOptions {
                        open_flags: gdal::GdalOpenFlags::GDAL_OF_UPDATE,
                        open_options: Some(&["IGNORE_COG_LAYOUT_BREAK=YES"]),
                        ..Default::default()
                    },
                )?;
                ds.set_geo_transform(&[0., 1., 0.1, 3., 0., -1.])?;
            }
            "mask-contract" => {
                let ds = Dataset::open_ex(
                    &f.spec.dem,
                    gdal::DatasetOptions {
                        open_flags: gdal::GdalOpenFlags::GDAL_OF_UPDATE,
                        open_options: Some(&["IGNORE_COG_LAYOUT_BREAK=YES"]),
                        ..Default::default()
                    },
                )?;
                ds.rasterband(1)?.set_no_data_value(Some(-999.))?;
            }
            _ => unreachable!(),
        }
        assert!(create_terrain_dataset(&f.spec).is_err(), "{kind}");
        assert!(!f.spec.output_dir.join("hand.tif").exists(), "{kind}");
    }
    Ok(())
}
#[test]
fn d8_errors_and_memory_admission_leave_no_products() -> Result<()> {
    for kind in [
        "cycle",
        "outside",
        "terminal",
        "code",
        "nodata",
        "missing",
        "budget",
        "workers",
        "routing-bytes",
        "sharp",
    ] {
        let mut f = Fixture::new()?;
        let mut codes = D8;
        let mut mask = MASK;
        match kind {
            "cycle" => {
                codes[2] = 6;
                codes[6] = 2;
            }
            "outside" => codes[2] = 3,
            "terminal" => codes[2] = 0,
            "code" => codes[2] = 9,
            "nodata" => mask[2] = 0,
            _ => {}
        }
        f.d8(&codes, &mask)?;
        match kind {
            "missing" => f.spec.d8 = None,
            "budget" => f.spec.memory_limit_mb = 1,
            "workers" => f.spec.workers = 0,
            "routing-bytes" => f.spec.routing_bytes_per_cell = 0,
            "sharp" => f.spec.agree_sharp = f64::NAN,
            _ => {}
        }
        assert!(create_terrain_dataset(&f.spec).is_err(), "{kind}");
        assert!(!f.spec.output_dir.join("hand.tif").exists());
    }
    Ok(())
}
#[test]
fn collision_preserves_existing_products_and_allows_shared_stage_directory() -> Result<()> {
    let f = Fixture::new()?;
    fs::create_dir_all(&f.spec.output_dir)?;
    fs::write(f.spec.output_dir.join("other-stage.csv"), "keep")?;
    create_terrain_dataset(&f.spec)?;
    let before = fs::read(f.spec.output_dir.join("hand.tif"))?;
    assert!(create_terrain_dataset(&f.spec).is_err());
    assert_eq!(before, fs::read(f.spec.output_dir.join("hand.tif"))?);
    assert_eq!(
        fs::read_to_string(f.spec.output_dir.join("other-stage.csv"))?,
        "keep"
    );
    Ok(())
}

#[test]
fn memory_budget_reduces_concurrency_without_changing_products() -> Result<()> {
    let mut f = Fixture::new()?;
    f.spec.memory_limit_mb = 81;
    let limited = mgb::prepro::create_terrain_dataset_with_progress(&f.spec, &|event| {
        if event.phase == "processing" {
            let cache = unsafe { gdal_sys::GDALGetCacheMax64() };
            assert!(cache >= 16 * 1024 * 1024);
            assert!(cache < (81 * 1024 * 1024) / 4);
        }
    })?;
    assert_eq!(limited.workers_used, 1);
    f.spec.memory_limit_mb = 256;
    f.spec.output_dir = f.spec.dem.with_file_name("unlimited");
    let wider = create_terrain_dataset(&f.spec)?;
    for (a, b) in [(&limited.hand, &wider.hand), (&limited.ltnd, &wider.ltnd)] {
        let (left, mask) = values(a)?;
        let (right, other) = values(b)?;
        assert_eq!(mask, other);
        for i in 0..mask.len() {
            if mask[i] != 0 {
                assert_eq!(left[i], right[i]);
            }
        }
    }
    Ok(())
}

#[test]
fn oversized_complete_mini_fails_with_required_budget() -> Result<()> {
    fn capture<T: Copy + GdalType>(path: &Path, data: &[T], dem: bool, index: bool) -> Result<()> {
        let mut ds = DriverManager::get_driver_by_name("MEM")?
            .create_with_band_type::<T, _>("", 2048, 1024, 1)?;
        ds.set_geo_transform(&[0., 1., 0., 1024., 0., -1.])?;
        ds.set_spatial_ref(&SpatialRef::from_epsg(3857)?)?;
        if dem {
            ds.set_metadata_item("units", "m", "")?;
        }
        if index {
            ds.set_metadata_item("mini_index", "[[1,0,0,1024,1024],[2,1024,0,2048,1024]]", "")?;
        }
        let mut band = ds.rasterband(1)?;
        if dem {
            assert_eq!(
                unsafe { gdal_sys::GDALSetRasterUnitType(band.c_rasterband(), c"m".as_ptr()) },
                0
            );
        }
        band.write(
            (0, 0),
            (2048, 1024),
            &mut Buffer::new((2048, 1024), data.to_vec()),
        )?;
        band.create_mask_band(true)?;
        band.open_mask_band()?.fill(255., None)?;
        ds.create_copy(
            &DriverManager::get_driver_by_name("COG")?,
            path,
            &RasterCreationOptions::from_iter(["BLOCKSIZE=512"]),
        )?
        .flush_cache()?;
        Ok(())
    }

    let mut f = Fixture::new()?;
    let owners: Vec<i32> = (0..2048 * 1024)
        .map(|i| if i % 2048 < 1024 { 1 } else { 2 })
        .collect();
    capture(&f.spec.dem, &vec![10f32; 2048 * 1024], true, false)?;
    capture(&f.spec.grid_catchments, &owners, false, true)?;
    capture(&f.spec.grid_segments, &owners, false, false)?;
    f.spec.memory_limit_mb = 128;
    let error = create_terrain_dataset(&f.spec).unwrap_err().to_string();
    assert!(error.contains("Mini 1 requires"), "{error}");
    assert!(error.contains("increase --memory-limit-mb"), "{error}");
    assert!(!f.spec.output_dir.join("hand.tif").exists());
    Ok(())
}

#[test]
fn cancellation_and_invalid_d8_publish_no_products() -> Result<()> {
    let mut f = Fixture::new()?;
    f.d8(&D8, &MASK)?;
    f.spec.direction_source = DirectionSource::D8;
    f.spec.output_dir = f.spec.dem.with_file_name("cancelled");
    assert!(
        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            mgb::prepro::create_terrain_dataset_with_progress(&f.spec, &|event| {
                if event.phase == "processing" && event.completed > 0 {
                    panic!("cancelled by caller");
                }
            })
        }))
        .is_err()
    );
    assert_eq!(fs::read_dir(&f.spec.output_dir)?.count(), 0);
    let mut invalid = D8;
    invalid[0] = 9;
    f.d8(&invalid, &MASK)?;
    f.spec.output_dir = f.spec.dem.with_file_name("failed");
    assert!(create_terrain_dataset(&f.spec).is_err());
    assert_eq!(fs::read_dir(&f.spec.output_dir)?.count(), 0);
    Ok(())
}
