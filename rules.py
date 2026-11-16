"""Declarative JSON condition engine and environment snapshot collector."""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


DEFAULT_RULES_PATH = Path(__file__).with_name("rules.json")


class _SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:
        return "N/A"


def load_rules(rules_path: Path | None = None) -> list[dict[str, Any]]:
    """Load rules from rules.json and pre-compile any regex patterns."""
    path = Path(rules_path) if rules_path is not None else DEFAULT_RULES_PATH
    payload = json.loads(path.read_text(encoding="utf-8"))
    rules = payload.get("rules", [])
    compiled: list[dict[str, Any]] = []
    for raw in rules:
        rule = dict(raw)
        pattern = rule.get("pattern")
        if pattern:
            rule["_compiled_pattern"] = re.compile(pattern, re.IGNORECASE)
        compiled.append(rule)
    return compiled


def _package_version(dist_name: str) -> str | None:
    try:
        return importlib.metadata.version(dist_name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _detect_custom_nodes(custom_nodes_dirs: list[Path] | None = None) -> dict[str, bool]:
    dirs = list(custom_nodes_dirs or [])
    if not dirs:
        folder_paths = sys.modules.get("folder_paths")
        getter = getattr(folder_paths, "get_folder_paths", None)
        if callable(getter):
            try:
                dirs = [Path(p) for p in getter("custom_nodes")]
            except Exception:
                dirs = []
    if not dirs:
        dirs = [Path(__file__).resolve().parent.parent]

    omnixpu_present = False
    legacy_present = False
    for base in dirs:
        if not base.is_dir():
            continue
        if (base / "ComfyUI-Intel-Arc-XPU").exists() or (base / "ComfyUI-OmniXPU").exists():
            omnixpu_present = True
        if (base / "comfyui-xpu-backend").exists():
            legacy_present = True

    if "ComfyUI-OmniXPU" in sys.modules:
        omnixpu_present = True
    return {
        "omnixpu_extension_present": omnixpu_present,
        "has_legacy_xpu_backend": legacy_present,
    }


def collect_environment(custom_nodes_dirs: list[Path] | None = None) -> dict[str, Any]:
    """Inspect current Python, PyTorch XPU, CLI flags, VRAM, and packages safely."""
    py_ver = f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}"
    snapshot: dict[str, Any] = {
        "platform": sys.platform,
        "python_version": py_ver,
        "torch_version": None,
        "xpu_available": False,
        "device_name": None,
        "total_vram_mb": None,
        "free_vram_mb": None,
        "free_vram_ratio": None,
        "reserved_vram_mb": None,
        "dynamic_vram_enabled": False,
        "reserve_vram_gb": None,
        "attn_backend": os.environ.get("OMNI_ATTN_BACKEND", "torch"),
        "omnixpu_enable": os.environ.get("OMNIXPU_ENABLE", "1"),
        "omni_kernel_version": _package_version("omni-xpu-kernel"),
        "aimdo_xpu_version": _package_version("comfy-aimdo-xpu-runtime"),
        "kitchen_xpu_version": _package_version("comfy-kitchen-xpu-runtime"),
        "kitchen_xpu_available": False,
    }

    snapshot.update(_detect_custom_nodes(custom_nodes_dirs))

    cli_args = sys.modules.get("comfy.cli_args")
    args = getattr(cli_args, "args", None)
    if args is not None:
        snapshot["dynamic_vram_enabled"] = bool(getattr(args, "enable_dynamic_vram", False))
        raw_reserve = getattr(args, "reserve_vram", None)
        snapshot["reserve_vram_gb"] = float(raw_reserve) if raw_reserve is not None else None

    torch_mod = sys.modules.get("torch")
    if torch_mod is None:
        try:
            import torch as torch_mod  # noqa: WPS433
        except Exception:
            torch_mod = None

    if torch_mod is not None:
        snapshot["torch_version"] = str(getattr(torch_mod, "__version__", ""))
        xpu = getattr(torch_mod, "xpu", None)
        try:
            xpu_ok = bool(xpu is not None and xpu.is_available())
        except Exception:
            xpu_ok = False
        snapshot["xpu_available"] = xpu_ok
        if xpu_ok:
            try:
                idx = xpu.current_device()
                snapshot["device_name"] = str(xpu.get_device_name(idx))
                free_b, total_b = xpu.mem_get_info(idx)
                snapshot["free_vram_mb"] = round(free_b / (1024 * 1024), 1)
                snapshot["total_vram_mb"] = round(total_b / (1024 * 1024), 1)
                if total_b > 0:
                    snapshot["free_vram_ratio"] = round(free_b / total_b, 4)
                snapshot["reserved_vram_mb"] = round(xpu.memory_reserved(idx) / (1024 * 1024), 1)
            except Exception:
                pass

    kitchen_mod = sys.modules.get("comfy_kitchen")
    if kitchen_mod is not None and hasattr(kitchen_mod, "list_backends"):
        try:
            xpu_backend = kitchen_mod.list_backends().get("xpu", {})
            snapshot["kitchen_xpu_available"] = bool(
                xpu_backend.get("available") and not xpu_backend.get("disabled")
            )
        except Exception:
            pass

    return snapshot


