"""dispatch-guard: enforce the default agent's output routing at the tool seam.

Three behaviours, gated by ``routes.json`` ``mode`` (enforce | warn | off):

* ``write_file``/``edit_file``/``append_file`` to a **deliverable-shaped**
  target outside the whitelist — under a deliverable directory such as
  ``outputs/``/``projects/`` or carrying a deliverable extension such as
  ``.docx`` -> DENIED in enforce mode with a "this belongs to {agent}, use
  submit_to_agent" message; in warn mode the call proceeds and a warning
  block is appended. Basic file operations are never the guard's business:
  every read passes, and writes of notes, scripts, configs, scratch or
  extension-less files pass untouched in every mode, for every agent.
* ``execute_shell_command`` whose command line names a deliverable-shaped
  output file (redirect/tee/pandoc/ffmpeg) -> warn-only (append a block),
  never denied, to avoid false positives on status-check commands.
* ``spawn_subagent`` -> DENIED as a backstop for the config-layer disable.

Two environment-aware rules sit above all of that. A denial must name a
**real** dispatch target: routes are validated against the agents actually
installed here (discovered from ``agents/*/agent.json`` manifests in the
workspace and the host tree), and a deliverable whose owner does not exist
passes with an ``no_owner`` audit record instead of being stranded — no owner,
no block. And a fresh install does not ask the operator to hand-write a table
from nothing: unconfigured mode scans the discovered agents' skills and drafts
``routes.draft.json`` (warn mode) for review; it never writes ``routes.json``
itself.

A freshly installed plugin has no ``routes.json`` at all (the repository ships
only ``routes.example.json``). That state is ``unconfigured``, not ``off``: the
middleware attaches, never denies a write, and appends a block telling the
agent to build the dispatch table together with the user. Detaching instead
would report a healthy install that enforces nothing.

Whitelist = orchestration/meta paths only: memory/, notes/, wiki/, episodes/,
tmp/, trash/, logs/ plus the top-level MD baselines and skill.json/agent.json.
Every interception is appended to ``<workspace>/logs/dispatch_guard.jsonl``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

try:
    from agentscope.message import TextBlock, ToolResultState
    from agentscope.middleware import MiddlewareBase
    from agentscope.tool import ToolResponse
except ImportError:  # allow unit tests and static checks without AgentScope
    from dataclasses import dataclass, field

    class MiddlewareBase:  # type: ignore[no-redef]
        pass

    @dataclass
    class TextBlock:  # type: ignore[no-redef]
        type: str = "text"
        text: str = ""

    @dataclass
    class ToolResponse:  # type: ignore[no-redef]
        content: list = field(default_factory=list)
        state: object = "success"

    class ToolResultState:  # type: ignore[no-redef]
        DENIED = "denied"
        SUCCESS = "success"

try:
    from qwenpaw.plugins.api import PluginApi
except ImportError:  # allow unit tests without QwenPaw installed

    class PluginApi:  # type: ignore[no-redef]
        pass

logger = logging.getLogger("dispatch_guard.qwenpaw")

PLUGIN_DIR = Path(__file__).resolve().parent.parent

# Audit trail rotation threshold: roll dispatch_guard.jsonl into a single
# .jsonl.1 backup when it exceeds this size.
AUDIT_MAX_BYTES = 1 << 20

WRITE_TOOLS = {
    "write_file",
    "edit_file",
    "append_file",
    "create_file",
    "write_text_file",
    "edit_text_file",
}
SHELL_TOOLS = {"execute_shell_command", "run_command"}
SPAWN_TOOLS = {"spawn_subagent", "spawn_agent"}

# A write is the guard's business only when the target is deliverable-shaped:
# inside one of these workspace directories, or carrying one of these
# extensions. Everything else — notes, scripts, configs, scratch files, files
# without an extension — is a basic operation that passes untouched in every
# mode. routes.json's ``deliverable_dirs``/``deliverable_exts`` keys extend
# these lists; they never remove from them.
DEFAULT_DELIVERABLE_DIRS = ("outputs/", "projects/")
DEFAULT_DELIVERABLE_EXTS = frozenset({
    # documents and data
    ".doc", ".docx", ".pdf", ".ppt", ".pptx", ".xls", ".xlsx", ".csv",
    ".odt", ".ods", ".odp", ".epub",
    # archives
    ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar",
    # images
    ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    # audio/video
    ".mp3", ".mp4", ".mov", ".mkv", ".wav", ".webm",
    # CAD / hardware artifacts
    ".dxf", ".step", ".stp", ".stl", ".gcode", ".kicad_pcb",
    # installers and images of disks
    ".exe", ".apk", ".dmg", ".iso",
})

WHITELIST_DIRS = (
    "memory/",
    "notes/",
    "wiki/",
    "episodes/",
    "tmp/",
    "trash/",
    "logs/",
)
WHITELIST_FILES = {
    "MEMORY.md",
    "PROFILE.md",
    "AGENTS.md",
    "SOUL.md",
    "HEARTBEAT.md",
    "skill.json",
    "agent.json",
}

# Command-line shapes that name an output FILE (redirect, tee, a converter's
# -o, or the destination of cp/mv/rsync/install). Whether that file is a
# deliverable is decided by _is_deliverable(), not here — so a redirect into
# a notes file never trips the heuristic. The cp-family pattern is greedy so
# the captured token is the segment's last argument = the destination.
SHELL_OUTPUT_PATTERNS = (
    re.compile(r"(?:>>|\btee\b(?:\s+-\w+)*\s+|>)\s*([^\s;&|]+)", re.I),
    re.compile(
        r"\b(?:pandoc|ffmpeg|soffice|libreoffice)\b[^;&|]*?"
        r"(?:--outdir(?:=|\s+)|--output(?:-dir)?(?:=|\s+)|-o\s+)([^\s;&|]+)",
        re.I,
    ),
    re.compile(r"\b(?:cp|mv|rsync|install)\b[^;&|]*\s+([^\s;&|]+)", re.I),
    # ffmpeg's real output form is positional: the last argument of the
    # command line, carrying an extension. The -i lookahead keeps a bare
    # converter line from matching, and the lookbehind keeps an `-i` input
    # ("ffmpeg -i clip.mov", a probe, not a write) from posing as the output.
    re.compile(
        r"\bffmpeg\b(?=[^;&|]*\s-i\s)[^;&|]*?\s(?<!-i\s)"
        r"([^\s;&|]*\.[A-Za-z0-9]{1,12})(?=\s*(?:[;&|]|$))",
        re.I,
    ),
)
# Quoted file paths appearing anywhere in a command or code body. A path in
# a quoted string is a MENTION — it names a deliverable the tool may write
# (code runners take their paths inside the code argument), but it may also
# be a probe, a test fixture name, or prose. Mentions earn a warning, never
# a denial: the permission-gate literature's coverage lesson is about seeing
# these paths, and the fail-open rule is about not stranding work on a guess.
MENTION_PATH_RE = re.compile(r"""["']([^"'`]*\.[A-Za-z0-9]{1,12})["']""")

_CONFIG_CACHE: dict[str, Any] = {"mtime": 0.0, "data": None, "source": None}
_UNCONFIGURED_LOGGED = False

UNCONFIGURED = "unconfigured"
SETUP_HINT = (
    "[dispatch-guard 未配置] 没有 routes.json（插件目录和 plugin-data 目录都查过），"
    "派发表为空，当前只提示不拦截。请和用户一起确认各类产出归属哪个 agent，"
    "把结论写成 routes.json（可从 routes.example.json 复制起步）："
    "mode 先设 warn 观察一轮拦截日志，确认派发表无误后再切 enforce。"
    "目标：{target}"
)


def _warn_unconfigured() -> None:
    """Announce the unconfigured state once per process.

    ``on_acting`` re-reads the config on every tool call, so logging there
    would flood the host log for an install the user has not configured yet.
    """
    global _UNCONFIGURED_LOGGED
    if _UNCONFIGURED_LOGGED:
        return
    _UNCONFIGURED_LOGGED = True
    logger.warning(
        "dispatch-guard: no routes.json in %s — attached in unconfigured mode, "
        "deliverable writes are warned but not denied until a dispatch table exists.",
        PLUGIN_DIR,
    )


def _normalize_config(data: Any) -> dict[str, Any]:
    """Coerce a structurally wrong routes.json into the shape the runtime
    reads, so one field's type mistake degrades that field to "absent"
    instead of crashing every interception later (the fail-open rule is
    about not wedging the agent on a config typo). Each dropped field says
    so in the host log, once per file change.
    """
    if not isinstance(data, dict):
        logger.warning(
            "dispatch-guard: routes.json must be a JSON object with a mode "
            "field; the file is ignored (failing open to off)"
        )
        return {"mode": "off", "routes": [], "ext_routes": {}}
    cfg = dict(data)
    if not isinstance(cfg.get("ext_routes"), dict):
        logger.warning(
            "dispatch-guard: routes.json 'ext_routes' must be a JSON object; "
            "extension routing is ignored"
        )
        cfg["ext_routes"] = {}
    routes = cfg.get("routes")
    if not isinstance(routes, list):
        logger.warning(
            "dispatch-guard: routes.json 'routes' must be a JSON array; "
            "keyword routing is ignored"
        )
        routes = []
    cleaned: list[Any] = []
    for rule in routes:
        if not isinstance(rule, dict):
            logger.warning(
                "dispatch-guard: routes entry %r is not an object; dropped", rule
            )
            continue
        match = rule.get("match")
        if match is not None and not isinstance(match, list):
            logger.warning(
                "dispatch-guard: route for %r has a non-array 'match'; its "
                "keywords are dropped",
                rule.get("agent"),
            )
            rule = {**rule, "match": []}
        cleaned.append(rule)
    cfg["routes"] = cleaned
    return cfg


def _data_dir() -> Path:
    """Durable home for the operator's private plugin state.

    The host installer replaces the plugin directory wholesale on every
    reinstall (rmtree + copytree), so anything the operator owns — the
    dispatch table, the review draft — must not live only there. Under a
    real host tree the data dir sits in ``<host>/plugin-data/<plugin-id>``,
    outside ``plugins/`` and outside every scanned workspace, so it
    survives reinstalls untouched. Without a host tree (development
    checkouts, unit tests) the plugin directory itself is the data dir,
    which keeps the historical layout working.
    """
    host = _host_tree()
    if host is None:
        return PLUGIN_DIR
    return host / "plugin-data" / PLUGIN_DIR.name


def _config_source() -> tuple[Path, str] | None:
    """(path, origin) of the active dispatch table, or None.

    The plugin directory wins: a table the operator hand-placed next to
    plugin.json is the explicit override. The plugin-data copy is what
    makes the table survive an installer that rebuilds the plugin
    directory — after a reinstall it is the copy that is found.
    """
    plugin_copy = PLUGIN_DIR / "routes.json"
    if plugin_copy.is_file():
        return plugin_copy, "plugin"
    data_copy = _data_dir() / "routes.json"
    if data_copy.is_file():
        return data_copy, "data"
    return None


def _mirror_to_data_dir(plugin_copy: Path) -> None:
    """Keep the plugin-data copy of the table in step with the plugin-dir one.

    Runs after every reload of a plugin-dir table, so an operator's edits
    never leave a stale rescue copy behind. Best-effort by design: an
    unwritable data dir degrades to the old behavior (the table lives only
    next to plugin.json and a reinstall wipes it), never to a broken guard.
    """
    data_copy = _data_dir() / "routes.json"
    if data_copy == plugin_copy:
        return
    try:
        if data_copy.is_file() and data_copy.read_bytes() == plugin_copy.read_bytes():
            return
        data_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(plugin_copy, data_copy)
        logger.info(
            "dispatch-guard: mirrored routes.json into %s — that copy "
            "survives plugin reinstalls",
            data_copy,
        )
    except OSError:
        logger.warning(
            "dispatch-guard: could not mirror routes.json into %s; a plugin "
            "reinstall will lose the table until the file is re-created",
            data_copy,
        )


def _load_config() -> dict[str, Any]:
    """Read routes.json, cached by mtime so mode flips apply without reload.

    The table is looked up in two homes: next to plugin.json (the explicit
    override) and in the plugin-data dir (the copy that survives a
    reinstall). A table found next to plugin.json is mirrored into the
    data dir, so the next ``plugin install --force`` — which rebuilds the
    plugin directory — finds it there instead of dropping the deployment
    back to unconfigured.
    """
    source = _config_source()
    if source is None:
        _warn_unconfigured()
        return {"mode": UNCONFIGURED, "routes": [], "ext_routes": {}}
    path, origin = source
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _warn_unconfigured()
        return {"mode": UNCONFIGURED, "routes": [], "ext_routes": {}}
    if (
        _CONFIG_CACHE["data"] is None
        or mtime != _CONFIG_CACHE["mtime"]
        or _CONFIG_CACHE["source"] != str(path)
    ):
        try:
            # Normalize BEFORE committing to the cache: the cache must only
            # ever hold dicts the runtime can read, or a malformed rewrite
            # would fail open once and then crash every later call.
            data = _normalize_config(json.loads(path.read_text(encoding="utf-8")))
            _CONFIG_CACHE["data"] = data
            _CONFIG_CACHE["mtime"] = mtime
            _CONFIG_CACHE["source"] = str(path)
            _lint_policy(data)
        except Exception:  # noqa: BLE001 - fail open
            logger.exception("dispatch-guard: bad routes.json, failing open")
            return {"mode": "off", "routes": [], "ext_routes": {}}
        if origin == "plugin":
            _mirror_to_data_dir(path)
    return _CONFIG_CACHE["data"]


def _lint_policy(cfg: dict[str, Any]) -> None:
    """Static checks on the dispatch table, in the spirit of policy-compiler
    work on agentic systems (contradiction / redundancy / dead-rule analyses):
    a table that cannot mean what its author wrote should say so in the host
    log at load time, not surprise them at interception time. Lint never
    changes behaviour — the runtime rules stay exactly as written.
    """
    mode = cfg.get("mode")
    if mode is not None and mode not in ("enforce", "warn", "off", UNCONFIGURED):
        logger.warning(
            "dispatch-guard lint: mode %r is not enforce|warn|off; it will be "
            "treated as warn",
            mode,
        )
    seen: dict[str, tuple[str, int]] = {}
    for index, rule in enumerate(cfg.get("routes") or []):
        if not isinstance(rule, dict):
            continue
        agent = str(rule.get("agent", ""))
        for keyword in rule.get("match") or []:
            key = str(keyword).lower()
            previous = seen.get(key)
            if previous is not None and previous[0] != agent:
                logger.warning(
                    "dispatch-guard lint: keyword %r is routed to both %s (rule %d) "
                    "and %s (rule %d); the earlier rule wins",
                    keyword,
                    previous[0],
                    previous[1],
                    agent,
                    index,
                )
            seen[key] = (agent, index)
    deliverable_exts = _deliverable_exts(cfg)
    for ext, agent in (cfg.get("ext_routes") or {}).items():
        normalized = str(ext).lower()
        if not normalized.startswith("."):
            normalized = f".{normalized}"
        if normalized not in deliverable_exts:
            logger.warning(
                "dispatch-guard lint: ext_routes entry %s -> %s can never fire: "
                "%s is not a deliverable extension (see deliverable_exts); writes "
                "with it pass as basic operations",
                ext,
                agent,
                ext,
            )


def _write_tools(cfg: dict[str, Any]) -> frozenset[str]:
    """Write-capable tool names: the built-ins plus operator extensions.

    Permission-gate evaluations of deployed coding agents found the dominant
    enforcement gap is coverage: agents achieve a blocked effect through a
    tool path the gate does not evaluate. Hosts grow host-specific
    file-producing tools (code runners that save artifacts, downloaders), so
    routes.json ``write_tools`` extends the built-in set instead of leaving
    those paths unguarded by construction.
    """
    tools = set(WRITE_TOOLS)
    custom = cfg.get("write_tools")
    if isinstance(custom, list):
        tools.update(str(name) for name in custom if str(name).strip())
    return frozenset(tools)


def _shell_enforce(cfg: dict[str, Any]) -> bool:
    """Whether enforce mode may deny shell commands that produce deliverables.

    Default off: shell heuristics are content-based, and a blanket shell deny
    wedges legitimate work. When on, only the unambiguous shapes are denied —
    an explicit output target (redirect/tee/-o) that is deliverable-shaped
    AND owned by a live agent; everything else still warns.
    """
    return cfg.get("shell_enforce") is True


def _deliverable_dirs(cfg: dict[str, Any]) -> tuple[str, ...]:
    """Deliverable directories: the defaults plus whatever routes.json adds."""
    dirs = list(DEFAULT_DELIVERABLE_DIRS)
    custom = cfg.get("deliverable_dirs")
    if isinstance(custom, list):
        dirs += tuple(
            str(d).strip().lower().rstrip("/") + "/" for d in custom if str(d).strip()
        )
    return tuple(dirs)


def _deliverable_exts(cfg: dict[str, Any]) -> frozenset[str]:
    """Deliverable extensions: the defaults plus whatever routes.json adds."""
    exts = set(DEFAULT_DELIVERABLE_EXTS)
    custom = cfg.get("deliverable_exts")
    if isinstance(custom, list):
        for entry in custom:
            ext = str(entry).strip().lower()
            if not ext:
                continue
            exts.add(ext if ext.startswith(".") else f".{ext}")
    return frozenset(exts)


# ---------------------------------------------------------------------------
# Environment discovery: which specialist agents actually exist here?
#
# A denial that routes a deliverable to an agent which is not installed just
# strands the write — there is nowhere to dispatch to. Two layouts are probed:
# workspace-local ``agents/*/agent.json`` trees, and the real QwenPaw host
# layout (one workspace per agent, manifest at ``~/.qwenpaw/workspaces/<id>/
# agent.json``) when the plugin genuinely lives inside ``~/.qwenpaw/plugins/``.
# The host's ``config.json`` ``agents.profiles`` carries the authoritative
# enabled flag; explicitly disabled agents (e.g. a specialist parked for the
# day) are excluded from the inventory, and so is ``default`` — the
# orchestrator is never its own dispatch target.

_AGENTS_CACHE: dict[str, Any] = {"ws": None, "agents": None, "ids": frozenset(), "stamp": None}
_DRAFT_WRITTEN = False
_NO_OWNER_LOGGED = False

_POLICY_SEGMENT = re.compile(r"[，,；;\n。！？]")
_POLICY_CLAIM = re.compile(r"(.+?)派(?:给|到|至)?\s*([A-Za-z_][A-Za-z0-9_-]*)")
_POLICY_TOKEN = re.compile(r"[/、]")

# Category presets used to draft a dispatch table from discovered agents:
# each group's extensions route to the first agent whose id / skills /
# description mentions any of the group's terms. Groups with no matching agent
# stay unrouted — those deliverable classes pass (no owner, no block).
_DRAFT_PRESETS = (
    {
        "exts": (".doc", ".docx", ".pdf", ".ppt", ".pptx", ".xls", ".xlsx", ".csv", ".epub"),
        "match": ("doc", "document", "writer", "report", "文档", "报告", "写作", "汇报"),
    },
    {
        "exts": (".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp", ".mp4", ".mov", ".mp3", ".wav"),
        "match": ("design", "image", "visual", "video", "media", "图", "设计", "海报", "视频"),
    },
    {
        "exts": (".dxf", ".step", ".stp", ".stl", ".gcode", ".kicad_pcb"),
        "match": ("hardware", "pcb", "cad", "firmware", "硬件", "固件", "制图"),
    },
    {
        "exts": (".zip", ".tar", ".gz", ".tgz", ".7z"),
        "match": ("ops", "release", "build", "devops", "运维", "发布", "部署"),
    },
)


def _parse_agent_manifest(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    agent_id = data.get("id") or data.get("agent_id") or data.get("name")
    if not agent_id:
        return None
    skills = data.get("skills") or data.get("capabilities") or []
    if isinstance(skills, str):
        skills = [s.strip() for s in re.split(r"[,;/]", skills) if s.strip()]
    return {
        "id": str(agent_id),
        "skills": [str(s) for s in skills],
        "description": str(data.get("description") or data.get("intro") or ""),
    }


def _host_tree() -> Path | None:
    """QwenPaw's home directory, or None outside a host plugin install.

    Real installs sit at ``~/.qwenpaw/plugins/<id>``; the checkout used for
    development does not, and must never have its parent directories guessed
    at. Everything host-level (workspaces/, config.json) hangs off this root.
    """
    if PLUGIN_DIR.parent.name == "plugins":
        return PLUGIN_DIR.parent.parent
    return None


def _disabled_agent_ids() -> frozenset[str]:
    """Agent ids the host has explicitly disabled (config.json profiles).

    Reading the host config is best-effort: an unreadable, absent or
    structurally wrong file means no exclusions, never a smaller inventory —
    a missing enable-flag must not invent a "no owner" verdict.
    """
    host = _host_tree()
    if host is None:
        return frozenset()
    try:
        cfg = json.loads((host / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    agents = cfg.get("agents") if isinstance(cfg, dict) else None
    profiles = agents.get("profiles") if isinstance(agents, dict) else None
    if not isinstance(profiles, dict):
        return frozenset()
    return frozenset(
        str(aid)
        for aid, profile in profiles.items()
        if isinstance(profile, dict) and profile.get("enabled") is False
    )


def _discovery_roots(ws: Path) -> list[Path]:
    roots = [ws / "agents", ws / ".qwenpaw" / "agents"]
    host = _host_tree()
    if host is not None:
        # Real QwenPaw layout: one workspace per agent with its manifest at
        # workspaces/<id>/agent.json; the legacy host agents dir is probed too.
        roots.append(host / "workspaces")
        roots.append(host / "agents")
    return roots


def _inventory_stamp(ws_path: Path) -> tuple:
    """Fingerprint of the inputs discovery reads: directory mtimes (which
    change when an agent is added or removed), each manifest's mtime (which
    changes when an agent's duties are edited), and the host config.json's
    mtime (which carries the enabled flags). Discovery re-runs whenever the
    fingerprint moves — the inventory is hot-reloaded the same way
    routes.json is, so an agent parked or enabled mid-process takes effect
    without a host restart.
    """
    parts: list = []
    host = _host_tree()
    if host is not None:
        parts.append(_mtime_or_none(host / "config.json"))
    for root in _discovery_roots(ws_path):
        parts.append(_mtime_or_none(root))
        try:
            manifests = sorted(root.glob("*/agent.json")) + sorted(root.glob("agent.json"))
        except OSError:
            parts.append(None)
            continue
        for manifest in manifests:
            parts.append(_mtime_or_none(manifest))
    return tuple(parts)


def _mtime_or_none(path: Path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _discover_agents(ws: str | Path) -> list[dict[str, Any]]:
    """Agents installed in this environment, cached per workspace until the
    inventory inputs change (see _inventory_stamp).

    ``default`` (the orchestrator this middleware attaches to) and explicitly
    disabled agents are never dispatch targets and are left out.
    """
    ws_path = Path(ws)
    stamp = _inventory_stamp(ws_path)
    if (
        _AGENTS_CACHE["ws"] == str(ws_path)
        and _AGENTS_CACHE["agents"] is not None
        and _AGENTS_CACHE.get("stamp") == stamp
    ):
        return _AGENTS_CACHE["agents"]
    found: dict[str, dict[str, Any]] = {}
    discovery_failed = False
    try:
        for root in _discovery_roots(ws_path):
            if not root.is_dir():
                continue
            manifests = sorted(root.glob("*/agent.json")) + sorted(root.glob("agent.json"))
            for manifest in manifests:
                parsed = _parse_agent_manifest(manifest)
                if parsed and parsed["id"] not in found:
                    found[parsed["id"]] = parsed
    except OSError:
        logger.exception("dispatch-guard: agent discovery failed")
        # Do not cache a partial inventory: the stamp did not move, so a
        # cached failure would never be retried and would invent no_owner
        # verdicts for agents that are actually installed.
        discovery_failed = True
    excluded = _disabled_agent_ids() | {"default"}
    agents = [
        found[aid]
        for aid in sorted(found)
        if aid not in excluded
    ]
    if not discovery_failed:
        _AGENTS_CACHE.update(
            {
                "ws": str(ws_path),
                "agents": agents,
                "ids": frozenset(a["id"] for a in agents),
                "stamp": stamp,
            }
        )
    return agents


def _dispatch_policy(ws: str | Path, agents: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(keyword token -> agent id) parsed from the orchestrator's own words.

    Deployments state their routing intent in the default agent.json
    description ("文档/报告/Office文档派 DocAgent，代码/仿真/CI派 CodeAgent…").
    Each "X派Y" claim yields one token per slash-separated term on the left;
    every claim in a segment is taken (finditer), and segment order is
    declaration order, so earlier claims outrank later ones. Claims naming an
    agent that is not in the live inventory are dropped — a policy pointing
    at a disabled or removed specialist is not a route.
    """
    try:
        manifest = json.loads((Path(ws) / "agent.json").read_text(encoding="utf-8"))
        description = str(manifest.get("description") or "")
    except (OSError, ValueError):
        return []
    known = {agent["id"] for agent in agents}
    policy: list[tuple[str, str]] = []
    for segment in _POLICY_SEGMENT.split(description):
        for claim in _POLICY_CLAIM.finditer(segment):
            if claim.group(2) not in known:
                continue
            for token in _POLICY_TOKEN.split(claim.group(1)):
                token = token.strip()
                if token:
                    policy.append((token, claim.group(2)))
    return policy


