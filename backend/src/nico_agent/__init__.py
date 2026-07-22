"""Nico Agent platform shared package."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    __version__: str


def _resolve_version() -> str:
    import tomllib
    from pathlib import Path

    source_root = Path(__file__).resolve().parents[3]
    source_pyproject = source_root / "backend" / "pyproject.toml"
    if source_pyproject.is_file():
        with source_pyproject.open("rb") as source:
            return str(tomllib.load(source)["project"]["version"])

    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("nico-agent-platform")
    except PackageNotFoundError:  # pragma: no cover - unpackaged source fragment
        return "0+unknown"


def __getattr__(name: str) -> str:
    if name != "__version__":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    resolved = _resolve_version()
    globals()[name] = resolved
    return resolved
