use crate::Result;
use chrono::{SecondsFormat, Utc};
use fs2::FileExt;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;
use std::thread::sleep;
use std::time::{Duration, Instant};
use uuid::Uuid;

#[derive(Debug)]
pub struct LockContended;

impl std::fmt::Display for LockContended {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("lock contention timeout")
    }
}

impl std::error::Error for LockContended {}

pub fn lock(path: &Path, timeout: Duration) -> Result<File> {
    let deadline = Instant::now() + timeout;
    loop {
        let attempt = (|| -> Result<File> {
            let mut file = OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .open(path)?;
            if let Err(error) = FileExt::try_lock_exclusive(&file) {
                if error.raw_os_error().is_some()
                    && error.raw_os_error() == fs2::lock_contended_error().raw_os_error()
                {
                    return Err(Box::new(LockContended));
                }
                return Err(error.into());
            }
            // Initialize only while holding the lock. On Windows a contender's
            // write to the locked byte would fail even if the file is empty.
            if file.metadata()?.len() == 0 {
                file.write_all(&[0])?;
            }
            Ok(file)
        })();
        match attempt {
            Ok(file) => return Ok(file),
            Err(error) if Instant::now() >= deadline => return Err(error),
            _ => sleep(Duration::from_millis(5)),
        }
    }
}

pub fn linked(path: &Path) -> bool {
    let Ok(metadata) = fs::symlink_metadata(path) else {
        return false;
    };
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        metadata.file_attributes() & 0x400 != 0
    }
    #[cfg(not(windows))]
    {
        metadata.file_type().is_symlink()
    }
}

/// Count and total `*.json` records and pending `*.tmp` files under `dir` (flat, no
/// recursion — the spool has no subdirectories, and the daemon drains it fast).
/// A missing directory is `(0, 0)`. `limits.json` is the daemon's own snapshot,
/// not a record, so it is not counted.
pub fn spool_footprint(dir: &Path) -> Result<(u64, u64)> {
    let entries = match fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok((0, 0)),
        Err(error) => return Err(error.into()),
    };
    let mut files: u64 = 0;
    let mut bytes: u64 = 0;
    for entry in entries {
        let entry = entry?;
        let path = entry.path();
        if !matches!(
            path.extension().and_then(|value| value.to_str()),
            Some("json" | "tmp")
        ) || path.file_name().and_then(|value| value.to_str()) == Some("limits.json")
        {
            continue;
        }
        let metadata = match entry.metadata() {
            Ok(value) => value,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error.into()),
        };
        if metadata.is_file() {
            files = files.saturating_add(1);
            bytes = bytes.saturating_add(metadata.len());
        }
    }
    Ok((files, bytes))
}

pub fn atomic(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path.parent().ok_or("missing parent")?;
    if linked(parent) {
        return Err("linked output directory".into());
    }
    fs::create_dir_all(parent)?;
    let temporary = parent.join(format!("{}.tmp", Uuid::new_v4()));
    let result = (|| -> Result<()> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        drop(file);
        let deadline = Instant::now() + Duration::from_millis(100);
        loop {
            match fs::rename(&temporary, path) {
                Ok(()) => return Ok(()),
                Err(error)
                    if matches!(error.raw_os_error(), Some(5 | 32))
                        && Instant::now() < deadline =>
                {
                    sleep(Duration::from_millis(10))
                }
                Err(error) => return Err(error.into()),
            }
        }
    })();
    let _ = fs::remove_file(temporary);
    result
}

