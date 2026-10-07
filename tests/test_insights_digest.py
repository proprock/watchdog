"""Offline contracts for `insights digest`, the cross-mode synthesis (WD-134)."""

import json
from datetime import timedelta
from typing import Any

import pytest
from helpers.insights import runner_for, store
from test_insights_modes import NONE, _prompt, _start, call
from test_insights_session import _cli
from test_insights_sessions import _calm, _stuck
from test_storage_v6 import BASE

from agent_watchdog import insights
from agent_watchdog.insights import bundle, contract, digest, llm, render
from agent_watchdog.insights.bundle import Draft
from agent_watchdog.storage import StorageError

WINDOW = {"since": None, "until": None, "provider": None, "session_id": None}


def _events():
    """One stuck session whose repeated failure is an error cluster and a loop, plus a prompt."""
    return [
        *_calm(),
        *_stuck(),
        _start(400, "Bash", {"command": "uv run pytest -q"}, "t1"),
        _prompt(401, "Bash"),
        call(430, "uv run pytest -q", tool_use_id="t1", session="calm"),
    ]


def _digest(tmp_path):
    paths, project = store(tmp_path, _events())
    return paths, project, digest.build(paths, project, **NONE)


def _sub_drafts(count: int) -> Draft:
    """Synthetic ranked items per mode, each large enough for the budget to bite."""
    facts = {name: {"total": count} for name in digest.SHARES}
    coverage = {name: {"notes": ["a note"]} for name in digest.SHARES}
    items = [
        {"mode": name, "item_id": f"{name[0].upper()}{number}", "text": "x" * 400}
        for name in digest.SHARES
        for number in range(1, count + 1)
    ]
    return Draft(facts, coverage, items)


def test_the_shares_are_declared_for_every_mode_and_leave_headroom():
    assert set(digest.SHARES) == {
        "errors",
        "context",
        "tokens",
        "workflow",
        "subagents",
        "permissions",
    }
    assert 0 < sum(digest.SHARES.values()) < 1


def test_the_build_keeps_each_modes_facts_coverage_and_items(tmp_path):
    _paths, _project, draft = _digest(tmp_path)

    assert list(draft.facts) == list(digest.SHARES)
    assert list(draft.coverage) == list(digest.SHARES)
    by_mode: dict[str, list[dict[str, Any]]] = {}
    for item in draft.items:
        by_mode.setdefault(item["mode"], []).append(item)
    assert {"errors", "workflow", "permissions"} <= set(by_mode)
    # A mode that found nothing still reports its facts and coverage.
    assert "subagents" not in by_mode
    assert draft.facts["subagents"] and draft.coverage["subagents"]
    # An item id never repeats across modes, so one id names one mode.
    ids = [item.get("item_id") or item.get("cluster_id") for item in draft.items]
    assert len(ids) == len(set(ids))


def test_every_mode_is_built_for_one_window(tmp_path, monkeypatch):
    paths, project = store(tmp_path, _events())
    seen = {}
    for name, module in digest.MODULES.items():
        original = module.build

        def spy(*args, _name=name, _original=original, **kwargs):
            seen[_name] = kwargs
            return _original(*args, **kwargs)

        monkeypatch.setattr(module, "build", spy)

    digest.build(paths, project, provider="claude", session_id=None, since=BASE, until=None)

    assert set(seen) == set(digest.SHARES)
    assert len({kwargs["until"] for kwargs in seen.values()}) == 1
    assert all(kwargs["until"] is not None for kwargs in seen.values())
    assert all(kwargs["provider"] == "claude" for kwargs in seen.values())


def test_each_mode_stays_within_its_share_of_the_budget():
    max_tokens = 4000
    fitted = digest.fit(_sub_drafts(40), mode="digest", window=WINDOW, max_tokens=max_tokens)

    for name, share in digest.SHARES.items():
        sub = {
            "facts": fitted["facts"]["modes"][name],
            "coverage": fitted["coverage"]["modes"][name],
            "items": fitted["items"][name],
        }
        assert bundle.estimate_tokens(bundle.dumps(sub)) <= max_tokens * share
        assert 0 < len(fitted["items"][name]) < 40
        assert fitted["coverage"]["modes"][name]["truncated"]["items"] == 40 - len(
            fitted["items"][name]
        )
    assert bundle.estimate_tokens(bundle.dumps(fitted)) <= max_tokens
    # Ranked order survives inside each mode: a cut drops the tail.
    assert [item["item_id"] for item in fitted["items"]["errors"]][:2] == ["E1", "E2"]
    total = fitted["coverage"]["truncated"]["items"]
    assert total == sum(
        fitted["coverage"]["modes"][name]["truncated"]["items"] for name in digest.SHARES
    )


