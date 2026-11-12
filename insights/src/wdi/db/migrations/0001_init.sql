-- Normalized schema for Claude Code and Codex sessions.
-- Agent-specific fields stay NULL for the other agent; raw extras go to `extra` JSON.

-- Derived. Required by preserved sessions.
CREATE TABLE machines (
    machine_id TEXT PRIMARY KEY,
    hostname   TEXT,
    os         TEXT,
    created_at TEXT NOT NULL
);

-- Derived. Required by preserved sessions.
CREATE TABLE projects (
    project_id     INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE
);

-- Derived. Associated paths are transferred with imported legacy projects.
CREATE TABLE project_paths (
    project_id INTEGER NOT NULL REFERENCES projects (project_id) ON DELETE CASCADE,
    machine_id TEXT NOT NULL REFERENCES machines (machine_id),
    path       TEXT NOT NULL,
    UNIQUE (machine_id, path)
);

-- Primary for legacy sessions. Otherwise derived.
CREATE TABLE sessions (
    session_id            TEXT PRIMARY KEY,
    machine_id            TEXT NOT NULL REFERENCES machines (machine_id),
    project_id            INTEGER REFERENCES projects (project_id),
    agent                 TEXT NOT NULL CHECK (agent IN ('claude', 'codex')),
    origin                TEXT NOT NULL DEFAULT 'file' CHECK (origin IN ('file', 'watchdog', 'legacy')),
    source_path           TEXT,
    cwd                   TEXT,
    git_branch            TEXT,
    cli_version           TEXT,
    originator            TEXT,
    title                 TEXT,
    model                 TEXT,
    started_at            TEXT,
    ended_at              TEXT,
    duration_ms           INTEGER,
    user_msg_count        INTEGER NOT NULL DEFAULT 0,
    assistant_msg_count   INTEGER NOT NULL DEFAULT 0,
    tool_call_count       INTEGER NOT NULL DEFAULT 0,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER,  -- Claude only
    reasoning_tokens      INTEGER,  -- Codex only
    context_window        INTEGER,  -- Codex only
    task_duration_ms      INTEGER,  -- Codex: SUM of task_complete durations
    time_to_first_token_ms INTEGER, -- Codex: AVG across task_complete turns
    rate_limit_primary_used_percent REAL,
    rate_limit_secondary_used_percent REAL,
    effort                TEXT,
    approval_policy       TEXT,
    sandbox_policy_type   TEXT,
    collaboration_mode    TEXT,
    developer_instructions_hash TEXT,
    user_instructions_hash TEXT,
    cache_creation_5m_tokens INTEGER, -- Claude only
    cache_creation_1h_tokens INTEGER, -- Claude only
    usage_iteration_count INTEGER NOT NULL DEFAULT 0,
    hook_count            INTEGER NOT NULL DEFAULT 0,
    hook_error_count      INTEGER NOT NULL DEFAULT 0,
    hook_commands         TEXT,
    hook_has_output_count INTEGER NOT NULL DEFAULT 0,
    compaction_count      INTEGER NOT NULL DEFAULT 0,
    api_error_count       INTEGER NOT NULL DEFAULT 0,
    entrypoint            TEXT,             -- Claude: cli / claude-desktop / sdk-cli;
                                            -- Codex: session_meta.source (vscode / subagent:<kind>)
    web_search_requests   INTEGER,          -- Claude: server-side searches; Codex: web tool calls
    web_fetch_requests    INTEGER,          -- Claude: server-side web fetches (billed)
    parent_session_id     TEXT,             -- Codex: subagent thread -> parent session id
    turn_count            INTEGER NOT NULL DEFAULT 0, -- Codex: turns (task_started/complete)
    turn_abort_count      INTEGER NOT NULL DEFAULT 0, -- Codex: turn_aborted (interruptions)
    peak_context_tokens   INTEGER,          -- both agents: MAX per-turn context from messages
    rate_limit_plan       TEXT,             -- Codex: rate_limits.plan_type ("plus")
    -- Background/headless one-shot sessions (observers etc.); excluded from
    -- aggregates by default. Recomputed at import from entrypoint.
    is_noise              INTEGER NOT NULL DEFAULT 0,
    extra                 TEXT
);

CREATE INDEX idx_sessions_project ON sessions (project_id);
CREATE INDEX idx_sessions_started ON sessions (started_at);
CREATE INDEX idx_sessions_agent ON sessions (agent);
CREATE INDEX idx_sessions_parent ON sessions (parent_session_id);

-- stream_id = 'main' for the session transcript, agent_run_id for subagent traces.
-- Primary for legacy sessions. Otherwise derived.
CREATE TABLE messages (
    session_id            TEXT NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    machine_id            TEXT NOT NULL,
    stream_id             TEXT NOT NULL DEFAULT 'main',
    idx                   INTEGER NOT NULL,
    message_id            TEXT,
    role                  TEXT NOT NULL,
    ts                    TEXT,
    model                 TEXT,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cache_read_tokens     INTEGER,
    cache_creation_tokens INTEGER,
    reasoning_tokens      INTEGER,
    stop_reason           TEXT,   -- Claude: end_turn / max_tokens / tool_use ...
    content_preview       TEXT,
    extra                 TEXT,
    PRIMARY KEY (session_id, stream_id, idx)
);

