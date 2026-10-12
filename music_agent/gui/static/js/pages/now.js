// Home page: what's playing, controls, volume (the group and each speaker), queue, and the chat with the DJ.

import { api } from "../api.js";
import { chatPanel } from "../chat.js";
import { attempt, clock, every, h, icon, toast } from "../ui.js";

const DJ_SUGGESTIONS = [
  "Play something upbeat",
  "Calm jazz in Night only",
  "Play one of my playlists",
  "I love this track",
  "More like this",
];

const STATES = { PLAYING: "Playing", PAUSED_PLAYBACK: "Paused", STOPPED: "Stopped", TRANSITIONING: "Loading" };
const POLL_MS = 3000;

export default {
  id: "now",
  title: "Now playing",
  icon: "disc",

  async mount(el) {
    let np = null;            // last /api/now response
    let sampledAt = 0;        // when it arrived, to move the progress bar between polls
    let trackKey = "";        // changes when the track changes
    let artUrl = null;
    let volumeTimer = null;
    let volumeBusy = false;   // don't overwrite the slider while it's being dragged

    // ---------------------------------------------------------------- elements
    const backdrop = h("div", { class: "now-backdrop" });
    const art = h("div", { class: "art" }, icon("music"));
    const state = h("div", { class: "now-state" });
    const title = h("div", { class: "now-title" }, "Loading…");
    const artist = h("div", { class: "now-artist" });
    const album = h("div", { class: "now-album" });
    const rooms = h("div", { class: "rooms" });
    const elapsed = h("span", {}, "-:--");
    const total = h("span", {}, "-:--");
    const fill = h("div", { class: "bar-fill" });
    const playBtn = h("button", { class: "icon-btn big", title: "Play / pause", onclick: togglePlay }, icon("play"));
    const likeBtn = h("button", { class: "icon-btn", title: "I like this (saved to your taste)", onclick: () => rate("like") }, icon("up"));
    const dislikeBtn = h("button", { class: "icon-btn", title: "Not for me (saved, and skips)", onclick: () => rate("dislike") }, icon("down"));
    const volume = h("input", { type: "range", min: 0, max: 100, value: 0, oninput: onVolume, "aria-label": "Volume" });
    const volumeValue = h("span", { class: "value" });
    const queueList = h("ol", { class: "queue" });
    const speakers = speakerControls();
    const guests = guestMode();
    const dj = chatPanel({
      conversation: "dj",
      placeholder: "Ask the DJ…  e.g. “something mellow for cooking”",
      suggestions: DJ_SUGGESTIONS,
      onReply: () => { trackKey = ""; refresh(); },   // the DJ may have changed the queue
    });

    el.append(
      backdrop,
      h("section", { class: "now" },
        h("div", {},
          h("div", { class: "now-hero" },
            art,
            h("div", { class: "now-meta" },
              state, title, artist, album, rooms,
              h("div", { class: "progress" }, elapsed, h("div", { class: "bar" }, fill), total),
              h("div", { class: "controls" },
                h("button", { class: "icon-btn", title: "Stop", onclick: () => control("stop") }, icon("stop")),
                playBtn,
                h("button", { class: "icon-btn", title: "Skip", onclick: () => control("skip") }, icon("skip")),
                h("span", { class: "spacer" }),
                likeBtn, dislikeBtn),
              h("div", { class: "volume" }, icon("volume"), volume, volumeValue))),
          h("div", { class: "card dj-chat" },
            h("div", { class: "dj-chat-head" },
              h("h2", {}, "Ask the DJ"),
              h("button", { class: "btn small", onclick: () => dj.reset() }, icon("plus"), "New conversation")),
            dj.messages,
            dj.composer)),
        h("aside", { class: "side" },
          guests.el,
          speakers.el,
          h("div", { class: "card" }, h("h2", {}, "Queue"), queueList))));

    // ---------------------------------------------------------------- data
    async function refresh() {
      try {
        np = await api("/now");
        sampledAt = performance.now();
        render();
        speakers.load();
        guests.load();
      } catch (err) {
        np = null;
        title.textContent = "Can't reach the speakers";
        artist.textContent = err.message;
        album.textContent = "";
        state.replaceChildren(h("span", { class: "dot error" }), "Offline");
      }
    }

    async function loadQueue() {
      const q = await api("/queue?limit=50").catch(() => null);
      if (!q) return;
      const cur = q.current_position || 0;
      queueList.replaceChildren(...(q.queue.length
        ? q.queue.map((t, i) => h("li", { class: i + 1 === cur ? "current" : i + 1 < cur ? "past" : "" },
            h("span", { class: "n" }, i + 1), h("span", { class: "t", title: t }, t)))
        : [h("li", { class: "empty" }, "The queue is empty.")]));
      const current = queueList.querySelector(".current");
      if (current) queueList.scrollTop = current.offsetTop - queueList.offsetTop - 60;
    }

    // ---------------------------------------------------------------- rendering
    function render() {
      const playing = np.state === "PLAYING";
      const hasTrack = Boolean(np.title);
      title.textContent = np.title || "Nothing playing";
      artist.textContent = np.artist || (hasTrack ? "" : "Ask the DJ for something below.");
      album.textContent = np.album || "";
      state.replaceChildren(
        h("span", { class: `dot ${playing ? "online" : ""}` }),
        STATES[np.state] || np.state,
        np.queue_length ? ` · track ${np.queue_position || "-"} of ${np.queue_length}` : "");
      rooms.replaceChildren(...np.rooms.map((r) => h("span", { class: "chip" }, r)));
      playBtn.replaceChildren(icon(playing ? "pause" : "play"));
      setArt(hasTrack ? np.album_art : null);

      if (!volumeBusy) {
        volume.max = np.volume_cap;
        volume.value = np.volume ?? 0;
        volumeValue.textContent = `${np.volume ?? "?"} / ${np.volume_cap}`;
      }

      const key = `${np.title}|${np.artist}|${np.queue_position}|${np.queue_length}`;
      if (key !== trackKey) {
        trackKey = key;
        likeBtn.classList.remove("liked");
        dislikeBtn.classList.remove("disliked");
        loadQueue();
      }
      tickProgress();
    }

    function setArt(url) {
      if (url === artUrl) return;
      artUrl = url;
      if (!url) {
        art.replaceChildren(icon("music"));
        backdrop.classList.remove("on");
        return;
      }
      const img = h("img", { src: url, alt: "" });
      img.onerror = () => { art.replaceChildren(icon("music")); backdrop.classList.remove("on"); };
      art.replaceChildren(img);
      backdrop.style.backgroundImage = `url("${url}")`;
      backdrop.classList.add("on");
    }

    function tickProgress() {
      if (!np) return;
      let pos = np.position_s ?? 0;
      if (np.state === "PLAYING") pos += (performance.now() - sampledAt) / 1000;
      const dur = np.duration_s;
      if (dur) pos = Math.min(pos, dur);
      elapsed.textContent = np.title ? clock(pos) : "-:--";
      total.textContent = dur ? clock(dur) : "-:--";
      fill.style.width = dur ? `${(pos / dur) * 100}%` : "0%";
    }

    // ---------------------------------------------------------------- actions
    async function control(action) {
      await attempt(() => api(`/playback/${action}`, { method: "POST" }));
      refresh();
    }

    function togglePlay() {
      control(np && np.state === "PLAYING" ? "pause" : "resume");
    }

    async function rate(sentiment) {
      const saved = await attempt(() => api("/rate", { method: "POST", body: { sentiment, skip: sentiment === "dislike" } }));
      if (!saved) return;
      if (sentiment === "like") {
        likeBtn.classList.add("liked");
        toast(`Noted: you like ${saved.target}`);
      } else {
        toast(`Noted, and skipped: ${saved.target}`);
        refresh();
      }
    }

    function onVolume() {
      volumeBusy = true;
      volumeValue.textContent = `${volume.value} / ${volume.max}`;
      clearTimeout(volumeTimer);
      volumeTimer = setTimeout(async () => {
        const out = await attempt(() => api("/volume", { method: "POST", body: { level: Number(volume.value) } }));
        volumeBusy = false;
        if (out) volume.value = out.volume;
        speakers.load();   // every speaker moved, keeping its balance
      }, 250);
    }

    // ---------------------------------------------------------------- timers
    const stopPolling = every(POLL_MS, refresh);
    const ticker = setInterval(tickProgress, 1000);
    await dj.load();
    return () => {
      stopPolling();
      clearInterval(ticker);
      clearTimeout(volumeTimer);
      speakers.destroy();
      dj.destroy();
    };
  },
};

