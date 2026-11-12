import json
from dataclasses import asdict

from wdi.context_analysis import reconstruct_snapshots
from wdi.ingest.claude_code import parse_claude_lines
from wdi.ingest.codex import parse_codex_lines
from wdi.ingest.context import json_text, tool_output_category


def test_codex_usage_notifications_are_not_calls_or_extra_input():
    usage = {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10}
    line = json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": usage, "total_token_usage": usage},
            },
        }
    ).encode()
    chunk = parse_codex_lines([line, line], msg_idx0=0, tool_idx0=0)
    snapshots = reconstruct_snapshots([asdict(e) for e in chunk.context_events])
    assert len(snapshots) == 1
    assert snapshots[0].context_tokens == 100
    assert snapshots[0].input_uncached == 20
    assert sum(m.input_tokens or 0 for m in chunk.messages) == 100


def test_claude_fragments_share_usage_and_user_turn():
    records = [{"type": "user", "uuid": "u1", "message": {"content": "Work"}}]
    for text, output in [("Thinking", 2), ("Done", 8)]:
        records.append(
            {
                "type": "assistant",
                "message": {
                    "id": "m1",
                    "model": "claude-test",
                    "content": [{"type": "text", "text": text}],
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 90,
                        "output_tokens": output,
                    },
                },
            }
        )
    chunk = parse_claude_lines([json.dumps(r).encode() for r in records], msg_idx0=0, tool_idx0=0)
    snapshots = reconstruct_snapshots([asdict(e) for e in chunk.context_events])
    assert len(snapshots) == 1
    assert snapshots[0].context_tokens == 100
    assert snapshots[0].output_tokens == 8
    assert snapshots[0].turn_id == "u1"
    assert sum(m.input_tokens or 0 for m in chunk.messages) == 10


def test_same_target_with_changed_content_is_not_exact_repeat():
    rows = [
        dict(
            idx=i,
            event_type="tool_output",
            category="code_file",
            byte_count=10,
            fingerprint=str(i),
            target_fingerprint="same",
        )
        for i in range(2)
    ]
    rows.append(dict(idx=2, event_type="model_call", input_tokens=10))
    snapshot = reconstruct_snapshots(rows)[0]
    assert snapshot.repeated_bytes == 0
    assert snapshot.changed_target_bytes == 10


def test_namespaced_and_wrapped_tools():
    assert tool_output_category("mcp__codebase_memory_mcp__get_code_snippet") == "code_file"
    assert (
        tool_output_category("functions.exec", 'text(await tools.exec_command({cmd:"rg --files"}))')
        == "directory_listing"
    )
    assert (
        tool_output_category("functions.exec", "rg --files; Get-Content x.py")
        == "mixed_tool_output"
    )


def test_zero_usage_after_compaction_is_not_a_model_call():
    line = json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"input_tokens": 0, "output_tokens": 0},
                    "total_token_usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        }
    ).encode()
    chunk = parse_codex_lines([line], msg_idx0=0, tool_idx0=0)
    assert reconstruct_snapshots([asdict(e) for e in chunk.context_events]) == []


def test_image_transport_is_not_visible_text():
    short = json_text([{"type": "image", "data": "x" * 100}])
    large = json_text([{"type": "image", "data": "x" * 100000}])
    assert short == large
    assert "opaque media" in (large or "")
