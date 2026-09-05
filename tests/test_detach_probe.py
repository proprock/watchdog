import json
import subprocess
import sys
from pathlib import Path


def test_detached_child_responds_after_parent_exit():
    script = Path(__file__).resolve().parents[1] / "scripts" / "detach_probe.py"
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=40
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["parent_exited_before_release"] is True
    assert evidence["child_responded"] is True
    assert evidence["child_finished"] is True
    assert evidence["parent_stdout_empty"] is True
