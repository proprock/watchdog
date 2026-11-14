import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "tdd_audit", Path(__file__).resolve().parents[1] / "scripts/kaboost_audit_tdd.py"
)
assert spec and spec.loader
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
check_structure, pair_up = audit.check_structure, audit.pair_up


PATTERN = r"(^|/)(tests?|spec)(/|_)|(_test|\.test|\.spec)\.|(^|/)test_"


def commit(kind, files, task="WD-233", expected="tests/test_example.py::test_case"):
    return {"sha": kind * 8, "kind": kind, "files": files, "task": task, "expected": expected}


def test_test_infrastructure_green_commit_is_accepted():
    red = commit("red", ["tests/test_example.py"])
    green = commit("green", ["tests/conftest.py"])
    pairs, problems = pair_up([red, green])
    assert problems == []
    assert check_structure(pairs, PATTERN) == []


def test_empty_green_commit_is_rejected():
    red = commit("red", ["tests/test_example.py"])
    green = commit("green", [])
    assert check_structure([(red, green)], PATTERN)


def test_unpaired_green_commit_is_rejected():
    pairs, problems = pair_up([commit("green", ["tests/conftest.py"])])
    assert pairs == []
    assert problems


def test_red_commit_with_non_test_file_is_rejected():
    red = commit("red", ["src/example.py"])
    green = commit("green", ["tests/conftest.py"])
    assert check_structure([(red, green)], PATTERN)
