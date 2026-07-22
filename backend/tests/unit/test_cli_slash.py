from __future__ import annotations

import pytest

from nico_agent.cli.errors import CliError
from nico_agent.cli.slash import help_rows, parse_slash


def test_slash_parser_handles_quotes_aliases_and_plain_messages() -> None:
    assert parse_slash("ordinary prompt") is None
    assert parse_slash("/quit").name == "exit"  # type: ignore[union-attr]
    parsed = parse_slash('/title "Research notes"')
    assert parsed is not None
    assert parsed.name == "title"
    assert parsed.args == ("Research notes",)
    assert parsed.raw_args == '"Research notes"'


def test_slash_registry_is_discoverable_and_rejects_unknown_command() -> None:
    commands = {row["command"] for row in help_rows()}
    assert {
        "/new",
        "/continue",
        "/plan",
        "/tools",
        "/permissions",
        "/queue",
        "/retry",
        "/compact",
        "/attach",
        "/download",
    } <= commands

    with pytest.raises(CliError) as captured:
        parse_slash("/not-a-command")
    assert captured.value.code == "UNKNOWN_SLASH_COMMAND"
