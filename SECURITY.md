# Security Policy

## Supported versions

Only the latest 0.3.x release receives security fixes. Older minor
versions are not patched — upgrade to the latest release before
reporting.

| Version | Supported |
|---------|-----------|
| latest 0.3.x | ✅ |
| < latest 0.3.x | ❌ |

## Reporting a vulnerability

Please report vulnerabilities privately — do not open a public issue.

- **Preferred:** [GitHub private vulnerability reporting](https://github.com/masondelan/selvedge/security/advisories/new)
  on `masondelan/selvedge`.
- **Email:** hello@selvedge.sh

You will receive an acknowledgement within 72 hours.

## Scope and threat model

Selvedge's core is a local-only tool:

- All data lives in a SQLite file under `.selvedge/` next to your code
  (or `~/.selvedge/` as a fallback). No code, paths, diffs, or reasoning text
  is sent by the core database or MCP server. The CLI has two separate,
  bounded network paths, neither carrying that data:
  - an interactive stable-install version check against PyPI, at most once per
    24 hours, which reads only the package's latest version and can be disabled
    with `SELVEDGE_NO_UPDATE_CHECK=1` (also suppressed by `SELVEDGE_QUIET=1`,
    `CI`, non-TTY runs, and development installs);
  - an anonymous heartbeat that is off unless you explicitly enable it (see
    `docs/telemetry.md`).
- The opt-in heartbeat contains only its documented schema fields. It does not
  include repository names, paths, diffs, reasoning, prompts, command
  arguments, environment variables, or agent-client conversation data.
- No network listeners. An opt-in HTTP layer is planned for v0.4.0;
  until then there is no remote attack surface.
- No LLM calls in core — output is templated and deterministic.
- Three runtime dependencies: `mcp`, `click`, `rich`.

The most interesting attack surface is input handling: SQL handling of
agent-supplied strings (everything an MCP client sends is untrusted)
and path canonicalization of entity paths. Reports in those areas are
especially welcome.

### Client, sharing, and website boundaries

Claude Code, Cursor, Copilot, and other MCP clients invoke Selvedge locally;
their own prompts, network behavior, sync settings, and account controls are
outside Selvedge's data path. If a user commits or copies `.selvedge/`, that
sharing is an explicit project or team operation and follows the receiving
system's access controls.

The public site is documentation and launch content, not a product telemetry
receiver. Its page views, outbound-link clicks, Product Hunt or DevHunt
metrics, and trial reports are separate reach signals; they do not prove an
installation, use of the local database, or adoption. The site does not change
the core data-flow guarantees above.
