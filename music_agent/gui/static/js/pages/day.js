// The day plan (day_plan.toml) and the orchestrator that follows it: a rough outline of
// the music you want through the day. Edit it here, by hand, or ask the curator.

import { api } from "../api.js";
import { ago, attempt, every, h, icon, toast } from "../ui.js";

const ACTIONS = {
  start: ["Started music", "good"], top_up: ["Topped up", "good"], new_slot: ["New part of the day", "good"],
  quiet: ["Stopped: staying quiet", "bad"], bedtime: ["Bedtime: faded out", ""], unquiet: ["Music back on", ""], adopt: ["Took over", ""], idle: ["Nothing to do", ""],
};
const DAYS = [["all", "Every day"], ["weekdays", "Weekdays"], ["weekend", "Weekend"]];
const REVIEW = "Review my day plan: compare it with what I actually listened to, skipped and stopped lately, and suggest changes.";

export default {
  id: "day",
  title: "Day",
  icon: "sun",

  async mount(el) {
    const status = h("div", { class: "card" });
    const today = h("div", { class: "card" });
    const editor = h("div", { class: "card", style: { marginTop: "16px" } });
    const history = h("div", { class: "card", style: { marginTop: "16px" } });
    el.append(
      h("div", { class: "page-head" },
        h("div", {}, h("h1", {}, "Day"),
          h("p", {}, "A rough outline of the music you want through the day. The orchestrator follows it and asks the DJ for fitting music.")),
        h("a", { class: "btn small", href: "#/chat?q=" + encodeURIComponent(REVIEW) }, icon("sparkles"), "Ask the curator to review it")),
      h("div", { class: "grid" }, status, today),
      editor, history);

    let data = null;

    async function load({ withEditor = false } = {}) {
      const d = await api("/dayplan").catch((err) => { toast(err.message, "error"); return null; });
      if (!d) return;
      data = d;
      renderStatus();
      renderToday();
      renderHistory();
      if (withEditor) renderEditor(structuredClone(d.plan));
    }

    // ------------------------------------------------------------ status
    function renderStatus() {
      const { plan, orchestrator: o, quiet_until: quiet } = data;
      const last = o.last_check;
      status.replaceChildren(
        h("h2", {}, "Orchestrator"),
        h("label", { class: "day-toggle" },
          h("input", { type: "checkbox", checked: plan.enabled, onchange: (e) => toggle(e.target.checked) }),
          plan.enabled ? "On: keeps music playing through the day" : "Off: turn on to follow the plan"),
        h("dl", { class: "kv" },
          h("dt", {}, "Running here"), h("dd", {}, o.running ? "yes" : "no (started with --no-bot or --no-orchestrator)"),
          h("dt", {}, "Last check"), h("dd", {}, last ? `${ago(last.time)}: ${last.reason}` : "not yet"),
          h("dt", {}, "Claude runs today"), h("dd", {}, `${o.runs_today} of at most ${o.max_runs_per_day}`),
          h("dt", {}, "After a stop"), h("dd", {}, quiet
            ? h("span", {}, `quiet until ${quiet.slice(11, 16)} `,
              h("button", { class: "btn small", onclick: resume }, "Resume now"))
            : `stays quiet for ${plan.quiet_after_stop_hours} h`)),
        h("button", { class: "btn", disabled: o.busy, onclick: checkNow, title: "Run the check now (may ask Claude)" },
          icon("refresh"), o.busy ? "Checking…" : "Check now"));
    }

    async function toggle(enabled) {
      const d = await attempt(() => api("/dayplan", { method: "PUT", body: { ...data.plan, enabled } }));
      if (d) { data = d; renderStatus(); renderToday(); }
    }

    async function resume() {
      const d = await attempt(() => api("/dayplan/resume", { method: "POST" }));
      if (d) { data = d; renderStatus(); }
    }

    async function checkNow(e) {
      e.target.disabled = true;
      const d = await attempt(() => api("/dayplan/check", { method: "POST" }));
      if (!d) { renderStatus(); return; }
      data = d;
      toast(d.check.reply || d.check.reason);
      renderStatus();
      renderHistory();
    }

    // ------------------------------------------------------------ today
    function renderToday() {
      const t = data.today;
      today.replaceChildren(
        h("h2", {}, `Today (${t.weekend ? "weekend" : "weekday"})`),
        h("p", { class: "muted", style: { marginTop: 0 } }, `Up at ${t.wake}, music until ${t.sleep}.`),
        t.slots.length
          ? h("ol", { class: "day-today" }, t.slots.map((s) => h("li", { class: s.now ? "now" : "" },
            h("span", { class: "day-time" }, s.time),
            h("span", {}, s.quiet ? h("em", { class: "faint" }, "quiet") : s.mood,
              s.rooms ? h("span", { class: "chip tiny" }, s.rooms.join(", ")) : null,
              s.volume_change ? h("span", { class: "chip tiny", title: "Volume change when this part begins" },
                `vol ${s.volume_change > 0 ? "+" : ""}${s.volume_change}`) : null))))
          : h("p", { class: "empty" }, "No parts of the day apply today."));
    }

    // ------------------------------------------------------------ history
    function renderHistory() {
      const items = data.orchestrator.history;
      history.replaceChildren(h("h2", {}, "Recent activity"),
        items.length
          ? h("div", { class: "day-history" }, items.map((it) => {
            const [label, cls] = ACTIONS[it.action] || [it.action, ""];
            return h("div", { class: "day-event" },
              h("div", {}, h("span", { class: `chip ${cls}` }, label), " ",
                h("span", { class: "faint" }, new Date(it.time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })),
                " ", it.reason),
              it.reply ? h("div", { class: "muted" }, it.reply) : null,
              it.error ? h("div", { style: { color: "var(--bad)" } }, it.error) : null,
              it.steps?.length ? h("details", {}, h("summary", { class: "faint" }, `${it.steps.length} steps`),
                h("pre", { class: "log" }, it.steps.join("\n"))) : null);
          }))
          : h("p", { class: "empty" }, "Nothing yet since this process started."));
    }

    // ------------------------------------------------------------ editor
    function renderEditor(plan) {
      const field = (label, key, attrs) => h("label", { class: "day-field" }, h("span", { class: "faint" }, label),
        h("input", { class: "input", value: plan[key], ...attrs,
          oninput: (e) => {
            const v = e.target.value;
            plan[key] = attrs.type !== "number" ? v : v === "" && attrs.optional ? null : Number(v);
          } }));
      const slots = h("div", { class: "day-slots" });
      const drawSlots = () => slots.replaceChildren(...plan.slots.map((s, i) => slotRow(s, i)));

      function slotRow(s, i) {
        const days = h("select", { class: "input", onchange: (e) => { s.days = e.target.value; } },
          DAYS.map(([v, label]) => h("option", { value: v }, label)));
        days.value = s.days || "all";
        return h("div", { class: "day-slot" },
          h("input", { class: "input day-start", value: s.start, placeholder: "wake+1h or 14:00", title: "HH:MM, or wake / sleep with an offset like wake+90m, sleep-1h",
            oninput: (e) => { s.start = e.target.value; } }),
          days,
          h("input", { class: "input day-mood", value: s.mood, placeholder: "The mood you want, roughly", disabled: s.quiet,
            oninput: (e) => { s.mood = e.target.value; } }),
          h("input", { class: "input day-rooms", value: (s.rooms || []).join(", "), placeholder: "All rooms",
            oninput: (e) => { s.rooms = e.target.value.split(",").map((r) => r.trim()).filter(Boolean); } }),
          h("input", { class: "input day-volume", type: "number", min: -30, max: 30, value: s.volume_change || "",
            placeholder: "±0", title: "Added to the volume when this part begins, e.g. 3 or -2 (on top of your own changes)",
            oninput: (e) => { s.volume_change = e.target.value === "" ? 0 : Number(e.target.value); } }),
          h("label", { class: "day-quiet", title: "Don't start music in this part of the day" },
            h("input", { type: "checkbox", checked: s.quiet, onchange: (e) => { s.quiet = e.target.checked; drawSlots(); } }), "quiet"),
          h("button", { class: "btn small", title: "Remove", onclick: () => { plan.slots.splice(i, 1); drawSlots(); } }, icon("trash")));
      }

      async function save() {
        plan.enabled = data.plan.enabled;      // the toggle above saves that one on its own
        const d = await attempt(() => api("/dayplan", { method: "PUT", body: plan }));
        if (!d) return;
        toast("Day plan saved.");
        data = d;
        renderStatus();
        renderToday();
        renderEditor(structuredClone(d.plan));
      }

      drawSlots();
      editor.replaceChildren(
        h("h2", {}, "Plan"),
        h("p", { class: "hint muted" }, "Times are HH:MM or relative to waking up / bedtime (wake, wake+90m, sleep-1h). A part lasts until the next one starts. The volume is reset to the morning volume when you wake up; after that each part's change is added to whatever it is then, so your own adjustments are kept. Saved to ", h("code", {}, "day_plan.toml"), "."),
        h("div", { class: "day-settings" },
          field("Wake (weekdays)", "wake", { type: "time" }),
          field("Bedtime (weekdays)", "sleep", { type: "time" }),
          field("Wake (weekend)", "weekend_wake", { type: "time" }),
          field("Bedtime (weekend)", "weekend_sleep", { type: "time" }),
          field("Quiet after a stop (hours)", "quiet_after_stop_hours", { type: "number", min: 0, max: 24, step: 0.5 }),
          field("Keep music you chose (min)", "respect_requests_minutes", { type: "number", min: 0, max: 1440, step: 15 }),
          field("Check every (min)", "check_every_minutes", { type: "number", min: 1, max: 60 }),
          field("Morning volume (blank: no reset)", "morning_volume",
            { type: "number", min: 0, max: 100, optional: true, value: plan.morning_volume ?? "" })),
        h("div", { class: "day-slot day-head faint" }, h("span", {}, "Starts"), h("span", {}, "Days"), h("span", {}, "Mood"),
          h("span", {}, "Rooms"), h("span", {}, "Vol. ±"), h("span", {}, ""), h("span", {}, "")),
        slots,
        h("div", { class: "day-actions" },
          h("button", { class: "btn", onclick: () => { plan.slots.push({ start: "", mood: "", days: "all", rooms: [], quiet: false, volume_change: 0 }); drawSlots(); } },
            icon("plus"), "Add a part of the day"),
          h("span", { style: { flex: 1 } }),
          h("button", { class: "btn", onclick: () => renderEditor(structuredClone(data.plan)) }, icon("undo"), "Discard changes"),
          h("button", { class: "btn primary", onclick: save }, icon("check"), "Save")));
    }

    await load({ withEditor: true });
    return every(15000, () => load());
  },
};
