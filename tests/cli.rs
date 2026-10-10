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
fn batch_size_is_only_exposed_by_roi_and_aggregation() {
    let help = |stage: &str| {
        let output = Command::new(env!("CARGO_BIN_EXE_mgb"))
            .args(["prepro", stage, "--help"])
            .output()
            .unwrap();
        assert!(output.status.success(), "{stage}: {output:?}");
        String::from_utf8(output.stdout).unwrap()
    };

    for stage in ["define-roi", "aggregate"] {
        assert!(help(stage).contains("--batch-size"));
    }
    assert!(!help("sample-minis").contains("--batch-size"));
    assert!(
        !Command::new(env!("CARGO_BIN_EXE_mgb"))
            .args(["prepro", "sample-minis", "--batch-size", "1"])
            .output()
            .unwrap()
            .status
            .success()
    );
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
fn terrain_help_exposes_routing_memory_estimate() {
    let output = Command::new(env!("CARGO_BIN_EXE_mgb"))
        .args(["prepro", "terrain-products", "--help"])
        .output()
        .unwrap();
    assert!(output.status.success());
    let help = String::from_utf8(output.stdout).unwrap();
    assert!(help.contains("--routing-bytes-per-cell"));
    assert!(help.contains("[default: 128]"));
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

fn replacement_command(output: &std::path::Path) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_mgb"));
    command.args([
        "prepro",
        "define-roi",
        "--catchments",
        "missing-catchments.fgb",
        "--segments",
        "missing-segments.fgb",
        "--crs",
        "EPSG:4326",
        "--outlet-id",
        "1",
        "--output-dir",
    ]);
    command.arg(output);
    command
}

#[test]
fn unattended_replacement_requires_explicit_flag() {
    let dir = tempfile::tempdir().unwrap();
    let product = dir.path().join("roi_segments.fgb");
    std::fs::write(&product, b"preserved").unwrap();
    let output = replacement_command(dir.path()).output().unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("use --overwrite"));
    let output = replacement_command(dir.path())
        .arg("--overwrite")
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("Input unavailable"));
    assert_eq!(std::fs::read(product).unwrap(), b"preserved");
}

#[cfg(target_os = "linux")]
#[test]
fn terminal_replacement_defaults_to_no_and_accepts_yes() {
    use std::{
        fs::File,
        io::{Read, Write},
        os::fd::FromRawFd,
        process::Stdio,
        time::{Duration, Instant},
    };
    for (answer, expected) in [
        ("\n", "Replacement declined"),
        ("n\n", "Replacement declined"),
        ("yes\n", "Input unavailable"),
    ] {
        let dir = tempfile::tempdir().unwrap();
        let product = dir.path().join("roi_segments.fgb");
        std::fs::write(&product, b"preserved").unwrap();
        let (mut master, mut slave) = (-1, -1);
        // SAFETY: openpty initializes both descriptors; no optional terminal settings are supplied.
        assert_eq!(
            unsafe {
                libc::openpty(
                    &mut master,
                    &mut slave,
                    std::ptr::null_mut(),
                    std::ptr::null(),
                    std::ptr::null(),
                )
            },
            0
        );
        // SAFETY: these successful openpty descriptors each have one owning File.
        let mut master = unsafe { File::from_raw_fd(master) };
        let slave = unsafe { File::from_raw_fd(slave) };
        let mut child = replacement_command(dir.path())
            .stdin(Stdio::from(slave.try_clone().unwrap()))
            .stderr(Stdio::from(slave))
            .stdout(Stdio::null())
            .spawn()
            .unwrap();
        use std::os::fd::AsRawFd;
        // SAFETY: fcntl changes only the owned master descriptor's blocking mode.
        assert_eq!(
            unsafe { libc::fcntl(master.as_raw_fd(), libc::F_SETFL, libc::O_NONBLOCK) },
            0
        );
        let deadline = Instant::now() + Duration::from_secs(10);
        let mut bytes = Vec::new();
        let mut answered = false;
        loop {
            let mut buffer = [0; 4096];
            if let Ok(count) = master.read(&mut buffer) {
                bytes.extend_from_slice(&buffer[..count]);
            }
            if !answered && String::from_utf8_lossy(&bytes).contains("[y/N]") {
                master.write_all(answer.as_bytes()).unwrap();
                answered = true;
            }
            if child.try_wait().unwrap().is_some() {
                while let Ok(count) = master.read(&mut buffer) {
                    if count == 0 {
                        break;
                    }
                    bytes.extend_from_slice(&buffer[..count]);
                }
                break;
            }
            if Instant::now() >= deadline {
                child.kill().unwrap();
                child.wait().unwrap();
                panic!(
                    "Replacement prompt timed out: {}",
                    String::from_utf8_lossy(&bytes)
                );
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(answered);
        assert!(
            String::from_utf8_lossy(&bytes).contains(expected),
            "{}",
            String::from_utf8_lossy(&bytes)
        );
        assert_eq!(std::fs::read(product).unwrap(), b"preserved");
    }
}