/**
 * Guest mode: while it's on, everything that plays is "for others" and doesn't train your taste
 * (the DJ also marks requests like "for the party" by itself). Returns { el, load }.
 */
function guestMode() {
  const status = h("div", { class: "faint small" });
  const button = h("button", { class: "btn small", onclick: toggle }, "Guests over");
  const el = h("div", { class: "card" },
    h("div", { class: "dj-chat-head" }, h("h2", {}, "Guest mode"), button),
    status);
  let on = false;

  function show(data) {
    on = data.on;
    button.textContent = on ? "Turn off" : "Guests over";
    button.classList.toggle("primary", on);
    status.textContent = on
      ? `On until ${data.until.slice(11, 16)}: what plays now doesn't shape your taste.`
      : "Off: what plays is learned from as your own listening.";
  }

  async function load() {
    const data = await api("/guest").catch(() => null);
    if (data) show(data);
  }

  async function toggle() {
    const data = await attempt(() => api("/guest", { method: "POST", body: { hours: on ? null : 4 } }));
    if (data) {
      show(data);
      toast(data.on ? "Guest mode on for 4 hours" : "Guest mode off");
    }
  }

  return { el, load };
}

/**
 * One volume slider per speaker. Moving one saves the balance between speakers, so the
 * group slider, new DJ sets and resuming keep e.g. Night 5 quieter than the Dining Room.
 * Returns { el, load, destroy }; `load()` refreshes the sliders that aren't being dragged.
 */
