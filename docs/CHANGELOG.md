# Changelog

## v0.1.11 — jev semantic arbitration layer (opt-in)

An opt-in L2 semantic arbitration layer for the two points where the
deterministic rules cannot decide. Judges are local System One services
inherited from the jev-kernel plugin's configuration (single-tier
whitelist); nothing reaches a cloud provider unless the operator
configures one inline.

- **P0 draft hook**: with jev enabled, `routes.draft.json` generation asks
  one Choice question per deliverable category in a single request
  (criteria = live agent ids + `unrouted`); uncertain or failed arbitration
  falls back to the keyword scan per category, so a draft never comes up
  empty.
- **P1 blind-spot hook**: a deliverable-shaped write whose keyword route
  names no live owner — previously a silent `no_owner` pass — gets one
  Choice arbitration (criteria = live agents + `basic_op` + `none`).
  Gating: p ≥ deny (0.85) with enforce denies naming the owner;
  warn (0.6) ≤ p < deny warns naming the owner; everything else is the
  unchanged pre-jev pass. The inventory-read-failure branch, L1 hits and
  the meta-integrity gate pay zero judge calls.
- **`backend/jev.py`**: stdlib-only System One client (raw dialect: baseUrl
  normalisation, noul/criteria translation, answer normalisation) mirroring
  jev-kernel's tested transports; `inherit:jev-kernel` resolves judge specs
  from the kernel's config (plugin dir → plugin-data) and honours its global
  kill-switch; hard timeout cap; every judge failure fails open (F17).
- **Audit**: records carry an optional `jev` subobject
  `{judge, latencyMs, probability, value, fallback}`; every other path stays
  byte-identical.
- **Config**: `jev` block in `routes.json` (`enabled` default false,
  `judges`, `tiers`, `thresholds`, `online_timeout_ms`, `record`); wrong
  types degrade per field. Absent or disabled block = byte-identical to
  v0.1.10.
- Docs: `docs/JEV_INTEGRATION.md` (design + measured latencies), README jev
  section and privacy wording. 210 tests (62 jev-layer tests across client,
  draft and blind-spot suites), ruff clean.

## v0.1.10 — v0.1.9 评审修复 (review-20261012)

Adversarial review 2026-10-12 (`review-20261012.md`) found 17 defects
(D1–D17) plus fail-open consistency items (F1–F18). Every fix landed on
`fix/review-20261012`; the review's 12-case evidence pack now passes 12/12,
and the repository suite covers every finding with targeted regression tests.
A second review pass over the fixes found ten more defects (B1–B10 —
a shell-arm false-positive surface, audit redaction gaps, meta-integrity
bypasses by mode, path shape, case and `cd`, a discovery fail-open, a
canonicalisation bypass, coverage gaps, a `~`-spelling hole, a gate that
could crash the middleware, and a strings walker that capped out) — all
closed and pinned by regressions on this branch.

- **D1 (Blocker) — the guarded agent can no longer rewrite its own dispatch
  table.** `routes.json`/`routes.draft.json` basenames are excluded from
  every whitelist (the plugin tree included), a write to either home is
  refused in every mode the middleware observes — `off` included, and
  whichever shape the path arrives in (string, list, dict, upper-case,
  trailing dot) — and only lint-clean tables mirror into plugin-data.
  The shell arm (D1-shell) matches the guard's two **real table paths**, not
  the bare basename: naming one warns, mutating one (redirect, `tee`,
  `sed -i`, `rm`/`mv`/`truncate`/`ln`, an in-process `open(..., "w")`) is
  refused in every mode and regardless of `shell_enforce`. Matching is
  case-insensitive and boundary-anchored (`routes.json.bak` is a backup, not
  the table; `ROUTES.JSON` IS the table on a case-insensitive host), expands
  `~`, and also covers a bare basename after a `cd` into a guard home in the
  same command. A project's own `routes.json` is never matched, so reading
  or grepping it stays untouched. The meta-integrity gate itself is total —
  it runs above the fail-open wrapper, and an unserializable input used to
  raise TypeError out of the middleware through it; it now fails open like
  everything else under F17.
- **D2 — dict-shaped `tool_call` no longer disarms the guard.** `on_acting`
  accepts Mapping-shaped calls; an unrecognizable shape is audited and
  spawn-suspect payloads are refused under the most conservative reading.
