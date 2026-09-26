mod disk;

use chrono::{SecondsFormat, Utc};
use serde_json::{Value, json};
use std::fs::File;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use uuid::Uuid;

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;

// Loss counter slots, matching agent_watchdog.resources.REASONS.
const QUOTA: usize = 0;
const PAYLOAD: usize = 1;
const IO: usize = 3;

struct Paths {
    config: PathBuf,
    data: PathBuf,
    runtime: PathBuf,
    python: PathBuf,
}

fn arguments() -> Result<(Paths, String)> {
    let mut values = std::collections::HashMap::new();
    let mut positionals: Vec<String> = Vec::new();
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--config" | "--data" | "--runtime" | "--python" | "--installation" => {
                values.insert(arg, args.next().ok_or("missing argument")?);
            }
            other if other.starts_with("--") => return Err("unknown argument".into()),
            _ => positionals.push(arg),
        }
    }
    let provider = match positionals.as_slice() {
        [verb, provider] if verb == "hook" && (provider == "codex" || provider == "claude") => {
            provider.clone()
        }
        _ => return Err("expected: hook <codex|claude>".into()),
    };
    let mut path = |name: &str| -> Result<PathBuf> {
        let path = PathBuf::from(values.remove(name).ok_or("missing path")?);
        if !path.is_absolute() {
            return Err("absolute paths required".into());
        }
        Ok(path)
    };
    Ok((
        Paths {
            config: path("--config")?,
            data: path("--data")?,
            runtime: path("--runtime")?,
            python: path("--python")?,
        },
        provider,
    ))
}

fn paused(paths: &Paths) -> Result<bool> {
    let path = paths.data.join("control.json");
    if !path.exists() {
        return Ok(false);
    }
    let mut bytes = Vec::new();
    File::open(path)?.take(8193).read_to_end(&mut bytes)?;
    if bytes.len() > 8192 {
        return Err("oversized control".into());
    }
    let value: Value = serde_json::from_slice(&bytes)?;
    if value["schema_version"].as_u64() != Some(1) || !value["request_id"].is_string() {
        return Err("invalid control".into());
    }
    value["paused"]
        .as_bool()
        .ok_or_else(|| "invalid control".into())
}

/// The only numbers the adapter needs: the daemon publishes them so the adapter
/// never parses `config.toml`. Absent or unreadable → conservative built-ins.
struct SpoolLimits {
    payload_bytes: u64,
    spool_bytes: u64,
    spool_files: u64,
    pipeline_telemetry: bool,
}

impl SpoolLimits {
    fn read(data: &Path) -> Self {
        let fallback = SpoolLimits {
            payload_bytes: 1024 * 1024,
            spool_bytes: 64 * 1024 * 1024,
            spool_files: 4096,
            pipeline_telemetry: true,
        };
        let read = || -> Option<Value> {
            let mut bytes = Vec::new();
            File::open(data.join("spool").join("limits.json"))
                .ok()?
                .take(8192)
                .read_to_end(&mut bytes)
                .ok()?;
            serde_json::from_slice(&bytes).ok()
        };
        let Some(value) = read() else {
            return fallback;
        };
        if value["schema_version"].as_u64() != Some(1) {
            return fallback;
        }
        let field = |name: &str, default: u64| value[name].as_u64().unwrap_or(default).max(1);
        SpoolLimits {
            payload_bytes: field("payload_bytes", fallback.payload_bytes),
            spool_bytes: field("spool_bytes", fallback.spool_bytes),
            spool_files: field("spool_files", fallback.spool_files),
            pipeline_telemetry: value["pipeline_telemetry"]
                .as_bool()
                .unwrap_or(fallback.pipeline_telemetry),
        }
    }
}

fn utc_now() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Micros, true)
}

/// Discovery record for the daemon's WD-014 policy socket (loopback-only,
/// started only while the daemon runs). Absent or unreadable -> no query is
/// attempted; every `PreToolUse` call proceeds exactly as it does today.
struct PolicySocket {
    port: u16,
    token: String,
}

impl PolicySocket {
    fn read(data: &Path) -> Option<Self> {
        let mut bytes = Vec::new();
        File::open(data.join("policy").join("socket.json"))
            .ok()?
            .take(4096)
            .read_to_end(&mut bytes)
            .ok()?;
        let value: Value = serde_json::from_slice(&bytes).ok()?;
        if value["schema_version"].as_u64() != Some(1) {
            return None;
        }
        let port = u16::try_from(value["port"].as_u64()?).ok()?;
        let token = value["token"].as_str()?;
        if token.is_empty() || token.len() > 128 {
            return None;
        }
        Some(PolicySocket {
            port,
            token: token.to_owned(),
        })
    }
}

const POLICY_TIMEOUT: std::time::Duration = std::time::Duration::from_millis(150);

