# Using Selvedge with coding agents

Selvedge is an **MCP server first** — that's the default integration and what
`selvedge setup` wires up. It also exposes the *same* local store through a
`selvedge` CLI, and for an agent that has a shell, being able to reach for the
CLI directly is valuable. This page covers when an agent uses which.

## Two paths to the same store

- **MCP server (default).** The agent calls Selvedge's tools (`log_change`,
  `prior_attempts`, `diff`, `blame`, …). Best for discoverability and for hosts
  without a shell. This is what most users install.
- **CLI.** The agent runs `selvedge …` on its shell. Useful when the MCP server
  isn't loaded, in a shell-only subagent, or to avoid loading MCP tool
  definitions up front. CLI instructions and command output still use context;
  the MCP footprint is measured below, and actual loading depends on the host.

Neither replaces the other. MCP is the front door; the CLI is there so a
shell-having agent is never blocked and can choose the cheaper path.

> Measure the MCP footprint from a source checkout:
> `python scripts/schema_tax.py --json`. See [Keeping the footprint honest](#keeping-the-footprint-honest)
> for the measured baseline, tokenizer and limits of the estimate.

## Letting an agent use the CLI

After `pip install selvedge` the `selvedge` command is on PATH, so any agent
with shell access can already call it. To make the agent actually *reach for
it*, the canonical agent-instructions block (installed by `selvedge setup` /
`selvedge prompt`) names the CLI equivalents alongside the MCP tools — see
[the canonical prompt source](../selvedge/prompt.py). Run `selvedge prompt` to
print the instructions for your installed version. The two operations that matter:

**Check before editing a meaningful entity** (DB column, function contract, env
var, dependency, auth/session mechanism):

```bash
selvedge blame users.auth_token      # most recent change + the reasoning behind it
selvedge diff users.auth_token       # full change history for the entity
```

**Log the why after a meaningful change:**

```bash
selvedge log users.auth_token retype \
  --reasoning "Per-user DB token couldn't be revoked without a write; moved to short-lived JWTs."
```

`change_type` is one of `add, remove, modify, rename, retype, create, delete,
index_add, index_remove, migrate, supersede, revert, reject`; for a rename add
`--rename-from OLD`. Use `reject` when an approach was considered and decided
against, `revert` when a change was tried and rolled back, and `supersede` to
explicitly reopen a previous verdict. Add `--json` to any read command for
machine-readable output.

## Keeping the footprint honest

The v0.3.16 baseline measures **4,504 core tokens** across eight tools using
`tiktoken/o200k_base`, or **5,509 tokens** including output schemas, annotations
and titles. These are offline proxy counts, not a universal cost for every
model or host. Without `tiktoken`, the script falls back to a character-based
estimate; check the reported `counter` before comparing numbers.

`python scripts/schema_tax.py --max 4600` is the current CI drift guard for the
core total (name, description and input schema), as recorded in the
[architecture plan](architecture.md). It fails if the measured core count
exceeds 4,600; it does not budget the full schema total. Remeasure when tools or
their descriptions change. For an Anthropic model-specific count, the optional
`--backend api` uses Anthropic's `count_tokens` endpoint and requires its SDK
and an API key.

## Native lifecycle hooks

`selvedge setup --agent AGENT` also offers the supported project hooks for that
client. See [agent lifecycle integrations](agent-hooks.md) for the capability
matrix, configuration paths, trust/activation and a disposable verification
workflow. MCP connectivity and an installed hook file are separate from proof
that the client has run a hook.
