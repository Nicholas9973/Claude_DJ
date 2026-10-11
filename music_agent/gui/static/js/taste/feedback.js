// Feedback: reactions to tracks and artists, and general notes for the DJ. They fade over time.
// Each one can be edited (sentiment, note) or forgotten, and you can add new ones by hand.

import { api } from "../api.js";
import { ago, attempt, h, icon, toast } from "../ui.js";
import { SENTIMENT_OF, inlineInput, select } from "./common.js";

const MOODS = { like: "like", dislike: "dislike", neutral: "note" };
const SCOPES = { general: "Note for the DJ", artist: "Artist", track: "Track" };

export function feedbackCard(rows, reload) {
  return h("div", {}, addForm(reload), rows.length
    ? h("div", {}, rows.map((r) => feedbackRow(r, reload)))
    : h("p", { class: "empty" }, "No feedback yet. Use 👍 / 👎 or tell the DJ what you think."));
}

function feedbackRow(r, reload) {
  const patch = async (body, message) => {
    if (await attempt(() => api(`/feedback/${r.id}`, { method: "PATCH", body }))) { toast(message); reload({ quiet: true }); }
  };
  const sentiment = select(MOODS, SENTIMENT_OF[r.sentiment], { class: `input inline mood ${r.sentiment > 0 ? "good" : r.sentiment < 0 ? "bad" : ""}`, "aria-label": "Sentiment" });
  sentiment.onchange = () => patch({ sentiment: sentiment.value }, "Feedback changed");
  const target = r.scope === "track" ? `${r.title} - ${r.artist || "?"}` : r.scope === "artist" ? r.artist : "General";
  return h("div", { class: "feedback-row" },
    sentiment,
    h("div", { class: "what" },
      h("div", {}, target, h("span", { class: "faint" }, `  · ${r.scope} · ${ago(r.ts)}`)),
      inlineInput(r.note || "", (v) => patch({ note: v.trim() || null }, "Note saved"),
        { placeholder: r.scope === "general" ? "note (required)" : "add a note", "aria-label": "Note", class: "input inline note" })),
    h("button", { class: "icon-btn danger", title: `Forget #${r.id}`, onclick: async () => {
      if (!confirm(`Forget feedback #${r.id}? The DJ will stop taking it into account.`)) return;
      if (await attempt(() => api(`/feedback/${r.id}`, { method: "DELETE" }))) reload({ quiet: true });
    } }, icon("x")));
}

function addForm(reload) {
  const scope = select(SCOPES, "general");
  const sentiment = select({ neutral: "Note", like: "Like", dislike: "Dislike" }, "neutral");
  const title = h("input", { class: "input", placeholder: "Song title", maxlength: 300, hidden: true });
  const artist = h("input", { class: "input", placeholder: "Artist", maxlength: 300, hidden: true });
  const note = h("input", { class: "input", placeholder: "e.g. less jazz in the morning", maxlength: 500 });
  scope.onchange = () => {
    title.hidden = scope.value !== "track";
    artist.hidden = scope.value === "general";
    note.placeholder = scope.value === "general" ? "e.g. less jazz in the morning" : "Note (optional)";
  };
  async function submit(event) {
    event.preventDefault();
    const body = { scope: scope.value, sentiment: sentiment.value, title: title.value.trim() || null,
      artist: artist.value.trim() || null, note: note.value.trim() || null };
    if (await attempt(() => api("/feedback", { method: "POST", body }))) { toast("Feedback added"); reload({ quiet: true }); }
  }
  return h("form", { class: "pref-form", onsubmit: submit }, scope, sentiment, title, artist, note,
    h("button", { class: "btn", type: "submit" }, icon("plus"), "Add"));
}
