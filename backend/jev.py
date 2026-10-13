"""dispatch-guard's jev layer: an opt-in semantic arbitration hook.

P0 scope (draft generation): when the orchestrator's manifest declares no
dispatch policy, the keyword blob scan is a guess; this layer asks a small
local judgment model (a jev "judge") which live agent should own each
deliverable category instead, falling back to the keyword scan whenever the
judge is absent, broken, slow or unsure. The keyword scan is the fallback,
never replaced.

Two halves, both stdlib-only so the plugin keeps its zero-dependency promise:

* Wire client — a protocol-faithful mirror of jev-kernel's
  ``backend/judges.py`` raw ``systemone`` dialect (baseUrl normalisation,
  boolean->noul translation, choice criteria as a {label: description}
  mapping, noul->probability and score->argmax answer translation, ordered
  answers-list zip). Mirrored instead of imported so dispatch-guard keeps
  its bare-Python test/CI promise and releases independently of jev-kernel.
  Contract tests pin the mirror against a fake server.

* Config resolution — routes.json's ``jev`` block: ``enabled`` (default
  false; absent/false is byte-identical to the pre-jev plugin), ``judges``
  (``"inherit:jev-kernel"`` reads the sibling plugin's jev.json as the single
  source of truth for endpoints/models — plugin-directory copy first,
  plugin-data copy second, ``$JEV_KERNEL_CONFIG`` override first of all — or
  an inline list of judge specs), ``tiers`` (whitelist; ``["lux"]`` only per
  the integration decision), ``thresholds`` (``"inherit"`` or
  ``{"deny":…, "warn":…}``), ``online_timeout_ms`` (hard cap per judge call,
  min'ed with the spec's own timeoutMs) and ``record`` (audit toggle).
  Every field degrades to its default on a wrong type instead of crashing,
  matching routes.json's own normalisation rule.

Privacy: disabled by default and fully offline then. Enabled with the
inherit chain it dials exactly the judge endpoints jev.json names (the
deployment's own LAN/CGNAT models) and nothing else.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger("dispatch_guard.jev")

JEV_KERNEL_ID = "jev-kernel"
INHERIT_PREFIX = "inherit:"
DEFAULT_TIMEOUT_MS = 2500
DEFAULT_THRESHOLDS = {"deny": 0.85, "warn": 0.6}
# jev-kernel's own defaults (backend/config.py DEFAULTS["thresholds"]); used
# when an inherited jev.json carries no thresholds block of its own.
_KERNEL_DEFAULT_THRESHOLDS = {"yes": 0.85, "no": 0.15, "minProbability": 0.6}


# ---------------------------------------------------------------------------
# Wire protocol — mirror of jev-kernel backend/judges.py (systemone dialect).
# Keep these five functions byte-equivalent in behaviour; the contract tests
# exist to catch drift.
# ---------------------------------------------------------------------------


def _systemone_url(base_url: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(base_url)
    if parts.path in ("", "/"):
        return base_url.rstrip("/") + "/v1/systemone"
    if parts.path == "/v1":
        return base_url.rstrip("/") + "/systemone"
    return base_url


def _to_systemone_questions(questions: dict[str, Any]) -> dict[str, Any]:
    """Gateway question names -> raw System One names (boolean is `noul`)."""
    out: dict[str, Any] = {}
    for key, question in questions.items():
        if not isinstance(question, dict):
            out[key] = question
            continue
        translated = dict(question)
        if translated.get("type") == "boolean":
            translated["type"] = "noul"
        if translated.get("type") == "choice" and not isinstance(translated.get("criteria"), dict):
            # gateway wire carries an `options` list; raw System One wants a
            # {label: description} object, so label each option with itself.
            options = translated.pop("options", None)
            if isinstance(options, list) and options:
                translated["criteria"] = {str(option): str(option) for option in options}
        out[key] = translated
    return out


def _from_systemone_answers(answers: dict[str, Any], wire: dict[str, Any]) -> dict[str, Any]:
    """Raw System One answers -> gateway-shaped answers the parser expects."""
    out: dict[str, Any] = {}
    for key, answer in answers.items():
        if not isinstance(answer, dict):
            out[key] = answer
            continue
        kind = answer.get("type")
        if kind == "noul":
            probability = answer.get("noul", answer.get("probability"))
            out[key] = {"type": "boolean", "probability": probability}
            continue
        if kind == "score":
            spec = wire.get(key)
            criteria = spec.get("criteria") if isinstance(spec, dict) else None
            levels = len(criteria) if isinstance(criteria, list) and criteria else None
            index = None
            probs = answer.get("probabilities")
            if isinstance(probs, dict) and probs:
                parsed: list[tuple[int, float]] = []
                for level, value in probs.items():
                    try:
                        level_index = int(level)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        if levels is None or 0 <= level_index < levels:
                            parsed.append((level_index, value))
                if parsed:
                    index = max(parsed, key=lambda item: item[1])[0]
            if index is None and isinstance(answer.get("score"), (int, float)):
                index = int(round(float(answer["score"])))
                if levels is not None:
                    index = min(max(index, 0), levels - 1)
            if index is None:
                out[key] = answer
                continue
            confidence = answer.get("confidence")
            if not isinstance(confidence, (int, float)) and isinstance(probs, dict):
                confidence = probs.get(str(index))
            out[key] = {"type": "score", "score": index, "confidence": confidence}
            continue
        out[key] = answer
    return out


def _answers_of(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("answers"), dict):
        return payload["answers"]
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("answers"), dict):
        return result["answers"]
    if isinstance(result, dict):
        return result
    # bare answers map: {"q0": {"type": ...}, ...}
    if any(isinstance(v, dict) and "type" in v for v in payload.values()):
        return payload
    return {}


def _normalize_answers(payload: Any, wire: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Answers map, positional list mapping, or a shape diagnostic."""
    answers = _answers_of(payload)
    if not answers and isinstance(payload, dict):
        listed = payload.get("answers")
        if isinstance(listed, list):  # answers as an ordered list
            # strict=False on purpose: a judge may answer fewer questions than
            # asked (the rest cascade); extra entries have no question to bind.
            answers = dict(zip(wire.keys(), listed, strict=False))
        result = payload.get("result")
        if not answers and isinstance(result, dict) and isinstance(result.get("answers"), list):
            answers = dict(zip(wire.keys(), result["answers"], strict=False))
    if not answers and payload:
        answers = {"_parse_error": json.dumps(payload, ensure_ascii=False)[:200]}
    return answers


