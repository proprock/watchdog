"""WD-142: the `rules` command."""

import builtins
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import tomli_w

from agent_watchdog.cli import main
from agent_watchdog.config import load_config
from agent_watchdog.events import Envelope
from agent_watchdog.rules import manage
from agent_watchdog.storage import Store

BODY = {
    "schema_version": 1,
    "name": "watch_prompts",
    "version": "1",
    "origin": "user",
    "provider": ["claude"],
    "event": "UserPromptSubmit",
    "action": {"kind": "context", "message": "Watchdog: noted."},
}


@pytest.fixture
def home(tmp_path):
    return tmp_path / "state"


def run(monkeypatch, capsys, home, *args):
    monkeypatch.setattr(sys, "argv", ["agent-watchdog", "--home", str(home), *args])
    code = main()
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def register(monkeypatch, capsys, home):
    """Register a project so the configuration file holds more than the rules."""
    root = home.parent / "source"
    root.mkdir(exist_ok=True)
    run(monkeypatch, capsys, home, "project", "add", str(root))
    return root


def write_rule(home, name="watch_prompts", **overrides):
    path = home / "rules" / f"{name}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps({**BODY, "name": name, **overrides}), encoding="utf-8")
    return path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def statuses(monkeypatch, capsys, home):
    _, listed, _ = run(monkeypatch, capsys, home, "rules", "list")
    return {item["name"]: item["status"] for item in listed["rules"]}


def test_list_shows_builtins_and_user_rules_with_their_status(monkeypatch, capsys, home):
    write_rule(home)

    code, listed, _ = run(monkeypatch, capsys, home, "rules", "list")

    assert code == 0
    by_name = {item["name"]: item for item in listed["rules"]}
    assert by_name["destructive_command"]["status"] == "approved"
    assert by_name["destructive_command"]["source"] == "builtin"
    assert by_name["destructive_command"]["action"] == "ask"
    assert by_name["stop_without_verification"]["status"] == "disabled"
    assert by_name["watch_prompts"]["status"] == "proposed"
    assert by_name["watch_prompts"]["event"] == "UserPromptSubmit"


def test_show_returns_the_body_and_the_hash_that_approval_would_record(monkeypatch, capsys, home):
    path = write_rule(home)

    code, shown, _ = run(monkeypatch, capsys, home, "rules", "show", "watch_prompts")

    assert code == 0
    assert shown["digest"] == digest(path)
    assert 'kind = "context"' in shown["text"]
    assert shown["status"] == "proposed"


def test_show_reports_why_a_rule_is_invalid(monkeypatch, capsys, home):
    write_rule(home, state={"made_up_predicate": True})

    code, shown, _ = run(monkeypatch, capsys, home, "rules", "show", "watch_prompts")

    assert code == 0 and shown["status"] == "invalid"
    assert "made_up_predicate" in shown["error"]


def test_approve_records_the_hash_and_keeps_the_rest_of_the_configuration(
    monkeypatch, capsys, home
):
    register(monkeypatch, capsys, home)
    path = write_rule(home)

    code, result, err = run(monkeypatch, capsys, home, "rules", "approve", "watch_prompts", "--yes")

    assert code == 0 and result["status"] == "approved"
    config = load_config(home / "config.toml")
    assert config.rules.approved == {"watch_prompts": digest(path)}
    assert len(config.projects) == 1
    # The reviewed body goes to stderr so stdout stays one JSON document.
    assert 'kind = "context"' in err and digest(path) in err
    assert statuses(monkeypatch, capsys, home)["watch_prompts"] == "approved"


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"state": {"made_up_predicate": True}}, "made_up_predicate"),
        ({"match": {"input_regex": "(a+)+"}}, "quantifier"),
        ({"event": "Stop"}, "cannot be delivered"),
    ],
)
def test_approve_refuses_an_invalid_rule_and_writes_nothing(
    monkeypatch, capsys, home, overrides, fragment
):
    register(monkeypatch, capsys, home)
    write_rule(home, **overrides)
    before = (home / "config.toml").read_bytes()

    code, result, _ = run(monkeypatch, capsys, home, "rules", "approve", "watch_prompts", "--yes")

    assert code == 1 and fragment in result["message"]
    assert (home / "config.toml").read_bytes() == before


