"""Direct control without Claude: test speakers and search, inspect memory, link the account.

Examples:
    python -m music_agent.cli link                  (one-time: link your Qobuz/... account)
    python -m music_agent.cli services              (is the service available, can it search?)
    python -m music_agent.cli rooms
    python -m music_agent.cli search "daft punk" [--kind album]
    python -m music_agent.cli find "One More Time" --artist "Daft Punk"
    python -m music_agent.cli play "One More Time" --artist "Daft Punk" [--rooms "Night,Morn"]
    python -m music_agent.cli play "Discovery" --kind album
    python -m music_agent.cli add "Get Lucky" --artist "Daft Punk" [--next]
    python -m music_agent.cli now | queue | pause | resume | skip | stop
    python -m music_agent.cli volume 20 | +3 | -5
    python -m music_agent.cli taste | feedback | forget 12     (what the agent has learned)
    python -m music_agent.cli info "Superstition" --artist "Stevie Wonder" [--refresh]   (track qualities)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import datetime

from music_agent.app import App, build
from music_agent.logs import setup_logging
from music_agent.models import MediaItem, part_of_day
from music_agent.players.base import PlayerError
from music_agent.providers.base import ProviderError
from music_agent.text import format_track_time


def _resolve(app: App, args: argparse.Namespace) -> MediaItem:
    """Find the item a play/add command refers to; raise ProviderError if nothing matches."""
    if args.kind == "track" and args.artist:
        item = app.provider.find_track(args.query, args.artist)
    else:
        query = f"{args.artist} {args.query}" if args.artist else args.query
        results = app.provider.search(query, args.kind, 5)
        item = results[0] if results else None
    if item is None:
        raise ProviderError(f"Nothing found for '{args.query}'" + (f" by {args.artist}" if args.artist else ""))
    return item


def cmd_link(app: App, args: argparse.Namespace) -> None:
    """Link the music service account to this PC (one-time, interactive)."""
    info = app.provider.info()
    if info["auth"] not in ("DeviceLink", "AppLink"):
        print(f"{info['name']} uses '{info['auth']}' sign-in; no linking needed.")
        return
    url, code = app.provider.begin_link()
    print(f"1. Open this URL and sign in to your {info['name']} account:\n\n   {url}\n")
    if code:
        print(f"   (link code, if the page asks for one: {code})\n")
    input("2. Press Enter here once the page says you are linked... ")
    app.provider.complete_link()
    print("Linked. Try: python -m music_agent.cli search \"daft punk\"")


def cmd_services(app: App, args: argparse.Namespace) -> None:
    """Show the music service's sign-in type and search categories."""
    info = app.provider.info()
    print(f"{info['name']}: sign-in type {info['auth']}, service id {info['id']}")
    print(f"Search categories: {', '.join(app.provider.categories())}")


def cmd_rooms(app: App, args: argparse.Namespace) -> None:
    """List rooms and the active group."""
    for name in app.player.rooms():
        print(name)
    now = app.player.now_playing()
    print(f"\nActive group: {', '.join(now.rooms)} (volume {now.volume}, {now.state})")


def cmd_search(app: App, args: argparse.Namespace) -> None:
    """Search the catalogue and print results with their refs."""
    items = app.provider.search(args.query, args.kind, args.limit)
    for i, item in enumerate(items):
        print(f"[{i}] {item.label():<70} {format_track_time(item.duration_s):>6}  {item.ref}")
    if not items:
        print("No results.")


def cmd_find(app: App, args: argparse.Namespace) -> None:
    """Look up one song the way the agent does; --explain shows every search and score."""
    lk = app.provider.lookup_track(args.query, args.artist, args.album)
    if args.explain:
        for n, attempt in enumerate(lk.attempts, 1):
            status = f"ERROR {attempt.error}" if attempt.error else f"{len(attempt.results)} results"
            print(f"search {n}: {attempt.query!r} -> {status}")
            for sc, item in attempt.results[:args.show]:
                mark = "*" if sc > 0 else " "
                print(f"   {mark} {sc:5.1f}  {item.label()}")
        print()
    if lk.item:
        print(f"MATCH: {lk.item.label()}  [{format_track_time(lk.item.duration_s)}]  {lk.item.ref}")
    else:
        print("No confident match." + (f" Error: {lk.error}" if lk.error else ""))
        if lk.artist_tracks:
            print(f"Tracks by {args.artist} in the catalogue: " + "; ".join(i.title for i in lk.artist_tracks[:15]))


def cmd_play(app: App, args: argparse.Namespace) -> None:
    """Replace the queue with one track, album or playlist and start playing."""
    item = _resolve(app, args)
    result = app.player.play([item], rooms=args.rooms, mode="replace", force=args.force)
    print(f"Playing {item.label()} on {', '.join(result.rooms)} at volume {result.volume}")


def cmd_add(app: App, args: argparse.Namespace) -> None:
    """Add one track, album or playlist to the end of the queue, or next."""
    item = _resolve(app, args)
    result = app.player.play([item], mode="next" if args.next else "append", force=args.force)
    where = "next" if args.next else "at the end of the queue"
    print(f"Added {item.label()} {where} ({', '.join(result.rooms)})")


def cmd_now(app: App, args: argparse.Namespace) -> None:
    """Show what's playing."""
    n = app.player.now_playing()
    track = " - ".join(p for p in (n.title, n.artist) if p) or "nothing"
    print(f"{n.state}: {track}")
    print(f"  {n.position or '?'} / {n.duration or '?'}   queue {n.queue_position or '-'}/{n.queue_length}")
    print(f"  rooms: {', '.join(n.rooms)}   volume: {n.volume}")


