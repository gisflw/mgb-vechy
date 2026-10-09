use std::process::Command;

#[test]
fn root_and_prepro_help_and_version() {
    for args in [
        vec!["--help"],
        vec!["prepro", "--help"],
        vec!["prepro", "sample-minis", "--help"],
        vec!["prepro", "terrain-products", "--help"],
        vec!["prepro", "define-roi", "--help"],
        vec!["prepro", "aggregate", "--help"],
        vec!["prepro", "prepare", "--help"],
        vec!["--version"],
    ] {
        let output = Command::new(env!("CARGO_BIN_EXE_mgb"))
            .args(&args)
            .output()
            .unwrap();
        assert!(output.status.success(), "{args:?}: {output:?}");
        let text = String::from_utf8(output.stdout).unwrap();
        assert!(text.contains("mgb"));
        if args == ["--version"] {
            assert!(text.contains("0.1.0"));
        }
    }
}

#[test]
fn vector_stages_require_explicit_inputs_and_reject_invalid_flags() {
    for args in [
        vec!["prepro", "define-roi"],
        vec!["prepro", "aggregate"],
        vec!["prepro", "define-roi", "--workers=-1"],
        vec!["prepro", "aggregate", "--lmin=invalid"],
    ] {
        assert!(
            !Command::new(env!("CARGO_BIN_EXE_mgb"))
                .args(args)
                .output()
                .unwrap()
                .status
                .success()
        );
    }
}

#[test]
fn sampling_requires_explicit_inputs() {
    let output = Command::new(env!("CARGO_BIN_EXE_mgb"))
        .args(["prepro", "sample-minis"])
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("--mini-catchments"));
}

#[test]
fn terrain_requires_explicit_inputs_and_rejects_invalid_flags() {
    for args in [
        vec!["prepro", "terrain-products"],
        vec![
            "prepro",
            "terrain-products",
            "--direction-source",
            "invalid",
        ],
        vec!["prepro", "terrain-products", "--agree-buffer=-1"],
    ] {
        let output = Command::new(env!("CARGO_BIN_EXE_mgb"))
            .args(args)
            .output()
            .unwrap();
        assert!(!output.status.success());
    }
}

#[test]
fn preparation_requires_inputs_and_valid_paired_options() {
    for args in [
        vec!["prepro", "prepare"],
        vec!["prepro", "prepare", "--d8", "directions.tif"],
        vec!["prepro", "prepare", "--d8-encoding", "esri"],
        vec!["prepro", "prepare", "--d8-encoding", "invalid"],
        vec!["prepro", "prepare", "--categorical-raster", "hru"],
        vec!["prepro", "prepare", "--workers=-1"],
    ] {
        assert!(
            !Command::new(env!("CARGO_BIN_EXE_mgb"))
                .args(args)
                .output()
                .unwrap()
                .status
                .success()
        );
    }
}
