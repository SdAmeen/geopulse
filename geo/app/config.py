"""Central configuration, loaded from environment variables / .env file."""
from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- API keys (leave blank to run that source in DEMO mode) ---
    google_places_api_key: str = ""
    besttime_api_key_private: str = ""
    tomtom_api_key: str = ""

    # --- Area being monitored ---
    area_name: str = "Bhubaneswar - Saheed Nagar"
    area_lat: float = 20.2961
    area_lng: float = 85.8245
    area_radius_m: int = 1500
    timezone: str = "Asia/Kolkata"

    # Google Places (New) types. Groups separated by ';' -> one Nearby Search call per group
    # (each call returns max 20 places, so groups let you collect more venues).
    place_type_groups: str = (
        "restaurant,cafe,coffee_shop,fast_food_restaurant;"
        "shopping_mall,clothing_store,supermarket,department_store;"
        "bar,gym,movie_theater,park"
    )
    max_venues: int = 40          # hard cap on venues tracked (controls BestTime credit use)

    # --- Polling schedule ---
    discovery_refresh_hours: int = 24     # Google Places re-scan
    forecast_refresh_days: int = 7        # BestTime weekly forecast (2 credits / venue)
    live_poll_minutes: int = 60           # BestTime live (1 credit / venue / call, data refreshes hourly)
    live_hours_start: int = 8             # only poll live between these local hours
    live_hours_end: int = 23
    max_live_venues: int = 25             # cap on venues polled live each cycle
    traffic_poll_minutes: int = 15        # TomTom flow polling
    traffic_grid_n: int = 4               # NxN sample grid over the area (points outside the circle are dropped)
    tomtom_zoom: int = 10                 # flowSegmentData zoom (road detail level)

    # --- Storage ---
    database_url: str = "sqlite:///./geopulse.db"
    history_hours: int = 72               # readings older than this are pruned

    # --- Map basemap (MapLibre style URL) ---
    map_style_url: str = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def type_groups(self) -> list[list[str]]:
        return [
            [t.strip() for t in group.split(",") if t.strip()]
            for group in self.place_type_groups.split(";")
            if group.strip()
        ]

    @property
    def places_live(self) -> bool:
        return bool(self.google_places_api_key)

    @property
    def besttime_live(self) -> bool:
        return bool(self.besttime_api_key_private)

    @property
    def tomtom_live(self) -> bool:
        return bool(self.tomtom_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