-- Primary for legacy sessions. Otherwise derived.
CREATE TABLE tool_calls (
    session_id     TEXT NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    machine_id     TEXT NOT NULL,
    stream_id      TEXT NOT NULL DEFAULT 'main',
    idx            INTEGER NOT NULL,  -- ordinal of the call within the stream
    call_id        TEXT,              -- tool_use id (Claude) / call_id (Codex), links results
    tool_name      TEXT NOT NULL,
    category       TEXT NOT NULL DEFAULT 'other',
    target         TEXT,              -- normalized file path / search pattern / command
    ts             TEXT,
    duration_ms    INTEGER,
    exit_code      INTEGER,
    is_error       INTEGER NOT NULL DEFAULT 0,
    command_kind   TEXT,
    stdout_bytes   INTEGER,
    stderr_bytes   INTEGER,
    output_bytes   INTEGER,
    patch_success  INTEGER,
    changed_file_count INTEGER,
    lines_added    INTEGER,
    lines_removed  INTEGER,
    input_preview  TEXT,
    output_preview TEXT,
    extra          TEXT,
    PRIMARY KEY (session_id, stream_id, idx)
);

CREATE INDEX idx_tool_calls_name ON tool_calls (tool_name);
CREATE INDEX idx_tool_calls_target ON tool_calls (session_id, target);
CREATE INDEX idx_tool_calls_call_id ON tool_calls (session_id, call_id);

-- Privacy-preserving structural record of the observable model context.
-- Text, paths and complete commands are represented only by sizes and hashes.
-- Primary for legacy sessions. Otherwise derived.
CREATE TABLE context_events (
    session_id            TEXT NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    machine_id            TEXT NOT NULL,
    stream_id             TEXT NOT NULL DEFAULT 'main',
    idx                   INTEGER NOT NULL,
    ts                    TEXT,
    turn_id               TEXT,
    model_call_index      INTEGER,
    event_type            TEXT NOT NULL,
    role                  TEXT,
    category              TEXT NOT NULL,
    char_count            INTEGER,
    byte_count            INTEGER,
    fingerprint           TEXT,
    tool_name             TEXT,
    command_kind          TEXT,
    target_fingerprint    TEXT,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cache_read_tokens     INTEGER,
    cache_creation_tokens INTEGER,
    reasoning_tokens      INTEGER,
    context_window        INTEGER,
    compaction_generation INTEGER NOT NULL DEFAULT 0,
    coverage              TEXT NOT NULL DEFAULT 'visible',
    extra                 TEXT,
    PRIMARY KEY (session_id, stream_id, idx)
);

CREATE INDEX idx_context_events_call
    ON context_events (session_id, stream_id, model_call_index);
CREATE INDEX idx_context_events_fingerprint
    ON context_events (session_id, fingerprint);
-- extra includes provider usage identity, raw observations, call_id and size method.
CREATE INDEX idx_context_events_tool_identity
    ON context_events (session_id, stream_id, json_extract(extra, '$.call_id'));

CREATE INDEX idx_context_events_type
    ON context_events (event_type);

-- Primary for legacy sessions. Otherwise derived.
CREATE TABLE subagent_runs (
    agent_run_id         TEXT PRIMARY KEY,
    session_id           TEXT NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    machine_id           TEXT NOT NULL,
    agent_type           TEXT,
    prompt               TEXT,
    status               TEXT,
    total_duration_ms    INTEGER,
    total_tokens         INTEGER,
    total_tool_use_count INTEGER,
    read_count           INTEGER,
    search_count         INTEGER,
    bash_count           INTEGER,
    edit_file_count      INTEGER,
    lines_added          INTEGER,
    lines_removed        INTEGER,
    extra                TEXT
);

CREATE INDEX idx_subagent_session ON subagent_runs (session_id);

-- Derived.
CREATE TABLE import_state (
    machine_id       TEXT NOT NULL,
    source_path      TEXT NOT NULL,
    file_size        INTEGER NOT NULL,
    file_mtime       REAL NOT NULL,
    byte_offset      INTEGER NOT NULL,
    last_imported_at TEXT NOT NULL,
    PRIMARY KEY (machine_id, source_path)
);

-- Primary.
CREATE TABLE llm_analyses (
    analysis_id    INTEGER PRIMARY KEY,
    session_id     TEXT NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    machine_id     TEXT NOT NULL,
    analysis_type  TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model          TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    score          REAL,
    result         TEXT NOT NULL,
    UNIQUE (session_id, analysis_type, prompt_version)
);

-- Cached dashboard-level coaching advice, keyed by the filter it was built for.
-- Primary.
CREATE TABLE coach_reports (
    coach_id       INTEGER PRIMARY KEY,
    filter_hash    TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model          TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    result         TEXT NOT NULL,
    UNIQUE (filter_hash, prompt_version)
);