def _draft_route_table(ws: str | Path) -> dict[str, Any] | None:
    """Map discovered agents onto deliverable categories, or None if vacuous.

    Routing follows the environment's own declared intent first: the
    orchestrator's "X派Y" policy (parsed from its agent.json description)
    decides each category. Only when no policy is parseable does the draft
    fall back to guessing from agent ids/skills/descriptions — a terse
    manifest scan invents ownership that the deployment never stated.
    """
    agents = _discover_agents(ws)
    if not agents:
        return None
    cfg: dict[str, Any] = {"mode": "warn", "routes": [], "ext_routes": {}}
    policy = _dispatch_policy(ws, agents)
    routed = False
    for preset in _DRAFT_PRESETS:
        owner = None
        if policy:
            lowered = [(token.lower(), agent_id) for token, agent_id in policy]
            for term in preset["match"]:
                term = term.lower()
                for token, agent_id in lowered:
                    if term in token or token in term:
                        owner = agent_id
                        break
                if owner:
                    break
        else:
            for agent in agents:
                blob = " ".join(
                    [agent["id"], *agent["skills"], agent["description"]]
                ).lower()
                if any(t.lower() in blob for t in preset["match"]):
                    owner = agent["id"]
                    break
        if owner:
            cfg["ext_routes"].update({ext: owner for ext in preset["exts"]})
            cfg["routes"].append({"match": list(preset["match"]), "agent": owner})
            routed = True
    return cfg if routed else None


