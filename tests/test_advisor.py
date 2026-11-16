"""Unit tests for ComfyUI-XPU-Advisor JSON rules and runtime observer."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from advisor import XPUAdvisor
from rules import evaluate_environment, load_rules, match_log_message


def _base_env(**overrides):
    env = {
        "platform": "win32",
        "python_version": "3.13.12",
        "torch_version": "2.13.0+xpu",
        "xpu_available": True,
        "device_name": "Intel(R) Arc(TM) Pro B70 Graphics",
        "total_vram_mb": 32768.0,
        "free_vram_mb": 20000.0,
        "free_vram_ratio": 0.61,
        "reserved_vram_mb": 1000.0,
        "dynamic_vram_enabled": True,
        "reserve_vram_gb": None,
        "attn_backend": "torch",
        "omnixpu_enable": "1",
        "omni_kernel_version": "0.2.0b3+torch213.bmg",
        "aimdo_xpu_version": "0.6.0",
        "kitchen_xpu_version": "0.2.41",
        "kitchen_xpu_available": True,
        "omnixpu_extension_present": True,
        "has_legacy_xpu_backend": False,
    }
    env.update(overrides)
    return env


class AdvisorRulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = load_rules()

    def test_healthy_environment_has_zero_advisories(self):
        findings = evaluate_environment(_base_env(), self.rules)
        self.assertEqual(findings, [])

    def test_stock_xpu_environment_flags_missing_omnixpu_and_dynamic_vram(self):
        env = _base_env(
            omnixpu_extension_present=False,
            omni_kernel_version=None,
            dynamic_vram_enabled=False,
        )
        ids = {f["id"] for f in evaluate_environment(env, self.rules)}
        self.assertIn("env_missing_omnixpu_extension", ids)
        self.assertIn("env_dynamic_vram_disabled", ids)
        self.assertNotIn("env_missing_omni_kernel", ids)

    def test_registry_without_binaries_flags_missing_kernel(self):
        env = _base_env(omnixpu_extension_present=True, omni_kernel_version=None)
        ids = {f["id"] for f in evaluate_environment(env, self.rules)}
        self.assertIn("env_missing_omni_kernel", ids)

    def test_oom_rule_precedence_without_dynamic_vram(self):
        env = _base_env(dynamic_vram_enabled=False)
        hits = match_log_message("RuntimeError: XPU out of memory", env, self.rules)
        self.assertEqual([h["id"] for h in hits], ["log_oom_no_dynamic_vram"])

    def test_oom_rule_precedence_with_dynamic_vram_without_reserve(self):
        env = _base_env(dynamic_vram_enabled=True, reserve_vram_gb=None)
        hits = match_log_message("Allocation on device 0 would exceed allowed memory", env, self.rules)
        self.assertEqual([h["id"] for h in hits], ["log_oom_low_reserve_vram"])

    def test_oom_rule_precedence_with_reserve(self):
        env = _base_env(dynamic_vram_enabled=True, reserve_vram_gb=4.0)
        hits = match_log_message("RuntimeError: XPU out of memory", env, self.rules)
        self.assertEqual([h["id"] for h in hits], ["log_oom_general"])

    def test_cuda_attention_and_omnixpu_fallback_rules(self):
        env = _base_env()
        h1 = match_log_message(
            "Attention backend 'comfy kitchen attention' is unavailable; using PyTorch attention.",
            env,
            self.rules,
        )
        h2 = match_log_message(
            "[OmniXPU] DynamicVRAM boundary trim conflict (test); falling back to stock ComfyUI loader",
            env,
            self.rules,
        )
        self.assertEqual([h["id"] for h in h1], ["log_cuda_attention_backend"])
        self.assertEqual([h["id"] for h in h2], ["log_omnixpu_fallback"])

    def test_advisor_cooldown_and_websocket_emission(self):
        now = [100.0]
        events = []
        advisor = XPUAdvisor(
            emitter=lambda name, payload: events.append((name, payload)),
            clock=lambda: now[0],
        )
        env = _base_env(dynamic_vram_enabled=False)
        first = advisor.process_log_message("XPU out of memory", env_override=env)
        second = advisor.process_log_message("XPU out of memory", env_override=env)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)
        now[0] = 200.0
        third = advisor.process_log_message("XPU out of memory", env_override=env)
        self.assertEqual(len(third), 1)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0][0], "xpu_advisor.alert")


if __name__ == "__main__":
    unittest.main()