def _post_json(url: str, body: dict[str, Any], api_key: str, timeout_s: float) -> Any:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _judge_sync(
    spec: dict[str, Any],
    state: Any,
    questions: dict[str, dict[str, Any]],
    timeout_ms: int,
) -> dict[str, Any]:
    """One judge, one request. Returns the raw answers mapping."""
    jtype = spec.get("type", "http")
    api_key = spec.get("apiKey") or ""
    if not api_key and spec.get("apiKeyEnv"):
        api_key = os.environ.get(spec["apiKeyEnv"], "")
    timeout_s = max(0.25, (timeout_ms or 4000) / 1000.0)

    if jtype == "systemone":
        url = _systemone_url(spec.get("baseUrl") or "")
        body = {
            "model": spec.get("model") or "clef-flash",
            "state": state,
            "questions": _to_systemone_questions(questions),
        }
        answers = _normalize_answers(_post_json(url, body, api_key, timeout_s), questions)
        return _from_systemone_answers(answers, questions)

    if jtype == "http":
        url = spec.get("baseUrl") or ""
        body = {"state": state, "questions": questions}
        return _normalize_answers(_post_json(url, body, api_key, timeout_s), questions)

    # typesafe/llm transports exist upstream but are out of scope here: the
    # deployment's judges are raw systemone, and an unknown type resolves to
    # no answers (the question cascades or falls back), never a crash.
    return {}


# ---------------------------------------------------------------------------
# Config resolution (routes.json "jev" block).
# ---------------------------------------------------------------------------


