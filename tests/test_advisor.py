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
        "shared_vram_mb": 120.0,
        "dynamic_vram_enabled": True,
        "reserve_vram_gb": None,
        "disable_pinned_memory": False,
        "attn_backend": "cute",
        "omnixpu_enable": "1",
        "omni_kernel_version": "0.2.0b3+torch213.bmg",
        "omni_xpu_target": "bmg",
        "aimdo_xpu_version": "0.6.0",
        "kitchen_xpu_version": "0.2.41",
        "kitchen_xpu_available": True,
        "omnixpu_extension_present": True,
        "has_legacy_xpu_backend": False,
        "has_loaded_fp8_diffusion": False,
        "loaded_fp8_model_names": "",
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
            kitchen_xpu_available=False,
            dynamic_vram_enabled=False,
        )
        findings = evaluate_environment(env, self.rules)
        by_id = {f["id"]: f for f in findings}
        self.assertIn("env_missing_omnixpu_extension", by_id)
        self.assertTrue(by_id["env_missing_omnixpu_extension"]["toast"])
        self.assertIn("env_dynamic_vram_disabled", by_id)
        self.assertNotIn("env_missing_omni_kernel", by_id)
        self.assertNotIn("env_kitchen_xpu_unavailable", by_id)

    def test_missing_kitchen_xpu_fires_when_kernel_exists(self):
        env = _base_env(kitchen_xpu_available=False)
        findings = evaluate_environment(env, self.rules)
        by_id = {f["id"]: f for f in findings}
        self.assertIn("env_kitchen_xpu_unavailable", by_id)
        self.assertTrue(by_id["env_kitchen_xpu_unavailable"]["toast"])

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


    def test_fp8_diffusion_detection_and_log_rule(self):
        env = _base_env(
            has_loaded_fp8_diffusion=True,
            loaded_fp8_model_names="MiniMaxH3",
        )
        findings = evaluate_environment(env, self.rules)
        fp8_items = [f for f in findings if f["id"] == "env_fp8_diffusion_loaded"]
        self.assertEqual(len(fp8_items), 1)
        self.assertIn("MiniMaxH3", fp8_items[0]["title"])
        self.assertFalse(fp8_items[0]["toast"])

        hits = match_log_message("Selected mixed precision dtype float8_e4m3fn", _base_env(), self.rules)
        self.assertEqual([h["id"] for h in hits], ["log_fp8_emulated_ops"])

    def test_shared_vram_spill_pinned_memory_and_bmg_torch_rules(self):
        no_dyn_ids = {
            f["id"]
            for f in evaluate_environment(
                _base_env(dynamic_vram_enabled=False, shared_vram_mb=1500.0), self.rules
            )
        }
        self.assertIn("env_shared_vram_spill_no_dyn", no_dyn_ids)
        self.assertNotIn("env_shared_vram_spill_dyn", no_dyn_ids)

        dyn_ids = {
            f["id"]
            for f in evaluate_environment(
                _base_env(
                    shared_vram_mb=900.0,
                    disable_pinned_memory=True,
                    attn_backend="torch",
                    omni_xpu_target="bmg",
                ),
                self.rules,
            )
        }
        self.assertIn("env_shared_vram_spill_dyn", dyn_ids)
        self.assertIn("env_disable_pinned_memory", dyn_ids)
        self.assertIn("env_bmg_attn_torch", dyn_ids)

    def test_linux_xpu_environment_rules_work_without_windows_counters(self):
        linux_env = _base_env(
            platform="linux",
            torch_version="2.12.1+xpu",
            shared_vram_mb=None,
            attn_backend="torch",
            dynamic_vram_enabled=False,
        )
        ids = {f["id"] for f in evaluate_environment(linux_env, self.rules)}
        self.assertIn("env_dynamic_vram_disabled", ids)
        self.assertIn("env_bmg_attn_torch", ids)
        self.assertNotIn("env_torch_version_mismatch", ids)
        self.assertNotIn("env_shared_vram_spill_no_dyn", ids)


if __name__ == "__main__":
    unittest.main()
