"""Comparison preserves unknowns and validates selection boundaries."""

import pytest


def test_summary_excludes_unknown_values_and_marks_small_samples():
    from wdi.metrics.compare import summarize

    result = summarize([1, None, 3, 5, 7])
    assert result == {"count": 4, "unknown": 1, "median": 4, "mean": 4, "p90": 7}


@pytest.mark.parametrize(
    "value", ["[]", '{"provider":"invalid"}', '{"since":"bad"}', '{"other":1}']
)
def test_invalid_filter_is_rejected(value):
    from wdi.metrics.compare import parse_filter

    with pytest.raises(ValueError):
        parse_filter(value)


@pytest.fixture
def observed_session(tmp_path):
    from types import SimpleNamespace

    from wdi.db.connection import connect

    conn = connect(tmp_path / "insights.db")
    conn.execute("INSERT INTO machines(machine_id, created_at) VALUES ('m', '2026-01-01')")
    conn.execute("INSERT INTO projects(project_id, canonical_name) VALUES (1, 'project-a')")
    conn.execute("""INSERT INTO sessions(session_id,machine_id,project_id,agent,model,started_at,
        duration_ms,user_msg_count,input_tokens,cache_read_tokens,cache_creation_tokens,
        output_tokens,peak_context_tokens,compaction_count)
        VALUES ('s','m',1,'claude','model','2026-01-02',100,2,10,3,4,5,20,1)""")
    conn.execute("""INSERT INTO messages(session_id,machine_id,idx,role,input_tokens,
        cache_read_tokens,cache_creation_tokens,output_tokens)
        VALUES ('s','m',1,'assistant',10,3,4,5)""")
    conn.execute("UPDATE messages SET model='model', ts='2026-01-02T12:00:00Z'")
    conn.execute(
        "INSERT INTO tool_calls(session_id,machine_id,idx,tool_name,is_error) "
        "VALUES ('s','m',1,'test',1)"
    )
    conn.execute("UPDATE tool_calls SET output_bytes=0")
    conn.execute(
        "INSERT INTO subagent_runs(agent_run_id,session_id,machine_id) VALUES ('run','s','m')"
    )
    conn.execute("""INSERT INTO session_env(machine_id,project_id,store_path,provider,session_id,
        captured_at,checkout_id,harness,files,watchdog) VALUES ('m','p','path','claude','s',
        '2026-01-02','checkout','{}','[]','{}')""")
    conn.execute("""INSERT INTO checkout_states(machine_id,project_id,store_path,checkout_id,
        observed_at,provider,session_id,trigger,base_head,head,files_changed,insertions,deletions)
        VALUES ('m','p','path','checkout','2026-01-02','claude','s','end','old','new',2,3,4)""")
    settings = SimpleNamespace(watchdog=SimpleNamespace(data_dirs=[tmp_path / "missing"]))
    pricing = SimpleNamespace(cost=lambda *args, **kwargs: 0.25)
    yield conn, pricing, settings
    conn.close()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("user_turns", 2),
        ("wall_time_ms", 100),
        ("input_tokens", 10),
        ("cached_input_tokens", 3),
        ("cache_write_tokens", 4),
        ("output_tokens", 5),
        ("estimated_cost", 0.25),
        ("peak_context_tokens", 20),
        ("compactions", 1),
        ("tool_calls", 1),
        ("tool_errors", 1),
        ("subagent_runs", 1),
        ("files_changed", 2),
        ("insertions", 3),
        ("deletions", 4),
        ("head_moved", 1),
    ],
)
def test_observed_session_metrics(observed_session, name, expected):
    from wdi.metrics.compare import compare

    conn, pricing, settings = observed_session
    result = compare(conn, pricing, {}, {}, settings)
    metric = next(item for item in result["metrics"] if item["name"] == name)
    assert metric["a"]["median"] == expected
    assert metric["difference"] == 0
    assert result["a_small"]


def test_unknown_and_selection_boundaries(observed_session):
    from wdi.metrics.compare import compare, parse_filter

    conn, pricing, settings = observed_session
    conn.execute("UPDATE messages SET input_tokens=NULL")
    result = compare(
        conn,
        pricing,
        parse_filter('{"project":"project-a","since":"2026-01-02","until":"2026-01-02"}'),
        parse_filter('{"session_ids":[]}'),
        settings,
    )
    metric = next(item for item in result["metrics"] if item["name"] == "input_tokens")
    assert (result["a_count"], result["b_count"]) == (1, 0)
    assert (metric["a"]["count"], metric["a"]["unknown"], metric["a"]["median"]) == (0, 1, None)


