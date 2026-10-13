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

import hashlib
import json
import logging
import os
import re
import shutil
import time
from collections.abc import AsyncGenerator, Callable, Iterator
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

# The guard's own dispatch tables (both live copies). A write to either of
# these reconfigures — or silently disables — the guard itself, so the
# guarded agent must never touch them: excluded from every whitelist and
# denied outright by the write-tools branch, in every mode the middleware
# observes (D1 — meta-integrity, not deliverable routing).
_GUARD_CONFIG_NAMES = frozenset({"routes.json", "routes.draft.json"})

# D1 (shell arm): the write-tool branch denies explicit guard-table targets,
# but the same routes.json is one `sed -i` / `tee` / `> redirect` away from
# being rewritten through execute_shell_command — the one bypass that would
# undo every other rule. ANY shell command whose text mentions the guard's
# own table paths (plugin tree or plugin-data mirror — both end in these
# basenames) is intercepted at mention level in every mode the middleware
# observes: denied under enforce (meta-integrity is not a routing question),
# warned elsewhere. The regex is boundary-anchored so "myroutes.json" and
# the innocent "routes.example.json" do not match.
_GUARD_CONFIG_MENTION_RE = re.compile(
    r"(?:\b|^)(?:routes\.draft\.json|routes\.json)(?![\w.-])"
)

