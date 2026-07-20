#!/usr/bin/env python3
"""Validate user-facing Markdown links and obvious publication leaks."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", ".venv", "node_modules", "artifacts", "dist"}
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
PUBLIC_DOCS = [
    ROOT / "README.md",
    ROOT / "backend" / "README.md",
    *sorted((ROOT / "docs").glob("*.md")),
]
README_PRIVATE_RE = re.compile(
    r"/home/|\.codex|pasted-text|attachments/[0-9a-f-]{16,}|"
    r"\bgoal\s+[a-l]\b|implementation taskbook|开发过程|内部任务",
    re.IGNORECASE,
)
SECRET_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b"
)


def markdown_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if not any(part in SKIP_PARTS for part in path.relative_to(ROOT).parts)
    )


def local_link_target(source: Path, raw_target: str) -> Path | None:
    target = raw_target.strip()
    if target.startswith("<") and ">" in target:
        target = target[1 : target.index(">")]
    else:
        target = target.split(maxsplit=1)[0]
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    path_text = unquote(parsed.path)
    if path_text.startswith("/"):
        return ROOT / path_text.lstrip("/")
    return (source.parent / path_text).resolve()


def main() -> int:
    failures: list[str] = []
    files = markdown_files()

    for source in files:
        content = source.read_text(encoding="utf-8")
        if content.count("```") % 2:
            failures.append(f"unbalanced fenced code block: {source.relative_to(ROOT)}")
        for raw_target in LINK_RE.findall(content):
            target = local_link_target(source, raw_target)
            if target is not None and not target.exists():
                failures.append(
                    "broken local link: "
                    f"{source.relative_to(ROOT)} -> {raw_target.strip()}"
                )

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if match := README_PRIVATE_RE.search(readme):
        failures.append(f"README contains internal publication marker: {match.group(0)!r}")

    for source in PUBLIC_DOCS:
        content = source.read_text(encoding="utf-8")
        if match := SECRET_RE.search(content):
            failures.append(
                f"possible credential in {source.relative_to(ROOT)}: {match.group(0)[:8]}..."
            )

    if failures:
        print("documentation checks failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print(
        f"documentation checks passed: {len(files)} Markdown files, "
        f"{len(PUBLIC_DOCS)} publication-surface files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
