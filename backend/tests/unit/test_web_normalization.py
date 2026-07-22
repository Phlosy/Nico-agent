from __future__ import annotations

from nico_agent.web.normalization import bounded_external_text, clean_external_text


def test_external_text_is_unicode_normalized_and_control_characters_are_removed() -> None:
    assert clean_external_text("  Cafe\u0301\x00\u202e\n next\tline  ") == "Café\n next\tline"


def test_external_text_envelope_is_bounded_and_explicitly_untrusted() -> None:
    content = bounded_external_text(
        "abcdef api_key=secret-value trailing",
        maximum=17,
        secrets={"api_key": "secret-value"},
    )

    assert content.text == "abcdef [REDACTED]"
    assert content.truncated is True
    assert content.metadata == {"external_content": True}
