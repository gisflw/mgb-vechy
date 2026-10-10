use super::{Network, Stage};
use anyhow::{Context, Result, ensure};
use gdal::{
    Dataset, Metadata,
    vector::{FieldValue, LayerAccess},
};
use geos::{Geom, Geometry};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, File},
    io::Read,
    path::Path,
};

pub fn close(actual: f64, expected: f64, rtol: f64, atol: f64) -> bool {
    actual == expected
        || (actual.is_nan() && expected.is_nan())
        || (actual.is_finite()
            && expected.is_finite()
            && (actual - expected).abs() <= atol + rtol * expected.abs())
}

fn product_names(directory: &Path) -> Result<BTreeSet<String>> {
    Ok(fs::read_dir(directory)?
        .map(|entry| entry.map(|e| e.path()))
        .collect::<std::io::Result<Vec<_>>>()?
        .into_iter()
        .filter(|path| {
            matches!(
                path.extension().and_then(|s| s.to_str()),
                Some("fgb" | "tif" | "csv")
            )
        })
        .map(|path| path.file_name().unwrap().to_string_lossy().into_owned())
        .collect())
}

fn output_records(value: &Value, records: &mut Vec<(String, String)>) -> Result<()> {
    let object = value
        .as_object()
        .context("Manifest outputs must be an object")?;
    if object.contains_key("path") || object.contains_key("sha256") {
        let path = object["path"]
            .as_str()
            .context("Manifest output path missing")?;
        let checksum = object["sha256"]
            .as_str()
            .context("Manifest output checksum missing")?;
        ensure!(
            checksum.len() == 64 && checksum.bytes().all(|byte| byte.is_ascii_hexdigit()),
            "Invalid manifest output checksum"
        );
        records.push((path.to_owned(), checksum.to_owned()));
    } else {
        ensure!(!object.is_empty(), "Manifest outputs must include files");
        for value in object.values() {
            output_records(value, records)?;
        }
    }
    Ok(())
}

fn checksum(path: &Path) -> Result<String> {
    let mut file = File::open(path)?;
    let mut hash = Sha256::new();
    let mut buffer = vec![0; 1024 * 1024];
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        hash.update(&buffer[..count]);
    }
    Ok(format!("{:x}", hash.finalize()))
}

fn check_manifest(manifest: &Value, stage: Stage, expected: &Path, output: &Path) -> Result<()> {
    ensure!(
        manifest["step"] == stage.name()
            && manifest["inputs"].is_object()
            && manifest["outputs"].is_object()
            && manifest["parameters"].is_object(),
        "Invalid audit manifest envelope for {}",
        stage.name()
    );
    let elapsed = manifest["runtime"]["elapsed_time"]["total"]
        .as_f64()
        .context("Manifest runtime is missing total elapsed time")?;
    ensure!(
        elapsed.is_finite() && elapsed >= 0.,
        "Invalid manifest runtime for {}",
        stage.name()
    );

    let mut expected_outputs: BTreeSet<_> =
        stage.products().into_iter().map(str::to_owned).collect();
    if stage == Stage::SampleMinis {
        expected_outputs.extend(
            product_names(expected)?
                .into_iter()
                .filter(|name| name.starts_with("nodata_")),
        );
    }
    let output_dir = fs::canonicalize(output)?;
    let mut records = Vec::new();
    output_records(&manifest["outputs"], &mut records)?;
    let mut found = BTreeSet::new();
    for (recorded, digest) in records {
        let recorded = Path::new(&recorded);
        let name = recorded
            .file_name()
            .and_then(|name| name.to_str())
            .context("Manifest output has an invalid filename")?;
        ensure!(
            expected_outputs.contains(name) && found.insert(name.to_owned()),
            "Unexpected or duplicate manifest output: {name}"
        );
        let product = fs::canonicalize(output_dir.join(name))?;
        ensure!(
            fs::canonicalize(recorded)? == product,
            "Manifest output path does not match {name}"
        );
        ensure!(
            checksum(&product)? == digest,
            "Manifest checksum mismatch: {name}"
        );
    }
    ensure!(
        found == expected_outputs,
        "Manifest output set mismatch; missing {:?}",
        expected_outputs.difference(&found).collect::<Vec<_>>()
    );
    Ok(())
}

pub fn compare(root: &Path, network: Network, stage: Stage, output: &Path) -> Result<()> {
    compare_products(
        &root.join("expected").join(network.name()),
        output,
        stage,
        false,
    )?;
    if matches!(stage, Stage::All | Stage::DefineRoi) {
        check_outlets(root, network, output)?;
    }
    Ok(())
}

