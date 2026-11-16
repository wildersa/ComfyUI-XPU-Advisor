"""ComfyUI XPU Advisor — live Intel Arc environment and runtime diagnostic alerts."""

from __future__ import annotations

import logging
import sys

try:
    from .advisor import get_advisor
    from .nodes import XPUAdvisorReport
except ImportError:
    from advisor import get_advisor
    from nodes import XPUAdvisorReport

log = logging.getLogger("ComfyUI-XPU-Advisor")

NODE_CLASS_MAPPINGS = {
    "XPUAdvisorReport": XPUAdvisorReport,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "XPUAdvisorReport": "XPU Advisor Report",
}
WEB_DIRECTORY = "./js"


def register_routes(advisor=None) -> bool:
    """Register REST endpoints on PromptServer if running inside ComfyUI."""
    target = advisor or get_advisor()
    try:
        from aiohttp import web
        import server

        prompt_server = getattr(server, "PromptServer", None)
        instance = getattr(prompt_server, "instance", None)
        routes = getattr(instance, "routes", None)
        if routes is None:
            return False

        add_prompt_handler = getattr(instance, "add_on_prompt_handler", None)
        if callable(add_prompt_handler) and not getattr(instance, "_xpu_advisor_prompt_hooked", False):
            def _on_prompt(json_data):
                try:
                    target.record_prompt(json_data)
                except Exception:
                    pass
                return json_data

            add_prompt_handler(_on_prompt)
            setattr(instance, "_xpu_advisor_prompt_hooked", True)

        @routes.get("/xpu_advisor/status")
        async def xpu_advisor_status(request):
            return web.json_response(target.get_snapshot())

        @routes.post("/xpu_advisor/status")
        async def xpu_advisor_status_post(request):
            wf_data = None
            try:
                body = await request.json()
                if isinstance(body, dict):
                    wf_data = body.get("workflow") if isinstance(body.get("workflow"), dict) else body
            except Exception:
                wf_data = None
            return web.json_response(target.get_snapshot(workflow_data=wf_data))

        @routes.post("/xpu_advisor/clear")
        async def xpu_advisor_clear(request):
            target.clear_runtime_alerts()
            return web.json_response(target.get_snapshot())

        @routes.post("/xpu_advisor/reload")
        async def xpu_advisor_reload(request):
            count = target.reload_rules()
            return web.json_response({"reloaded": count, **target.get_snapshot()})

        return True
    except Exception:
        return False


def _start():
    advisor = get_advisor()
    advisor.attach_logger()
    register_routes(advisor)
    if "pytest" not in sys.modules:
        snap = advisor.get_snapshot()
        advisories = snap["advisories"]
        log.info(
            "[XPUAdvisor] Loaded %d JSON rules (%d startup advisories)",
            snap["rules_count"],
            len(advisories),
        )


_start()

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
