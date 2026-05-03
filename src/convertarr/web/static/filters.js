// Custom-filter modal — fetches /api/filters?scope=..., renders the list and
// the row-builder editor.

let _filterMeta = {};  // { scope: { fields, ops } }

async function _fetchFilters(scope) {
    const r = await fetch("/api/filters?scope=" + encodeURIComponent(scope));
    if (!r.ok) throw new Error("Failed to load filters: " + r.status);
    const data = await r.json();
    _filterMeta[scope] = { fields: data.fields, ops: data.ops };
    return data;
}

async function convertarrOpenFilterBuilder(scope) {
    const dlg = document.getElementById("filter-builder-" + scope);
    const list = document.getElementById("filter-list-" + scope);
    list.innerHTML = '<div class="dim">Loading…</div>';
    dlg.showModal();

    let data;
    try { data = await _fetchFilters(scope); }
    catch (e) { list.innerHTML = '<div class="error-card card">' + e.message + '</div>'; return; }

    if (!data.custom.length) {
        list.innerHTML = '<div class="dim" style="padding: 1rem 0">No custom filters yet.</div>';
        return;
    }
    list.innerHTML = "";
    for (const f of data.custom) {
        const row = document.createElement("div");
        row.className = "custom-filter-row";
        const summary = (f.clauses || []).map(c => `${c.field} ${c.op} ${c.value}`).join(" AND ") || "(no clauses)";
        row.innerHTML = `
            <div>
                <strong>${escapeHtml(f.name)}</strong>
                <div class="dim" style="font-size:0.78rem; margin-top:0.2rem">${escapeHtml(summary)}</div>
            </div>
            <div class="row-actions">
                <button type="button" class="ghost" data-id="${f.id}" data-action="edit">Edit</button>
                <button type="button" class="danger" data-id="${f.id}" data-action="delete">Delete</button>
            </div>
        `;
        row.querySelector('[data-action="edit"]').addEventListener("click", () => {
            convertarrShowFilterEditor(scope, f);
        });
        row.querySelector('[data-action="delete"]').addEventListener("click", async () => {
            if (!confirm(`Delete filter "${f.name}"?`)) return;
            await fetch(`/api/filters/${f.id}/delete`, { method: "POST" });
            convertarrOpenFilterBuilder(scope);
        });
        list.appendChild(row);
    }
}

function convertarrShowFilterEditor(scope, existing) {
    const meta = _filterMeta[scope];
    if (!meta) { _fetchFilters(scope).then(() => convertarrShowFilterEditor(scope, existing)); return; }

    const dlg = document.getElementById("filter-editor-" + scope);
    document.getElementById("filter-builder-" + scope).close();

    const title = existing ? "Edit Custom Filter" : "Add Custom Filter";
    document.getElementById("filter-editor-title-" + scope).textContent = title;

    const form = document.getElementById("filter-editor-form-" + scope);
    form.dataset.editingId = existing ? existing.id : "";
    document.getElementById("filter-name-" + scope).value = existing ? existing.name : "";

    const clausesEl = document.getElementById("filter-clauses-" + scope);
    clausesEl.innerHTML = "";
    const initial = (existing && existing.clauses && existing.clauses.length) ? existing.clauses : [{}];
    for (const c of initial) _appendClauseRow(scope, c);

    dlg.showModal();
}

function _appendClauseRow(scope, clause) {
    const meta = _filterMeta[scope];
    const wrap = document.getElementById("filter-clauses-" + scope);
    const row = document.createElement("div");
    row.className = "filter-clause-row";

    const fieldSel = document.createElement("select");
    fieldSel.className = "clause-field";
    for (const f of meta.fields) {
        const o = document.createElement("option");
        o.value = f.key; o.textContent = f.label; o.dataset.type = f.type;
        o.dataset.suggestions = JSON.stringify(f.suggestions || []);
        if (clause.field === f.key) o.selected = true;
        fieldSel.appendChild(o);
    }

    const opSel = document.createElement("select");
    opSel.className = "clause-op";

    const valIn = document.createElement("input");
    valIn.type = "text"; valIn.className = "clause-value";
    valIn.value = clause.value || "";

    // Per-row datalist so each clause has its own suggestion set keyed off the
    // selected field. Browsers show this as a dropdown when the input is
    // focused while still allowing free-text entry.
    const listId = "clause-vals-" + Math.random().toString(36).slice(2, 9);
    const dataList = document.createElement("datalist");
    dataList.id = listId;
    valIn.setAttribute("list", listId);

    function refreshOps() {
        const opt = fieldSel.options[fieldSel.selectedIndex];
        const type = opt.dataset.type;
        opSel.innerHTML = "";
        for (const o of meta.ops) {
            if (!o.applies_to.includes(type)) continue;
            const optEl = document.createElement("option");
            optEl.value = o.key; optEl.textContent = o.label;
            if (clause.op === o.key) optEl.selected = true;
            opSel.appendChild(optEl);
        }
        if (type === "bool") { valIn.placeholder = "true | false"; }
        else if (type === "number") { valIn.placeholder = "e.g. 2020"; }
        else { valIn.placeholder = "value"; }

        // Refresh suggestion list for the current field.
        let suggestions = [];
        try { suggestions = JSON.parse(opt.dataset.suggestions || "[]"); } catch (e) {}
        dataList.innerHTML = "";
        for (const v of suggestions) {
            const o = document.createElement("option");
            o.value = v;
            dataList.appendChild(o);
        }
    }
    refreshOps();
    fieldSel.addEventListener("change", refreshOps);

    const minus = document.createElement("button");
    minus.type = "button"; minus.className = "ghost icon-only"; minus.textContent = "−";
    minus.title = "Remove clause";
    minus.addEventListener("click", () => {
        if (wrap.children.length > 1) row.remove();
    });

    const plus = document.createElement("button");
    plus.type = "button"; plus.className = "ghost icon-only"; plus.textContent = "+";
    plus.title = "Add clause";
    plus.addEventListener("click", () => _appendClauseRow(scope, {}));

    row.append(fieldSel, opSel, valIn, dataList, minus, plus);
    wrap.appendChild(row);
}

async function convertarrSaveFilter(event, scope) {
    event.preventDefault();
    const name = document.getElementById("filter-name-" + scope).value.trim();
    const wrap = document.getElementById("filter-clauses-" + scope);
    const clauses = Array.from(wrap.children).map(row => ({
        field: row.querySelector(".clause-field").value,
        op:    row.querySelector(".clause-op").value,
        value: row.querySelector(".clause-value").value,
    }));
    const r = await fetch("/api/filters", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope, name, clauses }),
    });
    if (!r.ok) {
        alert("Save failed: " + r.status);
        return false;
    }
    const saved = await r.json();
    document.getElementById("filter-editor-" + scope).close();
    // Switch to the newly-saved filter
    window.location.search = "?filter=custom-" + saved.id;
    return false;
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
}
