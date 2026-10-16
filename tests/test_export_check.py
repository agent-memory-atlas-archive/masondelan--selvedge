"""Read-only Markdown drift checks for local hooks and CI."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from selvedge.cli import cli
from selvedge.exporters.markdown import render_markdown
from selvedge.models import ChangeEvent
from selvedge.storage import SelvedgeStorage


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "home" / ".selvedge" / "selvedge.db"
    monkeypatch.setenv("SELVEDGE_DB", str(path))
    storage = SelvedgeStorage(path)
    storage.log_event(ChangeEvent(
        entity_path="users.token", change_type="add", project="app",
        timestamp="2026-01-01T00:00:00Z", reasoning="Keep credentials short-lived.",
    ))
    return storage


def _check(target, *args):
    return CliRunner().invoke(cli, [
        "export", "--format", "markdown", "-o", str(target), "--check", *args,
    ])


def _background_spies(monkeypatch):
    calls = []
    for module, name in [
        ("update_check_mod", "check_for_update_async"),
        ("update_check_mod", "register_atexit_notice"),
        ("telemetry_mod", "ping_async"),
    ]:
        monkeypatch.setattr(f"selvedge.cli.{module}.{name}", lambda *a: calls.append(a))
    return calls


def test_current_digest_is_silent_and_preserves_store(store, tmp_path, monkeypatch):
    target = tmp_path / "DECISIONS.md"
    generated = CliRunner().invoke(cli, ["export", "--format", "markdown", "-o", str(target)])
    assert generated.exit_code == 0, generated.output
    content = target.read_bytes()
    database = store.db_path.read_bytes()
    calls = _background_spies(monkeypatch)
    result = _check(target)
    assert result.exit_code == 0, result.output
    assert result.output == ""
    assert target.read_bytes() == content
    assert store.db_path.read_bytes() == database
    assert calls == []


def test_changed_record_has_actionable_literal_diff(store, tmp_path):
    target = tmp_path / "[red]DECISIONS.md"
    target.write_text(render_markdown(store.get_history()), encoding="utf-8")
    before = target.read_bytes()
    store.log_event(ChangeEvent(
        entity_path="users.token", change_type="remove", reasoning="[red]Retired[/oops]",
        timestamp="2026-02-01T00:00:00Z",
    ))
    result = _check(target)
    assert result.exit_code == 1, result.output
    assert str(target) in result.output
    assert "Regenerate with the same export command without --check" in result.output
    assert "--- " in result.output and "+++ " in result.output and "@@ " in result.output
    assert "-## Active decisions" in result.output
    assert "+## Tried and reverted" in result.output
    assert "[red]Retired[/oops]" in result.output
    assert target.read_bytes() == before


def test_missing_digest_reports_full_diff_without_creating_parent(store, tmp_path):
    target = tmp_path / "missing" / "DECISIONS.md"
    result = _check(target)
    assert result.exit_code == 1, result.output
    assert "missing" in result.output and "--- /dev/null" in result.output
    assert "+# Selvedge decision log" in result.output
    assert not target.parent.exists()


@pytest.mark.parametrize("args", [
    [], ["--format", "json", "-o", "digest.json"],
    ["--format", "csv", "-o", "digest.csv"],
    ["--format", "agent-trace", "-o", "trace.json"],
    ["--format", "markdown"], ["--format", "markdown", "-o", "-"],
    ["--format", "markdown", "-o", ""],
])
def test_invalid_check_arguments_exit_two_without_initialization(args, tmp_path, monkeypatch):
    db = tmp_path / "absent" / "db.sqlite"
    monkeypatch.setenv("SELVEDGE_DB", str(db))
    calls = _background_spies(monkeypatch)
    result = CliRunner().invoke(cli, ["export", "--check", *args])
    assert result.exit_code == 2, result.output
    assert "--check requires --format markdown" in result.output
    assert not db.parent.exists()
    assert calls == []


@pytest.mark.parametrize("global_fallback", [False, True])
def test_missing_database_does_not_initialize(tmp_path, monkeypatch, global_fallback):
    root = tmp_path / "uninitialized"
    root.mkdir()
    monkeypatch.chdir(root)
    if global_fallback:
        monkeypatch.delenv("SELVEDGE_DB")
        monkeypatch.setattr(Path, "home", lambda: root)
        monkeypatch.delenv("SELVEDGE_QUIET")
    else:
        monkeypatch.setenv("SELVEDGE_DB", str(root / ".selvedge" / "selvedge.db"))
    result = _check(root / "DECISIONS.md")
    assert result.exit_code == 1, result.output
    assert "Cannot read decision history" in result.output
    assert not (root / ".selvedge").exists()
    assert not (root / "DECISIONS.md").exists()


def test_current_global_database_is_silent(store, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("SELVEDGE_DB")
    monkeypatch.delenv("SELVEDGE_QUIET")
    monkeypatch.setattr(Path, "home", lambda: store.db_path.parent.parent)
    target = elsewhere / "DECISIONS.md"
    target.write_text(render_markdown(store.get_history()), encoding="utf-8")
    result = _check(target)
    assert result.exit_code == 0, result.output
    assert result.output == ""


@pytest.mark.parametrize("kind", ["non_utf8", "directory", "permission"])
def test_digest_read_errors_are_graceful(store, tmp_path, monkeypatch, kind):
    target = tmp_path / "[red]digest.md"
    if kind == "directory":
        target.mkdir()
    else:
        target.write_bytes(b"\xff" if kind == "non_utf8" else b"saved")
    if kind == "permission":
        read = Path.read_bytes

        def denied(path):
            if path == target:
                raise PermissionError("access denied [red]")
            return read(path)

        monkeypatch.setattr(Path, "read_bytes", denied)
    result = _check(target)
    assert result.exit_code == 1, result.output
    assert "Cannot check" in result.output
    assert str(target) in result.output
    assert "readable UTF-8 file" in result.output
    assert "Traceback" not in result.output


def test_database_error_does_not_run_migrations(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE old_schema (value TEXT)")
    monkeypatch.setenv("SELVEDGE_DB", str(path))
    before = path.read_bytes()
    result = _check(tmp_path / "DECISIONS.md")
    assert result.exit_code == 1
    assert "Cannot read decision history" in result.output
    assert path.read_bytes() == before
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT name FROM sqlite_schema WHERE type='table'").fetchall() == [("old_schema",)]


def test_invalid_since_is_a_literal_usage_error(tmp_path, monkeypatch):
    path = tmp_path / "missing" / "store.db"
    monkeypatch.setenv("SELVEDGE_DB", str(path))
    result = _check(tmp_path / "digest.md", "--since", "[/oops]")
    assert result.exit_code == 2, result.output
    assert "--since" in result.output and "[/oops]" in result.output
    assert not path.parent.exists()


def test_check_reads_committed_wal_rows_without_modifying_them(store, tmp_path):
    holder = sqlite3.connect(store.db_path)
    try:
        holder.execute("SELECT COUNT(*) FROM events").fetchone()
        store.log_event(ChangeEvent(
            entity_path="users.token", change_type="remove", reasoning="Latest WAL event.",
        ))
        wal = Path(str(store.db_path) + "-wal")
        assert wal.is_file() and wal.stat().st_size > 0
        target = tmp_path / "DECISIONS.md"
        target.write_text(render_markdown(store.get_history()), encoding="utf-8")
        before = (store.db_path.read_bytes(), wal.read_bytes(), target.read_bytes())
        result = _check(target)
        assert result.exit_code == 0, result.output
        assert result.output == ""
        assert (store.db_path.read_bytes(), wal.read_bytes(), target.read_bytes()) == before
    finally:
        holder.close()


@pytest.mark.parametrize("change", ["newline", "crlf"])
def test_check_compares_bytes_including_line_endings(store, tmp_path, change):
    target = tmp_path / "DECISIONS.md"
    expected = render_markdown(store.get_history()).encode("utf-8")
    actual = expected.rstrip(b"\n") if change == "newline" else expected.replace(b"\n", b"\r\n")
    target.write_bytes(actual)
    result = _check(target)
    assert result.exit_code == 1
    assert "@@ " in result.output
    if change == "newline":
        assert "No newline at end of file" in result.output
    assert target.read_bytes() == actual


def test_filters_match_normal_export(store, tmp_path):
    store.log_event(ChangeEvent(
        entity_path="users.token.child", change_type="modify", project="app",
        timestamp="2026-02-01T00:00:00Z", reasoning="Child decision.",
    ))
    store.log_event(ChangeEvent(
        entity_path="users.other", change_type="add", project="other",
        timestamp="2026-03-01T00:00:00Z", reasoning="Unrelated decision.",
    ))
    target = tmp_path / "DECISIONS.md"
    args = ["--since", "2026-01-15", "--entity", "users.token", "--project", "app", "--limit", "1"]
    export = CliRunner().invoke(cli, ["export", "--format", "markdown", "-o", str(target), *args])
    assert export.exit_code == 0, export.output
    assert "Child decision" in target.read_text()
    result = _check(target, *args)
    assert result.exit_code == 0, result.output
    assert result.output == ""


def test_normal_export_keeps_background_controls(store, monkeypatch):
    calls = _background_spies(monkeypatch)
    result = CliRunner().invoke(cli, ["export", "--format", "json"])
    assert result.exit_code == 0
    assert len(calls) == 3
    assert calls[-1] == ("cli",)
