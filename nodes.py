"""Optional workflow node to output XPU Advisor diagnostics as text."""

from __future__ import annotations

try:
    from .advisor import get_advisor
except ImportError:
    from advisor import get_advisor


class XPUAdvisorReport:
    CATEGORY = "XPU Advisor"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("report",)
    FUNCTION = "generate"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    def generate(self):
        snap = get_advisor().get_snapshot()
        env = snap["env"]
        lines = [
            "=== ComfyUI XPU Advisor ===",
            f"Device: {env.get('device_name') or 'N/A'} | XPU: {env.get('xpu_available')}",
            f"Python: {env.get('python_version')} | PyTorch: {env.get('torch_version')}",
            (
                f"VRAM: {env.get('free_vram_mb')} MB free / {env.get('total_vram_mb')} MB total "
                f"(DynamicVRAM={env.get('dynamic_vram_enabled')}, reserve_vram={env.get('reserve_vram_gb')})"
            ),
            f"OmniXPU kernel: {env.get('omni_kernel_version') or 'not installed'} | Attn: {env.get('attn_backend')}",
            "",
            "--- Environment Advisories ---",
        ]
        advisories = snap["advisories"]
        if not advisories:
            lines.append("No warnings or pending improvements.")
        else:
            for item in advisories:
                lines.append(f"[{item['severity'].upper()}] {item['title']}")
                if item.get("node_label") or item.get("target_object"):
                    ctx = " | ".join(
                        p
                        for p in (
                            f"Node: {item['node_label']}" if item.get("node_label") else "",
                            f"Model/Object: {item['target_object']}" if item.get("target_object") else "",
                        )
                        if p
                    )
                    lines.append(f"  {ctx}")
                lines.append(f"  Tip: {item['tip']}")

        alerts = snap["runtime_alerts"]
        if alerts:
            lines.append("")
            lines.append("--- Live Runtime Alerts ---")
            for alert in alerts[:10]:
                lines.append(f"[{alert['timestamp']}][{alert['severity'].upper()}] {alert['title']}")
                if alert.get("node_label") or alert.get("target_object"):
                    ctx = " | ".join(
                        p
                        for p in (
                            f"Node: {alert['node_label']}" if alert.get("node_label") else "",
                            f"Model/Object: {alert['target_object']}" if alert.get("target_object") else "",
                        )
                        if p
                    )
                    lines.append(f"  {ctx}")
                lines.append(f"  Tip: {alert['tip']}")

        return ("\n".join(lines),)