pub fn compare_products(
    expected: &Path,
    output: &Path,
    stage: Stage,
    legacy_sub: bool,
) -> Result<()> {
    let mut required: BTreeSet<String> = stage.products().into_iter().map(str::to_owned).collect();
    if matches!(stage, Stage::All | Stage::SampleMinis) {
        required.extend(
            product_names(expected)?
                .into_iter()
                .filter(|name| name.starts_with("nodata_")),
        );
    }
    let actual = product_names(output)
        .with_context(|| format!("Candidate output unavailable: {}", output.display()))?;
    ensure!(
        actual == required,
        "Product set mismatch; missing {:?}, unexpected {:?}",
        required.difference(&actual).collect::<Vec<_>>(),
        actual.difference(&required).collect::<Vec<_>>()
    );
    for name in &required {
        let actual = output.join(name);
        let reference = expected.join(name);
        ensure!(
            reference.is_file(),
            "Reference product missing: {}",
            reference.display()
        );
        let result = match actual.extension().and_then(|s| s.to_str()) {
            Some("fgb") => {
                if legacy_sub {
                    compare_vector_legacy_sub(&actual, &reference)
                } else {
                    compare_vector(&actual, &reference)
                }
            }
            Some("tif") => compare_raster(&actual, &reference),
            _ => {
                if legacy_sub {
                    compare_csv_legacy_sub(&actual, &reference, name == "sampled_minis.csv")
                } else {
                    compare_csv(&actual, &reference, name == "sampled_minis.csv")
                }
            }
        };
        result.with_context(|| name.to_string())?;
        println!("Compared {name}");
    }
    for step in stage.stages() {
        let path = output.join(format!("manifest-{}.json", step.name()));
        let manifest: Value = serde_json::from_reader(
            File::open(&path)
                .with_context(|| format!("Audit manifest missing: {}", path.display()))?,
        )?;
        check_manifest(&manifest, step, expected, output)
            .with_context(|| path.display().to_string())?;
    }
    Ok(())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum CsvType {
    Empty,
    Integer,
    Float,
    Boolean,
    Text,
}

fn csv_type<'a>(values: impl Iterator<Item = &'a str>) -> CsvType {
    let mut kind = CsvType::Empty;
    for text in values.filter(|value| !value.is_empty()) {
        let next = if text.parse::<i128>().is_ok() {
            CsvType::Integer
        } else if text.parse::<f64>().is_ok() {
            CsvType::Float
        } else if matches!(text, "True" | "False" | "true" | "false") {
            CsvType::Boolean
        } else {
            CsvType::Text
        };
        kind = match (kind, next) {
            (CsvType::Empty, next) => next,
            (a, b) if a == b => a,
            (CsvType::Integer | CsvType::Float, CsvType::Integer | CsvType::Float) => {
                CsvType::Float
            }
            _ => CsvType::Text,
        };
    }
    kind
}

pub fn compare_csv(actual: &Path, reference: &Path, sort_id: bool) -> Result<()> {
    compare_csv_impl(actual, reference, sort_id, false)
}

pub fn compare_csv_legacy_sub(actual: &Path, reference: &Path, sort_id: bool) -> Result<()> {
    compare_csv_impl(actual, reference, sort_id, true)
}