- **D3 — non-string `file_path` values (list/dict) are no longer str()-coerced
  into silent passes.** They route through the full-field scan; a deliverable
  inside them is treated as an explicit target.
- **D4 — `"match": null` degrades instead of crashing.** Normalization coerces
  null/non-list `match` to `[]` and members to strings; `_route_hit` skips
  non-strings. Per the ruling: a null-match rule owns nothing, so such a
  table passes writes with `no_owner` records rather than denying them.
- **D5 — a malformed agent manifest (int/bool `skills`) degrades to that
  agent not existing** instead of crashing every deliverable interception.
- **D6 — shell redirects `>|` (noclobber) and `tee` long options
  (`--append`) are visible targets.**
- **D7 — downloader/archiver/compiler/in-place-editor outputs are covered**:
  `curl -o/-O`, `wget -O`/`--output-document`, `dd of=`, `zip`/`tar` creation
  (short bundle `tar czf`/`tar cf` and long `--create --file[=]`), compiler
  `-o` for `gcc`/`g++`/`clang`/`clang++`/`cc`/`c++` in split and joined form,
  short output flags with the value joined (`curl -oFILE`), generic
  `--outdir`/`--output`, and `sed`/`perl -i` rewriting their trailing file
  argument. Read shapes (`tar xf`, `tar tf`, `sed -n`, `ffmpeg -i`), `find`'s
  `-o` operator and non-deliverable `-o` targets stay silent.
- **D8 — zero-agent environments now match the documented promise.** A
  readable-but-empty inventory is a genuine single-agent deployment: every
  class is `no_owner`, writes pass with an audit record. Discovery keeps
  trusting the operator's table only when it could not read the layout — the
  walk failed, **or a manifest exists but could not be opened**: a permission
  wall used to parse to nothing and report itself as "no specialists here",
  disarming the guard. Deny text never points at an invented "对应专业
  agent" — an unresolvable owner is handed back to the user.
- **D9 — extension perturbation cannot erase routing.** Trailing dots and
  whitespace are canonicalized to a fixed point before deliverable and route
  decisions, and `_whitelisted` canonicalizes through the same function:
  `report.docx.` routes as `report.docx`, and so does `report.docx .`, whose
  alternating dot-and-space ending used to survive a single strip-then-rstrip
  pass and match nothing at all — silently, with no audit record.
- **D10 — free-form task text is logged as a digest, not verbatim**:
  sha256[:16] + length + an excerpt filtered for known credential shapes
  (`sk-*`, AWS access key ids, `Bearer …`, `key=value` and `key is value`
  assignments). This covers the spawn backstop **and** a tool call whose shape
  the guard cannot parse, which is the path that used to write the raw
  structure — credential included — straight into the jsonl. The filter is not
  a secret scanner: a credential in an unrecognised spelling can still appear
  in the capped excerpt.
- **D11 — a legitimate path key no longer closes the scan of the rest of the
  input.** Deliverables hidden in other arguments earn a mention-level
  warning (never a deny from that tier).
- **D12 — one yardstick for whitelist and deliverable judgment**: both
  case-insensitive, matching `_is_deliverable`'s lowercasing; `Notes/x.docx`
  is no longer denied where `notes/x.docx` passes.
- **D13 — a parse failure keeps the last known-good table** with a loud
  warning instead of silently disarming; only a cold cache with nothing
  good ever loaded fails open to `off`.
- **D14 — `mode=off` no longer detaches the middleware.** The factory
  always attaches; off short-circuits inside `on_acting`, so an
  off→enforce flip hot-applies like every other flip, and each disarmed
  period is marked by one `action=off` audit record.
- **D15 — a missing `routes` key is a legal minimal config**, not a type
  error; the wrong-type warning fires only on an explicit null or wrong
  type.
- **D16 — non-string `deliverable_dirs` members are dropped with a warning**
  instead of `str()`-coercing into invented `none/` / `5/` directories.
- **D17 — the private-payload check matches table basenames at any directory
  depth**: `backend/routes.json` or `sub/routes.draft.json` in the candidate
  set can no longer slip into the published archive.
- **F17 — a guard-internal exception never bubbles out of the middleware**:
  it is host-logged, audited, and failed open with a visible warning block
  (the never-wedge rule), and a tool whose response already started is
  never re-run.