def cmd_queue(app: App, args: argparse.Namespace) -> None:
    """List the queue."""
    for i, line in enumerate(app.player.queue(args.limit), 1):
        print(f"{i:>3}. {line}")


def cmd_volume(app: App, args: argparse.Namespace) -> None:
    """Set the volume: absolute (20) or relative (+3 / -5)."""
    value = args.level
    level = app.player.change_volume(int(value)) if value[0] in "+-" else app.player.set_volume(int(value))
    print(f"Volume {level} (cap {app.config.safety.volume_cap(datetime.now().time())})")


def cmd_taste(app: App, args: argparse.Namespace) -> None:
    """Print the taste profile the agent sees."""
    profile = app.memory.taste_profile(part_of_day(datetime.now().hour))
    print(json.dumps(profile, indent=2, ensure_ascii=False))


def cmd_feedback(app: App, args: argparse.Namespace) -> None:
    """List recent feedback with ids."""
    rows = app.memory.recent_feedback(args.limit)
    if not rows:
        print("No feedback yet.")
    for r in rows:
        mood = {1: "like", -1: "dislike", 0: "note"}[r["sentiment"]]
        target = {"track": f"{r['title']} - {r['artist']}", "artist": r["artist"], "general": ""}[r["scope"]]
        note = f'  "{r["note"]}"' if r["note"] else ""
        print(f"#{r['id']:<4} {r['ts'][:16]}  {mood:<7} {r['scope']:<7} {target}{note}")


def cmd_forget(app: App, args: argparse.Namespace) -> None:
    """Delete one feedback entry by id."""
    print("Deleted." if app.memory.forget_feedback(args.id) else f"No feedback #{args.id}.")


def cmd_info(app: App, args: argparse.Namespace) -> None:
    """Look up a track's year, tempo, tags and popularity (MusicBrainz, Deezer; cached)."""
    if not app.trackinfo:
        print("Track lookups need memory, which is turned off.")
        return
    print(json.dumps(app.trackinfo.look_up(args.title, args.artist, args.refresh), indent=2, ensure_ascii=False))


def _simple(action: str) -> Callable[[App, argparse.Namespace], None]:
    """Make a handler for a one-word player command (pause, resume, skip, stop)."""
    def run(app: App, args: argparse.Namespace) -> None:
        getattr(app.player, action)(**({"force": args.force} if action == "resume" else {}))
        print({"pause": "Paused.", "resume": "Resumed.", "skip": "Skipped.", "stop": "Stopped."}[action])
    return run


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser; each subcommand sets ``fn`` to its handler."""
    p = argparse.ArgumentParser(prog="music_agent.cli", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("link", help="link your music service account (one-time)").set_defaults(fn=cmd_link)
    sub.add_parser("services", help="check the music service").set_defaults(fn=cmd_services)
    sub.add_parser("rooms").set_defaults(fn=cmd_rooms)

    sp = sub.add_parser("search")
    sp.add_argument("query")
    sp.add_argument("--kind", default="track", choices=["track", "album", "playlist", "artist"])
    sp.add_argument("--limit", type=int, default=10)
    sp.set_defaults(fn=cmd_search)

    sp = sub.add_parser("find")
    sp.add_argument("query", help="track title")
    sp.add_argument("--artist")
    sp.add_argument("--album")
    sp.add_argument("--explain", action="store_true", help="show every search and how each result scored")
    sp.add_argument("--show", type=int, default=8, help="results to show per search with --explain")
    sp.set_defaults(fn=cmd_find)

    for name, fn in (("play", cmd_play), ("add", cmd_add)):
        sp = sub.add_parser(name)
        sp.add_argument("query")
        sp.add_argument("--artist")
        sp.add_argument("--kind", default="track", choices=["track", "album", "playlist"])
        sp.add_argument("--force", action="store_true", help="ignore quiet hours")
        if name == "play":
            sp.add_argument("--rooms", help='"all" (default) or comma list, e.g. "Night,Morn"')
        else:
            sp.add_argument("--next", action="store_true", help="play after the current track")
        sp.set_defaults(fn=fn)

    sub.add_parser("now").set_defaults(fn=cmd_now)
    sp = sub.add_parser("queue")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(fn=cmd_queue)
    for action in ("pause", "skip", "stop"):
        sub.add_parser(action).set_defaults(fn=_simple(action))
    sp = sub.add_parser("resume")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=_simple("resume"))
    sub.add_parser("taste", help="show what the agent has learned").set_defaults(fn=cmd_taste)
    sp = sub.add_parser("feedback", help="list recent feedback")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(fn=cmd_feedback)
    sp = sub.add_parser("forget", help="delete one feedback entry by #id")
    sp.add_argument("id", type=int)
    sp.set_defaults(fn=cmd_forget)
    sp = sub.add_parser("info", help="look up a track's year, tempo, tags and popularity online")
    sp.add_argument("title")
    sp.add_argument("--artist")
    sp.add_argument("--refresh", action="store_true", help="ignore the cached answer")
    sp.set_defaults(fn=cmd_info)
    sp = sub.add_parser("volume")
    sp.add_argument("level", help="absolute (20) or relative (+3 / -5)")
    sp.set_defaults(fn=cmd_volume)
    return p


def main(argv: list[str] | None = None) -> int:
    """Run one CLI command.

    Args:
        argv: Arguments; defaults to ``sys.argv[1:]``.

    Returns:
        Exit code: 0 on success, 1 on a player or provider error.
    """
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        args.fn(build(), args)
    except (PlayerError, ProviderError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
