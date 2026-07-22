"""Terminal-safe Nico cat identity.

The mark is deliberately code-native: it never requires image assets, remains
legible without colour, and is omitted from machine-readable output.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import TextIO

from rich.text import Text

SIMPLE_CAT = (
    " /\\_/\\",
    "( o.o )",
    " > ^ <",
)


@dataclass(frozen=True, slots=True)
class TerminalCapabilities:
    width: int
    is_tty: bool
    unicode: bool
    color: bool


def capabilities(
    stream: TextIO | None = None,
    *,
    width: int | None = None,
    no_color: bool = False,
) -> TerminalCapabilities:
    target = stream or sys.stdout
    encoding = (getattr(target, "encoding", None) or "utf-8").lower()
    term = os.environ.get("TERM", "")
    is_tty = bool(getattr(target, "isatty", lambda: False)())
    resolved_width = width or _terminal_width(target)
    return TerminalCapabilities(
        width=resolved_width,
        is_tty=is_tty,
        unicode="utf" in encoding and term != "dumb",
        color=is_tty and not no_color and "NO_COLOR" not in os.environ and term != "dumb",
    )


def terminal_cat(caps: TerminalCapabilities) -> Text:
    """Return Nico's small cat mark with a terminal-safe colour accent."""

    use_color = caps.unicode and caps.width >= 48 and caps.color
    lines = SIMPLE_CAT
    text = Text()
    for index, line in enumerate(lines):
        if index:
            text.append("\n")
        if not use_color:
            text.append(line)
            continue
        # Keep the silhouette blue and use gold only for the face details.
        for char in line:
            style = "#d0a84e" if char in {"o", "^"} else "#7895ac"
            text.append(char, style=style)
    return text


def _terminal_width(stream: TextIO) -> int:
    try:
        return os.get_terminal_size(stream.fileno()).columns
    except (AttributeError, OSError, ValueError):
        return 80
