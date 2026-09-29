# Changelog

## v0.1.2

- **The repository no longer ships a dispatch table.** `routes.json` is private
  operator data (agent ids, internal system names, deployment shape) and is now
  gitignored; the repo carries `routes.example.json` with placeholder agent ids
  only. `build_plugin_zip.py` refuses to build if a real table is present.
- **New `unconfigured` mode** for installs with no `routes.json`: the middleware
  attaches, never denies, appends a block telling the agent to build the table
  with the user, logs one host-side warning per process, and records
  `needs_config` in the audit trail. Previously this state detached silently —
  a fresh install reported success while enforcing nothing.
- Audit records now carry the mode actually in effect for that call instead of
  the mode snapshotted when the middleware attached.
- Denial text no longer names an individual approver; it says "confirm with the
  user".
- Docs: first-run configuration added to the README; `docs/DESIGN.md` splits
  "missing config" (onboarding, visible) from "unreadable config" (fail open).

## v0.1.1

- Plugin-tree whitelist entries now match **absolute paths only**. Previously a
  workspace-relative target could resolve (against cwd) into the plugin parent
  directory and be silently whitelisted (e.g. when the dev repo sits under
  `PLUGIN_DIR.parent`).
- Production verification record (2026-09-29, enforce mode):
  - 10-case false-positive matrix clean: whitelist/plugin-tree/trash writes
    pass silently; `outputs/`+`projects/` writes denied with dispatch hints;
    ASCII word boundaries hold; read-only or mention-only shell commands never warn.
  - E2E: in-domain dispatch 3/5 (self-write first denied, then routed),
    micro/QA cases 0 dispatches, 0 `spawn_subagent` calls, 0 unsafe self-actions
    (warn baseline had root-dir self-writes and 172 browser touches on one case).
  - Known warn-only trade-off documented: shell heuristic is content-based, so
    bookkeeping heredocs that *mention* a deliverable extension emit a
    non-blocking warning (logged, never denied).

## v0.1.0

- Initial release: `on_acting` middleware that enforces an orchestrator agent's
  output routing at the tool seam.
- `write_file` / `edit_file` / `append_file` to non-whitelisted paths are denied
  in `enforce` mode with a dispatch hint naming the owning agent
  (`routes.json`: keyword + extension routing).
- Shell deliverable heuristics (redirects / pandoc / ffmpeg into `outputs/`,
  `projects/` or deliverable extensions) are warn-only — appended as a
  `ToolResponse` text block, never blocking.
- `spawn_subagent` denial as a backstop for the config-layer disable.
- Whitelist: orchestration/meta paths (`memory/`, `notes/`, `wiki/`,
  `episodes/`, `tmp/`, `trash/`, `logs/`, top-level baseline MDs,
  `skill.json`, `agent.json`) plus `~/.qwenpaw/plugins/` (the guard must not
  block its own maintainer).
- `mode` (`enforce` / `warn` / `off`) hot-reloaded via `routes.json` mtime;
  `off` removes the middleware from the chain entirely.
- Native-tool inputs arrive as JSON strings — parsed before path extraction
  (fixed a deny-everything bootstrap bug found on first live contact).
- Pure-ASCII route keywords use word-boundary matching so `CI` no longer
  matches inside `asyncio`; CJK keywords stay substring.
- Middleware only attaches to the `default` agent; other agents unaffected.
- Stdlib-only runtime path; AgentScope/QwenPaw imports degrade to local
  stand-ins so the test suite runs anywhere.
