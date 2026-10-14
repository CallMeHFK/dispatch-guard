# AGENTS.md

Guidance for AI agents working in this repository. Human docs: README.md
(usage), CONTRIBUTING.md (workflow), docs/ (design).

## What this plugin is

QwenPaw middleware that enforces an orchestrator agent's output routing at
the tool seam: one decision — *who owns a deliverable*. Reads and basic
writes are never intercepted; a block must name a specialist agent that
actually exists in this environment.

## Commands (all green before any commit)

```bash
python -m unittest discover -s tests   # bare Python >= 3.10, no host, no deps
ruff check .
python packaging/build_plugin_zip.py   # refuses to build with a real table present
```

## Invariants (rationale in docs/DESIGN.md — read it before touching decision paths)

- Fail-open everywhere except the two designed denies; a guard internal
  failure must never wedge the agent (F17 wrapper).
- A block must name a real dispatch target: a non-empty live inventory
  vetoes routes naming absent agents and unmatched classes (`no_owner`).
- The D1 meta-integrity gate (writes to `routes.json`/`routes.draft.json`
  denied in every mode, `off` included) sits above the fail-open wrapper and
  above the mode short-circuit; keep it total — it must never raise.
- Whitelisted and basic writes pass untouched in every mode with no audit
  record; only deliverable-shaped, non-whitelisted writes are guarded.
- stdlib only: no pip dependencies, no network calls except the opt-in jev
  layer (docs/JEV_INTEGRATION.md).

## Secrets (strict)

Never commit `routes.json`/`routes.draft.json`, real agent ids, internal
hosts/addresses, or people's names — not in code, docs, tests, fixtures, or
commit messages. Placeholders only (`DocAgent`, `judge.example.internal`).
Audit logs record real tool targets: sanitize before quoting anywhere.

## Map

- `backend/main.py` — the whole middleware (whitelist, modes, routing, audit).
- `backend/jev.py` — opt-in System One client; wire dialect mirrors
  jev-kernel's raw protocol, pinned by contract tests. Read
  docs/JEV_INTEGRATION.md before touching it or its hooks.
- `docs/DESIGN.md` — why the seam, the shell tiers and the fail-open rules
  look like this. `docs/RESEARCH.md` — literature mapping.
- `docs/CHANGELOG.md` — one `## vX.Y.Z` section per release, newest first.
- `CONTRIBUTING.md` — test conventions, commit/PR flow, release process.

## Conventions the environment will not tell you

- Tests write a real `routes.json` into a temp plugin tree and repoint
  `dg.PLUGIN_DIR` there; the temp plugin tree must sit outside the temp
  workspace. Injecting only `_CONFIG_CACHE` silently yields `unconfigured`.
- Config hot-reloads by mtime on every call; `mode=off` is decided at factory
  time, so leaving `off` needs a fresh session.
- Branches: `fix|feat|docs/<topic>`; PRs squash-merge; a release is a version
  bump plus changelog section in the same PR — merging that to main tags and
  publishes automatically.
- The jev layer defaults off: every change must keep behaviour byte-identical
  with the `jev` block absent or disabled (tests assert this).
