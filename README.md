# dispatch-guard

[![CI](https://github.com/CallMeHFK/dispatch-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/CallMeHFK/dispatch-guard/actions/workflows/ci.yml)
[![QwenPaw](https://img.shields.io/badge/QwenPaw-2.2.x-green)](https://github.com/agentscope-ai/QwenPaw)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

QwenPaw middleware plugin that **enforces an orchestrator agent's output routing
at the tool seam**. When the orchestrator (default agent) tries to write a
deliverable itself instead of dispatching to the owning specialist agent,
dispatch-guard denies the write and tells it whom to dispatch to. Shell
deliverable heuristics are warn-only, and `spawn_subagent` is denied as a
backstop for the config-layer disable. Stdlib-only, no pip dependencies, no
telemetry.

Your dispatch table is yours: the repository ships a sanitized
`routes.example.json`, never a real `routes.json`, and the packaging step refuses
to build one into the archive. A fresh install comes up in **`unconfigured`**
mode — attached, non-blocking, and telling the agent to build that table with
you. See [Quick start](#quick-start).

```bash
qwenpaw plugin install ~/.qwenpaw/plugins/dispatch-guard   # or a local checkout, see below
```

<details>
<summary>Contents</summary>

- [Why](#why)
- [Quick start](#quick-start) — install · configure · verify
- [How it works](#how-it-works) — three interception rules, four modes, routing hints
- [Whitelist](#whitelist)
- [Configuration](#configuration) — `routes.json`
- [Logging](#logging)
- [Troubleshooting](#troubleshooting)
- [Does it work?](#does-it-work)
- [Development](#development) · [Privacy](#privacy) · [License](#license)

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
agent that wrote domain deliverables, drew diagrams, and ran domain queries
itself despite a dispatch table saying otherwise.

The design rationale — why shell stays warn-only, why the plugin tree is
whitelisted, what fails open and what deliberately does not — is in
[docs/DESIGN.md](docs/DESIGN.md).

## Quick start

### 1 · Install

From a checkout (works today, no release needed):

```bash
git clone https://github.com/CallMeHFK/dispatch-guard.git
qwenpaw plugin install dispatch-guard
```

`qwenpaw plugin install` takes a local path or a URL. If QwenPaw is running the
plugin hot-loads immediately; otherwise it loads on next start. To ship the
archive form instead, build it and point the installer at it:

```bash
python packaging/build_plugin_zip.py
qwenpaw plugin install dist/dispatch-guard-qwenpaw-plugin.zip
```

Or drop the tree into the plugin directory yourself — QwenPaw discovers
`plugin.json` at startup:

- POSIX: `~/.qwenpaw/plugins/dispatch-guard/`
- Windows: `%USERPROFILE%\.qwenpaw\plugins\dispatch-guard\`

Once a GitHub Release exists, `qwenpaw plugin install
https://github.com/CallMeHFK/dispatch-guard/releases/latest/download/dispatch-guard-qwenpaw-plugin.zip`
is the one-liner version of the above. There is no release yet.

### 2 · Configure (required on a fresh install)

A new install has **no dispatch table at all** — only `routes.example.json`.
That state is called `unconfigured`: the guard attaches, blocks nothing, and
appends this block to any deliverable write it sees:

> `[dispatch-guard 未配置] 插件目录下没有 routes.json，派发表为空，当前只提示不拦截。
> 请和用户一起确认各类产出归属哪个 agent，把结论写成 routes.json
> （可从 routes.example.json 复制起步）：mode 先设 warn 观察一轮拦截日志，
> 确认派发表无误后再切 enforce。`

So the first thing your agent tells you is that it needs a table. Have it build
one with you — paste this into a session with the default agent:

```
dispatch-guard 提示未配置。请把你现在实际可用的专业 agent 列给我（id + 各自负责的产出类型），
逐个和我确认后写入插件目录的 routes.json：ext_routes 按扩展名、routes 按关键词，
mode 先用 warn。写完把你要改成的映射复述一遍让我确认。
```

Then set `"mode": "enforce"` once the warn log shows the table routes what you
expect and your own bookkeeping still passes.

Your real table is private data — it names your agents and your internal
systems. Keep it out of git: `routes.json` is gitignored and the build refuses
to package it. ([CONTRIBUTING.md](CONTRIBUTING.md) → *Secrets (strict rule)*.)

### 3 · Verify

```bash
qwenpaw plugin list              # dispatch-guard should appear
qwenpaw plugin validate <install-path>
qwenpaw plugin info dispatch-guard
```

Then in a session with the default agent, ask for a write to a deliverable path,
e.g. "用 write_file 把 hello 写入 outputs/probe.docx":

| your `mode` | what happens |
|---|---|
| no `routes.json` | write succeeds; the setup block above is appended; `needs_config` in the log |
| `warn` (the sample default) | write succeeds; a warning block is appended |
| `enforce` | write is denied and the denial names the owning agent |
| `off` | middleware not attached; nothing to see |

## How it works

The plugin registers one AgentScope `MiddlewareBase` factory
(`api.register_middleware`, priority 40). The factory only returns an instance
when `agent_config.id == "default"` — every other agent is untouched.

`on_acting` inspects each tool call and applies three rules:

| Tool call | `enforce` | `warn` | `unconfigured` | `off` |
|---|---|---|---|---|
| `write_file` / `edit_file` / `append_file` to a non-whitelisted path | **Denied** — "该产出属于 {agent} 域…请用 `submit_to_agent` 派发给 {agent}" | Passes, warning block appended to the `ToolResponse` | Passes, setup block appended (never denies: there is no table to judge ownership from) | Middleware not attached |
| `execute_shell_command` matching deliverable heuristics (redirects, `pandoc -o`, `ffmpeg -o` into `outputs/`, `projects/` or deliverable extensions) | Passes, warning block — shell heuristics **never block**, in any mode (status checks must stay cheap) | same | same, with the setup block | not attached |
| `spawn_subagent` / `spawn_agent` | **Denied** (backstop for the config-layer disable) | denied | denied | not attached |

Mode is re-read from `routes.json` on every tool call and cached by file mtime,
so `enforce ↔ warn` flips apply mid-session with no restart. `off` is the
exception: it is decided by the factory when the agent is built, so switching
*back* from `off` needs a new agent instance (a fresh session).

Routing hints: `routes.json` maps extensions (`ext_routes`, checked first) and
keywords (`routes`) to agent ids, so the denial names a concrete owner instead of
a generic refusal. Pure-ASCII keywords match on word boundaries (`CI` does not
fire inside `asyncio`); CJK keywords match as substrings.

## Whitelist

Writes under these workspace paths are always allowed — they are the
orchestrator's own bookkeeping, not deliverables:

- `memory/`, `notes/`, `wiki/`, `episodes/`, `tmp/`, `trash/`, `logs/`
- Top-level `MEMORY.md`, `PROFILE.md`, `AGENTS.md`, `SOUL.md`, `HEARTBEAT.md`,
  `skill.json`, `agent.json`
- the plugin tree itself (the guard must not block its own maintainer;
  **absolute paths only**, so workspace-relative paths can never accidentally
  resolve into it)

Everything else (`outputs/`, `projects/`, deliverable extensions anywhere) is
treated as a deliverable.

## Configuration

Copy `routes.example.json` to `routes.json` in the plugin directory and edit it.
The sample uses placeholder agent ids; replace them with your own:

```jsonc
{
  "mode": "warn",            // enforce | warn | off  (hot-reloaded by mtime)
  "routes": [                // keyword -> owning agent (CJK substring, ASCII word-boundary)
    {"match": ["文档", "报告", "docx", "pdf"], "agent": "DocAgent"},
    {"match": ["代码", "refactor", "debug", "CI"], "agent": "CodeAgent"}
  ],
  "ext_routes": {            // extension -> owning agent (checked first)
    ".docx": "DocAgent", ".py": "CodeAgent", ".svg": "DesignAgent"
  }
}
```

Unmatched denials fall back to a generic "对应专业 agent" hint. A `routes.json`
that exists but does not parse fails **open** to `off` (with an exception in the
host log) — a table we cannot read must not become a blanket blocker.

## Logging

Every interception is appended to `<workspace>/logs/dispatch_guard.jsonl`:

```json
{"ts": 1790592647.6, "plugin": "dispatch-guard", "action": "denied",
 "tool": "write_file", "target": "outputs/probe.docx", "mode": "enforce",
 "message": "该产出属于 DocAgent 域（命中 ext .docx），请用 submit_to_agent 派发给 DocAgent；…"}
```

`action` is `denied`, `warned`, or `needs_config`; `mode` is the mode that was
actually in effect for that call. This file is the audit trail for measuring how
often the orchestrator still tries to self-execute after prompt fixes — and, on a
fresh install, the record of how many writes went by unguarded. Records contain
real tool targets, so sanitize before attaching one to an issue.

## Troubleshooting

- **Nothing is ever blocked and the log fills with `needs_config`.** Expected on
  a fresh install: there is no `routes.json`. Go through
  [Quick start step 2](#2--configure-required-on-a-fresh-install).
- **No denials at all.** Check `mode` in `routes.json` (`off` detaches the
  middleware, and leaving `off` needs a new session), and confirm the plugin
  loaded: `qwenpaw plugin list`.
- **Everything gets denied, even whitelisted paths.** You are running pre-0.1.0
  logic, where native-tool inputs (JSON strings) were not parsed before path
  extraction. Upgrade to ≥0.1.0.
- **Legitimate infrastructure edits are denied.** The path is outside the
  whitelist. Either it is a deliverable (dispatch it), or it is a new
  meta-location (add it to `WHITELIST_DIRS` / `WHITELIST_FILES` and send a PR).
- **False-positive keyword hints.** ASCII keywords already match on word
  boundaries; if a CJK keyword is too broad, narrow it in `routes.json`.

## Does it work?

`docs/CHANGELOG.md` records the v0.1.1 production run under `enforce`: a 10-case
false-positive matrix (whitelist, plugin-tree and trash writes pass silently;
`outputs/` and `projects/` writes deny with dispatch hints; read-only shell never
warns) and end-to-end dispatch counts. The known trade-off is deliberate: the
shell heuristic is content-based, so a bookkeeping heredoc that merely *mentions*
a deliverable extension earns a non-blocking warning.

## Development

```bash
python -m unittest discover -s tests -v     # 18 tests, stdlib-only, no host needed
python packaging/build_plugin_zip.py        # dist/dispatch-guard-qwenpaw-plugin.zip
ruff check .                               # lint (dev-only; the runtime stays dependency-free)
```

The suite imports `backend/main.py` with AgentScope/QwenPaw imports degraded to
local stand-ins, so it runs on a bare Python 3.10+ (CI does exactly that, on
3.10/3.11/3.12). Setup, conventions, and the secrets rule are in
[CONTRIBUTING.md](CONTRIBUTING.md).

Cutting a release: bump `version` in `plugin.json`, add a `### vX.Y.Z` section to
`docs/CHANGELOG.md`, tag `vX.Y.Z` — the release workflow runs the tests, builds
the archive and publishes it with install instructions.

## Privacy

No network calls, no telemetry. The only writes are denial/warning records to
`<workspace>/logs/dispatch_guard.jsonl` on the local machine.

## Changelog

See [docs/CHANGELOG.md](docs/CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).
