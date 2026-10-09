use std::process::Command;

#[test]
fn root_and_prepro_help_and_version() {
    for args in [
        vec!["--help"],
        vec!["prepro", "--help"],
        vec!["prepro", "sample-minis", "--help"],
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
fn sampling_requires_explicit_inputs() {
    let output = Command::new(env!("CARGO_BIN_EXE_mgb"))
        .args(["prepro", "sample-minis"])
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("--mini-catchments"));
}
