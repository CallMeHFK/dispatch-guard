# Changelog

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