fn compare_csv_impl(
    actual: &Path,
    reference: &Path,
    sort_id: bool,
    legacy_sub: bool,
) -> Result<()> {
    let read = |path: &Path| -> Result<(csv::StringRecord, Vec<csv::StringRecord>)> {
        let mut reader = csv::Reader::from_path(path)?;
        Ok((
            reader.headers()?.clone(),
            reader.records().collect::<std::result::Result<_, _>>()?,
        ))
    };
    let (headers, mut left) = read(actual)?;
    let (expected_headers, mut right) = read(reference)?;
    ensure!(headers == expected_headers, "CSV column names/order differ");
    ensure!(left.len() == right.len(), "CSV row counts differ");
    if sort_id {
        let index = headers
            .iter()
            .position(|name| name == "id")
            .context("Sampling CSV lacks id")?;
        let sort = |rows: &mut Vec<csv::StringRecord>| -> Result<()> {
            let ids: Vec<_> = rows.iter().map(|row| row[index].to_owned()).collect();
            ensure!(
                ids.iter().all(|id| !id.is_empty())
                    && ids.iter().collect::<BTreeSet<_>>().len() == rows.len(),
                "Sampling IDs must be present and unique"
            );
            rows.sort_by(|a, b| a[index].cmp(&b[index]));
            Ok(())
        };
        sort(&mut left)?;
        sort(&mut right)?;
    }
    for column in 0..headers.len() {
        let mut kind = csv_type(right.iter().map(|row| &row[column]));
        if legacy_sub && &headers[column] == "sub" && kind == CsvType::Float {
            for row in &mut right {
                let value: f64 = row[column].parse()?;
                ensure!(
                    value.is_finite()
                        && value.fract() == 0.
                        && value >= i64::MIN as f64
                        && value < -(i64::MIN as f64),
                    "Non-integral legacy sub"
                );
                let fields: Vec<String> = row
                    .iter()
                    .enumerate()
                    .map(|(i, field)| {
                        if i == column {
                            (value as i64).to_string()
                        } else {
                            field.to_owned()
                        }
                    })
                    .collect();
                *row = fields.into();
            }
            kind = CsvType::Integer;
        }
        ensure!(
            csv_type(left.iter().map(|row| &row[column])) == kind,
            "CSV type differs for {}",
            &headers[column]
        );
        for (row, (a, b)) in left.iter().zip(&right).enumerate() {
            let (a, b) = (&a[column], &b[column]);
            let same = if a.is_empty() || b.is_empty() {
                a == b
            } else {
                match kind {
                    CsvType::Float => close(a.parse()?, b.parse()?, 1e-10, 1e-10),
                    CsvType::Integer => a.parse::<i128>()? == b.parse::<i128>()?,
                    CsvType::Boolean => a.eq_ignore_ascii_case(b),
                    _ => a == b,
                }
            };
            ensure!(
                same,
                "CSV row {row}, column {} differs: {a:?} != {b:?}",
                &headers[column]
            );
        }
    }
    Ok(())
}

struct VectorRow {
    id: String,
    fields: Vec<Option<FieldValue>>,
    geometry: Option<Vec<u8>>,
}

fn vector_rows(layer: &mut gdal::vector::Layer<'_>) -> Result<Vec<VectorRow>> {
    let id = layer.defn().field_index("id")?;
    layer
        .features()
        .map(|feature| {
            let value = feature.field(id)?.context("Vector ID is null")?;
            let id = match value {
                FieldValue::IntegerValue(v) => v.to_string(),
                FieldValue::Integer64Value(v) => v.to_string(),
                FieldValue::StringValue(v) => v,
                _ => anyhow::bail!("Unexpected vector ID type"),
            };
            Ok(VectorRow {
                id,
                fields: (0..feature.field_count())
                    .map(|i| feature.field(i))
                    .collect::<gdal::errors::Result<_>>()?,
                geometry: feature.geometry().map(|g| g.wkb()).transpose()?,
            })
        })
        .collect()
}

fn field_equal(a: &Option<FieldValue>, b: &Option<FieldValue>) -> bool {
    match (a, b) {
        (Some(FieldValue::RealValue(a)), Some(FieldValue::RealValue(b))) => {
            close(*a, *b, 1e-10, 1e-10)
        }
        (Some(FieldValue::RealListValue(a)), Some(FieldValue::RealListValue(b))) => {
            a.len() == b.len() && a.iter().zip(b).all(|(a, b)| close(*a, *b, 1e-10, 1e-10))
        }
        _ => a == b,
    }
}

pub fn geometry_equal(a: &[u8], b: &[u8]) -> Result<bool> {
    Ok(Geometry::new_from_wkb(a)?.equals(&Geometry::new_from_wkb(b)?)?)
}

pub fn compare_vector(actual: &Path, reference: &Path) -> Result<()> {
    compare_vector_impl(actual, reference, false)
}

pub fn compare_vector_legacy_sub(actual: &Path, reference: &Path) -> Result<()> {
    compare_vector_impl(actual, reference, true)
}

