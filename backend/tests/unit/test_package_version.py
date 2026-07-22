from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import nico_agent


def test_package_version_matches_source_metadata() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject.open("rb") as source:
        expected = tomllib.load(source)["project"]["version"]

    assert nico_agent.__version__ == expected


def test_source_version_changes_without_reinstall(tmp_path) -> None:
    package_file = tmp_path / "backend" / "src" / "nico_agent" / "__init__.py"
    package_file.parent.mkdir(parents=True)
    package_file.touch()
    pyproject = tmp_path / "backend" / "pyproject.toml"
    pyproject.write_text('[project]\nname = "nico-agent-platform"\nversion = "9.8.7"\n')
    code = """
import importlib
import nico_agent
from typer.testing import CliRunner

nico_agent.__file__ = __import__('sys').argv[1]
nico_agent.__dict__.pop('__version__', None)
assert nico_agent.__version__ == '9.8.7'

config = importlib.import_module('nico_agent.config')
assert config.Settings().app_version == '9.8.7'
cli = importlib.import_module('nico_agent.cli.app')
result = CliRunner().invoke(cli.app, ['--version'])
assert result.exit_code == 0
assert result.stdout.strip() == 'nico 9.8.7'
"""
    subprocess.run(
        [sys.executable, "-c", code, str(package_file)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_installed_version_uses_distribution_metadata(tmp_path) -> None:
    installed_package = tmp_path / "site-packages" / "nico_agent" / "__init__.py"
    installed_package.parent.mkdir(parents=True)
    installed_package.touch()
    code = """
import importlib.metadata
import nico_agent

nico_agent.__file__ = __import__('sys').argv[1]
nico_agent.__dict__.pop('__version__', None)
importlib.metadata.version = lambda name: '7.6.5'
assert nico_agent.__version__ == '7.6.5'
"""
    subprocess.run(
        [sys.executable, "-c", code, str(installed_package)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_package_import_defers_distribution_metadata_lookup() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, nico_agent; print('importlib.metadata' in sys.modules)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "False"
