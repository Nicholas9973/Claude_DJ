"""Weather lookup (network faked)."""
from __future__ import annotations

from music_agent.weather import WeatherService
from tests.fakes import (
    fake_weather_fetch,
)


def test_weather_geocodes_caches_and_summarises():
    calls = []
    ws = WeatherService("Lyon", fetch=fake_weather_fetch(calls))
    w = ws.current()
    assert w.summary() == "light rain, 15°C (dark outside)"
    assert w.as_dict()["place"] == "Lyon, France" and w.sunset == "19:24"
    ws.current()
    assert len(calls) == 2


def test_weather_is_optional():
    assert WeatherService("").current() is None

    def broken(url, params):
        raise OSError("offline")
    assert WeatherService("Lyon", fetch=broken).current() is None


def test_failures_are_retried_sooner_than_successes(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("music_agent.weather.time.monotonic", lambda: now[0])
    state = {"up": False, "calls": 0}

    def fetch(url, params):
        state["calls"] += 1
        if not state["up"]:
            raise OSError("HTTP Error 503: Service Unavailable")
        return fake_weather_fetch([])(url, params)

    ws = WeatherService("Lyon", fetch=fetch)
    assert ws.current() is None
    state["up"] = True
    now[0] += 60
    assert ws.current() is None                      # still within the 2-minute retry window
    now[0] += 61
    assert ws.current() is not None                  # retried after 2 minutes
    calls = state["calls"]
    now[0] += 600
    ws.current()
    assert state["calls"] == calls                   # successes are cached for 15 minutes
