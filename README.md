# Claude Music DJ

Message a Discord bot ("put on something upbeat, I'm cooking") and a Claude
agent picks real tracks, finds them on Qobuz and plays them on your Sonos
speakers. It considers the time of day, weather and what you've liked or
skipped before, and learns from your feedback. Left to itself, it can also
keep fitting music playing through the day, following a rough plan you write.

There are three agents with different jobs:

- the **DJ** plays music and controls playback (Discord, the GUI's Now
  playing page, the terminal). For each request it settles on one genre or
  coherent theme and chooses within it by your taste, keeps to it for
  follow-ups for about an hour, now and then plays a whole celebrated album,
  and leaves the curator a note when a kind of request comes up often enough
  to deserve a playlist;
- the **curator** shapes your taste: it saves what you tell it ("I'm done with
  Coldplay"), runs listening quizzes, builds saved playlists the DJ can play
  in one step, and reviews and edits your day plan (the GUI's Chat page, or
  `chat --curator`);
- the **orchestrator** runs your day by itself: it follows `day_plan.toml`, a
  rough outline of the music you want when (wake-up, focus, dinner,
  wind-down...), reads your taste, and asks the DJ for a fitting style. Code
  checks the speakers every few minutes and only wakes it when something needs
  doing (nothing playing, the queue running out, a new part of the day). At
  bedtime it fades out and stops its own music (music you chose is left
  alone). It never stops your music, and after you stop or pause the
  music (from Discord, the GUI or the Sonos app) it stays quiet for a few
  hours; asking for music again ends that early.

The DJ and orchestrator run on Sonnet and the curator (used less often) on
Opus by default (`[agent]` in `config.toml`); the System page can switch each one.

```
GUI / Discord / terminal ──► Claude agents (Agent SDK, your Claude subscription)
day_plan.toml ──► orchestrator  each can only call its own list of tools
                          │
                          ▼
   DJ:           context · play_tracks · play_playlist · search · controls · feedback
   curator:      taste · preferences · feedback · playlists · quiz samples · day plan
   orchestrator: day plan · context · ask_dj (it never plays or stops music itself)
            │                  │                        │
   Qobuz search via Sonos   Sonos speakers (SoCo)   memory + playlists (SQLite)
```

## Quick start (Windows, PowerShell)

```powershell
cd C:\dev\Claude_music
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt   # this project + dependencies + test tools
python -m pytest                            # should end with "passed"
```

One-time setup:

1. **Claude**: install Claude Code and run `claude` once to log in with your
   Claude subscription. The agent uses that login (no API key).
2. **Music service**: `python -m music_agent.cli link` and sign in to Qobuz.
3. **Discord**: follow `docs/discord_setup.md`, then copy `.env.example` to
   `.env` and fill it in.
4. **Personal settings** (optional): put your town for weather in
   `config.local.toml`: `[location]` then `city = "Lyon"`.

Run it:

```powershell
python -m music_agent.gui             # control panel in your browser + the Discord bot (or: dj-gui)
python -m music_agent.discord_bot     # just the Discord bot (or: dj-bot)
python -m music_agent.chat            # talk to the DJ in the terminal (or: dj-chat; --curator for the curator)
python -m music_agent.cli --help      # direct control, no AI (or: dj)
```

## Using it

**GUI** (`python -m music_agent.gui`) opens http://127.0.0.1:8765 and also
starts the Discord bot and the orchestrator, so this one command runs
everything. (Don't run `dj-bot` at the same time, or two bots will answer and
two orchestrators will run the day. `--no-bot` starts neither.) Pages:

- **Now playing**: cover art, progress, play/pause/skip/stop, volume (up to the
  safety cap), 👍 / 👎, the queue, and your chat with the DJ (separate from
  Discord). 👍 is green while a song you've liked plays (liked anywhere: GUI,
  Discord or a quiz); click it again to take the like back. Each reply shows the tools Claude used. The **Speakers** card has a
  slider per room: set Night 5 quieter than the Dining Room and that balance is
  remembered, so the main slider and new DJ sets move all rooms together.
  "Level all" puts them back at one level. The DJ can do this too ("turn Night
  down a bit"). **Guest mode** ("Guests over") marks everything that plays for
  the next 4 hours as music for others, so it doesn't train your taste; the DJ
  also does this by itself for requests like "something for the party".
- **Chat**: the taste curator. Tell it what you like or don't ("more bossa
  nova in the evenings", "a bit less Radiohead lately"), say "quiz me" for a
  listening quiz (it plays samples, offers one-tap answers, and asks about
  patterns in what you skip), or ask for a playlist ("make a playlist for
  cooking dinner"). It keeps a notebook, so later quizzes build on earlier ones.
  What it concludes in a quiz becomes a tentative *finding* until enough
  samples back it (see Learning below). It can also look up a track's year,
  tempo, style tags and popularity.
- **Day**: the day plan and the orchestrator. Turn it on or off, see today's
  parts of the day (the current one highlighted), edit wake-up and bedtime
  (weekdays and weekend), how long to stay quiet after a stop, and each part of
  the day: when it starts (`14:00`, or relative like `wake+1h`, `sleep-90m`),
  which days, the mood in your words, rooms, a volume change, or "quiet" for
  no music. The volume is reset to the **morning volume** at the first check
  after you wake up, so last night's level never carries over; after that each
  part's change (e.g. +3) is added to whatever the volume is when it begins, so
  your own adjustments are kept. At bedtime the orchestrator's own music fades
  out. "Check now" runs a check immediately; "Recent activity" shows what it
  did and why, including volume changes. The plan is the file `day_plan.toml`,
  which you can also edit by hand or through the curator.
- **Playlists**: saved playlists. Play, shuffle or queue them; rename them and
  edit the description (the DJ reads it to decide when a playlist fits); add
  or remove tracks.
- **Taste**: everything that shapes the DJ, all editable:
  - your stated preferences (add, edit, remove), including how often a love
    should come up and when ("The Hu: rarely, at the gym");
  - the curator's quiz findings with the samples behind them (confirm once
    the evidence is enough, drop, reword);
  - a score for every artist and track, showing where it came from, with
    buttons to nudge it up or down, reset what was learned (history is kept),
    or save it as a stated love/avoid;
  - score adjustments, the curator's notebook (add notes, choose whether the
    DJ sees them) and every piece of feedback (edit or forget it, or add new);
  - the scoring settings: how much each signal counts, how fast feedback
    fades, and where "liked" and "avoided" begin;
  - what the DJ is resting because it played it a lot lately;
  - the raw profile Claude sees.
