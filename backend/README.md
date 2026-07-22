# Nico Agent backend

The backend package contains the FastAPI control plane, persistent Run Worker, Nico Native and optional Runtime adapters, dynamic Parent/Child coordination, private Artifact services, Tool Gateway, Sandbox Runner, tenant-scoped persistence, and Memory/Skill governance.

Start data dependencies in Compose and all application services from source:

```bash
make run
```

This also synchronizes and links the editable `nico` CLI. The development path
does not build Nico application images and stops stale containerized application
services while preserving Compose infrastructure and volumes. Use
`scripts/bootstrap.sh && scripts/dev.sh --detach` only when validating the full
containerized stack.

See the root [README](../README.md), [Architecture](../docs/architecture.md), and [Contributing Guide](../CONTRIBUTING.md).
