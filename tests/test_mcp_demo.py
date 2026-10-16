"""The user-facing MCP demo tests real restart, isolation and failure cleanup."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from click.testing import CliRunner

from selvedge import mcp_demo
from selvedge.cli import cli


def _local_temp(monkeypatch, root: Path) -> None:
    root.mkdir()
    monkeypatch.setattr(
        mcp_demo, "TemporaryDirectory", lambda **kw: TemporaryDirectory(dir=root, **kw),
    )


def test_real_mcp_demo_restarts_and_preserves_ambient_state(tmp_path, monkeypatch):
    """Two distinct real servers return the same ID without touching project state."""
    real_db = tmp_path / "real.db"
    real_db.write_bytes(b"real database must not be opened")
    monkeypatch.setenv("SELVEDGE_DB", str(real_db))
    monkeypatch.setenv("SELVEDGE_TELEMETRY", "1")
    monkeypatch.setenv("SELVEDGE_REASONING_BYTES", "1")
    monkeypatch.chdir(tmp_path)
    disposable = tmp_path / "disposable"
    _local_temp(monkeypatch, disposable)
    launches = tmp_path / "launches.jsonl"
    original = mcp_demo._server_parameters

    def recording_parameters(directory):
        params = original(directory)
        # Record actual process identities and suppression values before
        # invoking the real server, not an in-process tool stand-in.
        params.args = ["-c", (
            "import json, os\n"
            f"with open({str(launches)!r}, 'a') as stream:\n"
            " stream.write(json.dumps({'pid':os.getpid(),"
            "'no_telemetry':os.environ['SELVEDGE_NO_TELEMETRY'],"
            "'telemetry':os.environ['SELVEDGE_TELEMETRY'],"
            "'no_update':os.environ['SELVEDGE_NO_UPDATE_CHECK']})+'\\n')\n"
            "from selvedge.server import main\nmain()\n"
        )]
        return params

    monkeypatch.setattr(mcp_demo, "_server_parameters", recording_parameters)
    result = CliRunner().invoke(cli, ["demo", "--mcp", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["status"] == "ok"
    assert data["event_id"] == data["retrieved_event_id"]
    assert data["event_id"]
    assert data["outcome"] == "rejected"
    assert data["confidence"] == "exact"
    assert data["reasoning"] == mcp_demo._REASON
    assert data["client_activation"] == "unverified"
    assert data["transport"] == "stdio"
    assert data["command"][0] == sys.executable
    assert data["package_version"] and data["protocol_version"]
    records = [json.loads(line) for line in launches.read_text().splitlines()]
    assert len(records) == 2
    assert records[0]["pid"] != records[1]["pid"]
    assert all(row["no_telemetry"] == row["no_update"] == "1" for row in records)
    assert all(row["telemetry"] == "0" for row in records)
    assert real_db.read_bytes() == b"real database must not be opened"
    assert os.environ["SELVEDGE_DB"] == str(real_db)
    assert os.environ["SELVEDGE_TELEMETRY"] == "1"
    assert list(disposable.iterdir()) == []
    assert set(tmp_path.iterdir()) == {real_db, disposable, launches}


@pytest.mark.parametrize("use_mcp", [False, True])
def test_demo_skips_parent_network_and_global_cache_work(monkeypatch, use_mcp):
    """Even an opted-in caller must not start background work for a demo."""
    from selvedge import cli as cli_module

    def forbidden(*args, **kwargs):
        pytest.fail("demo started parent network/cache work")

    monkeypatch.setattr(cli_module.update_check_mod, "check_for_update_async", forbidden)
    monkeypatch.setattr(cli_module.update_check_mod, "register_atexit_notice", forbidden)
    monkeypatch.setattr(cli_module.telemetry_mod, "ping_async", forbidden)
    monkeypatch.setenv("SELVEDGE_TELEMETRY", "1")
    monkeypatch.setattr(mcp_demo, "run_mcp_demo", lambda: {"status": "ok"})
    result = CliRunner().invoke(cli, ["demo", "--json", *(["--mcp"] if use_mcp else [])])
    assert result.exit_code == 0, result.output
    assert isinstance(json.loads(result.stdout), dict)


@pytest.mark.parametrize("as_json", [False, True])
def test_missing_server_fails_actionably_and_cleans_up(tmp_path, monkeypatch, as_json):
    disposable = tmp_path / "disposable"
    _local_temp(monkeypatch, disposable)
    original = mcp_demo._server_parameters

    def missing_parameters(directory):
        params = original(directory)
        params.command = str(tmp_path / "missing-python")
        return params

    monkeypatch.setattr(mcp_demo, "_server_parameters", missing_parameters)
    result = CliRunner().invoke(cli, ["demo", "--mcp", *(["--json"] if as_json else [])])
    assert result.exit_code == 1
    if as_json:
        error = json.loads(result.stdout)
        assert error["status"] == "error"
        assert error["transport"] == "stdio"
        message = error["error"]
    else:
        message = result.stderr
    assert "Could not launch" in message
    assert "pip check" in message
    assert list(disposable.iterdir()) == []


def test_hung_server_times_out_is_terminated_and_cleans_up(tmp_path, monkeypatch):
    disposable = tmp_path / "disposable"
    _local_temp(monkeypatch, disposable)
    pid_file = tmp_path / "pid"
    original = mcp_demo._server_parameters

    def hanging_parameters(directory):
        params = original(directory)
        params.args = ["-c", (
            "import os, time\n"
            f"with open({str(pid_file)!r}, 'w') as f: f.write(str(os.getpid()))\n"
            "time.sleep(60)\n"
        )]
        return params

    monkeypatch.setattr(mcp_demo, "_server_parameters", hanging_parameters)
    monkeypatch.setattr(mcp_demo, "_TIMEOUT_SECONDS", 0.3)
    start = time.monotonic()
    result = CliRunner().invoke(cli, ["demo", "--mcp", "--json"])
    assert time.monotonic() - start < 10
    assert result.exit_code == 1
    assert "exceeded" in json.loads(result.stdout)["error"]
    assert list(disposable.iterdir()) == []
    assert pid_file.is_file(), "the real subprocess must have started"
    if os.name == "posix":
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)


@pytest.mark.parametrize("wrong", ["id", "reasoning"])
def test_mcp_demo_refuses_mismatched_retrieval(tmp_path, monkeypatch, wrong):
    """A successful protocol response alone is not proof of faithful persistence."""
    _local_temp(monkeypatch, tmp_path / "disposable")

    async def mismatch(params, errlog, tool, arguments):
        if tool == "log_change":
            return {"status": "logged", "id": "saved-id"}, {}
        row = {
            "id": "saved-id", "entity_path": mcp_demo._ENTITY, "reasoning": mcp_demo._REASON,
            "outcome": "rejected", "confidence": "exact", "stale_when": mcp_demo._CONDITION,
        }
        row[wrong] = "mismatch"
        return [row], {}

    monkeypatch.setattr(mcp_demo, "_call", mismatch)
    result = CliRunner().invoke(cli, ["demo", "--mcp", "--json"])
    assert result.exit_code == 1
    assert "did not match" in json.loads(result.stdout)["error"]
    assert list((tmp_path / "disposable").iterdir()) == []