- **Database**: browse, search and edit every table in `music.db`. Click a cell
  to change it (lists for fixed choices, date pickers for times), add rows, ×
  deletes one. The same rules as everywhere else apply, so an edit can't leave
  something the agents can't read.
- **System**: Discord bot status with start/stop, each agent's model (applies
  from its next request), safety settings, and the live log.

`--no-bot` runs only the GUI (no bot and no orchestrator, so it's safe next to
a running bot), `--no-orchestrator` keeps the bot but doesn't follow the day
plan, `--no-browser` skips opening a tab, and `[gui]` in `config.toml` sets the
port. The GUI has no login, so it only listens on
this PC (`127.0.0.1`).

**Discord.** Only the user IDs in `DISCORD_ALLOWED_USER_IDS` are answered, in
`DISCORD_CHANNEL_ID` or by DM; everyone else is ignored.

- Normal messages go to Claude: "calm music in Night only", "more like this",
  "no more Coldplay", "love this".
- `!` commands are instant and use no Claude credit: `!now` `!queue` `!skip`
  `!pause` `!resume` `!stop` `!vol 20|+3|-3` `!new` `!taste` `!help`.
- Replies that started music get 👍 (like this track), 👎 (dislike and skip)
  and ⏭️ (skip) reactions.
- A conversation remembers earlier messages and restarts after 60 quiet
  minutes or 20 requests, so each request stays cheap. Your taste profile is
  kept.

**Terminal chat** (`python -m music_agent.chat`) shows every tool Claude calls,
then time, tokens and credit used. `/new` starts over, `/quit` exits.
`--model haiku` tries a faster model; `--curator` talks to the curator.

