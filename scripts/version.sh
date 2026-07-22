#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYPROJECT="$ROOT_DIR/backend/pyproject.toml"
PYTHON="${PYTHON:-python3}"

die() {
  printf '[nico-version] ERROR: %s\n' "$*" >&2
  exit 1
}

current_version() {
  "$PYTHON" - "$PYPROJECT" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as source:
    print(tomllib.load(source)["project"]["version"])
PY
}

validate_version() {
  local version="$1"
  [[ "$version" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || \
    die "version must use stable SemVer syntax: X.Y.Z"
}

check_tag() {
  local candidate="$1"
  local version
  version="$(current_version)"
  validate_version "$version"
  [[ "$candidate" == "v$version" ]] || \
    die "tag $candidate differs from the package version v$version"
}

sanitize_development_identifier() {
  "$PYTHON" - "$1" <<'PY'
import re
import sys

value = re.sub(r"[^A-Za-z0-9_.-]+", "-", sys.argv[1]).strip(".-").lower()
if not value:
    value = "unknown"
print(value[:128].rstrip(".-"))
PY
}

development_version() {
  local branch commit identifier
  branch="$(git -C "$ROOT_DIR" branch --show-current 2>/dev/null || true)"
  [[ -n "$branch" ]] || branch=detached
  commit="$(git -C "$ROOT_DIR" describe --always --dirty --abbrev=12 2>/dev/null || true)"
  [[ -n "$commit" ]] || commit=unknown
  if [[ "$commit" != *-dirty && \
        -n "$(git -C "$ROOT_DIR" status --porcelain --untracked-files=normal 2>/dev/null)" ]]; then
    commit="$commit-dirty"
  fi
  identifier="$branch-$commit"
  if [[ -n "${DTN_SUB:-}" ]]; then
    identifier="$identifier-$DTN_SUB"
  fi
  sanitize_development_identifier "$identifier"
}

set_version() {
  local version="$1"
  validate_version "$version"
  "$PYTHON" - "$PYPROJECT" "$version" <<'PY'
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

path = Path(sys.argv[1])
version = sys.argv[2]
source = path.read_text(encoding="utf-8")
project_start = source.find("[project]")
if project_start < 0:
    raise SystemExit("[project] table is missing")
next_table = source.find("\n[", project_start + len("[project]"))
if next_table < 0:
    next_table = len(source)
prefix = source[:project_start]
project = source[project_start:next_table]
suffix = source[next_table:]
project, replacements = re.subn(
    r'(?m)^version\s*=\s*"[^"]+"$',
    f'version = "{version}"',
    project,
)
if replacements != 1:
    raise SystemExit("expected exactly one project.version field")
mode = stat.S_IMODE(path.stat().st_mode)
with tempfile.NamedTemporaryFile(
    mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
) as destination:
    temporary = Path(destination.name)
    destination.write(prefix + project + suffix)
    destination.flush()
    os.fsync(destination.fileno())
temporary.chmod(mode)
os.replace(temporary, path)
directory_fd = os.open(path.parent, os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
  printf '[nico-version] package version set to %s; commit this change before tagging\n' "$version"
}

default_branch() {
  local branch
  branch="$(git -C "$ROOT_DIR" symbolic-ref refs/remotes/origin/HEAD 2>/dev/null || true)"
  if [[ -n "$branch" ]]; then
    printf '%s\n' "${branch##*/}"
  elif git -C "$ROOT_DIR" show-ref --verify --quiet refs/heads/main; then
    printf 'main\n'
  else
    printf 'master\n'
  fi
}

create_tag() {
  git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 || \
    die "release tags require a Git worktree"

  local version tag branch expected_branch
  version="$(current_version)"
  validate_version "$version"
  tag="v$version"
  branch="$(git -C "$ROOT_DIR" branch --show-current)"
  expected_branch="$(default_branch)"

  [[ "$branch" == "$expected_branch" ]] || \
    die "release tags must be created from $expected_branch, not $branch"
  [[ -z "$(git -C "$ROOT_DIR" status --porcelain)" ]] || \
    die "release tags require a clean worktree"
  if git -C "$ROOT_DIR" rev-parse --verify --quiet "refs/tags/$tag" >/dev/null; then
    die "tag already exists: $tag"
  fi

  git -C "$ROOT_DIR" remote get-url origin >/dev/null 2>&1 || \
    die "release tags require an origin remote"
  git -C "$ROOT_DIR" fetch --quiet --no-tags origin \
    "$expected_branch:refs/remotes/origin/$expected_branch" || \
    die "could not refresh origin/$expected_branch"
  [[ "$(git -C "$ROOT_DIR" rev-parse HEAD)" == \
     "$(git -C "$ROOT_DIR" rev-parse "origin/$expected_branch")" ]] || \
    die "HEAD must exactly match the refreshed origin/$expected_branch before tagging"

  local remote_tag
  remote_tag="$(git -C "$ROOT_DIR" ls-remote --tags origin "refs/tags/$tag")" || \
    die "could not check remote tag $tag"
  [[ -z "$remote_tag" ]] || die "tag already exists on origin: $tag"

  git -C "$ROOT_DIR" tag --annotate "$tag" --message "Release $tag"
  printf '[nico-version] created annotated tag %s; publish with: git push origin %s\n' \
    "$tag" "$tag"
}

show_status() {
  local version tag branch commit state head_tag development
  version="$(current_version)"
  validate_version "$version"
  tag="v$version"
  branch="$(git -C "$ROOT_DIR" branch --show-current 2>/dev/null || printf 'none')"
  commit="$(git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || printf 'none')"
  state=clean
  [[ -z "$(git -C "$ROOT_DIR" status --porcelain 2>/dev/null || true)" ]] || state=dirty
  head_tag="$(git -C "$ROOT_DIR" tag --points-at HEAD 2>/dev/null | paste -sd, -)"
  [[ -n "$head_tag" ]] || head_tag=none
  development="$(development_version)"
  printf 'version=%s\ntag=%s\ndevelopment=%s\nbranch=%s\ncommit=%s\nworktree=%s\nhead_tag=%s\n' \
    "$version" "$tag" "$development" "$branch" "$commit" "$state" "$head_tag"
}

command="${1:-status}"
case "$command" in
  current) current_version ;;
  tag) printf 'v%s\n' "$(current_version)" ;;
  development) development_version ;;
  status) show_status ;;
  check-tag)
    (($# == 2)) || die "usage: scripts/version.sh check-tag vX.Y.Z"
    check_tag "$2"
    printf '[nico-version] validated %s\n' "$2"
    ;;
  set)
    (($# == 2)) || die "usage: scripts/version.sh set X.Y.Z"
    set_version "$2"
    ;;
  create-tag)
    (($# == 1)) || die "usage: scripts/version.sh create-tag"
    create_tag
    ;;
  *) die "unknown command: $command" ;;
esac