/// Ask the running daemon, synchronously and with a strict bounded timeout,
/// whether a same-model subagent spawn should be denied. `None` on any
/// failure (no socket file, refused/timed-out connection, malformed
/// response) always means "do not deny" -- a false deny is the one Watchdog
/// failure that would actually stop the harness, so every error path here
/// fails open, never closed.
fn ask_policy(data: &Path, cwd: &str, session_id: &str, candidate_model: &str) -> bool {
    let Some(socket) = PolicySocket::read(data) else {
        return false;
    };
    let address = std::net::SocketAddr::from(([127, 0, 0, 1], socket.port));
    let Ok(mut stream) = std::net::TcpStream::connect_timeout(&address, POLICY_TIMEOUT) else {
        return false;
    };
    if stream.set_write_timeout(Some(POLICY_TIMEOUT)).is_err()
        || stream.set_read_timeout(Some(POLICY_TIMEOUT)).is_err()
    {
        return false;
    }
    let request = json!({
        "schema_version": 1,
        "token": socket.token,
        "cwd": cwd,
        "session_id": session_id,
        "candidate_model": candidate_model,
    });
    let Ok(mut payload) = serde_json::to_vec(&request) else {
        return false;
    };
    payload.push(b'\n');
    use std::io::Write;
    if stream.write_all(&payload).is_err() || stream.shutdown(std::net::Shutdown::Write).is_err() {
        return false;
    }
    let mut response = Vec::new();
    if stream.take(4096).read_to_end(&mut response).is_err() {
        return false;
    }
    let Ok(value) = serde_json::from_slice::<Value>(&response) else {
        return false;
    };
    value["schema_version"].as_u64() == Some(1) && value["decision"].as_str() == Some("deny")
}

fn identifier<'a>(input: &'a Value, key: &str) -> Option<&'a str> {
    input[key]
        .as_str()
        .filter(|s| !s.trim().is_empty() && s.chars().count() <= 256)
}

fn ensure_daemon(paths: &Paths) -> Result<()> {
    let Ok(guard) = disk::lock(&paths.data.join("daemon.lock"), std::time::Duration::ZERO) else {
        return Ok(());
    };
    drop(guard);
    disk::atomic(&paths.runtime.join("status.json"), b"{}")?;
    let mut command = Command::new(&paths.python);
    command
        .args(["-m", "agent_watchdog", "--config"])
        .arg(&paths.config)
        .arg("--data")
        .arg(&paths.data)
        .arg("--runtime")
        .arg(&paths.runtime)
        .args(["daemon", "run"])
        .current_dir(&paths.data)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // CREATE_NO_WINDOW keeps the detached daemon invisible when the hook
        // itself was launched without a console.
        command.creation_flags(0x08000000 | 0x00000200);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // Only the async-signal-safe setsid syscall runs between fork and exec.
        unsafe {
            command.pre_exec(|| {
                if libc::setsid() == -1 {
                    Err(std::io::Error::last_os_error())
                } else {
                    Ok(())
                }
            });
        }
    }
    command.spawn()?;
    Ok(())
}

