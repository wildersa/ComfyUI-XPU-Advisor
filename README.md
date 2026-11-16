# ComfyUI-XPU-Advisor

Performance and diagnostics advisor for **Intel Arc (`xpu`)** setups in ComfyUI (Windows & Linux). Runs deterministic checks over your active environment, loaded weights, and runtime logs to flag misconfigured settings, slow emulation bottlenecks, Shared GPU Memory spills, OOM causes, and CUDA-only workflow incompatibilities directly in the ComfyUI UI — pure Python/JS, with zero prebuilt binaries or `exec()`/`subprocess`.

---

## Features

- **Performance & Misconfiguration Alerts:** Flags bottlenecks you can fix immediately — e.g., emulated FP8 diffusion models when faster `int8_convrot` weights exist, PyTorch SDPA instead of CUTE on Battlemage (`OMNI_ATTN_BACKEND=cute`), Windows WDDM Shared GPU Memory spills slowing down sampling, or `--disable-pinned-memory` blocking RAM staging.
- **Startup & Environment Checks:** Detects missing XPU runtime, Windows `torch==2.13.0+xpu` requirement mismatches, missing `--enable-dynamic-vram`, tight VRAM headroom, missing native XPU kernels (`omni_xpu_kernel`), or conflicting legacy `comfyui-xpu-backend`.
- **Live Runtime Log Watcher:** Listens to ComfyUI logs in real time via a thread-safe `logging.Handler` + WebSocket (`xpu_advisor.alert`) and shows contextual toasts when issues occur (e.g. `XPU out of memory` -> tells you whether to enable `--enable-dynamic-vram` or add `--reserve-vram 2`/`4`; `comfy kitchen attention unavailable` -> tells you to switch `ModelAttentionBackend` to `pytorch attention`).
- **UI Panel (`XPU Advisor` button):** Top-bar button & drawer showing live GPU/VRAM/Shared Memory metrics, optimization tips, runtime alert history, and hot-reload without restarting ComfyUI.
- **Extensible Deterministic Rules ([`rules.json`](./rules.json)):** Easy to grow over time by adding new `condition -> advisory` entries without changing Python or JS.

---

## Adding New Rules in `rules.json`

Each rule in `rules.json` supports:

- `"scope"`: `"env"` (evaluated against environment/VRAM snapshot) or `"log"` (matched by regex `"pattern"` against runtime log lines).
- `"exclusive_group"` *(optional)*: First matching rule in the group wins (useful for specific-to-general fallbacks like OOM causes).
- `"severity"`: `"error"`, `"warn"`, or `"info"`.
- `"toast"`: `true` to show an immediate popup toast in ComfyUI, `false` to display only inside the Advisor panel.
- `"when"`: Dictionary of field checks over the environment snapshot (supports literal equality, `is_null`, `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `prefix`, `not_prefix`, `contains`, `in`, `not_in`).

Example:

```json
{
  "id": "log_oom_low_reserve_vram",
  "scope": "log",
  "exclusive_group": "xpu_oom",
  "severity": "error",
  "toast": true,
  "cooldown_seconds": 30,
  "pattern": "XPU out of memory",
  "when": {
    "dynamic_vram_enabled": true,
    "reserve_vram_gb": { "is_null": true }
  },
  "title": "Memory spike caused OOM under DynamicVRAM",
  "tip": "Add --reserve-vram 2 (or 4 for heavy video) to leave headroom for activations."
}
```
