"""Plain data objects shared by the real API clients and the demo generators."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Place:
    place_id: str
    name: str
    address: str
    lat: float
    lng: float
    primary_type: str = ""
    types: list[str] = field(default_factory=list)
    rating: float | None = None
    user_rating_count: int | None = None


@dataclass
class Forecast:
    """BestTime weekly forecast. week[day_int][i]: day_int 0=Mon..6=Sun, i=0 is 6 AM ... i=23 is 5 AM next day."""
    ok: bool
    venue_id: str | None = None
    week: list[list[int]] | None = None
    dwell_min: int | None = None
    dwell_max: int | None = None
    dwell_avg: int | None = None
    message: str = ""


@dataclass
class Live:
    ok: bool
    live_available: bool = False
    live_busyness: int | None = None      # can exceed 100 (surge above weekly peak)
    forecast_busyness: int | None = None
    delta: int | None = None              # live - forecast, percent (-100..100)
    dwell_avg: int | None = None
    message: str = ""


@dataclass
class FlowSegment:
    """One TomTom road segment reading."""
    current_speed: float
    free_flow_speed: float
    current_travel_time: int
    free_flow_travel_time: int
    confidence: float
    road_closure: bool
    frc: str
    coordinates: list[tuple[float, float]]   # (lat, lng)

    @property
    def congestion(self) -> float:
        """0 = free flow, 1 = stopped."""
        if self.road_closure:
            return 1.0
        if self.free_flow_speed <= 0:
            return 0.0
        return max(0.0, min(1.0, 1 - self.current_speed / self.free_flow_speed))

    @property
    def key(self) -> str:
        """Stable id so that several sample points snapping to the same road are de-duplicated."""
        a, b = self.coordinates[0], self.coordinates[-1]
        return f"{a[0]:.5f},{a[1]:.5f}|{b[0]:.5f},{b[1]:.5f}"
