"""Talk to the DJ (or the taste curator) in the terminal and watch it work.

In interactive mode, /new starts a fresh conversation and /quit exits.

Examples:
    python -m music_agent.chat                           # interactive
    python -m music_agent.chat "play something upbeat"   # one request, then exit
    python -m music_agent.chat --model haiku             # try another model
    python -m music_agent.chat --show-results            # print full tool output
    python -m music_agent.chat --curator                 # preferences, quizzes, playlists
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from music_agent.agent.describe import describe_call, describe_result
from music_agent.agent.session import Event, MusicAgent
from music_agent.app import build
from music_agent.logs import setup_logging

DIM, CYAN, RED, BOLD, RESET = "\033[2m", "\033[36m", "\033[31m", "\033[1m", "\033[0m"


def render(event: Event, show_results: bool) -> None:
    """Print one agent event to the terminal.

    Args:
        event: The event.
        show_results: Print full tool results instead of summaries.
    """
    if event.kind == "tool_call":
        print(f"{CYAN}  -> {describe_call(event.name, event.data)}{RESET}")
    elif event.kind == "tool_result":
        colour = RED if event.is_error else DIM
        body = event.data if show_results else describe_result(event.data)
        print(f"{colour}     {body}{RESET}")
    elif event.kind == "text":
        print(f"\n{event.data.strip()}\n")
    elif event.kind == "done":
        turn = event.data
        r = turn.result
        usage = r.usage or {}
        tokens_in = sum(usage.get(k, 0) or 0 for k in
                        ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        cost = f" · ~${turn.cost_usd:.3f} of credit" if turn.cost_usd else ""
        status = f"{RED}error: {r.subtype}{RESET} " if r.is_error else ""
        print(f"{DIM}  [{status}{r.num_turns} turns · {r.duration_ms / 1000:.1f}s · "
              f"{tokens_in / 1000:.1f}k in / {(usage.get('output_tokens') or 0) / 1000:.1f}k out{cost}]{RESET}")


async def run_one(agent: MusicAgent, text: str, show_results: bool) -> None:
    """Send one request and print everything that happens.

    Args:
        agent: Connected agent.
        text: The request.
        show_results: Print full tool results.
    """
    async for event in agent.ask(text):
        render(event, show_results)


async def main_async(args: argparse.Namespace) -> int:
    """Run one request, or the interactive loop, with the watcher running alongside.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code.
    """
    app = build()
    agent = MusicAgent(app, role="curator" if args.curator else "dj", model=args.model)
    print(f"{DIM}Starting the {agent.role} (model: {agent.model})...{RESET}")
    try:
        await agent.start()
        if app.watcher:
            app.watcher.start()  # learns from completed / skipped tracks while the chat is open
        if args.request:
            await run_one(agent, " ".join(args.request), args.show_results)
            return 0
        print(f"{BOLD}Ready.{RESET} Ask for music. /new = fresh conversation, /quit = exit.\n")
        while True:
            try:
                text = (await asyncio.to_thread(input, "you> ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            if text in ("/quit", "/exit"):
                break
            if text == "/new":
                await agent.reset()
                print(f"{DIM}(new conversation){RESET}")
                continue
            try:
                await run_one(agent, text, args.show_results)
            except Exception as exc:  # keep the chat alive
                print(f"{RED}Error: {exc}{RESET}")
    finally:
        if app.watcher:
            await app.watcher.stop()
        await agent.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m music_agent.chat``.

    Args:
        argv: Arguments; defaults to ``sys.argv[1:]``.

    Returns:
        Exit code.
    """
    p = argparse.ArgumentParser(prog="music_agent.chat", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("request", nargs="*", help="one request to run, then exit")
    p.add_argument("--model", help="override config and the GUI: sonnet / haiku / opus / full model id")
    p.add_argument("--show-results", action="store_true", help="print full tool results")
    p.add_argument("--curator", action="store_true", help="talk to the taste curator instead of the DJ")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    setup_logging(args.verbose)
    if sys.platform == "win32":
        os.system("")  # enable ANSI colours in older Windows consoles
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
