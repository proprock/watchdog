from pathlib import Path

import pytest
from wdi import config


@pytest.fixture(autouse=True)
def isolate_default_watchdog_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "default_data_dir", lambda: tmp_path / "unconfigured-watchdog")
