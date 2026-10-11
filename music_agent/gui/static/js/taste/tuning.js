// The numbers behind the scores: how much each signal counts, how fast feedback fades,
// and where "liked" and "avoided" begin. Changes apply to the agents' next request.

import { api } from "../api.js";
import { attempt, h, icon, toast } from "../ui.js";

export function tuningCard(tuning, reload) {
  const save = async (t, value) => {
    const out = await attempt(() => api(`/tuning/${t.key}`, { method: "PUT", body: { value } }));
    if (out) { toast(value === null ? `${t.label}: back to ${t.default}` : `${t.label}: ${value}`); reload({ quiet: true }); }
  };
  return h("div", { class: "tuning" }, tuning.map((t) => {
    const input = h("input", {
      class: `input inline narrow ${t.changed ? "changed" : ""}`, type: "number", step: "any",
      value: t.value, min: t.minimum ?? undefined, max: t.maximum ?? undefined, "aria-label": t.label,
    });
    input.onchange = () => {
      const value = Number(input.value);
      if (input.value.trim() === "" || Number.isNaN(value)) { input.value = t.value; return; }
      save(t, value);
    };
    return h("div", { class: "tuning-row" },
      h("div", { class: "what" }, h("div", {}, t.label), h("div", { class: "faint small" }, t.description)),
      input,
      h("button", { class: "icon-btn small", title: `Back to the default (${t.default})`, disabled: !t.changed,
        onclick: () => save(t, null) }, icon("undo")));
  }));
}