**CLI** (`python -m music_agent.cli`), no AI involved:

```powershell
python -m music_agent.cli rooms | now | queue | skip | pause | resume | stop
python -m music_agent.cli volume 20 | +3 | -5
python -m music_agent.cli play "Get Lucky" --artist "Daft Punk" [--rooms Night]
python -m music_agent.cli find "Cielito Lindo" --artist "Lila Downs" --explain
python -m music_agent.cli taste | feedback | forget 12     # what the agent has learned
python -m music_agent.cli info "Superstition" --artist "Stevie Wonder"   # year, tempo, tags, popularity
python -m music_agent.cli link | services                  # music service account
```

## How it works

| Piece | What it does |
|---|---|
| `music_agent/agent/tools.py` | The DJ's actions: get context, queue tracks, play a saved playlist, search, controls, volume, record feedback, suggest a playlist idea to the curator. |
| `music_agent/agent/curator_tools.py` | The curator's extra actions: read the taste profile and listening history, add/change/remove preferences, build quiz findings from evidence, look up track qualities, nudge or reset scores, write in its notebook, forget feedback, save playlists. |
| `music_agent/agent/dayplan_tools.py` | Day plan actions: read and change the plan (curator), read it and ask the DJ for music (orchestrator). |
| `music_agent/agent/prompt.py` | The agents' instructions in plain English. **Edit this to change their taste and behaviour.** |
| `music_agent/agent/server.py` | How each tool is described to Claude (names, descriptions, JSON schemas) and which tools each agent gets (`ROLE_TOOLS`). |
| `music_agent/agent/session.py` | Agent SDK setup: locks everything else down, runs the conversation, logs each request. `role` picks the DJ, the curator or the orchestrator, and each role's model. |
| `music_agent/providers/` | Finding music. `sonos_service.py` searches any service linked in the Sonos app; `base.py` tries progressively broader searches; `matching.py` picks the original recording over covers, karaoke and live versions. |
| `music_agent/players/sonos.py` | Speakers: grouping, queue, volume. Enforces the volume cap and quiet hours, and recovers from stale speaker state after sleep or network drops. |
| `music_agent/players/balance.py` | The per-room volume balance (saved in `music.db`), kept when the group volume changes. |
| `music_agent/learning/memory.py` | Memory (requests, plays, listens, feedback, preferences, findings, adjustments, notebook, settings) and the taste profile Claude sees before choosing. |
| `music_agent/learning/scoring.py` | How signals add up to a score per artist and track, and the tunable numbers (`TUNING`). |
| `music_agent/learning/playlists.py` | Saved playlists, stored with each track's catalogue ref so playing one needs no searching. |
| `music_agent/learning/database.py` | The shared SQLite file, plus the generic browse/search/add/edit the Database page uses, with per-table checks. |
| `music_agent/learning/watcher.py` | Notices tracks you skip early or hear to the end. |
| `music_agent/trackinfo.py` | Track qualities (original year, approximate tempo, style tags, popularity) from MusicBrainz and Deezer, no keys needed, cached in `music.db`. Only code goes online; the curator gets the results. |
| `music_agent/weather.py` | Local weather (Open-Meteo, no key). |
| `music_agent/dayplan.py` | The day plan file: reading, checking and saving it, and working out today's times (relative to waking up and bedtime). |
| `music_agent/orchestrator.py` | When to wake the orchestrator agent (checked in code every few minutes, so Claude is asked only when something needs doing), the volume through the day, and the bedtime fade-out. |
| `music_agent/runtime.py` | The long-running service the GUI and Discord share: one set of speakers, memory and watcher, the bot, the orchestrator, and the conversations. |
| `music_agent/gui/` | The control panel: `routes/` is the JSON API (one file per feature, browse it at `/docs`); `static/` is plain HTML/CSS/JS with one file per page and no build step. |
| `music_agent/cli.py`, `chat.py`, `discord_bot.py` | The other ways in. |
| `music_agent/app.py`, `config.py`, `models.py`, `logs.py`, `text.py` | Wiring, settings, shared data types, logging, text helpers. |