function speakerControls() {
  const rows = new Map();     // room -> { row, slider, value, note, chip, timer, busy }
  const list = h("div", { class: "speakers" });
  const levelBtn = h("button", {
    class: "btn small", title: "Forget the balance: every speaker at the same level as the main slider",
    onclick: async () => {
      if (await attempt(() => api("/volumes/reset", { method: "POST" }))) { toast("All speakers at the same level"); load(); }
    },
  }, "Level all");
  const el = h("div", { class: "card" },
    h("div", { class: "dj-chat-head" }, h("h2", {}, "Speakers"), levelBtn),
    list);
  let alive = true;

  async function load() {
    const data = await api("/volumes").catch(() => null);
    if (!data || !alive) return;
    const reference = data.rooms.find((r) => r.reference)?.room;
    levelBtn.disabled = !data.rooms.some((r) => r.difference);
    for (const r of data.rooms) {
      const ctl = rows.get(r.room) || addRow(r.room);
      ctl.slider.max = data.volume_cap;
      if (!ctl.busy) {
        ctl.slider.value = r.volume;
        ctl.value.textContent = r.volume;
      }
      ctl.row.classList.toggle("idle", !r.in_group);
      ctl.chip.textContent = r.in_group ? "playing" : "not playing";
      ctl.chip.className = `chip tiny ${r.in_group ? "good" : ""}`;
      ctl.note.textContent = r.reference ? "the others are balanced against this room"
        : r.difference ? `${r.difference > 0 ? "+" : ""}${r.difference} vs ${reference || "the main room"}`
        : `level with ${reference || "the main room"}`;
    }
  }

  function addRow(room) {
    const slider = h("input", { type: "range", min: 0, max: 100, value: 0, "aria-label": `${room} volume` });
    const value = h("span", { class: "value" });
    const note = h("div", { class: "faint small" });
    const chip = h("span", { class: "chip tiny" });
    const row = h("div", { class: "speaker" },
      h("div", { class: "speaker-head" }, h("span", { class: "speaker-name" }, room), chip),
      h("div", { class: "volume" }, slider, value),
      note);
    const ctl = { row, slider, value, note, chip, timer: null, busy: false };
    slider.oninput = () => {
      ctl.busy = true;
      value.textContent = slider.value;
      clearTimeout(ctl.timer);
      ctl.timer = setTimeout(async () => {
        const out = await attempt(() => api("/volume", { method: "POST", body: { room, level: Number(slider.value) } }));
        ctl.busy = false;
        if (out) { slider.value = out.volume; value.textContent = out.volume; }
        load();
      }, 250);
    };
    rows.set(room, ctl);
    list.append(row);
    return ctl;
  }

  return {
    el,
    load,
    destroy: () => { alive = false; rows.forEach((c) => clearTimeout(c.timer)); },
  };
}
