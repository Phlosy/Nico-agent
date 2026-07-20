"""Terminal-safe Nico coin-cat identity.

The mark is deliberately code-native: it never requires image assets, remains
legible without colour, and is omitted from machine-readable output.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import TextIO

from rich.text import Text

PIXEL_CAT = (
    "       ▄██████▄",
    "     ▄█▓▓▓▓▓▓▓▓█▄",
    "    █▓▒▄▀▄▓▓▄▀▄▒▓█",
    "   █▓▒█  ●██●  █▒▓█",
    "   █▓▒█   ██   █▒▓█",
    "   █▓▒▀▄  ▄▄  ▄▀▒▓█",
    "    █▓▒▒▀▄▄▄▄▀▒▒▓█",
    "     ▀█▓▓▓▓▓▓█▀",
    "       ▀████▀",
)

COMPACT_CAT = (
    "    .-====-.",
    "   / /\\_/\\ \\",
    "  | ( o.o ) |",
    "  |  > ^ <  |",
    "   '-====-'",
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


def coin_cat(caps: TerminalCapabilities) -> Text:
    """Return the full pixel mark when possible, otherwise its ASCII fallback."""

    lines = PIXEL_CAT if caps.unicode and caps.width >= 48 and caps.color else COMPACT_CAT
    text = Text()
    for index, line in enumerate(lines):
        if index:
            text.append("\n")
        if lines is COMPACT_CAT or not caps.color:
            text.append(line)
            continue
        # Muted blue, soft gold, warm white and dark slate form Nico's palette.
        for char in line:
            style = "#dbe7ee"
            if char in {"▓", "█"}:
                style = "#7895ac"
            elif char in {"▒", "▄", "▀"}:
                style = "#d0a84e"
            elif char == "●":
                style = "#263847"
            text.append(char, style=style)
    return text


def _terminal_width(stream: TextIO) -> int:
    try:
        return os.get_terminal_size(stream.fileno()).columns
    except (AttributeError, OSError, ValueError):
        return 80
