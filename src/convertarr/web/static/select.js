// Sonarr-style bulk-select for the Series and Movies grids.
// Toolbar "Select" button toggles select mode; clicking posters in that mode
// adds them to the selection; the sticky footer lets the user trigger
// /series/bulk-rescan or /movies/bulk-rescan with the current selection.

(function () {
    // scope -> { mode: bool, selected: Set<"iid:eid"> }
    const state = {};
    function ensure(scope) {
        if (!state[scope]) state[scope] = { mode: false, selected: new Set() };
        return state[scope];
    }
    function key(iid, eid) { return iid + ":" + eid; }

    function renderCount(scope) {
        const footer = document.querySelector(`[data-bulk-footer="${scope}"]`);
        if (!footer) return;
        footer.querySelector(".bulk-count").textContent = ensure(scope).selected.size;
    }

    function setMode(scope, on) {
        const s = ensure(scope);
        s.mode = on;
        if (!on) s.selected.clear();
        document.querySelectorAll(`.poster-grid[data-scope="${scope}"]`).forEach(g => {
            g.classList.toggle("select-mode", on);
            if (!on) g.querySelectorAll(".poster.selected").forEach(p => p.classList.remove("selected"));
        });
        const footer = document.querySelector(`[data-bulk-footer="${scope}"]`);
        if (footer) footer.hidden = !on;
        renderCount(scope);
    }

    function toggle(scope, posterEl) {
        const iid = posterEl.dataset.instanceId, eid = posterEl.dataset.entityId;
        if (!iid || !eid) return;
        const s = ensure(scope);
        const k = key(iid, eid);
        if (s.selected.has(k)) {
            s.selected.delete(k);
            posterEl.classList.remove("selected");
        } else {
            s.selected.add(k);
            posterEl.classList.add("selected");
        }
        renderCount(scope);
    }

    function selectAll(scope) {
        const s = ensure(scope);
        document.querySelectorAll(`.poster-grid[data-scope="${scope}"] .poster`).forEach(p => {
            const iid = p.dataset.instanceId, eid = p.dataset.entityId;
            if (!iid || !eid) return;
            s.selected.add(key(iid, eid));
            p.classList.add("selected");
        });
        renderCount(scope);
    }

    function clearSelection(scope) {
        const s = ensure(scope);
        s.selected.clear();
        document.querySelectorAll(`.poster-grid[data-scope="${scope}"] .poster.selected`)
            .forEach(p => p.classList.remove("selected"));
        renderCount(scope);
    }

    async function bulkConvert(scope) {
        const s = ensure(scope);
        if (!s.selected.size) return;
        const items = [...s.selected].map(k => {
            const [instance_id, entity_id] = k.split(":").map(Number);
            return { instance_id, entity_id };
        });
        const url = scope === "series" ? "/series/bulk-rescan" : "/movies/bulk-rescan";
        const btn = document.querySelector(`[data-bulk-footer="${scope}"] [data-bulk-action="convert"]`);
        const picker = document.querySelector(`[data-bulk-footer="${scope}"] [data-bulk-workflow-picker]`);
        const originalLabel = btn.textContent;
        btn.disabled = true;
        btn.textContent = `Converting ${items.length}…`;
        try {
            const payload = { items };
            // Only set workflow_id when the picker is rendered (i.e. there's
            // more than one workflow and the user actually chose). With zero
            // workflows the Convert button is disabled, with exactly one the
            // server walks the matcher and picks it automatically.
            if (picker && picker.value) payload.workflow_id = parseInt(picker.value, 10);
            const r = await fetch(url, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            // Worker-mode 409 — paired-as-worker installs reject queueing
            // because the local worker loop is paused while paired. Show a
            // toast pointing at the host's UI rather than a generic alert.
            if (r.status === 409) {
                let body = {};
                try { body = await r.json(); } catch (e) {}
                const detail = body.detail || {};
                if (detail.code === "worker_mode" && typeof convertarrToast === "function") {
                    convertarrToast(
                        detail.message || "Worker mode — convert from the host's UI.",
                        {
                            type: "warning",
                            action: detail.host_url
                                ? { label: "Open host →", href: detail.host_url }
                                : undefined,
                        },
                    );
                    return;
                }
            }
            if (!r.ok) {
                alert(`Bulk convert failed: HTTP ${r.status}`);
                return;
            }
            const data = await r.json();
            // Land on /queue so the user sees the new jobs immediately.
            const params = new URLSearchParams({
                queued: String(data.queued || 0),
                skipped: String(data.skipped || 0),
                failed: String(data.failed || 0),
            });
            window.location = `/queue?${params.toString()}`;
        } catch (e) {
            alert(`Bulk convert failed: ${e}`);
        } finally {
            btn.disabled = false;
            btn.textContent = originalLabel;
        }
    }

    document.addEventListener("DOMContentLoaded", () => {
        document.querySelectorAll("[data-select-toggle]").forEach(btn => {
            btn.addEventListener("click", () => {
                const scope = btn.dataset.selectToggle;
                setMode(scope, !ensure(scope).mode);
            });
        });

        // One delegated click handler per .poster-grid so we can short-circuit
        // navigation only when select-mode is on for that grid's scope.
        document.querySelectorAll(".poster-grid").forEach(grid => {
            const scope = grid.dataset.scope;
            if (!scope) return;
            grid.addEventListener("click", e => {
                if (!ensure(scope).mode) return;
                const poster = e.target.closest(".poster");
                if (!poster) return;
                e.preventDefault();
                toggle(scope, poster);
            });
        });

        document.querySelectorAll(".bulk-footer").forEach(footer => {
            const scope = footer.dataset.bulkFooter;
            footer.addEventListener("click", e => {
                const t = e.target.closest("[data-bulk-action]");
                if (!t) return;
                const action = t.dataset.bulkAction;
                if (action === "select-all") selectAll(scope);
                else if (action === "clear") clearSelection(scope);
                else if (action === "cancel") setMode(scope, false);
                else if (action === "convert") bulkConvert(scope);
            });
        });
    });
})();
