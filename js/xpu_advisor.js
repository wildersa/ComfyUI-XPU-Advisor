import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const GUARD_KEY = "__xpu_advisor_initialized__";

const SEVERITY_COLORS = {
    error: { bg: "#3b171b", border: "#d9534f", pill: "#d9534f", text: "#ffd6d6" },
    warn: { bg: "#3a2b10", border: "#f0ad4e", pill: "#f0ad4e", text: "#ffe8bd" },
    info: { bg: "#13293d", border: "#4da3ff", pill: "#4da3ff", text: "#d6ebff" },
};

function apiFetch(path, options = {}) {
    const fetcher = api?.fetchApi ? api.fetchApi.bind(api) : fetch;
    return fetcher(path, options);
}

function pushToast(item) {
    const toast = app?.extensionManager?.toast;
    if (!toast) {
        return;
    }
    const severity = item.severity || "info";
    const summary = item.title || "XPU Advisor";
    const detail = item.tip || "";
    const life = severity === "error" ? 16000 : severity === "warn" ? 12000 : 8000;

    if (typeof toast.add === "function") {
        toast.add({ severity, summary, detail, life });
    } else if (typeof toast.addAlert === "function") {
        toast.addAlert(`${summary}: ${detail}`);
    }
}

function el(tag, style = {}, text = "") {
    const node = document.createElement(tag);
    Object.assign(node.style, style);
    if (text) {
        node.textContent = text;
    }
    return node;
}

class AdvisorModal {
    constructor() {
        this.overlay = null;
        this.body = null;
        this.onSnapshotChange = null;
    }

    close() {
        if (this.overlay) {
            this.overlay.style.display = "none";
        }
    }

    ensureDom() {
        if (this.overlay) {
            return;
        }
        const overlay = el("div", {
            position: "fixed",
            inset: "0",
            background: "rgba(0, 0, 0, 0.55)",
            zIndex: "9999",
            display: "none",
            alignItems: "center",
            justifyContent: "center",
            fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
        });
        overlay.addEventListener("click", (e) => {
            if (e.target === overlay) {
                this.close();
            }
        });

        const dialog = el("div", {
            width: "min(760px, 92vw)",
            maxHeight: "84vh",
            background: "#1c1e24",
            color: "#e6e8ee",
            border: "1px solid #353945",
            borderRadius: "10px",
            boxShadow: "0 18px 48px rgba(0, 0, 0, 0.55)",
            display: "flex",
            flexDirection: "column",
            overflow: "hidden",
        });

        const header = el("div", {
            padding: "12px 16px",
            borderBottom: "1px solid #2e323d",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: "8px",
        });
        header.appendChild(el("strong", { fontSize: "15px" }, "ComfyUI XPU Advisor"));

        const actions = el("div", { display: "flex", gap: "8px" });
        const makeBtn = (label, onClick) => {
            const btn = el("button", {
                padding: "5px 10px",
                borderRadius: "6px",
                border: "1px solid #444a57",
                background: "#2a2e38",
                color: "#e6e8ee",
                cursor: "pointer",
                fontSize: "12px",
            }, label);
            btn.addEventListener("click", onClick);
            return btn;
        };

        actions.appendChild(makeBtn("Refresh", () => this.refresh()));
        actions.appendChild(makeBtn("Reload JSON", () => this.reloadRules()));
        actions.appendChild(makeBtn("Clear Alerts", () => this.clearAlerts()));
        actions.appendChild(makeBtn("Close", () => this.close()));
        header.appendChild(actions);

        const body = el("div", {
            padding: "14px 16px",
            overflowY: "auto",
            display: "flex",
            flexDirection: "column",
            gap: "12px",
        });

        dialog.appendChild(header);
        dialog.appendChild(body);
        overlay.appendChild(dialog);
        document.body.appendChild(overlay);
        this.overlay = overlay;
        this.body = body;
    }

    async open() {
        this.ensureDom();
        this.overlay.style.display = "flex";
        await this.refresh();
    }

    async refresh() {
        try {
            const resp = await apiFetch("/xpu_advisor/status");
            if (resp.ok) {
                const snap = await resp.json();
                this.render(snap);
                this.onSnapshotChange?.(snap);
            }
        } catch (err) {
            console.warn("[XPUAdvisor] Refresh failed:", err);
        }
    }

    async reloadRules() {
        const resp = await apiFetch("/xpu_advisor/reload", { method: "POST" });
        if (resp.ok) {
            const snap = await resp.json();
            this.render(snap);
            this.onSnapshotChange?.(snap);
            pushToast({
                severity: "info",
                title: "XPU Advisor: rules.json recarregado",
                tip: `${snap.rules_count} regras ativas carregadas.`,
            });
        }
    }

    async clearAlerts() {
        const resp = await apiFetch("/xpu_advisor/clear", { method: "POST" });
        if (resp.ok) {
            const snap = await resp.json();
            this.render(snap);
            this.onSnapshotChange?.(snap);
        }
    }

