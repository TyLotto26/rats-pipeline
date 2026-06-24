#!/usr/bin/env python3
"""Shared JSON state management for the Rats on Wallstreet pipeline."""

from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


HOME = Path.home()
PIPELINE_STATE = HOME / "pipeline-state.json"
TRADING_STATE = HOME / "trading-state.json"

STAGE_ORDER = ["polyscan", "whalewatch", "polybrain", "polyexec"]
STAGE_DEFAULTS: dict[str, dict[str, Any]] = {
    "polyscan": {"status": "idle", "markets": []},
    "whalewatch": {"status": "idle", "signals": []},
    "polybrain": {"status": "idle", "proposals": [], "notes": ""},
    "polyexec": {"status": "idle", "last_result": ""},
}
TRADING_DEFAULTS: dict[str, Any] = {"mode": "paper", "bankroll": 48.85}
STAGE_LIST_KEYS = {
    "polyscan": ("markets",),
    "whalewatch": ("signals",),
    "polybrain": ("proposals",),
}
VALID_STAGE_STATUSES = {"idle", "pending", "running", "complete", "completed", "error", "skipped", "active"}

UPSTREAM = {
    "polyscan": [],
    "whalewatch": ["polyscan"],
    "polybrain": ["polyscan", "whalewatch"],
    "polyexec": ["polyscan", "whalewatch", "polybrain"],
}


