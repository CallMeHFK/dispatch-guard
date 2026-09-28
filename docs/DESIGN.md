# dispatch-guard design notes

## Why the tool seam, not the prompt

Prompt-level dispatch rules compete with the model's tool-loop instincts and
with every skill description saying "I can do X". Each prompt fix raises the
cost of compliance but never blocks the cheapest path. The middleware moves
the rule to the seam where the cost of violation becomes a hard denial plus a
concrete redirect. The prompt stays as the *first* line (routing by intent);
the plugin is the *last* line (routing by side effect).

## Deny writes, only warn on shell

Write tools carry an explicit path — a reliable signal. Shell commands are
ambiguous: `pandoc -o out.docx` might be a deliverable, might be a temp
conversion in a scratch dir, and status-check commands must stay cheap and
unimpeded. So shell heuristics append a warning block to the `ToolResponse`
(visible to the model on its next step) and never block. All warnings land in
the audit log for later tuning.

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

Routing hints exist to make denials actionable ("派发给 IPP", not "denied").
CJK terms match as substrings (Chinese has no word boundaries); pure-ASCII
terms require word boundaries so `CI` does not fire inside `asyncio` and `cad`
does not fire inside `decade`. Extension routing is checked before keywords
because file extensions are the least ambiguous signal.

## Fail-open everywhere

A bad `routes.json`, a missing config, a logging failure — all degrade to
"no interception" rather than a wedged agent. The guard is an enforcement
convenience, not a security boundary; it must never be the reason work stops.
