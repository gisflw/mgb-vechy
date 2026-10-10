use anyhow::Result;
use gdal::{
    DriverManager, Metadata,
    raster::{Buffer, GdalType, RasterCreationOptions},
    spatial_ref::SpatialRef,
    vector::{
        Feature, FieldDefn, Geometry, LayerAccess, LayerOptions, OGRFieldType, OGRwkbGeometryType,
    },
};
use mgb::prepro::{SamplingSpec, sample_minibasins};
use serde_json::Value;
use std::{fs, path::Path, process::Command};

const ATTRIBUTES: [&str; 8] = [
    "id",
    "id_down",
    "sub",
    "p_order",
    "unit_length",
    "upstream_length",
    "unit_area",
    "upstream_area",
];
const DEM: [f32; 8] = [0., 10., 100., 110., 20., 30., 120., 130.];
const HAND: [f32; 8] = [-1., 1.1, 2., 3., 100., 101., 4., 5.];
const LTND: [f32; 8] = [1000., 999.995, 500., 1000., 3., 2., 100., 100.];
const OWNERS: [i32; 8] = [2, 2, 12, 12, 2, 2, 12, 12];
const SEGMENTS: [i32; 8] = [2, 2, 12, 12, 0, 0, 0, 0];
const HRU: [i32; 8] = [1, 2, 1, 3, 2, 2, 3, 3];

