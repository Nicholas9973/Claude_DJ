// Learned scores per artist and track, where each came from, and the controls to change them:
// nudge up/down, reset what was learned, or turn it into a stated love/avoid.
// Plus the list of hand-made adjustments, each editable.

import { api } from "../api.js";
import { attempt, h, icon, toast } from "../ui.js";
import { SOURCE_LABELS, inlineInput, select, signed } from "./common.js";
import { statePreference } from "./preferences.js";

const SHOWN = 60;

/**
 * The scores card. `state` ({ tab, search }) survives reloads so the view stays put while you click.
 *   affinities: { artists: [...], tracks: [...] } from /api/taste
 */
export function scoresCard(affinities, state, reload) {
  const tabs = h("div", { class: "tabs compact" });
  const search = h("input", { class: "input search", type: "search", value: state.search, "aria-label": "Search" });
  const body = h("div");
  search.oninput = () => { state.search = search.value; render(); };   // filters locally, no reload

  function render() {
    const isTrack = state.tab === "tracks";
    const all = affinities[state.tab] || [];
    const needle = state.search.trim().toLowerCase();
    const matching = needle ? all.filter((e) => e.name.toLowerCase().includes(needle)) : all;
    const shown = matching.slice(0, SHOWN);
    const max = Math.max(1, ...shown.map((e) => Math.abs(e.score)));
    search.placeholder = `Find ${isTrack ? "a track" : "an artist"}…`;
    tabs.replaceChildren(...["artists", "tracks"].map((t) => h("button", {
      class: `tab ${state.tab === t ? "active" : ""}`, type: "button",
      onclick: () => { state.tab = t; render(); },
    }, t === "artists" ? "Artists" : "Tracks", h("span", { class: "count" }, (affinities[t] || []).length))));
    body.replaceChildren(
      shown.length
        ? h("div", { class: "scores scroll" }, shown.map((e) => scoreRow(e, isTrack, max, reload)))
        : h("p", { class: "empty" }, needle ? "Nothing matches. Use “Adjust by hand” below to add one." : "No signals yet."),
      matching.length > SHOWN
        ? h("p", { class: "faint small" }, `Showing ${SHOWN} of ${matching.length}, strongest first. Search to find others.`)
        : null);
  }
  render();

  return h("div", {},
    h("div", { class: "toolbar" }, tabs, search),
    body,
    h("details", { class: "raw", style: { marginTop: "14px" } },
      h("summary", {}, "Adjust by hand"),
      nudgeForm(reload)));
}

function scoreRow(e, isTrack, max, reload) {
  const width = `${(Math.abs(e.score) / max) * 50}%`;
  const parts = Object.entries(e.parts).filter(([, v]) => v !== 0)
    .map(([k, v]) => `${SOURCE_LABELS[k] || k} ${signed(v)}`);
  if (isTrack && e.skips) parts.push(`${e.skips} skip${e.skips === 1 ? "" : "s"}`);
  const reset = e.adjustment?.ignore_before;
  const subject = isTrack ? { kind: "track", subject: e.title, artist: e.artist } : { kind: "artist", subject: e.name };

  const nudge = async (change, reset_learning = false) => {
    const out = await attempt(() => api("/adjustments", { method: "POST", body: { ...subject, change, reset_learning } }));
    if (out) {
      if (reset_learning) toast(`Learning about ${e.name} reset (history kept)`);
      reload({ quiet: true });
    }
  };
  const prefer = async (sentiment) => {
    if (await statePreference(e, isTrack, sentiment)) {
      toast(`Saved: ${sentiment === "like" ? "love" : "avoid"} ${e.name}`);
      reload({ quiet: true });
    }
  };

  return h("div", { class: "score-row big" },
    h("div", { class: "name" },
      h("div", { class: "title", title: e.name }, e.name),
      h("div", { class: "faint small" }, parts.join(" · ") || "no signals",
        reset ? h("span", { class: "chip tiny" }, `reset ${reset.slice(0, 10)}`) : null)),
    h("div", { class: "diverge" }, h("span", { class: e.score >= 0 ? "pos" : "neg", style: { width } })),
    h("span", { class: "num" }, signed(e.score)),
    h("div", { class: "row-tools" },
      h("button", { class: "icon-btn small", title: "Nudge down by 1", onclick: () => nudge(-1) }, icon("minus")),
      h("button", { class: "icon-btn small", title: "Nudge up by 1", onclick: () => nudge(1) }, icon("plus")),
      h("button", { class: "icon-btn small", title: "Reset what was learned (ignore older feedback and listens; history is kept)", onclick: () => nudge(0, true) }, icon("undo")),
      h("button", { class: "icon-btn small good", title: "Save as a stated love", onclick: () => prefer("like") }, icon("heart")),
      h("button", { class: "icon-btn small danger", title: "Save as a stated avoid", onclick: () => prefer("dislike") }, icon("ban"))));
}

