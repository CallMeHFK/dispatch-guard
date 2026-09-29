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

# Command-line shapes that name an output FILE (redirect, tee, or a converter's
# -o). Whether that file is a deliverable is decided by _is_deliverable(), not
# here — so a redirect into a notes file never trips the heuristic.
SHELL_OUTPUT_PATTERNS = (
    re.compile(r"(?:>>|\btee\b(?:\s+-\w+)*\s+|>)\s*([^\s;&|]+)", re.I),
    re.compile(
        r"\b(?:pandoc|ffmpeg|soffice|libreoffice)\b[^;&|]*?-o\s+([^\s;&|]+)", re.I
    ),
)

_CONFIG_CACHE: dict[str, Any] = {"mtime": 0.0, "data": None}
_UNCONFIGURED_LOGGED = False

UNCONFIGURED = "unconfigured"
SETUP_HINT = (
    "[dispatch-guard 未配置] 插件目录下没有 routes.json，派发表为空，"
    "当前只提示不拦截。请和用户一起确认各类产出归属哪个 agent，"
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


def _load_config() -> dict[str, Any]:
    """Read routes.json, cached by mtime so mode flips apply without reload."""
    path = PLUGIN_DIR / "routes.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _warn_unconfigured()
        return {"mode": UNCONFIGURED, "routes": [], "ext_routes": {}}
    if _CONFIG_CACHE["data"] is None or mtime != _CONFIG_CACHE["mtime"]:
        try:
            _CONFIG_CACHE["data"] = json.loads(path.read_text(encoding="utf-8"))
            _CONFIG_CACHE["mtime"] = mtime
        except Exception:  # noqa: BLE001 - fail open
            logger.exception("dispatch-guard: bad routes.json, failing open")
            return {"mode": "off", "routes": [], "ext_routes": {}}
    return _CONFIG_CACHE["data"]


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
# strands the write — there is nowhere to dispatch to. Manifests are read from
# the workspace (``agents/*/agent.json`` and a top-level ``agent.json``) and,
# when the plugin really lives inside a QwenPaw tree, the host-level agents
# directory beside ``plugins/``.

_AGENTS_CACHE: dict[str, Any] = {"ws": None, "agents": None, "ids": frozenset()}
_DRAFT_WRITTEN = False
_NO_OWNER_LOGGED = False

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


def _discovery_roots(ws: Path) -> list[Path]:
    roots = [ws / "agents", ws / ".qwenpaw" / "agents"]
    # Host-level agents dir only when the plugin genuinely lives in a QwenPaw
    # tree (~/.qwenpaw/plugins/<id>); a checkout anywhere else must not have
    # its parent directories guessed at.
    if PLUGIN_DIR.parent.name == "plugins":
        roots.append(PLUGIN_DIR.parent.parent / "agents")
    return roots


def _discover_agents(ws: str | Path) -> list[dict[str, Any]]:
    """Agents installed in this environment, cached per workspace."""
    ws_path = Path(ws)
    if _AGENTS_CACHE["ws"] == str(ws_path) and _AGENTS_CACHE["agents"] is not None:
        return _AGENTS_CACHE["agents"]
    found: dict[str, dict[str, Any]] = {}
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
    agents = list(found.values())
    _AGENTS_CACHE.update(
        {"ws": str(ws_path), "agents": agents, "ids": frozenset(found)}
    )
    return agents


def _draft_route_table(ws: str | Path) -> dict[str, Any] | None:
    """Map discovered agents onto deliverable categories, or None if vacuous."""
    agents = _discover_agents(ws)
    if not agents:
        return None
    cfg: dict[str, Any] = {"mode": "warn", "routes": [], "ext_routes": {}}
    for preset in _DRAFT_PRESETS:
        owner = None
        for agent in agents:
            blob = " ".join([agent["id"], *agent["skills"], agent["description"]]).lower()
            if any(term.lower() in blob for term in preset["match"]):
                owner = agent["id"]
                break
        if owner:
            cfg["ext_routes"].update({ext: owner for ext in preset["exts"]})
            cfg["routes"].append({"match": list(preset["match"]), "agent": owner})
    return cfg if cfg["ext_routes"] else None


def _maybe_write_draft(ws: str | Path) -> Path | None:
    """Draft routes.draft.json once per process; routes.json is never written.

    The draft lands in warn mode and needs an explicit rename to take effect:
    the operator confirms the mapping, the plugin does not activate itself.
    """
    global _DRAFT_WRITTEN
    path = PLUGIN_DIR / "routes.draft.json"
    if _DRAFT_WRITTEN:
        return path if path.exists() else None
    _DRAFT_WRITTEN = True
    cfg = _draft_route_table(ws)
    if cfg is None:
        return None
    try:
        path.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        logger.exception("dispatch-guard: could not write routes.draft.json")
        return None
    logger.info(
        "dispatch-guard: drafted routes.draft.json from %d discovered agent(s); "
        "review it and rename to routes.json to activate",
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
            record = {"ts": time.time(), "plugin": "dispatch-guard", **record}
            with (log_dir / "dispatch_guard.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 - logging must never wedge the agent
            logger.exception("dispatch-guard: log write failed")

    @staticmethod
    def _target_of(tool_input: Any) -> str:
        if isinstance(tool_input, str):
            stripped = tool_input.strip()
            if stripped.startswith("{"):
                try:
                    tool_input = json.loads(stripped)
                except (ValueError, TypeError):
                    pass
        if isinstance(tool_input, dict):
            for key in ("file_path", "path", "filename", "file"):
                if key in tool_input:
                    return str(tool_input[key])
            return json.dumps(tool_input, ensure_ascii=False)
        return str(tool_input)

    def _rel(self, target: str) -> str:
        try:
            p = Path(target)
            if not p.is_absolute():
                p = self._ws / p
            return str(p.resolve().relative_to(self._ws))
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
        """
        p = rel.replace("\\", "/").lower()
        if p.startswith(_deliverable_dirs(cfg)):
            return True
        return os.path.splitext(p)[1] in _deliverable_exts(cfg)

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
                "[dispatch-guard 未配置] 插件目录下没有 routes.json。已按当前环境的专业 "
                f"agent 生成草稿 {self._draft_path.name}（mode=warn，只观察不拦截）。"
                "请和用户逐条确认草稿里的归属（ext_routes / 关键词 -> agent），"
                "确认后把它改名为 routes.json 生效；观察一轮 warn 日志无误再切 enforce。"
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
        target = self._target_of(raw_input)

        if name in SPAWN_TOOLS:
            yield self._deny(
                "spawn_subagent 已停用（配置层禁用）。盲评或并行 worker 请用 "
                "chat_with_agent / submit_to_agent 派给专业 agent。",
                name,
                target,
                mode,
            )
            return

        if name in WRITE_TOOLS:
            rel = self._rel(target)
            # Whitelisted bookkeeping writes and every non-deliverable write
            # (notes, scripts, configs, scratch, extension-less files) are
            # basic operations: they pass untouched in every mode, without a
            # log entry. Only deliverable-shaped targets are the guard's
            # business.
            if not self._whitelisted(rel) and self._is_deliverable(rel, cfg):
                if mode == UNCONFIGURED:
                    async for event in self._warn_and_pass(
                        self._setup_hint(rel),
                        name,
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
                        f"no live owner for {rel}", name, rel, mode, next_handler
                    ):
                        yield event
                    return
                hint = owner or "对应专业 agent"
                if mode == "enforce":
                    yield self._deny(
                        f"该产出属于 {hint} 域（命中 {why or '交付物路径'}），"
                        f"请用 submit_to_agent 派发给 {hint}；"
                        f"如确属编排/元操作，请与用户确认后加入白名单。目标：{rel}",
                        name,
                        rel,
                        mode,
                    )
                    return
                async for event in self._warn_and_pass(
                    f"[dispatch-guard warn] 写入交付物 {rel} 命中 {hint}"
                    f"（{why or '交付物路径'}），enforce 模式下将被拒绝并提示派发。",
                    name,
                    rel,
                    mode,
                    next_handler,
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
            # Heuristic: does the command name an output FILE at all? Only then
            # does deliverable-ness matter — `echo hi > todo.txt` is a basic
            # operation and must stay silent, `pandoc -o outputs/r.docx` is not.
            hit = None
            for pattern in SHELL_OUTPUT_PATTERNS:
                for match in pattern.finditer(cmd):
                    candidate = match.group(1).strip("\"'")
                    rel = self._rel(candidate)
                    if self._whitelisted(rel) or not self._is_deliverable(rel, cfg):
                        continue
                    hit = rel
                    break
                if hit:
                    break
            if hit:
                if mode == UNCONFIGURED:
                    async for event in self._warn_and_pass(
                        self._setup_hint(hit[:120]),
                        name,
                        hit[:120],
                        mode,
                        next_handler,
                        action="needs_config",
                    ):
                        yield event
                    return
                agent_id, why = self._route_hit(hit)
                owner, blockable = self._live_owner(agent_id)
                if not blockable:
                    async for event in self._no_owner_pass(
                        f"no live owner for {hit[:120]}", name, hit[:120], mode, next_handler
                    ):
                        yield event
                    return
                hint = owner or "对应专业 agent"
                async for event in self._warn_and_pass(
                    f"[dispatch-guard warn] 命令疑似生成或覆盖交付物（{hit[:80]}），"
                    f"命中 {hint}（{why or '交付物路径'}）；enforce 模式下写交付物将被拒绝，"
                    f"请改用 submit_to_agent 派发。",
                    name,
                    hit[:120],
                    mode,
                    next_handler,
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