def _matches_condition(actual: Any, expected: Any) -> bool:
    if not isinstance(expected, dict):
        return actual == expected

    for op, target in expected.items():
        if op == "is_null":
            if (actual is None) is not bool(target):
                return False
        elif op == "eq":
            if actual != target:
                return False
        elif op == "ne":
            if actual == target:
                return False
        elif op == "lt":
            if actual is None or not (actual < target):
                return False
        elif op == "lte":
            if actual is None or not (actual <= target):
                return False
        elif op == "gt":
            if actual is None or not (actual > target):
                return False
        elif op == "gte":
            if actual is None or not (actual >= target):
                return False
        elif op == "prefix":
            if not isinstance(actual, str) or not actual.startswith(str(target)):
                return False
        elif op == "not_prefix":
            if actual is not None and isinstance(actual, str) and actual.startswith(str(target)):
                return False
        elif op == "contains":
            if actual is None or str(target) not in str(actual):
                return False
        elif op == "in":
            if actual not in target:
                return False
        elif op == "not_in":
            if actual in target:
                return False
        else:
            return False
    return True


def conditions_match(when: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    """Return True if every field condition in ``when`` holds on ``snapshot``."""
    return all(_matches_condition(snapshot.get(field), spec) for field, spec in when.items())


def render_finding(rule: dict[str, Any], snapshot: dict[str, Any], raw_message: str = "") -> dict[str, Any]:
    """Materialize a UI-friendly advisory dict from a rule and snapshot."""
    fmt_ctx = _SafeFormatDict({
        k: ("N/A" if v is None else v) for k, v in snapshot.items()
    })
    fmt_ctx["log_message"] = raw_message
    title = str(rule.get("title", rule.get("id", "XPU Advisory"))).format_map(fmt_ctx)
    tip = str(rule.get("tip", "")).format_map(fmt_ctx)
    return {
        "id": rule["id"],
        "scope": rule.get("scope", "env"),
        "severity": rule.get("severity", "info"),
        "toast": bool(rule.get("toast", False)),
        "title": title,
        "tip": tip,
    }


def evaluate_environment(
    snapshot: dict[str, Any],
    rules: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Evaluate all ``scope == 'env'`` rules respecting exclusive_group precedence."""
    findings: list[dict[str, Any]] = []
    seen_groups: set[str] = set()
    for rule in rules:
        if rule.get("scope") != "env":
            continue
        group = rule.get("exclusive_group")
        if group and group in seen_groups:
            continue
        if conditions_match(rule.get("when", {}), snapshot):
            if group:
                seen_groups.add(group)
            findings.append(render_finding(rule, snapshot))
    return findings


def match_log_message(
    message: str,
    snapshot: dict[str, Any],
    rules: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Evaluate all ``scope == 'log'`` rules against a log line."""
    findings: list[dict[str, Any]] = []
    seen_groups: set[str] = set()
    for rule in rules:
        if rule.get("scope") != "log":
            continue
        group = rule.get("exclusive_group")
        if group and group in seen_groups:
            continue
        compiled = rule.get("_compiled_pattern")
        if compiled is None or not compiled.search(message):
            continue
        if conditions_match(rule.get("when", {}), snapshot):
            if group:
                seen_groups.add(group)
            finding = render_finding(rule, snapshot, raw_message=message)
            finding["cooldown_seconds"] = float(rule.get("cooldown_seconds", 60))
            finding["matched_log"] = message.strip()[:400]
            findings.append(finding)
    return findings
