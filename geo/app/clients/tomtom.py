"""TomTom Traffic Flow - Flow Segment Data (live speed vs free-flow speed per road segment).

Docs: https://developer.tomtom.com/traffic-api/documentation/traffic-flow/flow-segment-data
Free tier: 2,500 non-tile requests/day - see README for polling budget.
"""
from __future__ import annotations

import logging

import httpx

from .models import FlowSegment

log = logging.getLogger(__name__)

URL = "https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/{zoom}/json"


def parse_flow(payload: dict) -> FlowSegment | None:
    d = payload.get("flowSegmentData")
    if not d:
        return None
    coords = [(c["latitude"], c["longitude"]) for c in (d.get("coordinates") or {}).get("coordinate", [])]
    if len(coords) < 2:
        return None
    return FlowSegment(
        current_speed=float(d.get("currentSpeed", 0)),
        free_flow_speed=float(d.get("freeFlowSpeed", 0)),
        current_travel_time=int(d.get("currentTravelTime", 0)),
        free_flow_travel_time=int(d.get("freeFlowTravelTime", 0)),
        confidence=float(d.get("confidence", 0)),
        road_closure=bool(d.get("roadClosure", False)),
        frc=str(d.get("frc", "")),
        coordinates=coords,
    )


class TomTomClient:
    def __init__(self, api_key: str, zoom: int = 10, http: httpx.AsyncClient | None = None):
        self.key = api_key
        self.zoom = zoom
        self.http = http or httpx.AsyncClient(timeout=20)

    async def flow(self, lat: float, lng: float) -> FlowSegment | None:
        r = await self.http.get(
            URL.format(zoom=self.zoom),
            params={"key": self.key, "point": f"{lat},{lng}", "unit": "KMPH"},
        )
        if r.status_code in (400, 404):   # no road close to this sample point
            return None
        if r.status_code != 200:
            log.error("TomTom %s: %s", r.status_code, r.text[:200])
            return None
        return parse_flow(r.json())