fn compare_vector_impl(actual: &Path, reference: &Path, legacy_sub: bool) -> Result<()> {
    let a = Dataset::open(actual)?;
    let b = Dataset::open(reference)?;
    ensure!(
        a.layer_count() == b.layer_count() && a.layer_count() == 1,
        "Vector layer counts differ"
    );
    let mut a = a.layer(0)?;
    let mut b = b.layer(0)?;
    ensure!(
        a.spatial_ref() == b.spatial_ref() && a.spatial_ref().is_some(),
        "Vector CRS differs or is missing"
    );
    let schema = |layer: &gdal::vector::Layer<'_>| {
        layer
            .defn()
            .fields()
            .enumerate()
            .map(|(index, f)| {
                // SAFETY: the index comes from this live layer's field iterator;
                // both borrowed definitions remain valid during these calls.
                let subtype = unsafe {
                    let field = gdal_sys::OGR_FD_GetFieldDefn(layer.defn().c_defn(), index as i32);
                    gdal_sys::OGR_Fld_GetSubType(field)
                };
                (
                    f.name(),
                    f.field_type(),
                    subtype,
                    f.width(),
                    f.precision(),
                    f.is_nullable(),
                )
            })
            .collect::<Vec<_>>()
    };
    let actual_schema = schema(&a);
    let mut reference_schema = schema(&b);
    let sub = reference_schema.iter().position(|field| field.0 == "sub");
    let adapt_sub = legacy_sub
        && sub.is_some_and(|i| reference_schema[i].1 == gdal::vector::OGRFieldType::OFTReal);
    if adapt_sub {
        let i = sub.unwrap();
        ensure!(
            actual_schema
                .get(i)
                .is_some_and(|field| field.1 == gdal::vector::OGRFieldType::OFTInteger64),
            "Candidate sub must be int64"
        );
        reference_schema[i].1 = gdal::vector::OGRFieldType::OFTInteger64;
    }
    ensure!(
        actual_schema == reference_schema,
        "Vector field names/types/order differ: {:?} vs {:?}",
        schema(&a),
        schema(&b)
    );
    let geometry_schema = |layer: &gdal::vector::Layer<'_>| {
        layer
            .defn()
            .geom_fields()
            .map(|f| (f.name(), f.field_type()))
            .collect::<Vec<_>>()
    };
    ensure!(
        geometry_schema(&a) == geometry_schema(&b),
        "Vector geometry schemas differ: {:?} vs {:?}",
        geometry_schema(&a),
        geometry_schema(&b)
    );
    let mut a = vector_rows(&mut a)?;
    let mut b = vector_rows(&mut b)?;
    if adapt_sub {
        let i = sub.unwrap();
        for row in &mut b {
            let Some(FieldValue::RealValue(value)) = row.fields[i] else {
                anyhow::bail!("Null legacy sub");
            };
            ensure!(
                value.is_finite()
                    && value.fract() == 0.
                    && value >= i64::MIN as f64
                    && value < -(i64::MIN as f64),
                "Non-integral legacy sub"
            );
            row.fields[i] = Some(FieldValue::Integer64Value(value as i64));
        }
    }
    ensure!(a.len() == b.len(), "Vector row counts differ");
    for rows in [&a, &b] {
        ensure!(
            rows.iter().map(|r| &r.id).collect::<BTreeSet<_>>().len() == rows.len(),
            "Vector IDs are duplicated"
        );
    }
    if actual
        .file_name()
        .unwrap()
        .to_string_lossy()
        .starts_with("roi_")
    {
        a.sort_by(|a, b| a.id.cmp(&b.id));
        b.sort_by(|a, b| a.id.cmp(&b.id));
    }
    for (row, (a, b)) in a.iter().zip(&b).enumerate() {
        ensure!(a.id == b.id, "Vector ID/order mismatch at row {row}");
        ensure!(
            a.fields
                .iter()
                .zip(&b.fields)
                .all(|(a, b)| field_equal(a, b)),
            "Vector attributes differ at ID {}",
            a.id
        );
        let equal = match (&a.geometry, &b.geometry) {
            (Some(a), Some(b)) => geometry_equal(a, b)?,
            (None, None) => true,
            _ => false,
        };
        ensure!(equal, "Vector geometry differs at ID {}", a.id);
    }
    Ok(())
}

fn check_outlets(root: &Path, network: Network, output: &Path) -> Result<()> {
    let manifest: Value = serde_json::from_reader(File::open(
        root.join("expected")
            .join(network.name())
            .join("manifest-define-roi.json"),
    )?)?;
    let outlets = manifest["parameters"]["outlet_ids"]
        .as_array()
        .context("Outlet list missing")?;
    ensure!(
        outlets.len() == 3,
        "Jacui reference requires three ordered outlets"
    );
    let dataset = Dataset::open(output.join("roi_segments.fgb"))?;
    let mut layer = dataset.layer(0)?;
    let id = layer.defn().field_index("id")?;
    let sub = layer.defn().field_index("sub")?;
    let mut subs = BTreeMap::new();
    for feature in layer.features() {
        subs.insert(
            feature
                .field_as_integer64(id)?
                .context("Outlet ID missing")?
                .to_string(),
            feature.field_as_integer(sub)?,
        );
    }
    for (index, outlet) in outlets.iter().enumerate() {
        let outlet = outlet.as_str().context("Outlet ID is not a string")?;
        ensure!(
            subs.get(outlet) == Some(&Some(3 - index as i32)),
            "Outlet {outlet} has incorrect sub precedence"
        );
    }
    Ok(())
}

