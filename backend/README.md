# Nico Agent backend

The backend package contains the FastAPI control plane, persistent Run Worker, Nico Native and optional Runtime adapters, dynamic Parent/Child coordination, private Artifact services, Tool Gateway, Sandbox Runner, tenant-scoped persistence, and Memory/Skill governance.

Start the complete stack from the repository root:

```bash
cp .env.example .env
scripts/dev.sh --detach
```

See the root [README](../README.md), [Architecture](../docs/architecture.md), and [Contributing Guide](../CONTRIBUTING.md).