    render(snap) {
        if (!this.body) {
            return;
        }
        this.body.replaceChildren();
        const env = snap.env || {};

        const summaryBox = el("div", {
            background: "#23262f",
            border: "1px solid #323746",
            borderRadius: "8px",
            padding: "10px 12px",
            fontSize: "12px",
            lineHeight: "1.6",
        });
        summaryBox.appendChild(
            el("div", { fontWeight: "600", marginBottom: "4px" }, `GPU: ${env.device_name || "No XPU detected"}`)
        );
        const vramText =
            env.total_vram_mb != null
                ? `${env.free_vram_mb} MB free / ${env.total_vram_mb} MB total`
                : "VRAM N/A";
        summaryBox.appendChild(
            el(
                "div",
                { color: "#b9c0ce" },
                `PyTorch: ${env.torch_version || "N/A"} | Python: ${env.python_version || "N/A"} | ${vramText}`
            )
        );
        summaryBox.appendChild(
            el(
                "div",
                { color: "#b9c0ce" },
                `DynamicVRAM: ${env.dynamic_vram_enabled ? "ON" : "OFF"} | --reserve-vram: ${
                    env.reserve_vram_gb != null ? `${env.reserve_vram_gb} GB` : "default"
                } | Attn: ${env.attn_backend || "torch"} | omni_xpu_kernel: ${
                    env.omni_kernel_version || "not installed"
                }`
            )
        );
        this.body.appendChild(summaryBox);

        const appendSection = (heading, items, emptyText) => {
            this.body.appendChild(el("div", { fontWeight: "600", fontSize: "13px" }, heading));
            if (!items || items.length === 0) {
                this.body.appendChild(el("div", { fontSize: "12px", color: "#8e96a6" }, emptyText));
                return;
            }
            for (const item of items) {
                const palette = SEVERITY_COLORS[item.severity] || SEVERITY_COLORS.info;
                const card = el("div", {
                    background: palette.bg,
                    border: `1px solid ${palette.border}`,
                    borderRadius: "8px",
                    padding: "9px 11px",
                    fontSize: "12px",
                });
                const titleRow = el("div", {
                    display: "flex",
                    alignItems: "center",
                    gap: "8px",
                    marginBottom: "4px",
                });
                const badge = el(
                    "span",
                    {
                        background: palette.pill,
                        color: "#111",
                        borderRadius: "4px",
                        padding: "1px 6px",
                        fontSize: "10px",
                        fontWeight: "700",
                        textTransform: "uppercase",
                    },
                    item.severity || "info"
                );
                titleRow.appendChild(badge);
                const prefix = item.timestamp ? `[${item.timestamp}] ` : "";
                titleRow.appendChild(el("strong", { color: palette.text }, `${prefix}${item.title}`));
                card.appendChild(titleRow);
                card.appendChild(el("div", { color: "#e6e8ee", lineHeight: "1.45" }, item.tip || ""));
                this.body.appendChild(card);
            }
        };

        appendSection("Environment & Configuration", snap.advisories, "Environment looks healthy.");
        appendSection("Live Runtime Alerts", snap.runtime_alerts, "No runtime issues detected yet.");
    }
}

async function mountBadge(modal) {
    const labelFor = (snap) => {
        const items = [...(snap?.advisories || []), ...(snap?.runtime_alerts || [])];
        const important = items.filter((i) => i.severity === "error" || i.severity === "warn");
        return important.length > 0 ? `XPU Advisor (${important.length})` : "XPU Advisor";
    };

    try {
        const { ComfyButton } = await import("../../scripts/ui/components/button.js");
        const button = new ComfyButton({
            icon: "chip",
            content: "XPU Advisor",
            tooltip: "ComfyUI XPU Advisor — live Intel Arc hints & alerts",
            action: () => modal.open(),
            classList: "comfyui-button comfyui-menu-mobile-collapse",
        });
        if (app?.menu?.settingsGroup?.element) {
            app.menu.settingsGroup.element.before(button.element);
            return (snap) => {
                button.element.title = labelFor(snap);
                const span = button.element.querySelector("span.comfyui-button-content");
                if (span) {
                    span.textContent = labelFor(snap);
                }
            };
        }
    } catch {
        // Fall back to lightweight floating badge on older frontends.
    }

    const pill = el("button", {
        position: "fixed",
        right: "14px",
        bottom: "14px",
        zIndex: "9000",
        padding: "6px 11px",
        borderRadius: "999px",
        border: "1px solid #3c4252",
        background: "#1c1e24",
        color: "#e6e8ee",
        cursor: "pointer",
        fontSize: "12px",
        boxShadow: "0 4px 16px rgba(0,0,0,0.4)",
    }, "XPU Advisor");
    pill.addEventListener("click", () => modal.open());
    document.body.appendChild(pill);
    return (snap) => {
        pill.textContent = labelFor(snap);
    };
}

app.registerExtension({
    name: "XPUAdvisor.LiveMonitor",
    async setup() {
        if (window[GUARD_KEY]) {
            return;
        }
        window[GUARD_KEY] = true;

        const modal = new AdvisorModal();
        const updateBadge = await mountBadge(modal);
        let latestSnap = null;
        modal.onSnapshotChange = (snap) => {
            latestSnap = snap;
            updateBadge(snap);
        };

        api.addEventListener("xpu_advisor.alert", (event) => {
            const alert = event?.detail;
            if (!alert) {
                return;
            }
            if (alert.toast) {
                pushToast(alert);
            }
            if (latestSnap) {
                latestSnap.runtime_alerts = [alert, ...(latestSnap.runtime_alerts || [])].slice(0, 50);
                updateBadge(latestSnap);
            }
            if (modal.overlay && modal.overlay.style.display === "flex") {
                modal.refresh();
            }
        });

        try {
            const resp = await apiFetch("/xpu_advisor/status");
            if (resp.ok) {
                const snap = await resp.json();
                latestSnap = snap;
                updateBadge(snap);
                for (const item of snap.advisories || []) {
                    if (item.toast) {
                        pushToast(item);
                    }
                }
            }
        } catch (err) {
            console.warn("[XPUAdvisor] Initial status fetch failed:", err);
        }
    },
});
