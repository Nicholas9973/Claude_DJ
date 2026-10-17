# CLAUDE.md

Context for Claude Code working in this repo. Read README.md for the user-facing
overview; this file holds what a new session needs to work safely and quickly.

## What this is

A personal project: a Discord bot → Claude agent (Claude Agent SDK) → Sonos
speakers, playing from Qobuz. Three agent roles share one `MusicAgent` class:
the **DJ** (plays music; Discord, GUI Now playing page), the **curator**
(preferences, listening quizzes, saved playlists, the day plan; GUI Chat page)
and the **orchestrator** (runs by itself: follows `day_plan.toml` and asks the
DJ for music through `ask_dj`; GUI Day page). The owner is learning how Claude agents work with
multiple systems, so explain choices briefly and prefer clear code over clever code.

## Environment

- Windows, PowerShell, Python 3.13, venv at `.venv` (activate with
  `.\.venv\Scripts\Activate.ps1`). Repo at `C:\dev\Claude_music`.
- This PC is always on and on the same LAN as the speakers, so you can talk to
  the real speakers from here (`python -m music_agent.cli rooms`).
- Speakers: **Dining Room** (Sonos Beam), **Morn’** (One SL, note the curly
  apostrophe), **Night** (One SL). Default: all three grouped under Dining Room.
- Music service: Qobuz, linked through the Sonos household. The link token lives
  in SoCo's token store in the Windows user profile, not in the repo. Re-link
  with `python -m music_agent.cli link`.
- Claude auth: the bundled Claude Code inside `claude-agent-sdk` uses the
  owner's **subscription login** (Agent SDK monthly credit). `ANTHROPIC_API_KEY`
  must stay unset; `agent/session.py::use_subscription_login()` removes it.

## Commands

```powershell
python -m pip install -r requirements.txt   # = pip install -e .[dev] (pyproject.toml is the source of truth)
python -m pytest                            # all tests use fakes; no network, no speakers, no Claude
ruff check .                                # must stay clean
python -m music_agent.gui                   # GUI + bot (dj-gui); http://127.0.0.1:8765, logs to data/logs/gui.log
python -m music_agent.gui --no-bot --no-browser --port 8799   # safe for testing alongside a running bot (no orchestrator either)
python -m music_agent.discord_bot           # just the bot (dj-bot); logs to data/logs/bot.log
python -m music_agent.chat                  # DJ in the terminal, shows every tool call (dj-chat); --curator for the curator
python -m music_agent.cli --help            # no-AI control and diagnostics (dj)
python -m music_agent.cli find "Title" --artist "Artist" --explain   # debug a search miss
```

Stop a running bot before changing code it imports, then restart it.

## Layout

```
music_agent/
  agent/tools.py        the DJ's actions (plain async Python, returns dicts); also shared track lookup
  agent/curator_tools.py  the curator's extra actions: preferences, findings + quiz answers, track lookups,
                        score nudges, notebook, history, playlists
  agent/dayplan_tools.py  day plan actions: get/update the plan (curator), get it + ask_dj (orchestrator)
  agent/prompt.py       the agents' instructions in plain English (DJ, CURATOR, ORCHESTRATOR prompts)
  agent/server.py       tool names, descriptions, JSON schemas, and ROLE_TOOLS (which tools each role gets)
  agent/session.py      SDK options (lockdown) + conversation + per-request JSONL log; MusicAgent(app, role)
  providers/            search: sonos_service.py (SoCo MusicService), base.py (lookup cascade), matching.py (scoring)
  players/sonos.py      grouping, queue, volume, safety limits, stale-state recovery
  players/balance.py    per-room volume differences from the volume-source room (speaker_balance table)
  learning/memory.py    memory + taste profile (feedback fades; preferences, adjustments, notebook don't); watcher.py (skips)
  learning/scoring.py   how signals add up per artist/track; TUNING = defaults for the Taste page's settings
  learning/playlists.py saved playlists (tracks stored with refs, so playing needs no search)
  learning/database.py  the shared SQLite connection + generic browse/search/add/edit for the GUI; add_check() rules; ensure_column()
  trackinfo.py          track qualities from MusicBrainz + Deezer (no keys), cached in track_info; curator-only
  weather.py            Open-Meteo (no key), cached, optional
  dayplan.py            day_plan.toml: load/check/save, slot times relative to wake/sleep
  orchestrator.py       when to wake the orchestrator agent: decide() (pure) + the background loop
  text.py               norm() for loose name matching; duration formatting
  runtime.py            shared service: App + watcher + BotRunner + Orchestrator + named Conversations
                        (each with a role; "orchestrator" and "auto-dj" are the orchestrator's) + tools/curator
  gui/                  launcher.py (entry), server.py (FastAPI app), routes/ (JSON API, one file per feature),
                        static/ (plain HTML/CSS/JS ES modules, no build; pages/index.js lists the pages;
                        js/chat.js is the chat panel both agents' pages use)
  agent/describe.py     one-line summaries of tool calls/results (terminal chat + GUI)
  cli.py chat.py discord_bot.py   entry points (discord_bot.py also has BotRunner)
  app.py config.py models.py logs.py   wiring, settings, shared types, logging
tests/                  one file per module; shared fakes/builders in tests/fakes.py
```

