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
- **What it writes:** `routes.draft.json` in the plugin-data directory
  (`~/.qwenpaw/plugin-data/dispatch-guard/` on a host install — a location a
  plugin reinstall never touches), `mode` on `warn`. Each deliverable category
  (documents, media, hardware artifacts, archives) routes to the first
  discovered agent whose id/skills/description mentions that category;
  categories with no matching agent stay unrouted.
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
| `off` | middleware stays attached and passes every call unexamined; the first off-observed call writes one `action=off` audit record |

## How it works

The plugin registers one AgentScope `MiddlewareBase` factory
(`api.register_middleware`, priority 40). The factory only returns an instance
when `agent_config.id == "default"` — every other agent is untouched.

`on_acting` inspects each tool call and applies three rules, all gated by one
principle — **a block must name a real dispatch target**. Agents actually
present in the environment are discovered from their manifests
(`agents/*/agent.json` in the workspace, plus the host agents directory), and
a route whose agent does not exist — or a deliverable class no discovered
agent owns — is never blocked, just recorded as `no_owner` in the audit log.
A *readable* inventory with zero specialists is treated for what it is — a
single-agent deployment, every class `no_owner` — and only an inventory the
guard could not read falls back to trusting the operator's table verbatim.
"Could not read" covers a failed glob **and** any manifest that exists but
cannot be opened: a permission wall must not be able to report itself as
"this deployment has no specialists". (A manifest that opens and turns out to
be malformed is a different case — it is read, it just describes no agent, and
it stays non-fatal.) This is the one place where an unrouted deliverable can
still be blocked, and the block then points at the user, never at an invented
agent:

