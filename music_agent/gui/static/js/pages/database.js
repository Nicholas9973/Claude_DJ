// Browser and editor for the memory database (data/music.db), newest rows first.
// Search any table, add rows, click a cell to edit it, × deletes a row. Each column gets a
// fitting editor: a list for columns with fixed choices, a date picker for times, a text box
// for long text. Changes take effect on the agents' next request; the tables' rules still apply.

import { api } from "../api.js";
import { attempt, h, icon, toast } from "../ui.js";

const PAGE_SIZE = 50;
const TIME_COLUMNS = new Set(["ts", "created", "updated", "ignore_before"]);
const LONG_TEXT = new Set(["note", "text", "prompt", "reply", "description"]);
const ABOUT = {
  requests: "Every message the agents handled (Discord, GUI and terminal), with its reply and cost.",
  plays: "Tracks that were queued, and who queued them (source: dj, curator or gui).",
  listens: "What the watcher saw: tracks finished or skipped early.",
  feedback: "What you said about tracks and artists: likes, dislikes and notes. Fades over time.",
  preferences: "What you stated outright (via the curator or the Taste page). Never fades.",
  adjustments: "Hand-made changes to scores: delta is added; feedback and listens up to ignore_before are ignored.",
  notebook: "Notes by the curator and you. for_dj = 1 means the DJ sees the note too.",
  taste_settings: "Your values for the scoring settings (see the Taste page). Delete a row to go back to the default.",
  speaker_balance: "How much louder (+) or quieter (-) each room plays than the volume-source room. Set on the Now playing page.",
  playlists: "Saved playlists. Deleting one here also deletes its tracks.",
  playlist_tracks: "The tracks in each playlist, by position.",
};

export default {
  id: "database",
  title: "Database",
  icon: "database",

  async mount(el) {
    let current = null;
    let offset = 0;
    let search = "";
    let info = [];       // column descriptions for the current table
    let searchTimer = null;

    const tabs = h("div", { class: "tabs" });
    const about = h("p", { class: "muted", style: { margin: "0 0 12px" } });
    const searchBox = h("input", { class: "input search", type: "search", placeholder: "Search this table…", "aria-label": "Search" });
    searchBox.oninput = () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => { search = searchBox.value; offset = 0; show(); }, 250);
    };
    const addBtn = h("button", { class: "btn small", onclick: () => toggleAddForm() }, icon("plus"), "Add row");
    const addArea = h("div");
    const content = h("div");
    el.append(
      h("div", { class: "page-head" },
        h("div", {}, h("h1", {}, "Database"),
          h("p", {}, "Everything the agents remember. Click a cell to edit it (Enter saves, Esc cancels, empty = NULL).")),
        h("button", { class: "btn small", onclick: () => loadTables() }, icon("refresh"), "Refresh")),
      tabs, about,
      h("div", { class: "toolbar" }, searchBox, addBtn),
      addArea, content);

    async function loadTables() {
      const { tables } = await api("/db/tables");
      if (!current) current = (tables.find((t) => t.name === "requests") || tables[0])?.name;
      tabs.replaceChildren(...tables.map((t) => h("button", {
        class: `tab ${t.name === current ? "active" : ""}`,
        onclick: () => {
          current = t.name; offset = 0; search = ""; searchBox.value = "";
          addArea.replaceChildren();
          loadTables();
        },
      }, t.name, h("span", { class: "count" }, t.rows))));
      if (current) await show();
    }

    async function show() {
      about.textContent = ABOUT[current] || "";
      const q = search.trim() ? `&search=${encodeURIComponent(search.trim())}` : "";
      const page = await api(`/db/tables/${encodeURIComponent(current)}?limit=${PAGE_SIZE}&offset=${offset}${q}`);
      info = page.info;
      if (!page.rows.length) {
        if (offset > 0) { offset = Math.max(0, offset - PAGE_SIZE); return show(); }
        content.replaceChildren(h("p", { class: "empty" }, search ? "No rows match." : "No rows yet."));
        return;
      }
      const idCol = page.columns.indexOf("id");
      const last = Math.min(offset + page.rows.length, page.total);
      content.replaceChildren(
        h("div", { class: "table-wrap" },
          h("table", { class: "data" },
            h("thead", {}, h("tr", {}, info.map((c) => h("th", { title: describe(c) }, c.name)), idCol >= 0 ? h("th") : null)),
            h("tbody", {}, page.rows.map((row) => {
              const id = idCol >= 0 ? row[idCol] : null;
              return h("tr", {},
                row.map((value, i) => cell(value, info[i], id)),
                id === null ? null : h("td", { class: "row-actions" },
                  h("button", { class: "icon-btn danger small", title: `Delete row ${id}`, onclick: () => deleteRow(id) }, icon("x"))));
            })))),
        h("div", { class: "pager" },
          h("button", { class: "btn small", disabled: offset === 0, onclick: () => { offset = Math.max(0, offset - PAGE_SIZE); show(); } }, "Newer"),
          h("span", {}, `${offset + 1}–${last} of ${page.total}${search ? " matching" : ""}`),
          h("button", { class: "btn small", disabled: last >= page.total, onclick: () => { offset += PAGE_SIZE; show(); } }, "Older")));
    }

    function cell(value, column, id) {
      const text = value === null || value === undefined ? null
        : typeof value === "number" && !Number.isInteger(value) ? String(Math.round(value * 10000) / 10000) : String(value);
      const td = text === null ? h("td", { class: "null" }, "—") : h("td", { title: text }, text);
      if (column.name !== "id" && id !== null) {
        td.classList.add("editable");
        td.onclick = () => edit(td, value, column, id);
      }
      return td;
    }

    function edit(td, value, column, id) {
      if (td.classList.contains("editing")) return;
      const input = editorFor(column, value);
      td.replaceChildren(input);
      td.classList.add("editing");
      input.focus();
      input.select?.();
      const original = input.value;
      let finished = false;
      const finish = async (save) => {
        if (finished) return;
        finished = true;
        if (save && input.value !== original) {
          const body = { values: { [column.name]: toValue(column, input.value) } };
          if (await attempt(() => api(`/db/tables/${current}/rows/${id}`, { method: "PATCH", body }))) toast(`Saved ${column.name}`);
        }
        show();
      };
      input.onkeydown = (e) => {
        if (e.key === "Enter" && !(input.tagName === "TEXTAREA" && e.shiftKey)) { e.preventDefault(); finish(true); }
        else if (e.key === "Escape") finish(false);
      };
      if (input.tagName === "SELECT") input.onchange = () => finish(true);
      input.onblur = () => finish(false);   // clicking away cancels, so nothing changes by accident
    }

    function toggleAddForm() {
      if (addArea.firstChild) { addArea.replaceChildren(); return; }
      const fields = info.filter((c) => c.name !== "id").map((c) => {
        const input = editorFor(c, null, { forNew: true });
        return { column: c, input };
      });
      async function submit(event) {
        event.preventDefault();
        const values = {};
        for (const { column, input } of fields) {
          const v = toValue(column, input.value);
          if (v !== null) values[column.name] = v;
        }
        const out = await attempt(() => api(`/db/tables/${current}/rows`, { method: "POST", body: { values } }));
        if (out) { toast(`Added row ${out.row.id ?? ""} to ${current}`); addArea.replaceChildren(); loadTables(); }
      }
      addArea.replaceChildren(h("form", { class: "card add-row", onsubmit: submit },
        h("h2", {}, `New row in ${current}`),
        h("div", { class: "add-grid" }, fields.map(({ column, input }) => h("label", { class: "field" },
          h("span", { class: "faint small" }, column.name, column.required ? " *" : "", TIME_COLUMNS.has(column.name) ? " (empty = now)" : ""),
          input))),
        h("div", { class: "controls" },
          h("button", { class: "btn primary", type: "submit" }, icon("plus"), "Add"),
          h("button", { class: "btn", type: "button", onclick: () => addArea.replaceChildren() }, "Cancel"))));
      fields[0]?.input.focus();
    }

    async function deleteRow(id) {
      if (!confirm(`Delete row ${id} from ${current}? This can't be undone.`)) return;
      if (await attempt(() => api(`/db/tables/${current}/rows/${id}`, { method: "DELETE" }))) loadTables();
    }

    await loadTables();
  },
};

