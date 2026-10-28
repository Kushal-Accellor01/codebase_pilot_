import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("dev", Path(__file__).resolve().parents[2] / "scripts" / "dev.py")
dev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dev)


@pytest.mark.parametrize("command", dev.COMMANDS)
def test_every_command_has_a_plan(command):
    steps = dev.plan(command, {})
    assert steps and all(argv and cwd.exists() for argv, cwd, _ in steps)


def test_seed_demo_allows_the_mirror_for_execution_unless_set():
    (_, _, extra), (argv, _, _) = dev.plan("seed-demo", {})
    assert extra["EXEC_ALLOWED_REPOS"] == str(dev.mirror_path()) and "pipeline" in argv
    assert dev.plan("seed-demo", {"EXEC_ALLOWED_REPOS": "acme/shop"})[0][2]["EXEC_ALLOWED_REPOS"] == "acme/shop"


def test_dry_run_prints_and_runs_nothing(capsys):
    assert dev.main(["reset-demo", "--dry-run"]) == 0
    assert "reset-demo" in capsys.readouterr().out