def test_approve_refuses_builtins_and_unknown_names(monkeypatch, capsys, home):
    for name in ("destructive_command", "no_such_rule"):
        code, result, _ = run(monkeypatch, capsys, home, "rules", "approve", name, "--yes")
        assert code == 1 and result["error"] == "RulesError"
    assert not (home / "config.toml").exists()


def test_approve_asks_for_a_yes_unless_told_not_to(monkeypatch, capsys, home):
    register(monkeypatch, capsys, home)
    write_rule(home)
    for answer in ("", "y", "no"):
        monkeypatch.setattr(builtins, "input", lambda _prompt, answer=answer: answer)
        code, result, _ = run(monkeypatch, capsys, home, "rules", "approve", "watch_prompts")
        assert code == 1 and "not confirmed" in result["message"]
        assert load_config(home / "config.toml").rules.approved == {}

    monkeypatch.setattr(builtins, "input", lambda _prompt: "yes")
    code, result, _ = run(monkeypatch, capsys, home, "rules", "approve", "watch_prompts")

    assert code == 0 and result["status"] == "approved"


def test_approve_without_a_terminal_and_without_yes_fails_closed(monkeypatch, capsys, home):
    register(monkeypatch, capsys, home)
    write_rule(home)

    def no_terminal(_prompt):
        raise EOFError

    monkeypatch.setattr(builtins, "input", no_terminal)
    code, result, _ = run(monkeypatch, capsys, home, "rules", "approve", "watch_prompts")

    assert code == 1 and load_config(home / "config.toml").rules.approved == {}


def test_a_changed_file_is_stale_until_it_is_approved_again(monkeypatch, capsys, home):
    path = write_rule(home)
    run(monkeypatch, capsys, home, "rules", "approve", "watch_prompts", "--yes")
    write_rule(home, action={"kind": "context", "message": "Watchdog: different now."})

    assert statuses(monkeypatch, capsys, home)["watch_prompts"] == "stale"

    run(monkeypatch, capsys, home, "rules", "approve", "watch_prompts", "--yes")
    assert statuses(monkeypatch, capsys, home)["watch_prompts"] == "approved"
    assert load_config(home / "config.toml").rules.approved["watch_prompts"] == digest(path)


def test_the_file_changing_after_review_is_not_approved(tmp_path, home):
    from agent_watchdog.config import UserPaths

    paths = UserPaths(home / "config.toml", home / "data", home / "runtime")
    path = write_rule(home)
    reviewed = digest(path)
    write_rule(home, action={"kind": "context", "message": "Watchdog: swapped after review."})

    with pytest.raises(manage.RulesError, match="changed"):
        manage.approve(paths, "watch_prompts", reviewed)

    assert not paths.config.exists() or load_config(paths.config).rules.approved == {}


def test_reject_revokes_the_approval_and_keeps_the_rule_off(monkeypatch, capsys, home):
    write_rule(home)
    run(monkeypatch, capsys, home, "rules", "approve", "watch_prompts", "--yes")

    code, result, _ = run(monkeypatch, capsys, home, "rules", "reject", "watch_prompts")

    assert code == 0 and result["status"] == "disabled"
    config = load_config(home / "config.toml")
    assert config.rules.approved == {} and config.rules.disabled == ["watch_prompts"]


def test_enable_never_approves(monkeypatch, capsys, home):
    write_rule(home)
    run(monkeypatch, capsys, home, "rules", "disable", "watch_prompts")

    code, result, _ = run(monkeypatch, capsys, home, "rules", "enable", "watch_prompts")

    assert code == 0 and result["status"] == "proposed"


