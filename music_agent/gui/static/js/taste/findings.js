// Findings: conclusions the curator drew from quizzes, each with the samples behind it.
// They stay tentative (a soft hint for the DJ) until the evidence passes the same bar for every
// genre; the server refuses to confirm one before that and says what's missing.

import { api } from "../api.js";
import { ago, attempt, h, icon, toast } from "../ui.js";
import { inlineInput } from "./common.js";

const REACTION = { 1: "love", 0: "ok", [-1]: "not for me" };
const STATUS_CHIP = { confirmed: "good", tentative: "", dropped: "bad" };

export function findingsCard(findings, reload) {
  const patch = async (f, body, message) => {
    if (await attempt(() => api(`/findings/${f.id}`, { method: "PATCH", body }))) { toast(message); reload({ quiet: true }); }
  };

  if (!findings.length) {
    return h("p", { class: "empty" },
      "None yet. During a quiz the curator writes down what it concludes here, with the samples behind it.");
  }
  return h("div", {}, findings.map((f) => {
    const c = f.check;
    const actions = [];
    if (f.status === "tentative" && c.ready) {
      actions.push(h("button", { class: "btn small", onclick: () => patch(f, { status: "confirmed" }, "Finding confirmed") },
        icon("check"), "Confirm"));
    }
    if (f.status !== "dropped") {
      actions.push(h("button", { class: "btn small", onclick: () => patch(f, { status: "dropped" }, "Finding dropped") },
        icon("ban"), "Drop"));
    } else {
      actions.push(h("button", { class: "btn small", onclick: () => patch(f, { status: "tentative" }, "Finding reopened") },
        icon("undo"), "Reopen"));
    }
    return h("div", { class: "feedback-row top" },
      h("span", { class: `chip ${STATUS_CHIP[f.status]}` }, f.status),
      h("div", { class: "what" },
        h("div", { class: "edit-line" },
          inlineInput(f.statement, (v) => v.trim() && patch(f, { statement: v.trim() }, "Finding reworded"),
            { "aria-label": "Statement", maxlength: 300 }),
          h("span", { class: "faint small" }, `${f.kind}: ${f.subject} · ${ago(f.updated || f.ts)}`)),
        h("div", { class: "faint small" },
          `${c.samples} sample(s), ${c.artists} ${f.kind === "artist" ? "song" : "artist"}(s), ` +
          `${c.agreeing} agreeing, ${c.salvage_tries} salvage tr${c.salvage_tries === 1 ? "y" : "ies"}`),
        f.status === "tentative" && c.missing.length
          ? h("div", { class: "small" }, "Still needs: ", c.missing.join("; "))
          : null,
        f.evidence.length
          ? h("ul", { class: "list small" }, f.evidence.map((e) => h("li", {},
              `${e.title || "?"} - ${e.artist}: ${REACTION[e.reaction]}`,
              e.popularity ? h("span", { class: "faint" }, ` (${e.popularity})`) : null,
              e.salvage ? h("span", { class: "chip tiny" }, "salvage") : null)))
          : null),
      h("div", { class: "edit-line" }, ...actions,
        h("button", { class: "icon-btn danger", title: "Delete finding and its evidence", onclick: async () => {
          if (!confirm(`Delete “${f.statement}” and its evidence?`)) return;
          if (await attempt(() => api(`/findings/${f.id}`, { method: "DELETE" }))) reload({ quiet: true });
        } }, icon("x"))));
  }));
}
