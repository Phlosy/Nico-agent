# Security Policy

Nico Agent is experimental software. It does not currently provide formal control-plane authentication and must not be exposed directly to the public Internet.

## Supported versions

Nico has no published releases or production support channel yet.

| Code line | Security reports |
| --- | --- |
| Current `main` branch | Accepted on a best-effort basis |
| Older commits, forks, and unpublished builds | Not supported |

Security fixes may include incompatible API or database changes while the project remains experimental.

## Report a vulnerability privately

Do not disclose vulnerabilities, exploit details, credentials, tenant data, or private prompts in a public Issue or Pull Request.

Use [GitHub Private Vulnerability Reporting](https://github.com/Phlosy/Nico-agent/security/advisories/new) to contact the repository owner privately. Include:

- the affected commit and deployment mode;
- impact and the security boundary that is crossed;
- minimal reproduction steps or a proof of concept;
- whether credentials or tenant data may have been exposed;
- any known mitigation.

If GitHub does not offer the private reporting form, stop before publishing details. The project owner must enable Private Vulnerability Reporting or publish a dedicated security contact. No verified security email is currently available.

## Response process

The maintainer will, without a guaranteed service-level objective:

1. acknowledge the report through the private channel;
2. reproduce and assess affected boundaries;
3. coordinate a fix and a safe disclosure plan with the reporter;
4. add regression coverage where practical;
5. publish a GitHub Security Advisory when public disclosure is appropriate.

## Known deployment limitations

- The public API has no API Key, JWT, or equivalent production identity provider.
- `X-Tenant-ID` and `X-Actor-ID` are local/test context headers, not authentication.
- The supplied Compose stack runs the API in local mode and is intended for localhost or a trusted network.
- `sandbox-runner` mounts the Docker Socket and must be treated as a host-privileged service.
- The Python sandbox narrows untrusted code execution but is not a complete host isolation boundary.
- Hermes model execution depends on separately installed CLI software and provider credentials; it has not been validated as a production deployment in this repository.
- Kubernetes manifests, network policies, backup/restore automation, rate limits, and production secret management are not provided.

See [docs/security.md](docs/security.md) for the current threat boundaries and deployment guidance.
