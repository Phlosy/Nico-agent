#!/usr/bin/env python3
"""Run the provider-neutral conversation continuity evaluation suite."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from nico_agent.evals.conversation_continuity import (
    DEFAULT_SUITE_PATH,
    ContinuityEvaluationReport,
    load_continuity_suite,
    run_external_suite,
    run_hermetic_suite,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE_PATH)
    parser.add_argument(
        "--provider",
        choices=("hermetic", "external", "both"),
        default="hermetic",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument(
        "--require-pass",
        action="store_true",
        help="exit non-zero unless every non-skipped case passes",
    )
    return parser.parse_args()


async def run(args: argparse.Namespace) -> tuple[dict, bool]:
    suite = load_continuity_suite(args.suite)
    reports: list[ContinuityEvaluationReport] = []
    if args.provider in {"hermetic", "both"}:
        reports.append(await run_hermetic_suite(suite))
    if args.provider in {"external", "both"}:
        reports.append(
            await run_external_suite(
                suite,
                timeout_seconds=args.timeout_seconds,
            )
        )
    payload: dict
    if len(reports) == 1:
        payload = reports[0].model_dump(mode="json")
    else:
        payload = {
            "schema_version": 1,
            "suite_id": suite.suite_id,
            "reports": [report.model_dump(mode="json") for report in reports],
        }
    all_passed = all(
        all(case.status in {"passed", "skipped"} for case in report.cases)
        and (
            report.verification_status != "unverified"
            or report.credential_status == "unavailable"
        )
        for report in reports
    )
    return payload, all_passed


def main() -> int:
    args = parse_args()
    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be positive")
    payload, all_passed = asyncio.run(run(args))
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 1 if args.require_pass and not all_passed else 0


if __name__ == "__main__":
    raise SystemExit(main())
