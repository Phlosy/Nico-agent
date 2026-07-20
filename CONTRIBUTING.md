# Contributing to Nico Agent

Thanks for helping improve Nico Agent. The project is experimental and its API and database contracts may still change.

The repository does not yet include an open-source license. Before making a substantial contribution, review the [license status](README.md#license) and confirm that you are comfortable contributing while the project owner decides the long-term license.

## Prerequisites

- Docker Engine and Docker Compose v2
- Python 3.11 or newer
- Node.js 20 and npm
- `curl`

## Set up the repository

```bash
cp .env.example .env
scripts/bootstrap.sh
scripts/dev.sh --detach
```

The API container applies Alembic migrations before starting. Check the stack with:

```bash
curl http://localhost:18000/api/v1/health/ready
docker compose ps
```

Stop the stack without deleting data:

```bash
scripts/cleanup.sh
```

## Tests and formatting

Run the backend lint/format checks, backend unit tests, frontend component tests, and frontend production build:

```bash
scripts/test.sh
```

Run tests against real PostgreSQL, Redis, and MinIO dependencies:

```bash
scripts/test-integration.sh
```

Run the Compose health smoke test:

```bash
scripts/e2e.sh
```

Format Python code before opening a Pull Request:

```bash
.venv/bin/ruff format backend
.venv/bin/ruff check --fix backend
```

There is no standalone frontend lint or formatting command in the current package configuration. `npm --prefix frontend test` and `npm --prefix frontend run build` are the authoritative frontend checks.

## Report bugs and request features

- Use the [Bug report](https://github.com/Phlosy/Nico-agent/issues/new?template=bug_report.yml) form for reproducible incorrect behavior.
- Use the [Feature request](https://github.com/Phlosy/Nico-agent/issues/new?template=feature_request.yml) form for proposals.
- Follow [SECURITY.md](SECURITY.md) for vulnerabilities; never report them in a public Issue.

GitHub Discussions is not enabled for this repository, so usage questions currently use a sanitized GitHub Issue.

## Pull Requests

1. Keep each change focused on one user-visible outcome.
2. Add tests for behavior changes and update public documentation when contracts change.
3. List exact validation commands in the Pull Request description.
4. Do not commit `.env`, `.nico/`, credentials, tenant data, private prompts, database dumps, or runtime workspaces.
5. Discuss changes to domain models, migrations, Runtime contracts, Tool Gateway policy, tenant isolation, or security boundaries in an Issue before implementation.

Use a concise value-oriented title. Preferred forms are:

- `feat(runtime): add <capability>`
- `fix(tool-gateway): prevent <failure>`
- `docs: clarify <user workflow>`
- `test(memory): cover <boundary>`

Commit messages may use the same convention. Avoid Goal numbers, session details, or implementation chronology in public-facing titles.

## Good first contributions

Good starting areas include:

- user documentation and troubleshooting;
- Console accessibility and responsive behavior;
- tests for existing API contracts;
- examples that use the Mock Runtime;
- clearer error messages that do not expose secrets.

Core state machines, database migrations, Runtime recovery, sandboxing, authorization, and Memory/Skill publication require a design discussion first because mistakes can break persistence or security boundaries.

By submitting a contribution, you confirm that you have the right to submit it. A formal contribution license policy may be added after the project owner selects a repository license.