def test_permission_wait_uses_completed_hook_observations(observed_session):
    from wdi.metrics.compare import compare

    conn, pricing, settings = observed_session
    for index, kind, timestamp, notification in [
        (1, "waiting", 1000000, "permission_prompt"),
        (2, "tool.start", 4000000, None),
    ]:
        conn.execute(
            "INSERT INTO hook_events VALUES ('m','p',?,?, '2026-01-02','s',?, '{}')",
            (index, f"e{index}", kind),
        )
        conn.execute(
            """INSERT INTO hook_facts(machine_id,project_id,event_rowid,event_id,provider,
            kind,received_at_us,session_id,notification_type) VALUES ('m','p',?,?,'claude',?,?,
            's',?)""",
            (index, f"e{index}", kind, timestamp, notification),
        )
    metric = next(
        m
        for m in compare(conn, pricing, {}, {}, settings)["metrics"]
        if m["name"] == "permission_wait_ms"
    )
    assert metric["a"]["median"] == 3000


def test_label_selection_and_categorical_counts(observed_session, monkeypatch):
    from wdi.metrics import compare as module

    conn, pricing, settings = observed_session
    monkeypatch.setattr(
        module, "_labels", lambda settings, rows: {("m", "p", "claude", "s"): "success"}
    )
    result = module.compare(conn, pricing, {"label": "success"}, {"label": "fail"}, settings)
    metric = next(m for m in result["metrics"] if m["name"] == "outcome_label")
    assert (result["a_count"], result["b_count"]) == (1, 0)
    assert metric["a"]["categories"] == {"success": 1}
    assert metric["difference"] is None


def test_manual_labels_are_read_from_the_matching_watchdog_store(
    observed_session, monkeypatch, tmp_path
):
    import sqlite3
    from contextlib import contextmanager
    from types import SimpleNamespace

    from wdi.metrics import compare as module

    conn, pricing, settings = observed_session
    store = tmp_path / "store"
    store.mkdir()
    (store / "events.sqlite3").touch()
    info = SimpleNamespace(path=store, machine_id="m", project_id="p")
    source = sqlite3.connect(":memory:")
    source.execute(
        "CREATE TABLE session_labels (provider TEXT, session_id TEXT, "
        "task_outcome TEXT, task_type TEXT)"
    )
    source.execute("INSERT INTO session_labels VALUES ('claude', 's', 'success', NULL)")

    @contextmanager
    def open_events(_info):
        yield source

    monkeypatch.setattr(module.readapi, "stores", lambda _directory: [info])
    monkeypatch.setattr(module.readapi, "open_events", open_events)
    try:
        result = module.compare(conn, pricing, {"label": "success"}, {}, settings)
    finally:
        source.close()
    assert result["a_count"] == 1
    outcome = next(m for m in result["metrics"] if m["name"] == "outcome_label")
    assert outcome["a"]["categories"] == {"success": 1}


def test_five_sessions_are_not_marked_small(observed_session):
    from wdi.metrics.compare import compare

    conn, pricing, settings = observed_session
    for index in range(4):
        conn.execute(
            "INSERT INTO sessions(session_id,machine_id,agent) VALUES (?, 'm', 'codex')",
            (f"x{index}",),
        )
    result = compare(conn, pricing, {}, {}, settings)
    assert result["a_count"] == 5
    assert result["a_small"] is False


def test_codex_cost_prices_inclusive_input_without_cache_write(observed_session):
    from wdi.metrics.compare import compare

    conn, _, settings = observed_session
    conn.execute("UPDATE sessions SET agent='codex', cache_creation_tokens=NULL")
    conn.execute("UPDATE messages SET cache_creation_tokens=NULL")
    calls = []

    class Pricing:
        def cost(self, model, **kwargs):
            calls.append(kwargs)
            return 0.5

    result = compare(conn, Pricing(), {}, {}, settings)
    metric = next(m for m in result["metrics"] if m["name"] == "estimated_cost")
    assert metric["a"]["median"] == 0.5
    assert calls[0]["input_is_net"] is False


def test_cost_is_unknown_if_usage_has_multiple_models(observed_session):
    from wdi.metrics.compare import compare

    conn, pricing, settings = observed_session
    conn.execute("""INSERT INTO messages(session_id,machine_id,idx,role,model,ts,input_tokens,
                 cache_read_tokens,cache_creation_tokens,output_tokens)
                 VALUES ('s','m',2,'assistant','other','2026-01-02T13:00:00Z',1,1,1,1)""")
    result = compare(conn, pricing, {}, {}, settings)
    metric = next(m for m in result["metrics"] if m["name"] == "estimated_cost")
    assert metric["a"]["unknown"] == 1


def test_tool_errors_are_unknown_when_a_call_has_no_observed_result(observed_session):
    from wdi.metrics.compare import compare

    conn, pricing, settings = observed_session
    conn.execute("UPDATE tool_calls SET is_error=0, output_bytes=NULL, output_preview=NULL")
    result = compare(conn, pricing, {}, {}, settings)
    metric = next(m for m in result["metrics"] if m["name"] == "tool_errors")
    assert metric["a"]["unknown"] == 1
