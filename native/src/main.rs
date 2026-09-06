mod config;
mod disk;
mod privacy;

use chrono::{SecondsFormat, Utc};
use config::{Config, Project, normalized};
use serde_json::{Value, json};
use std::fs::{self, File};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};
use uuid::Uuid;

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
const WAIT: Duration = Duration::from_millis(100);

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

fn git_checkout(cwd: &Path) -> Result<(PathBuf, Option<PathBuf>)> {
    let root = fs::canonicalize(cwd)?;
    if !root.is_dir() {
        return Err("cwd is not a directory".into());
    }
    let Some(dot_git) = root
        .ancestors()
        .map(|parent| parent.join(".git"))
        .find(|candidate| candidate.exists())
    else {
        return Ok((normalized(&root), None));
    };
    // Resolve the checkout from filesystem reads; spawning git is the fallback for
    // layouts this does not recognize (see WD-022a Part 2.2).
    if let Some((toplevel, common)) = fast_checkout(&dot_git) {
        return Ok((normalized(&toplevel), Some(normalized(&common))));
    }
    git_checkout_via_git(&root)
}

/// Join `path` onto `base` unless it is already absolute.
fn join_relative(base: &Path, path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        base.join(path)
    }
}

/// Resolve `(toplevel, git-common-dir)` from a discovered `.git` entry without
/// spawning git. Returns `None` for anything unrecognized so the caller can fall
/// back. `normalized()` collapses the `..` segments a `commondir` file introduces.
fn fast_checkout(dot_git: &Path) -> Option<(PathBuf, PathBuf)> {
    let toplevel = dot_git.parent()?.to_path_buf();
    let metadata = fs::symlink_metadata(dot_git).ok()?;
    let git_dir = if metadata.is_dir() {
        dot_git.to_path_buf()
    } else if metadata.is_file() {
        let text = fs::read_to_string(dot_git).ok()?;
        let target = text.lines().next()?.strip_prefix("gitdir:")?.trim();
        let git_dir = join_relative(&toplevel, Path::new(target));
        if !git_dir.is_dir() {
            return None;
        }
        git_dir
    } else {
        return None;
    };
    let common = match fs::read_to_string(git_dir.join("commondir")) {
        Ok(text) => join_relative(&git_dir, Path::new(text.trim())),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => git_dir.clone(),
        Err(_) => return None,
    };
    Some((toplevel, common))
}

fn git_checkout_via_git(root: &Path) -> Result<(PathBuf, Option<PathBuf>)> {
    let mut command = Command::new("git");
    command
        .arg("-C")
        .arg(root)
        .args([
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-common-dir",
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("GIT_") {
            command.env_remove(key);
        }
    }
    let mut child = command.spawn()?;
    let deadline = Instant::now() + Duration::from_millis(250);
    while child.try_wait()?.is_none() {
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err("git lookup timed out".into());
        }
        sleep(Duration::from_millis(5));
    }
    let output = child.wait_with_output()?;
    if !output.status.success() {
        return Err("git lookup failed".into());
    }
    let text = String::from_utf8(output.stdout)?;
    let lines: Vec<_> = text.lines().collect();
    if lines.len() != 2 {
        return Err("unsupported git layout".into());
    }
    Ok((
        normalized(Path::new(lines[0])),
        Some(normalized(Path::new(lines[1]))),
    ))
}

fn resolve<'a>(config: &'a Config, cwd: &Path) -> Result<Option<(&'a Project, Uuid)>> {
    let (checkout, common) = git_checkout(cwd)?;
    for project in &config.projects {
        let registered = normalized(&project.root);
        let matches = match (&common, &project.git_common_dir) {
            (Some(a), Some(b)) => *a == normalized(b),
            (None, None) => checkout.starts_with(&registered),
            _ => false,
        };
        if matches {
            let root = if common.is_none() {
                registered
            } else {
                checkout.clone()
            };
            return Ok(Some((
                project,
                Uuid::new_v5(&project.id, root.to_string_lossy().as_bytes()),
            )));
        }
    }
    Ok(None)
}

fn identifier<'a>(input: &'a Value, key: &str) -> Option<&'a str> {
    input[key]
        .as_str()
        .filter(|s| !s.trim().is_empty() && s.chars().count() <= 256)
}

