# dispatch-guard design notes

## Why the tool seam, not the prompt

Prompt-level dispatch rules compete with the model's tool-loop instincts and
with every skill description saying "I can do X". Each prompt fix raises the
cost of compliance but never blocks the cheapest path. The middleware moves
the rule to the seam where the cost of violation becomes a hard denial plus a
concrete redirect. The prompt stays as the *first* line (routing by intent);
the plugin is the *last* line (routing by side effect).

## Deny writes, only warn on shell — until the operator opts in

Write tools carry an explicit path — a reliable signal. Shell commands are
ambiguous: `pandoc -o out.docx` might be a deliverable, might be a temp
conversion in a scratch dir, and status-check commands must stay cheap and
unimpeded. So shell heuristics append a warning block to the `ToolResponse`
(visible to the model on its next step) and never block. All warnings land in
the audit log for later tuning.

One caveat earned in the field: the permission-gate stress-test of Claude
Code's auto mode (see docs/RESEARCH.md) showed that agents under a gate
route around it through whatever path the gate does not evaluate — here, a
shell redirect into the deliverable target. That is why v0.1.5 added an
opt-in `shell_enforce`: when it is on, enforce mode denies the *unambiguous*
shapes only (an explicit output target that is deliverable-shaped and owned
by a live agent); ambiguous or basic targets still just warn.

