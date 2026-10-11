// Saved playlists: the DJ sees their names and descriptions and plays a fitting one
// in a single step. Build them with the curator (Chat page) or by hand here.

import { api } from "../api.js";
import { ago, attempt, clock, h, icon, toast } from "../ui.js";

export default {
  id: "playlists",
  title: "Playlists",
  icon: "playlist",

  async mount(el, { params }) {
    let selected = Number(params.get("id")) || null;
    const list = h("div", { class: "pl-list" });
    const detail = h("div", { class: "pl-detail" });
    const newName = h("input", { class: "input", placeholder: "New playlist name", maxlength: 120 });

    el.append(
      h("div", { class: "page-head" },
        h("div", {}, h("h1", {}, "Playlists"),
          h("p", {}, "Ready-made sets the DJ can play in one go. The description tells the DJ when one fits.")),
        h("a", { class: "btn small", href: "#/chat?q=" + encodeURIComponent("Make me a new playlist. Ask me what it's for first.") },
          icon("sparkles"), "Ask the curator to build one")),
      h("div", { class: "pl-layout" },
        h("div", {},
          h("form", { class: "pl-new", onsubmit: create }, newName,
            h("button", { class: "btn", type: "submit", title: "Create an empty playlist" }, icon("plus"))),
          list),
        detail));

    // ---------------------------------------------------------------- list
    async function loadList() {
      const { playlists } = await api("/playlists");
      if (!playlists.length) {
        list.replaceChildren(h("p", { class: "empty" }, "No playlists yet."));
        selected = null;
      } else {
        if (!playlists.some((p) => p.id === selected)) selected = playlists[0].id;
        list.replaceChildren(...playlists.map((p) => h("button", {
          class: `pl-item ${p.id === selected ? "active" : ""}`,
          onclick: () => { selected = p.id; loadList(); },
        },
          h("div", { class: "pl-name" }, p.name),
          h("div", { class: "faint" }, `${p.tracks} tracks · ${minutes(p.duration_s)} · ${ago(p.updated)}`))));
      }
      await showDetail();
    }

    async function create(event) {
      event.preventDefault();
      const name = newName.value.trim();
      if (!name) return;
      const p = await attempt(() => api("/playlists", { method: "POST", body: { name } }));
      if (!p) return;
      newName.value = "";
      selected = p.id;
      loadList();
    }

    // ---------------------------------------------------------------- one playlist
    async function showDetail() {
      if (!selected) {
        detail.replaceChildren(h("div", { class: "card" }, h("p", { class: "empty" },
          "Ask the curator on the Chat page for a playlist (“make a playlist for cooking”), or create one on the left.")));
        return;
      }
      const p = await api(`/playlists/${selected}`);
      const name = h("input", { class: "input title-input", value: p.name, maxlength: 120, "aria-label": "Name" });
      const description = h("textarea", {
        class: "input area", rows: 2, maxlength: 500, "aria-label": "Description",
        placeholder: "When does this fit? e.g. “Warm, unhurried jazz for cooking or dinner”. The DJ reads this.",
      });
      description.value = p.description || "";
      const total = p.tracks.reduce((s, t) => s + (t.duration_s || 0), 0);

      const title = h("input", { class: "input", placeholder: "Title", required: true });
      const artist = h("input", { class: "input", placeholder: "Artist", required: true });
      const addForm = h("form", { class: "pl-add", onsubmit: addTrack }, title, artist,
        h("button", { class: "btn", type: "submit" }, icon("plus"), "Add"));

      detail.replaceChildren(h("div", { class: "card" },
        h("div", { class: "pl-head" },
          h("div", { class: "pl-fields" }, name, description),
          h("button", { class: "btn small", onclick: save }, "Save")),
        h("div", { class: "controls" },
          h("button", { class: "btn primary", disabled: !p.tracks.length, onclick: () => play("replace", false) }, icon("play"), "Play"),
          h("button", { class: "btn", disabled: !p.tracks.length, onclick: () => play("replace", true) }, icon("shuffle"), "Shuffle"),
          h("button", { class: "btn", disabled: !p.tracks.length, onclick: () => play("append", false) }, icon("plus"), "Add to queue"),
          h("span", { class: "spacer" }),
          h("span", { class: "faint" }, `${p.tracks.length} tracks · ${minutes(total)}`),
          h("button", { class: "icon-btn danger", title: "Delete playlist", onclick: remove }, icon("trash"))),
        p.tracks.length
          ? h("ol", { class: "pl-tracks" }, p.tracks.map((t) => h("li", {},
              h("span", { class: "n" }, t.position),
              h("div", { class: "t" }, h("div", {}, t.title), h("div", { class: "faint" }, [t.artist, t.album].filter(Boolean).join(" · "))),
              h("span", { class: "d" }, t.duration_s ? clock(t.duration_s) : ""),
              h("button", { class: "icon-btn danger small", title: "Remove from playlist", onclick: () => removeTrack(t) }, icon("x")))))
          : h("p", { class: "empty" }, "No tracks yet. Add some below, or ask the curator to fill it."),
        h("h2", { style: { margin: "20px 0 10px" } }, "Add a track"),
        addForm));

      async function save() {
        const out = await attempt(() => api(`/playlists/${p.id}`, {
          method: "PATCH", body: { name: name.value.trim(), description: description.value } }));
        if (out) { toast("Saved"); loadList(); }
      }

      async function play(mode, shuffle) {
        const out = await attempt(() => api(`/playlists/${p.id}/play`, { method: "POST", body: { mode, shuffle } }));
        if (out) toast(`${mode === "append" ? "Added" : "Playing"} ${out.queued} tracks from ${out.playlist}`);
      }

      async function remove() {
        if (!confirm(`Delete the playlist “${p.name}”?`)) return;
        if (await attempt(() => api(`/playlists/${p.id}`, { method: "DELETE" }))) { selected = null; loadList(); }
      }

      async function removeTrack(t) {
        if (await attempt(() => api(`/playlists/${p.id}/tracks/${t.id}`, { method: "DELETE" }))) loadList();
      }

      async function addTrack(event) {
        event.preventDefault();
        const out = await attempt(() => api(`/playlists/${p.id}/tracks`, {
          method: "POST", body: { title: title.value.trim(), artist: artist.value.trim() } }));
        if (!out) return;
        if (out.saved) {
          toast(`Added: ${out.playlist.tracks.at(-1).title}`);
          loadList();
        } else if (out.not_found.length) {
          const miss = out.not_found[0];
          const hint = miss.catalogue_tracks_by_artist ? ` By this artist: ${miss.catalogue_tracks_by_artist.slice(0, 5).join(", ")}…` : "";
          toast(`Not found in the catalogue (${miss.reason}).${hint}`, "error");
        } else {
          toast("Already in this playlist.");
        }
      }
    }

    await loadList();
  },
};

function minutes(seconds) {
  if (!seconds) return "0 min";
  const m = Math.round(seconds / 60);
  return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`;
}
