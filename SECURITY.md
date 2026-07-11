# Security Policy

SpeakesQuery is designed as inspectable infrastructure.
Security is treated as an engineering responsibility, not a feature.

## Supported versions

Only the current main branch and the most recent tagged release are actively supported with security updates.

Older versions may contain known vulnerabilities and should not be relied upon in sensitive environments.

## Reporting a vulnerability

If you discover a security vulnerability, please report it responsibly.

**Do not open a public issue for security findings.**

Instead, contact the project author directly with:

- A clear description of the issue
- Steps to reproduce (if applicable)
- Impact assessment (data exposure, code execution, denial of service, etc.)
- Any suggested mitigations or patches

Contact method:
- GitHub private security advisory **or**
- Direct contact via the repository owner’s listed email (if available)

You will receive an acknowledgment as soon as practicable.

## Disclosure process

- Valid security reports will be investigated promptly.
- Fixes will be prioritized based on severity and exploitability.
- Public disclosure will occur **after** a fix is available or mitigation guidance is published.
- Credit will be given to reporters unless anonymity is requested.

## Security scope

In scope:
- Query execution and parsing
- File ingestion and index handling
- Authentication and authorization logic
- Background workers and schedulers
- Native / C++ extensions
- Environment variable handling
- File upload and parsing logic

Out of scope:
- User-managed deployment misconfiguration
- Compromised host systems
- Third-party dependencies outside their documented behavior

## Secure usage expectations

Operators are responsible for:
- Running SpeakesQuery in a trusted environment
- Restricting filesystem access appropriately
- Protecting environment variables and secrets
- Applying OS-level hardening and network controls
- Reviewing ingestion scripts before execution

SpeakesQuery intentionally avoids executing arbitrary shell commands and enforces directory and input restrictions where possible, but no system is secure if deployed carelessly.

## Philosophy

Security through obscurity is rejected.

SpeakesQuery favors:
- Transparency
- Auditable behavior
- Explicit trust boundaries
- Predictable failure modes

Security improvements are welcome contributions.