Coverage is tiered by what the command actually proves. An *explicit* output
target (redirect, tee, converter `-o`, or the destination of `cp`/`mv`/`rsync`)
is where bytes land — the same reliability as a write-tool path argument, so
it follows the full write rules and can be denied. A deliverable path merely
*mentioned* in a quoted string (shell-embedded code, a code-runner's argument)
proves visibility but not effect — grep for `report.docx` is not writing
`report.docx` — so mentions warn and are never denied, even with
`shell_enforce` on. The tiers exist because the coverage lesson demands seeing
the second class of paths while the fail-open rule demands not acting on them.

## Why only deliverable-shaped writes

The first version treated every write outside the whitelist as a deliverable.
That made `enforce` a blanket blocker: a root `notes.md`, a `scripts/setup.py`,
a `Makefile` — ordinary file operations — were denied and routed to a
"specialist" that has no business receiving them. A guard that denies the
basics trains its operator to disable the guard.

So the deny set is now exactly *deliverable-shaped* targets: under a
deliverable directory (`outputs/`, `projects/` by default, configurable) or
carrying a deliverable extension (`.docx`, `.pdf`, `.zip`, `.png`, CAD
artifacts, … — configurable, extending the built-in list). Everything else is
a basic operation that passes untouched in every mode, without a log entry.
Code and text formats are deliberately not deliverables: the orchestrator
writing a script or a config file is doing its job, not producing output that
belongs to a specialist. Reads are outside the mandate entirely — the guard
governs who *produces* outputs, never who may look at them.

The whitelist still wins over deliverable shape (`tmp/render.png` passes), and
the shell heuristic now shares the same predicate, so a redirect into a notes
file no longer earns a warning while `pandoc -o outputs/r.docx` still does.
The trade-off is deliberate: a deliverable saved to an unexpected location
(e.g. `reports/q3.pdf` at the workspace root) now passes instead of being
denied. That is the cost of never blocking basic operations; `deliverable_exts`
and `deliverable_dirs` exist to close exactly that gap per deployment.

## No owner, no block

A denial is only useful if it names a dispatch target that exists. The first
design trusted `routes.json` blindly, so a table that drifted from reality —
an agent renamed, a specialist removed, a category nobody covers — kept
denying writes and pointing at agents that were not there. The deliverable had
nowhere to go: the guard had become a wall, not a router.

The guard therefore discovers the environment's specialists before it blocks
anything: `agents/*/agent.json` manifests in the workspace plus the host-level
agents directory (when the plugin really lives in a QwenPaw tree). Every route
is validated against that inventory at interception time, and the rule is
absolute: **a block must name a real dispatch target.** A route naming an
absent agent, or a deliverable class no discovered agent owns, passes —
recorded as `no_owner` in the audit log, silent to the model (a warning that
names no one is noise), audible to the operator.

One deliberate asymmetry: discovery finding *zero* agents does not disarm the
guard. An empty inventory means "we could not read this deployment", not
"this deployment has no specialists" — the operator's table is then trusted
verbatim, preserving the pre-discovery behaviour. Only a *non-empty*
inventory gets veto power.

The natural follow-up — "then why does my `.docx` write pass when there is no
doc agent?" — is the point: the plugin's contract is routing, not prohibition.
Blocking a write you cannot route is just breaking the agent's work with
extra steps. Install or designate a specialist for the class and the same
write starts being enforced.

## Draft, don't interrogate

The original unconfigured flow asked the agent to interview the user and
hand-write a dispatch table — accurate, but slow, and every fresh install
started from a blank file the plugin could have filled in. The plugin can see
the same inventory the denial logic uses, so `unconfigured` mode now drafts
`routes.draft.json` itself: deliverable categories (documents, media, hardware
artifacts, archives) mapped to the first discovered agent whose
id/skills/description mentions the category, `mode` pinned to `warn`.

When the orchestrator states its routing policy in its own `agent.json`
description ("文档/报告/Office文档派 DocAgent，…"), that policy outranks any
guess: each "X派Y" claim maps its slash-separated keywords to the named
agent, claims naming agents outside the live inventory are dropped, and only
categories the policy leaves uncovered fall back to the manifest scan. The
draft is then the deployment's declared intent in machine-checkable form,
not the plugin's opinion about it.

Two guardrails keep the draft honest. It never writes `routes.json` —
activation is an explicit rename, so the plugin cannot switch itself into
enforcement. And the draft is environment-derived data (real agent ids), so it
is gitignored and the packaging step refuses to ship it, exactly like a
hand-written table.

## Why the whitelist includes the plugin tree

First live contact produced the bootstrap paradox: the guard denied its own
maintainer (the default agent editing `~/.qwenpaw/plugins/dispatch-guard/`)
because the plugin tree is outside the workspace whitelist. Plugin maintenance
is the orchestrator's architecture duty, so the tree is whitelisted — but only
for targets that arrive as absolute paths. Resolving workspace-relative paths
against the process cwd would whitelist whatever directory the repo happens to
sit in (a bug the test suite caught when run from inside the repo).

## Why native inputs are parsed as JSON strings

Host-native tools deliver `tool_call.input` as a JSON *string*, not a dict.
The first version skipped parsing, so the whole JSON blob became the "path",
failed every whitelist check, and the guard denied every write the default
agent attempted — including its own memory files. `_target_of` now parses
JSON-looking strings first; `memory/2026-09-28.md` is seen as a path again.

## Keyword matching

Routing hints exist to make denials actionable ("派发给 DocAgent", not "denied").
CJK terms match as substrings (Chinese has no word boundaries); pure-ASCII
terms require word boundaries so `CI` does not fire inside `asyncio` and `cad`
does not fire inside `decade`. Extension routing is checked before keywords
because file extensions are the least ambiguous signal.

## Fail-open, with one visible exception

A malformed `routes.json`, a logging failure, an unreadable config — all degrade
to "no interception" rather than a wedged agent. The guard is an enforcement
convenience, not a security boundary; it must never be the reason work stops.
Denying everything because the table could not be parsed would also invent
ownership claims from an unread source.

A **missing** `routes.json` is not a failure, it is a fresh install, and it is
the one case that does not go silent. The repository ships only
`routes.example.json`, so "no table yet" is the default state every new user
starts in; detaching there would report a healthy plugin that enforces nothing,
and the operator would only learn the truth the day a deliverable lands in the
wrong agent's hands. So `unconfigured` mode stays attached, never denies, appends
a block telling the agent to build the table with the user, logs one host-side
warning per process, and records `needs_config` in the audit trail.