struct Fixture {
    _directory: tempfile::TempDir,
    spec: SamplingSpec,
    crs: SpatialRef,
    affine: [f64; 6],
}
impl Fixture {
    fn new(epsg: u32) -> Result<Self> {
        let directory = tempfile::tempdir()?;
        let root = directory.path();
        let spec = SamplingSpec {
            overwrite: false,
            io_slots: 2,
            mini_catchments: root.join("mini_catchments.fgb"),
            mini_segments: root.join("mini_segments.fgb"),
            dem: root.join("dem.tif"),
            grid_catchments: root.join("grid_catchments.tif"),
            grid_segments: root.join("grid_segments.tif"),
            hand: root.join("hand.tif"),
            ltnd: root.join("ltnd.tif"),
            hru: root.join("hru.tif"),
            output_dir: root.join("sampled"),
            workers: 2,
            memory_limit_mb: 128,
        };
        let fixture = Self {
            _directory: directory,
            spec,
            crs: SpatialRef::from_epsg(epsg)?,
            affine: if epsg == 3857 {
                [1000., 100., 0., 8399737., 0., -200.]
            } else {
                [-45., 0.01, 0., -13., 0., -0.02]
            },
        };
        fixture.vectors(false, 2., 0., false)?;
        fixture.vectors(true, 2., 0., false)?;
        fixture.raster(&fixture.spec.dem, &DEM, &[255; 8], true, "m")?;
        fixture.raster(&fixture.spec.hand, &HAND, &[255; 8], true, "m")?;
        fixture.raster(&fixture.spec.ltnd, &LTND, &[255; 8], false, "m")?;
        fixture.raster(&fixture.spec.grid_catchments, &OWNERS, &[255; 8], false, "")?;
        fixture.raster(&fixture.spec.grid_segments, &SEGMENTS, &[255; 8], false, "")?;
        fixture.raster(&fixture.spec.hru, &HRU, &[255; 8], false, "")?;
        Ok(fixture)
    }
    fn raster<T: Copy + GdalType>(
        &self,
        path: &Path,
        data: &[T],
        mask: &[u8],
        units: bool,
        unit: &str,
    ) -> Result<()> {
        if path.exists() {
            fs::remove_file(path)?;
        }
        let mut ds =
            DriverManager::get_driver_by_name("MEM")?.create_with_band_type::<T, _>("", 4, 2, 1)?;
        ds.set_geo_transform(&self.affine)?;
        ds.set_spatial_ref(&self.crs)?;
        if units || path == self.spec.ltnd {
            ds.set_metadata_item("units", unit, "")?;
        }
        if path == self.spec.ltnd {
            ds.set_metadata_item("distance_method", "geodesic", "")?;
        }
        if path == self.spec.grid_catchments {
            let t = self.affine;
            ds.set_metadata_item(
                "mini_index",
                &serde_json::to_string(&vec![
                    (2, t[0], t[3] + 2. * t[5], t[0] + 2. * t[1], t[3]),
                    (
                        12,
                        t[0] + 2. * t[1],
                        t[3] + 2. * t[5],
                        t[0] + 4. * t[1],
                        t[3],
                    ),
                ])?,
                "",
            )?;
        }
        let mut band = ds.rasterband(1)?;
        if units || path == self.spec.ltnd {
            let unit = std::ffi::CString::new(unit)?;
            assert_eq!(
                unsafe { gdal_sys::GDALSetRasterUnitType(band.c_rasterband(), unit.as_ptr()) },
                0
            );
        }
        band.write((0, 0), (4, 2), &mut Buffer::new((4, 2), data.to_vec()))?;
        band.create_mask_band(true)?;
        band.open_mask_band()?
            .write((0, 0), (4, 2), &mut Buffer::new((4, 2), mask.to_vec()))?;
        ds.create_copy(
            &DriverManager::get_driver_by_name("COG")?,
            path,
            &RasterCreationOptions::from_iter(["BLOCKSIZE=512", "COMPRESS=DEFLATE"]),
        )?
        .flush_cache()?;
        Ok(())
    }
    fn vectors(
        &self,
        segments: bool,
        reach_length: f64,
        offset: f64,
        float_sub: bool,
    ) -> Result<()> {
        let path = if segments {
            &self.spec.mini_segments
        } else {
            &self.spec.mini_catchments
        };
        if path.exists() {
            fs::remove_file(path)?;
        }
        let mut ds = DriverManager::get_driver_by_name("FlatGeobuf")?.create_vector_only(path)?;
        let layer = ds.create_layer(LayerOptions {
            name: "minis",
            srs: Some(&self.crs),
            ty: if segments {
                OGRwkbGeometryType::wkbLineString
            } else {
                OGRwkbGeometryType::wkbPolygon
            },
            options: Some(&["SPATIAL_INDEX=NO"]),
        })?;
        for (i, name) in ATTRIBUTES.iter().enumerate() {
            let ty = if i < 4 && !(float_sub && i == 2) {
                OGRFieldType::OFTInteger64
            } else {
                OGRFieldType::OFTReal
            };
            FieldDefn::new(name, ty)?.add_to_layer(&layer)?;
        }
        // Deliberately reversed feature order and an ID above nine.
        for id in [12, 2] {
            let mut feature = Feature::new(layer.defn())?;
            for (i, value) in [id, -1, 1, 1].into_iter().enumerate() {
                if float_sub && i == 2 {
                    feature.set_field_double(i, value as f64)?;
                } else {
                    feature.set_field_integer64(i, value)?;
                }
            }
            for (i, value) in [
                if segments { reach_length } else { 4. },
                10.,
                50. + offset,
                50.,
            ]
            .into_iter()
            .enumerate()
            {
                feature.set_field_double(i + 4, value)?;
            }
            let t = self.affine;
            let x0 = t[0] + if id == 12 { 2. * t[1] } else { 0. };
            let x1 = x0 + 2. * t[1];
            let y0 = t[3];
            let y1 = y0 + 2. * t[5];
            let wkt = if segments {
                format!("LINESTRING ({x0} {y0}, {x1} {y1})")
            } else {
                format!("POLYGON (({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))")
            };
            feature.set_geometry(Geometry::from_wkt(&wkt)?)?;
            feature.create(&layer)?;
        }
        ds.flush_cache()?;
        Ok(())
    }
    fn rows(&self) -> Result<Vec<std::collections::BTreeMap<String, String>>> {
        let mut reader = csv::Reader::from_path(self.spec.output_dir.join("sampled_minis.csv"))?;
        let headers = reader.headers()?.clone();
        reader
            .records()
            .map(|record| {
                Ok(headers
                    .iter()
                    .map(str::to_owned)
                    .zip(record?.iter().map(str::to_owned))
                    .collect())
            })
            .collect()
    }
    fn failure(&self, text: &str) {
        let error = sample_minibasins(&self.spec).unwrap_err();
        assert!(format!("{error:#}").contains(text), "{error:#}");
        assert!(!self.spec.output_dir.join("sampled_minis.csv").exists());
        assert!(
            !self
                .spec
                .output_dir
                .join("manifest-sample-minis.json")
                .exists()
        );
    }
}

