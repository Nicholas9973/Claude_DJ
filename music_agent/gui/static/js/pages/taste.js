// Everything that shapes the agents' choices, and every way to change it by hand:
// stated preferences, quiz findings, learned scores (nudge, reset, or make them preferences), adjustments,
// the curator's notebook, feedback, and the scoring settings. The cards live in ../taste/.

import { api } from "../api.js";
import { h, icon } from "../ui.js";
import { card, chips, list, section, stat } from "../taste/common.js";
import { feedbackCard } from "../taste/feedback.js";
import { findingsCard } from "../taste/findings.js";
import { notebookCard } from "../taste/notebook.js";
import { preferencesEditor } from "../taste/preferences.js";
import { adjustmentsList, scoresCard } from "../taste/scores.js";
import { tuningCard } from "../taste/tuning.js";

export default {
  id: "taste",
  title: "Taste",
  icon: "heart",

  async mount(el) {
    const body = h("div");
    const scoresState = { tab: "artists", search: "" }; // kept across reloads
    el.append(
      h("div", { class: "page-head" },
        h("div", {}, h("h1", {}, "What the DJ thinks"),
          h("p", {}, "Your stated preferences come first and never fade. Quiz findings stay tentative until enough samples back them. Scores are learned from what you say and from skips (not from tracks that just played to the end); nudge or reset any of them, or tune how they're counted.")),
        h("button", { class: "btn small", onclick: () => load() }, icon("refresh"), "Refresh")),
      body);

    /** Fetch and redraw. `quiet` keeps the scroll position (used after an edit). */
    async function load({ quiet = false } = {}) {
      const scroll = window.scrollY;
      const [t, f] = await Promise.all([api("/taste"), api("/feedback?limit=200")]);
      const p = t.profile;
      const hist = p.history || {};
      const favouritesNow = p[`liked_in_the_${t.part_of_day}`] || [];
      const recent = Object.entries(p).find(([k]) => k.startsWith("played_in_last_"))?.[1] || [];

      body.replaceChildren(
        card("Your preferences", preferencesEditor(t.preferences, load), { wide: true, top: true }),
        card("Quiz findings", findingsCard(t.findings, load), {
          wide: true, top: true,
          hint: "What the curator concluded from quizzes. Tentative ones are only soft hints for the DJ; one can be confirmed once several samples from different artists back it (and, for a dislike, a fair second chance).",
        }),
        h("div", { class: "stats" },
          stat(hist.tracks_played ?? 0, "tracks played"),
          stat(hist.feedback_given ?? 0, "pieces of feedback"),
          stat(hist.skips_seen ?? 0, "skips noticed"),
          stat(t.adjustments.length, "scores adjusted"),
          stat(t.part_of_day, "part of day now")),
        h("div", { class: "grid" },
          card("Scores", scoresCard(t.affinities, scoresState, load), {
            wide: true,
            hint: "− / + nudge a score (nudges never fade). ↺ resets what was learned, keeping the history. ♥ / ⊘ save it as a stated love or avoid.",
          }),
          card("Score adjustments", adjustmentsList(t.adjustments, load), { wide: true }),
          card("Notebook", notebookCard(t.notes, load), {
            wide: true, hint: "The curator's memory between conversations. It reads every note; tick “DJ sees this” to pass one on to the DJ.",
          }),
          card("Feedback", feedbackCard(f.feedback, load), {
            wide: true, hint: "Reactions fade over time. Change a sentiment or note in place.",
          }),
          card("At a glance", h("div", {},
            section("Liked artists", chips(p.liked_artists, "good")),
            section(`Favourites in the ${t.part_of_day}`, chips(favouritesNow, "good")),
            section("Avoiding artists", chips(p.avoid_artists, "bad")),
            section("Avoiding tracks", chips(p.avoid_tracks, "bad")))),
          card("Notes the DJ follows", list(p.notes, "Add a note in Feedback or the Notebook.")),
          card("Played in the last 24 hours", list(recent, "Nothing yet today.")),
          card("Keeping it fresh", h("div", {},
            section("Played often lately (the DJ rests these)", chips(p.played_often_lately, "")),
            section("Artists to rest for a while", chips(p.artists_to_rest, "")))),
          card("Scoring settings", tuningCard(t.tuning, load), {
            wide: true, hint: "How much each signal counts and where “liked” and “avoided” begin. Changed values are highlighted.",
          }),
          card("Raw profile", h("details", { class: "raw" },
            h("summary", {}, "Exactly what Claude sees in get_context"),
            h("pre", { class: "json" }, JSON.stringify(p, null, 2))), { wide: true })));
      if (quiet) window.scrollTo(0, scroll);
    }

    await load();
  },
};