def test_a_mode_without_items_keeps_its_facts_and_coverage_and_frees_nothing():
    draft = _sub_drafts(40)
    draft = Draft(
        draft.facts, draft.coverage, [item for item in draft.items if item["mode"] != "permissions"]
    )

    fitted = digest.fit(draft, mode="digest", window=WINDOW, max_tokens=4000)
    full = digest.fit(_sub_drafts(40), mode="digest", window=WINDOW, max_tokens=4000)

    assert fitted["items"]["permissions"] == []
    assert fitted["facts"]["modes"]["permissions"] == {"total": 40}
    assert fitted["coverage"]["modes"]["permissions"]["truncated"] == {"items": 0, "bytes": 0}
    # The other modes keep exactly their own share; no redistribution.
    for name in digest.SHARES:
        if name != "permissions":
            assert fitted["items"][name] == full["items"][name]


def test_a_tiny_budget_still_keeps_facts_and_coverage_for_every_mode():
    fitted = digest.fit(_sub_drafts(5), mode="digest", window=WINDOW, max_tokens=1000)

    assert set(fitted["facts"]["modes"]) == set(digest.SHARES)
    assert set(fitted["coverage"]["modes"]) == set(digest.SHARES)
    assert fitted["facts"]["shares"] == digest.SHARES


