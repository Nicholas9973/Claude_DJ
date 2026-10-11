// Health of the moving parts: Discord bot (start/stop), agent, safety limits, and the live log.

import { api } from "../api.js";
import { attempt, every, h, icon } from "../ui.js";

const BOT_LABELS = {
  online: "Online", connecting: "Connecting…", not_configured: "Not set up", error: "Error", stopped: "Stopped", off: "Off",
};

export default {
  id: "system",
  title: "System",
  icon: "settings",

  async mount(el) {
    const cards = h("div", { class: "grid" });
    const log = h("pre", { class: "log" }, "Loading…");
    el.append(
      h("div", { class: "page-head" },
        h("div", {}, h("h1", {}, "System"), h("p", {}, "Everything running in this process.")),
        h("a", { class: "btn small", href: "/docs", target: "_blank" }, "API docs")),
      cards,
      h("div", { class: "card", style: { marginTop: "16px" } }, h("h2", {}, "Log"), log));

    async function loadStatus() {
      const s = await api("/status").catch(() => null);
      if (!s) return;
      const bot = s.bot;
      const running = bot.state === "online" || bot.state === "connecting";
      cards.replaceChildren(
        h("div", { class: "card" },
          h("h2", {}, "Discord bot"),
          h("div", { class: "bot-state" }, h("span", { class: `dot ${bot.state}` }), BOT_LABELS[bot.state] || bot.state),
          h("p", { class: "muted", style: { marginTop: 0 } }, bot.detail || (running ? "" : "Not running.")),
          running
            ? h("button", { class: "btn danger", onclick: () => botAction("stop") }, "Stop bot")
            : h("button", { class: "btn primary", onclick: () => botAction("start") }, "Start bot")),
        kvCard("Agent", {
          Model: s.agent.model,
          "Music service": s.agent.service,
        }),
        kvCard("Safety (enforced in code)", {
          "Max volume": s.safety.max_volume,
          "Volume cap now": s.safety.volume_cap_now,
          "Quiet hours": `${s.safety.quiet_hours} (${s.safety.quiet_mode})`,
          "In quiet hours": s.safety.in_quiet_hours ? "yes" : "no",
        }),
        kvCard("This process", {
          Started: new Date(s.started_at).toLocaleString(),
          Memory: s.memory.enabled ? s.memory.path : "off",
          "Playback watcher": s.watcher ? "running" : "stopped",
          "Log file": s.log_file || "none",
        }));
    }

    async function botAction(action) {
      await attempt(() => api(`/bot/${action}`, { method: "POST" }));
      loadStatus();
    }

    async function loadLog() {
      const { lines } = await api("/logs?lines=300").catch(() => ({ lines: null }));
      if (!lines) return;
      const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
      log.textContent = lines.join("\n") || "(empty)";
      if (atBottom) log.scrollTop = log.scrollHeight;
    }

    const stopStatus = every(5000, loadStatus);
    const stopLog = every(5000, loadLog);
    return () => { stopStatus(); stopLog(); };
  },
};

function kvCard(title, pairs) {
  return h("div", { class: "card" }, h("h2", {}, title),
    h("dl", { class: "kv" }, Object.entries(pairs).flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, String(v))])));
}