#[test]
fn sampling_values_order_and_worker_determinism() -> Result<()> {
    let mut fixture = Fixture::new(4326)?;
    fixture.spec.workers = 1;
    let serial = sample_minibasins(&fixture.spec)?;
    assert_eq!(serial.workers_used, 1);
    let rows = fixture.rows()?;
    assert_eq!(
        rows.iter().map(|r| r["id"].as_str()).collect::<Vec<_>>(),
        ["2", "12"]
    );
    let first = &rows[0];
    assert_eq!(first["sub"], "1");
    assert_eq!(first["unit_length"], "4.0");
    assert_eq!(first["reach_elevation"], "5.0");
    assert_eq!(first["reach_slope"], "5.0");
    assert_eq!(first["tributary_length"], "1.0");
    assert_eq!(first["hru_1"], "25.0");
    assert_eq!(first["hru_2"], "75.0");
    assert_eq!(first["hru_3"], "0.0");
    assert!(first["flooded_area_100"].parse::<f64>()? < 50.);
    let bytes = fs::read(&serial.sampled_minis)?;
    fixture.spec.output_dir = fixture.spec.output_dir.with_file_name("parallel");
    fixture.spec.workers = 2;
    let parallel = sample_minibasins(&fixture.spec)?;
    assert_eq!(parallel.workers_used, 2);
    assert_eq!(bytes, fs::read(&parallel.sampled_minis)?);
    let manifest: Value = serde_json::from_reader(fs::File::open(parallel.manifest)?)?;
    assert_eq!(manifest["step"], "sample-minis");
    assert_eq!(manifest["runtime"]["workers_used"], parallel.workers_used);
    assert!(
        manifest["runtime"]["elapsed_time"]["total"]
            .as_f64()
            .unwrap()
            .is_finite()
    );
    assert!(
        !manifest["parameters"]
            .as_object()
            .unwrap()
            .contains_key("batch_size")
    );
    assert!(Path::new(manifest["inputs"]["dem"]["path"].as_str().unwrap()).is_absolute());
    assert!(manifest["parameters"].get("dem").is_none());
    assert!(manifest["parameters"].get("overwrite").is_none());
    assert_eq!(
        manifest["outputs"]["sampled_minis"]["sha256"]
            .as_str()
            .unwrap()
            .len(),
        64
    );
    use sha2::Digest;
    let expected_hash = format!(
        "{:x}",
        sha2::Sha256::digest(fs::read(&parallel.sampled_minis)?)
    );
    assert_eq!(
        manifest["outputs"]["sampled_minis"]["sha256"],
        expected_hash
    );
    assert_eq!(manifest["runtime"]["ran_at"].as_str().unwrap().len(), 16);
    assert_eq!(manifest["runtime"]["timezone"].as_str().unwrap().len(), 6);
    for field in [
        "peak_ram_usage_mib",
        "peak_cpu_usage_percent",
        "cpu_time_seconds",
    ] {
        if let Some(value) = manifest["runtime"][field].as_f64() {
            assert!(value.is_finite() && value >= 0.);
        }
    }
    Ok(())
}

#[test]
fn nodata_reports_valid_denominators_and_retained_failure_reports() -> Result<()> {
    let fixture = Fixture::new(4326)?;
    let mut hand = HAND;
    hand[4] = f32::NAN;
    fixture.raster(&fixture.spec.hand, &hand, &[255; 8], true, "m")?;
    let mut mask = [255; 8];
    mask[1] = 0;
    fixture.raster(&fixture.spec.hru, &HRU, &mask, false, "")?;
    let report = sample_minibasins(&fixture.spec)?;
    assert_eq!(report.nodata_reports.len(), 2);
    let rows = fixture.rows()?;
    assert!((rows[0]["hru_1"].parse::<f64>()? - 100. / 3.).abs() < 1e-12);
    let diagnostic = fs::read_to_string(fixture.spec.output_dir.join("nodata_hand.csv"))?;
    assert_eq!(
        diagnostic,
        "mini_id,nodata_cells,total_cells,percentage_nodata\n2,1,4,25.0\n"
    );
    for name in ["dem", "hand", "ltnd", "hru"] {
        let fixture = Fixture::new(4326)?;
        let mask = [0, 0, 255, 255, 0, 0, 255, 255];
        let (path, data) = match name {
            "dem" => (&fixture.spec.dem, &DEM),
            "hand" => (&fixture.spec.hand, &HAND),
            _ => (&fixture.spec.ltnd, &LTND),
        };
        if name == "hru" {
            fixture.raster(&fixture.spec.hru, &HRU, &mask, false, "")?;
        } else {
            fixture.raster(path, data, &mask, true, "m")?;
        }
        fixture.failure("Mini 2 has no valid data");
        assert!(
            fixture
                .spec
                .output_dir
                .join(format!("nodata_{name}.csv"))
                .exists()
        );
        assert!(!fixture.spec.output_dir.join("nodata_dem.csv").exists() || name == "dem");
    }
    Ok(())
}

