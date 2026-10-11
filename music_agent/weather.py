"""Current local weather from Open-Meteo (free, no API key).

Optional: if the location isn't configured or the service is unreachable, the
agent just doesn't get weather. Results are cached for 15 minutes.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes, simplified.
WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
    77: "snow grains", 80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers", 95: "thunderstorm", 96: "thunderstorm with hail",
    99: "thunderstorm with hail",
}


def _get_json(url: str, params: dict[str, Any], timeout: float = 4.0) -> dict:
    """GET a URL with query parameters and decode the JSON response."""
    full = f"{url}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(full, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


@dataclass
class Weather:
    """Current conditions at one place.

    Attributes:
        place: Place name, e.g. ``"Lyon, France"``.
        description: Plain words, e.g. ``"light rain"``.
        temperature_c: Air temperature.
        is_day: False after sunset.
        precipitation_mm: Current precipitation.
        wind_kmh: Wind speed.
        sunrise: Local sunrise as ``"HH:MM"``.
        sunset: Local sunset as ``"HH:MM"``.
    """

    place: str
    description: str
    temperature_c: float
    is_day: bool
    precipitation_mm: float
    wind_kmh: float
    sunrise: str | None
    sunset: str | None

    def summary(self) -> str:
        """One line, e.g. ``"light rain, 12°C (dark outside)"``."""
        return f"{self.description}, {self.temperature_c:.0f}°C" + ("" if self.is_day else " (dark outside)")

    def as_dict(self) -> dict[str, Any]:
        """Compact form for the agent's context."""
        return {"place": self.place, "summary": self.summary(), "precipitation_mm": self.precipitation_mm,
                "wind_kmh": round(self.wind_kmh), "sunrise": self.sunrise, "sunset": self.sunset}


class WeatherService:
    """Fetches and caches the current weather for a configured place.

    Args:
        city: Town name, looked up once to get coordinates.
        latitude: Exact latitude, used instead of ``city``.
        longitude: Exact longitude, used instead of ``city``.
        cache_s: How long a good result is reused.
        retry_after_failure_s: How long to wait before retrying after a failure.
        fetch: HTTP JSON getter, replaceable in tests.
    """

    def __init__(self, city: str = "", latitude: float | None = None, longitude: float | None = None,
                 cache_s: int = 900, retry_after_failure_s: int = 120, fetch: Callable[[str, dict], dict] = _get_json):
        self.city = city.strip()
        self.lat, self.lon = latitude, longitude
        self.place = self.city or (f"{latitude:.2f},{longitude:.2f}" if latitude is not None else "")
        self.cache_s = cache_s
        self.retry_after_failure_s = retry_after_failure_s
        self._fetch = fetch
        self._cached: tuple[float, Weather | None] | None = None

    @property
    def configured(self) -> bool:
        """True if a city or coordinates were given."""
        return bool(self.city) or (self.lat is not None and self.lon is not None)

    def _resolve(self) -> bool:
        """Look up the city's coordinates if needed; False if the place is unknown."""
        if self.lat is not None and self.lon is not None:
            return True
        data = self._fetch(GEOCODE_URL, {"name": self.city, "count": 1, "format": "json"})
        results = data.get("results") or []
        if not results:
            log.warning("Weather: could not find a place called %r", self.city)
            return False
        r = results[0]
        self.lat, self.lon = r["latitude"], r["longitude"]
        self.place = ", ".join(x for x in (r.get("name"), r.get("country")) if x)
        return True

    def current(self) -> Weather | None:
        """Return the current weather, from cache when fresh.

        Returns:
            The weather, or None if not configured or unavailable. Never raises.
        """
        if not self.configured:
            return None
        if self._cached:
            age = time.monotonic() - self._cached[0]
            if age < (self.cache_s if self._cached[1] else self.retry_after_failure_s):
                return self._cached[1]
        weather = None
        try:
            if self._resolve():
                data = self._fetch(FORECAST_URL, {
                    "latitude": self.lat, "longitude": self.lon, "timezone": "auto", "forecast_days": 1,
                    "current": "temperature_2m,weather_code,is_day,precipitation,wind_speed_10m",
                    "daily": "sunrise,sunset",
                })
                cur, daily = data["current"], data.get("daily", {})
                weather = Weather(
                    place=self.place,
                    description=WMO.get(int(cur.get("weather_code", -1)), "unknown"),
                    temperature_c=float(cur["temperature_2m"]),
                    is_day=bool(cur.get("is_day", 1)),
                    precipitation_mm=float(cur.get("precipitation", 0) or 0),
                    wind_kmh=float(cur.get("wind_speed_10m", 0) or 0),
                    sunrise=_clock(daily.get("sunrise")),
                    sunset=_clock(daily.get("sunset")),
                )
        except Exception as exc:  # network down, API change: weather is optional
            log.info("Weather unavailable: %s", exc)
        self._cached = (time.monotonic(), weather)
        return weather


def _clock(times: list[str] | None) -> str | None:
    """``"HH:MM"`` from the first ISO timestamp in an Open-Meteo daily list."""
    return times[0][-5:] if times else None
