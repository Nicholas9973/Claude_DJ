// App shell: builds the sidebar from the page list and shows one page at a time.
// URLs look like #/chat?q=hello. Pages live in ./pages/ and are listed in ./pages/index.js.

import { api } from "./api.js";
import { PAGES } from "./pages/index.js";
import { every, h, icon } from "./ui.js";

const nav = document.getElementById("nav");
const main = document.getElementById("page");
let cleanup = null;

for (const page of PAGES) {
  nav.append(h("li", {}, h("a", { href: `#/${page.id}`, dataset: { page: page.id } }, icon(page.icon), page.title)));
}

async function route() {
  const [path, query] = location.hash.replace(/^#\/?/, "").split("?");
  const page = PAGES.find((p) => p.id === path) || PAGES[0];
  if (cleanup) cleanup();
  cleanup = null;
  main.replaceChildren();
  main.dataset.page = page.id;
  document.title = `${page.title} · Claude DJ`;
  for (const a of nav.querySelectorAll("a")) a.classList.toggle("active", a.dataset.page === page.id);
  try {
    // A page may return a function that stops its timers when you leave it.
    cleanup = (await page.mount(main, { params: new URLSearchParams(query || "") })) || null;
  } catch (err) {
    main.append(h("div", { class: "card" }, h("h2", {}, "Something went wrong"), h("p", { class: "muted" }, err.message)));
  }
}

// Discord bot status in the sidebar footer.
const pill = document.getElementById("bot-pill");
const LABELS = { online: "Discord online", connecting: "Discord connecting", not_configured: "Discord not set up",
  error: "Discord error", stopped: "Discord stopped", off: "Discord off" };
every(10000, async () => {
  try {
    const s = await api("/status");
    pill.replaceChildren(h("a", { class: "status-pill", href: "#/system", title: s.bot.detail || "" },
      h("span", { class: `dot ${s.bot.state}` }), LABELS[s.bot.state] || s.bot.state));
  } catch {
    pill.replaceChildren(h("span", { class: "status-pill" }, h("span", { class: "dot error" }), "GUI server offline"));
  }
});

window.addEventListener("hashchange", route);
route();
