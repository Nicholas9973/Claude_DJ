// Chat with the taste curator: change your preferences, take a listening quiz,
// or have it build playlists the DJ can then play in one go.
// (The DJ, for playing music, lives on the Now playing page.)

import { chatPanel } from "../chat.js";
import { h, icon } from "../ui.js";

const SUGGESTIONS = [
  "Quiz me to figure out my taste",
  "What do you know about my taste?",
  "Make a playlist for cooking dinner",
  "Build a calm focus playlist for work",
  "I'm done with Coldplay",
  "More bossa nova in the evenings",
];

export default {
  id: "chat",
  title: "Chat",
  icon: "chat",

  async mount(el, { params }) {
    const panel = chatPanel({
      conversation: "curator",
      placeholder: "Tell me what you like, ask for a quiz, or describe a playlist…",
      suggestions: SUGGESTIONS,
    });

    el.append(h("section", { class: "chat" },
      h("div", { class: "page-head" },
        h("div", {},
          h("h1", {}, "Taste chat"),
          h("p", {}, "Shape what the DJ knows about you: state preferences, take a listening quiz, or build ",
            h("a", { class: "link", href: "#/playlists" }, "playlists"), " it can play instantly. Everything saved shows up on the ",
            h("a", { class: "link", href: "#/taste" }, "Taste"), " page.")),
        h("button", { class: "btn small", onclick: () => panel.reset() }, icon("plus"), "New conversation")),
      panel.messages,
      panel.composer));

    await panel.load();
    const q = params.get("q");
    if (q) {
      history.replaceState(null, "", "#/chat"); // so a reload doesn't send it again
      panel.send(q);
    }
    panel.focus();
    return () => panel.destroy();
  },
};
