"""Command-line interface for the regression catalog."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from nico_agent.regressions.catalog import (
    DEFAULT_CATALOG_PATH,
    REPO_ROOT,
    RegressionCatalogError,
    classify_incident,
    load_catalog,
    select_test_nodeids,
)


def main(
    argv: Sequence[str] | None = None,
    *,
    repo_root: Path = REPO_ROOT,
) -> int:
    """Validate, classify, or select tests from the project catalog."""

    parser = _parser()
    args = parser.parse_args(argv)
    try:
        catalog = load_catalog(args.catalog, repo_root=repo_root)
        if args.command == "check":
            case_count = sum(len(family.cases) for family in catalog.families)
            incident_count = sum(len(family.incidents) for family in catalog.families)
            print(
                json.dumps(
                    {
                        "suite_id": catalog.suite_id,
                        "families": len(catalog.families),
                        "cases": case_count,
                        "incidents": incident_count,
                        "status": "valid",
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "classify":
            matches = classify_incident(catalog, args.symptom)
            print(
                json.dumps(
                    {
                        "classification": (
                            "existing_family_candidate" if matches else "new_family_candidate"
                        ),
                        "matches": [
                            {
                                "family_id": match.family_id,
                                "matched_patterns": list(match.matched_patterns),
                            }
                            for match in matches
                        ],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        tiers = frozenset(args.tier) if args.tier else None
        for nodeid in select_test_nodeids(
            catalog,
            family_id=args.family,
            tiers=tiers,
        ):
            print(nodeid)
        return 0
    except RegressionCatalogError as exc:
        print(f"regression catalog error: {exc}", file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=DEFAULT_CATALOG_PATH,
        help="path to catalog.json",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="validate catalog structure and test references")
    classify = commands.add_parser(
        "classify",
        help="find existing problem-family candidates for a sanitized symptom",
    )
    classify.add_argument("symptom")
    tests = commands.add_parser("tests", help="print registered pytest node IDs")
    tests.add_argument("--family", help="select one regression family")
    tests.add_argument(
        "--tier",
        action="append",
        choices=("unit", "integration", "e2e"),
        help="select one or more test tiers",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