## Rules (don't break these)

- **Agent lockdown:** `tools=[]`, `permission_mode="dontAsk"`, `setting_sources=[]`,
  `allowed_tools` = exactly the role's `ROLE_TOOLS` as `mcp__music__*`. Discord messages
  are untrusted input; no agent may get shell, file or web tools. `tests/test_session.py`
  checks this for every role. A new tool must be added to `ROLE_TOOLS` to be visible.
- **Safety in code, not prompt:** volume cap (35) and quiet hours (`[safety]` in config.toml, currently 23:59–08:00,
  mode "block") are enforced in `players/sonos.py`. The agent can't override
  them; only the CLI's `--force` can.
- **Secrets and personal data stay out of git:** `.env` (Discord token and IDs),
  `config.local.toml` (town etc., overrides `config.toml`), `data/` (music.db,
  logs). Don't print or read `.env` values; ask the owner if something there
  needs checking.
- **Provider-neutral core:** Claude only sees `MediaItem` refs like
  `qobuz|track|track:1068442:6`. Switching service = `[music] service` in config.
- Tests must not need network, speakers or Claude. Use `tests/fakes.py`
  (`make_player`, `make_tools`, `make_memory`, `FakeProvider`, fixed clocks).
- Absolute imports (`from music_agent.x import y`). Keep `ruff check .` clean.
- Google-style docstrings (`Args:`, `Returns:`, `Raises:`, `Attributes:`) on
  every public module, class and function; ruff's `D` rules enforce this.
  Constructor arguments go in the class docstring, not `__init__`.

- **GUI:** only on `127.0.0.1` (no login). Routes call `runtime.tools` /
  `runtime.curator` (same validation and safety as the agents) rather than the
  player directly, and let `ToolError`/`PlayerError`/`ValueError`/`sqlite3.IntegrityError`
  propagate; `gui/server.py` maps them to 400/503. GUI chats are named after their
  role (`dj`, `curator`). The runtime owns the playback watcher; front-ends must not start/stop it.
