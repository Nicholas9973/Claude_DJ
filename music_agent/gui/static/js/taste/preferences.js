// Stated preferences: add, edit and remove. They never fade and outrank everything else.

import { api } from "../api.js";
import { ago, attempt, h, icon, toast } from "../ui.js";
import { PARTS, SENTIMENT_OF, SENTIMENTS, select } from "./common.js";

const KINDS = { artist: "Artist", track: "Track", genre: "Genre / style", rule: "Rule" };
const PLACEHOLDERS = {
  artist: "e.g. Radiohead", track: "Song title", genre: "e.g. bossa nova", rule: "e.g. instrumental only while I work",
};

export function preferencesEditor(prefs, reload) {
  let editing = null; // id of the preference loaded into the form
  const kind = select(KINDS, "artist");
  const sentiment = select(SENTIMENTS, "like");
  const part = select(PARTS, "");
  const subject = h("input", { class: "input", required: true, maxlength: 300 });
  const artist = h("input", { class: "input", placeholder: "Artist", maxlength: 300 });
  const note = h("input", { class: "input", placeholder: "Note (optional)", maxlength: 500 });
  const submit = h("button", { class: "btn primary", type: "submit" }, icon("plus"), "Add");
  const cancel = h("button", { class: "btn", type: "button", hidden: true, onclick: () => fill(null) }, "Cancel");
  const sync = () => {
    subject.placeholder = PLACEHOLDERS[kind.value];
    artist.hidden = kind.value !== "track";
  };
  kind.onchange = sync;
  sync();

  function fill(p) {
    editing = p ? p.id : null;
    kind.value = p ? p.kind : "artist";
    sentiment.value = p ? SENTIMENT_OF[p.sentiment] : "like";
    part.value = p ? p.part_of_day || "" : "";
    subject.value = p ? p.subject : "";
    artist.value = p ? p.artist || "" : "";
    note.value = p ? p.note || "" : "";
    submit.replaceChildren(icon(p ? "edit" : "plus"), p ? "Save" : "Add");
    cancel.hidden = !p;
    sync();
    if (p) subject.focus();
  }

  async function save(event) {
    event.preventDefault();
    const body = {
      kind: kind.value, sentiment: sentiment.value, subject: subject.value.trim(),
      artist: kind.value === "track" ? artist.value.trim() || null : null,
      part_of_day: part.value || null, note: note.value.trim() || null,
    };
    const out = await attempt(() => editing
      ? api(`/preferences/${editing}`, { method: "PUT", body })
      : api("/preferences", { method: "POST", body }));
    if (out) { toast(editing ? "Preference updated" : "Preference saved"); reload(); }
  }

  async function remove(p) {
    if (!confirm(`Remove “${p.text}”?`)) return;
    if (await attempt(() => api(`/preferences/${p.id}`, { method: "DELETE" }))) reload();
  }

  const rows = prefs.length
    ? h("div", {}, prefs.map((p) => h("div", { class: "feedback-row" },
        h("span", { class: `chip ${p.kind === "rule" ? "" : p.sentiment > 0 ? "good" : p.sentiment < 0 ? "bad" : ""}` },
          p.kind === "rule" ? "rule" : SENTIMENTS[SENTIMENT_OF[p.sentiment]].toLowerCase()),
        h("div", { class: "what" },
          h("div", {}, p.text.replace(/^(love|avoid|note on|rule):?\s*/, ""), h("span", { class: "faint" }, `  · ${ago(p.ts)}`))),
        h("button", { class: "icon-btn", title: "Edit", onclick: () => fill(p) }, icon("edit")),
        h("button", { class: "icon-btn danger", title: "Remove", onclick: () => remove(p) }, icon("x")))))
    : h("p", { class: "empty" }, "Nothing stated yet. Add one here, or tell the curator on the Chat page (“I'm done with Coldplay”).");

  return h("div", {},
    h("form", { class: "pref-form", onsubmit: save }, sentiment, kind, subject, artist, part, note, submit, cancel),
    rows);
}

/** Save an artist or track as a stated love/avoid in one click (from the scores list). */
export async function statePreference(entry, isTrack, sentiment) {
  const body = isTrack
    ? { kind: "track", sentiment, subject: entry.title, artist: entry.artist || null }
    : { kind: "artist", sentiment, subject: entry.name };
  return attempt(() => api("/preferences", { method: "POST", body }));
}
