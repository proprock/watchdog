use crate::{Result, config::Limits};
use chrono::{SecondsFormat, Utc};
use fs2::FileExt;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;
use std::thread::sleep;
use std::time::{Duration, Instant};
use uuid::Uuid;

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
            if file.metadata()?.len() == 0 {
                file.write_all(&[0])?;
            }
            FileExt::try_lock_exclusive(&file)?;
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

pub fn usage(root: &Path) -> Result<u64> {
    if linked(root) {
        return Err("linked data directory".into());
    }
    if !root.exists() {
        return Ok(0);
    }
    let mut total: u64 = 0;
    for entry in fs::read_dir(root)? {
        let path = entry?.path();
        if linked(&path) {
            continue;
        }
        let metadata = match path.metadata() {
            Ok(value) => value,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error.into()),
        };
        total = total
            .checked_add(if metadata.is_dir() {
                usage(&path)?
            } else {
                metadata.len()
            })
            .ok_or("size overflow")?;
    }
    Ok(total)
}

pub fn room(root: &Path, limits: &Limits) -> Result<u64> {
    Ok(limits
        .project_bytes
        .saturating_sub(usage(root)?)
        .min(fs2::available_space(root)?)
        .saturating_sub(limits.reserve_bytes))
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