| Tool call | `enforce` | `warn` | `unconfigured` | `off` |
|---|---|---|---|---|
| reads, directory listings, grep, and every non-write tool | **Passes untouched** — in all modes, for all agents; the guard never sees them | same | same | same |
| `write_file` / `edit_file` / `append_file` to a **basic** target (whitelisted paths, notes, scripts, configs, scratch, extension-less files — anything not deliverable-shaped) | **Passes untouched**, not even logged | same | same | attached; passes unexamined |
| `write_file` / `edit_file` / `append_file` to a **deliverable-shaped** target whose owner does not exist here (route names an absent agent, or no discovered agent owns the class) | **Passes untouched**; `no_owner` audit record | same | same | attached; passes unexamined |
| `write_file` / `edit_file` / `append_file` to a **deliverable-shaped** target outside the whitelist, owned by a **live** agent (under `outputs/`, `projects/`, or carrying a deliverable extension — see [Configuration](#configuration)) | **Denied** — "该产出属于 {agent} 域…请用 `submit_to_agent` 派发给 {agent}" | Passes, warning block appended to the `ToolResponse` | Passes, setup block appended (never denies: there is no table to judge ownership from) | Attached; passes unexamined |
| `execute_shell_command` naming a deliverable-shaped output file with a live owner (redirects `>`/`>>`/`>|`, `tee` including long options, `curl`/`wget` outputs, `dd of=`, `zip`/`tar` creation, compiler `-o`, `sed`/`perl -i` in-place edits, converter `-o`/`--outdir`, `cp`/`mv` destinations, ffmpeg positional output) | Passes, warning block — shell heuristics **never block** unless `shell_enforce` is on, in which case the unambiguous shapes are denied (status checks must stay cheap). Exception, and not a heuristic: a command naming one of the guard's own table **paths** warns, and one that mutates it is denied in every mode — `shell_enforce` does not apply (meta-integrity, see below). A project's own `routes.json` is never matched | same | same, with the setup block | attached; passes unexamined |
| `spawn_subagent` / `spawn_agent` | **Denied** (backstop for the config-layer disable) | denied | denied | attached; passes unexamined |

Mode is re-read from `routes.json` on every tool call and cached by file
mtime, so every flip — `enforce ↔ warn ↔ off` — applies mid-session with no
restart. `off` keeps the middleware attached and short-circuits inside the
hook: nothing is judged, but the first call observed in each off period
writes a single `action=off` audit record, so a disarmed stretch is visible
in the trail instead of invisible.

**Two coverage tiers.** A target that is *explicitly written to* — a redirect
(`>` / `>>` / `>|` / `tee`, with or without long options), an output flag
(`-o` / `-O` / `--outdir` / `--output-document` / `dd of=`), the destination
of `cp`/`mv`/`rsync`, an archive being created (`zip out.zip`, `tar cf`), a
compiler's `-o`, an in-place edit (`sed -i` / `perl -i`), or ffmpeg's
trailing positional output — is treated like a write-tool call: warned by
default, denied when the mode allows it (shell targets additionally need
`shell_enforce`). A deliverable path
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
  resolve into it) — **except the guard's own dispatch tables**:
  `routes.json`/`routes.draft.json` are never whitelisted, and a write to
  either real table is refused in every mode the middleware observes, `off`
  included, whichever shape the path arrives in — a plain string, a list or
  dict value, an upper-case or trailing-dot spelling. Through
  `execute_shell_command` the same two files are matched **by their real
  path** (plugin directory or plugin-data mirror, absolute or `~`-spelled,
  case-insensitively, and never past a path boundary — `routes.json.bak` is
  a backup, not the table), or by a bare basename after the command has
  `cd`'d into a guard home: naming one warns, mutating one — a redirect,
  `tee`, `sed -i`, `rm`/`mv`/`truncate`/`ln`, an in-process
  `open(..., "w")` — is refused. Bare basenames elsewhere are never matched,
  so a project that owns its own `routes.json` can still read, grep, copy
  and delete it. The table is what the guard is made of; the guarded agent
  must not hold the eraser.
- any other non-deliverable write: notes, scripts, configs, scratch and
  extension-less files anywhere in the workspace

The whitelist also beats deliverable shape: `tmp/render.png` passes even though
`.png` is a deliverable extension. Only what remains — deliverable-shaped,
non-whitelisted writes — is guarded. And the middleware attaches to the
`default` agent only; every other agent is untouched from the start.

## Configuration

Copy `routes.example.json` to `routes.json` in the plugin directory and edit it.
The sample uses placeholder agent ids; replace them with your own. Where the
table lives:

- **Plugin directory** (`~/.qwenpaw/plugins/dispatch-guard/routes.json`) — the
  explicit override, checked first. Convenient to edit, **but the host
  installer rebuilds the plugin directory on every reinstall**, so the plugin
  mirrors this copy into the plugin-data directory after every reload.
- **Plugin-data directory** (`~/.qwenpaw/plugin-data/dispatch-guard/routes.json`)
  — the durable home, checked second. After a reinstall this is the copy the
  guard finds, so your table survives `qwenpaw plugin install --force`. Rename
  the generated draft here and it activates.

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
                             // live agent (closes the redirect/tee/curl/zip/tar/
                             // compiler -o/sed -i copy bypass; see
                             // docs/RESEARCH.md → permission-gate coverage gap;
                             // guard-table mentions are intercepted regardless)
  "write_tools": [],         // extra write-capable tool names to guard (host tools
                             // that produce files, e.g. code runners, downloaders;
                             // paths inside their arguments are scanned too)
  "jev": {                   // OPT-IN semantic arbitration layer (default off =
                             // zero network, byte-identical behaviour; see below)
    "enabled": false,
    "judges": "inherit:jev-kernel",  // read endpoints/models from the jev-kernel
                                     // plugin's jev.json (single source of truth),
                                     // or an inline [{"name","type","baseUrl",
                                     // "model","timeoutMs"}] list
    "tiers": ["lux"],        // judge whitelist — lux only
    "thresholds": "inherit", // or {"deny": 0.85, "warn": 0.6}
    "online_timeout_ms": 2500,       // hard cap per judge call (min'ed with the
                                     // judge spec's own timeoutMs)
    "record": true           // write jev decisions into the audit trail
  },
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

Unmatched blocks always name a verified owner — the live agent from the
route. When no owner can be confirmed (the agent inventory itself could not
be read), the block tells the agent to hand the case back to the user; the
guard never invents a "corresponding specialist agent" that does not exist.
Judgment is case-insensitive and spelling-tolerant: `Notes/x.docx` is
whitelisted like `notes/x.docx`, and `report.docx.` / `report.docx ` route as
`report.docx` — trailing dots and spaces are accidents of typing, not new
files. A `routes.json` that exists but no longer parses does **not** disarm
the guard: the reload keeps the last known-good table and warns loudly; only
a cold start with nothing good ever loaded fails open to `off` (with an
exception in the host log) — a table we cannot read must not become a
blanket blocker, and a table we read *yesterday* still must not become a
blanket pass-through.

### jev: opt-in semantic arbitration (P0)

With `jev.enabled: true`, the draft generator gains a semantic layer between
the orchestrator's declared dispatch policy (which always wins, and is never
charged judge latency) and the keyword scan: one judge call asks which live
agent owns each deliverable category, and only **certain answers naming a
live agent** count. An "unrouted" answer, sub-threshold confidence, a judge
naming an agent that is not installed, or a dead/slow/garbled judge all fall
back to the keyword scan — the scan is the fallback, never replaced, and the
hook fails open on any internal error. Judge endpoints and models come from
the jev-kernel plugin's own `jev.json` (`inherit:jev-kernel`; `$JEV_KERNEL_CONFIG`
→ jev-kernel's plugin directory → its plugin-data copy, in that order), the
`tiers` whitelist pins which of those judges may be dialed, and jev-kernel's
global `enabled: false` is authoritative — a parked kernel disables this
layer too. The wire dialect is a stdlib mirror of jev-kernel's raw
`systemone` protocol, pinned by contract tests against a fake server.
Decisions land in the audit trail under a `jev` subobject
(`{judge, latencyMs, probability, value, fallback}`); set `record: false` to
suppress that. Scope note: P0 covers draft generation only — online
blind-spot arbitration (P1) and shell-mention escalation (P2) are designed
in [docs/JEV_INTEGRATION.md](docs/JEV_INTEGRATION.md) and intentionally not
implemented yet.

