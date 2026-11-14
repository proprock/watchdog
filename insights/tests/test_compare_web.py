"""HTTP acceptance for cohort comparison."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from wdi.config import Settings
from wdi.web import create_app
from wdi.web.routes import compare as compare_route


def test_compare_initial_page_shows_filter_form(tmp_path: Path) -> None:
    client = TestClient(create_app(Settings(machine_id="machine", db_path=tmp_path / "db.sqlite")))
    response = client.get("/compare")
    assert response.status_code == 200
    assert 'name="a"' in response.text
    assert 'name="b"' in response.text
    assert "Compare cohorts" in response.text


def test_compare_invalid_filter_shows_error(tmp_path: Path) -> None:
    client = TestClient(create_app(Settings(machine_id="machine", db_path=tmp_path / "db.sqlite")))
    response = client.get("/compare", params={"a": "not json", "b": "{}"})
    assert response.status_code == 400
    assert "Invalid" in response.text
    assert "not json" in response.text


def test_compare_renders_empty_cohorts(tmp_path: Path) -> None:
    client = TestClient(create_app(Settings(machine_id="machine", db_path=tmp_path / "db.sqlite")))
    response = client.get("/compare", params={"a": "{}", "b": "{}"})
    assert response.status_code == 200
    assert "Cohort A" in response.text
    assert "Cohort B" in response.text
    assert "0 sessions" in response.text


def test_compare_displays_statistics_and_outcome_categories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    numeric = {"count": 2, "unknown": 1, "median": 4, "mean": 5, "p90": 6}
    categorical = {**numeric, "categories": {"success": 2}}
    monkeypatch.setattr(
        compare_route,
        "compare",
        lambda *args: {
            "a_count": 3,
            "b_count": 3,
            "a_small": True,
            "b_small": True,
            "metrics": [
                {"name": "cost", "a": numeric, "b": numeric, "difference": 0},
                {"name": "outcome_label", "a": categorical, "b": categorical, "difference": None},
            ],
        },
    )
    client = TestClient(create_app(Settings(machine_id="machine", db_path=tmp_path / "db.sqlite")))
    response = client.get("/compare", params={"a": "{}", "b": "{}"})
    assert response.status_code == 200
    assert "Median: 4" in response.text
    assert "unknown: 1" in response.text
    assert "success: 2" in response.text
    assert "small sample" in response.text
