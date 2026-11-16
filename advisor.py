"""Runtime log observer and WebSocket emitter for XPU Advisor."""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

try:
    from .rules import collect_environment, evaluate_environment, evaluate_workflow, load_rules, match_log_message
except ImportError:
    from rules import collect_environment, evaluate_environment, evaluate_workflow, load_rules, match_log_message


MAX_RUNTIME_ALERTS = 50

_RE_STORAGE_PATHS = re.compile(r"Model storage policy:.*paths=\[([^\]]+)\]")
_RE_REQUESTED_LOAD = re.compile(r"Requested to load ([A-Za-z0-9_]+)")
_RE_PREPARED_MODEL = re.compile(r"Model ([A-Za-z0-9_]+) prepared for dynamic VRAM loading")
_RE_MODEL_FILE = re.compile(
    r"([A-Za-z0-9_.\-\\/]+?\.(?:safetensors|ckpt|gguf|pt|pth|bin|sft))\b",
    re.IGNORECASE,
)
_MODEL_INPUT_KEYS = (
    "unet_name",
    "ckpt_name",
    "model_name",
    "diffusion_model",
    "clip_name",
    "clip_name1",
    "clip_name2",
    "vae_name",
    "lora_name",
)


def _extract_basename(raw_path: str) -> str:
    cleaned = str(raw_path).strip().strip("'\"")
    if not cleaned:
        return ""
    normalized = cleaned.replace("\\", "/")
    return normalized.rsplit("/", 1)[-1]


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
        self._prompt_nodes: dict[str, dict[str, Any]] = {}
        self._file_to_node: dict[str, str] = {}
        self._node_to_files: dict[str, list[str]] = {}
        self._last_staged_files: list[str] = []
        self._class_to_models: dict[str, list[str]] = {}
        self._class_to_node: dict[str, str] = {}
        self._latest_workflow: dict[str, Any] | None = None

    def reload_rules(self) -> int:
        with self._lock:
            self.rules = load_rules(self.rules_path)
            return len(self.rules)

    def update_workflow(self, workflow_data: dict[str, Any] | None) -> None:
        if isinstance(workflow_data, dict):
            with self._lock:
                self._latest_workflow = workflow_data

    def record_prompt(self, prompt_data: dict[str, Any] | None) -> None:
        """Index node IDs, class types, titles, and model filenames from a ComfyUI prompt."""
        if not isinstance(prompt_data, dict):
            return
        prompt_graph = prompt_data.get("prompt") if isinstance(prompt_data.get("prompt"), dict) else prompt_data
        if not isinstance(prompt_graph, dict):
            return

        nodes: dict[str, dict[str, Any]] = {}
        file_to_node: dict[str, str] = {}
        node_to_files: dict[str, list[str]] = {}

        for raw_id, spec in prompt_graph.items():
            if not isinstance(spec, dict) or "class_type" not in spec:
                continue
            node_id = str(raw_id)
            class_type = str(spec.get("class_type") or "").strip()
            meta = spec.get("_meta") if isinstance(spec.get("_meta"), dict) else {}
            title = str(meta.get("title") or "").strip()
            if title and title != class_type:
                node_label = f"#{node_id} · {class_type} ({title})"
            elif class_type:
                node_label = f"#{node_id} · {class_type}"
            else:
                node_label = f"#{node_id}"

            inputs = spec.get("inputs") if isinstance(spec.get("inputs"), dict) else {}
            model_files: list[str] = []
            for key, val in inputs.items():
                if not isinstance(val, str):
                    continue
                if key in _MODEL_INPUT_KEYS or _RE_MODEL_FILE.search(val):
                    base = _extract_basename(val)
                    if base and base not in model_files:
                        model_files.append(base)

            nodes[node_id] = {
                "node_id": node_id,
                "class_type": class_type,
                "title": title,
                "node_label": node_label,
                "model_files": model_files,
            }
            if model_files:
                node_to_files[node_id] = model_files
                for fname in model_files:
                    file_to_node[fname] = node_id

        if not nodes:
            return
        with self._lock:
            self._latest_workflow = prompt_graph
            self._prompt_nodes = nodes
            self._file_to_node.update(file_to_node)
            self._node_to_files.update(node_to_files)

    def _get_current_node_id(self) -> str | None:
        try:
            import server

            prompt_server = getattr(server, "PromptServer", None)
            instance = getattr(prompt_server, "instance", None)
            last_node = getattr(instance, "last_node_id", None)
            if last_node is not None and str(last_node).strip():
                return str(last_node).strip()
        except Exception:
            pass
        return None

    def _sync_from_prompt_server(self) -> None:
        with self._lock:
            has_nodes = bool(self._prompt_nodes)
        if has_nodes:
            return
        try:
            import server

            prompt_server = getattr(server, "PromptServer", None)
            instance = getattr(prompt_server, "instance", None)
            prompt_queue = getattr(instance, "prompt_queue", None)
            if prompt_queue is None:
                return
            currently_running = getattr(prompt_queue, "currently_running", None)
            if isinstance(currently_running, dict):
                for item in currently_running.values():
                    if isinstance(item, (list, tuple)) and len(item) >= 3 and isinstance(item[2], dict):
                        self.record_prompt(item[2])
                        return
            get_history = getattr(prompt_queue, "get_history", None)
            if callable(get_history):
                history = get_history(max_items=3)
                if isinstance(history, dict):
                    for entry in reversed(list(history.values())):
                        prompt_tuple = entry.get("prompt") if isinstance(entry, dict) else None
                        if isinstance(prompt_tuple, (list, tuple)) and len(prompt_tuple) >= 3 and isinstance(prompt_tuple[2], dict):
                            self.record_prompt(prompt_tuple[2])
                            return
        except Exception:
            pass

    def _format_node_label(self, node_id: str | None) -> str | None:
        if not node_id:
            return None
        info = self._prompt_nodes.get(str(node_id))
        if info:
            return str(info["node_label"])
        return f"#{node_id}"

    def _update_log_context_locked(
        self,
        message: str,
        active_node_id: str | None,
    ) -> tuple[str | None, str | None, str | None]:
        """Update tracked model/node mappings from log lines and return (node_id, node_label, target_object)."""
        curr_node = str(active_node_id).strip() if active_node_id else None

        storage_match = _RE_STORAGE_PATHS.search(message)
        if storage_match:
            raw_list = storage_match.group(1)
            staged = [
                _extract_basename(part)
                for part in raw_list.split(",")
                if _extract_basename(part)
            ]
            if staged:
                self._last_staged_files = staged
                if not curr_node:
                    for fname in staged:
                        if fname in self._file_to_node:
                            curr_node = self._file_to_node[fname]
                            break
                if curr_node:
                    self._node_to_files[curr_node] = staged
                    for fname in staged:
                        self._file_to_node[fname] = curr_node

        cls_match = _RE_REQUESTED_LOAD.search(message) or _RE_PREPARED_MODEL.search(message)
        matched_cls = cls_match.group(1) if cls_match else None
        if matched_cls:
            files_for_cls = list(self._last_staged_files)
            if not files_for_cls and curr_node and curr_node in self._node_to_files:
                files_for_cls = list(self._node_to_files[curr_node])
            if files_for_cls:
                self._class_to_models[matched_cls] = files_for_cls
                if not curr_node:
                    for fname in files_for_cls:
                        if fname in self._file_to_node:
                            curr_node = self._file_to_node[fname]
                            break
            if curr_node:
                self._class_to_node[matched_cls] = curr_node

        if not matched_cls:
            for known_cls in self._class_to_models:
                if re.search(rf"\b{re.escape(known_cls)}\b", message):
                    matched_cls = known_cls
                    break

        target_object: str | None = None
        if matched_cls:
            mapped_files = self._class_to_models.get(matched_cls, [])
            if mapped_files:
                target_object = f"{', '.join(mapped_files)} ({matched_cls})"
            else:
                target_object = matched_cls
            if not curr_node and matched_cls in self._class_to_node:
                curr_node = self._class_to_node[matched_cls]

        if not target_object:
            inline_files = [
                _extract_basename(m)
                for m in _RE_MODEL_FILE.findall(message)
                if _extract_basename(m)
            ]
            if inline_files:
                dedup = list(dict.fromkeys(inline_files))
                target_object = ", ".join(dedup)
                if not curr_node:
                    for fname in dedup:
                        if fname in self._file_to_node:
                            curr_node = self._file_to_node[fname]
                            break

        if not target_object and curr_node and curr_node in self._node_to_files:
            target_object = ", ".join(self._node_to_files[curr_node])

        if not target_object and self._last_staged_files:
            target_object = ", ".join(self._last_staged_files)
            if not curr_node:
                for fname in self._last_staged_files:
                    if fname in self._file_to_node:
                        curr_node = self._file_to_node[fname]
                        break

        node_label = self._format_node_label(curr_node)
        return curr_node, node_label, target_object

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
        active_node_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Evaluate log text against log rules and emit deduplicated alerts."""
        if not message or "[XPUAdvisor]" in message:
            return []
        self._sync_from_prompt_server()
        resolved_active_node = active_node_id if active_node_id is not None else self._get_current_node_id()
        with self._lock:
            node_id, node_label, target_object = self._update_log_context_locked(message, resolved_active_node)
            rules_copy = list(self.rules)

        snapshot = env_override if env_override is not None else collect_environment()
        matches = match_log_message(message, snapshot, rules_copy)
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
                if node_id:
                    alert["node_id"] = node_id
                if node_label:
                    alert["node_label"] = node_label
                if target_object:
                    alert["target_object"] = target_object
                self._runtime_alerts.appendleft(alert)
                emitted.append(alert)

        for alert in emitted:
            self._emit_ws("xpu_advisor.alert", alert)
        return emitted

    def _enrich_fp8_context_locked(self, env: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
        """Resolve specific model filenames and loader node for loaded FP8 diffusion models."""
        if not env.get("has_loaded_fp8_diffusion"):
            return None, None, None
        raw_names = str(env.get("loaded_fp8_model_names") or "").strip()
        cls_list = [c.strip() for c in raw_names.split(",") if c.strip()]

        detailed_parts: list[str] = []
        resolved_node_id: str | None = None

        for cls_name in cls_list:
            files = self._class_to_models.get(cls_name, [])
            if not files:
                # Fallback: check prompt loader nodes with fp8 model files
                for nid, ninfo in self._prompt_nodes.items():
                    fp8_files = [
                        f for f in ninfo.get("model_files", [])
                        if "fp8" in f.lower() or "float8" in f.lower()
                    ]
                    if fp8_files and ("Loader" in ninfo.get("class_type", "") or "Model" in ninfo.get("class_type", "")):
                        files = fp8_files
                        if not resolved_node_id:
                            resolved_node_id = nid
                        break
            if files:
                detailed_parts.append(f"{', '.join(files)} ({cls_name})")
                if not resolved_node_id:
                    resolved_node_id = self._class_to_node.get(cls_name) or self._file_to_node.get(files[0])
            else:
                detailed_parts.append(cls_name)
                if not resolved_node_id:
                    resolved_node_id = self._class_to_node.get(cls_name)

        target_object = ", ".join(detailed_parts) if detailed_parts else raw_names or None
        if target_object:
            env["loaded_fp8_model_names"] = target_object
        node_label = self._format_node_label(resolved_node_id)
        return resolved_node_id, node_label, target_object

    def get_snapshot(
        self,
        custom_nodes_dirs: list[Path] | None = None,
        workflow_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return environment telemetry, env advisories, workflow node advisories, and live runtime alerts."""
        self._sync_from_prompt_server()
        if isinstance(workflow_data, dict):
            self.update_workflow(workflow_data)
        env = collect_environment(custom_nodes_dirs=custom_nodes_dirs)
        with self._lock:
            fp8_node_id, fp8_node_label, fp8_target = self._enrich_fp8_context_locked(env)
            rules_copy = list(self.rules)
            runtime_alerts = list(self._runtime_alerts)
            wf_target = workflow_data if isinstance(workflow_data, dict) else self._latest_workflow
        advisories = evaluate_environment(env, rules_copy)
        for item in advisories:
            if item.get("id") == "env_fp8_diffusion_loaded":
                if fp8_node_id:
                    item["node_id"] = fp8_node_id
                if fp8_node_label:
                    item["node_label"] = fp8_node_label
                if fp8_target:
                    item["target_object"] = fp8_target
        workflow_advisories = evaluate_workflow(wf_target, env, rules_copy)
        return {
            "env": env,
            "advisories": advisories,
            "workflow_advisories": workflow_advisories,
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