- Documentation synced to the implementation. `docs/DESIGN.md`: the
  "no owner, no block" asymmetry is now the three-state rule the code
  implements (specialists found / readable-empty / unreadable), "fail-open"
  describes the last-known-good fallback and names the guard's own table as
  the second exception, the shell tiers carry the meta-integrity exception,
  and the stale root-level `.pdf` claim (`review-20261012.md` §8.3 item 1) is
  corrected — `reports/q3.pdf` is deliverable, `.pdf` being a default
  extension, and was never passed. README and plugin.json `meta.notes`: off
  semantics and hot-reload, the shell coverage families, the no-owner rule,
  last-known-good
  fail-open, case-insensitive matching, the guard-table path matching, and
  what audit redaction does and does not catch.

## v0.1.9

- **Your dispatch table now survives plugin reinstalls.** The host installer
  rebuilds the plugin directory on every `plugin install --force` (rmtree +
  copytree), which is how a v0.1.8 hot-upgrade silently dropped a deployment
  back to `unconfigured` and destroyed an in-review draft. The plugin now
  keeps private state in `<host>/plugin-data/dispatch-guard/`, a location the
  installer never touches: the active table is looked up next to plugin.json
  first (the explicit override) and in plugin-data second, a plugin-dir table
  is mirrored into plugin-data after every reload (so operator edits never
  leave a stale rescue copy), and after a reinstall the plugin-data copy is
  what the guard finds.
- `routes.draft.json` moved to the plugin-data directory for the same reason,
  and an existing draft is now **adopted, not regenerated**: a draft from a
  previous process — including pre-migration ones left in the plugin
  directory — carries operator edits the draft table must not overwrite.
  Development checkouts without a host tree keep the historical layout
  (the plugin directory is its own data dir).
- Setup hints and the onboarding docs name the actual draft path and the
  two configuration homes.

## v0.1.8

- **Path traversal can no longer dress a destination as something else.** When
  a target escaped the workspace, `_rel` handed back the raw un-normalized
  string, so `notes/../../outputs/report.docx` inherited the `notes/` whitelist
  prefix and passed untouched in every mode while the identical real
  destination was denied; the reverse spelling was audited as a deliverable.
  Escaped targets now resolve to their real absolute location before any
  prefix matching, and unresolvable paths (`..` through a symlink loop) fall
  back to syntactic normalization.
- **Structurally wrong configs degrade instead of crashing.** A host
  config.json with `"agents": null` (or `profiles` as an array) raised
  `AttributeError` out of discovery and crashed every guarded tool call; a
  routes.json whose `ext_routes`/`routes` were the wrong JSON type failed open
  once and then crashed every later call from the poisoned cache. All config
  fields are now type-normalized at load — a wrong-typed field is dropped with
  a warning, and non-object routes.json fails open to `off`.
- **ffmpeg's real output form is covered.** ffmpeg has no `-o` flag, so
  `ffmpeg -i clip.mov clip.mp4` was invisible to the shell heuristics; the
  trailing positional output is now an explicit target (an `-i` input is not
  mistaken for one). Converter destination flags gained `--outdir` /
  `--output[-dir]=`, and naming the deliverable directory itself
  (`--outdir outputs`) counts as a target.
- **Draft dispatch policies lose fewer claims.** Sentences separated by `。`,
  claims phrased as "派给/派到/派至 X", and several claims in one segment are
  all parsed now; previously only the first claim per punctuation segment was
  read, silently dropping whole routing groups from routes.draft.json.
- A transient I/O error during agent discovery no longer caches a partial
  inventory under the unchanged stamp (which would have invented no_owner
  verdicts until the next manifest edit); failed discovery is retried on the
  next guarded call.
- Audit attribution: the generic deny hint "对应专业 agent" (no live
  discovery) is no longer counted as an agent by `audit_summary.py`, and
  glob patterns like `find . -name '*.docx'` no longer earn a shell warning.
- `packaging/release_notes.py` exits non-zero when the tag has no changelog
  section: the publish step runs under `set -e`, so a release now fails
  loudly instead of shipping placeholder notes.

## v0.1.7

- **The agent inventory is now hot-reloaded.** Discovery is fingerprinted by
  mtime over the discovery roots, every manifest, and the host config's
  enabled flags — adding, editing, parking or re-enabling an agent takes
  effect on the next guarded call, the same freshness routes.json always
  had. Previously the inventory was a per-process snapshot, so a parked
  specialist kept receiving routes (or a new one was never seen) until the
  host restarted.
