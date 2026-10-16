"""Literal rendering of user data in ``log`` and ``changeset``.

Rich parses ``[...]`` in printed strings as markup. Before this fix, a
reasoning, entity path, agent or changeset id such as ``[/nonexistent]``
raised ``MarkupError``. For ``selvedge log`` that happened on the success
line, after the event was committed, so the command exited nonzero on a
successful write and a caller retrying it would store a duplicate.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from selvedge.cli import cli
from selvedge.models import ChangeEvent
from selvedge.presenters import changeset_payload
from selvedge.storage import SelvedgeStorage

HOSTILE_REASONING = r"why [bold]x[/] [link=x]y [red]unclosed \[ok] [/nonexistent] end"
HOSTILE_ENTITY = "users.[/x]email"
HOSTILE_AGENT = "ag[/]"
HOSTILE_CS = "c[/x]"


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SELVEDGE_DB", str(tmp_path / "selvedge.db"))
    import selvedge.server as srv

    srv._storage = None
    yield
    srv._storage = None


def _storage() -> SelvedgeStorage:
    from selvedge.config import get_db_path

    return SelvedgeStorage(get_db_path())


def _squash(s: str) -> str:
    # Rich wraps at the terminal width; compare content, not line breaks.
    return "".join(s.split())


def _log_hostile(runner):
    return runner.invoke(
        cli,
        [
            "log", HOSTILE_ENTITY, "add",
            "--reasoning", HOSTILE_REASONING,
            "--changeset", HOSTILE_CS,
            "--agent", HOSTILE_AGENT,
        ],
    )


def _event_count() -> int:
    return len(_storage().get_changeset(HOSTILE_CS))


def test_log_hostile_data_exits_zero_and_stores_exactly_one(runner):
    result = _log_hostile(runner)
    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert _event_count() == 1
    out = _squash(result.output)
    assert _squash(f"Logged {HOSTILE_ENTITY} (add)") in out
    assert _squash(f"changeset:{HOSTILE_CS}") in out


@pytest.mark.parametrize(
    "payload",
    ["[bold]", "[/]", "[link=x]", "[red]unclosed", r"\[", "[/nonexistent]", "trailing\\"],
)
def test_log_each_markup_shape_in_entity_is_literal(runner, payload):
    entity = f"t.col{payload}"
    result = runner.invoke(cli, ["log", entity, "add", "--reasoning", "because it is needed"])
    assert result.exit_code == 0, result.output
    # Storage canonicalizes paths (e.g. "\\" -> "/"); the success line must
    # show the stored path literally, whatever it is.
    rows = _storage().get_history(limit=10)
    assert len(rows) == 1
    assert _squash(f"Logged {rows[0]['entity_path']} (add)") in _squash(result.output)


def test_log_rename_hostile_paths_literal(runner):
    result = runner.invoke(
        cli,
        ["log", "new.[/a]path", "rename", "--rename-from", "old.[b]path", "--reasoning", "moved it"],
    )
    assert result.exit_code == 0, result.output
    assert _squash("Renamed old.[b]path → new.[/a]path") in _squash(result.output)


def test_log_supersede_stored_markup_id_exits_zero_after_single_write(runner):
    storage = _storage()
    previous = storage.log_event(
        ChangeEvent(
            entity_path=HOSTILE_ENTITY,
            change_type="reject",
            id="[/oops]prior",
            reasoning="The current constraint prevents this approach",
        )
    )
    result = runner.invoke(
        cli,
        ["log", HOSTILE_ENTITY, "supersede", "--reasoning", "The constraint is now resolved"],
    )
    assert result.exit_code == 0, result.output
    assert result.exception is None
    rows = storage.get_history(limit=10)
    assert len(rows) == 2
    reopened = [row for row in rows if row["change_type"] == "supersede"]
    assert len(reopened) == 1
    assert reopened[0]["supersedes"] == previous.id
    assert _squash(f"Superseded {previous.id[:8]} on {HOSTILE_ENTITY}") in _squash(result.output)


def test_changeset_invalid_since_renders_validation_error_literally(runner):
    invalid = "[/oops]"
    result = runner.invoke(cli, ["changeset", "--list", "--since", invalid])
    assert result.exit_code == 2, result.output
    assert invalid in result.output
    assert "error:" in result.output


@pytest.mark.parametrize("option", ["--revisit-after", "--expires-when"])
def test_log_invalid_policy_renders_validation_error_without_writing(runner, option):
    invalid = "[/oops]"
    result = runner.invoke(cli, ["log", HOSTILE_ENTITY, "add", option, invalid])
    assert result.exit_code == 2, result.output
    assert invalid in result.output
    assert _storage().get_history(limit=10) == []


def test_changeset_show_renders_complete_literal_data(runner):
    assert _log_hostile(runner).exit_code == 0
    result = runner.invoke(cli, ["changeset", HOSTILE_CS])
    assert result.exit_code == 0, result.output
    out = _squash(result.output)
    for literal in (HOSTILE_CS, HOSTILE_AGENT, HOSTILE_ENTITY, HOSTILE_REASONING):
        assert _squash(literal) in out, literal
    assert _event_count() == 1


def test_changeset_list_renders_literal_id(runner):
    assert _log_hostile(runner).exit_code == 0
    result = runner.invoke(cli, ["changeset", "--list"])
    assert result.exit_code == 0, result.output
    assert HOSTILE_CS in result.output


def test_changeset_unknown_error_path_is_literal(runner):
    missing = "missing[/nonexistent][red]"
    result = runner.invoke(cli, ["changeset", missing])
    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert missing in result.output


def test_changeset_json_unchanged_and_parses(runner):
    assert _log_hostile(runner).exit_code == 0
    result = runner.invoke(cli, ["changeset", HOSTILE_CS, "--json"])
    assert result.exit_code == 0
    # Byte-identical to the shared presenter payload, no Rich escaping.
    expected = json.dumps(changeset_payload(_storage(), HOSTILE_CS), indent=2) + "\n"
    assert result.output == expected
    rows = json.loads(result.output)
    assert len(rows) == 1
    assert rows[0]["reasoning"] == HOSTILE_REASONING
    assert rows[0]["entity_path"] == HOSTILE_ENTITY
    assert "\\[" not in rows[0]["changeset_id"]

    listed = runner.invoke(cli, ["changeset", "--list", "--json"])
    assert listed.exit_code == 0
    assert json.loads(listed.output)[0]["changeset_id"] == HOSTILE_CS

    missing = runner.invoke(cli, ["changeset", "nope[/x]", "--json"])
    assert json.loads(missing.output)[0]["error"].count("nope[/x]") == 1
