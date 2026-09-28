# dispatch-guard

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/CallMeHFK/dispatch-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/CallMeHFK/dispatch-guard/actions/workflows/ci.yml)
[![QwenPaw](https://img.shields.io/badge/QwenPaw-2.2.x-green)](https://github.com/agentscope-ai/QwenPaw)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)

QwenPaw middleware plugin that **enforces an orchestrator agent's output routing
at the tool seam**. When the orchestrator (default agent) tries to write a
deliverable itself instead of dispatching to the owning specialist agent,
dispatch-guard denies the write and tells it whom to dispatch to. Shell
deliverable heuristics are warn-only, and `spawn_subagent` is denied as a
backstop for the config-layer disable. Stdlib-only, no pip dependencies, no
telemetry.

```bash
qwenpaw plugin install https://github.com/CallMeHFK/dispatch-guard/releases/latest/download/dispatch-guard-qwenpaw-plugin.zip
```

<details>
<summary>Contents</summary>

- [Why](#why)
- [Quick start](#quick-start)
- [How it works](#how-it-works) — three interception rules, modes, routing hints
- [Whitelist](#whitelist)
- [Configuration](#configuration) — `routes.json`
- [Logging](#logging)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Privacy](#privacy) · [Changelog](#changelog) · [License](#license)

</details>

## Why

Multi-agent hosts tend to grow a "default" agent that carries dozens of domain
skills and a `spawn_subagent` tool — and because doing the work itself is always
the cheapest-looking path, the dispatch rules in its system prompt lose to the
tool list. Prompt-level fixes (stronger wording, more rules) decay; this plugin
moves enforcement **from the prompt to the tool seam**: even if the model
decides to self-execute, the write never lands, and the denial message tells it
exactly which agent should receive the task instead.

It exists to fix a specific failure pattern observed in production: a default
agent that wrote patent disclosures, drew diagrams, and ran domain queries
itself despite a dispatch table saying otherwise.

## Quick start

### 1 · Install

```bash
qwenpaw plugin install https://github.com/CallMeHFK/dispatch-guard/releases/latest/download/dispatch-guard-qwenpaw-plugin.zip
```

Or clone into `~/.qwenpaw/plugins/dispatch-guard/` — QwenPaw discovers
`plugin.json` at startup.

### 2 · Verify

```bash
qwenpaw plugin list        # dispatch-guard should appear
qwenpaw plugin validate ~/.qwenpaw/plugins/dispatch-guard
```

Then, in a session with the default agent, ask it to write a deliverable path,
e.g. "用 write_file 把 hello 写入 outputs/probe.docx". With the shipped default
mode (`warn`) the write succeeds and a warning block is appended; flip
`mode` to `enforce` in `routes.json` and the same attempt is denied with a
dispatch hint.

## How it works

The plugin registers one AgentScope `MiddlewareBase` factory
(`api.register_middleware`, priority 40). The factory only returns an instance
when `agent_config.id == "default"` — every other agent is untouched.

`on_acting` inspects each tool call and applies three rules:

| Tool call | `enforce` | `warn` | `off` |
|---|---|---|---|
| `write_file` / `edit_file` / `append_file` to a non-whitelisted path | **Denied** with "this output belongs to {agent} — dispatch via `submit_to_agent`" | Passes, warning block appended to the `ToolResponse` | Middleware not attached |
| `execute_shell_command` matching deliverable heuristics (redirects, `pandoc -o`, `ffmpeg -o` into `outputs/`, `projects/` or deliverable extensions) | Passes, warning block appended — shell heuristics **never block** (status checks must stay cheap) | same | not attached |
| `spawn_subagent` / `spawn_agent` | **Denied** (backstop for the config-layer disable) | denied | not attached |

Mode is read from `routes.json` and cached by file mtime — editing the file
takes effect without a restart. `off` removes the middleware from the chain
entirely.

Routing hints: `routes.json` maps extensions (`ext_routes`) and keywords
(`routes`) to agent ids, so the denial names a concrete owner
("该产出属于 IPP 域") instead of a generic refusal. Pure-ASCII keywords match
on word boundaries (`CI` does not fire inside `asyncio`); CJK keywords match as
substrings.

## Whitelist

Writes under these workspace paths are always allowed — they are the
orchestrator's own bookkeeping, not deliverables:

- `memory/`, `notes/`, `wiki/`, `episodes/`, `tmp/`, `trash/`, `logs/`
- Top-level `MEMORY.md`, `PROFILE.md`, `AGENTS.md`, `SOUL.md`, `HEARTBEAT.md`,
  `skill.json`, `agent.json`
- `~/.qwenpaw/plugins/` — the plugin tree itself (the guard must not block its
  own maintainer; absolute paths only, so workspace-relative paths can never
  accidentally resolve into it)

Everything else (`outputs/`, `projects/`, deliverable extensions anywhere) is
treated as a deliverable.

## Configuration

`routes.json` next to `plugin.json`:

```jsonc
{
  "mode": "warn",            // enforce | warn | off  (hot-reloaded by mtime)
  "routes": [                // keyword -> owning agent (CJK substring, ASCII word-boundary)
    {"match": ["专利", "patent", "docx"], "agent": "IPP"},
    {"match": ["code", "refactor", "CI"], "agent": "Codex_Agent"}
  ],
  "ext_routes": {            // extension -> owning agent (checked first)
    ".docx": "IPP", ".py": "Codex_Agent", ".svg": "Designer"
  }
}
```

Edit the match lists to mirror your own dispatch table. Unmatched denials fall
back to a generic "对应专业 agent" hint.

## Logging

Every interception is appended to `<workspace>/logs/dispatch_guard.jsonl`:

```json
{"ts": 1790592647.6, "plugin": "dispatch-guard", "action": "denied",
 "tool": "write_file", "target": "outputs/probe.docx", "mode": "enforce",
 "message": "该产出属于 IPP 域（命中 ext .docx），请用 submit_to_agent 派发给 IPP；…"}
```

`action` is `denied` or `warned`. This file is the audit trail for measuring
how often the orchestrator still tries to self-execute after prompting fixes.

## Troubleshooting

- **Everything gets denied, even whitelisted paths.** You are running the
  pre-0.1.0 logic where native-tool inputs (JSON strings) were not parsed
  before path extraction. Upgrade to ≥0.1.0.
- **Legitimate infrastructure edits are denied.** The path is outside the
  whitelist. Either it is a deliverable (dispatch it), or it is a new
  meta-location (add it to `WHITELIST_DIRS` / `WHITELIST_FILES` and send a PR).
- **No denials at all.** Check `mode` in `routes.json` (`off` detaches the
  middleware), and confirm the plugin loaded: `qwenpaw plugin list`.
- **False-positive keyword hints.** ASCII keywords match on word boundaries
  already; if a CJK keyword is too broad, narrow it in `routes.json`.

## Development

```bash
python -m unittest discover -s tests -v     # 13 tests, stdlib-only
python packaging/build_plugin_zip.py        # dist/dispatch-guard-qwenpaw-plugin.zip
```

The test suite imports `backend/main.py` with AgentScope/QwenPaw imports
degraded to local stand-ins, so it runs on a bare Python 3.10+ (CI does exactly
that). Cutting a release: bump `version` in `plugin.json`, add a `### vX.Y.Z`
section to `docs/CHANGELOG.md`, tag `vX.Y.Z` — the release workflow runs the
tests, builds the archive and publishes it with install instructions.

## Privacy

No network calls, no telemetry. The only writes are denial/warning records to
`<workspace>/logs/dispatch_guard.jsonl` on the local machine.

## Changelog

See [docs/CHANGELOG.md](docs/CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).
