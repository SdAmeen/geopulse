"""Google Places API (New) - Nearby Search for competitor / venue discovery.

Docs: https://developers.google.com/maps/documentation/places/web-service/nearby-search
"""
from __future__ import annotations

import logging

import httpx

from .models import Place

log = logging.getLogger(__name__)

NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.location",
        "places.primaryType",
        "places.types",
        "places.rating",
        "places.userRatingCount",
    ]
)


def parse_places(payload: dict) -> list[Place]:
    out: list[Place] = []
    for p in payload.get("places", []) or []:
        loc = p.get("location") or {}
        if "latitude" not in loc or "longitude" not in loc:
            continue
        out.append(
            Place(
                place_id=p["id"],
                name=(p.get("displayName") or {}).get("text", "Unnamed"),
                address=p.get("formattedAddress", ""),
                lat=float(loc["latitude"]),
                lng=float(loc["longitude"]),
                primary_type=p.get("primaryType", ""),
                types=p.get("types", []) or [],
                rating=p.get("rating"),
                user_rating_count=p.get("userRatingCount"),
            )
        )
    return out


class GooglePlacesClient:
    def __init__(self, api_key: str, http: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self.http = http or httpx.AsyncClient(timeout=20)

    async def search_nearby(self, lat: float, lng: float, radius_m: int, types: list[str]) -> list[Place]:
        body = {
            "includedTypes": types,
            "maxResultCount": 20,
            "rankPreference": "POPULARITY",
            "locationRestriction": {
                "circle": {"center": {"latitude": lat, "longitude": lng}, "radius": float(min(radius_m, 50000))}
            },
        }
        headers = {"X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": FIELD_MASK}
        r = await self.http.post(NEARBY_URL, json=body, headers=headers)
        if r.status_code != 200:
            log.error("Places API %s: %s", r.status_code, r.text[:300])
            r.raise_for_status()
        return parse_places(r.json())
