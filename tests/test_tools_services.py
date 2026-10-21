# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""get_weather and generate_image against fake web services (httpx.MockTransport) - no network."""

from __future__ import annotations

import io
import time

import httpx
import pytest
from PIL import Image

from app.tools import weather
from app.tools.image import file_name, generate_image
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


def test_days_count_days_ahead():
    calls = []
    get_weather("Paris, France", days=1, http=fake(SERVICES, calls))  # "tomorrow": the model writes days=1
    asked = [c for c in calls if c.url.host == "api.open-meteo.com"][0]
    assert asked.url.params["forecast_days"] == "2"  # today and tomorrow


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


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.delenv("POLLINATIONS_TOKEN", raising=False)
    monkeypatch.setenv("POLLINATIONS_KEY", "sk_test")


def test_image_is_checked_and_stored(ws, keyed):
    calls = []
    http = fake({"gen.pollinations.ai": lambda req: httpx.Response(200, content=png_bytes(),
                                                                   headers={"content-type": "image/png"})}, calls)
    r = generate_image(ws, "  a teal   square ", size="wide", http=http)
    assert r.status == "ok" and r.result["image"] == "image_1" and (r.result["width"], r.result["height"]) == (64, 32)
    assert ws.path("image_1").read_bytes() == png_bytes() and r.artifact["prompt"] == "a teal square"
    url = calls[0].url
    assert url.raw_path.split(b"?")[0] == b"/image/a%20teal%20square" and url.params["width"] == "1344"
    assert url.params["model"] == "zimage" and calls[0].headers["Authorization"] == "Bearer sk_test"
    assert "prompt" not in r.result  # the model gets the id, not its own prompt back
    assert ws.entry("image_1")["name"] == "a-teal-square.png"  # a name a script can open, with its extension
    long = file_name("A photo-real red fox, sitting in deep snow at dawn; soft light, 85mm lens", "jpg")
    assert long == "a-photo-real-red-fox-sitting-in-deep.jpg" and file_name("!!!", "png") == "image.png"


def test_a_generated_image_is_looked_at_so_main_can_check_the_request(ws, keyed):
    """The vision model describes the new picture and quotes its text; a failed look never costs the image."""
    http = fake({"gen.pollinations.ai": lambda req: httpx.Response(200, content=png_bytes(),
                                                                   headers={"content-type": "image/png"})})
    asked = []

    def look(image, mime, question):
        asked.append(question)
        return "A teal rectangle with the words 'HAPY BIRTHDAY'."

    r = generate_image(ws, "a teal card saying HAPPY BIRTHDAY", http=http, look=look)
    assert r.result["what_it_shows"] == "A teal rectangle with the words 'HAPY BIRTHDAY'."
    assert "quote it exactly" in asked[0] and "say so plainly" in r.result["check"]

    def broken(image, mime, question):
        raise RuntimeError("vision server down")

    r = generate_image(ws, "a teal card", http=http, look=broken)
    assert r.status == "ok" and "what_it_shows" not in r.result


def test_without_a_key_nothing_is_sent_and_the_user_is_told(ws, monkeypatch):
    monkeypatch.delenv("POLLINATIONS_KEY", raising=False)
    monkeypatch.delenv("POLLINATIONS_TOKEN", raising=False)
    calls = []
    r = generate_image(ws, "a cat", http=fake({"gen.pollinations.ai": lambda req: httpx.Response(200)}, calls))
    assert r.status == "failed" and "POLLINATIONS_KEY" in r.result["error"]["message"] and calls == []


def test_an_empty_balance_is_said_plainly(ws, keyed):
    r = generate_image(ws, "a cat", http=fake({"gen.pollinations.ai": lambda req: httpx.Response(402)}))
    assert r.status == "failed" and "no Pollen left" in r.result["error"]["message"]


def test_error_page_is_never_stored_as_an_image(ws, keyed):
    http = fake({"gen.pollinations.ai": lambda req: httpx.Response(200, content=b"<html>busy</html>")})
    r = generate_image(ws, "a cat", http=http)
    assert r.status == "failed" and "not a valid image" in r.result["error"]["message"]
    assert ws.ids() == []


def test_rate_limit_is_retried_but_bad_requests_are_not(ws, keyed):
    answers = [httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200, content=png_bytes())]
    calls = []
    r = generate_image(ws, "a dog", http=fake({"gen.pollinations.ai": lambda req: answers.pop(0)}, calls))
    assert r.status == "ok" and len(calls) == 2
    calls = []
    r = generate_image(ws, "a dog", http=fake({"gen.pollinations.ai": lambda req: httpx.Response(400)}, calls))
    assert r.status == "failed" and len(calls) == 1
    assert generate_image(ws, "   ").status == "invalid_arguments"
