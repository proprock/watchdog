mod disk;

use chrono::{SecondsFormat, Utc};
use serde_json::{Value, json};
use std::fs::File;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};
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

fn arguments() -> Result<(Paths, String, Option<String>)> {
    let mut values = std::collections::HashMap::new();
    let mut positionals: Vec<String> = Vec::new();
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--config"
            | "--data"
            | "--runtime"
            | "--python"
            | "--installation"
            | "--codex-control-profile" => {
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
    let paths = {
        let mut path = |name: &str| -> Result<PathBuf> {
            let path = PathBuf::from(values.remove(name).ok_or("missing path")?);
            if !path.is_absolute() {
                return Err("absolute paths required".into());
            }
            Ok(path)
        };
        Paths {
            config: path("--config")?,
            data: path("--data")?,
            runtime: path("--runtime")?,
            python: path("--python")?,
        }
    };
    let codex_control_profile = values.remove("--codex-control-profile");
    Ok((paths, provider, codex_control_profile))
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

/// One hook the daemon wants to be asked about.
struct Subscription {
    provider: String,
    event: String,
    tool: Option<String>,
}

/// Discovery record for the daemon's decision channel (loopback-only, started
/// only while the daemon runs). Absent, unreadable, or any other schema -> no
/// query is attempted and every hook proceeds exactly as an unsubscribed one.
struct PolicySocket {
    port: u16,
    benchmark_http: bool,
    token: String,
    timeout: Duration,
    max_request_bytes: u64,
    subscriptions: Vec<Subscription>,
}

/// Discovery file read cap. Generous so the subscription list can grow.
const DISCOVERY_BYTES: u64 = 16 * 1024;
const DEFAULT_DECISION_TIMEOUT_MS: u64 = 300;
/// The installed hook entries time out at two seconds; the whole round trip
/// must finish well inside that, whatever the daemon publishes.
const MAX_DECISION_TIMEOUT_MS: u64 = 1500;
/// The decision-channel schema this adapter speaks. `--version` declares it so
/// `agent-watchdog readiness` can compare it with the schema the daemon publishes
/// in `policy/socket.json` and say so when they differ, instead of the adapter
/// silently ignoring a channel it cannot use.
const DECISION_SCHEMA: u64 = 2;

fn banner() -> String {
    format!(
        "agent-watchdog-hook {} (providers: codex, claude; decision schema {})",
        env!("CARGO_PKG_VERSION"),
        DECISION_SCHEMA
    )
}

impl PolicySocket {
    fn read(data: &Path) -> Option<Self> {
        let benchmark_http = std::env::var("WATCHDOG_BENCHMARK_HTTP").as_deref() == Ok("1");
        let discovery = if benchmark_http {
            "benchmark-http.json"
        } else {
            "socket.json"
        };
        let mut bytes = Vec::new();
        File::open(data.join("policy").join(discovery))
            .ok()?
            .take(DISCOVERY_BYTES + 1)
            .read_to_end(&mut bytes)
            .ok()?;
        if bytes.len() as u64 > DISCOVERY_BYTES {
            return None;
        }
        let value: Value = serde_json::from_slice(&bytes).ok()?;
        if value["schema_version"].as_u64() != Some(DECISION_SCHEMA) {
            return None;
        }
        let port_key = if benchmark_http {
            "benchmark_http_port"
        } else {
            "port"
        };
        let port = u16::try_from(value[port_key].as_u64()?).ok()?;
        let token = value["token"].as_str()?;
        if token.is_empty() || token.len() > 128 {
            return None;
        }
        let max_request_bytes = value["max_request_bytes"].as_u64().filter(|n| *n > 0)?;
        let timeout_ms = value["timeout_ms"]
            .as_u64()
            .unwrap_or(DEFAULT_DECISION_TIMEOUT_MS)
            .clamp(1, MAX_DECISION_TIMEOUT_MS);
        let mut subscriptions = Vec::new();
        for entry in value["subscriptions"].as_array()? {
            subscriptions.push(Subscription {
                provider: entry["provider"].as_str()?.to_owned(),
                event: entry["hook_event_name"].as_str()?.to_owned(),
                tool: match &entry["tool_name"] {
                    Value::Null => None,
                    other => Some(other.as_str()?.to_owned()),
                },
            });
        }
        Some(PolicySocket {
            port,
            benchmark_http,
            token: token.to_owned(),
            timeout: Duration::from_millis(timeout_ms),
            max_request_bytes,
            subscriptions,
        })
    }

    fn subscribed(&self, provider: &str, event: &str, tool: Option<&str>) -> bool {
        self.subscriptions.iter().any(|s| {
            s.provider == provider
                && s.event == event
                && s.tool.as_deref().is_none_or(|wanted| Some(wanted) == tool)
        })
    }
}

/// The daemon's answer to one hook call. `allow` is "no opinion".
struct Decision {
    action: String,
    reason: Option<String>,
    updated_input: Option<Value>,
    context: Option<String>,
}

/// Ask the running daemon, synchronously and inside one strict deadline,
/// what to do about this hook call. `None` on any failure (refused or
/// timed-out connection, oversized request, malformed or unknown response)
/// always means "no opinion": a false deny is the one Watchdog failure that
/// would actually stop the harness, so every error path fails open.
fn ask_decision(
    socket: &PolicySocket,
    provider: &str,
    codex_control_profile: Option<&str>,
    input: &Value,
    event_id: &Uuid,
) -> Option<Decision> {
    let deadline = Instant::now() + socket.timeout;
    let remaining = || {
        deadline
            .checked_duration_since(Instant::now())
            .filter(|left| !left.is_zero())
    };
    let request = json!({
        "schema_version": DECISION_SCHEMA,
        "token": socket.token,
        "provider": provider,
        "codex_control_profile": codex_control_profile,
        "event_id": event_id,
        "input": input,
    });
    let mut payload = serde_json::to_vec(&request).ok()?;
    payload.push(b'\n');
    if payload.len() as u64 > socket.max_request_bytes {
        return None;
    }
    let address = std::net::SocketAddr::from(([127, 0, 0, 1], socket.port));
    let mut stream = std::net::TcpStream::connect_timeout(&address, remaining()?).ok()?;
    stream.set_write_timeout(Some(remaining()?)).ok()?;
    // Armed while the connection is still open: macOS rejects setsockopt (EINVAL)
    // once both directions are closed, i.e. after our shutdown and the daemon's FIN.
    stream.set_read_timeout(Some(remaining()?)).ok()?;
    if socket.benchmark_http {
        let header = format!(
            "POST /decision HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            payload.len()
        );
        stream.write_all(header.as_bytes()).ok()?;
        stream.set_write_timeout(Some(remaining()?)).ok()?;
    }
    stream.write_all(&payload).ok()?;
    // A peer that already answered and closed can make this fail (ENOTCONN on
    // macOS); the read below still returns what it sent, or fails and fails open.
    let _ = stream.shutdown(std::net::Shutdown::Write);
    let mut response = Vec::new();
    let mut chunk = [0_u8; 4096];
    loop {
        // Re-arm with the time left, best effort: once the peer has closed, the
        // read cannot block, and the timeout armed above still bounds it before then.
        let left = remaining()?;
        let _ = stream.set_read_timeout(Some(left));
        match stream.read(&mut chunk).ok()? {
            0 => break,
            read => response.extend_from_slice(&chunk[..read]),
        }
        let header_allowance = if socket.benchmark_http { 8192 } else { 0 };
        if response.len() as u64 > socket.max_request_bytes.saturating_add(header_allowance) {
            return None;
        }
    }
    let response = if socket.benchmark_http {
        let boundary = response.windows(4).position(|w| w == b"\r\n\r\n")?;
        if boundary > 8192 {
            return None;
        }
        let headers = std::str::from_utf8(&response[..boundary]).ok()?;
        let mut lines = headers.split("\r\n");
        if !matches!(lines.next(), Some("HTTP/1.0 200 OK" | "HTTP/1.1 200 OK")) {
            return None;
        }
        let length = lines.find_map(|line| {
            let (name, value) = line.split_once(':')?;
            name.eq_ignore_ascii_case("content-length")
                .then(|| value.trim().parse::<usize>().ok())
                .flatten()
        })?;
        let body = &response[boundary + 4..];
        if body.len() != length || body.len() as u64 > socket.max_request_bytes {
            return None;
        }
        body
    } else {
        &response
    };
    let value: Value = serde_json::from_slice(response).ok()?;
    if value["schema_version"].as_u64() != Some(DECISION_SCHEMA) {
        return None;
    }
    let action = value["action"].as_str()?;
    if !["allow", "deny", "ask", "rewrite", "context", "block"].contains(&action) {
        return None;
    }
    let text = |key: &str| value[key].as_str().map(str::to_owned);
    Some(Decision {
        action: action.to_owned(),
        reason: text("reason"),
        updated_input: value["updated_input"]
            .is_object()
            .then(|| value["updated_input"].clone()),
        context: text("context"),
    })
}

/// Render a decision as the hook's stdout. Only Claude WD-139 and the
/// installation-scoped Codex WD-151 cells confirmed live are rendered;
/// every other combination is `None` (silence),
/// and `allow` is always silence -- an explicit `permissionDecision: "allow"`
/// would auto-approve the tool and bypass the user's permission rules. Keep in
/// step with `_RENDERABLE` in
/// `agent_watchdog/rules/api.py`.
fn render(
    provider: &str,
    codex_control_profile: Option<&str>,
    input: &Value,
    decision: &Decision,
) -> Option<Value> {
    let event = identifier(input, "hook_event_name")?;
    if provider == "codex" {
        return render_codex(codex_control_profile?, event, input, decision);
    }
    if provider != "claude" {
        return None;
    }
    let reason = || {
        decision
            .reason
            .clone()
            .unwrap_or_else(|| "Blocked by a Watchdog rule.".to_owned())
    };
    match (event, decision.action.as_str()) {
        ("PreToolUse", "deny" | "ask") => Some(json!({
            "hookSpecificOutput": {
                "hookEventName": event,
                "permissionDecision": decision.action,
                "permissionDecisionReason": reason(),
            }
        })),
        // Claude Code treats a rewrite as an approval: it carries
        // `permissionDecision: "allow"` with the full replacement input.
        ("PreToolUse", "rewrite") => Some(json!({
            "hookSpecificOutput": {
                "hookEventName": event,
                "permissionDecision": "allow",
                "permissionDecisionReason": reason(),
                "updatedInput": decision.updated_input.clone()?,
            }
        })),
        (
            "PreToolUse" | "PostToolUse" | "UserPromptSubmit" | "SessionStart" | "PreCompact",
            "context",
        ) => Some(json!({
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": decision.context.clone()?,
            }
        })),
        ("PostToolUse" | "Stop" | "SubagentStop", "block") if !stop_continuing(event, input) => {
            Some(json!({ "decision": "block", "reason": reason() }))
        }
        _ => None,
    }
}

/// Exact Codex desktop app/backend pair confirmed by WD-153; keep in step with
/// `CODEX_CELLS` in `agent_watchdog/rules/api.py`.
const DESKTOP_PROFILE: &str = "windows-desktop-26.1016.62342-0.163.0-alpha.11.2";

fn render_codex(profile: &str, event: &str, input: &Value, decision: &Decision) -> Option<Value> {
    if !cfg!(windows) {
        return None;
    }
    let tool = identifier(input, "tool_name");
    let reason = || {
        decision
            .reason
            .clone()
            .unwrap_or_else(|| "Blocked by a Watchdog rule.".to_owned())
    };
    let output = match (profile, event, tool, decision.action.as_str()) {
        (DESKTOP_PROFILE, "SessionStart", _, "context")
        | ("windows-cli-0.160.0", "SessionStart" | "UserPromptSubmit", _, "context")
        | ("windows-cli-0.160.0", "PostToolUse", Some("Bash"), "context")
        | ("windows-cli-exec-0.162.0-alpha.17.2", "PreToolUse", Some("Bash"), "context")
        | ("windows-cli-exec-0.162.0-alpha.17.2", "PreToolUse", Some("apply_patch"), "context")
        | ("windows-cli-exec-0.162.0-alpha.17.2", "SubagentStart", _, "context") => json!({
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": decision.context.clone()?,
            }
        }),
        (DESKTOP_PROFILE, "PreToolUse", Some("Bash" | "apply_patch"), "deny")
        | ("windows-cli-0.160.0", "PreToolUse", Some("Bash"), "deny")
        | ("windows-cli-exec-0.162.0-alpha.17.2", "PreToolUse", Some("apply_patch"), "deny") => {
            json!({
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason(),
                }
            })
        }
        (DESKTOP_PROFILE, "PreToolUse", Some("Bash" | "apply_patch"), "rewrite")
        | ("windows-cli-0.160.0", "PreToolUse", Some("Bash"), "rewrite")
        | (
            "windows-cli-interactive-0.162.0-alpha.17.2",
            "PreToolUse",
            Some("apply_patch"),
            "rewrite",
        ) if input["tool_input"].is_object()
            && input["tool_input"]["command"].is_string()
            && decision
                .updated_input
                .as_ref()
                .is_some_and(|updated| updated.is_object() && updated["command"].is_string()) =>
        {
            json!({
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "updatedInput": decision.updated_input.clone()?,
                }
            })
        }
        (DESKTOP_PROFILE, "Stop" | "SubagentStop", _, "block")
        | ("windows-cli-0.160.0", "SubagentStop", _, "block")
        | ("windows-cli-exec-0.162.0-alpha.17.2", "Stop", _, "block")
            if !stop_continuing(event, input) =>
        {
            json!({
                "decision": "block",
                "reason": reason(),
            })
        }
        (DESKTOP_PROFILE, "PostToolUse", Some("Bash"), "block") => json!({
            "decision": "block",
            "reason": reason(),
        }),
        _ => return None,
    };
    Some(output)
}

