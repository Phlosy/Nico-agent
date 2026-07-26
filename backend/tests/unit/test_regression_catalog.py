from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nico_agent.regressions.catalog import (
    RegressionCatalogError,
    classify_incident,
    load_catalog,
    select_test_nodeids,
)
from nico_agent.regressions.cli import main
from nico_agent.regressions.pytest_plugin import FailOnSkippedRegressions


def _family(
    *,
    family_id: str = "runtime.agent_action.unoffered_tool",
    matcher: str = r"unauthorized tool|final_output",
    nodeid: str = (
        "backend/tests/unit/test_example.py::test_model_tool_is_corrected_before_dispatch"
    ),
) -> dict:
    return {
        "id": family_id,
        "title": "Unoffered model tool call",
        "invariant": "A model cannot dispatch a tool absent from its request.",
        "owners": ["runtime"],
        "tags": ["agent-action", "authorization"],
        "matchers": [matcher],
        "cases": [
            {
                "id": "unoffered-tool-corrected",
                "title": "Correct before dispatch",
                "tier": "unit",
                "protects": "One tool-free correction occurs before any side effect.",
                "test_nodeids": [nodeid],
            }
        ],
        "incidents": [
            {
                "id": f"inc-20260724-{family_id.replace('.', '-')}",
                "detected_at": "2026-07-24",
                "classification": "new_family",
                "symptom": "Model requested unauthorized tool final_output.",
                "root_cause": "Tool-name validation happened after protocol correction.",
                "case_ids": ["unoffered-tool-corrected"],
                "references": ["user-report:2026-07-24"],
            }
        ],
    }


def _write_catalog(root: Path, families: list[dict]) -> Path:
    path = root / "backend" / "regressions" / "catalog.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "suite_id": "nico-regression-catalog-v1",
                "families": families,
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_test(root: Path) -> None:
    path = root / "backend" / "tests" / "unit" / "test_example.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "def test_model_tool_is_corrected_before_dispatch():\n    pass\n",
        encoding="utf-8",
    )


def test_catalog_validates_traceability_and_classifies_same_family(tmp_path: Path) -> None:
    _write_test(tmp_path)
    catalog = load_catalog(_write_catalog(tmp_path, [_family()]), repo_root=tmp_path)

    matches = classify_incident(
        catalog,
        "the model requested an unauthorized tool named final_output",
    )

    assert [(match.family_id, match.matched_patterns) for match in matches] == [
        (
            "runtime.agent_action.unoffered_tool",
            (r"unauthorized tool|final_output",),
        )
    ]
    assert select_test_nodeids(catalog, family_id=matches[0].family_id) == (
        "backend/tests/unit/test_example.py::test_model_tool_is_corrected_before_dispatch",
    )


@pytest.mark.parametrize(
    ("families", "message"),
    [
        (
            [_family(), _family()],
            "duplicate regression family id",
        ),
        (
            [
                _family(),
                _family(
                    family_id="cli.queue.paused_head",
                    matcher=r"UNAUTHORIZED   TOOL|FINAL_OUTPUT",
                ),
            ],
            "matcher is already owned by another family",
        ),
        (
            [_family(nodeid=("backend/tests/unit/test_example.py::test_missing_regression"))],
            "test function does not exist",
        ),
        (
            [
                _family(),
                _family(
                    family_id="cli.queue.paused_head",
                    matcher=r"model requested",
                ),
            ],
            "test nodeid is already owned by another case",
        ),
    ],
)
def test_catalog_rejects_duplicate_or_untraceable_entries(
    tmp_path: Path,
    families: list[dict],
    message: str,
) -> None:
    _write_test(tmp_path)
    path = _write_catalog(tmp_path, families)

    with pytest.raises(RegressionCatalogError, match=message):
        load_catalog(path, repo_root=tmp_path)


def test_catalog_rejects_an_incident_that_cannot_classify_to_its_family(
    tmp_path: Path,
) -> None:
    _write_test(tmp_path)
    path = _write_catalog(tmp_path, [_family(matcher=r"paused queue")])

    with pytest.raises(RegressionCatalogError, match="symptom does not match its family"):
        load_catalog(path, repo_root=tmp_path)


def test_catalog_rejects_a_case_without_an_incident_reference(tmp_path: Path) -> None:
    _write_test(tmp_path)
    family = _family()
    family["cases"].append(
        {
            **family["cases"][0],
            "id": "unreferenced-boundary",
        }
    )
    path = _write_catalog(tmp_path, [family])

    with pytest.raises(RegressionCatalogError, match="cases are not referenced by an incident"):
        load_catalog(path, repo_root=tmp_path)


def test_cli_reports_existing_family_candidates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_test(tmp_path)
    path = _write_catalog(tmp_path, [_family()])

    exit_code = main(
        [
            "--catalog",
            str(path),
            "classify",
            "model requested unauthorized tool final_output",
        ],
        repo_root=tmp_path,
    )

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["classification"] == "existing_family_candidate"
    assert [match["family_id"] for match in result["matches"]] == [
        "runtime.agent_action.unoffered_tool"
    ]


def test_regression_plugin_fails_a_green_run_when_a_case_is_skipped() -> None:
    plugin = FailOnSkippedRegressions()
    plugin.pytest_runtest_logreport(
        SimpleNamespace(skipped=True, nodeid="backend/tests/unit/test_example.py::test_case")
    )
    session = SimpleNamespace(exitstatus=pytest.ExitCode.OK)

    plugin.pytest_sessionfinish(session, pytest.ExitCode.OK)

    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