## Logging

Every interception — and nothing else — is appended to
`<workspace>/logs/dispatch_guard.jsonl`; basic operations leave no trace:

```json
{"ts": 1790592647.6, "plugin": "dispatch-guard", "action": "denied",
 "tool": "write_file", "target": "outputs/probe.docx", "mode": "enforce",
 "message": "该产出属于 DocAgent 域（命中 ext .docx），请用 submit_to_agent 派发给 DocAgent；…"}
```

`action` is `denied`, `warned`, `needs_config`, `no_owner`, or `off` (one
marker per disarmed period); `mode` is the mode that was actually in effect
for that call. `no_owner` records a deliverable-shaped write that passed
because no live agent owns it — the signal to either install a specialist
for that class or extend the whitelist. This file is the audit trail for
measuring how often the orchestrator still tries to self-execute after
prompt fixes. Shell records hold only the extracted paths and hint text —
never the raw command line. Spawn denials (whose input is free-form task
text that routinely embeds API keys) are recorded as a digest: sha256[:16],
length, and a credential-redacted excerpt, so the task text itself is not in
the log. A tool call whose shape the guard cannot parse gets the same digest
— that path used to log the raw structure verbatim, which is exactly where a
credential would have landed. The redaction is a known-shape filter (`sk-…`,
AWS access key ids, `Bearer …`, `key=value` and `key is value` assignments),
not a secret scanner: a credential in some other spelling can still appear in
the excerpt, which is why the excerpt is capped and the hash, not the text,
is the record's identity. Other records contain real tool targets, so still
sanitize before attaching one to an issue.

## Troubleshooting

- **Nothing is ever blocked and the log fills with `needs_config`.** Expected on
  a fresh install: there is no `routes.json`. Check the generated
  `routes.draft.json` (plugin-data directory), review it with your agents' real
  duties, rename it to
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
- **No denials at all.** Check `mode` in `routes.json` — `off` keeps the
  middleware attached but passes everything unexamined (the log marks each
  off period with `action=off`). Flipping `mode` back to `warn`/`enforce`
  hot-applies by mtime; no new session is needed. Then confirm the plugin
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

`docs/CHANGELOG.md` records the v0.1.7 verification run under `enforce` — an
18-case matrix against a live multi-agent inventory: whitelist, basic writes
and reads pass silently; owned deliverables are denied with the owning agent
named (by extension, directory — relative or absolute — or keyword); unowned
deliverable classes pass with `no_owner` audit records; shell explicit targets
warn by default (`shell_enforce` opts into denial); quoted mentions warn only;
spawn stays denied. Older per-version records are in the changelog.

Known trade-offs are documented in [docs/DESIGN.md](docs/DESIGN.md): shell
heuristics are content-based (a bookkeeping heredoc that merely *mentions* a
deliverable earns a non-blocking warning), and a deliverable class no rule
covers passes with an audit record instead of a denial.

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

No network calls, no telemetry — by default. The one opt-in exception is the
`jev` arbitration layer: with `jev.enabled: true` the draft generator dials
exactly the judge endpoints named by the resolved judge configuration —
with `inherit:jev-kernel` that is your own jev-kernel `jev.json`, i.e. the
local/LAN models you already run, and the `tiers` whitelist locks which of
them may be contacted (`["lux"]` by default). Nothing is dialed while
`enabled` is false (the test suite asserts zero outbound sockets in that
state), and only agent manifests (ids, skills, descriptions) and the
deliverable category list are ever sent — never file contents or command
lines. Inline `judges` pointing at a cloud endpoint are possible by
configuration; that choice and its disclosure are then yours.

What the plugin writes on the local machine:
interception records in `<workspace>/logs/dispatch_guard.jsonl` (tool targets
and hint text — shell command lines are never stored verbatim, and free-form
task text arrives only as a hash plus a credential-redacted excerpt; see the
audit-log note above for what that redaction does and does not catch), the
operator's draft `routes.draft.json`, and the reinstall-proof mirror of
`routes.json` under `<host>/plugin-data/dispatch-guard/`. The dispatch tables
themselves name your real agents and internal keywords — treat them as
private state: they are gitignored, never packaged, and the guarded agent is
denied any write to them (plugin tree, plugin-data mirror, or via shell).

## Changelog

See [docs/CHANGELOG.md](docs/CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).
