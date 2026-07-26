"""Load, validate, classify, and select tests from the regression catalog."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CATALOG_PATH = REPO_ROOT / "backend" / "regressions" / "catalog.json"
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_TIERS = frozenset({"unit", "integration", "e2e"})
_CLASSIFICATIONS = frozenset({"new_family", "existing_family"})


class RegressionCatalogError(ValueError):
    """The catalog is structurally invalid or points at missing tests."""


@dataclass(frozen=True, slots=True)
class RegressionCase:
    id: str
    title: str
    tier: str
    protects: str
    test_nodeids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RegressionIncident:
    id: str
    detected_at: date
    classification: str
    symptom: str
    root_cause: str
    case_ids: tuple[str, ...]
    references: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RegressionFamily:
    id: str
    title: str
    invariant: str
    owners: tuple[str, ...]
    tags: tuple[str, ...]
    matchers: tuple[str, ...]
    cases: tuple[RegressionCase, ...]
    incidents: tuple[RegressionIncident, ...]


@dataclass(frozen=True, slots=True)
class RegressionCatalog:
    schema_version: int
    suite_id: str
    families: tuple[RegressionFamily, ...]


@dataclass(frozen=True, slots=True)
class ClassificationMatch:
    family_id: str
    matched_patterns: tuple[str, ...]


def load_catalog(
    path: Path = DEFAULT_CATALOG_PATH,
    *,
    repo_root: Path = REPO_ROOT,
) -> RegressionCatalog:
    """Load a catalog and fail closed on duplicates or broken test references."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegressionCatalogError(f"regression catalog does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RegressionCatalogError(
            f"regression catalog is not valid JSON at line {exc.lineno}"
        ) from exc
    root = _mapping(raw, "catalog")
    _keys(root, {"schema_version", "suite_id", "families"}, "catalog")
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise RegressionCatalogError("catalog.schema_version must equal 1")
    suite_id = _identifier(root["suite_id"], "catalog.suite_id")
    family_values = _sequence(root["families"], "catalog.families")
    if not family_values:
        raise RegressionCatalogError("catalog.families must not be empty")

    families: list[RegressionFamily] = []
    family_ids: set[str] = set()
    incident_ids: set[str] = set()
    matcher_owners: dict[str, str] = {}
    nodeid_owners: dict[str, str] = {}
    resolved_repo_root = repo_root.resolve()
    for position, value in enumerate(family_values):
        label = f"catalog.families[{position}]"
        family_id = _identifier(_mapping(value, label).get("id"), f"{label}.id")
        if family_id in family_ids:
            raise RegressionCatalogError(f"duplicate regression family id: {family_id}")
        family = _family(
            value,
            label=label,
            repo_root=resolved_repo_root,
            incident_ids=incident_ids,
        )
        family_ids.add(family.id)
        for matcher in family.matchers:
            normalized = _normalize_matcher(matcher)
            owner = matcher_owners.get(normalized)
            if owner is not None:
                raise RegressionCatalogError(f"matcher is already owned by another family: {owner}")
            matcher_owners[normalized] = family.id
        for case in family.cases:
            owner = f"{family.id}/{case.id}"
            for nodeid in case.test_nodeids:
                existing_owner = nodeid_owners.get(nodeid)
                if existing_owner is not None:
                    raise RegressionCatalogError(
                        f"test nodeid is already owned by another case: {existing_owner}"
                    )
                nodeid_owners[nodeid] = owner
        families.append(family)
    return RegressionCatalog(
        schema_version=1,
        suite_id=suite_id,
        families=tuple(families),
    )


def classify_incident(
    catalog: RegressionCatalog,
    symptom: str,
) -> tuple[ClassificationMatch, ...]:
    """Return deterministic family candidates; an empty result means manual new-family review."""

    if not symptom.strip():
        return ()
    matches: list[ClassificationMatch] = []
    for family in catalog.families:
        matched = tuple(
            matcher
            for matcher in family.matchers
            if re.search(matcher, symptom, flags=re.IGNORECASE) is not None
        )
        if matched:
            matches.append(ClassificationMatch(family.id, matched))
    return tuple(
        sorted(
            matches,
            key=lambda match: (-len(match.matched_patterns), match.family_id),
        )
    )


