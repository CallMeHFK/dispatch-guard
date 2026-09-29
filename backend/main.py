"""dispatch-guard: enforce the default agent's output routing at the tool seam.

Three behaviours, gated by ``routes.json`` ``mode`` (enforce | warn | off):

* ``write_file``/``edit_file``/``append_file`` outside the whitelist ->
  DENIED in enforce mode with a "this belongs to {agent}, use submit_to_agent"
  message; in warn mode the call proceeds and a warning block is appended.
* ``execute_shell_command`` whose command line heuristically produces a
  deliverable (redirect/pandoc/ffmpeg into outputs/, projects/ or a
  deliverable extension) -> warn-only (append a block), never denied, to
  avoid false positives on status-check commands.
* ``spawn_subagent`` -> DENIED as a backstop for the config-layer disable.

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

SHELL_WARN_PATTERNS = (
    re.compile(
        r"(?:>>?|\btee\s+)[^;&|]*(?:outputs?/|projects?/|"
        r"\.(?:md|docx|pptx|xlsx|pdf|py|svg|png|mp4))",
        re.I,
    ),
    re.compile(r"\bpandoc\b[^;&|]*?-o\s+[^;&|]+", re.I),
    re.compile(r"\bffmpeg\b[^;&|]*?(?:-o|>)\s*[^;&|]+", re.I),
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


class DispatchGuardMiddleware(MiddlewareBase):
    def __init__(self, workspace_dir: str, mode: str) -> None:
        self._ws = Path(workspace_dir).resolve()
        self._mode = mode

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
        mode = _load_config().get("mode", "enforce")
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
            if not self._whitelisted(rel):
                if mode == UNCONFIGURED:
                    async for event in self._warn_and_pass(
                        SETUP_HINT.format(target=rel),
                        name,
                        rel,
                        mode,
                        next_handler,
                        action="needs_config",
                    ):
                        yield event
                    return
                agent_id, why = self._route_hit(rel)
                hint = agent_id or "对应专业 agent"
                if mode == "enforce":
                    yield self._deny(
                        f"该产出属于 {hint} 域（命中 {why or '白名单外路径'}），"
                        f"请用 submit_to_agent 派发给 {hint}；"
                        f"如确属编排/元操作，请与用户确认后加入白名单。目标：{rel}",
                        name,
                        rel,
                        mode,
                    )
                    return
                async for event in self._warn_and_pass(
                    f"[dispatch-guard warn] 写入 {rel} 命中 {hint}（{why}），"
                    f"enforce 模式下将被拒绝并提示派发。",
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
            hit = next((m.group(0) for p in SHELL_WARN_PATTERNS if (m := p.search(cmd))), None)
            if hit:
                if mode == UNCONFIGURED:
                    async for event in self._warn_and_pass(
                        SETUP_HINT.format(target=hit[:120]),
                        name,
                        hit[:120],
                        mode,
                        next_handler,
                        action="needs_config",
                    ):
                        yield event
                    return
                agent_id, why = self._route_hit(hit)
                hint = agent_id or "对应专业 agent"
                async for event in self._warn_and_pass(
                    f"[dispatch-guard warn] 命令疑似生成交付物（{hit[:80]}），"
                    f"命中 {hint}（{why}）；enforce 模式下写交付物将被拒绝，"
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
    return DispatchGuardMiddleware(workspace_dir, mode)


class DispatchGuardPlugin:
    def register(self, api: PluginApi) -> None:
        api.register_middleware(_factory, priority=40)


plugin = DispatchGuardPlugin()
