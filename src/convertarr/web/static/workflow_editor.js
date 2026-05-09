// Node-graph workflow editor. Renders the workflow as a vertical chain of
// blocks (trigger → conditions → target codecs) on a canvas. Conditions are
// drag-reorderable; everything else is fixed in place.
//
// State lives in a single JS object; every interaction mutates state and
// re-renders the canvas. The DOM is throwaway — the source of truth is the
// state — which keeps drag/reorder/edit logic from getting tangled.

(function () {
    const metaTag = document.getElementById("wf-meta");
    if (!metaTag) return;

    let META;
    try { META = JSON.parse(metaTag.textContent); }
    catch (e) { console.error("workflow editor meta parse failed", e); return; }

    // Initialize state from the server-provided workflow (edit) or sane defaults (new).
    const incoming = META.workflow;
    const state = incoming ? {
        id: incoming.id,
        name: incoming.name,
        priority: incoming.priority,
        enabled: !!incoming.enabled,
        conditions: (incoming.conditions || []).map(c => ({...c})),
        target_video_codec: incoming.target_video_codec || "hevc",
        target_audio_codec: incoming.target_audio_codec || "aac",
    } : {
        id: null,
        // Pre-fill with the server-suggested "Workflow N" so saving without
        // typing a name still works — the user can overwrite it any time.
        name: META.default_name || "",
        priority: 100,
        enabled: true,
        conditions: [],
        target_video_codec: "hevc",
        target_audio_codec: "aac",
    };

    // Sync top-of-page form to state.
    const nameEl = document.getElementById("wf-name");
    const priorityEl = document.getElementById("wf-priority");
    const enabledEl = document.getElementById("wf-enabled");
    nameEl.value = state.name;
    priorityEl.value = state.priority;
    enabledEl.checked = state.enabled;

    nameEl.addEventListener("input", () => state.name = nameEl.value);
    priorityEl.addEventListener("input", () => {
        const n = parseInt(priorityEl.value, 10);
        state.priority = isNaN(n) ? 100 : n;
    });
    enabledEl.addEventListener("change", () => state.enabled = enabledEl.checked);

    // ---- Helpers ----------------------------------------------------------

    const canvas = document.getElementById("wf-canvas");

    function fieldType(key) {
        const f = META.fields.find(f => f.key === key);
        return f ? f.type : "string";
    }
    function fieldSuggestions(key) {
        const f = META.fields.find(f => f.key === key);
        return (f && f.suggestions) || [];
    }
    function opsForType(type) {
        return META.ops.filter(o => o.applies_to.includes(type));
    }
    function defaultOpFor(type) {
        const list = opsForType(type);
        return list.length ? list[0].key : "equal";
    }

    function makeEl(tag, cls, text) {
        const el = document.createElement(tag);
        if (cls) el.className = cls;
        if (text != null) el.textContent = text;
        return el;
    }

    // ---- Block renderers --------------------------------------------------

    function renderTriggerBlock() {
        const block = makeEl("div", "wf-block wf-block-trigger");
        const head = makeEl("div", "wf-block-header");
        head.append(
            makeEl("span", "wf-block-icon", "▶"),
            makeEl("span", "wf-block-title", "When a media file is processed"),
        );
        block.appendChild(head);
        return block;
    }

    function renderConnectorBadge(idx, cond) {
        // First block: static "IF" badge — there's no prior clause to connect to.
        // Subsequent blocks: AND/OR dropdown that the user can change.
        if (idx === 0) {
            return makeEl("span", "wf-conn-badge wf-conn-if", "IF");
        }
        const sel = makeEl("select", "wf-conn-badge wf-conn-select");
        for (const [key, label] of [["and", "AND"], ["or", "OR"]]) {
            const opt = makeEl("option");
            opt.value = key;
            opt.textContent = label;
            if ((cond.connector || "and").toLowerCase() === key) opt.selected = true;
            sel.appendChild(opt);
        }
        // Style hook so AND/OR can be color-keyed in CSS.
        sel.classList.add("wf-conn-" + ((cond.connector || "and").toLowerCase()));
        sel.addEventListener("change", () => {
            cond.connector = sel.value;
            // Re-render to recolor the wire above this block + the badge.
            render();
        });
        return sel;
    }

    function _scalarValueAsList(v) {
        // Tolerate legacy/scalar inputs and normalize to a list of trimmed strings.
        if (Array.isArray(v)) return v.map(x => String(x).trim()).filter(Boolean);
        const s = String(v ?? "").trim();
        return s ? [s] : [];
    }

    function renderValueControl(cond, type) {
        // Numbers stay scalar (free-entry input). Always store the typed
        // value as a one-element list so the wire format is uniform —
        // matcher reads value[0] for numbers.
        if (type === "number") {
            const inp = makeEl("input", "wf-input wf-input-value");
            inp.type = "number";
            inp.placeholder = "e.g. 1080";
            const current = _scalarValueAsList(cond.value);
            inp.value = current[0] || "";
            inp.addEventListener("input", () => {
                cond.value = inp.value ? [inp.value] : [];
            });
            return inp;
        }
        // String fields get a multi-select. The trigger button shows the
        // selected chips (or a placeholder); clicking opens a popover with
        // a checkbox per suggestion.
        return renderMultiSelect(cond);
    }

    function renderMultiSelect(cond) {
        const wrap = makeEl("div", "wf-multi");
        const trigger = makeEl("button", "wf-multi-trigger");
        trigger.type = "button";
        const chevron = makeEl("span", "wf-multi-chevron", "▾");
        const chipsHost = makeEl("span", "wf-multi-chips");
        trigger.append(chipsHost, chevron);

        const popover = makeEl("div", "wf-multi-popover");
        // Visibility is controlled via the `is-open` class on the wrap so
        // the popover can transition opacity/transform. (Used to toggle the
        // `hidden` attribute, but display:none can't be transitioned.)

        const suggestions = fieldSuggestions(cond.field);

        function getSelected() { return _scalarValueAsList(cond.value); }

        function repaintTrigger() {
            const selected = getSelected();
            chipsHost.innerHTML = "";
            if (!selected.length) {
                const ph = makeEl("span", "wf-multi-placeholder", "Select values…");
                chipsHost.appendChild(ph);
                return;
            }
            for (const v of selected) {
                const chip = makeEl("span", "wf-multi-chip");
                chip.textContent = v;
                const x = makeEl("span", "wf-multi-chip-x", "×");
                x.title = "Remove";
                x.addEventListener("click", (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    cond.value = getSelected().filter(s => s !== v);
                    repaintTrigger();
                    repaintCheckboxes();
                });
                chip.appendChild(x);
                chipsHost.appendChild(chip);
            }
        }

        function repaintCheckboxes() {
            const selected = new Set(getSelected());
            popover.querySelectorAll("input[type=checkbox]").forEach(cb => {
                cb.checked = selected.has(cb.value);
            });
        }

        // Build the popover contents once. Each row toggles its value in
        // cond.value (preserving existing order is tidy but not load-bearing).
        for (const v of suggestions) {
            const row = makeEl("label", "wf-multi-option");
            const cb = makeEl("input");
            cb.type = "checkbox";
            cb.value = v;
            cb.addEventListener("change", () => {
                const current = new Set(getSelected());
                if (cb.checked) current.add(v);
                else current.delete(v);
                // Preserve suggestion ordering for stable display.
                cond.value = suggestions.filter(s => current.has(s));
                repaintTrigger();
            });
            const span = makeEl("span");
            span.textContent = v;
            row.append(cb, span);
            popover.appendChild(row);
        }

        function open() {
            // Close any other open popover first — only one at a time keeps
            // the canvas readable when the user is editing several rows.
            document.querySelectorAll(".wf-multi.is-open").forEach(el => {
                if (el !== wrap) el.classList.remove("is-open");
            });
            wrap.classList.add("is-open");
        }
        function close() {
            wrap.classList.remove("is-open");
        }
        trigger.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            if (wrap.classList.contains("is-open")) close(); else open();
        });
        // Close on outside click. Captured globally with a single listener
        // wired once (see _ensureMultiSelectGlobalListener below).
        wrap.dataset.wfMulti = "1";
        wrap._wfClose = close;

        wrap.append(trigger, popover);
        repaintTrigger();
        repaintCheckboxes();
        return wrap;
    }

    // One document-level listener, idempotent so the editor's full re-render
    // doesn't pile up handlers. Closes any open multi-select when the user
    // clicks anywhere outside it.
    function _ensureMultiSelectGlobalListener() {
        if (document._wfMultiListener) return;
        document._wfMultiListener = true;
        document.addEventListener("click", (e) => {
            document.querySelectorAll(".wf-multi.is-open").forEach(el => {
                if (!el.contains(e.target) && el._wfClose) el._wfClose();
            });
        });
    }
    _ensureMultiSelectGlobalListener();

    function renderConditionBlock(idx) {
        const cond = state.conditions[idx];
        const block = makeEl("div", "wf-block wf-block-condition");
        block.draggable = true;
        block.dataset.idx = String(idx);

        // Header: drag grip + IF/AND/OR badge + diamond icon + remove button
        const head = makeEl("div", "wf-block-header");
        const grip = makeEl("span", "wf-block-grip", "⋮⋮");
        grip.title = "Drag to reorder";
        head.append(
            grip,
            renderConnectorBadge(idx, cond),
            makeEl("span", "wf-block-icon", "◆"),
            makeEl("span", "wf-block-title", "Condition"),
        );
        const remove = makeEl("button", "wf-block-remove", "×");
        remove.type = "button";
        remove.title = "Remove condition";
        remove.addEventListener("click", () => {
            state.conditions.splice(idx, 1);
            render();
        });
        head.appendChild(remove);
        block.appendChild(head);

        // Body: field / op / value
        const body = makeEl("div", "wf-block-body");

        const fieldSel = makeEl("select", "wf-input wf-input-field");
        for (const f of META.fields) {
            const opt = makeEl("option");
            opt.value = f.key;
            opt.textContent = f.label;
            if (cond.field === f.key) opt.selected = true;
            fieldSel.appendChild(opt);
        }
        if (!cond.field) {
            cond.field = META.fields[0].key;
            fieldSel.value = cond.field;
        }
        fieldSel.addEventListener("change", () => {
            cond.field = fieldSel.value;
            // Reset op + value if the new type doesn't support the old op /
            // doesn't have the old value in its suggestion list. The whole
            // row re-renders so the value control swaps between input/select.
            const newType = fieldType(cond.field);
            if (!opsForType(newType).some(o => o.key === cond.op)) {
                cond.op = defaultOpFor(newType);
            }
            cond.value = [];
            render();
        });

        const opSel = makeEl("select", "wf-input wf-input-op");
        const currentType = fieldType(cond.field);
        for (const o of opsForType(currentType)) {
            const opt = makeEl("option");
            opt.value = o.key;
            opt.textContent = o.label;
            if (cond.op === o.key) opt.selected = true;
            opSel.appendChild(opt);
        }
        if (!cond.op || !opsForType(currentType).some(o => o.key === cond.op)) {
            cond.op = defaultOpFor(currentType);
            opSel.value = cond.op;
        }
        opSel.addEventListener("change", () => { cond.op = opSel.value; });

        body.append(fieldSel, opSel, renderValueControl(cond, currentType));
        block.appendChild(body);

        // Drag-reorder
        block.addEventListener("dragstart", (e) => {
            e.dataTransfer.effectAllowed = "move";
            e.dataTransfer.setData("text/wf-idx", String(idx));
            block.classList.add("wf-block-dragging");
        });
        block.addEventListener("dragend", () => block.classList.remove("wf-block-dragging"));

        return block;
    }

    function renderConditionDropZone(insertAt) {
        const dz = makeEl("div", "wf-drop-zone");
        dz.dataset.insertAt = String(insertAt);
        dz.addEventListener("dragover", (e) => {
            // Have to preventDefault to mark the zone as a valid drop target.
            e.preventDefault();
            e.dataTransfer.dropEffect = "move";
            dz.classList.add("wf-drop-zone-active");
        });
        dz.addEventListener("dragleave", () => dz.classList.remove("wf-drop-zone-active"));
        dz.addEventListener("drop", (e) => {
            e.preventDefault();
            dz.classList.remove("wf-drop-zone-active");
            const fromIdx = parseInt(e.dataTransfer.getData("text/wf-idx"), 10);
            if (isNaN(fromIdx)) return;
            let toIdx = parseInt(dz.dataset.insertAt, 10);
            // Splicing past the source needs to compensate for the removed item.
            const moved = state.conditions.splice(fromIdx, 1)[0];
            if (fromIdx < toIdx) toIdx -= 1;
            state.conditions.splice(toIdx, 0, moved);
            render();
        });
        return dz;
    }

    function renderAddConditionButton() {
        const btn = makeEl("button", "wf-add-block", "+ Add condition");
        btn.type = "button";
        btn.addEventListener("click", () => {
            const fkey = META.fields[0].key;
            state.conditions.push({
                field: fkey, op: defaultOpFor(fieldType(fkey)), value: "",
                connector: "and",
            });
            render();
        });
        return btn;
    }

    function renderTargetBlock(kind) {
        // kind = "video" | "audio"
        const block = makeEl("div", `wf-block wf-block-action wf-block-action-${kind}`);
        const head = makeEl("div", "wf-block-header");
        head.append(
            makeEl("span", "wf-block-icon", kind === "video" ? "▶" : "♪"),
            makeEl("span", "wf-block-title",
                kind === "video" ? "Then convert video to" : "Then convert audio to"),
        );
        block.appendChild(head);

        const body = makeEl("div", "wf-block-body");
        const sel = makeEl("select", "wf-input wf-input-target");
        const targets = kind === "video" ? META.video_targets : META.audio_targets;
        const stateKey = kind === "video" ? "target_video_codec" : "target_audio_codec";
        for (const t of targets) {
            const opt = makeEl("option");
            opt.value = t;
            opt.textContent = t;
            if (state[stateKey] === t) opt.selected = true;
            sel.appendChild(opt);
        }
        sel.addEventListener("change", () => {
            state[stateKey] = sel.value;
            // Re-render so the colored "copy" pill updates.
            render();
        });
        body.appendChild(sel);

        // Helper text — small italic note inside the block body.
        const note = makeEl("span", "wf-block-note");
        if (state[stateKey] === "copy") {
            note.textContent = "(stream-copy — leave this track alone)";
        } else {
            note.textContent = `(re-encode every non-${state[stateKey]} stream)`;
        }
        body.appendChild(note);
        block.appendChild(body);
        return block;
    }

    function renderConnector(flavor) {
        // flavor: "default" | "and" | "or". OR wires get a colored break +
        // a label so the visual matches what the matcher actually does
        // (sum-of-products evaluation).
        const c = makeEl("div", "wf-connector wf-connector-" + (flavor || "default"));
        if (flavor === "or") {
            c.appendChild(makeEl("span", "wf-connector-label", "OR"));
        }
        return c;
    }

    // ---- Top-level render -------------------------------------------------

    function render() {
        canvas.innerHTML = "";

        canvas.append(
            renderTriggerBlock(),
            renderConnector(),
        );

        // Conditions section: drop zone before each block, add-button at the
        // end, with a final drop zone for "drop to end". The wire ABOVE each
        // block takes that block's connector flavor so the OR break lands
        // visually in the right place.
        if (state.conditions.length === 0) {
            const empty = makeEl("div", "wf-empty-conditions",
                "No conditions — this workflow matches every file.");
            canvas.append(empty, renderConnector("default"));
        } else {
            for (let i = 0; i < state.conditions.length; i++) {
                canvas.append(renderConditionDropZone(i));
                canvas.append(renderConditionBlock(i));
                // The wire BETWEEN block i and block i+1 takes block i+1's
                // connector flavor (the relationship is "between them").
                if (i < state.conditions.length - 1) {
                    const nextConnector = (state.conditions[i + 1].connector || "and").toLowerCase();
                    canvas.append(renderConnector(nextConnector));
                }
            }
            canvas.append(renderConditionDropZone(state.conditions.length));
            canvas.append(renderConnector("default"));
        }

        canvas.append(renderAddConditionButton(), renderConnector("default"));

        canvas.append(
            renderTargetBlock("video"),
            renderConnector(),
            renderTargetBlock("audio"),
        );
    }

    render();

    // ---- Save -------------------------------------------------------------

    const saveBtn = document.getElementById("wf-save");
    saveBtn.addEventListener("click", async () => {
        if (!state.name.trim()) {
            alert("Workflow needs a name.");
            nameEl.focus();
            return;
        }
        // Drop conditions whose value list is empty — almost always means the
        // user added a row and forgot to pick anything. A condition with no
        // values would either match everything (no-op) or nothing (silent
        // filter) depending on the operator, neither helpful.
        const conditions = state.conditions
            .filter(c => {
                if (!c.field || !c.op) return false;
                const v = _scalarValueAsList(c.value);
                return v.length > 0;
            })
            .map(c => ({
                field: c.field, op: c.op,
                value: _scalarValueAsList(c.value),
                connector: (c.connector || "and").toLowerCase() === "or" ? "or" : "and",
            }));
        const payload = {
            name: state.name.trim(),
            priority: state.priority,
            enabled: state.enabled,
            conditions,
            target_video_codec: state.target_video_codec,
            target_audio_codec: state.target_audio_codec,
        };
        const url = state.id ? `/api/workflows/${state.id}` : "/api/workflows";
        saveBtn.disabled = true;
        try {
            const r = await fetch(url, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            if (!r.ok) {
                const text = await r.text();
                alert("Save failed: " + r.status + "\n" + text);
                return;
            }
            window.location.href = "/settings/workflows";
        } finally {
            saveBtn.disabled = false;
        }
    });
})();

// Toggle / delete actions are referenced from the list page, so they're
// global. Both reload the page after a successful update — simpler than
// patching the DOM in place and the page is cheap to render.
async function convertarrToggleWorkflow(id) {
    const r = await fetch(`/api/workflows/${id}/toggle`, { method: "POST" });
    if (!r.ok) {
        alert("Toggle failed: " + r.status);
        window.location.reload();
    } else {
        window.location.reload();
    }
}

async function convertarrDeleteWorkflow(id, name) {
    if (!confirm(`Delete workflow "${name}"?`)) return;
    const r = await fetch(`/api/workflows/${id}/delete`, { method: "POST" });
    if (r.ok) window.location.reload();
    else alert("Delete failed: " + r.status);
}