function nudgeForm(reload) {
  const kind = select({ artist: "Artist", track: "Track" }, "artist");
  const subject = h("input", { class: "input", placeholder: "Artist", required: true, maxlength: 300 });
  const artist = h("input", { class: "input", placeholder: "Artist", maxlength: 300, hidden: true });
  const change = h("input", { class: "input narrow", type: "number", step: "0.5", min: -50, max: 50, value: 1, title: "Added to the score" });
  const reset = h("input", { type: "checkbox" });
  const note = h("input", { class: "input", placeholder: "Why (optional)", maxlength: 500 });
  kind.onchange = () => {
    subject.placeholder = kind.value === "track" ? "Song title" : "Artist";
    artist.hidden = kind.value !== "track";
  };
  async function submit(event) {
    event.preventDefault();
    const body = {
      kind: kind.value, subject: subject.value.trim(), artist: kind.value === "track" ? artist.value.trim() || null : null,
      change: Number(change.value) || 0, reset_learning: reset.checked, note: note.value.trim() || null,
    };
    if (await attempt(() => api("/adjustments", { method: "POST", body }))) { toast("Adjusted"); reload(); }
  }
  return h("form", { class: "pref-form", onsubmit: submit }, kind, subject, artist, change,
    h("label", { class: "check" }, reset, "reset learning"), note,
    h("button", { class: "btn", type: "submit" }, icon("sliders"), "Apply"));
}

/** Every hand-made adjustment: change its nudge or note, undo its reset, or remove it. */
export function adjustmentsList(adjustments, reload) {
  if (!adjustments.length) {
    return h("p", { class: "empty" }, "None yet. Use − / + / ↺ on a score above, or ask the curator (“a bit less Radiohead lately”).");
  }
  const patch = async (a, body, message) => {
    if (await attempt(() => api(`/adjustments/${a.id}`, { method: "PATCH", body }))) { toast(message); reload({ quiet: true }); }
  };
  return h("div", {}, adjustments.map((a) => h("div", { class: "feedback-row" },
    h("span", { class: "chip" }, a.kind),
    h("div", { class: "what" },
      h("div", {}, a.artist ? `${a.subject} - ${a.artist}` : a.subject),
      h("div", { class: "edit-line" },
        h("label", { class: "faint small" }, "nudge"),
        inlineInput(String(a.delta), (v) => patch(a, { delta: Number(v) || 0 }, "Nudge saved"),
          { type: "number", step: "0.5", class: "input inline narrow", "aria-label": "Nudge" }),
        inlineInput(a.note || "", (v) => patch(a, { note: v.trim() || null }, "Note saved"),
          { placeholder: "note", "aria-label": "Note" }),
        a.ignore_before
          ? h("button", { class: "btn small", title: "Count the older feedback and listens again",
              onclick: () => patch(a, { ignore_before: null }, "Reset undone") }, icon("undo"), `undo reset (${a.ignore_before.slice(0, 10)})`)
          : null)),
    h("button", { class: "icon-btn danger", title: "Remove this adjustment (nudge and reset)", onclick: async () => {
      if (await attempt(() => api(`/adjustments/${a.id}`, { method: "DELETE" }))) reload({ quiet: true });
    } }, icon("x")))));
}
