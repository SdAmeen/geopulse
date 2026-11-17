"""BestTime.app foot-traffic API - weekly forecasts, live busyness and dwell time.

Docs: https://documentation.besttime.app/
Credits: new forecast = 2 (1 if it fails), live = 1 per call.
"""
from __future__ import annotations

import logging

import httpx

from .models import Forecast, Live

log = logging.getLogger(__name__)

BASE = "https://besttime.app/api/v1"


def _int_or_none(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_forecast(payload: dict) -> Forecast:
    if payload.get("status") != "OK":
        return Forecast(ok=False, message=str(payload.get("message", "forecast failed")))
    info = payload.get("venue_info") or {}
    analysis = payload.get("analysis") or []
    week: list[list[int]] = [[0] * 24 for _ in range(7)]
    for day in analysis:
        day_int = (day.get("day_info") or {}).get("day_int")
        raw = day.get("day_raw")
        if day_int is None or not raw:
            continue
        week[int(day_int)] = [int(x) for x in raw[:24]] + [0] * max(0, 24 - len(raw))
    return Forecast(
        ok=True,
        venue_id=info.get("venue_id"),
        week=week,
        dwell_min=_int_or_none(info.get("venue_dwell_time_min")),
        dwell_max=_int_or_none(info.get("venue_dwell_time_max")),
        dwell_avg=_int_or_none(info.get("venue_dwell_time_avg")),
    )


def parse_live(payload: dict) -> Live:
    if payload.get("status") != "OK":
        return Live(ok=False, message=str(payload.get("message", "live failed")))
    a = payload.get("analysis") or {}
    info = payload.get("venue_info") or {}
    live_ok = bool(a.get("venue_live_busyness_available"))
    fc_ok = bool(a.get("venue_forecast_busyness_available", True))
    return Live(
        ok=True,
        live_available=live_ok,
        live_busyness=_int_or_none(a.get("venue_live_busyness")) if live_ok else None,
        forecast_busyness=_int_or_none(a.get("venue_forecasted_busyness")) if fc_ok else None,
        delta=_int_or_none(a.get("venue_live_forecasted_delta")) if live_ok else None,
        dwell_avg=_int_or_none(info.get("venue_dwell_time_avg")),
    )


class BestTimeClient:
    def __init__(self, api_key_private: str, http: httpx.AsyncClient | None = None):
        self.key = api_key_private
        self.http = http or httpx.AsyncClient(timeout=60)

    async def forecast(self, venue_name: str, venue_address: str) -> Forecast:
        r = await self.http.post(
            f"{BASE}/forecasts",
            params={"api_key_private": self.key, "venue_name": venue_name, "venue_address": venue_address},
        )
        try:
            return parse_forecast(r.json())
        except ValueError:
            log.error("BestTime forecast bad response %s: %s", r.status_code, r.text[:200])
            return Forecast(ok=False, message=f"HTTP {r.status_code}")

    async def live(self, venue_id: str) -> Live:
        r = await self.http.post(f"{BASE}/forecasts/live", params={"api_key_private": self.key, "venue_id": venue_id})
        try:
            return parse_live(r.json())
        except ValueError:
            log.error("BestTime live bad response %s: %s", r.status_code, r.text[:200])
            return Live(ok=False, message=f"HTTP {r.status_code}")
