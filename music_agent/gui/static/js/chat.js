// A conversation with one of the agents. The Now playing page uses it for the DJ,
// the Chat page for the taste curator; each has its own history on the server.
//
// Each reply shows the tools Claude used, live, then folds them away. If a reply's
// last line is "Options: A | B | C", those become one-tap answer buttons (the
// curator does this in quizzes).

import { api, stream } from "./api.js";
import { ago, attempt, h, icon, markdown } from "./ui.js";

/**
 * Build a chat panel. Put `panel.messages` and `panel.composer` where you like,
 * then `await panel.load()`. Call `panel.destroy()` when the page goes away.
 *   conversation: "dj" | "curator"
 *   suggestions:  starter prompts shown while the conversation is empty
 *   onReply:      called after every finished reply (e.g. to refresh the queue)
 */
export function chatPanel({ conversation, placeholder = "Message…", suggestions = [], onReply = null }) {
  let busy = false;
  let alive = true;

  const messages = h("div", { class: "messages" });
  const input = h("textarea", {
    class: "input", rows: 1, placeholder,
    onkeydown: (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input.value); } },
    oninput: () => { input.style.height = "auto"; input.style.height = `${input.scrollHeight}px`; },
  });
  const sendBtn = h("button", { class: "btn primary", type: "submit", title: "Send" }, icon("send"));
  const composer = h("form", { class: "composer", onsubmit: (e) => { e.preventDefault(); send(input.value); } },
    input, sendBtn);

  const scrollDown = () => { if (alive) messages.scrollTop = messages.scrollHeight; };

  async function load() {
    const data = await api(`/chat?conversation=${conversation}`);
    messages.replaceChildren();
    data.history.forEach((msg, i) => {
      if (msg.role === "user") {
        messages.append(userBubble(msg.text));
        return;
      }
      const view = agentView(send);
      for (const step of msg.steps) step.type === "tool_call" ? view.call(step) : view.result(step);
      if (msg.text) view.text(msg.text);
      if (msg.error && !msg.text) view.error(msg.error);
      view.done({ cost_usd: msg.cost_usd, error: msg.error }, msg.seconds, msg.time,
        { withOptions: i === data.history.length - 1 });
      messages.append(view.el);
    });
    if (!data.history.length) showSuggestions();
    scrollDown();
  }

  async function send(text) {
    text = (text || "").trim();
    if (busy || !text) return;
    busy = true;
    sendBtn.disabled = true;
    input.value = "";
    input.style.height = "auto";
    messages.querySelectorAll(".suggestions, .quick-replies").forEach((el) => el.remove());
    messages.append(userBubble(text));
    const view = agentView(send);
    messages.append(view.el);
    scrollDown();
    const started = performance.now();
    try {
      for await (const item of stream("/chat", { text, conversation })) {
        if (item.type === "tool_call") view.call(item);
        else if (item.type === "tool_result") view.result(item);
        else if (item.type === "text") view.text(item.text);
        else if (item.type === "done") view.done(item, (performance.now() - started) / 1000);
        else if (item.type === "error") view.error(item.message);
        scrollDown();
      }
    } catch (err) {
      view.error(err.message);
    } finally {
      busy = false;
      sendBtn.disabled = false;
      scrollDown();
      if (alive) {
        input.focus();
        if (onReply) onReply();
      }
    }
  }

  async function reset() {
    if (busy) return;
    if (await attempt(() => api("/chat/reset", { method: "POST", body: { conversation } }))) {
      messages.replaceChildren();
      showSuggestions();
    }
  }

  function showSuggestions() {
    if (!suggestions.length) return;
    messages.append(h("div", { class: "suggestions" },
      suggestions.map((s) => h("button", { class: "btn", type: "button", onclick: () => send(s) }, s))));
  }

  return {
    messages, composer, load, send, reset,
    focus: () => input.focus(),
    destroy: () => { alive = false; },
  };
}

function userBubble(text) {
  return h("div", { class: "msg user" }, text);
}

/** Split off a trailing "Options: A | B | C" line. Returns [text, options]. */
function splitOptions(text) {
  const m = text.match(/(?:^|\n)[ \t]*\**Options:?\**[ \t]*:?[ \t]*([^\n]+?)[ \t]*$/i);
  if (!m) return [text, []];
  const options = m[1].split("|").map((o) => o.trim()).filter(Boolean);
  return options.length >= 2 ? [text.slice(0, m.index), options] : [text, []];
}

/** An agent reply that fills in as events arrive. `pick(text)` sends an option as the next message. */
function agentView(pick) {
  const label = h("span", {}, "Working…");
  const steps = h("div");
  const details = h("details", { class: "steps", open: true }, h("summary", {}, icon("tool"), label), steps);
  const reply = h("div", {}, h("span", { class: "typing" }, h("span"), h("span"), h("span")));
  const foot = h("div", { class: "foot" });
  const el = h("div", { class: "msg agent" }, h("div", { class: "body" }, details, reply), foot);
  let count = 0;
  let text = "";
  let options = [];

  return {
    el,
    call(item) {
      count += 1;
      label.textContent = `${count} step${count === 1 ? "" : "s"}`;
      steps.append(h("div", { class: "step" },
        h("div", {}, h("div", { class: "call" }, item.summary), h("div", { class: "result" }, "…"))));
    },
    result(item) {
      // Results arrive in call order; fill the first step still waiting.
      const step = steps.querySelector(".step:not(.done)");
      if (!step) return;
      step.classList.add("done");
      if (item.is_error) step.classList.add("error");
      step.querySelector(".result").textContent = item.summary;
    },
    text(t) {
      text = text ? `${text}\n\n${t}` : t;
      let shown;
      [shown, options] = splitOptions(text);
      reply.innerHTML = markdown(shown);
    },
    error(message) {
      el.classList.add("error");
      reply.textContent = message || "Something went wrong.";
      details.open = false;
    },
    done(item, seconds, time, { withOptions = true } = {}) {
      if (!text && !el.classList.contains("error")) reply.textContent = item.error ? "Something went wrong on my side." : "Done.";
      details.open = false;
      if (!count) details.remove();
      if (withOptions && options.length) {
        el.append(h("div", { class: "quick-replies" },
          options.map((o) => h("button", { class: "btn small", type: "button", onclick: () => pick(o) }, o))));
      }
      const bits = [];
      if (time) bits.push(ago(time));
      if (seconds) bits.push(`${seconds.toFixed(1)} s`);
      if (item.cost_usd) bits.push(`~$${item.cost_usd.toFixed(3)} credit`);
      foot.textContent = bits.join(" · ");
    },
  };
}