#[test]
fn rejects_wrong_schema_attributes_units_classes_and_segment_ownership() -> Result<()> {
    let fixture = Fixture::new(4326)?;
    fixture.vectors(false, 2., 0., true)?;
    fixture.failure("schema");
    let fixture = Fixture::new(4326)?;
    fixture.vectors(true, 2., 1., false)?;
    fixture.failure("attributes differ");
    let fixture = Fixture::new(4326)?;
    fixture.vectors(true, 0., 0., false)?;
    fixture.failure("reach length");
    let fixture = Fixture::new(4326)?;
    fixture.raster(&fixture.spec.dem, &DEM, &[255; 8], true, "km")?;
    fixture.failure("metre units");
    let fixture = Fixture::new(4326)?;
    let mut hru = HRU;
    hru[0] = 101;
    fixture.raster(&fixture.spec.hru, &hru, &[255; 8], false, "")?;
    fixture.failure("HRU class");
    let fixture = Fixture::new(4326)?;
    let mut dem = DEM;
    dem[0] = f32::INFINITY;
    fixture.raster(&fixture.spec.dem, &dem, &[255; 8], true, "m")?;
    fixture.failure("infinite");
    let fixture = Fixture::new(4326)?;
    let mut segments = SEGMENTS;
    segments[0] = 12;
    fixture.raster(&fixture.spec.grid_segments, &segments, &[255; 8], false, "")?;
    fixture.failure("ownership mismatch");
    Ok(())
}

#[test]
fn missing_segment_coverage_and_nonpositive_ltnd() -> Result<()> {
    let fixture = Fixture::new(4326)?;
    let mut mask = [255; 8];
    mask[0] = 0;
    fixture.raster(&fixture.spec.grid_segments, &SEGMENTS, &mask, false, "")?;
    sample_minibasins(&fixture.spec)?;
    assert_eq!(fixture.rows()?[0]["reach_elevation"], "10.0");
    assert!(
        fixture
            .spec
            .output_dir
            .join("nodata_grid_segments.csv")
            .exists()
    );
    let fixture = Fixture::new(4326)?;
    fixture.raster(&fixture.spec.ltnd, &[0_f32; 8], &[255; 8], true, "m")?;
    fixture.failure("non-positive maximum LTND");
    Ok(())
}

#[test]
fn output_protection_and_memory_admission() -> Result<()> {
    let mut fixture = Fixture::new(4326)?;
    fs::create_dir(&fixture.spec.output_dir)?;
    let product = fixture.spec.output_dir.join("sampled_minis.csv");
    fs::write(&product, "previous result\n")?;
    assert!(sample_minibasins(&fixture.spec).is_err());
    assert_eq!(fs::read_to_string(&product)?, "previous result\n");
    fixture.spec.output_dir = fixture.spec.output_dir.with_file_name("low-memory");
    fixture.spec.memory_limit_mb = 1;
    fixture.failure("48 MiB");
    assert!(!fixture.spec.output_dir.exists());
    fixture.spec.memory_limit_mb = 48;
    fixture.failure("sampling windows");
    fixture.spec.memory_limit_mb = 64;
    fixture.spec.workers = 4;
    let result = sample_minibasins(&fixture.spec)?;
    assert_eq!(result.workers_used, 1);
    fixture.spec.output_dir = fixture.spec.dem.parent().unwrap().to_owned();
    fs::write(fixture.spec.output_dir.join("unrelated.txt"), "keep")?;
    sample_minibasins(&fixture.spec)?;
    assert!(fixture.spec.dem.exists());
    assert_eq!(
        fs::read_to_string(fixture.spec.output_dir.join("unrelated.txt"))?,
        "keep"
    );
    Ok(())
}

