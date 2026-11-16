# ComfyUI-XPU-Advisor

Lightweight, **100% pure-Python + JSON rule-based diagnostics advisor** for Intel Arc (`xpu`) setups in ComfyUI. Flags environment gaps, VRAM pressure / OOM causes, CUDA-only workflow incompatibilities, and extension fallbacks directly inside the ComfyUI web UI with actionable tips — no prebuilt binaries or `exec()`/`subprocess`.

---

## Features

- **Startup & Environment Check:** Detects missing XPU runtime, `torch` version mismatch (`2.13.0+xpu`), missing `--enable-dynamic-vram`, tight VRAM headroom, missing `ComfyUI-Intel-Arc-XPU` kernels, or conflicting legacy `comfyui-xpu-backend`.
- **Live Runtime Alert Watcher:** Listens to ComfyUI logs in real time via a thread-safe `logging.Handler` + WebSocket (`xpu_advisor.alert`) and pops up contextual toasts when errors happen (e.g. `XPU out of memory` -> tells you whether to add `--enable-dynamic-vram` or `--reserve-vram 2`/`4`; `comfy kitchen attention unavailable` -> tells you to switch `ModelAttentionBackend` to `pytorch attention`).
- **UI Panel (`XPU Advisor` button):** Top-bar button & drawer listing live GPU/VRAM metrics, configuration tips, runtime alert history, and a **Reload JSON** button to hot-reload `rules.json` without restarting ComfyUI.
- **Declarative JSON Rule Table ([`rules.json`](./rules.json)):** Add new `condition -> advisory` entries purely in JSON without touching Python or JS.

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
  "title": "Pico de memória causou OOM com DynamicVRAM",
  "tip": "Adicione --reserve-vram 2 (ou 4 em vídeo pesado) para guardar folga para ativações."
}
```
