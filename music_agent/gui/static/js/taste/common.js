// Small building blocks shared by the Taste page's cards.

import { h } from "../ui.js";

export const SENTIMENTS = { like: "Love", dislike: "Avoid", neutral: "Neither" };
export const SENTIMENT_OF = { 1: "like", [-1]: "dislike", 0: "neutral" };
export const PARTS = { "": "Any time", morning: "Mornings", afternoon: "Afternoons", evening: "Evenings", night: "Nights" };
export const SOURCE_LABELS = { stated: "stated", feedback: "feedback", quiz: "quiz", listening: "listening", adjusted: "nudged" };
export const FREQUENCIES = { regular: "Regularly", sometimes: "Sometimes", rarely: "Rarely", on_request: "Only when asked" };

/** A titled card; `wide` makes it span the whole grid row, `top` adds space below it (outside a grid). */
export function card(title, content, { wide = false, top = false, hint = null } = {}) {
  const style = { ...(wide ? { gridColumn: "1 / -1" } : {}), ...(top ? { marginBottom: "16px" } : {}) };
  return h("div", { class: "card", style },
    h("h2", {}, title),
    hint ? h("p", { class: "muted hint" }, hint) : null,
    content);
}

export function select(options, value, props = {}) {
  const el = h("select", { class: "input", ...props },
    Object.entries(options).map(([v, label]) => h("option", { value: v }, label)));
  el.value = value;
  return el;
}

export function stat(value, label) {
  return h("div", { class: "stat" }, h("div", { class: "v" }, value), h("div", { class: "k" }, label));
}

export function section(label, content) {
  return h("div", { style: { marginBottom: "14px" } },
    h("div", { class: "muted", style: { fontSize: "13px", marginBottom: "6px" } }, label), content);
}

export function chips(items, kind) {
  if (!items || !items.length) return h("span", { class: "empty" }, "None yet");
  return h("div", { class: "chips" }, items.map((i) => h("span", { class: `chip ${kind}` }, i)));
}

export function list(items, emptyText) {
  if (!items || !items.length) return h("p", { class: "empty" }, emptyText);
  return h("ul", { class: "list" }, items.map((i) => h("li", {}, i)));
}

/** "+2.5" / "-1" / "0". */
export function signed(n) {
  const v = Math.round(n * 100) / 100;
  return v > 0 ? `+${v}` : `${v}`;
}

/** A text input that saves on Enter or when it loses focus (if changed). Esc puts the old value back. */
export function inlineInput(value, onSave, props = {}) {
  const original = value ?? "";
  const input = h("input", { class: "input inline", value: original, ...props });
  let saving = false;
  const save = async () => {
    if (saving || input.value === original) return;
    saving = true;
    await onSave(input.value);
    saving = false;
  };
  input.onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); input.blur(); }
    else if (e.key === "Escape") { input.value = original; input.blur(); }
  };
  input.onblur = save;
  return input;
}
