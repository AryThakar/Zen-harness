"""get_weather and generate_image against fake web services (httpx.MockTransport) - no network."""

from __future__ import annotations

import io
import time

import httpx
import pytest
from PIL import Image

from app.tools import weather
from app.tools.image import generate_image
from app.tools.weather import get_weather
from app.tools.workspace import Workspace

PLACES = {"results": [
    {"name": "Paris", "admin1": "Île-de-France", "country": "France", "country_code": "FR", "latitude": 48.85, "longitude": 2.35},
    {"name": "Paris", "admin1": "Texas", "country": "United States", "country_code": "US", "latitude": 33.66, "longitude": -95.55},
]}
FORECAST = {"utc_offset_seconds": 7200,
            "current": {"time": "2026-10-02T14:00", "temperature_2m": 18.4, "apparent_temperature": 17.0,
                        "weather_code": 2, "relative_humidity_2m": 60, "wind_speed_10m": 12.0, "uv_index": 3.4},
            "daily": {"time": ["2026-10-02", "2026-10-03"], "weather_code": [2, 63], "temperature_2m_min": [11, 10],
                      "temperature_2m_max": [19, 15], "precipitation_probability_max": [10, 80]}}
AIR = {"utc_offset_seconds": 7200, "current": {"us_aqi": 42, "pm2_5": 8.1, "pm10": 14.0}}


def fake(routes: dict, calls: list | None = None) -> httpx.Client:
    """A client whose answers come from routes: host -> response or callable(request)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        answer = routes.get(request.url.host)
        if callable(answer):
            return answer(request)
        if answer is None:
            return httpx.Response(503)
        return httpx.Response(200, json=answer)
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    weather._cache.clear()
    monkeypatch.delenv("ASSISTANT_HOME_LOCATION", raising=False)
    monkeypatch.setattr(time, "sleep", lambda s: None)  # retries without waiting


SERVICES = {"geocoding-api.open-meteo.com": PLACES, "api.open-meteo.com": FORECAST,
            "air-quality-api.open-meteo.com": AIR}


# ---------------------------------------------------------------- weather

def test_named_place_with_forecast():
    r = get_weather("Paris, France", days=2, http=fake(SERVICES))
    assert r.status == "ok"
    res = r.result
    assert res["place"] == "Paris, Île-de-France, France" and res["location_source"] == "named place"
    assert res["current"]["condition"] == "partly cloudy" and res["current"]["uv"] == "moderate"
    assert [d["condition"] for d in res["daily"]] == ["partly cloudy", "rain"]
    assert res["air_quality"] == {"us_aqi": 42, "pm2_5": 8.1, "pm10": 14.0, "category": "good"}
    assert "80% chance of rain" in res["summary"] and r.artifact["type"] == "weather"


def test_country_hint_picks_the_right_place():
    r = get_weather("Paris, Texas", http=fake(SERVICES))
    assert r.result["place"] == "Paris, Texas, United States"


def test_unknown_place_and_bad_days():
    r = get_weather("Atlantis", http=fake({**SERVICES, "geocoding-api.open-meteo.com": {"results": []}}))
    assert r.status == "invalid_arguments" and "No place called 'Atlantis'" in r.result["error"]["message"]
    assert get_weather("Paris", days=9, http=fake(SERVICES)).status == "invalid_arguments"


def test_missing_air_quality_is_partial_not_failed():
    routes = {**SERVICES, "air-quality-api.open-meteo.com": None}
    r = get_weather("Paris", http=fake(routes))
    assert r.status == "partial" and "air_quality" not in r.result
    assert "Air quality is unavailable" in r.result["warnings"][0] and r.result["current"]["temperature_c"] == 18.4


def test_stale_reading_is_labelled_when_the_service_is_down():
    assert get_weather("Paris", http=fake(SERVICES)).status == "ok"
    for key, (t, data) in list(weather._cache.items()):
        weather._cache[key] = (t - 3600, data)  # an hour old: past the 5-minute freshness
    down = {"geocoding-api.open-meteo.com": PLACES}
    r = get_weather("Paris", http=fake(down))
    assert r.result["current"]["reading_age"] == "60 min"
    assert r.result["summary"].split(". ")[1].startswith("Last available weather")
    assert any("showing the reading from 60 min ago" in w for w in r.result["warnings"])


def test_user_location_from_ip_or_home_setting(monkeypatch):
    ip = {"success": True, "city": "Vadodara", "region": "Gujarat", "country": "India",
          "latitude": 22.3, "longitude": 73.18, "timezone": {"offset": 19800}}
    r = get_weather(http=fake({**SERVICES, "ipwho.is": ip}))
    assert r.result["place"] == "Vadodara, Gujarat, India" and "internet address" in r.result["location_source"]
    r = get_weather(http=fake({**SERVICES, "ipwho.is": {"success": False}}))
    assert r.status == "failed" and "ask the user for a place" in r.result["error"]["message"]
    monkeypatch.setenv("ASSISTANT_HOME_LOCATION", "Paris")
    r = get_weather(http=fake(SERVICES))
    assert r.result["location_source"] == "home location setting"


# ---------------------------------------------------------------- image

def png_bytes(w=64, h=32) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "teal").save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path / "ws")


def test_image_is_checked_and_stored(ws):
    calls = []
    http = fake({"image.pollinations.ai": lambda req: httpx.Response(200, content=png_bytes(),
                                                                     headers={"content-type": "image/png"})}, calls)
    r = generate_image(ws, "  a teal   square ", size="wide", http=http)
    assert r.status == "ok" and r.result["image"] == "image_1" and (r.result["width"], r.result["height"]) == (64, 32)
    assert ws.path("image_1").read_bytes() == png_bytes() and r.artifact["prompt"] == "a teal square"
    url = calls[0].url
    assert url.raw_path.split(b"?")[0].endswith(b"/a%20teal%20square") and url.params["width"] == "1344"
    assert "prompt" not in r.result  # the model gets the id, not its own prompt back


def test_error_page_is_never_stored_as_an_image(ws):
    http = fake({"image.pollinations.ai": lambda req: httpx.Response(200, content=b"<html>busy</html>")})
    r = generate_image(ws, "a cat", http=http)
    assert r.status == "failed" and "not a valid image" in r.result["error"]["message"]
    assert ws.ids() == []


def test_rate_limit_is_retried_but_bad_requests_are_not(ws):
    answers = [httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200, content=png_bytes())]
    calls = []
    r = generate_image(ws, "a dog", http=fake({"image.pollinations.ai": lambda req: answers.pop(0)}, calls))
    assert r.status == "ok" and len(calls) == 2
    calls = []
    r = generate_image(ws, "a dog", http=fake({"image.pollinations.ai": lambda req: httpx.Response(400)}, calls))
    assert r.status == "failed" and len(calls) == 1
    assert generate_image(ws, "   ").status == "invalid_arguments"
