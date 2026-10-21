# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""get_weather: local time, weather, UV and air quality for a place - now, or up to 7 days ahead.

Ported from the Zen Rust environment tool (native_tools/environment.rs) and extended with a place
name and a daily forecast. Services: Open-Meteo for geocoding, weather and air quality (free, no
key) and, only when no place is given and ASSISTANT_HOME_LOCATION is unset, ipwho.is to locate the
computer by its IP address (city level, can be wrong behind a VPN - the result says which source
was used).

Truthful status: weather and air quality are fetched separately; if one fails the result is
'partial' and says what is missing. Readings are cached for 5 minutes; when a service is down a
reading up to 24 hours old is used and labelled as such, never presented as current.
"""

from __future__ import annotations

import os
import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import httpx

from . import net
from .core import ToolError, ToolResult, ok, tool

GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
AIR = "https://air-quality-api.open-meteo.com/v1/air-quality"
IP_LOCATION = "https://ipwho.is/"
CACHE_TTL = 5 * 60
MAX_STALE = 24 * 3600
MAX_DAYS = 7

CURRENT = "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,uv_index,precipitation"
DAILY = ("weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,"
         "uv_index_max,wind_speed_10m_max")

_cache: dict[tuple, tuple[float, dict]] = {}


@tool
def get_weather(location: str | None = None, days: int = 0, *, http: httpx.Client | None = None) -> ToolResult:
    """Weather for a place (default: the user's location): current conditions, and with days=1..7 a
    daily forecast for today and that many days ahead (days=1: today and tomorrow)."""
    if not 0 <= days <= MAX_DAYS:
        raise ToolError(f"days must be 0 to {MAX_DAYS}.", field="days")
    with (net.client() if http is None else nullcontext(http)) as client:  # its own client is closed after
        return _weather(client, location, days)


def _weather(http: httpx.Client, location: str | None, days: int) -> ToolResult:
    place = _place(http, location)
    warnings: list[str] = []
    weather, weather_age = _fetch(http, ("weather", place["latitude"], place["longitude"], days), FORECAST, {
        "latitude": place["latitude"], "longitude": place["longitude"], "current": CURRENT, "timezone": "auto",
        **({"daily": DAILY, "forecast_days": days + 1} if days else {})}, "weather", warnings)
    air, air_age = _fetch(http, ("air", place["latitude"], place["longitude"]), AIR, {
        "latitude": place["latitude"], "longitude": place["longitude"], "current": "us_aqi,pm2_5,pm10",
        "timezone": "auto"}, "air quality", warnings)

    offset = (weather or air or {}).get("utc_offset_seconds", place.get("utc_offset"))
    local = datetime.now(timezone.utc) + timedelta(seconds=offset) if offset is not None else datetime.now()
    result = {"place": _label(place), "location_source": place["source"],
              "local_time": local.strftime("%A, %d %B %Y, %H:%M")
                            + ("" if offset is not None else " (this computer's clock; the place's time zone is unknown)")}
    if weather:
        result["current"] = _current(weather.get("current", {}), weather_age)
        if days:
            result["daily"] = _daily(weather.get("daily", {}))
    if air:
        result["air_quality"] = _air(air.get("current", {}), air_age)
    result["summary"] = _summary(result)
    if warnings:
        result["warnings"] = warnings
    artifact = {"type": "weather", **{k: v for k, v in result.items() if k != "summary"}}
    return ok(result, artifact, partial=not (weather and air))


# ---------------------------------------------------------------- place

def _place(http: httpx.Client, location: str | None) -> dict:
    if location is None and os.environ.get("ASSISTANT_HOME_LOCATION"):
        place = _geocode(http, os.environ["ASSISTANT_HOME_LOCATION"])
        return {**place, "source": "home location setting"}
    if location is not None:
        if not location.strip():
            raise ToolError("location is empty; give a place name or leave it out for the user's location.",
                            field="location")
        return {**_geocode(http, location), "source": "named place"}
    try:
        data = net.get_json(http, IP_LOCATION)
    except net.NetError as exc:
        raise ToolError(f"Could not detect the user's location ({exc.message}); ask the user for a place.",
                        invalid=False) from exc
    lat, lon = data.get("latitude"), data.get("longitude")
    if data.get("success") is False or not _valid(lat, lon):
        raise ToolError("Could not detect the user's location; ask the user for a place.", invalid=False)
    tz = data.get("timezone") or {}
    return {"name": data.get("city"), "region": data.get("region"), "country": data.get("country"),
            "latitude": float(lat), "longitude": float(lon), "utc_offset": tz.get("offset"),
            "source": "detected from this computer's internet address (city level; can be wrong behind a VPN)"}


def _geocode(http: httpx.Client, text: str) -> dict:
    """'Pune' or 'Paris, France': search the first part, prefer a match on the rest."""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    try:
        data = net.get_json(http, GEOCODE, {"name": parts[0], "count": 10, "language": "en", "format": "json"})
    except net.NetError as exc:
        raise ToolError(f"Could not look up '{text}' ({exc.message}).", invalid=False) from exc
    hits = [h for h in data.get("results") or [] if _valid(h.get("latitude"), h.get("longitude"))]
    if not hits:
        raise ToolError(f"No place called '{text}' was found; check the spelling or add the country.",
                        field="location")
    rest = [p.casefold() for p in parts[1:]]
    if rest:
        def matches(h):
            names = {str(h.get(k, "")).casefold() for k in ("country", "admin1", "admin2", "country_code")}
            return all(any(r == n or r in n for n in names) for r in rest)
        hits = [h for h in hits if matches(h)] or hits
    h = hits[0]  # Open-Meteo ranks by population
    return {"name": h.get("name"), "region": h.get("admin1"), "country": h.get("country"),
            "latitude": float(h["latitude"]), "longitude": float(h["longitude"])}


def _valid(lat, lon) -> bool:
    return isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and -90 <= lat <= 90 and -180 <= lon <= 180


def _label(place: dict) -> str:
    return ", ".join(str(place[k]) for k in ("name", "region", "country") if place.get(k)) or "the detected location"


# ---------------------------------------------------------------- readings

def _fetch(http, key, url, params, what, warnings) -> tuple[dict | None, float | None]:
    """(data, age in seconds) - fresh, or a labelled stale copy when the service is down."""
    key = tuple(round(k, 2) if isinstance(k, float) else k for k in key)
    cached = _cache.get(key)
    now = time.time()
    if cached and now - cached[0] <= CACHE_TTL:
        return cached[1], now - cached[0]
    try:
        data = net.get_json(http, url, params)
        if not isinstance(data.get("current"), dict) or not any(v is not None for v in data["current"].values()):
            raise net.NetError("empty", f"the {what} service sent no readings", False)
    except net.NetError as exc:
        if cached and now - cached[0] <= MAX_STALE:
            age = now - cached[0]
            warnings.append(f"Live {what} is unavailable ({exc.message}); showing the reading from "
                            f"{_ago(age)} ago.")
            return cached[1], age
        warnings.append(f"{what.capitalize()} is unavailable ({exc.message}).")
        return None, None
    _cache[key] = (now, data)
    return data, 0.0


def _current(c: dict, age: float | None) -> dict:
    code = c.get("weather_code")
    out = {"temperature_c": c.get("temperature_2m"), "feels_like_c": c.get("apparent_temperature"),
           "condition": WMO.get(code) if code is not None else None, "humidity_pct": c.get("relative_humidity_2m"),
           "wind_kmh": c.get("wind_speed_10m"), "precipitation_mm": c.get("precipitation"),
           "uv_index": c.get("uv_index"), "observed": c.get("time")}
    if c.get("uv_index") is not None:
        out["uv"] = _uv(c["uv_index"])
    if age:
        out["reading_age"] = _ago(age)
    return {k: v for k, v in out.items() if v is not None}


def _daily(d: dict) -> list[dict]:
    rows = []
    for i, day in enumerate(d.get("time") or []):
        def at(name):
            values = d.get(name) or []
            return values[i] if i < len(values) else None
        code = at("weather_code")
        row = {"date": day, "condition": WMO.get(code) if code is not None else None,
               "min_c": at("temperature_2m_min"), "max_c": at("temperature_2m_max"),
               "rain_chance_pct": at("precipitation_probability_max"), "rain_mm": at("precipitation_sum"),
               "uv_max": at("uv_index_max"), "wind_max_kmh": at("wind_speed_10m_max")}
        rows.append({k: v for k, v in row.items() if v is not None})
    return rows


def _air(c: dict, age: float | None) -> dict:
    out = {"us_aqi": c.get("us_aqi"), "pm2_5": c.get("pm2_5"), "pm10": c.get("pm10")}
    if c.get("us_aqi") is not None:
        out["category"] = _aqi(c["us_aqi"])
    if age:
        out["reading_age"] = _ago(age)
    return {k: v for k, v in out.items() if v is not None}


def _summary(r: dict) -> str:
    parts = [f"{r['place']}, local time {r['local_time']}."]
    c = r.get("current")
    if c:
        bits = [f"{c['temperature_c']}°C" if "temperature_c" in c else None, c.get("condition"),
                f"feels like {c['feels_like_c']}°C" if "feels_like_c" in c else None,
                f"humidity {c['humidity_pct']}%" if "humidity_pct" in c else None,
                f"wind {c['wind_kmh']} km/h" if "wind_kmh" in c else None,
                f"UV {c['uv_index']} ({c['uv']})" if "uv" in c else None]
        label = "Last available weather" if "reading_age" in c else "Now"
        parts.append(f"{label}: " + ", ".join(b for b in bits if b) + ".")
    else:
        parts.append("Weather is unavailable.")
    a = r.get("air_quality")
    if a and "us_aqi" in a:
        parts.append(f"Air quality: AQI {a['us_aqi']} ({a['category']}).")
    for d in r.get("daily", []):
        parts.append(f"{d['date']}: {d.get('condition', '?')}, {d.get('min_c')}-{d.get('max_c')}°C"
                     + (f", {d['rain_chance_pct']}% chance of rain" if "rain_chance_pct" in d else "") + ".")
    return " ".join(parts)


def _ago(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h"


def _uv(uv: float) -> str:
    return "low" if uv < 3 else "moderate" if uv < 6 else "high" if uv < 8 else "very high" if uv < 11 else "extreme"


def _aqi(aqi: float) -> str:
    return ("good" if aqi <= 50 else "moderate" if aqi <= 100 else "unhealthy for sensitive groups" if aqi <= 150
            else "unhealthy" if aqi <= 200 else "very unhealthy" if aqi <= 300 else "hazardous")


WMO = {0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers", 81: "rain showers",
       82: "violent rain showers", 85: "snow showers", 86: "snow showers", 95: "thunderstorm",
       96: "thunderstorm with hail", 99: "thunderstorm with hail"}
