import json
import os

import pytest

from agent_watchdog.config import Config, UserPaths
from agent_watchdog.daemon import _Channel, _policy_server
from agent_watchdog.files import open_shared_read

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows file sharing")


@pytest.fixture
def paths(tmp_path):
    return UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")


def test_policy_start_replaces_discovery_while_shared_reader_is_open(paths):
    path = paths.data / "policy" / "socket.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"old": true}', encoding="utf-8")
    with open_shared_read(path) as old_reader, _policy_server(paths) as channel:
        assert channel._document is not None
        assert json.load(old_reader) == {"old": True}
        assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 2


def test_policy_sync_replaces_discovery_while_shared_reader_is_open(paths, monkeypatch):
    import agent_watchdog.daemon as daemon

    path = paths.data / "policy" / "socket.json"
    path.parent.mkdir(parents=True)
    old: dict[str, object] = {"subscriptions": [], "max_request_bytes": 1}
    path.write_text(json.dumps(old), encoding="utf-8")
    new_subscriptions = [{"provider": "claude", "hook_event_name": "Stop", "tool_name": None}]
    monkeypatch.setattr(daemon, "subscriptions", lambda *_: new_subscriptions)
    channel = _Channel(paths, old)
    with open_shared_read(path) as old_reader:
        channel.sync(Config())
        assert json.load(old_reader) == old
        assert json.loads(path.read_text(encoding="utf-8"))["subscriptions"] == new_subscriptions