/** The right input for a column: a list of choices, a date picker, a text box or a plain field. */
function editorFor(column, value, { forNew = false } = {}) {
  const cls = forNew ? "input" : "cell-input";
  if (column.choices) {
    const options = [...(column.required ? [] : [["", "— (empty)"]]), ...column.choices.map((c) => [String(c), String(c)])];
    if (forNew && column.required) options.unshift(["", "choose…"]);
    const el = h("select", { class: cls, "aria-label": column.name }, options.map(([v, label]) => h("option", { value: v }, label)));
    el.value = value === null || value === undefined ? "" : String(value);
    return el;
  }
  if (TIME_COLUMNS.has(column.name)) {
    return h("input", { class: cls, type: "datetime-local", step: 1, "aria-label": column.name,
      value: value ? String(value).slice(0, 19) : "" });
  }
  const text = value === null || value === undefined ? "" : String(value);
  if (LONG_TEXT.has(column.name)) {
    const el = h("textarea", { class: `${cls} area`, rows: forNew ? 2 : 4, "aria-label": column.name });
    el.value = text;
    return el;
  }
  const numeric = column.type === "INTEGER" || column.type === "REAL";
  return h("input", { class: cls, value: text, "aria-label": column.name,
    ...(numeric ? { type: "number", step: column.type === "REAL" ? "any" : 1 } : {}) });
}

/** Editor text as a database value: "" -> NULL; numbers only for numeric columns (so "007" stays text). */
function toValue(column, text) {
  if (text === "") return null;
  if (column.choices) {
    const match = column.choices.find((c) => String(c) === text);
    return match === undefined ? text : match;
  }
  if (TIME_COLUMNS.has(column.name)) return text.length === 16 ? `${text}:00` : text;
  if (column.type === "INTEGER" && /^-?\d+$/.test(text.trim())) return Number.parseInt(text, 10);
  if (column.type === "REAL" && text.trim() !== "" && !Number.isNaN(Number(text))) return Number(text);
  return text;
}

function describe(column) {
  const bits = [column.type || "any type"];
  if (column.required) bits.push("required");
  if (column.choices) bits.push(`one of: ${column.choices.join(", ")}`);
  return bits.join(" · ");
}
