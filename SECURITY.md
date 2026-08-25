# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

Toolshed sits between an agent and its tool surface — security reports are
taken seriously.

**Please do NOT open a public issue for security vulnerabilities.**

Instead:

1. Use GitHub's private vulnerability reporting
   (Repository → Security → Report a vulnerability), or
2. Contact the maintainer directly via a private channel.

Include:

- affected version (`toolshed.__version__` or release tag),
- Hermes Agent version tested against,
- minimal reproduction steps,
- expected vs actual behavior.

You will receive an initial response within 7 days.

## Security Model

- Toolshed requires the explicit `tools.override` capability grant
  (`hermes plugins enable hermes-token-router --allow-tool-override`).
- **Install ≠ authorization.** Without the grant, Toolshed stays inactive
  (fail-closed) and never manipulates the tool surface.
- Routing errors within granted permissions degrade to the built-in
  recovery path (`request_toolset`, plus automatic middleware recovery for
  registered-but-filtered tools), not to functionality loss.
- **Scope note:** the recovery paths check registry existence, not permissions.
  Toolshed is a routing/context-efficiency layer, not an authorization boundary —
  access control remains Hermes' job.
- Malicious text in repositories, issues, tool descriptions, or prompts
  cannot expand grants or change routing policy.

See `adr/` for design decisions and threat model details.