def _maybe_write_draft(ws: str | Path) -> Path | None:
    """Draft routes.draft.json once per process; routes.json is never written.

    The draft lives in the plugin-data dir so an installer that rebuilds
    the plugin directory cannot lose it mid-review. An existing draft is
    adopted, not regenerated — the operator may have edited it between
    process starts. The draft lands in warn mode and needs an explicit
    rename to take effect: the operator confirms the mapping, the plugin
    does not activate itself.
    """
    global _DRAFT_WRITTEN
    path = _data_dir() / "routes.draft.json"
    if _DRAFT_WRITTEN:
        return path if path.exists() else None
    _DRAFT_WRITTEN = True
    if not path.exists():
        # Adopt before generating: a draft from a previous process (or a
        # pre-migration one left in the plugin directory) carries operator
        # edits the draft table must not overwrite.
        legacy = PLUGIN_DIR / "routes.draft.json"
        if legacy.is_file() and legacy != path:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(legacy, path)
                logger.info(
                    "dispatch-guard: adopted routes.draft.json from the plugin "
                    "directory into %s — that copy survives reinstalls",
                    path.parent,
                )
            except OSError:
                logger.exception(
                    "dispatch-guard: could not migrate routes.draft.json into %s",
                    path.parent,
                )
    if path.exists():
        return path
    cfg = _draft_route_table(ws)
    if cfg is None:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        logger.exception("dispatch-guard: could not write routes.draft.json")
        return None
    logger.info(
        "dispatch-guard: drafted routes.draft.json (in %s) from %d discovered "
        "agent(s); review it and rename to routes.json to activate",
        path.parent,
        len(_discover_agents(ws)),
    )
    return path


