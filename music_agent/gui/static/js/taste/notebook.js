// The curator's notebook: what it asked in quizzes and noticed, plus your own notes.
// The curator reads all of it; notes marked "DJ sees this" reach the DJ too.

import { api } from "../api.js";
import { ago, attempt, h, icon, toast } from "../ui.js";
import { inlineInput } from "./common.js";

export function notebookCard(notes, reload) {
  const text = h("textarea", { class: "input area", rows: 2, maxlength: 2000, required: true,
    placeholder: "A note for the curator, e.g. “I cook on Sundays, keep it mellow then”" });
  const topic = h("input", { class: "input", placeholder: "Topic (optional)", maxlength: 120 });
  const forDj = h("input", { type: "checkbox" });

  async function add(event) {
    event.preventDefault();
    const body = { text: text.value.trim(), topic: topic.value.trim() || null, for_dj: forDj.checked };
    if (!body.text) return;
    if (await attempt(() => api("/notes", { method: "POST", body }))) { toast("Note saved"); reload({ quiet: true }); }
  }

  const patch = async (n, body, message) => {
    if (await attempt(() => api(`/notes/${n.id}`, { method: "PATCH", body }))) { toast(message); reload({ quiet: true }); }
  };

  const rows = notes.length
    ? h("div", {}, notes.map((n) => {
        const share = h("input", { type: "checkbox", checked: Boolean(n.for_dj),
          onchange: () => patch(n, { for_dj: share.checked }, share.checked ? "The DJ sees this note now" : "Only the curator sees this note now") });
        const body = h("textarea", { class: "input area note-text", rows: 2, maxlength: 2000, "aria-label": "Note" });
        body.value = n.text;
        body.onblur = () => { if (body.value.trim() && body.value !== n.text) patch(n, { text: body.value.trim() }, "Note saved"); };
        return h("div", { class: "feedback-row top" },
          h("span", { class: `chip ${n.author === "you" ? "good" : ""}` }, n.author === "you" ? "you" : "curator"),
          h("div", { class: "what" },
            h("div", { class: "edit-line" },
              inlineInput(n.topic || "", (v) => patch(n, { topic: v.trim() || null }, "Topic saved"),
                { placeholder: "topic", "aria-label": "Topic", class: "input inline topic" }),
              h("span", { class: "faint small" }, ago(n.ts)),
              h("label", { class: "check small" }, share, "DJ sees this")),
            body),
          h("button", { class: "icon-btn danger", title: "Delete note", onclick: async () => {
            if (!confirm("Delete this note?")) return;
            if (await attempt(() => api(`/notes/${n.id}`, { method: "DELETE" }))) reload({ quiet: true });
          } }, icon("x")));
      }))
    : h("p", { class: "empty" }, "Empty. The curator writes here during quizzes; you can add notes too.");

  return h("div", {},
    h("form", { class: "note-form", onsubmit: add }, text,
      h("div", { class: "pref-form" }, topic, h("label", { class: "check" }, forDj, "DJ sees this too"),
        h("button", { class: "btn", type: "submit" }, icon("plus"), "Add note"))),
    rows);
}