/// A `Stop`/`SubagentStop` already continuing from an earlier block carries
/// `stop_hook_active`; blocking it again would loop the harness. Anything but
/// an explicit `false` counts as continuing, so the guard fails safe.
fn stop_continuing(event: &str, input: &Value) -> bool {
    matches!(event, "Stop" | "SubagentStop")
        && input
            .get("stop_hook_active")
            .is_some_and(|active| active.as_bool() != Some(false))
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

/// The whole hook: durably spool the complete
/// provider input. `cwd` remains a transport field because the daemon needs it
/// verbatim to resolve the checkout; all semantic projection happens at drain.
///
/// Returns the stdout to print in place of the per-provider default, when the
/// daemon's decision channel answers a subscribed hook with an action this
/// adapter can render for the provider (see `render`). The event is spooled
/// unconditionally either way -- the `log` half of every rule always fires.
fn observe(
    paths: &Paths,
    provider: &str,
    codex_control_profile: Option<&str>,
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

    // The spool record and any control event the daemon records for this call
    // share one id, so a control event can point at the hook event it answered.
    let event_id = Uuid::new_v4();

    // Unsubscribed events (and every call while no daemon publishes a
    // discovery file) never connect: the cost is one bounded file read. A
    // `Stop` already continuing from a block (`stop_hook_active`) is never
    // asked about, so a block cannot loop the harness.
    let (stdout, control_delivery) = match (
        identifier(&input, "hook_event_name"),
        PolicySocket::read(&paths.data),
    ) {
        (Some(event), Some(socket))
            if socket.subscribed(provider, event, identifier(&input, "tool_name"))
                && !stop_continuing(event, &input) =>
        {
            let decision =
                ask_decision(&socket, provider, codex_control_profile, &input, &event_id);
            let response_received = decision.is_some();
            let requested_action = decision.as_ref().map(|decision| decision.action.clone());
            let stdout = decision
                .as_ref()
                .and_then(|decision| render(provider, codex_control_profile, &input, decision));
            let adapter_rendered = decision
                .as_ref()
                .and_then(|decision| (decision.action != "allow").then_some(stdout.is_some()));
            (
                stdout,
                Some(json!({
                    "response_received": response_received,
                    "adapter_rendered": adapter_rendered,
                    "requested_action": requested_action,
                })),
            )
        }
        _ => (None, None),
    };

    let mut forwarded = input;
    forwarded
        .as_object_mut()
        .ok_or("invalid input")?
        .remove("cwd");
    let mut record = json!({
        "schema_version": 2,
        "record_type": "hook",
        "event_id": event_id,
        "received_at": utc_now(),
        "provider": provider,
        "cwd": cwd,
        "input": forwarded,
    });

    let spool = paths.data.join("spool");
    if disk::linked(&spool) {
        return Err("linked spool directory".into());
    }
    std::fs::create_dir_all(&paths.data)?;
    let admission = disk::lock(
        &paths.data.join("admission.lock"),
        Duration::from_millis(100),
    )?;
    let (files, used) = disk::spool_footprint(&spool)?;
    let mut delivery = json!({});
    let delivery_object = delivery.as_object_mut().ok_or("invalid delivery")?;
    if let Some(adapter_started_at) = adapter_started_at {
        delivery_object.insert("adapter_started_at".to_owned(), json!(adapter_started_at));
        delivery_object.insert("spool_enqueued_at".to_owned(), json!(utc_now()));
        delivery_object.insert(
            "spool_occupancy".to_owned(),
            json!({"files": files, "bytes": used}),
        );
    }
    if let Some(control_delivery) = control_delivery {
        delivery_object.insert("control".to_owned(), control_delivery);
    }
    if !delivery_object.is_empty() {
        record
            .as_object_mut()
            .ok_or("invalid record")?
            .insert("delivery".to_owned(), delivery);
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
    drop(admission);
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
        println!("{}", banner());
        return;
    }
    if std::env::args().any(|arg| arg == "--help") {
        println!(
            "agent-watchdog-hook --python PATH --config PATH --data PATH --runtime PATH \
             [--codex-control-profile VALUE] \
             hook <codex|claude>"
        );
        return;
    }
    let mut deny_stdout: Option<Value> = None;
    if let Ok((paths, provider, codex_control_profile)) = arguments() {
        let mut event_hint = None;
        match observe(
            &paths,
            &provider,
            codex_control_profile.as_deref(),
            &mut event_hint,
        ) {
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
        // The only case where Claude gets meaningful stdout: a rendered
        // decision-channel action for a subscribed hook.
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_banner_declares_the_decision_schema_the_adapter_speaks() {
        let line = banner();
        assert!(line.starts_with("agent-watchdog-hook "));
        assert!(line.ends_with(&format!("decision schema {DECISION_SCHEMA})")));
    }

    fn decision(action: &str) -> Decision {
        Decision {
            action: action.to_owned(),
            reason: Some("because".to_owned()),
            updated_input: None,
            context: Some("note".to_owned()),
        }
    }

    #[test]
    fn render_denies_a_claude_pretooluse_with_the_reason() {
        let input = json!({"hook_event_name": "PreToolUse"});
        let output = render("claude", None, &input, &decision("deny")).unwrap();
        assert_eq!(output["hookSpecificOutput"]["permissionDecision"], "deny");
        assert_eq!(
            output["hookSpecificOutput"]["permissionDecisionReason"],
            "because"
        );
    }

    #[test]
    fn render_keeps_allow_and_codex_silent() {
        let input = json!({"hook_event_name": "PreToolUse"});
        assert!(render("claude", None, &input, &decision("allow")).is_none());
        assert!(render("codex", None, &input, &decision("deny")).is_none());
    }

    #[test]
    fn render_never_blocks_a_stop_that_is_already_continuing() {
        let continuing = json!({"hook_event_name": "Stop", "stop_hook_active": true});
        let first = json!({"hook_event_name": "Stop", "stop_hook_active": false});
        assert!(render("claude", None, &continuing, &decision("block")).is_none());
        assert_eq!(
            render("claude", None, &first, &decision("block")).unwrap()["decision"],
            "block"
        );
    }

    #[test]
    fn render_needs_an_object_to_rewrite() {
        let input = json!({"hook_event_name": "PreToolUse"});
        assert!(render("claude", None, &input, &decision("rewrite")).is_none());
        let mut with_input = decision("rewrite");
        with_input.updated_input = Some(json!({"command": "ls"}));
        let output = render("claude", None, &input, &with_input).unwrap();
        assert_eq!(output["hookSpecificOutput"]["permissionDecision"], "allow");
        assert_eq!(
            output["hookSpecificOutput"]["updatedInput"]["command"],
            "ls"
        );
    }

    #[test]
    fn stop_guard_fails_safe_on_anything_but_an_explicit_false() {
        let active = |value: Value| json!({"stop_hook_active": value});
        assert!(!stop_continuing("Stop", &json!({})));
        assert!(!stop_continuing("Stop", &active(json!(false))));
        assert!(stop_continuing("Stop", &active(json!(true))));
        assert!(stop_continuing("SubagentStop", &active(json!("yes"))));
        assert!(!stop_continuing("PreToolUse", &active(json!(true))));
    }

    #[test]
    fn identifier_rejects_blank_and_overlong_values() {
        let input = json!({"a": "  ", "b": "x".repeat(257), "c": "ok", "d": 7});
        assert_eq!(identifier(&input, "a"), None);
        assert_eq!(identifier(&input, "b"), None);
        assert_eq!(identifier(&input, "c"), Some("ok"));
        assert_eq!(identifier(&input, "d"), None);
    }

    #[test]
    fn fault_category_is_content_free() {
        let known: Box<dyn std::error::Error> = "relative cwd".into();
        assert_eq!(fault_category(&*known), "cwd-relative");
        let secret: Box<dyn std::error::Error> = r"C:\Users\someone\secret".into();
        assert_eq!(fault_category(&*secret), "other");
        assert_eq!(fault_category(&std::io::Error::other("x")), "io-error");
    }

    /// A daemon that answers the request line and closes at once must still be
    /// heard: the closed-socket state is where macOS refuses `setsockopt`.
    #[test]
    fn ask_decision_reads_an_answer_from_a_peer_that_closes_immediately() {
        use std::io::{BufRead, BufReader};
        let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut line = String::new();
            BufReader::new(stream.try_clone().unwrap())
                .read_line(&mut line)
                .unwrap();
            stream
                .write_all(br#"{"schema_version": 2, "action": "deny", "reason": "no"}"#)
                .unwrap();
        });
        let socket = PolicySocket {
            port,
            benchmark_http: false,
            token: "t".to_owned(),
            timeout: Duration::from_secs(5),
            max_request_bytes: 65536,
            subscriptions: Vec::new(),
        };
        let input = json!({"hook_event_name": "PreToolUse"});
        let decision = ask_decision(&socket, "claude", None, &input, &Uuid::new_v4()).unwrap();
        server.join().unwrap();
        assert_eq!(decision.action, "deny");
        assert_eq!(decision.reason.as_deref(), Some("no"));
    }
}