fn tags(object: &impl Metadata) -> BTreeMap<String, String> {
    object
        .metadata_domain("")
        .unwrap_or_default()
        .into_iter()
        .filter_map(|item| {
            item.split_once('=')
                .map(|(key, value)| (key.to_owned(), value.to_owned()))
        })
        .collect()
}

fn compare_tags(mut a: BTreeMap<String, String>, mut b: BTreeMap<String, String>) -> Result<()> {
    match (a.remove("mini_index"), b.remove("mini_index")) {
        (Some(a), Some(b)) => {
            let a: Vec<Vec<f64>> = serde_json::from_str(&a)?;
            let b: Vec<Vec<f64>> = serde_json::from_str(&b)?;
            ensure!(a.len() == b.len(), "Mini-index lengths differ");
            for (a, b) in a.iter().zip(&b) {
                ensure!(
                    a.len() == 5
                        && b.len() == 5
                        && a[0] == b[0]
                        && a[1..]
                            .iter()
                            .zip(&b[1..])
                            .all(|(a, b)| close(*a, *b, 0., 1e-12)),
                    "Mini-index IDs/order/bounds differ"
                );
            }
        }
        (None, None) => {}
        _ => anyhow::bail!("Mini-index metadata missing on one raster"),
    }
    ensure!(a == b, "Raster metadata differs");
    Ok(())
}

pub fn compare_raster(actual: &Path, reference: &Path) -> Result<()> {
    let a = Dataset::open(actual)?;
    let b = Dataset::open(reference)?;
    ensure!(a.spatial_ref()? == b.spatial_ref()?, "Raster CRS differs");
    ensure!(
        a.raster_size() == b.raster_size() && a.raster_count() == b.raster_count(),
        "Raster dimensions/band counts differ"
    );
    ensure!(
        a.geo_transform()?
            .iter()
            .zip(b.geo_transform()?)
            .all(|(a, b)| close(*a, b, 0., 1e-12)),
        "Raster affine transforms differ"
    );
    compare_tags(tags(&a), tags(&b))?;
    ensure!(
        a.metadata_item("LAYOUT", "IMAGE_STRUCTURE").as_deref() == Some("COG")
            && b.metadata_item("LAYOUT", "IMAGE_STRUCTURE").as_deref() == Some("COG"),
        "Raster is not a COG"
    );
    for index in 1..=b.raster_count() {
        let a = a.rasterband(index)?;
        let b = b.rasterband(index)?;
        ensure!(a.band_type() == b.band_type(), "Raster dtype differs");
        ensure!(
            a.no_data_value() == b.no_data_value(),
            "Raster nodata sentinel differs"
        );
        ensure!(a.unit() == b.unit(), "Raster band units differ");
        let af = a.mask_flags()?;
        let bf = b.mask_flags()?;
        ensure!(
            (
                af.is_all_valid(),
                af.is_per_dataset(),
                af.is_alpha(),
                af.is_nodata()
            ) == (
                bf.is_all_valid(),
                bf.is_per_dataset(),
                bf.is_alpha(),
                bf.is_nodata()
            ),
            "Raster mask flags differ"
        );
        compare_tags(tags(&a), tags(&b))?;
        let am = a.open_mask_band()?;
        let bm = b.open_mask_band()?;
        let (width, height) = b.size();
        // Fixed maximum windows keep comparisons bounded even for striped or
        // oversized source blocks; no full-raster arrays are materialized.
        for y in (0..height).step_by(512) {
            for x in (0..width).step_by(512) {
                let window = ((width - x).min(512), (height - y).min(512));
                let offset = (x as isize, y as isize);
                let av = a.read_as::<f64>(offset, window, window, None)?;
                let bv = b.read_as::<f64>(offset, window, window, None)?;
                let am = am.read_as::<u8>(offset, window, window, None)?;
                let bm = bm.read_as::<u8>(offset, window, window, None)?;
                ensure!(
                    am.data() == bm.data(),
                    "Raster masks differ in window {x},{y}"
                );
                // Contract integer products are int32/uint8, exactly represented
                // in f64; continuous products retain float32 tolerances.
                let integer = b.band_type().is_integer();
                for (cell, ((av, bv), mask)) in
                    av.data().iter().zip(bv.data()).zip(bm.data()).enumerate()
                {
                    if *mask != 0 {
                        ensure!(
                            av.is_finite()
                                && bv.is_finite()
                                && if integer {
                                    av == bv
                                } else {
                                    close(*av, *bv, 1e-6, 1e-6)
                                },
                            "Raster values differ in window {x},{y}, cell {cell}"
                        );
                    }
                }
            }
        }
    }
    Ok(())
}
