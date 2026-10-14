// The pages in the sidebar, in order. The first one is the home page.
//
// A page is a module whose default export looks like:
//   export default {
//     id: "now",              // URL: #/now
//     title: "Now playing",   // sidebar label
//     icon: "disc",           // name from ICONS in ../ui.js
//     async mount(el, { params }) { ...; return () => { /* stop timers */ }; },
//   };
// To add a page: create a file here and add it to this list.

import chat from "./chat.js";
import database from "./database.js";
import day from "./day.js";
import now from "./now.js";
import playlists from "./playlists.js";
import system from "./system.js";
import taste from "./taste.js";

export const PAGES = [now, chat, day, playlists, taste, database, system];
