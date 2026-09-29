# Contributing to dispatch-guard

## What this plugin is for

dispatch-guard exists to move one decision out of the prompt and into the tool
seam: *who owns a deliverable*. Changes that keep that seam narrow are welcome;
changes that turn it into a general policy engine are usually better as a fork,
because every new blocking rule is a new way to strand a legitimate write.

## Development setup

Prerequisites: Python ≥ 3.10. There are no runtime or test dependencies — the
plugin is stdlib-only and `backend/main.py` degrades its AgentScope/QwenPaw
imports to local stand-ins, so the suite runs on a bare interpreter.

```bash
git clone https://github.com/CallMeHFK/dispatch-guard.git
cd dispatch-guard
python -m unittest discover -s tests -v
python packaging/build_plugin_zip.py      # dist/dispatch-guard-qwenpaw-plugin.zip
```

Try it against a real host by copying the checkout into the plugin directory
(`~/.qwenpaw/plugins/dispatch-guard/`, or
`%USERPROFILE%\.qwenpaw\plugins\dispatch-guard\` on Windows) and restarting QwenPaw.

## Repository layout

| Path | Role |
|---|---|
| `plugin.json` | QwenPaw manifest; `entry.backend` points at `backend/main.py` |
| `backend/main.py` | the whole middleware: whitelist, modes, route matching, audit log |
| `routes.example.json` | sanitized sample dispatch table — the only table in the repo |
| `routes.json` | **your** real dispatch table; gitignored, never committed |
| `packaging/build_plugin_zip.py` | archive builder + the guard that refuses to ship `routes.json` |
| `tests/` | middleware behaviour (`test_middleware.py`), publish hygiene (`test_packaging.py`) |
| `docs/DESIGN.md` | why the seam looks like this, including the fail-open decisions |

## Secrets (strict rule)

The dispatch table is the interesting data in this repository, and it is nobody's
business but the operator's. It names your agents, the internal systems they own,
and the shape of your deployment.

- **Never commit `routes.json`**, and never paste a real one into an issue, PR,
  commit message, doc, or test fixture. It is gitignored; if `git status` offers
  to add it, stop and check why.
- Sample tables, README examples, and test fixtures use placeholder agent ids
  (`DocAgent`, `CodeAgent`, `HardwareAgent`, `DesignAgent`, `OpsAgent`, `LifeAgent`).
  Keep it that way.
- Denial and warning strings must not name a person, a team, an internal host, or
  an internal system. Say "the user" and "the owning agent".
- `build_plugin_zip.py` refuses to build when a real table is present. Do not
  weaken that check to make a local build pass — move the file instead.
- Audit logs (`<workspace>/logs/dispatch_guard.jsonl`) record real tool targets.
  Sanitize before attaching one to a bug report.

## Tests

Every behaviour change needs a test that fails without it. Two conventions worth
knowing before you write one:

- Tests write a real `routes.json` into a temp plugin tree and repoint
  `dg.PLUGIN_DIR` there, because `_load_config()` stats the file before consulting
  its cache. Injecting only `_CONFIG_CACHE` silently yields the `unconfigured` mode.
- The temp plugin tree must sit **outside** the temp workspace, mirroring how the
  host installs plugins; otherwise `_rel()` rewrites plugin paths into relative
  ones and the absolute-path-only plugin whitelist never fires.

## Commit & pull request guidelines

- Branch names: `fix/<topic>`, `feat/<topic>`, `docs/<topic>`.
- Commit messages: imperative summary, body for the *why*. One logical change per commit.
- Bump `version` in `plugin.json` and add a `### vX.Y.Z` section to
  `docs/CHANGELOG.md` in the same PR; cutting a tag publishes the archive.
- Use the provided issue and PR templates.

## Where to ask

Open an issue. Design questions ("why does shell stay warn-only?") belong in
`docs/DESIGN.md` discussion, not in chat logs.
