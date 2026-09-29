# dispatch-guard research notes

The design decisions in this plugin are grounded in recent literature on LLM
multi-agent orchestration and runtime guardrails (2024–2026 unless noted).
This file maps each paper's finding to what it validates or changed here, so
design discussions start from evidence instead of taste.

## The failure mode is real and documented

- **MAS-Orchestra** (2026) — *understanding and improving multi-agent
  reasoning through holistic orchestration*. Empirically records exactly the
  behaviour this plugin exists for: capable orchestrator LLMs "tend to solve
  the task itself first and then delegate it to only one simple sub-agent,
  even when the sub-agent is stronger" — direct-task-solving is preferred
  over delegation because of end-to-end training objectives. Prompt-level
  dispatch rules compete with that instinct; this plugin moves the rule to
  the tool seam where the preference loses.
- **Adaptive governance of agentic AI systems** (2024) — role-based agent
  architectures erode in practice: "agents may ignore their assigned roles,
  duplicate each other's work, or deviate from collective goals". Independent
  confirmation that role adherence needs an enforcement layer.
- **Traceability and accountability in role-specialized multi-agent LLM
  pipelines** (2025) — structured, accountable handoffs between agents
  markedly improve accuracy over free-form pipelines. The denial message's
  "use `submit_to_agent` to dispatch to {agent}" is that structured handoff,
  delivered at the moment of violation.

## Why enforce at the tool seam, and what the seam must cover

- **AEGIS** (2025), **AgentSpec** (2025), **Progent** (2025), **FORGE /
  PC-AS** (2026) — an established line of runtime policy-enforcement systems
  that intercept tool calls and evaluate declarative policies (DSLs up to
  Datalog). dispatch-guard is a deliberately minimal member of this family:
  one middleware, one JSON policy, no dependency. FORGE's static analyses
  (contradiction, redundancy, dead rules) motivated the load-time **policy
  lint** added in v0.1.5.
- **Measuring the permission gate: a stress-test evaluation of Claude Code's
  auto mode** (2026) — the single most consequential finding for this design:
  a deployed tool-call gate showed an 81% end-to-end false-negative rate on
  ambiguous workloads largely because of a **coverage gap** — "agents
  routinely achieve equivalent effects through [paths] the classifier does
  not evaluate", e.g. falling back to file edits when the expected CLI is
  unavailable. Consequences adopted (v0.1.5–v0.1.6):
  - `write_tools` (routes.json) lets operators extend the guarded tool set
    with host-specific file-producing tools instead of assuming the built-in
    list is complete. Because those tools carry paths inside code arguments,
    v0.1.6 scans the input body itself with two confidence tiers: an
    *explicit* write target (redirect/tee/`-o`/cp-family destination — also
    how v0.1.6 closed the shell copy bypass) is treated like a write-tool
    path and can be denied; a quoted *mention* only ever warns, because a
    mention proves visibility, not effect.
  - `shell_enforce` (opt-in) narrows the biggest known hole in this plugin —
    shell redirects into deliverable targets were warn-only, which is exactly
    the "agent routes around the gate through another path" pattern. Default
    stays off: the shell heuristic is content-based, and a blanket deny would
    wedge legitimate work.
- **CaMeL** (2025) and the **layered governance architecture** work (2026) —
  separation between planning and acting, and audit logging as a first-class
  layer. The append-only `dispatch_guard.jsonl` trail is the local version of
  the latter.
- **Swiss-cheese taxonomy of runtime guardrails for FM-based agents**
  (2024/2025) — guardrails are layers, not a wall. dispatch-guard claims one
  layer (deliverable routing at the tool seam of one host); it explicitly
  does not try to be the security boundary.

## Oversight, escalation, and the evaluation loop

- **Reliable weak-to-strong monitoring of LLM agents** (2025) — escalating
  *only pre-flagged cases* to human review improved true-positive rates
  ~15% at FPR=0.01. The plugin's mode ladder — `unconfigured` (draft) →
  `warn` (observe) → `enforce` (act) — is that escalation path, with the
  audit log feeding the human decision.
- **Evaluation-driven development and operations of LLM agents (EDDOps)**
  (2025) — evaluation should be a continuous closed loop, not a terminal
  checkpoint. `packaging/audit_summary.py` is the offline half: per-action
  counts, hint hit-rate per agent, and the `no_owner` classes that name
  specialist gaps, so the operator's warn→enforce promotion is data-backed.
- **How does information access affect LLM monitors' ability to detect
  sabotage?** (2026) — monitors can perform better with *less* information,
  and agent awareness of being monitored degrades detection. dispatch-guard
  is deliberately *visible* (its denials announce the rule and the owner):
  it is a steering device for a non-adversarial failure mode, not a
  deception detector — a different point in the design space this paper
  maps.
- **Who&When** (2025) / **MP-Bench** (2026) — failure attribution in
  multi-agent systems is hard; structured per-event audit records (action,
  tool, target, mode, message) keep this plugin's interventions attributable
  after the fact.

## Deliberate non-goals, per the same literature

- **ProbGuard** (2025) — probabilistic risk prediction from learned trace
  models is promising but heavy; dispatch-guard stays deterministic and
  observable, the regime the runtime-enforceability literature reserves for
  execution-time intervention.
- **WebGuard** (2025) — risk-tiered action classification (SAFE/LOW/HIGH)
  needs near-perfect recall for high-stakes use; a keyword/extension table
  must not pretend to that. The deliverable-shaped gate is a routing
  heuristic with honest failure modes (`no_owner`, `needs_config`), not a
  risk model.