class StateValidationError(ValueError):
    """Raised when a state document does not match the expected shape."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return deepcopy(default)
    try:
        with path.open() as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise StateValidationError(f"{path} contains invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise StateValidationError(f"{path} must contain a JSON object")
    return data


def validate_pipeline_state(state: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise StateValidationError("pipeline state must be a dict")
    for stage, defaults in STAGE_DEFAULTS.items():
        section = state.setdefault(stage, deepcopy(defaults))
        if not isinstance(section, dict):
            raise StateValidationError(f"pipeline state stage {stage!r} must be a dict")
        section.setdefault("status", defaults["status"])
        if not isinstance(section.get("status"), str):
            raise StateValidationError(f"pipeline state stage {stage!r}.status must be a string")
        if section["status"] not in VALID_STAGE_STATUSES:
            raise StateValidationError(f"pipeline state stage {stage!r}.status is invalid: {section['status']!r}")
        for key, value in defaults.items():
            if key != "status":
                section.setdefault(key, deepcopy(value))
        for key in STAGE_LIST_KEYS.get(stage, ()):
            if not isinstance(section.get(key), list):
                raise StateValidationError(f"pipeline state stage {stage!r}.{key} must be a list")
        if "last_run" in section and not isinstance(section["last_run"], str):
            raise StateValidationError(f"pipeline state stage {stage!r}.last_run must be a string")
    state.setdefault("cycle", 0)
    if not isinstance(state["cycle"], int):
        raise StateValidationError("pipeline state cycle must be an integer")
    state.setdefault("last_updated", utc_now())
    if not isinstance(state["last_updated"], str):
        raise StateValidationError("pipeline state last_updated must be a string")
    return state


def validate_trading_state(state: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise StateValidationError("trading state must be a dict")
    state.setdefault("mode", TRADING_DEFAULTS["mode"])
    state.setdefault("bankroll", TRADING_DEFAULTS["bankroll"])
    try:
        state["bankroll"] = float(state["bankroll"])
    except (TypeError, ValueError) as exc:
        raise StateValidationError("trading state bankroll must be numeric") from exc
    if not isinstance(state.get("mode"), str):
        raise StateValidationError("trading state mode must be a string")
    return state


def load(path: str | Path | None = None, *, state_type: str = "pipeline") -> dict[str, Any]:
    target = Path(path).expanduser() if path else (TRADING_STATE if state_type == "trading" else PIPELINE_STATE)
    default = TRADING_DEFAULTS if state_type == "trading" else {}
    state = _read_json(target, default)
    return validate_trading_state(state) if state_type == "trading" else validate_pipeline_state(state)


def save(state: dict[str, Any], path: str | Path | None = None, *, state_type: str = "pipeline") -> dict[str, Any]:
    target = Path(path).expanduser() if path else (TRADING_STATE if state_type == "trading" else PIPELINE_STATE)
    if state_type == "trading":
        data = validate_trading_state(state)
    else:
        data = validate_pipeline_state(state)
        data["last_updated"] = utc_now()
    _atomic_write_json(target, data)
    return data


def load_pipeline_state(path: str | Path | None = None) -> dict[str, Any]:
    return load(path, state_type="pipeline")


def save_pipeline_state(state: dict[str, Any], path: str | Path | None = None) -> dict[str, Any]:
    return save(state, path, state_type="pipeline")


def load_trading_state(path: str | Path | None = None) -> dict[str, Any]:
    return load(path, state_type="trading")


def save_trading_state(state: dict[str, Any], path: str | Path | None = None) -> dict[str, Any]:
    return save(state, path, state_type="trading")


def read_stage(stage: str, path: str | Path | None = None) -> dict[str, Any]:
    if stage not in STAGE_DEFAULTS:
        raise StateValidationError(f"unknown pipeline stage: {stage}")
    return load_pipeline_state(path).get(stage, deepcopy(STAGE_DEFAULTS[stage]))


def write_stage(stage: str, data: dict[str, Any], path: str | Path | None = None) -> dict[str, Any]:
    if stage not in STAGE_DEFAULTS:
        raise StateValidationError(f"unknown pipeline stage: {stage}")
    if not isinstance(data, dict):
        raise StateValidationError("stage data must be a dict")
    state = load_pipeline_state(path)
    section = state.setdefault(stage, deepcopy(STAGE_DEFAULTS[stage]))
    section["status"] = data.get("status", "complete")
    section["last_run"] = utc_now()
    for key, value in data.items():
        if key not in {"status", "last_run"}:
            section[key] = value
    return save_pipeline_state(state, path)


def check_upstream(stage: str, path: str | Path | None = None) -> dict[str, Any]:
    state = load_pipeline_state(path)
    result: dict[str, Any] = {"ready": True, "upstream": {}}
    for dep in UPSTREAM.get(stage, []):
        status = state.get(dep, {}).get("status", "unknown")
        ready = status in {"complete", "completed"}
        result["upstream"][dep] = {"status": status, "ready": ready}
        if not ready:
            result["ready"] = False
    return result


def is_fresh(stage: str, path: str | Path | None = None, max_age_minutes: int = 5) -> bool:
    """Return True if `stage` exists, is completed, and completed within `max_age_minutes`."""
    state = load_pipeline_state(path)
    section = state.get(stage, {})

    if section.get("status") not in {"complete", "completed"}:
        return False

    last_run = section.get("last_run")
    if not last_run:
        return False

    try:
        last_dt = datetime.fromisoformat(str(last_run).replace("Z", "+00:00"))
        elapsed = datetime.now(timezone.utc) - last_dt
        return elapsed < timedelta(minutes=max_age_minutes)
    except (ValueError, TypeError):
        return False


def wait_for_upstream(stage: str, path: str | Path | None = None, *, timeout: int = 180, poll_interval: int = 3) -> dict[str, Any]:
    """Block until all upstream deps for `stage` are complete AND fresh (within 15 min)."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        upstream = check_upstream(stage, path)
        if not upstream["ready"]:
            time.sleep(poll_interval)
            continue
        all_fresh = all(
            is_fresh(dep, path, max_age_minutes=15)
            for dep in UPSTREAM.get(stage, [])
        )
        if all_fresh or not UPSTREAM.get(stage):
            return upstream
        time.sleep(poll_interval)
    return {
        "ready": False,
        "error": f"TIMEOUT: upstream for {stage} not fresh after {timeout}s"
    }


__all__ = [
    "PIPELINE_STATE",
    "TRADING_STATE",
    "STAGE_ORDER",
    "STAGE_DEFAULTS",
    "UPSTREAM",
    "StateValidationError",
    "load",
    "save",
    "load_pipeline_state",
    "save_pipeline_state",
    "load_trading_state",
    "save_trading_state",
    "read_stage",
    "write_stage",
    "check_upstream",
    "is_fresh",
    "wait_for_upstream",
    "validate_pipeline_state",
    "validate_trading_state",
]
