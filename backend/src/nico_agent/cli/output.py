"""Consistent human and machine output for every CLI command."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping
from typing import Any, TextIO

from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.table import Table

from nico_agent.cli.errors import CliError


class Output:
    def __init__(
        self,
        *,
        json_mode: bool = False,
        no_color: bool = False,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        self.json_mode = json_mode
        self.no_color = no_color
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        color_system = None if no_color else "auto"
        self.out = Console(
            file=self.stdout,
            color_system=color_system,
            no_color=no_color,
            highlight=False,
            soft_wrap=False,
        )
        self.err = Console(
            file=self.stderr,
            color_system=color_system,
            no_color=no_color,
            highlight=False,
            soft_wrap=False,
        )

    def emit(self, value: Any, *, title: str | None = None) -> None:
        if self.json_mode:
            self.out.print_json(json.dumps(value, ensure_ascii=False, default=str))
            return
        if isinstance(value, list):
            self.table(value, title=title)
        elif isinstance(value, Mapping):
            rendered = JSON.from_data(value, ensure_ascii=False, indent=2)
            self.out.print(Panel(rendered, title=title, border_style="blue") if title else rendered)
        else:
            self.out.print(str(value))

    def table(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        title: str | None = None,
        columns: list[str] | None = None,
    ) -> None:
        materialized = list(rows)
        if self.json_mode:
            self.emit(materialized)
            return
        if not materialized:
            self.out.print(f"[dim]{title or 'Result'}: no records[/dim]")
            return
        selected = columns or list(materialized[0])
        table = Table(title=title, header_style="bold blue")
        for column in selected:
            table.add_column(column.replace("_", " ").title())
        for row in materialized:
            table.add_row(*[_cell(row.get(column)) for column in selected])
        self.out.print(table)

    def error(self, error: CliError) -> None:
        if self.json_mode:
            self.err.print_json(json.dumps(error.as_dict(), ensure_ascii=False, default=str))
            return
        request = f" [dim](request {error.request_id})[/dim]" if error.request_id else ""
        self.err.print(f"[bold red]{error.code}[/bold red]: {error.message}{request}")


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)
