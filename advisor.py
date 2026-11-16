"""Runtime log observer and WebSocket emitter for XPU Advisor."""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

try:
    from .rules import collect_environment, evaluate_environment, load_rules, match_log_message
except ImportError:
    from rules import collect_environment, evaluate_environment, load_rules, match_log_message


MAX_RUNTIME_ALERTS = 50


class XPUAdvisor:
    """Evaluates rules.json against live environment state and ComfyUI logs."""

    def __init__(
        self,
        rules_path: Path | None = None,
        emitter: Callable[[str, dict[str, Any]], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self.rules_path = rules_path
        self.rules = load_rules(rules_path)
        self._emitter = emitter
        self._clock = clock or time.monotonic
        self._cooldowns: dict[str, float] = {}
        self._runtime_alerts: deque[dict[str, Any]] = deque(maxlen=MAX_RUNTIME_ALERTS)
        self._handler: _AdvisorLogHandler | None = None

    def reload_rules(self) -> int:
        with self._lock:
            self.rules = load_rules(self.rules_path)
            return len(self.rules)

    def _emit_ws(self, event_name: str, payload: dict[str, Any]) -> None:
        if self._emitter is not None:
            self._emitter(event_name, payload)
            return
        try:
            import server

            prompt_server = getattr(server, "PromptServer", None)
            instance = getattr(prompt_server, "instance", None)
            send_sync = getattr(instance, "send_sync", None)
            if callable(send_sync):
                send_sync(event_name, payload)
        except Exception:
            pass

    def process_log_message(
        self,
        message: str,
        *,
        env_override: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Evaluate log text against log rules and emit deduplicated alerts."""
        if not message or "[XPUAdvisor]" in message:
            return []
        snapshot = env_override if env_override is not None else collect_environment()
        matches = match_log_message(message, snapshot, self.rules)
        if not matches:
            return []

        now = self._clock()
        emitted: list[dict[str, Any]] = []
        with self._lock:
            for finding in matches:
                rule_id = finding["id"]
                cooldown = finding.get("cooldown_seconds", 60.0)
                last_seen = self._cooldowns.get(rule_id)
                if last_seen is not None and (now - last_seen) < cooldown:
                    continue
                self._cooldowns[rule_id] = now
                alert = {
                    **finding,
                    "timestamp": time.strftime("%H:%M:%S"),
                }
                self._runtime_alerts.appendleft(alert)
                emitted.append(alert)

        for alert in emitted:
            self._emit_ws("xpu_advisor.alert", alert)
        return emitted

    def get_snapshot(self, custom_nodes_dirs: list[Path] | None = None) -> dict[str, Any]:
        """Return environment telemetry, env advisories, and live runtime alerts."""
        env = collect_environment(custom_nodes_dirs=custom_nodes_dirs)
        with self._lock:
            rules_copy = list(self.rules)
            runtime_alerts = list(self._runtime_alerts)
        advisories = evaluate_environment(env, rules_copy)
        return {
            "env": env,
            "advisories": advisories,
            "runtime_alerts": runtime_alerts,
            "rules_count": len(rules_copy),
        }

    def clear_runtime_alerts(self) -> None:
        with self._lock:
            self._runtime_alerts.clear()
            self._cooldowns.clear()

    def attach_logger(self, logger_name: str = "") -> None:
        """Attach a single non-intrusive handler to capture runtime warnings/errors."""
        root = logging.getLogger(logger_name)
        with self._lock:
            if self._handler is not None and self._handler in root.handlers:
                return
            for existing in list(root.handlers):
                if getattr(existing, "_is_xpu_advisor_handler", False):
                    self._handler = existing
                    return
            handler = _AdvisorLogHandler(self)
            root.addHandler(handler)
            self._handler = handler


class _AdvisorLogHandler(logging.Handler):
    _is_xpu_advisor_handler = True

    def __init__(self, advisor: XPUAdvisor) -> None:
        super().__init__(level=logging.INFO)
        self._advisor = advisor
        self._in_emit = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._in_emit, "active", False):
            return
        self._in_emit.active = True
        try:
            msg = record.getMessage()
            if record.exc_info:
                exc_type, exc_val, _ = record.exc_info
                if exc_val is not None:
                    msg = f"{msg} | {type(exc_val).__name__}: {exc_val}"
            self._advisor.process_log_message(msg)
        except Exception:
            pass
        finally:
            self._in_emit.active = False


_DEFAULT_ADVISOR: XPUAdvisor | None = None


def get_advisor() -> XPUAdvisor:
    global _DEFAULT_ADVISOR
    if _DEFAULT_ADVISOR is None:
        _DEFAULT_ADVISOR = XPUAdvisor()
    return _DEFAULT_ADVISOR