class DispatchGuardMiddleware(MiddlewareBase):
    def __init__(self, workspace_dir: str, mode: str) -> None:
        self._ws = Path(workspace_dir).resolve()
        self._mode = mode
        self._draft_path: Path | None = None

    # -- helpers ---------------------------------------------------------

    def _log(self, record: dict[str, Any]) -> None:
        try:
            log_dir = self._ws / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            # Single-generation rotation: the audit trail is evidence, not a
            # forever-growing file. Keep the most recent window plus one
            # backup; anything older than that has already served its turn.
            log_path = log_dir / "dispatch_guard.jsonl"
            if log_path.exists() and log_path.stat().st_size > AUDIT_MAX_BYTES:
                log_path.replace(log_dir / "dispatch_guard.jsonl.1")
            record = {"ts": time.time(), "plugin": "dispatch-guard", **record}
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 - logging must never wedge the agent
            logger.exception("dispatch-guard: log write failed")

    @staticmethod
    def _target_of(tool_input: Any) -> tuple[str, bool]:
        """(target, is_path): the path argument if the tool names one.

        is_path=False means the input has no file argument — for code-runner
        style tools the deliverable path, if any, lives inside the code/text
        body, which _scan_candidates() inspects instead.
        """
        if isinstance(tool_input, str):
            stripped = tool_input.strip()
            if stripped.startswith("{"):
                try:
                    tool_input = json.loads(stripped)
                except (ValueError, TypeError):
                    pass
        if isinstance(tool_input, str):
            return tool_input, True
        if isinstance(tool_input, dict):
            for key in ("file_path", "path", "filename", "file"):
                if key in tool_input:
                    return str(tool_input[key]), True
            return json.dumps(tool_input, ensure_ascii=False), False
        return str(tool_input), False

    def _scan_candidates(self, blob: str, cfg: dict[str, Any]) -> list[tuple[str, bool]]:
        """(rel, explicit) deliverable-shaped paths named inside a blob.

        Two confidence tiers, ordered explicit-first: an explicit output
        target (redirect/tee/converter -o or --outdir/cp destination/ffmpeg's
        trailing positional output) is where bytes land and may be denied
        under the right mode; a quoted path is only a mention — enough to
        warn about, never enough to block.
        """
        found: list[tuple[str, bool]] = []
        seen: set[str] = set()
        for pattern in SHELL_OUTPUT_PATTERNS:
            for match in pattern.finditer(blob):
                rel = self._rel(match.group(1).strip("\"'"))
                if rel in seen or self._whitelisted(rel) or not self._is_deliverable(rel, cfg):
                    continue
                seen.add(rel)
                found.append((rel, True))
        for match in MENTION_PATH_RE.finditer(blob):
            raw = match.group(1)
            if any(ch in raw for ch in "*?["):
                # A glob pattern ("find . -name '*.docx'") names a class of
                # files, not a deliverable target.
                continue
            rel = self._rel(raw)
            if rel in seen or self._whitelisted(rel) or not self._is_deliverable(rel, cfg):
                continue
            seen.add(rel)
            found.append((rel, False))
        return found

    def _rel(self, target: str) -> str:
        try:
            p = Path(target)
            if not p.is_absolute():
                p = self._ws / p
            try:
                resolved = p.resolve()
            except (OSError, ValueError, RuntimeError):
                # Symlink loop, permission wall, NUL byte: syntactic
                # normalization still collapses "..", which is what the
                # prefix matchers must not be fooled by.
                resolved = Path(os.path.normpath(str(p)))
            try:
                return str(resolved.relative_to(self._ws))
            except ValueError:
                # Resolves outside the workspace: hand back the real absolute
                # location, never the raw spelling — "notes/../../outputs/x.docx"
                # must not inherit the whitelist prefix it was dressed in.
                return str(resolved)
        except Exception:  # noqa: BLE001
            return target

    @staticmethod
    def _whitelisted(rel: str) -> bool:
        if rel in WHITELIST_FILES:
            return True
        if any(rel.startswith(d) for d in WHITELIST_DIRS):
            return True
        # 插件本体目录 = default 的架构维护区，非交付物。
        # 只对原本就是绝对路径的目标生效：rel 相对路径是工作区相对的，
        # 按 cwd resolve 会误判（仓库恰好在 PLUGIN_DIR.parent 下时全白名单）。
        p = Path(rel)
        if p.is_absolute():
            try:
                p.resolve().relative_to(PLUGIN_DIR.parent.resolve())
                return True
            except (ValueError, OSError):
                return False
        return False

    def _is_deliverable(self, rel: str, cfg: dict[str, Any]) -> bool:
        """Deliverable-shaped: inside a deliverable directory or extension.

        The single gate for every interception (deny, warn, and the
        unconfigured setup block). Anything not deliverable-shaped is a basic
        operation and must reach the next handler untouched.

        Directory matching is prefix-based for workspace-relative paths
        (``outputs/x`` is deliverable, ``src/foo/outputs/x`` is a source
        tree), but segment-based for absolute paths: ``_rel`` leaves absolute
        targets untouched, so an absolute path into a deliverable directory
        (``/somewhere/outputs/report``) must match on the segment or the
        guard simply never sees it.
        """
        p = rel.replace("\\", "/").lower()
        dirs = _deliverable_dirs(cfg)
        if p.startswith(dirs):
            return True
        if os.path.splitext(p)[1] in _deliverable_exts(cfg):
            return True
        # The deliverable directory itself named as an output target
        # (`soffice --convert-to docx --outdir outputs …`) is where bytes land.
        if p in {d.rstrip("/") for d in dirs}:
            return True
        if os.path.isabs(p):
            segments = set(p.split("/"))
            if any(d.rstrip("/") in segments for d in dirs):
                return True
        return False

    def _live_owner(self, agent_id: str | None) -> tuple[str | None, bool]:
        """(owner_id, blockable): a block needs a real dispatch target.

        Discovery is authoritative only when it found at least one specialist.
        An empty discovery means "agent layout we cannot read", and the
        operator's table is then trusted verbatim rather than used to disarm
        the guard; a non-empty discovery vetoes routes naming absent agents
        and unmatched deliverable classes alike — no owner, no block.
        """
        if _AGENTS_CACHE["agents"] is None or _AGENTS_CACHE["ws"] != str(self._ws):
            _discover_agents(self._ws)
        if not _AGENTS_CACHE["agents"]:
            return agent_id, True
        if agent_id and agent_id in _AGENTS_CACHE["ids"]:
            return agent_id, True
        return None, False

    def _setup_hint(self, target: str) -> str:
        if self._draft_path is not None and self._draft_path.exists():
            return (
                "[dispatch-guard 未配置] 没有 routes.json。已按当前环境的专业 "
                f"agent 生成草稿 {self._draft_path}（mode=warn，只观察不拦截）。"
                "请和用户逐条确认草稿里的归属（ext_routes / 关键词 -> agent），"
                "确认后把它改名为 routes.json 生效（留在原目录即可，该目录不受"
                "插件重装影响）；观察一轮 warn 日志无误再切 enforce。"
                f"目标：{target}"
            )
        return SETUP_HINT.format(target=target)

    async def _no_owner_pass(
        self,
        reason: str,
        tool_name: str,
        target: str,
        mode: str,
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """Pass a deliverable-shaped write whose owner does not exist here.

        Blocking would strand the deliverable with nowhere to dispatch to —
        the exact failure that trains operators to turn the guard off. The
        pass is silent to the agent (a warning naming no one is noise) and
        audible in the audit trail.
        """
        global _NO_OWNER_LOGGED
        self._log(
            {
                "action": "no_owner",
                "tool": tool_name,
                "target": target,
                "mode": mode,
                "message": reason,
            }
        )
        if not _NO_OWNER_LOGGED:
            _NO_OWNER_LOGGED = True
            logger.warning(
                "dispatch-guard: %s — no specialist to dispatch to in this "
                "environment; the write passes (audit action=no_owner)",
                reason,
            )
        async for event in next_handler():
            yield event

    async def _guard_target(
        self,
        rel: str,
        *,
        tool_name: str,
        mode: str,
        cfg: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
        allow_deny: bool,
        source: str,
    ) -> AsyncGenerator[Any, None]:
        """The single decision path for a deliverable-shaped target.

        Used by both branches (write tools with a path argument, and
        explicit/mentioned targets scanned out of command lines or code
        bodies). `allow_deny` carries the confidence/mode policy: only
        explicit write targets may ever be denied, and shell targets need
        the opt-in `shell_enforce`; mentions never deny.
        """
        if mode == UNCONFIGURED:
            async for event in self._warn_and_pass(
                self._setup_hint(rel),
                tool_name,
                rel,
                mode,
                next_handler,
                action="needs_config",
            ):
                yield event
            return
        agent_id, why = self._route_hit(rel)
        owner, blockable = self._live_owner(agent_id)
        if not blockable:
            async for event in self._no_owner_pass(
                f"no live owner for {rel}", tool_name, rel, mode, next_handler
            ):
                yield event
            return
        hint = owner or "对应专业 agent"
        if allow_deny and mode == "enforce":
            yield self._deny(
                f"该产出属于 {hint} 域（命中 {why or '交付物路径'}），"
                f"请用 submit_to_agent 派发给 {hint}；"
                f"如确属编排/元操作，请与用户确认后加入白名单。目标：{rel}",
                tool_name,
                rel,
                mode,
            )
            return
        async for event in self._warn_and_pass(
            f"[dispatch-guard warn] {source} {rel} 命中 {hint}"
            f"（{why or '交付物路径'}）；enforce 模式下交付物写入将被拒绝，"
            f"请改用 submit_to_agent 派发。",
            tool_name,
            rel,
            mode,
            next_handler,
        ):
            yield event

    def _route_hit(self, blob: str) -> tuple[str | None, str | None]:
        cfg = _load_config()
        ext = os.path.splitext(blob)[1].lower()
        agent = cfg.get("ext_routes", {}).get(ext)
        if agent:
            return agent, f"ext {ext}"
        low = blob.lower()
        for rule in cfg.get("routes", []):
            for kw in rule.get("match", []):
                k = kw.lower()
                if k.isascii():
                    if re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", low):
                        return rule.get("agent"), f"kw {kw}"
                elif k in low:
                    return rule.get("agent"), f"kw {kw}"
        return None, None

    def _deny(self, text: str, tool_name: str, target: str, mode: str) -> ToolResponse:
        self._log(
            {
                "action": "denied",
                "tool": tool_name,
                "target": target,
                "mode": mode,
                "message": text,
            }
        )
        return ToolResponse(
            content=[TextBlock(type="text", text=text)],
            state=ToolResultState.DENIED,
        )

    async def _warn_and_pass(
        self,
        warn: str,
        tool_name: str,
        target: str,
        mode: str,
        next_handler: Callable[..., AsyncGenerator[Any, None]],
        action: str = "warned",
    ) -> AsyncGenerator[Any, None]:
        self._log(
            {
                "action": action,
                "tool": tool_name,
                "target": target,
                "mode": mode,
                "message": warn,
            }
        )
        async for event in next_handler():
            if isinstance(event, ToolResponse):
                event.content.append(TextBlock(type="text", text=warn))
            yield event

    # -- hook ------------------------------------------------------------

    async def on_acting(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        cfg = _load_config()
        mode = cfg.get("mode", "enforce")
        if mode == "off":
            async for event in next_handler():
                yield event
            return

        tool_call = input_kwargs.get("tool_call")
        name = getattr(tool_call, "name", "") or ""
        raw_input = getattr(tool_call, "input", "") or ""
        target, target_is_path = self._target_of(raw_input)

        if name in SPAWN_TOOLS:
            yield self._deny(
                "spawn_subagent 已停用（配置层禁用）。盲评或并行 worker 请用 "
                "chat_with_agent / submit_to_agent 派给专业 agent。",
                name,
                target,
                mode,
            )
            return

        if name in _write_tools(cfg):
            # Whitelisted bookkeeping writes and every non-deliverable write
            # (notes, scripts, configs, scratch, extension-less files) are
            # basic operations: they pass untouched in every mode, without a
            # log entry. Only deliverable-shaped targets are the guard's
            # business. A tool without a path argument (a code runner, a
            # downloader) is still guarded: its deliverable paths, if any,
            # are scanned out of the input body — explicit write shapes may
            # deny, quoted mentions only warn.
            rel: str | None = None
            explicit = False
            source = "写入交付物"
            if target_is_path:
                candidate = self._rel(target)
                if not self._whitelisted(candidate) and self._is_deliverable(candidate, cfg):
                    rel, explicit = candidate, True
            else:
                found = self._scan_candidates(target, cfg)
                if found:
                    rel, explicit = found[0]
                    source = "参数/代码中出现交付物路径" if not explicit else "生成交付物"
            if rel is not None:
                async for event in self._guard_target(
                    rel,
                    tool_name=name,
                    mode=mode,
                    cfg=cfg,
                    next_handler=next_handler,
                    allow_deny=explicit,
                    source=source,
                ):
                    yield event
                return

        if name in SHELL_TOOLS:
            if isinstance(raw_input, dict):
                cmd = str(
                    raw_input.get("command")
                    or raw_input.get("cmd")
                    or json.dumps(raw_input, ensure_ascii=False)
                )
            else:
                cmd = str(raw_input)
            found = self._scan_candidates(cmd, cfg)
            if found:
                rel, explicit = found[0]
                async for event in self._guard_target(
                    rel,
                    tool_name=name,
                    mode=mode,
                    cfg=cfg,
                    next_handler=next_handler,
                    # deny needs BOTH the unambiguous shape and the opt-in;
                    # a mention in a command never blocks (grep for a file
                    # name is not a write).
                    allow_deny=explicit and _shell_enforce(cfg),
                    source="命令生成或覆盖交付物" if explicit else "命令中提到交付物路径",
                ):
                    yield event
                return

        async for event in next_handler():
            yield event


def _factory(ctx: Any, agent_config: Any) -> DispatchGuardMiddleware | None:
    agent_id = getattr(agent_config, "id", None) or getattr(ctx, "agent_id", None)
    if agent_id != "default":
        return None
    mode = _load_config().get("mode", "enforce")
    if mode == "off":
        return None
    workspace_dir = getattr(ctx, "workspace_dir", None)
    if not workspace_dir:
        return None
    mw = DispatchGuardMiddleware(workspace_dir, mode)
    if mode == UNCONFIGURED:
        # Fresh install: look around before asking anyone to configure — a
        # draft table from the real agent inventory is the reviewable version
        # of "build a dispatch table with the user".
        mw._draft_path = _maybe_write_draft(workspace_dir)
    return mw


class DispatchGuardPlugin:
    def register(self, api: PluginApi) -> None:
        api.register_middleware(_factory, priority=40)


plugin = DispatchGuardPlugin()