class Runtime:
    """A resolved, ready-to-call judge configuration."""

    def __init__(
        self,
        specs: list[dict[str, Any]],
        thresholds: dict[str, float],
        timeout_ms: int,
        record: bool,
    ) -> None:
        self.specs = specs
        self.thresholds = thresholds
        self.timeout_ms = timeout_ms
        self.record = record


def _num(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _jev_kernel_config_path(plugin_dir: Path) -> Path | None:
    """jev-kernel's jev.json, in its own resolution order (config.py):

    ``$JEV_KERNEL_CONFIG`` override first, then the plugin-directory copy
    (the operator's explicit override), then the plugin-data copy (the one
    that survives a reinstall of jev-kernel).
    """
    env = os.environ.get("JEV_KERNEL_CONFIG")
    if env:
        path = Path(env)
        return path if path.exists() else None
    if plugin_dir.parent.name == "plugins":
        host = plugin_dir.parent.parent
    else:
        host = Path.home() / ".qwenpaw"
    for candidate in (
        host / "plugins" / JEV_KERNEL_ID / "jev.json",
        host / "plugin-data" / JEV_KERNEL_ID / "jev.json",
    ):
        if candidate.exists():
            return candidate
    return None


def _clean_spec(entry: Any) -> dict[str, Any] | None:
    """One inline judge spec, type-normalised; None when unusable."""
    if not isinstance(entry, dict):
        logger.warning("dispatch-guard jev: judge spec %r is not an object; dropped", entry)
        return None
    name = entry.get("name")
    base_url = entry.get("baseUrl")
    if not name or not isinstance(name, str) or not isinstance(base_url, str) or not base_url:
        logger.warning(
            "dispatch-guard jev: judge spec %r needs a string name and baseUrl; dropped",
            entry,
        )
        return None
    return {
        "name": name,
        "type": entry.get("type") if isinstance(entry.get("type"), str) else "systemone",
        "baseUrl": base_url,
        "model": entry.get("model") if isinstance(entry.get("model"), str) else "",
        "timeoutMs": _num(entry.get("timeoutMs"), 3000),
        **({"apiKeyEnv": entry["apiKeyEnv"]} if isinstance(entry.get("apiKeyEnv"), str) else {}),
    }


def _inherit_specs(
    tiers: list[str], plugin_dir: Path
) -> tuple[list[dict[str, Any]], str | None, dict[str, float]]:
    """Judge specs from jev-kernel's jev.json, filtered to the tier whitelist.

    Returns (specs, disabled_reason, thresholds): a reason other than None
    means the jev layer is configured but cannot run — missing kernel config,
    an upstream kill-switch, or no whitelisted tier with a spec — and the
    caller audits it. Thresholds come back mapped into this plugin's
    {deny, warn} vocabulary.
    """
    path = _jev_kernel_config_path(plugin_dir)
    if path is None:
        return [], "no_judge_configured", dict(DEFAULT_THRESHOLDS)
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("dispatch-guard jev: %s is unreadable; jev layer disabled", path)
        return [], "no_judge_configured", dict(DEFAULT_THRESHOLDS)
    if not isinstance(cfg, dict):
        return [], "no_judge_configured", dict(DEFAULT_THRESHOLDS)
    if cfg.get("enabled") is False:
        # jev-kernel's global kill-switch is authoritative: a deployment that
        # parked its judges must not be dialed around by this plugin.
        return [], "disabled_upstream", dict(DEFAULT_THRESHOLDS)
    kernel_thresholds = cfg.get("thresholds") if isinstance(cfg.get("thresholds"), dict) else {}
    thresholds = {
        "deny": _num(kernel_thresholds.get("yes"), _KERNEL_DEFAULT_THRESHOLDS["yes"]),
        "warn": _num(
            kernel_thresholds.get("minProbability"),
            _KERNEL_DEFAULT_THRESHOLDS["minProbability"],
        ),
    }
    judges = cfg.get("judges") if isinstance(cfg.get("judges"), dict) else {}
    specs: list[dict[str, Any]] = []
    for tier in tiers:
        entry = judges.get(tier)
        if entry is None:
            logger.warning(
                "dispatch-guard jev: tier %r has no spec in %s; skipped", tier, path
            )
            continue
        spec = _clean_spec({"name": tier, **entry} if isinstance(entry, dict) else entry)
        if spec is not None:
            specs.append(spec)
    if not specs:
        return [], "no_judge_configured", thresholds
    return specs, None, thresholds


def resolve(
    cfg: dict[str, Any], plugin_dir: Path
) -> tuple[Runtime | None, str | None, bool]:
    """(runtime, disabled_reason, record) for routes.json's ``jev`` block.

    (None, None, …) means jev is simply off — absent block or enabled:false —
    and the caller stays byte-identical to the pre-jev plugin, no audit
    record. (None, reason, …) means the operator turned jev on but it cannot
    run; the reason lands in the audit trail. A Runtime means ready.
    """
    block = cfg.get("jev")
    if not isinstance(block, dict):
        if block is not None:
            logger.warning(
                "dispatch-guard jev: 'jev' must be an object; jev layer disabled"
            )
        return None, None, True
    if block.get("enabled") is not True:
        return None, None, True
    record = block.get("record") is not False
    timeout_ms = int(_num(block.get("online_timeout_ms"), DEFAULT_TIMEOUT_MS))
    tiers_raw = block.get("tiers")
    tiers = (
        [str(t) for t in tiers_raw if str(t).strip()]
        if isinstance(tiers_raw, list)
        else ["lux"]
    )
    judges = block.get("judges", f"{INHERIT_PREFIX}{JEV_KERNEL_ID}")
    if isinstance(judges, str) and judges == f"{INHERIT_PREFIX}{JEV_KERNEL_ID}":
        specs, reason, thresholds = _inherit_specs(tiers, plugin_dir)
    elif isinstance(judges, list):
        specs, reason = [], None
        for entry in judges:
            spec = _clean_spec(entry)
            if spec is not None:
                specs.append(spec)
        thresholds_raw = block.get("thresholds")
        thresholds = dict(DEFAULT_THRESHOLDS)
        if isinstance(thresholds_raw, dict):
            thresholds = {
                "deny": _num(thresholds_raw.get("deny"), DEFAULT_THRESHOLDS["deny"]),
                "warn": _num(thresholds_raw.get("warn"), DEFAULT_THRESHOLDS["warn"]),
            }
        if not specs:
            reason = "no_judge_configured"
    else:
        logger.warning(
            "dispatch-guard jev: 'judges' must be %r or a list of specs; "
            "jev layer disabled",
            f"{INHERIT_PREFIX}{JEV_KERNEL_ID}",
        )
        return None, "no_judge_configured", record
    if reason is not None:
        return None, reason, record
    return Runtime(specs, thresholds, timeout_ms, record), None, record


# ---------------------------------------------------------------------------
# Asking.
# ---------------------------------------------------------------------------


def _fallback_verdict(reason: str) -> dict[str, Any]:
    return {
        "value": None,
        "probability": 0.0,
        "uncertain": True,
        "judge": "",
        "latencyMs": 0.0,
        "fallback": reason,
    }


def _parse_choice(
    raw: Any, options: list[str], warn: float, judge: str, latency_ms: float
) -> dict[str, Any] | None:
    """Validate one choice answer (mirror of questions.py parse_verdict).

    None when malformed; otherwise a verdict dict whose ``uncertain`` flag is
    p < warn — this plugin's single acceptance gate for draft routing.
    """
    if not isinstance(raw, dict):
        return None
    choice = raw.get("choice")
    if choice not in options:
        return None
    probs = raw.get("probabilities")
    p = probs.get(choice) if isinstance(probs, dict) else raw.get("probability")
    if p is None or isinstance(p, bool) or not isinstance(p, (int, float)):
        return None
    p = max(0.0, min(1.0, float(p)))
    return {
        "value": choice,
        "probability": p,
        "uncertain": p < warn,
        "judge": judge,
        "latencyMs": round(latency_ms, 1),
        "fallback": "",
    }


def ask(
    runtime: Runtime, state: Any, questions: dict[str, dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Ask every question through the tier list; later tiers see only the
    questions earlier ones left uncertain (jev-kernel's cascade semantics —
    with the lux-only whitelist this is exactly one request).

    Returns (verdicts, audit): every question gets a verdict dict, and audit
    carries the whole call's {judge, latencyMs} for the caller's log record.
    A judge that errors, times out or answers garbage never raises: its
    questions fall through with a ``fallback`` reason.
    """
    pending = dict(questions)
    results: dict[str, dict[str, Any]] = {}
    tried: list[str] = []
    errors: list[str] = []
    total_ms = 0.0
    for spec in runtime.specs:
        if not pending:
            break
        tried.append(spec["name"])
        timeout_ms = int(min(_num(spec.get("timeoutMs"), 3000), runtime.timeout_ms))
        started = time.monotonic()
        failed = False
        try:
            answers = _judge_sync(spec, state, dict(pending), timeout_ms)
        except (urllib.error.URLError, OSError, ValueError, socket.timeout) as exc:
            reason = "timeout" if isinstance(exc, (socket.timeout, TimeoutError)) else f"error:{type(exc).__name__}"
            errors.append(f"{spec['name']}:{reason}")
            logger.warning("dispatch-guard jev: judge %s failed: %s", spec["name"], exc)
            answers = {}
            failed = True
        except Exception as exc:  # noqa: BLE001 - a judge must never wedge the agent
            errors.append(f"{spec['name']}:error:{type(exc).__name__}")
            logger.warning("dispatch-guard jev: judge %s failed: %s", spec["name"], exc)
            answers = {}
            failed = True
        latency_ms = (time.monotonic() - started) * 1000.0
        total_ms += latency_ms
        if "_parse_error" in answers:
            errors.append(f"{spec['name']}:parse_error")
        still: dict[str, dict[str, Any]] = {}
        if not failed:
            # The judge answered; grade each question. (On a wholesale failure
            # the tier's error already says why — marking every question
            # "malformed" on top would be noise, and would bury "timeout".)
            for key, question in pending.items():
                verdict = _parse_choice(
                    answers.get(key),
                    [str(o) for o in question.get("criteria", {})],
                    runtime.thresholds["warn"],
                    spec["name"],
                    latency_ms,
                )
                if verdict is None:
                    errors.append(f"{spec['name']}:malformed:{key}")
                    still[key] = question
                elif verdict["uncertain"]:
                    still[key] = question
                else:
                    results[key] = verdict
            pending = still
    for key in pending:
        reason = ";".join(errors) if errors else "uncertain"
        results[key] = _fallback_verdict(reason)
    return results, {"judge": ",".join(tried), "latencyMs": round(total_ms, 1)}


def category_questions(
    agents: list[dict[str, Any]], presets: tuple[dict[str, Any], ...]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """(state, questions) for the P0 draft hook: one choice question per
    deliverable category, asked in a single request.

    Criteria are the live agent ids (description truncated to 200 chars, per
    the integration plan) plus ``unrouted`` — a judge that cannot place a
    category must say so, and that answer falls back to the keyword scan.
    """
    criteria_base: dict[str, str] = {}
    for agent in agents:
        description = str(agent.get("description") or "")[:200]
        criteria_base[str(agent["id"])] = description or str(agent["id"])
    criteria_base["unrouted"] = "no listed specialist owns this category"
    state = {
        "categories": [
            {"key": f"q{i}", "extensions": list(preset["exts"])}
            for i, preset in enumerate(presets)
        ],
        "agents": [
            {
                "id": str(a["id"]),
                "skills": [str(s) for s in a.get("skills", [])],
                "description": str(a.get("description") or "")[:200],
            }
            for a in agents
        ],
    }
    questions: dict[str, dict[str, Any]] = {}
    for i, preset in enumerate(presets):
        exts = ", ".join(preset["exts"])
        questions[f"q{i}"] = {
            "type": "choice",
            "instructions": (
                "Which agent should own deliverable files with extensions "
                f"{exts}? Answer 'unrouted' when none fits."
            ),
            "criteria": dict(criteria_base),
        }
    return state, questions