/// Append one content-free line describing an adapter failure to a rotating,
/// size-capped log under the data directory. Best-effort, like `loss`. The
/// caller passes a fixed category string; only the event name comes from input,
/// and it is stripped to `[A-Za-z0-9._-]` and truncated here.
pub fn fault(root: &Path, reason: &str, event: &str) {
    const CAP: u64 = 32 * 1024;
    let _ = (|| -> Result<()> {
        if linked(root) {
            return Err("linked diagnostics directory".into());
        }
        fs::create_dir_all(root)?;
        let guard = lock(&root.join("faults.lock"), Duration::from_millis(100))?;
        let safe: String = event
            .chars()
            .filter(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
            .take(64)
            .collect();
        let line = format!(
            "{} {} {}\n",
            Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true),
            if safe.is_empty() { "unknown" } else { &safe },
            reason,
        );
        let path = root.join("faults.log");
        let existing = fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
        if existing.saturating_add(line.len() as u64) > CAP {
            let _ = fs::rename(&path, root.join("faults.log.1"));
        }
        let mut file = OpenOptions::new().create(true).append(true).open(&path)?;
        file.write_all(line.as_bytes())?;
        drop(guard);
        file.sync_all()?;
        Ok(())
    })();
}

pub fn loss(root: &Path, reason: usize) {
    let _ = (|| -> Result<()> {
        if linked(root) {
            return Err("linked diagnostics directory".into());
        }
        fs::create_dir_all(root)?;
        let guard = lock(&root.join("losses.lock"), Duration::from_millis(100))?;
        let path = root.join("losses.bin");
        let mut bytes = [0_u8; 40];
        if path.exists() {
            let mut file = File::open(&path)?;
            if file.metadata()?.len() != 40 {
                return Err("invalid counters".into());
            }
            file.read_exact(&mut bytes)?;
        }
        let range = reason * 8..(reason + 1) * 8;
        let number = u64::from_le_bytes(bytes[range.clone()].try_into()?).saturating_add(1);
        bytes[range].copy_from_slice(&number.to_le_bytes());
        let mut file = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(false)
            .open(&path)?;
        file.seek(SeekFrom::Start(0))?;
        file.write_all(&bytes)?;
        drop(guard);
        file.sync_all()?;
        Ok(())
    })();
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Scratch(std::path::PathBuf);

    impl Scratch {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!("watchdog-disk-{}", Uuid::new_v4()));
            fs::create_dir_all(&path).unwrap();
            Scratch(path)
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn spool_footprint_counts_records_and_skips_the_limits_snapshot() {
        let dir = Scratch::new();
        fs::write(dir.0.join("a.json"), b"12345").unwrap();
        fs::write(dir.0.join("b.json"), b"123").unwrap();
        fs::write(dir.0.join("limits.json"), b"ignored").unwrap();
        fs::write(dir.0.join("c.log"), b"ignored").unwrap();
        assert_eq!(spool_footprint(&dir.0).unwrap(), (2, 8));
    }

    #[test]
    fn spool_footprint_counts_abandoned_and_empty_temporary_files() {
        let dir = Scratch::new();
        let abandoned = dir.0.join("record.json.crashed.tmp");
        fs::write(&abandoned, b"pending").unwrap();
        fs::write(dir.0.join("empty.tmp"), b"").unwrap();
        fs::create_dir(dir.0.join("directory.tmp")).unwrap();
        assert_eq!(spool_footprint(&dir.0).unwrap(), (2, 7));
        fs::remove_file(abandoned).unwrap();
        assert_eq!(spool_footprint(&dir.0).unwrap(), (1, 0));
    }

    #[test]
    fn spool_footprint_of_a_missing_directory_is_zero() {
        let dir = Scratch::new();
        assert_eq!(spool_footprint(&dir.0.join("absent")).unwrap(), (0, 0));
    }

    #[test]
    fn atomic_replaces_the_file_and_leaves_no_temporary() {
        let dir = Scratch::new();
        let target = dir.0.join("out").join("record.json");
        atomic(&target, b"first").unwrap();
        atomic(&target, b"second").unwrap();
        assert_eq!(fs::read(&target).unwrap(), b"second");
        assert_eq!(fs::read_dir(target.parent().unwrap()).unwrap().count(), 1);
    }

    #[test]
    fn lock_is_exclusive_until_released() {
        let dir = Scratch::new();
        let path = dir.0.join("x.lock");
        let held = lock(&path, Duration::from_millis(200)).unwrap();
        assert!(lock(&path, Duration::from_millis(50)).is_err());
        drop(held);
        assert!(lock(&path, Duration::from_millis(200)).is_ok());
    }
}
