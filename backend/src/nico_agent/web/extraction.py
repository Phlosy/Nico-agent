"""Bounded offline extraction for already-downloaded Web response bytes."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from typing import Literal

import trafilatura

from nico_agent.web.normalization import bounded_external_text, clean_external_text

ExtractMode = Literal["markdown", "text"]


class WebExtractionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ExtractedWebContent:
    content_type: str
    title: str | None
    extractor: str
    content: str
    truncated: bool


def extract_web_content(
    body: bytes,
    content_type: str,
    *,
    mode: ExtractMode,
    max_chars: int,
) -> ExtractedWebContent:
    if mode not in {"markdown", "text"}:
        raise WebExtractionError("WEB_FETCH_EXTRACTION_FAILED", "Web extract mode is invalid")
    if not 100 <= max_chars <= 20_000:
        raise WebExtractionError("WEB_FETCH_EXTRACTION_FAILED", "Web content limit is invalid")
    media_type, charset = _parse_content_type(content_type)
    if media_type in {"text/html", "application/xhtml+xml"}:
        text = _decode(body, charset)
        return _extract_html(text, media_type=media_type, mode=mode, max_chars=max_chars)
    if media_type in {"text/plain", "text/markdown", "text/x-markdown"}:
        text = _decode(body, charset)
        if mode == "text" and media_type in {"text/markdown", "text/x-markdown"}:
            text = _markdown_as_text(text)
        return _bounded(
            text,
            media_type=media_type,
            title=None,
            extractor="markdown" if media_type != "text/plain" else "plain",
            max_chars=max_chars,
        )
    if media_type == "application/json" or media_type.endswith("+json"):
        text = _decode(body, charset)
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise WebExtractionError(
                "WEB_FETCH_EXTRACTION_FAILED",
                "Web JSON content could not be decoded",
            ) from exc
        stable = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        return _bounded(
            stable,
            media_type=media_type,
            title=None,
            extractor="json",
            max_chars=max_chars,
        )
    raise WebExtractionError(
        "WEB_FETCH_CONTENT_UNSUPPORTED",
        "Web response content type is not supported",
    )


def _extract_html(
    text: str,
    *,
    media_type: str,
    mode: ExtractMode,
    max_chars: int,
) -> ExtractedWebContent:
    parser = _VisibleHtmlParser()
    try:
        parser.feed(text)
        parser.close()
        extracted = trafilatura.extract(
            text,
            output_format="markdown" if mode == "markdown" else "txt",
            include_comments=False,
            include_tables=False,
        )
    except Exception as exc:
        raise WebExtractionError(
            "WEB_FETCH_EXTRACTION_FAILED",
            "Web HTML extraction failed",
        ) from exc
    fallback = parser.visible_text
    content = extracted if isinstance(extracted, str) and extracted.strip() else fallback
    return _bounded(
        content,
        media_type=media_type,
        title=clean_external_text(parser.title, 500) or None,
        extractor="trafilatura" if extracted and extracted.strip() else "html_fallback",
        max_chars=max_chars,
    )


def _bounded(
    text: str,
    *,
    media_type: str,
    title: str | None,
    extractor: str,
    max_chars: int,
) -> ExtractedWebContent:
    bounded = bounded_external_text(text, maximum=max_chars)
    if not bounded.text:
        raise WebExtractionError(
            "WEB_FETCH_EXTRACTION_FAILED",
            "Web response did not contain extractable text",
        )
    return ExtractedWebContent(
        content_type=media_type,
        title=title,
        extractor=extractor,
        content=bounded.text,
        truncated=bounded.truncated,
    )


def _parse_content_type(value: str) -> tuple[str, str]:
    message = Message()
    message["content-type"] = value
    media_type = message.get_content_type().lower()
    charset = message.get_content_charset() or "utf-8"
    if not value or media_type == "text/plain" and not value.lower().startswith("text/plain"):
        raise WebExtractionError(
            "WEB_FETCH_CONTENT_UNSUPPORTED",
            "Web response content type is not supported",
        )
    return media_type, charset


def _decode(body: bytes, charset: str) -> str:
    try:
        return body.decode(charset, errors="strict")
    except (LookupError, UnicodeDecodeError) as exc:
        raise WebExtractionError(
            "WEB_FETCH_EXTRACTION_FAILED",
            "Web response text encoding is invalid",
        ) from exc


def _markdown_as_text(value: str) -> str:
    text = re.sub(r"!\[([^]]*)\]\([^)]*\)", r"\1", value)
    text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"[`*_~]", "", text)
    return text


class _VisibleHtmlParser(HTMLParser):
    _HIDDEN = frozenset({"script", "style", "noscript", "template", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._hidden_depth = 0
        self._title_depth = 0
        self._visible: list[str] = []
        self._title: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._HIDDEN:
            self._hidden_depth += 1
        if tag == "title":
            self._title_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._HIDDEN and self._hidden_depth:
            self._hidden_depth -= 1
        if tag == "title" and self._title_depth:
            self._title_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            self._title.append(data)
        if not self._hidden_depth and not self._title_depth:
            self._visible.append(data)

    @property
    def visible_text(self) -> str:
        return "\n".join(part.strip() for part in self._visible if part.strip())

    @property
    def title(self) -> str:
        return " ".join(part.strip() for part in self._title if part.strip())
