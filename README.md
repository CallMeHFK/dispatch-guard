# dispatch-guard

[![CI](https://github.com/CallMeHFK/dispatch-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/CallMeHFK/dispatch-guard/actions/workflows/ci.yml)
[![QwenPaw](https://img.shields.io/badge/QwenPaw-2.2.x-green)](https://github.com/agentscope-ai/QwenPaw)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

QwenPaw middleware plugin that **enforces an orchestrator agent's output routing
at the tool seam**. When the orchestrator (default agent) tries to write a
deliverable itself instead of dispatching to the owning specialist agent,
dispatch-guard denies the write and tells it whom to dispatch to. Basic
operations are never the guard's business: every read passes, and writes of
notes, scripts, configs or scratch files are not intercepted. A write is
blocked only when it is deliverable-shaped **and** a specialist agent that
actually exists in this environment owns it — no owner, no block. The guard
discovers the environment's agents itself, drafts a dispatch table from their
declared skills, and only then starts enforcing. Shell deliverable heuristics
are warn-only, and `spawn_subagent` is denied as a backstop for the
config-layer disable. Stdlib-only, no pip dependencies, no telemetry.

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
- [What is never touched](#what-is-never-touched) — reads, whitelist, basic writes
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
qwenpaw plugin install dist/dispatch-guard.zip
```

Or drop the tree into the plugin directory yourself — QwenPaw discovers
`plugin.json` at startup:

- POSIX: `~/.qwenpaw/plugins/dispatch-guard/`
- Windows: `%USERPROFILE%\.qwenpaw\plugins\dispatch-guard\`

Every release ships the archive as a release asset with a permanent URL —
the filename never carries a version (that lives in `plugin.json` inside the
zip), so this one-liner always installs the latest release:

```bash
qwenpaw plugin install https://github.com/CallMeHFK/dispatch-guard/releases/latest/download/dispatch-guard.zip
```

### 2 · Configure (a fresh install drafts the table itself)

A new install has **no dispatch table at all** — only `routes.example.json`.
That state is called `unconfigured`: the guard attaches, blocks nothing, and
on the first deliverable write it sees, it scans the environment for
specialist agents and **drafts the table for you**:

- **Where it looks:** `agents/*/agent.json` and top-level `agent.json` in the
  workspace, plus the host-level agents directory (`~/.qwenpaw/agents/`) when
  the plugin is installed under `~/.qwenpaw/plugins/`.
- **What it writes:** `routes.draft.json` in the plugin directory, `mode` on
  `warn`. Each deliverable category (documents, media, hardware artifacts,
  archives) routes to the first discovered agent whose id/skills/description
  mentions that category; categories with no matching agent stay unrouted.
- **What it never does:** write `routes.json` itself or turn on `enforce`.
  Activation is an explicit rename.

So onboarding is a review, not an interrogation: open
`routes.draft.json`, fix whatever the keyword match got wrong (agent
inventories are terse), rename it to `routes.json`, and let `warn` run for a
session before flipping to `"mode": "enforce"`. If the environment has no
discoverable specialists at all, no draft is written and the guard falls back
to asking the agent to build the table with you from `routes.example.json` —
and regardless of the table, a deliverable class with no live owner is never
blocked (see [How it works](#how-it-works)).

Your real table is private data — it names your agents and your internal
systems. Keep it out of git: `routes.json` **and the generated
`routes.draft.json`** are gitignored and the build refuses to package either.
([CONTRIBUTING.md](CONTRIBUTING.md) → *Secrets (strict rule)*.)

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
| no `routes.json` | write succeeds; a setup block is appended — it names `routes.draft.json` when the environment's agents allowed one to be generated; `needs_config` in the log |
| `warn` (the draft default) | write succeeds; a warning block naming the owning agent is appended |
| `enforce` | write is denied and the denial names the owning agent — but only if that agent exists here |
| `off` | middleware not attached; nothing to see |

## How it works

The plugin registers one AgentScope `MiddlewareBase` factory
(`api.register_middleware`, priority 40). The factory only returns an instance
when `agent_config.id == "default"` — every other agent is untouched.

`on_acting` inspects each tool call and applies three rules, all gated by one
principle — **a block must name a real dispatch target**. Agents actually
present in the environment are discovered from their manifests
(`agents/*/agent.json` in the workspace, plus the host agents directory), and
a route whose agent does not exist — or a deliverable class no discovered
agent owns — is never blocked, just recorded as `no_owner` in the audit log:

| Tool call | `enforce` | `warn` | `unconfigured` | `off` |
|---|---|---|---|---|
| reads, directory listings, grep, and every non-write tool | **Passes untouched** — in all modes, for all agents; the guard never sees them | same | same | same |
| `write_file` / `edit_file` / `append_file` to a **basic** target (whitelisted paths, notes, scripts, configs, scratch, extension-less files — anything not deliverable-shaped) | **Passes untouched**, not even logged | same | same | not attached |
| `write_file` / `edit_file` / `append_file` to a **deliverable-shaped** target whose owner does not exist here (route names an absent agent, or no discovered agent owns the class) | **Passes untouched**; `no_owner` audit record | same | same | not attached |
| `write_file` / `edit_file` / `append_file` to a **deliverable-shaped** target outside the whitelist, owned by a **live** agent (under `outputs/`, `projects/`, or carrying a deliverable extension — see [Configuration](#configuration)) | **Denied** — "该产出属于 {agent} 域…请用 `submit_to_agent` 派发给 {agent}" | Passes, warning block appended to the `ToolResponse` | Passes, setup block appended (never denies: there is no table to judge ownership from) | Middleware not attached |
| `execute_shell_command` naming a deliverable-shaped output file with a live owner (redirects, `tee`, `pandoc -o` into deliverable targets) | Passes, warning block — shell heuristics **never block** unless `shell_enforce` is on, in which case the unambiguous shapes are denied (status checks must stay cheap) | same | same, with the setup block | not attached |
| `spawn_subagent` / `spawn_agent` | **Denied** (backstop for the config-layer disable) | denied | denied | not attached |

Mode is re-read from `routes.json` on every tool call and cached by file mtime,
so `enforce ↔ warn` flips apply mid-session with no restart. `off` is the
exception: it is decided by the factory when the agent is built, so switching
*back* from `off` needs a new agent instance (a fresh session).

**Two coverage tiers.** A target that is *explicitly written to* — a redirect
(`>` / `>>` / `tee`), a converter `-o`, or the destination of `cp`/`mv`/`rsync`
— is treated like a write-tool call: warned by default, denied when the mode
allows it (shell targets additionally need `shell_enforce`). A deliverable path
that is merely *mentioned* in a quoted string (inside a command, or inside the
argument of a `write_tools`-declared code runner) only ever earns a warning:
a mention proves the path was visible, not that bytes will land there, and
blocking on guesses is how a guard strands legitimate work.

Routing hints: `routes.json` maps extensions (`ext_routes`, checked first) and
keywords (`routes`) to agent ids, so the denial names a concrete owner instead of
a generic refusal. Pure-ASCII keywords match on word boundaries (`CI` does not
fire inside `asyncio`); CJK keywords match as substrings.

## What is never touched

Whitelisted writes — the orchestrator's own bookkeeping — and every basic
operation are invisible to the guard, in all modes:

- reads, directory listings, grep, and every tool that is not a write/shell/spawn tool
- writes under `memory/`, `notes/`, `wiki/`, `episodes/`, `tmp/`, `trash/`, `logs/`
- top-level `MEMORY.md`, `PROFILE.md`, `AGENTS.md`, `SOUL.md`, `HEARTBEAT.md`,
  `skill.json`, `agent.json`
- the plugin tree itself (the guard must not block its own maintainer;
  **absolute paths only**, so workspace-relative paths can never accidentally
  resolve into it)
- any other non-deliverable write: notes, scripts, configs, scratch and
  extension-less files anywhere in the workspace

The whitelist also beats deliverable shape: `tmp/render.png` passes even though
`.png` is a deliverable extension. Only what remains — deliverable-shaped,
non-whitelisted writes — is guarded. And the middleware attaches to the
`default` agent only; every other agent is untouched from the start.

## Configuration

Copy `routes.example.json` to `routes.json` in the plugin directory and edit it.
The sample uses placeholder agent ids; replace them with your own:

```jsonc
{
  "mode": "warn",            // enforce | warn | off  (hot-reloaded by mtime)
  "deliverable_dirs": ["outputs", "projects"],
                             // workspace dirs whose writes are deliverables
                             // (extends the built-in defaults)
  "deliverable_exts": [],    // extra extensions that mark a write as a deliverable
                             // (extends the built-in list, never replaces it)
  "shell_enforce": false,    // opt-in: in enforce mode, DENY shell commands whose
                             // explicit output target is a deliverable owned by a
                             // live agent (closes the redirect/copy bypass; see
                             // docs/RESEARCH.md → permission-gate coverage gap)
  "write_tools": [],         // extra write-capable tool names to guard (host tools
                             // that produce files, e.g. code runners, downloaders;
                             // paths inside their arguments are scanned too)
  "routes": [                // keyword -> owning agent (CJK substring, ASCII word-boundary)
    {"match": ["文档", "报告", "docx", "pdf"], "agent": "DocAgent"},
    {"match": ["代码", "refactor", "debug", "CI"], "agent": "CodeAgent"}
  ],
  "ext_routes": {            // extension -> owning agent (checked first)
    ".docx": "DocAgent", ".svg": "DesignAgent"
  }
}
```

A write is only ever guarded when it is **deliverable-shaped** — inside a
`deliverable_dirs` directory, or carrying a `deliverable_exts` extension — and
outside the whitelist. The built-in defaults cover document/data formats
(`.docx`, `.pdf`, `.pptx`, `.xlsx`, `.csv`, …), archives, images, audio/video,
and CAD artifacts (`.dxf`, `.step`, `.stl`, `.gcode`, `.kicad_pcb`, …). Code and
text formats (`.py`, `.md`, `.json`, …) are deliberately **not** deliverables:
writing them is a basic operation. If your workflow does treat some of them as
deliverables, add the extensions to `deliverable_exts`.

Unmatched denials fall back to a generic "对应专业 agent" hint. A `routes.json`
that exists but does not parse fails **open** to `off` (with an exception in the
host log) — a table we cannot read must not become a blanket blocker.

## Logging

Every interception — and nothing else — is appended to
`<workspace>/logs/dispatch_guard.jsonl`; basic operations leave no trace:

```json
{"ts": 1790592647.6, "plugin": "dispatch-guard", "action": "denied",
 "tool": "write_file", "target": "outputs/probe.docx", "mode": "enforce",
 "message": "该产出属于 DocAgent 域（命中 ext .docx），请用 submit_to_agent 派发给 DocAgent；…"}
```

`action` is `denied`, `warned`, `needs_config`, or `no_owner`; `mode` is the
mode that was actually in effect for that call. `no_owner` records a
deliverable-shaped write that passed because no live agent owns it — the
signal to either install a specialist for that class or extend the whitelist.
This file is the audit trail for measuring how often the orchestrator still
tries to self-execute after prompt fixes. Records contain real tool targets,
so sanitize before attaching one to an issue.

## Troubleshooting

- **Nothing is ever blocked and the log fills with `needs_config`.** Expected on
  a fresh install: there is no `routes.json`. Check the generated
  `routes.draft.json`, review it with your agents' real duties, rename it to
  `routes.json` (see [Quick start step 2](#2--configure-a-fresh-install-drafts-the-table-itself)).
- **A deliverable write is never denied, and the log says `no_owner`.** The
  route names an agent that does not exist in this environment, or no
  discovered agent owns that deliverable class. The guard will not strand a
  deliverable with nowhere to go: install/rename the specialist, fix the
  route, or add the path to the whitelist if it is genuinely the
  orchestrator's own output.
- **The draft routed a class to the wrong agent.** Keyword matching against
  terse agent manifests is a first guess, not an oracle. Edit
  `routes.draft.json` before renaming it; delete groups that should stay
  unrouted.
- **No denials at all.** Check `mode` in `routes.json` (`off` detaches the
  middleware, and leaving `off` needs a new session), and confirm the plugin
  loaded: `qwenpaw plugin list`.
- **Everything gets denied, even whitelisted paths.** You are running pre-0.1.0
  logic, where native-tool inputs (JSON strings) were not parsed before path
  extraction. Upgrade to ≥0.1.0.
- **Legitimate deliverable-shaped edits are denied.** The path is outside the
  whitelist. Either it really is a deliverable (dispatch it), or it is a new
  meta-location (add it to `WHITELIST_DIRS` / `WHITELIST_FILES` and send a PR).
  Writes that are *not* deliverable-shaped — notes, scripts, configs,
  extension-less files — are never denied on ≥0.1.3; if you want one of those
  guarded, add its extension to `deliverable_exts`.
- **A write you expected to be denied passes silently.** Either it is not
  deliverable-shaped (outside `deliverable_dirs`, without a deliverable
  extension — add the extension to `deliverable_exts`), or it is
  deliverable-shaped but ownerless here (`no_owner` in the audit log).
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
python -m unittest discover -s tests -v     # stdlib-only, no host needed
python packaging/build_plugin_zip.py        # dist/dispatch-guard.zip
ruff check .                               # lint (dev-only; the runtime stays dependency-free)
```

The suite imports `backend/main.py` with AgentScope/QwenPaw imports degraded to
local stand-ins, so it runs on a bare Python 3.10+ (CI does exactly that, on
3.10/3.11/3.12, plus `ruff check .`). Setup, conventions, and the secrets rule
are in [CONTRIBUTING.md](CONTRIBUTING.md).

To decide the `warn` → `enforce` promotion on evidence, summarize the audit
trail:

```bash
python packaging/audit_summary.py <workspace>/logs/dispatch_guard.jsonl
```

The design is grounded in recent literature on LLM-agent guardrails and
orchestration failures — [docs/RESEARCH.md](docs/RESEARCH.md) maps each paper
to the decision it informs.

Cutting a release: bump `version` in `plugin.json` and add a `## vX.Y.Z`
section at the top of `docs/CHANGELOG.md` — pushing that to `main` is the whole
release process. The release workflow tags `vX.Y.Z` itself, re-runs the suite,
builds the archive, and publishes the GitHub release with the changelog notes
and its sha256. (Release-hygiene tests fail the release if the changelog
section is missing or the README's install URL no longer names the built
asset.) Pushing a `v*` tag by hand still works as the manual path.

## Privacy

No network calls, no telemetry. The only writes are denial/warning records to
`<workspace>/logs/dispatch_guard.jsonl` on the local machine.

## Changelog

See [docs/CHANGELOG.md](docs/CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).