def test_builtins_can_be_disabled_and_the_default_off_one_enabled(monkeypatch, capsys, home):
    run(monkeypatch, capsys, home, "rules", "disable", "destructive_command")
    run(monkeypatch, capsys, home, "rules", "enable", "stop_without_verification")

    current = statuses(monkeypatch, capsys, home)

    assert current["destructive_command"] == "disabled"
    assert current["stop_without_verification"] == "approved"

    run(monkeypatch, capsys, home, "rules", "enable", "destructive_command")
    run(monkeypatch, capsys, home, "rules", "disable", "stop_without_verification")
    current = statuses(monkeypatch, capsys, home)
    assert current["destructive_command"] == "approved"
    assert current["stop_without_verification"] == "disabled"
    config = load_config(home / "config.toml")
    assert config.rules.enabled == [] and "stop_without_verification" in config.rules.disabled


def test_stats_counts_firings_verdicts_and_precision(monkeypatch, capsys, home):
    register(monkeypatch, capsys, home)
    project = load_config(home / "config.toml").projects[0]
    start = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    fingerprints = []
    with Store(home / "data" / "projects" / str(project.id), project.id) as store:
        for number, (rule, action) in enumerate(
            [("destructive_command", "ask")] * 3 + [("repeat_same_input_same_output", "context")]
        ):
            event = Envelope(
                provider="claude",
                project_id=project.id,
                session_id="s1",
                kind="control",
                source="daemon",
                received_at=start + timedelta(seconds=number),
                payload={
                    "claude": {
                        "rule": rule,
                        "rule_version": "1",
                        "action": action,
                        "reason": "r",
                        "text": "r",
                        "evidence_ids": [str(uuid4())],
                    }
                },
            )
            store.put(event)
        from agent_watchdog.analysis import control_findings

        rows = [
            Envelope.model_validate_json(row[0])
            for row in store.connection.execute("SELECT envelope FROM events")
        ]
        fingerprints = [
            item["fingerprint"]
            for item in control_findings(rows)
            if item["rule"] == "destructive_command"
        ]
        for fingerprint, verdict in zip(
            fingerprints, ("true_positive", "true_positive", "false_positive"), strict=True
        ):
            store.finding_verdict(
                "claude",
                "s1",
                rule="destructive_command",
                rule_version="1",
                fingerprint=fingerprint,
                verdict=verdict,
                note=None,
            )

    code, stats, _ = run(monkeypatch, capsys, home, "rules", "stats")

    assert code == 0
    by_rule = {item["rule"]: item for item in stats["rules"]}
    destructive = by_rule["destructive_command"]
    assert destructive["fired"] == 3 and destructive["actions"] == {"ask": 3}
    assert (destructive["true_positive"], destructive["false_positive"]) == (2, 1)
    assert destructive["precision"] == pytest.approx(2 / 3, abs=1e-3)
    repeat = by_rule["repeat_same_input_same_output"]
    assert repeat["fired"] == 1 and repeat["precision"] is None and repeat["reviewed"] == 0


def test_stats_since_excludes_older_firings(monkeypatch, capsys, home):
    register(monkeypatch, capsys, home)
    project = load_config(home / "config.toml").projects[0]
    with Store(home / "data" / "projects" / str(project.id), project.id) as store:
        store.put(
            Envelope(
                provider="claude",
                project_id=project.id,
                session_id="s1",
                kind="control",
                source="daemon",
                received_at=datetime(2026, 1, 1, tzinfo=UTC),
                payload={
                    "claude": {"rule": "destructive_command", "rule_version": "1", "action": "ask"}
                },
            )
        )

    _, everything, _ = run(monkeypatch, capsys, home, "rules", "stats")
    _, recent, _ = run(
        monkeypatch, capsys, home, "rules", "stats", "--since", "2026-06-01T00:00:00+00:00"
    )

    assert everything["rules"][0]["fired"] == 1
    assert recent["rules"] == []


def test_stats_without_collected_data_is_empty_not_an_error(monkeypatch, capsys, home):
    code, stats, _ = run(monkeypatch, capsys, home, "rules", "stats")

    assert code == 0 and stats["rules"] == []