fn event(
    input: &Value,
    project: &Project,
    checkout: Uuid,
    capture: bool,
    provider: &str,
) -> Result<Value> {
    let native = identifier(input, "hook_event_name").unwrap_or("unknown");
    let kind = if provider == "claude" {
        match native {
            "SessionStart" => "session.start",
            "SessionEnd" => "session.end",
            "UserPromptSubmit" => "turn.start",
            "Stop" => "turn.end",
            "PreToolUse" => "tool.start",
            "PostToolUse" | "PostToolUseFailure" => "tool.finish",
            "PreCompact" => "compaction.start",
            "PostCompact" => "compaction.end",
            "SubagentStart" => "agent.start",
            "SubagentStop" => "agent.end",
            "Notification" => "waiting",
            _ => "unknown",
        }
    } else {
        match native {
            "SessionStart" => "session.start",
            "SessionEnd" => "session.end",
            "UserPromptSubmit" => "turn.start",
            "Stop" => "turn.end",
            "PreToolUse" => "tool.start",
            "PostToolUse" => "tool.finish",
            "PreCompact" => "compaction.start",
            "PostCompact" => "compaction.end",
            "SubagentStart" => "agent.start",
            "SubagentStop" => "agent.end",
            "Interrupt" => "interrupt",
            _ => "unknown",
        }
    };
    let redact = privacy::Redactor::new()?;
    let mut content = serde_json::Map::new();
    if capture {
        for key in [
            "prompt",
            "tool_input",
            "tool_response",
            "last_assistant_message",
        ] {
            if let Some(value) = input.get(key) {
                content.insert(key.into(), value.clone());
            }
        }
    }
    let has_content = !content.is_empty();
    let response_type = input.get("tool_response").map(|v| match v {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
        Value::Number(n) if n.is_f64() => "float",
        Value::Number(_) => "int",
    });
    let namespace = json!({
        "hook_event_name": if kind == "unknown" { "unknown" } else { native },
        "tool_use_id": identifier(input, "tool_use_id"),
        "tool_name": identifier(input, "tool_name"),
        "tool_response_type": response_type,
        "content": if has_content { Value::Object(content) } else { json!("omitted") }});
    let mut payload = serde_json::Map::new();
    payload.insert(provider.to_string(), namespace);
    let mut envelope = json!({"schema_version": 1, "event_id": Uuid::new_v4(),
        "provider": provider, "provider_version": null, "surface": "unknown",
        "project_id": project.id, "checkout_id": checkout,
        "kind": kind, "source": "hook", "occurred_at": null,
        "received_at": Utc::now().to_rfc3339_opts(SecondsFormat::Micros, true),
        "native_event_id": null, "parent_agent_id": null,
        "payload": Value::Object(payload),
        "availability": {"content": if has_content {"observed"} else {"unavailable"},
            "surface": "unknown", "provider_version": "unknown", "occurred_at": "unavailable",
            "tool_outcome": "unknown"}});
    for key in ["session_id", "turn_id", "agent_id"] {
        envelope[key] = identifier(input, key)
            .map(|s| json!(redact.text(s)))
            .unwrap_or(Value::Null);
    }
    envelope["payload"] = redact.value(&envelope["payload"]);
    Ok(envelope)
}