def _fitted(items: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {"items": items}


def _links(fitted, *entries):
    known = bundle.evidence_ids(fitted)
    return digest.resolve(contract.ground(list(entries), known), fitted, "repo")


def _link(item_ids, evidence_ids):
    return {
        "title": "t",
        "explanation": "e",
        "item_ids": item_ids,
        "confidence": "high",
        "evidence_ids": evidence_ids,
    }


def test_a_link_citing_two_modes_is_cross_mode_and_one_mode_is_demoted():
    fitted = _fitted(
        {
            "errors": [{"cluster_id": "E1", "event_ids": ["ev1"]}, {"cluster_id": "E2"}],
            "workflow": [{"item_id": "W1", "evidence_ids": ["ev2"]}],
            "permissions": [],
        }
    )

    cross, single, bare = _links(
        fitted,
        _link(["E1", "W1"], ["ev1", "ev2"]),
        _link(["E1", "E2"], ["ev1"]),
        _link([], ["ev2"]),
    )

    assert (cross["modes"], cross["cross_mode"], cross["ungrounded"]) == (
        ["errors", "workflow"],
        True,
        False,
    )
    assert (single["modes"], single["cross_mode"], single["ungrounded"]) == (
        ["errors"],
        False,
        False,
    )
    assert (bare["modes"], bare["cross_mode"]) == ([], False)


def test_an_invented_id_or_one_from_a_truncated_tail_is_ungrounded():
    # E9 was cut from the bundle, so it is as unknown to the model's claim as an invented id.
    fitted = _fitted(
        {
            "errors": [{"cluster_id": "E1", "event_ids": ["ev1"]}],
            "workflow": [{"item_id": "W1", "evidence_ids": ["ev2"]}],
        }
    )

    invented, cut = _links(
        fitted,
        _link(["E1", "W1"], ["ev1", "ev-invented"]),
        _link(["E9", "W1"], ["ev2"]),
    )

    assert invented["ungrounded"] is True
    assert invented["unknown_evidence_ids"] == ["ev-invented"]
    assert cut["ungrounded"] is True
    assert cut["unknown_evidence_ids"] == ["E9"]
    # The unknown id supports no mode, so the link does not count as cross-mode by it.
    assert cut["modes"] == ["workflow"]
    assert cut["cross_mode"] is False


def test_an_event_shown_by_two_modes_grounds_a_link_but_names_no_mode():
    fitted = _fitted(
        {
            "errors": [{"cluster_id": "E1", "event_ids": ["shared"]}],
            "workflow": [{"item_id": "W1", "evidence_ids": ["shared"]}],
        }
    )

    (link,) = _links(fitted, _link([], ["shared"]))

    assert link["ungrounded"] is False
    assert link["modes"] == []
    assert link["cross_mode"] is False


def _answer(links, recommendations):
    return {
        "summary": "A failing test causes a retry loop.",
        "links": links,
        "recommendations": recommendations,
        "rule_candidates": [],
    }


def _recommendation(item_ids, evidence_ids):
    return {
        "title": "Fix the test environment",
        "item_ids": item_ids,
        "kind": "environment",
        "target": "claude",
        "recommendation": "Repair the failing command before retrying.",
        "draft": None,
        "confidence": "medium",
        "evidence_ids": evidence_ids,
    }


_run = runner_for("digest", since=BASE - timedelta(days=1))


def test_an_answer_is_grounded_across_modes_and_rendered(tmp_path):
    paths, project = store(tmp_path, _events())
    dry = _run(paths, project, lambda request: pytest.fail("no call"), dry_run=True)
    items = dry["bundle"]["items"]
    cluster = items["errors"][0]["cluster_id"]
    loop = items["workflow"][0]["item_id"]
    event_id = items["errors"][0]["event_ids"][0]
    output = tmp_path / "digest.md"
    answer = _answer(
        [_link([cluster, loop], [event_id]), _link([cluster], [event_id])],
        [_recommendation([cluster, loop], [event_id])],
    )

    result = _run(paths, project, lambda request: llm.Result(answer, None, {}), output=output)

    assert result["status"] == "ok"
    cross, single = result["links"]
    assert cross["cross_mode"] is True
    assert cross["modes"] == ["errors", "workflow"]
    assert single["modes"] == ["errors"]
    assert single["cross_mode"] is False
    (action,) = result["recommendations"]
    assert action["ungrounded"] is False
    text = output.read_text(encoding="utf-8")
    assert "## Cross-mode links" in text
    assert "single-mode" in text
    assert "errors, workflow" in text
    assert "Fix the test environment" in text


def test_a_request_carries_the_digest_prompt_and_schema(tmp_path):
    paths, project = store(tmp_path, _events())
    seen = []

    def runner(request):
        seen.append(request)
        return llm.Result(_answer([], []), None, {})

    result = _run(paths, project, runner)

    assert result["status"] == "ok"
    assert result["links"] == []
    (request,) = seen
    assert "Mode: digest" in request.system_prompt
    assert "at least two modes" in request.system_prompt
    assert "links" in request.schema["properties"]


def test_the_dry_run_sends_nothing_and_shows_every_mode(tmp_path):
    paths, project = store(tmp_path, _events())

    result = _run(paths, project, lambda request: pytest.fail("no model call"), dry_run=True)

    assert result["status"] == "dry_run"
    assert set(result["bundle"]["items"]) == set(digest.SHARES)
    assert set(result["facts"]["modes"]) == set(digest.SHARES)
    assert result["estimated_bundle_tokens"] <= result["budget"]["max_bundle_tokens"]


def test_the_mode_covers_the_project_not_one_session_or_all_projects(tmp_path):
    paths, project = store(tmp_path, _events())
    runner = lambda request: pytest.fail("no call for a refused scope")  # noqa: E731
    with pytest.raises(StorageError, match="whole project"):
        _run(paths, project, runner, provider="claude", session_id="calm")
    with pytest.raises(StorageError, match="--all-projects"):
        insights.run_all(
            paths,
            mode="digest",
            provider=None,
            since=None,
            until=None,
            model="sonnet",
            effort=None,
            timeout=60.0,
            max_bundle_tokens=None,
            language="English",
            dry_run=True,
            output=None,
            runner=runner,
        )


def test_the_cli_offers_the_mode(tmp_path, monkeypatch, capsys):
    paths, project = store(tmp_path, _events())
    since = (BASE - timedelta(days=1)).isoformat()

    code = _cli(
        monkeypatch,
        capsys,
        paths,
        *("insights", "digest", "--project", str(project.id)),
        *("--since", since, "--dry-run"),
    )

    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["mode"] == "digest"
    assert set(printed["bundle"]["items"]) == set(digest.SHARES)


def test_the_schema_is_self_contained_and_strict():
    schema = contract.json_schema(digest.Output)
    assert "$ref" not in json.dumps(schema)
    for field in ("links", "recommendations"):
        assert schema["properties"][field]["items"]["additionalProperties"] is False


def test_render_leaves_other_modes_without_the_links_section():
    text = render.markdown({"mode": "errors", "summary": "s", "recommendations": []})
    assert "Cross-mode links" not in text
