import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const GUARD_KEY = "__xpu_advisor_initialized__";

const SEVERITY_COLORS = {
    error: { bg: "#3b171b", border: "#d9534f", pill: "#d9534f", text: "#ffd6d6" },
    warn: { bg: "#3a2b10", border: "#f0ad4e", pill: "#f0ad4e", text: "#ffe8bd" },
    info: { bg: "#13293d", border: "#4da3ff", pill: "#4da3ff", text: "#d6ebff" },
};

function apiFetch(path, options = {}) {
    if (api && typeof api.fetchApi === "function") {
        return api.fetchApi(path, options);
    }
    return fetch(path, options);
}

function pushToast(item) {
    const toast = app?.extensionManager?.toast;
    if (!toast) {
        return;
    }
    const severity = item.severity || "info";
    const summary = item.title || "XPU Advisor";
    const ctxParts = [];
    if (item.node_label) {
        ctxParts.push(`Node ${item.node_label}`);
    }
    if (item.target_object) {
        ctxParts.push(`Model ${item.target_object}`);
    }
    const ctxPrefix = ctxParts.length > 0 ? `[${ctxParts.join(" | ")}] ` : "";
    const detail = `${ctxPrefix}${item.tip || ""}`;
    const life = severity === "error" ? 16000 : severity === "warn" ? 12000 : 8000;

    if (typeof toast.add === "function") {
        toast.add({ severity, summary, detail, life });
    } else if (typeof toast.addAlert === "function") {
        toast.addAlert(`${summary}: ${detail}`);
    }
}

function focusNodeOnCanvas(rawNodeId, modal) {
    if (!rawNodeId || !app?.graph || !app?.canvas) {
        return false;
    }
    const strId = String(rawNodeId);
    let node =
        app.graph.getNodeById(strId) ||
        app.graph.getNodeById(Number(strId));
    if (!node && strId.includes(":")) {
        const rootId = strId.split(":")[0];
        node =
            app.graph.getNodeById(rootId) ||
            app.graph.getNodeById(Number(rootId));
    }
    if (!node) {
        return false;
    }
    if (typeof app.canvas.selectNode === "function") {
        app.canvas.selectNode(node, false);
    }
    if (typeof app.canvas.centerOnNode === "function") {
        app.canvas.centerOnNode(node);
    }
    modal?.close();
    return true;
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
                title: "XPU Advisor: rules.json reloaded",
                tip: `Loaded ${snap.rules_count} active rules.`,
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
        const sharedText = env.shared_vram_mb != null ? ` | Shared: ${env.shared_vram_mb} MB` : "";
        summaryBox.appendChild(
            el(
                "div",
                { color: "#b9c0ce" },
                `PyTorch: ${env.torch_version || "N/A"} | Python: ${env.python_version || "N/A"} | ${vramText}${sharedText}`
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

                if (item.node_label || item.target_object) {
                    const metaRow = el("div", {
                        display: "flex",
                        flexWrap: "wrap",
                        gap: "6px",
                        marginBottom: "6px",
                        fontSize: "11px",
                    });
                    if (item.node_label) {
                        const nodeChip = el(
                            item.node_id ? "button" : "span",
                            {
                                background: "rgba(255, 255, 255, 0.08)",
                                border: "1px solid rgba(255, 255, 255, 0.18)",
                                color: "#e6e8ee",
                                borderRadius: "4px",
                                padding: "2px 7px",
                                fontSize: "11px",
                                cursor: item.node_id ? "pointer" : "default",
                            },
                            `Node: ${item.node_label}`
                        );
                        if (item.node_id) {
                            nodeChip.title = "Click to locate node on canvas";
                            nodeChip.addEventListener("click", () => {
                                focusNodeOnCanvas(item.node_id, this);
                            });
                        }
                        metaRow.appendChild(nodeChip);
                    }
                    if (item.target_object) {
                        const objChip = el(
                            "span",
                            {
                                background: "rgba(255, 255, 255, 0.08)",
                                border: "1px solid rgba(255, 255, 255, 0.18)",
                                color: "#d6ebff",
                                borderRadius: "4px",
                                padding: "2px 7px",
                                fontSize: "11px",
                                fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace",
                            },
                            `Model / Object: ${item.target_object}`
                        );
                        metaRow.appendChild(objChip);
                    }
                    card.appendChild(metaRow);
                }

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

    if (app?.menu?.settingsGroup?.element) {
        const btn = el("button", {
            cursor: "pointer",
        }, "XPU Advisor");
        btn.className = "comfyui-button comfyui-menu-mobile-collapse";
        btn.title = "ComfyUI XPU Advisor — live Intel Arc hints & alerts";
        btn.addEventListener("click", () => modal.open());
        app.menu.settingsGroup.element.before(btn);
        return (snap) => {
            const text = labelFor(snap);
            btn.title = text;
            btn.textContent = text;
        };
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

        const seenEnvToasts = new Set();

        const syncStatus = async () => {
            try {
                const resp = await apiFetch("/xpu_advisor/status");
                if (!resp.ok) {
                    return;
                }
                const snap = await resp.json();
                latestSnap = snap;
                updateBadge(snap);
                for (const item of snap.advisories || []) {
                    if (item.toast && !seenEnvToasts.has(item.id)) {
                        seenEnvToasts.add(item.id);
                        pushToast(item);
                    }
                }
                if (modal.overlay && modal.overlay.style.display === "flex") {
                    modal.render(snap);
                }
            } catch (err) {
                console.warn("[XPUAdvisor] Status sync failed:", err);
            }
        };

        api.addEventListener("execution_success", () => {
            syncStatus();
        });
        api.addEventListener("execution_error", () => {
            syncStatus();
        });

        await syncStatus();
    },
});