fn ensure_daemon(paths: &Paths) -> Result<()> {
    let Ok(guard) = disk::lock(&paths.data.join("daemon.lock"), Duration::ZERO) else {
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
        command.creation_flags(0x00000008 | 0x00000200);
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

fn observe(paths: &Paths, provider: &str, event_hint: &mut Option<String>) -> Result<()> {
    if paused(paths)? || !paths.config.exists() {
        return Ok(());
    }
    let config = Config::read(&paths.config)?;
    if config.projects.is_empty() {
        return Ok(());
    }
    let maximum = config
        .projects
        .iter()
        .map(|p| p.limits(&config.defaults).map(|l| l.payload_bytes))
        .collect::<Result<Vec<_>>>()?
        .into_iter()
        .max()
        .ok_or("no limit")?;
    let mut raw = Vec::new();
    std::io::stdin()
        .take(maximum.checked_add(1).ok_or("limit overflow")?)
        .read_to_end(&mut raw)?;
    if raw.len() as u64 > maximum {
        disk::loss(&paths.data, 1);
        return Ok(());
    }
    let input: Value = serde_json::from_slice(&raw)?;
    if let Some(name) = identifier(&input, "hook_event_name") {
        *event_hint = Some(name.to_string());
    }
    let cwd = Path::new(input["cwd"].as_str().ok_or("missing cwd")?);
    if !cwd.is_absolute() {
        return Err("relative cwd".into());
    }
    let Some((project, checkout)) = resolve(&config, cwd)? else {
        return Ok(());
    };
    let limits = project.limits(&config.defaults)?;
    let root = paths.data.join("projects").join(project.id.to_string());
    if raw.len() as u64 > limits.payload_bytes {
        disk::loss(&root, 1);
        return Ok(());
    }
    let envelope = event(&input, project, checkout, limits.capture_content, provider)?;
    // Match Python's ASCII JSON size accounting before durable admission.
    let serialized = serde_json::to_string(&envelope)?;
    let mut bytes = Vec::new();
    for character in serialized.chars() {
        if character.is_ascii() {
            bytes.push(character as u8);
        } else {
            for unit in character.encode_utf16(&mut [0; 2]) {
                bytes.extend_from_slice(format!("\\u{unit:04x}").as_bytes());
            }
        }
    }
    if bytes.len() as u64 > limits.payload_bytes {
        disk::loss(&root, 1);
        return Ok(());
    }
    fs::create_dir_all(&paths.data)?;
    let Ok(_control) = disk::lock(&paths.data.join("control.lock"), WAIT) else {
        disk::loss(&root, 4);
        return Ok(());
    };
    if paused(paths)? {
        return Ok(());
    }
    // Recheck the allowlist/settings under the same lock as registry mutations.
    let current = Config::read(&paths.config)?;
    let Some(registered) = current.projects.iter().find(|p| p.id == project.id) else {
        return Ok(());
    };
    if serde_json::to_value(&limits)?
        != serde_json::to_value(registered.limits(&current.defaults)?)?
        || normalized(&registered.root) != normalized(&project.root)
        || registered.git_common_dir.as_deref().map(normalized)
            != project.git_common_dir.as_deref().map(normalized)
    {
        return Err("configuration changed during admission".into());
    }
    if disk::linked(&root) {
        return Err("linked project data".into());
    }
    fs::create_dir_all(&root)?;
    let Ok(_admission) = disk::lock(&root.join("admission.lock"), WAIT) else {
        disk::loss(&root, 4);
        return Ok(());
    };
    let size = bytes.len() as u64;
    if disk::usage(&root.join("inbox"))?.saturating_add(size) > limits.inbox_bytes
        || disk::room(&root, &limits)? < size.saturating_add(4096)
    {
        disk::loss(&root, 0);
        drop(_admission);
        ensure_daemon(paths)?;
        return Ok(());
    }
    if disk::atomic(
        &root.join("inbox").join(format!("{}.json", Uuid::new_v4())),
        &bytes,
    )
    .is_err()
    {
        disk::loss(&root, 3);
        return Ok(());
    }
    drop(_admission);
    ensure_daemon(paths)
}

/// Map an `observe()` failure to a stable, content-free category for the fault
/// log. The raw error text is never written out: it may carry a config snippet
/// or an OS path (WD-022a Part 2.1 — reason strings only).
fn fault_category(error: &(dyn std::error::Error + 'static)) -> &'static str {
    match error.to_string().as_str() {
        "git lookup timed out" => "git-timeout",
        "git lookup failed" => "git-failed",
        "unsupported git layout" => "git-layout",
        "cwd is not a directory" => "cwd-not-dir",
        "relative cwd" => "cwd-relative",
        "missing cwd" => "cwd-missing",
        "configuration changed during admission" => "config-race",
        "linked project data" => "linked-data",
        "oversized control" | "invalid control" => "control-invalid",
        "no limit" | "limit overflow" => "limits-invalid",
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
    if let Ok((paths, provider)) = arguments() {
        let mut event_hint = None;
        if let Err(error) = observe(&paths, &provider, &mut event_hint) {
            disk::loss(&paths.data, 2);
            disk::fault(
                &paths.data,
                fault_category(error.as_ref()),
                event_hint.as_deref().unwrap_or("unknown"),
            );
        }
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