- **Absolute-path writes into deliverable directories are seen.** `_rel`
  leaves absolute targets untouched, so the `outputs/` prefix match never
  fired for them; deliverable-directory matching is now segment-based for
  absolute paths (`/somewhere/outputs/report` is the deliverable dir wherever
  it sits). Extension matching already covered `.docx`-style absolute
  targets; this closes the extensionless hole. Workspace-relative paths keep
  prefix semantics, so `src/myapp/projects/notes.txt` stays a basic
  operation.
- Audit trail rotation: `dispatch_guard.jsonl` rolls into a single
  `.jsonl.1` backup past 1 MiB instead of growing forever.
- The release workflow now lints with ruff like CI does.
- Verification record (2026-10-05, enforce mode, draft-derived routes against
  the live inventory of seven specialist workspaces):
  - 18-case matrix design-consistent: whitelist, basic writes and reads pass
    silently; owned deliverables denied with the owning agent named — by
    extension, directory (relative *and* absolute), or keyword; unowned
    deliverable classes pass with `no_owner` audit records; shell explicit
    targets warn by default (`shell_enforce` opts into denial); quoted
    mentions warn only; spawn stays denied.
  - The three non-obvious cases were confirmed by direct audit-line
    inspection: an unmatched deliverable class passes silently under a live
    inventory (no owner, no block), keyword hits fire on absolute-path
    filenames, and the extensionless absolute-path case is now visible and
    routable.

## v0.1.6

- Review fixes to v0.1.5's coverage features — which had half-applied the
  permission-gate lesson they were built on:
  - `write_tools` now covers pathless tools. A code runner names its
    deliverable inside the argument body, not in a path field, so declaring
    it used to be fake coverage: the tool was intercepted but nothing was
    ever recognized. The input body is now scanned with the same patterns as
    the shell.
  - Shell explicit targets now include `cp`/`mv`/`rsync`/`install`
    destinations — moving a generated deliverable bypassed detection
    entirely, even with `shell_enforce` on.
  - Two confidence tiers: an *explicit* write target (redirect/tee/-o/cp
    destination) follows the full write rules and can be denied; a quoted
    *mention* of a deliverable path (shell-embedded code, grep arguments)
    warns but is never denied — a mention proves visibility, not effect.
- `audit_summary.py` attribution now ignores spawn-backstop denials, which
  name no dispatch agent and previously polluted the per-agent tally.
- Corrected the literature window stated in `docs/RESEARCH.md`.

## v0.1.5

Optimizations grounded in the recent guardrail/orchestration literature — the
full paper-to-decision mapping now lives in [docs/RESEARCH.md](RESEARCH.md).

