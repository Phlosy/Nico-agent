from __future__ import annotations

from nico_agent.cli.logo import SIMPLE_CAT, TerminalCapabilities, terminal_cat


def test_terminal_cat_is_the_same_simple_shape_in_every_terminal() -> None:
    wide = TerminalCapabilities(width=80, is_tty=True, unicode=True, color=True)
    no_color = TerminalCapabilities(width=80, is_tty=True, unicode=True, color=False)
    narrow = TerminalCapabilities(width=32, is_tty=True, unicode=True, color=True)
    ascii_only = TerminalCapabilities(width=80, is_tty=False, unicode=False, color=False)

    expected = "\n".join((" /\\_/\\", "( o.o )", " > ^ <"))

    assert SIMPLE_CAT == (" /\\_/\\", "( o.o )", " > ^ <")
    assert terminal_cat(wide).plain == expected
    assert terminal_cat(no_color).plain == expected
    assert terminal_cat(narrow).plain == expected
    assert terminal_cat(ascii_only).plain == expected


def test_terminal_cat_uses_a_simple_blue_and_gold_palette_in_color_mode() -> None:
    caps = TerminalCapabilities(width=80, is_tty=True, unicode=True, color=True)

    rendered = terminal_cat(caps)

    styles = {str(span.style) for span in rendered.spans}
    assert {"#7895ac", "#d0a84e"} <= styles