**Safety.** Claude has no shell, file or web access, and ignores your personal
Claude Code settings; it can only call the music tools. The volume cap and
quiet hours are enforced in the player code, so no message can talk it past
them.

**Learning.** The agent learns mostly from what you say (`record_feedback`,
👍 / 👎) and a little from what you skip. These add up to a score per artist
and track, and older signals fade, halving every 60 days (skips every 30). It
deliberately ignores some things:

- A track that played to the end counts for nothing by default: music often
  plays in the background.
- A skip counts mostly against that track, not the artist, and wears off.
- A quiz reaction counts fully for the sample but only a little for its
  artist, never makes the sample a regular, and never decides what you like
  at a time of day (that needs likes on several different days).
- Quiz samples, and music played for other people (guest mode, or the DJ
  noticing "for the party"), don't count at all.

The DJ also sees what it has played a lot in the last two weeks, and rests
those tracks and artists. On top of the scores:

- *Preferences* you state outright never fade and outrank everything else.
  They reach the DJ in your words. A love can say how often (regularly,
  sometimes, rarely, only when asked) and when ("at the gym"); anything but
  "regularly" keeps it out of the liked lists the DJ leans on.
- *Findings* are what the curator concludes from quizzes ("dislikes K-pop").
  They stay tentative (a soft hint) until the evidence passes the same bar for
  every genre: several samples from several artists, not all famous hits,
  mostly agreeing, and for a dislike one "salvage" try from another side of
  the genre. The code checks this; the curator can't skip it.
- *Adjustments* are nudges to a score ("+2 for Nina Simone") that never fade,
  and can *reset* what was learned about an artist or track: older feedback
  and listens are then ignored but kept in the history, so a reset can be undone.
- The curator's *notebook* is its memory between conversations; notes marked
  for the DJ reach it too. The DJ writes playlist ideas there (author "dj")
  when a genre is requested often and no saved playlist fits.
- Every weight and threshold is a setting on the Taste page (`scoring.py` has
  the defaults).

Delete `data\music.db` to start fresh (this also deletes saved playlists).

**Switching music service.** Set `[music] service = "Spotify"` (or any
service linked in your Sonos app) in `config.local.toml`, then run
`python -m music_agent.cli link`.

## Configuration

| File | Contents | In git? |
|---|---|---|
| `config.toml` | Shared defaults: service, rooms, each agent's model, safety limits, memory | yes |
| `day_plan.toml` | The day plan the orchestrator follows (also editable on the Day page) | yes |
| `config.local.toml` | Your overrides (town, speaker IPs...); wins over `config.toml` | no |
| `.env` | Discord token and IDs (template: `.env.example`) | no |
| `data/` | `music.db` (memory), `logs/gui.log`, `logs/bot.log`, `logs/agent.jsonl` (every request) | no |

Speakers not found? Put their IPs in `config.local.toml` under `[sonos]`:
`hosts = ["192.168.1.10", ...]`.

## Development

```powershell
python -m pytest          # tests use fake speakers and a scripted agent; no network
ruff check .              # lint
```

Tests mirror the package (`tests/test_player.py` covers `players/sonos.py`,
etc.); shared fakes and builders live in `tests/fakes.py`.

**Adding a GUI feature.** Add `music_agent/gui/routes/<feature>.py` with a
`router` and list it in `routes/__init__.py`. Then add
`music_agent/gui/static/js/pages/<feature>.js` and list it in
`static/js/pages/index.js`; it appears in the sidebar. Restart the GUI after
Python changes; HTML/CSS/JS changes only need a browser reload.

## Publishing the repo

Everything personal is git-ignored: `.env`, `config.local.toml`, `data/`,
`.venv/`. Before your first push, `git status --ignored` should list those as
ignored. If a token is ever committed or posted, reset it in the Discord
developer portal; deleting the file afterwards is not enough.

## Status

Done: speakers and search, the DJ, curator and orchestrator agents, memory and
taste learning, the Discord bot and the GUI.
Next: start automatically with Windows.