def select_test_nodeids(
    catalog: RegressionCatalog,
    *,
    family_id: str | None = None,
    tiers: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """Select stable, de-duplicated pytest node IDs for a family or the full catalog."""

    if tiers is not None:
        unknown = tiers - _TIERS
        if unknown:
            raise RegressionCatalogError(f"unknown regression tiers: {sorted(unknown)}")
    families = catalog.families
    if family_id is not None:
        families = tuple(family for family in families if family.id == family_id)
        if not families:
            raise RegressionCatalogError(f"unknown regression family id: {family_id}")
    selected: list[str] = []
    seen: set[str] = set()
    for family in families:
        for case in family.cases:
            if tiers is not None and case.tier not in tiers:
                continue
            for nodeid in case.test_nodeids:
                if nodeid not in seen:
                    seen.add(nodeid)
                    selected.append(nodeid)
    return tuple(selected)


def _family(
    value: Any,
    *,
    label: str,
    repo_root: Path,
    incident_ids: set[str],
) -> RegressionFamily:
    raw = _mapping(value, label)
    _keys(
        raw,
        {
            "id",
            "title",
            "invariant",
            "owners",
            "tags",
            "matchers",
            "cases",
            "incidents",
        },
        label,
    )
    family_id = _identifier(raw["id"], f"{label}.id")
    owners = _strings(raw["owners"], f"{label}.owners")
    tags = _strings(raw["tags"], f"{label}.tags")
    matchers = _strings(raw["matchers"], f"{label}.matchers")
    for matcher in matchers:
        try:
            re.compile(matcher, flags=re.IGNORECASE)
        except re.error as exc:
            raise RegressionCatalogError(
                f"{label}.matchers contains invalid regular expression: {matcher!r}"
            ) from exc

    cases: list[RegressionCase] = []
    case_ids: set[str] = set()
    for position, case_value in enumerate(_sequence(raw["cases"], f"{label}.cases")):
        case = _case(
            case_value,
            label=f"{label}.cases[{position}]",
            repo_root=repo_root,
        )
        if case.id in case_ids:
            raise RegressionCatalogError(f"duplicate regression case id in {family_id}: {case.id}")
        case_ids.add(case.id)
        cases.append(case)
    if not cases:
        raise RegressionCatalogError(f"{label}.cases must not be empty")

    incidents: list[RegressionIncident] = []
    new_family_count = 0
    referenced_case_ids: set[str] = set()
    for position, incident_value in enumerate(_sequence(raw["incidents"], f"{label}.incidents")):
        incident = _incident(
            incident_value,
            label=f"{label}.incidents[{position}]",
            case_ids=case_ids,
        )
        if incident.id in incident_ids:
            raise RegressionCatalogError(f"duplicate regression incident id: {incident.id}")
        incident_ids.add(incident.id)
        new_family_count += incident.classification == "new_family"
        referenced_case_ids.update(incident.case_ids)
        if not any(
            re.search(matcher, incident.symptom, flags=re.IGNORECASE) for matcher in matchers
        ):
            raise RegressionCatalogError(
                f"{label}.incidents[{position}].symptom does not match its family"
            )
        incidents.append(incident)
    if not incidents:
        raise RegressionCatalogError(f"{label}.incidents must not be empty")
    if new_family_count != 1:
        raise RegressionCatalogError(
            f"{label}.incidents must contain exactly one new_family classification"
        )
    unreferenced_cases = case_ids - referenced_case_ids
    if unreferenced_cases:
        raise RegressionCatalogError(
            f"{label}.cases are not referenced by an incident: {sorted(unreferenced_cases)}"
        )
    return RegressionFamily(
        id=family_id,
        title=_text(raw["title"], f"{label}.title"),
        invariant=_text(raw["invariant"], f"{label}.invariant"),
        owners=owners,
        tags=tags,
        matchers=matchers,
        cases=tuple(cases),
        incidents=tuple(incidents),
    )


def _case(value: Any, *, label: str, repo_root: Path) -> RegressionCase:
    raw = _mapping(value, label)
    _keys(raw, {"id", "title", "tier", "protects", "test_nodeids"}, label)
    tier = _text(raw["tier"], f"{label}.tier")
    if tier not in _TIERS:
        raise RegressionCatalogError(f"{label}.tier must be one of {sorted(_TIERS)}")
    nodeids = _strings(raw["test_nodeids"], f"{label}.test_nodeids")
    for nodeid in nodeids:
        _validate_nodeid(repo_root, nodeid)
    return RegressionCase(
        id=_identifier(raw["id"], f"{label}.id"),
        title=_text(raw["title"], f"{label}.title"),
        tier=tier,
        protects=_text(raw["protects"], f"{label}.protects"),
        test_nodeids=nodeids,
    )


def _incident(value: Any, *, label: str, case_ids: set[str]) -> RegressionIncident:
    raw = _mapping(value, label)
    _keys(
        raw,
        {
            "id",
            "detected_at",
            "classification",
            "symptom",
            "root_cause",
            "case_ids",
            "references",
        },
        label,
    )
    classification = _text(raw["classification"], f"{label}.classification")
    if classification not in _CLASSIFICATIONS:
        raise RegressionCatalogError(
            f"{label}.classification must be one of {sorted(_CLASSIFICATIONS)}"
        )
    linked_cases = _strings(raw["case_ids"], f"{label}.case_ids")
    missing = set(linked_cases) - case_ids
    if missing:
        raise RegressionCatalogError(
            f"{label}.case_ids references unknown cases: {sorted(missing)}"
        )
    detected = _text(raw["detected_at"], f"{label}.detected_at")
    try:
        detected_at = date.fromisoformat(detected)
    except ValueError as exc:
        raise RegressionCatalogError(f"{label}.detected_at must use YYYY-MM-DD") from exc
    return RegressionIncident(
        id=_identifier(raw["id"], f"{label}.id"),
        detected_at=detected_at,
        classification=classification,
        symptom=_text(raw["symptom"], f"{label}.symptom"),
        root_cause=_text(raw["root_cause"], f"{label}.root_cause"),
        case_ids=linked_cases,
        references=_strings(raw["references"], f"{label}.references"),
    )


def _validate_nodeid(repo_root: Path, nodeid: str) -> None:
    parts = nodeid.split("::")
    if len(parts) != 2 or not parts[0].startswith("backend/tests/"):
        raise RegressionCatalogError(f"test nodeid must be a backend test function path: {nodeid}")
    relative = Path(parts[0])
    if relative.is_absolute() or ".." in relative.parts:
        raise RegressionCatalogError(f"test nodeid escapes the repository: {nodeid}")
    path = (repo_root / relative).resolve()
    try:
        path.relative_to(repo_root)
    except ValueError as exc:
        raise RegressionCatalogError(f"test nodeid escapes the repository: {nodeid}") from exc
    if not path.is_file():
        raise RegressionCatalogError(f"test file does not exist: {nodeid}")
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        raise RegressionCatalogError(f"test file has invalid Python syntax: {nodeid}") from exc
    functions = {
        item.name for item in tree.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if parts[1] not in functions:
        raise RegressionCatalogError(f"test function does not exist: {nodeid}")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RegressionCatalogError(f"{label} must be an object")
    return value


def _sequence(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise RegressionCatalogError(f"{label} must be an array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegressionCatalogError(f"{label} must be a non-empty string")
    return value.strip()


def _identifier(value: Any, label: str) -> str:
    identifier = _text(value, label)
    if _ID_PATTERN.fullmatch(identifier) is None:
        raise RegressionCatalogError(f"{label} is not a stable identifier: {identifier!r}")
    return identifier


def _strings(value: Any, label: str) -> tuple[str, ...]:
    raw = _sequence(value, label)
    values = tuple(_text(item, f"{label}[]") for item in raw)
    if not values:
        raise RegressionCatalogError(f"{label} must not be empty")
    if len(values) != len(set(values)):
        raise RegressionCatalogError(f"{label} contains duplicate values")
    return values


def _keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        raise RegressionCatalogError(
            f"{label} fields do not match the catalog contract; "
            f"missing={sorted(missing)}, extra={sorted(extra)}"
        )


def _normalize_matcher(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()