- **Closed the shell-redirect bypass (opt-in `shell_enforce`).** A stress-test
  evaluation of Claude Code's auto mode (2026) measured an 81% end-to-end
  false-negative rate on a deployed tool-call gate, dominated by a *coverage
  gap*: agents achieve a blocked effect through a path the gate does not
  evaluate. dispatch-guard's analogue was the shell: under enforce, `echo x >
  outputs/r.docx` only warned. With `"shell_enforce": true`, enforce mode
  denies shell commands whose explicit output target is deliverable-shaped
  and owned by a live agent; basic and ambiguous targets still warn. Default
  remains off.
- **`write_tools` extends guarded coverage to host-specific tools.** Same
  coverage lesson: hosts grow file-producing tools (code runners,
  downloaders) the built-in list cannot know. routes.json can now name them;
  they then follow the exact write-tool rules (deliverable-shaped gate,
  whitelist, no-owner stand-down).
- **Load-time policy lint.** Following the static analyses of policy-compiler
  work (FORGE/PC-AS, 2026), routes.json is checked when loaded: keywords
  claimed by rules naming different agents (the earlier rule silently wins),
  ext_routes entries that can never fire because the extension is not a
  deliverable, and unknown `mode` values. Lint only logs — behaviour stays
  exactly as written.
- **`packaging/audit_summary.py`** — evidence for the warn→enforce promotion:
  per-action counts, denials per agent, and the `no_owner` classes that name
  specialist gaps. Per the escalation finding in the weak-to-strong
  monitoring literature (2025), escalating only pre-flagged cases to human
  review is where the accuracy is.

## v0.1.4

- **Discovery now reads the real QwenPaw host layout.** Found during a live
  test on a production install: agents live one-per-workspace at
  `~/.qwenpaw/workspaces/<id>/agent.json`, not only in workspace-local
  `agents/` trees the first cut scanned — so on a real deployment the
  inventory was empty and both the no-owner stand-down and the draft were
  dead code. The host tree is probed when the plugin genuinely lives under
  `~/.qwenpaw/plugins/`; a checkout elsewhere is never guessed at.
- **The orchestrator and disabled agents are never dispatch targets.**
  `default` is excluded from the inventory, and the host `config.json`
  `agents.profiles` enabled flag is honoured (a specialist parked for the
  day, like this deployment's Qoder, does not receive routes). A broken or
  missing config.json excludes nothing — fail open.
- **Drafts follow the environment's own declared policy.** The orchestrator's
  `agent.json` description states the routing intent in prose ("文档/报告/
  Office文档派 DocAgent，图形/图像/视频/PPT派 DesignAgent…"); the draft parses those
  "X派Y" claims and routes each deliverable category to the agent the
  operator already named, dropping claims that point at agents outside the
  live inventory. The id/skills/description scan remains only as a fallback
  for deployments without a written policy. On the reference deployment the
  generated draft now routes documents→DocAgent, media→DesignAgent, CAD→HardwareAgent
  with archives unrouted — matching the declared policy exactly.

## v0.1.3

- **No owner, no block.** A denial is only useful if it names a dispatch
  target that exists. The guard now discovers the environment's specialist
  agents (from `agents/*/agent.json` manifests in the workspace and the
  host-level agents directory) and validates every route against that
  inventory before blocking: a route naming an absent agent — or a
  deliverable class no discovered agent owns — passes untouched and is
  recorded as `no_owner` in the audit log. Blocking a write you cannot route
  just strands the deliverable. An *empty* discovery does not disarm the
  guard: an unreadable deployment falls back to trusting the operator's table
  verbatim.
- **Fresh installs get a drafted table, not an interrogation.** In
  `unconfigured` mode the plugin scans the discovered agents'
  id/skills/description and generates `routes.draft.json` (`mode=warn`) from
  deliverable categories: each category routes to the first agent that
  mentions it, and categories with no matching agent stay unrouted. The
  plugin never writes `routes.json` itself — activation is an explicit
  rename after review. The draft names real agents, so it is gitignored and
  the build refuses to package it, exactly like the hand-written table.
- **Basic operations are no longer intercepted.** Previously every write
  outside the whitelist was treated as a deliverable, so `enforce` denied
  ordinary file operations (a root `notes.md`, `scripts/setup.py`, a
  `Makefile`). The guard now only sees **deliverable-shaped** targets: under
  `outputs/`/`projects/` (configurable via `deliverable_dirs`) or carrying a
  deliverable extension such as `.docx` (configurable via `deliverable_exts`,
  which extends the built-in list). Reads, whitelist writes, and all other
  basic file operations pass untouched in every mode, without log entries —
  and the middleware still attaches to the default agent only, so no other
  agent was ever touched.
- The shell heuristic now shares the same deliverable predicate: a redirect
  into a notes file or a script stays silent (`.py`/`.md` no longer count as
  deliverables), while `pandoc -o outputs/r.docx` and redirects into
  deliverable targets still warn. Shell remains warn-only in every mode.
- **Releases are now automatic.** Pushing a version bump to `main` (a changed
  `plugin.json` version with no matching tag) makes the release workflow tag
  `v<version>` itself, run the suite, build the archive and publish the GitHub
  release — no manual tagging step. Pushing a `v*` tag by hand still works.
- **Fixed: release notes were placeholders.** `release_notes.py` looked for a
  `### vX.Y.Z` heading while the changelog uses `## vX.Y.Z`, so every release
  page would have carried the "See docs/CHANGELOG.md" filler instead of the
  actual notes. The extractor now accepts both levels and a regression test
  pins it to the real changelog.
- The release asset is named after the plugin (`dispatch-guard.zip`), matching
  the stable-URL convention of agent-shepherd: the version lives inside
  `plugin.json` where the host reads it, and
  `releases/latest/download/dispatch-guard.zip` never changes between releases.
  The release notes also carry the archive's sha256.
- New release-hygiene tests (pattern from agent-shepherd's
  `test_release_hygiene.py`): `plugin.json` version must equal the newest
  changelog section, the changelog section must actually be extractable as
  release notes, and the README's install URL must name the real asset.
- CI now runs `ruff check .` (pinned to 0.16.8) alongside the test matrix.

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