# Command-line shapes that name an output FILE (redirect, tee, an output
# flag, or the destination of cp/mv/rsync/install). Whether that file is a
# deliverable is decided by _is_deliverable(), not here — so a redirect into
# a notes file never trips the heuristic. The cp-family pattern is greedy so
# the captured token is the segment's last argument = the destination.
SHELL_OUTPUT_PATTERNS = (
    # Redirects and tee. ``>|`` (the noclobber override) needs its own
    # alternative — the capture class excludes ``|``, so the bare ``>``
    # branch never sees it (D6). tee accepts long options (``--append``),
    # which ``-\w+`` cannot spell (D6).
    re.compile(r"(?:>\||>>|\btee\b(?:\s+-{1,2}[\w-]+)*\s+|>)\s*([^\s;&|]+)", re.I),
    # An explicit output flag on ANY producer (D7): -o FILE / -O FILE,
    # --outdir / --output(-dir) / --output-document, value joined by = or
    # whitespace. Anchored to the flag shape instead of a fixed tool list —
    # the false-positive cost is absorbed downstream by _is_deliverable
    # (the captured token still has to look like a deliverable) and by the
    # warn tier.
    re.compile(
        r"(?:^|[\s;&|])(?:--outdir|--output(?:-dir)?|--output-document"
        r"|-[oO])(?:=|\s+)([^\s;&|]+)",
        re.I,
    ),
    # Compilers also accept the joined form: gcc -ooutputs/prog.exe (D7).
    re.compile(
        r"\b(?:gcc|g\+\+|clang|clang\+\+|cc|c\+\+)\b[^;&|]*?[\s;]"
        r"-o(?=[^\s;&|])([^\s;&|]+)",
        re.I,
    ),
    # dd's output operand (D7).
    re.compile(r"\bdd\b(?:\s+[^;&|\s]+)*?\s+of=([^\s;&|]+)", re.I),
    # Archivers (D7): zip's first positional argument is the archive being
    # created; tar's is the token after a flag bundle containing c(reate)
    # and f(ile) — an extraction (xf) names an input, not an output.
    re.compile(r"\bzip\b(?:\s+-{1,2}[\w-]+)*\s+([^\s;&|]+)", re.I),
    re.compile(
        r"\btar\b(?:\s+[^;&|\s]+)*?\s+(?:-{1,2})?[a-zA-Z]*c[a-zA-Z]*f[a-zA-Z]*"
        r"(?:\s+|=)([^\s;&|]+)",
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
    # In-place editors (D7): sed -i / perl -i rewrite their trailing file
    # argument — the same edit through edit_file would be denied.
    re.compile(
        r"\b(?:sed|perl)\b(?=[^;&|]*(?:\s--in-place|\s-[\w-]*i\b))"
        r"[^;&|]*?\s([^\s;&|]*\.[A-Za-z0-9]{1,12})(?=\s*(?:[;&|]|$))",
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
# D14: mode=off keeps the middleware attached (so off→enforce hot-applies
# without a session restart) and announces itself in the audit trail once
# per off period — "what happened while it was off" stays visible.
_OFF_LOGGED = False

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
    if routes is None and "routes" not in cfg:
        # D15: an absent key is a legal minimal config (ext_routes-only
        # tables), not a type error — no warning. Only the explicit wrong
        # type (including an explicit null) says so.
        routes = []
    elif not isinstance(routes, list):
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
        if match is None:
            # An explicit JSON null is a typo, not an empty list: degrade to
            # no keywords instead of letting _route_hit crash on it (D4).
            logger.warning(
                "dispatch-guard: route for %r has a null 'match'; treated as "
                "empty",
                rule.get("agent"),
            )
            rule = {**rule, "match": []}
        elif not isinstance(match, list):
            logger.warning(
                "dispatch-guard: route for %r has a non-array 'match'; its "
                "keywords are dropped",
                rule.get("agent"),
            )
            rule = {**rule, "match": []}
        else:
            kws: list[str] = []
            for kw in match:
                if kw is None:
                    continue
                text = kw if isinstance(kw, str) else str(kw)
                if text:
                    kws.append(text)
            if len(kws) != len(match):
                logger.warning(
                    "dispatch-guard: route for %r had non-string 'match' "
                    "keywords; coerced to strings, unusable ones dropped",
                    rule.get("agent"),
                )
            rule = {**rule, "match": kws}
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
        # Defense in depth (D1): only a table that parses as a JSON object and
        # survives normalization + lint may propagate into the reinstall-proof
        # home. _load_config already guarantees that ordering at its call site;
        # this gate keeps any future caller honest too.
        try:
            parsed = json.loads(plugin_copy.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            parsed = None
        if not isinstance(parsed, dict):
            logger.warning(
                "dispatch-guard: plugin-dir routes.json is not a parseable "
                "JSON object; refusing to mirror it into %s",
                data_copy,
            )
            return
        _lint_policy(_normalize_config(parsed))
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
            last_good = _CONFIG_CACHE["data"]
            if isinstance(last_good, dict):
                # D13: a half-written edit must not silently disarm the guard.
                # The previous known-good table keeps every promise fail-open
                # makes (never wedge the agent) without dropping authority;
                # only a cold cache with no good table may fall to off. The
                # mtime stays mismatched, so a fixed file hot-loads on the
                # next call; each failed reload says so here.
                logger.warning(
                    "dispatch-guard: %s failed to load; continuing on the last "
                    "known-good table — fix the file, this warning means the "
                    "guard is enforcing yesterday's rules",
                    path,
                )
                return last_good
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
        for d in custom:
            if not isinstance(d, str):
                # D16: a non-string member is a config mistake, not a
                # directory. str()-coercing it invented "none/" / "5/"
                # entries the operator never wrote — drop it with a warning
                # instead, per the "wrong type degrades to absent field"
                # promise.
                logger.warning(
                    "dispatch-guard: ignoring non-string deliverable_dirs entry %r", d
                )
                continue
            s = d.strip().lower().rstrip("/")
            if s:
                dirs.append(s + "/")
    return tuple(dirs)


def _canonical_target(p: str) -> str:
    """The target string judged by the guard (D9).

    Trailing whitespace and trailing dots are spelling accidents, not
    different files: ``report.docx.`` is the same write as ``report.docx``
    on every filesystem that matters here, and an ext-only routes table
    must not stand down because of one stray dot. Display and audit keep
    the original string; only the judgment form is canonicalized.

    Removed to a fixed point, not one pass each way. Dots and whitespace
    alternate at the end of a spelling, and a single ``strip().rstrip(".")``
    only ever peels one layer: ``"report.docx ."`` ends in a dot, so strip
    finds nothing to do and rstrip leaves ``"report.docx "`` — an extension
    with a trailing space, which matched no route and produced no audit
    record at all. Loop until the string stops changing.
    """
    text = p
    while True:
        peeled = text.strip().rstrip(".")
        if peeled == text:
            return peeled
        text = peeled


# D10: the spawn backstop's denial record used to carry the tool input
# verbatim — task text routinely embeds API keys and bearer tokens, and the
# audit log's readership is wider than the credential holders'. The record
# now carries only a hash (identity/dedup), the length, and a redacted
# excerpt; raw input never reaches the jsonl.
#
# The patterns are a known-shape filter, not a secret scanner: they catch the
# spellings that actually turn up in task text (provider keys, AWS access key
# ids, bearer headers, ``key=value`` and ``key is value`` assignments) and
# nothing else. A credential in some other shape still reaches the excerpt —
# which is why the excerpt is capped and the hash, not the text, is the
# record's identity. Redaction lowers the exposure surface; it does not close
# it, and the docs say so rather than promising the impossible.
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{4,}"),
    # AWS access key id / temporary credential id.
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # Authorization header value: "Bearer <token>" is space-separated, so the
    # key=value pattern below never sees it.
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|token|secret|passw(?:ord|ork|rase)|"
        r"bearer|authorization|credential)\b\s*(?:[:=]|\bis\b|\bare\b)\s*[^\s,;\"']+"
    ),
)


def _redacted_audit_target(raw: str) -> str:
    """Digest form of a denial's target for the audit trail (D10)."""
    text = raw if isinstance(raw, str) else str(raw)
    redacted = text
    for pat in _SECRET_PATTERNS:
        redacted = pat.sub("[redacted]", redacted)
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
    excerpt = redacted[:80]
    return f"sha256:{digest} len:{len(text)} excerpt:{excerpt!r}"


def _strings_in(value: Any, depth: int = 0) -> Iterator[str]:
    """Every string reachable in a tool input, at any nesting depth.

    A path argument the guard cannot recognise as one is still a path: the
    D3 shapes (``file_path`` as a list or a dict) used to clear the checks
    gated on ``target_is_path`` and walk straight past them. Walking the
    whole input means nesting buys nothing.

    Depth-capped because the input is untrusted and this runs before the
    fail-open wrapper in ``on_acting``: a deeply nested body must cost a
    bounded amount of work and must not raise RecursionError out of the
    middleware. Past the cap the remainder is simply not inspected.
    """
    if isinstance(value, str):
        yield value
    elif depth >= 12:
        return
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings_in(key, depth + 1)
            yield from _strings_in(item, depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _strings_in(item, depth + 1)


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
    # OSError is deliberately not caught here (B4). A manifest that exists
    # but cannot be read means the walk did not succeed, and swallowing it
    # turned a permission wall into an empty inventory that
    # _discover_agents reported as readable — the guard then stood down
    # over a layout it had never read, which is the fail-open D8 exists to
    # prevent. Letting it reach _discover_agents' handler reports the whole
    # inventory as unreadable instead, and keeps it out of the cache so the
    # next call retries. Only a file that WAS read and does not describe an
    # agent returns None.
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    agent_id = data.get("id") or data.get("agent_id") or data.get("name")
    if not agent_id:
        return None
    skills = data.get("skills") or data.get("capabilities") or []
    if isinstance(skills, str):
        skills = [s.strip() for s in re.split(r"[,;/]", skills) if s.strip()]
    elif not isinstance(skills, list):
        # A malformed manifest must not crash discovery for every later
        # interception (D5): degrade this agent's skills to empty and say so.
        logger.warning(
            "dispatch-guard: %s has non-list skills (%s); treated as empty",
            path.name,
            type(skills).__name__,
        )
        skills = []
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


def _discover_agents(ws: str | Path) -> tuple[list[dict[str, Any]], bool]:
    """(agents, readable) — agents installed in this environment, cached per
    workspace until the inventory inputs change (see _inventory_stamp).

    ``readable`` says whether the walk succeeded: the glob ran AND every
    manifest it matched was actually read. An empty inventory with
    ``readable=True`` is therefore a genuine single-agent deployment, not a
    layout we failed to parse — the distinction decides ``_live_owner``'s
    no_owner-vs-trust-table verdict (D8). A manifest that exists but cannot
    be read makes the whole inventory unreadable (B4); dropping it silently
    used to report a permission wall as "no specialists here".

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
        # Only successful walks are cached, so a cache hit is readable by
        # construction — even when the cached inventory is legitimately empty.
        return _AGENTS_CACHE["agents"], True
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
    # readable=False means the WALK failed (the inventory tells us nothing);
    # readable=True with an empty list means the environment genuinely has
    # no specialists. _live_owner treats these oppositely (D8).
    return agents, not discovery_failed


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
    agents, _readable = _discover_agents(ws)
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
        len(_discover_agents(ws)[0]),
    )
    return path


def _tool_call_parts(tool_call: Any) -> tuple[str, Any]:
    """(name, input) from either an object or a Mapping-shaped tool_call.

    QwenPaw passes an object with .name/.input, but a dict-shaped call must
    not fall through getattr()'s defaults and silently disarm every rule
    (D2 — a dict tool_call used to read as name="" and slip the spawn
    backstop).
    """
    if isinstance(tool_call, dict):
        name = tool_call.get("name") or ""
        return str(name), tool_call.get("input", "") or ""
    name = getattr(tool_call, "name", "") or ""
    return str(name), getattr(tool_call, "input", "") or ""


def _has_typed_path_value(raw_input: Any) -> bool:
    """True when a path-argument key holds a non-string value (D3)."""
    if not isinstance(raw_input, dict):
        return False
    return any(
        key in raw_input and not isinstance(raw_input[key], str)
        for key in ("file_path", "path", "filename", "file")
    )


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
                    value = tool_input[key]
                    if isinstance(value, str):
                        return value, True
                    # D3: never str() a non-string path value into a fake
                    # path ("['outputs/a.docx']" is not a path — treating it
                    # as one silently disarmed every rule). Hand the whole
                    # input to _scan_candidates as a blob instead; the write
                    # branch escalates deliverable shapes inside it to
                    # explicit targets.
                    return json.dumps(tool_input, ensure_ascii=False), False
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
        # Single yardstick with _is_deliverable (D12): that side lowercases
        # the path before judging, so whitelist comparisons must too —
        # otherwise "notes/x" passed while "Notes/x.docx" was denied. On a
        # case-sensitive FS the two spellings are different directories and
        # both get the whitelist's benefit; on a case-insensitive host the
        # same file now gets one answer. The plugin-tree containment check
        # below stays case-sensitive: it resolves real paths, not prefixes.
        # Canonicalised for the same reason (B5): _is_deliverable peels
        # trailing dots and spaces before judging, so a spelling accident
        # must not make the two halves of one decision disagree about which
        # string they are looking at.
        key = _canonical_target(rel).replace("\\", "/").lower()
        if Path(key).name in _GUARD_CONFIG_NAMES:
            # 守卫自身路由表永不白名单（D1）：default 不得改写自己的枷锁。
            # Tested first, not last: below the two early returns it was
            # unreachable for any table parked under a whitelisted prefix,
            # so "tmp/routes.json" was whitelisted on the prefix match.
            return False
        if key in {f.lower() for f in WHITELIST_FILES}:
            return True
        if any(key.startswith(d) for d in WHITELIST_DIRS):
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

    def _is_guard_config(self, target: str) -> bool:
        """True when the target is one of the guard's own dispatch tables.

        Both live locations count: the plugin directory (explicit override)
        and the plugin-data directory (the reinstall-proof copy). Symlinks
        are resolved so a crafted path cannot dress the table as something
        else (D1).

        The basename test is canonicalised and case-insensitive (B3). A
        trailing dot or space is the same spelling accident D9 handles, and
        on a case-insensitive host ``ROUTES.JSON`` is the guard's table, not
        a neighbour of it. The cost of that choice falls on a case-sensitive
        filesystem, where a file genuinely named ``ROUTES.JSON`` inside the
        guard's own two directories would be refused — a file nobody has a
        reason to write there, against a bypass that works on every macOS
        and Windows host.
        """
        text = _canonical_target(str(target))
        if Path(text).name.lower() not in _GUARD_CONFIG_NAMES:
            return False
        p = Path(self._rel(text))
        if not p.is_absolute():
            p = self._ws / p
        try:
            rp = p.resolve()
        except (OSError, ValueError, RuntimeError):
            rp = Path(os.path.normpath(str(p)))
        for root in (PLUGIN_DIR, _data_dir()):
            try:
                rp.relative_to(root.resolve())
                return True
            except (ValueError, OSError):
                continue
        return False

    def _guard_config_write(self, name: str, raw_input: Any, cfg: dict[str, Any]) -> str | None:
        """The guard table this call would write, or None (D1).

        Walks every string in the input rather than only the recognised path
        argument: gating on ``target_is_path`` meant a list- or dict-valued
        ``file_path`` — the D3 shape — skipped the meta-integrity check
        entirely and the write went through.
        """
        if name not in _write_tools(cfg):
            return None
        for candidate in _strings_in(raw_input):
            if self._is_guard_config(candidate):
                return candidate
        return None

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
        p = _canonical_target(rel).replace("\\", "/").lower()
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

        An empty inventory is read for what it is (D8): when the walk
        SUCCEEDED and found nothing, this is a single-agent deployment and
        the documented promise holds — every class is no_owner, the write
        passes with an audit record. Only when the walk FAILED (we cannot
        read the layout at all) do we keep the deliberate asymmetry and
        trust the operator's table verbatim — a broken inventory must not
        disarm the guard. In that branch no owner can be verified, so the
        caller gets (agent_id, True) with agent_id possibly None and must
        word its block accordingly.
        """
        if _AGENTS_CACHE["agents"] is None or _AGENTS_CACHE["ws"] != str(self._ws):
            _agents, readable = _discover_agents(self._ws)
        else:
            # Only successful walks are cached; a populated cache is readable.
            readable = True
        if not _AGENTS_CACHE["agents"]:
            if readable and _AGENTS_CACHE["agents"] is not None:
                # Genuinely zero specialists: no owner, no block.
                return None, False
            # The walk failed: distrust the emptiness, trust the table.
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
        if owner is None:
            # Trust-the-table block with no nameable owner (the agent
            # inventory could not be read — a readable-empty one already
            # stood down above, D8). The text must not point the agent at
            # an invented dispatch target; the only valid next step is the
            # user.
            if allow_deny and mode == "enforce":
                yield self._deny(
                    f"该产出命中守卫规则（{why or '交付物路径'}），但当前环境的 agent "
                    f"清单读取失败，无法确认派发目标；请勿自行产出，把该目标与"
                    f"拦截原因回报给用户确认归属。目标：{rel}",
                    tool_name,
                    rel,
                    mode,
                )
                return
            async for event in self._warn_and_pass(
                f"[dispatch-guard warn] {source} {rel} 命中 {why or '交付物路径'}，"
                f"但 agent 清单读取失败、无法确认派发目标；enforce 模式下将被拒绝，"
                f"请与用户确认归属。",
                tool_name,
                rel,
                mode,
                next_handler,
            ):
                yield event
            return
        hint = owner
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
        blob = _canonical_target(blob)  # D9: "a.docx." routes as "a.docx"
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
        tool_call = input_kwargs.get("tool_call")
        name, raw_input = _tool_call_parts(tool_call)

        # D1: the guard's own dispatch table is meta-integrity, not a routing
        # rule, so it is refused in every mode the middleware observes — off
        # included. This check has to stay above the off short-circuit: below
        # it, a disarmed guard permitted exactly the one write that keeps it
        # disarmed, and the "untouchable in any mode" promise was false in the
        # one mode that mattered.
        guard_table = self._guard_config_write(name, raw_input, cfg)
        if guard_table is not None:
            yield self._deny(
                f"dispatch-guard 元完整性：{Path(_canonical_target(guard_table)).name} "
                "是本守卫的路由配置，agent 一律不得写改（D1）；配置变更请由"
                "操作员在会话外完成。",
                name,
                self._rel(_canonical_target(guard_table)),
                mode,
            )
            return

        if mode == "off":
            # D14: off short-circuits here, not in the factory. Everything
            # passes, but the first off-observed call after entering (or
            # re-entering) off writes one audit record and a host-log line:
            # operators keep visibility into what ran while disarmed, and
            # flipping the table back to enforce hot-applies on the next
            # mtime reload — no new session required.
            global _OFF_LOGGED
            if not _OFF_LOGGED:
                _OFF_LOGGED = True
                logger.info(
                    "dispatch-guard: mode=off — attached and passing all tool "
                    "calls (audit action=off marks the start of this period)"
                )
                self._log(
                    {
                        "action": "off",
                        "tool": "*",
                        "target": "",
                        "mode": "off",
                        "message": "guard disarmed by config; calls pass unexamined "
                        "until mode returns to enforce/warn",
                    }
                )
            async for event in next_handler():
                yield event
            return
        _OFF_LOGGED = False

        if not name:
            # D2: an unrecognizable tool_call shape must not silently disarm
            # the guard. Audit it; when anything about it smells like a spawn
            # call, refuse outright — the most conservative reading.
            #
            # The smell test reads the raw text, but the audit record does not
            # carry it: an unrecognizable shape is exactly the case where the
            # guard cannot tell a spawn body from anything else, so it gets the
            # same D10 digest treatment as the spawn backstop. Logging the blob
            # verbatim here would have reopened D10 on a new path.
            blob = str(tool_call)
            audit_target = _redacted_audit_target(blob)
            if "spawn" in blob.lower():
                yield self._deny(
                    "dispatch-guard: 无法识别的 tool_call 形状且疑似 spawn "
                    "调用，按最保守策略拒绝（D2）。",
                    "unknown",
                    audit_target,
                    mode,
                )
                return
            self._log(
                {
                    "action": "warned",
                    "tool": "unknown",
                    "target": audit_target,
                    "mode": mode,
                    "message": "dispatch-guard: 无法识别的 tool_call 形状；"
                    "已放行并记录（D2）",
                }
            )
            async for event in next_handler():
                yield event
            return
        forwarded = False
        try:
            async for event in self._dispatch(name, raw_input, mode, cfg, next_handler):
                forwarded = True
                yield event
        except Exception:
            if forwarded:
                # The tool already started answering: failing open now would
                # run next_handler a second time. A tool-originated error is
                # not the guard's to swallow — let it propagate.
                raise
            # F17: a guard-internal failure must never wedge the agent — host
            # log the traceback, audit it, and fail open with a warning block
            # (the project's fail-open rule), instead of letting the exception
            # bubble out of the middleware.
            logger.exception(
                "dispatch-guard: unexpected error while guarding %s; "
                "failing open with a warning",
                name or "<unknown>",
            )
            async for event in self._warn_and_pass(
                "[dispatch-guard warn] 守卫内部异常，本次按 warn 语义放行；"
                "详情见 host log。",
                name or "unknown",
                "",
                mode,
                next_handler,
            ):
                yield event

    async def _dispatch(
        self,
        name: str,
        raw_input: Any,
        mode: str,
        cfg: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """The decision path, split from on_acting so F17 can fail it open."""
        target, target_is_path = self._target_of(raw_input)

        if name in SPAWN_TOOLS:
            # D10: the spawn input body is not audit-log material — task
            # text carries credentials, and the log outlives the call.
            spawn_target = target if isinstance(target, str) else str(target)
            yield self._deny(
                "spawn_subagent 已停用（配置层禁用）。盲评或并行 worker 请用 "
                "chat_with_agent / submit_to_agent 派给专业 agent。",
                name,
                _redacted_audit_target(spawn_target),
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
                elif isinstance(raw_input, dict):
                    # D11: a legitimate non-deliverable path key must not
                    # close the scanner over the rest of the input — a
                    # deliverable path hidden in another argument is still
                    # worth a mention-level warning.
                    found = self._scan_candidates(
                        json.dumps(raw_input, ensure_ascii=False), cfg
                    )
                    if found:
                        rel, explicit = found[0][0], False
                        source = "其它参数中出现交付物路径"
            else:
                found = self._scan_candidates(target, cfg)
                if found:
                    rel, explicit = found[0]
                    if _has_typed_path_value(raw_input):
                        # D3: a deliverable shape inside a non-string path
                        # argument is an explicit write target, not a casual
                        # mention.
                        explicit = True
                        source = "类型异常的路径参数中含交付物"
                    else:
                        source = (
                            "参数/代码中出现交付物路径" if not explicit else "生成交付物"
                        )
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
            # D1 (shell arm): a command that MENTIONS the guard's own table
            # is treated as meta-integrity, not routing — denied under
            # enforce, warned in every other observed mode, before the
            # deliverable scan even runs.
            mention = _GUARD_CONFIG_MENTION_RE.search(cmd)
            if mention:
                note = (
                    f"dispatch-guard 元完整性：命令中提到本守卫的派发表 "
                    f"{mention.group(0)}（D1）。routes.json/routes.draft.json "
                    "只能由操作员在会话外维护；agent 经由 shell 触碰它们"
                    "（重定向、tee、sed -i、rm 等任意形式）一律按提及级拦截。"
                )
                if mode == "enforce":
                    yield self._deny(note + " 目标命令已拒绝。", name, mention.group(0), mode)
                    return
                async for event in self._warn_and_pass(
                    f"[dispatch-guard warn] {note}",
                    name,
                    mention.group(0),
                    mode,
                    next_handler,
                ):
                    yield event
                return
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
    # D14: the middleware attaches in EVERY mode. ``off`` short-circuits
    # inside on_acting — cheap to carry, and it is what makes an
    # off→enforce flip hot-apply by mtime instead of needing a new session.
    # Detaching on off made disarmament one-way and left no audit at all.
    mode = _load_config().get("mode", "enforce")
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
