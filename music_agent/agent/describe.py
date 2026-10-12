"""One-line summaries of tool calls and results, for the terminal chat and the GUI."""

from __future__ import annotations

import json


def describe_call(name: str, args: dict) -> str:
    """Summarise a tool call in one line.

    Args:
        name: Tool name.
        args: Tool input.

    Returns:
        Text like ``play_tracks [replace] 12 tracks: A (X), B (Y), C (Z) +9 more``.
    """
    if name in ("play_tracks", "save_playlist"):
        tracks = args.get("tracks", [])
        first = ", ".join(f"{t.get('title')} ({t.get('artist')})" for t in tracks[:3])
        more = f" +{len(tracks) - 3} more" if len(tracks) > 3 else ""
        rooms = f", rooms={args['rooms']}" if args.get("rooms") else ""
        target = f" '{args.get('name')}'" if name == "save_playlist" else ""
        return f"{name}{target} [{args.get('mode', 'replace')}{rooms}] {len(tracks)} tracks: {first}{more}"
    if not args:
        return name
    return f"{name} {json.dumps(args, ensure_ascii=False)}"


def describe_result(text: str) -> str:
    """Summarise a tool result in one line (known shapes are summarised, others truncated).

    Args:
        text: The tool's JSON (or error) text.

    Returns:
        A short summary.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text[:200]
    nf = [m["requested"] if isinstance(m, dict) else str(m)
          for m in (data.get("not_found") or [] if isinstance(data, dict) else [])]
    missing = f"; not found: {', '.join(nf)}" if nf else ""
    if isinstance(data, dict) and "queued" in data and "tracks" in data:
        source = f"'{data['playlist']}': " if data.get("playlist") else ""
        return (f"{source}queued {data['queued']} ({data.get('total_duration')}) on {', '.join(data.get('rooms', []))}"
                f" at volume {data.get('volume')}" + missing)
    if isinstance(data, dict) and "total_tracks" in data:
        verb = "created" if data.get("created") else "updated"
        return (f"{verb} playlist '{data['playlist']}': +{data['saved']} tracks, "
                f"{data['total_tracks']} in total ({data.get('length')})" + missing)
    if isinstance(data, dict) and "preferences" in data and "profile" in data:
        return (f"{len(data['preferences'])} preferences, {len(data.get('recent_feedback', []))} recent feedback, "
                f"{len(data.get('adjustments', []))} adjustments, {len(data.get('notebook', []))} notes, "
                f"{len(data.get('saved_playlists', []))} playlists")
    if isinstance(data, dict) and data.get("saved") and "preference" in data:
        return f"saved preference #{data['id']}: {data['preference']}"
    if isinstance(data, dict) and data.get("updated") and "preference" in data:
        return f"updated preference #{data['id']}: {data['preference']}"
    if isinstance(data, dict) and "adjustment" in data and "score" in data:
        return f"{data['adjustment'] or 'adjustment removed'}; score now {data['score']:+g}"
    if isinstance(data, dict) and data.get("saved") and "note_id" in data:
        return f"wrote note #{data['note_id']}" + (" (shared with the DJ)" if data.get("shared_with_dj") else "")
    if isinstance(data, dict) and "listens" in data and "by_artist" in data:
        skipped = sum(1 for x in data["listens"] if x["outcome"] == "skipped")
        return (f"last {data['days']} days: {len(data['listens'])} listens ({skipped} skipped), "
                f"{len(data['dj_requests'])} DJ requests")
    if isinstance(data, dict) and "statement" in data and "status" in data:
        needs = f"; still needs: {', '.join(data['still_needs'])}" if data.get("still_needs") else ""
        return f"finding #{data['id']} ({data['status']}, {len(data.get('evidence', []))} samples): {data['statement']}{needs}"
    if isinstance(data, dict) and "feedback_id" in data:
        found = "; ".join(f"#{f['id']} {f['status']}" + (" (can confirm)" if f.get("can_confirm") else "")
                          for f in data.get("findings", []))
        return f"saved quiz answer #{data['feedback_id']}" + (f"; findings: {found}" if found else "")
    if isinstance(data, dict) and isinstance(data.get("tracks"), list) and data["tracks"] \
            and isinstance(data["tracks"][0], dict) and "status" in data["tracks"][0]:
        return "; ".join(f"{t.get('title', '?')}: " + (", ".join(str(x) for x in (t.get("year"), t.get("popularity"))
                                                                  if x) or t["status"])
                         for t in data["tracks"])
    if isinstance(data, dict) and "local_time" in data:
        pb = data.get("playback", {})
        taste = data.get("taste", {})
        hist = taste.get("history", {})
        weather = f"; {data['weather']['summary']}" if data.get("weather") else ""
        learned = (f"; taste: {len(taste.get('liked_artists', []))} liked / "
                   f"{len(taste.get('avoid_artists', []))} avoided artists, {len(taste.get('notes', []))} notes, "
                   f"{hist.get('tracks_played', 0)} plays" if taste else "")
        return (f"{data['weekday']} {data['local_time']} ({data['part_of_day']}){weather}; "
                f"{pb.get('state', '?')}: {pb.get('current') or 'nothing'}{learned}")
    if isinstance(data, dict) and data.get("saved"):
        return f"saved {data['sentiment']} about {data['about']}: {data['target']}"
    return text[:200]