/// The whole hook: redact known secret forms and durably spool the complete
/// provider input. `cwd` remains a transport field because the daemon needs it
/// verbatim to resolve the checkout; all semantic projection happens at drain.
///
/// Returns the stdout to print in place of the per-provider default, when the
/// WD-014 same-model-subagent-spawn policy rule denies this call (Claude
/// `PreToolUse` only). The event is spooled unconditionally either way -- the
/// `log` half of the rule's `both` action always fires.
fn observe(
    paths: &Paths,
    provider: &str,
    event_hint: &mut Option<String>,
) -> Result<Option<Value>> {
    if paused(paths)? || !paths.config.exists() {
        return Ok(None);
    }
    let limits = SpoolLimits::read(&paths.data);
    let adapter_started_at = limits.pipeline_telemetry.then(utc_now);
    let mut raw = Vec::new();
    std::io::stdin()
        .take(
            limits
                .payload_bytes
                .checked_add(1)
                .ok_or("limit overflow")?,
        )
        .read_to_end(&mut raw)?;
    if raw.len() as u64 > limits.payload_bytes {
        disk::loss(&paths.data, PAYLOAD);
        return Ok(None);
    }
    let input: Value = serde_json::from_slice(&raw)?;
    if let Some(name) = identifier(&input, "hook_event_name") {
        *event_hint = Some(name.to_string());
    }
    let cwd = input["cwd"].as_str().ok_or("missing cwd")?.to_owned();
    if !Path::new(&cwd).is_absolute() {
        return Err("relative cwd".into());
    }

    // Cheap checks first, so every other hook stays on the unchanged,
    // zero-cost path: only Claude PreToolUse on the Agent tool, with an
    // explicit model override in tool_input, ever touches the policy socket.
    // A subagent spawned without an explicit override (the common case) has
    // no model visible here at all -- an accepted, documented recall gap, not
    // a bug.
    let deny_reason = if provider == "claude"
        && identifier(&input, "hook_event_name") == Some("PreToolUse")
        && identifier(&input, "tool_name") == Some("Agent")
    {
        match (
            identifier(&input, "session_id"),
            input["tool_input"]["model"].as_str(),
        ) {
            (Some(session_id), Some(candidate_model))
                if ask_policy(&paths.data, &cwd, session_id, candidate_model) =>
            {
                Some(format!(
                    "Watchdog: this subagent would run on the same model tier ({candidate_model}) \
                     as its coordinating conversation. Downgrade the subagent's model."
                ))
            }
            _ => None,
        }
    } else {
        None
    };
    let stdout = deny_reason.map(|reason| {
        json!({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        })
    });

    let mut forwarded = input;
    forwarded
        .as_object_mut()
        .ok_or("invalid input")?
        .remove("cwd");
    let mut record = json!({
        "schema_version": 1,
        "event_id": Uuid::new_v4(),
        "received_at": utc_now(),
        "provider": provider,
        "cwd": cwd,
        "input": forwarded,
    });

    let spool = paths.data.join("spool");
    if disk::linked(&spool) {
        return Err("linked spool directory".into());
    }
    let (files, used) = disk::spool_footprint(&spool)?;
    if let Some(adapter_started_at) = adapter_started_at {
        record.as_object_mut().ok_or("invalid record")?.insert(
            "delivery".to_owned(),
            json!({
                "adapter_started_at": adapter_started_at,
                "spool_enqueued_at": utc_now(),
                "spool_occupancy": {
                    "files": files,
                    "bytes": used,
                },
            }),
        );
    }
    let bytes = serde_json::to_vec(&record)?;
    if files.saturating_add(1) > limits.spool_files
        || used.saturating_add(bytes.len() as u64) > limits.spool_bytes
    {
        disk::loss(&paths.data, QUOTA);
        return Ok(stdout);
    }
    if disk::atomic(&spool.join(format!("{}.json", Uuid::new_v4())), &bytes).is_err() {
        disk::loss(&paths.data, IO);
        return Ok(stdout);
    }
    ensure_daemon(paths)?;
    Ok(stdout)
}

/// Map an `observe()` failure to a stable, content-free category for the fault
/// log. The raw error text is never written out: it may carry a config snippet
/// or an OS path (WD-022a Part 2.1 — reason strings only).
fn fault_category(error: &(dyn std::error::Error + 'static)) -> &'static str {
    match error.to_string().as_str() {
        "cwd is not a directory" => "cwd-not-dir",
        "relative cwd" => "cwd-relative",
        "missing cwd" => "cwd-missing",
        "linked spool directory" => "linked-data",
        "oversized control" | "invalid control" => "control-invalid",
        "limit overflow" => "limits-invalid",
        _ if error.is::<std::io::Error>() => "io-error",
        _ if error.is::<serde_json::Error>() => "json-error",
        _ => "other",
    }
}

fn main() {
    #[cfg(windows)]
    prevent_stdio_inheritance();
    if std::env::args().any(|arg| arg == "--version") {
        println!(
            "agent-watchdog-hook {} (providers: codex, claude)",
            env!("CARGO_PKG_VERSION")
        );
        return;
    }
    if std::env::args().any(|arg| arg == "--help") {
        println!(
            "agent-watchdog-hook --python PATH --config PATH --data PATH --runtime PATH \
             hook <codex|claude>"
        );
        return;
    }
    let mut deny_stdout: Option<Value> = None;
    if let Ok((paths, provider)) = arguments() {
        let mut event_hint = None;
        match observe(&paths, &provider, &mut event_hint) {
            Ok(value) => deny_stdout = value,
            Err(error) => {
                disk::loss(&paths.data, 2);
                disk::fault(
                    &paths.data,
                    fault_category(error.as_ref()),
                    event_hint.as_deref().unwrap_or("unknown"),
                );
            }
        }
    }
    if let Some(value) = deny_stdout {
        // The one case where Claude gets meaningful stdout: a WD-014
        // same-model-subagent-spawn deny on PreToolUse.
        println!("{value}");
        return;
    }
    // Claude adds hook stdout to model context on SessionStart and UserPromptSubmit;
    // stay silent for Claude even when argument parsing failed.
    if !std::env::args().any(|arg| arg == "claude") {
        println!("{{}}");
    }
}

#[cfg(windows)]
fn prevent_stdio_inheritance() {
    use std::ffi::c_void;
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn GetStdHandle(kind: u32) -> *mut c_void;
        fn SetHandleInformation(handle: *mut c_void, mask: u32, flags: u32) -> i32;
    }
    // Rust Command inherits unrelated inheritable handles on Windows. A detached
    // daemon must not keep the hook caller's pipes open after this process exits.
    for kind in [-10_i32, -11, -12] {
        // These are borrowed process handles; never close them. Invalid/null
        // handles simply make SetHandleInformation fail without changing state.
        unsafe {
            SetHandleInformation(GetStdHandle(kind as u32), 1, 0);
        }
    }
}