#[test]
fn cli_runs_and_reports_errors() -> Result<()> {
    let fixture = Fixture::new(4326)?;
    let mut command = Command::new(env!("CARGO_BIN_EXE_mgb"));
    command.args(["prepro", "sample-minis"]);
    for (name, path) in [
        ("mini-catchments", &fixture.spec.mini_catchments),
        ("mini-segments", &fixture.spec.mini_segments),
        ("dem", &fixture.spec.dem),
        ("grid-catchments", &fixture.spec.grid_catchments),
        ("grid-segments", &fixture.spec.grid_segments),
        ("hand", &fixture.spec.hand),
        ("ltnd", &fixture.spec.ltnd),
        ("hru", &fixture.spec.hru),
        ("output-dir", &fixture.spec.output_dir),
    ] {
        command.arg(format!("--{name}")).arg(path);
    }
    let output = command.output()?;
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("Sampling complete."));
    assert!(stdout.contains("Outputs:"));
    assert!(stdout.contains("sampled_minis.csv"));
    assert!(stdout.contains("manifest-sample-minis.json"));
    let output = command.output()?;
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("already exists"));
    Ok(())
}

#[test]
fn flooded_areas_match_independent_proj_geodesic_reference() -> Result<()> {
    // Native PROJ geod_polygonarea on all four corners; projected corners use
    // the independent inverse Web Mercator equations.
    for (epsg, first_cell, flood100) in [
        (4326, 2.400249953801155, 7.2005610551521775),
        (3857, 0.00501691495025158, 0.015051016418457032),
        (4267, 2.4001407728488444, 7.200233575557231),
    ] {
        let fixture = Fixture::new(epsg)?;
        sample_minibasins(&fixture.spec)?;
        let rows = fixture.rows()?;
        for (column, expected) in [
            ("flooded_area_1", first_cell),
            ("flooded_area_100", flood100),
        ] {
            let actual = rows[0][column].parse::<f64>()?;
            assert!(
                (actual - expected).abs() <= 1e-11 + 1e-10 * expected,
                "EPSG:{epsg} {column}: {actual} != {expected}"
            );
        }
    }
    Ok(())
}

#[test]
fn excludes_outside_ownership_and_rejects_bad_grid_metadata() -> Result<()> {
    let mut fixture = Fixture::new(4326)?;
    // Mask one corner, keeping each mini's indexed rectangle tight.
    let mut mask = [255; 8];
    mask[0] = 0;
    fixture.raster(&fixture.spec.grid_catchments, &OWNERS, &mask, false, "")?;
    fixture.raster(&fixture.spec.grid_segments, &SEGMENTS, &mask, false, "")?;
    let mut dem = DEM;
    dem[0] = f32::INFINITY;
    fixture.raster(&fixture.spec.dem, &dem, &mask, true, "m")?;
    let mut hand = HAND;
    hand[0] = f32::INFINITY;
    fixture.raster(&fixture.spec.hand, &hand, &mask, true, "m")?;
    let mut hru = HRU;
    hru[0] = 101;
    fixture.raster(&fixture.spec.hru, &hru, &mask, false, "")?;
    sample_minibasins(&fixture.spec)?;
    assert_eq!(fixture.rows()?[0]["reach_elevation"], "10.0");
    assert!(!fixture.spec.output_dir.join("nodata_dem.csv").exists());
    fixture.spec.output_dir = fixture.spec.output_dir.with_file_name("mismatch");
    fixture.affine[0] += 0.01;
    fixture.raster(&fixture.spec.hru, &HRU, &[255; 8], false, "")?;
    fixture.failure("grid/CRS mismatch");
    let fixture = Fixture::new(4326)?;
    fixture.raster(
        &fixture.spec.grid_catchments,
        &[12; 8],
        &[255; 8],
        false,
        "",
    )?;
    fixture.failure("ownership mismatch");
    let fixture = Fixture::new(4326)?;
    fixture.raster(&fixture.spec.hru, &[1_f32; 8], &[255; 8], false, "")?;
    fixture.failure("dtype");
    Ok(())
}
