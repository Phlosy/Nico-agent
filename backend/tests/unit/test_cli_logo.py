from __future__ import annotations

from nico_agent.cli.logo import COMPACT_CAT, PIXEL_CAT, TerminalCapabilities, coin_cat


def test_coin_cat_pixel_and_narrow_snapshots() -> None:
    wide = TerminalCapabilities(width=80, is_tty=True, unicode=True, color=True)
    no_color = TerminalCapabilities(width=80, is_tty=True, unicode=True, color=False)
    narrow = TerminalCapabilities(width=32, is_tty=True, unicode=True, color=True)
    ascii_only = TerminalCapabilities(width=80, is_tty=False, unicode=False, color=False)

    assert coin_cat(wide).plain == "\n".join(PIXEL_CAT)
    assert coin_cat(no_color).plain == "\n".join(COMPACT_CAT)
    assert coin_cat(narrow).plain == "\n".join(COMPACT_CAT)
    assert coin_cat(ascii_only).plain == "\n".join(COMPACT_CAT)


def test_coin_cat_uses_blue_gold_white_palette_in_color_mode() -> None:
    caps = TerminalCapabilities(width=80, is_tty=True, unicode=True, color=True)

    rendered = coin_cat(caps)

    styles = {str(span.style) for span in rendered.spans}
    assert {"#7895ac", "#d0a84e", "#dbe7ee", "#263847"} <= styles
