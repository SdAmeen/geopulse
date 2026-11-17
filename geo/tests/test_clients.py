"""Real-client tests against recorded response shapes (HTTP mocked with respx - no keys or network needed)."""
import httpx
import pytest
import respx

from app.clients.besttime import BestTimeClient, parse_forecast, parse_live
from app.clients.google_places import NEARBY_URL, GooglePlacesClient
from app.clients.tomtom import TomTomClient

PLACES_RESPONSE = {
    "places": [
        {
            "id": "ChIJabc",
            "displayName": {"text": "Esplanade One", "languageCode": "en"},
            "formattedAddress": "Rasulgarh, Bhubaneswar, Odisha 751010, India",
            "location": {"latitude": 20.2905, "longitude": 85.8601},
            "primaryType": "shopping_mall",
            "types": ["shopping_mall", "point_of_interest"],
            "rating": 4.4,
            "userRatingCount": 38211,
        },
        {"id": "no-location", "displayName": {"text": "Broken"}},
    ]
}

FORECAST_RESPONSE = {
    "status": "OK",
    "venue_info": {"venue_id": "ven_123", "venue_name": "Esplanade One", "venue_address": "Rasulgarh",
                   "venue_dwell_time_min": 60, "venue_dwell_time_max": 150, "venue_dwell_time_avg": 105},
    "analysis": [
        {"day_info": {"day_int": d}, "day_raw": [d * 10 + i % 5 for i in range(24)]} for d in range(7)
    ],
}

LIVE_RESPONSE = {
    "status": "OK",
    "analysis": {"venue_live_busyness": 112, "venue_live_busyness_available": True,
                 "venue_forecasted_busyness": 80, "venue_forecast_busyness_available": True,
                 "venue_live_forecasted_delta": 32, "hour_start": 18},
    "venue_info": {"venue_id": "ven_123", "venue_dwell_time_avg": 105},
}

TOMTOM_RESPONSE = {
    "flowSegmentData": {
        "frc": "FRC2", "currentSpeed": 18, "freeFlowSpeed": 45, "currentTravelTime": 140,
        "freeFlowTravelTime": 56, "confidence": 0.9, "roadClosure": False,
        "coordinates": {"coordinate": [{"latitude": 20.29, "longitude": 85.82}, {"latitude": 20.291, "longitude": 85.823}]},
    }
}


@pytest.mark.asyncio
@respx.mock
async def test_google_places_nearby():
    route = respx.post(NEARBY_URL).mock(return_value=httpx.Response(200, json=PLACES_RESPONSE))
    places = await GooglePlacesClient("KEY").search_nearby(20.29, 85.82, 1500, ["shopping_mall"])
    assert len(places) == 1
    p = places[0]
    assert (p.name, p.primary_type, p.user_rating_count) == ("Esplanade One", "shopping_mall", 38211)
    req = route.calls.last.request
    assert req.headers["X-Goog-Api-Key"] == "KEY"
    assert "places.location" in req.headers["X-Goog-FieldMask"]


@pytest.mark.asyncio
@respx.mock
async def test_besttime_forecast_and_live():
    respx.post("https://besttime.app/api/v1/forecasts").mock(return_value=httpx.Response(200, json=FORECAST_RESPONSE))
    respx.post("https://besttime.app/api/v1/forecasts/live").mock(return_value=httpx.Response(200, json=LIVE_RESPONSE))
    c = BestTimeClient("pri_key")
    f = await c.forecast("Esplanade One", "Rasulgarh")
    assert f.ok and f.venue_id == "ven_123" and f.dwell_avg == 105
    assert len(f.week) == 7 and len(f.week[3]) == 24 and f.week[3][0] == 30
    lv = await c.live("ven_123")
    assert lv.live_available and lv.live_busyness == 112 and lv.delta == 32 and lv.forecast_busyness == 80


def test_besttime_errors_and_no_live():
    assert not parse_forecast({"status": "Error", "message": "Venue not found"}).ok
    lv = parse_live({"status": "OK", "analysis": {"venue_live_busyness_available": False,
                                                   "venue_forecasted_busyness": 40}})
    assert lv.ok and not lv.live_available and lv.live_busyness is None and lv.forecast_busyness == 40


@pytest.mark.asyncio
@respx.mock
async def test_tomtom_flow():
    respx.get(url__regex=r"https://api\.tomtom\.com/traffic/services/4/flowSegmentData/.*").mock(
        side_effect=[httpx.Response(200, json=TOMTOM_RESPONSE), httpx.Response(400, text="Point too far from road")]
    )
    c = TomTomClient("KEY")
    seg = await c.flow(20.29, 85.82)
    assert seg.frc == "FRC2" and round(seg.congestion, 2) == 0.6
    assert await c.flow(0, 0) is None