- **Schema changes:** tables use `CREATE TABLE IF NOT EXISTS`, so a new column on an
  existing table needs `db.ensure_column(...)` too (the owner's `music.db` already exists).
- **Editable data stays valid:** the GUI can edit any row, so every rule a Python method
  enforces must also be registered with `db.add_check(table, fn)`; time columns
  (`ts`, `created`, `updated`) are checked automatically. Readers of the data should still
  not crash on a bad value (see `Memory._weight`).
- **Taste model:** preferences (worded, never fade; a love has `frequency` + `context`),
  findings (curator's quiz conclusions + evidence; tentative until `check_finding` passes),
  feedback and listens (fade), adjustments (numeric nudge + optional reset that hides
  older signals), notebook (curator's memory; `for_dj` notes reach the DJ). Weights and
  thresholds live in `scoring.TUNING`, overridable in `taste_settings`. Don't hard-code
  a weight elsewhere.
- **What doesn't train the taste:** finished tracks (weight 0), quiz samples
  (`source='curator'`), music for others (`audience='others'`: the DJ's `for_others`,
  guest mode, or a queue started for others). Quiz feedback counts fully for the track,
  `quiz_feedback` for the artist, and never for liked tracks or time-of-day favourites.
  A listen gets its source/audience from the latest play of that title (`_play_for`).
- **Agents stay offline:** only `trackinfo.py` goes online, with title + artist, to two
  fixed hosts. Only the curator (GUI, 127.0.0.1) has `look_up_tracks`; keep it off the
  DJ, which reads Discord messages.
- **Checking the GUI visually:** Node isn't installed. Headless Edge works:
  `msedge --headless=new --screenshot=out.png --window-size=1440,900 --virtual-time-budget=6000 URL`
  (its minimum viewport is ~492px; narrower screenshots are cropped, not broken).

## Hard-won lessons

- **SoCo search-category race:** SoCo marks its category map loaded *before*
  downloading it, so parallel searches saw "Qobuz does not support 'tracks'".
  `SonosServiceProvider.categories()` loads it once under a lock and discards an
  empty map. Keep searches going through it.
- **Stale speaker state** after sleep or network drops gave `spk.group is None`
  and crashed `play`. Use `SonosPlayer._group_of()` (never `spk.group` directly)
  and keep `@_refresh_and_retry_once` only on operations that are safe to repeat
  (not `skip` or relative volume).
- **Search misses:** Qobuz returns only the top N versions of common titles.
  `lookup_track` tries artist+title, then title (25 results), then artist
  (50 results). On a miss it returns the artist's real tracks so Claude can
  substitute. Claude sometimes invents titles; the prompt forbids that and
  forbids pre-checking with `search_music` (that cost 14 calls once).
- **discord.py:** needs Message Content Intent. Voice warnings (PyNaCl/davey)
  and reconnect tracebacks are tidied by `logs.DiscordNoiseFilter`. `on_ready`
  fires on every reconnect.
- **Windows:** a venv breaks if the folder moves (recreate it). Python 3.13
  removed `audioop`; discord.py pulls in `audioop-lts`. The project used to live
  in OneDrive, which overwrote files; it's in `C:\dev` now for that reason.
- Real usage (2026-10-14 to 10-17, 108 requests, no failures): 96% of requested songs
  found. Since the genre-first DJ, a DJ request takes a median ~30 s and ~$0.08 of credit
  (Sonnet); an orchestrator run ~$0.05 plus its DJ request. Earlier (45 requests): ~5 s, ~$0.02.
- **UPnP error 800** on `add_to_queue` means the speaker won't play that item (e.g. not
  licensed here), though search lists it. `SonosPlayer.play` adds the ref to
  `provider.unplayable` (in-process) and `lookup_track` passes over it next time.
- **Volume balance:** group volume = the volume-source room's level; other rooms play at
  that plus their saved difference (`SpeakerBalance`), clamped to the cap per speaker.
  `set_room_volume` saves the current differences of all rooms. Use `_apply_volume` /
  `_group_level`, never set `spk.volume` for a group directly.
- **Taste JS:** `gui/static/js/taste/` holds the Taste page's cards (one module each);
  `pages/taste.js` only assembles them.
- **Preference wording matters:** a time of day on a *love* is rendered "(especially
  in the evening)", on an *avoid* "(in the morning only)". With "(evening only)" on a
  love, the curator excluded that artist at other times. Frequencies are worded in
  `FREQUENCY_WORDS`.
- **Taste rework (2026-10-12):** the profile used to reflect background listening and
  one long quiz evening: finished tracks scored +0.3 (enough to make background music
  a top "liked" artist), skips avoided a track forever, quiz likes became "liked tracks"
  in rotation, and "rarely" was faked with negative nudges. The DJ also picked by title
  words (sunshine songs for mornings, the same one 10× in two weeks). A backup from
  before is in `data/backups/` (git-ignored).
- **Deezer tempo** is often 0 or doubled; `plausible_tempo` halves >190 and drops the
  rest outside 50-190. MusicBrainz: ≤1 request/s and a User-Agent (enforced in `_mb_get`).
- **One genre per set (2026-10-14):** the DJ passes `genre` to its play tools; it's stored on
  `requests.genre`. `get_context` shows `current_genre` (held `GENRE_HOLD_MINUTES` after the
  last request choosing it) and `often_requested_genres_last_30_days`; the DJ's
  `suggest_playlist` writes a notebook note with author `dj` for the curator. Saved playlists
  are now used sparingly (only when named or an exact fit). Old `music.db` files get the
  notebook rebuilt once to allow `'dj'` (`db.widen_check`).
- **Models per role:** `[agent] model` (DJ, sonnet), `curator_model` (opus) and
  `orchestrator_model` (sonnet); the System page's choice (`agent_models` table) wins;
  `dj-chat --model` wins over both.
  `session.model_for(app, role)` resolves it; a change applies via `set_model` on the next request.
- **Orchestrator (2026-10-14):** code decides *when* (`orchestrator.decide`: nothing playing,
  ≤2 tracks left, a new slot unless the person's own music is < `respect_requests_minutes`
  old), Claude decides *what*. Each run starts fresh conversations ("orchestrator" and
  "auto-dj") to stay cheap; at most `MAX_RUNS_PER_DAY`. The auto-DJ's `MusicTools.source` is
  `"auto"`: its plays don't count as the person's choice and its stops don't start a quiet
  period. Anyone else's pause/stop calls `memory.start_quiet` (table `quiet_requests`); any
  non-auto play or resume ends it; a playing→paused change seen on the speakers counts too.
  Volume is handled in code (`Orchestrator._follow_volume`), never by an agent: reset to
  `morning_volume` at the first check of the day (only within `MORNING_RESET_HOURS` of waking),
  then each slot's `volume_change` is added relative to the current level, so the person's own
  changes are kept (the owner explicitly didn't want absolute per-slot volumes).
  At bedtime `decide` returns "bedtime" when the playing music is the orchestrator's
  (`memory.last_play()` source "auto"): `_fade_out` steps the volume down, stops, restores the
  volume; at most once per `FADE_ONCE_HOURS`. The person's own music is never faded.
  Only one process may orchestrate: `Runtime.start(orchestrate=...)`, off with `--no-bot`.
  CHECK constraints that gain values use `db.widen_check` (rebuilds the table, keeps rows).
- **Genres in the taste model:** listens copy the set's genre from their play's request
  (`listens.genre`, via `_play_for`), so `listening_history` has `by_genre` and `genres_chosen`
  (you vs. the day plan, told apart by the `[Day plan]` prefix). A "genre" is a coherent theme
  (may cross neighbouring genres, must work as one set), not a strict record-shop genre; the
  curator keeps preferences to one such theme each and quizzes within loved genres.
- **Database lock is an RLock** so a row check (e.g. confirming a finding) can query
  other tables inside `update_row`'s transaction.
