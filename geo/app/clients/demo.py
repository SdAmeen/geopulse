"""Synthetic stand-ins for the three APIs, used for any source whose API key is missing.

They return the same data objects as the real clients, so the rest of the app can't tell
the difference. Values are deterministic per venue and vary smoothly with the local time,
so the dashboard behaves like a real city: lunch/dinner peaks, evening rush hour, weekend malls.
"""
from __future__ import annotations

import hashlib
import math
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import FlowSegment, Forecast, Live, Place

M_PER_DEG_LAT = 111_320.0


def _h(s: str) -> int:
    return int(hashlib.md5(s.encode()).hexdigest()[:8], 16)


def _offset(lat: float, lng: float, dx_m: float, dy_m: float) -> tuple[float, float]:
    return lat + dy_m / M_PER_DEG_LAT, lng + dx_m / (M_PER_DEG_LAT * math.cos(math.radians(lat)))


# ---------------------------------------------------------------- venues

_NAMES = {   # fictional names only - the demo must never show made-up numbers next to real businesses
    "restaurant": ["Saffron Table", "Spice Route Kitchen", "The Clay Pot", "Tandoor Junction", "Coastal Plate", "Biryani Bay"],
    "cafe": ["Brew Theory", "Kettle & Leaf", "Third Wave Corner", "Bean There", "Mocha Lane"],
    "coffee_shop": ["Roast Republic", "Filter Kaapi Bar", "Morning Grind"],
    "fast_food_restaurant": ["Burger Stop", "Roll Express", "Pizza Pronto", "Momo Hub"],
    "shopping_mall": ["Riverside Galleria", "Lotus Square Mall", "Meridian Arcade"],
    "clothing_store": ["Handloom House", "Urban Threads", "Weave & Drape", "Denim Lab"],
    "supermarket": ["GreenCart Market", "Daily Needs Mart", "Harvest Basket"],
    "department_store": ["Mosaic Department Store", "Value Bazaar"],
    "bar": ["The Tap Room", "Skyline Lounge", "Hops & Barley"],
    "gym": ["Iron Temple Gym", "PulseFit Studio", "Core Club"],
    "movie_theater": ["Starlight Cinemas", "Reel Palace"],
    "park": ["Lakeview Garden", "Banyan Park", "Sunrise Green"],
}

DEMO_TYPES: dict[str, str] = {}   # venue name -> type (lets the demo forecast pick the right daily curve)


class DemoPlacesClient:
    async def search_nearby(self, lat: float, lng: float, radius_m: int, types: list[str]) -> list[Place]:
        out = []
        for t in types:
            for i, name in enumerate(_NAMES.get(t, [])):
                rnd = random.Random(_h(f"{t}:{name}:{lat:.3f}:{lng:.3f}"))
                # malls/markets cluster near the centre, others spread out
                spread = 0.45 if t in ("shopping_mall", "clothing_store", "department_store") else 0.9
                r = radius_m * spread * math.sqrt(rnd.random())
                a = rnd.random() * 2 * math.pi
                plat, plng = _offset(lat, lng, r * math.cos(a), r * math.sin(a))
                DEMO_TYPES[name] = t
                out.append(
                    Place(
                        place_id=f"demo_{_h(name):x}",
                        name=name,
                        address=f"{100 + i * 7}, Demo Road, {['Saheed Nagar','Jaydev Vihar','Patia','Unit 4'][i % 4]}",
                        lat=plat,
                        lng=plng,
                        primary_type=t,
                        types=[t, "point_of_interest", "establishment"],
                        rating=round(3.6 + rnd.random() * 1.3, 1),
                        user_rating_count=int(50 + rnd.random() ** 2 * 6000),
                    )
                )
        return out[:20]


# ---------------------------------------------------------------- foot traffic

# (local hour of peak, width in hours, weight) per venue type
_PROFILES: dict[str, list[tuple[float, float, float]]] = {
    "restaurant": [(13.5, 1.5, 0.8), (20.5, 1.8, 1.0)],
    "cafe": [(9.5, 1.5, 0.7), (18, 2.0, 1.0)],
    "coffee_shop": [(9, 1.5, 0.9), (17.5, 2.0, 0.8)],
    "fast_food_restaurant": [(13, 1.5, 0.8), (20, 2.5, 1.0)],
    "shopping_mall": [(13, 2.0, 0.55), (19, 2.5, 1.0)],
    "clothing_store": [(12.5, 2.0, 0.6), (19, 2.0, 1.0)],
    "department_store": [(13, 2.0, 0.6), (19, 2.0, 1.0)],
    "supermarket": [(11, 1.5, 0.7), (19, 1.8, 1.0)],
    "bar": [(21.5, 1.8, 1.0)],
    "gym": [(7, 1.2, 1.0), (19, 1.5, 0.9)],
    "movie_theater": [(15, 1.5, 0.6), (21, 1.5, 1.0)],
    "park": [(6.5, 1.0, 0.9), (18, 1.5, 1.0)],
}
_DWELL = {
    "restaurant": (35, 75), "cafe": (20, 60), "coffee_shop": (10, 35), "fast_food_restaurant": (10, 25),
    "shopping_mall": (60, 150), "clothing_store": (15, 40), "department_store": (25, 60),
    "supermarket": (15, 35), "bar": (60, 150), "gym": (45, 90), "movie_theater": (120, 180), "park": (30, 75),
}
_WEEKEND = {"shopping_mall": 1.35, "movie_theater": 1.3, "park": 1.25, "bar": 1.2, "restaurant": 1.15, "gym": 0.75}


def _hourly(t: str, day_int: int) -> list[float]:
    prof = _PROFILES.get(t, [(13, 2, 0.8), (19, 2, 1)])
    w = _WEEKEND.get(t, 1.0) if day_int >= 5 else 1.0
    vals = []
    for hr in range(24):
        v = sum(wt * math.exp(-((hr - pk) ** 2) / (2 * sd**2)) for pk, sd, wt in prof)
        if 7 <= hr <= 22:
            v += 0.06          # baseline trickle while open
        vals.append(v * w)
    return vals


def demo_week(t: str) -> list[list[int]]:
    """Week in BestTime layout: day_raw[0] is 6 AM."""
    days = [_hourly(t, d) for d in range(7)]
    peak = max(max(d) for d in days) or 1
    return [[round(100 * d[(i + 6) % 24] / peak) for i in range(24)] for d in days]


def besttime_index(local: datetime) -> tuple[int, int]:
    """(day_int, hour_index) in BestTime's 6 AM-based layout. Hours 0-5 belong to the previous day."""
    day = local.weekday()
    if local.hour < 6:
        day = (day - 1) % 7
    return day, (local.hour - 6) % 24


class DemoBestTimeClient:
    def __init__(self, tz: ZoneInfo):
        self.tz = tz

    async def forecast(self, venue_name: str, venue_address: str) -> Forecast:
        t = DEMO_TYPES.get(venue_name, "restaurant")
        lo, hi = _DWELL.get(t, (20, 60))
        return Forecast(ok=True, venue_id=f"btdemo_{_h(venue_name):x}", week=demo_week(t),
                        dwell_min=lo, dwell_max=hi, dwell_avg=(lo + hi) // 2)

    async def live(self, venue_id: str) -> Live:
        seed = _h(venue_id)
        name = next((n for n in DEMO_TYPES if f"btdemo_{_h(n):x}" == venue_id), None)
        t = DEMO_TYPES.get(name or "", "restaurant")
        now = datetime.now(self.tz)
        d, i = besttime_index(now)
        fc = demo_week(t)[d][i]
        if seed % 10 >= 7:     # ~30% of venues have no live signal, like the real API
            return Live(ok=True, live_available=False, forecast_busyness=fc)
        # smooth drift + a rotating "surge" so some venues are busier than usual
        slot = int(now.timestamp() // 1800)
        drift = 0.18 * math.sin(now.timestamp() / 5400 + seed % 97)
        surge = 0.45 if (slot + seed) % 9 == 0 else 0.0
        live = max(0, round(fc * (1 + drift + surge) + (3 if fc else 0)))
        lo, hi = _DWELL.get(t, (20, 60))
        return Live(ok=True, live_available=True, live_busyness=live, forecast_busyness=fc,
                    delta=max(-100, min(100, live - fc)), dwell_avg=(lo + hi) // 2)


# ---------------------------------------------------------------- road traffic

class DemoTomTomClient:
    """A handful of synthetic arterial roads crossing the area, split into ~350 m segments."""

    def __init__(self, lat: float, lng: float, radius_m: int, tz: ZoneInfo):
        self.tz = tz
        self.segments: list[tuple[str, str, float, list[tuple[float, float]]]] = []
        roads = [  # (name, frc, free-flow km/h, angle deg, offset m)
            ("NH-16 Service Rd", "FRC1", 55, 15, -350), ("Janpath", "FRC2", 45, 95, 120),
            ("Cuttack Rd", "FRC2", 42, 60, 450), ("Station Sq Link", "FRC3", 32, 140, -200),
            ("Market Lane", "FRC4", 25, 5, 380), ("Jaydev Vihar Rd", "FRC3", 35, 115, -600),
        ]
        for name, frc, ff, ang, off in roads:
            a = math.radians(ang)
            ux, uy = math.cos(a), math.sin(a)
            nx, ny = -uy, ux
            length = radius_m * 1.8
            steps = int(length / 350)
            pts = []
            for s in range(steps + 1):
                d = -length / 2 + s * 350
                wiggle = 25 * math.sin(s * 1.3 + ang)
                pts.append(_offset(lat, lng, ux * d + nx * (off + wiggle), uy * d + ny * (off + wiggle)))
            for s in range(len(pts) - 1):
                self.segments.append((f"{name}#{s}", frc, ff, [pts[s], pts[s + 1]]))

    def _congestion(self, seg_id: str, frc: str, at: datetime | None = None) -> float:
        now = at or datetime.now(self.tz)
        h = now.hour + now.minute / 60
        rush = 0.55 * math.exp(-((h - 10) ** 2) / 1.5) + 0.7 * math.exp(-((h - 18.5) ** 2) / 2.2)
        base = 0.08 + rush * (1.15 if frc in ("FRC1", "FRC2") else 0.8)
        if now.weekday() >= 5:
            base *= 0.7
        rnd = random.Random(_h(seg_id) + int(now.timestamp() // 900))
        return max(0.0, min(0.92, base + rnd.uniform(-0.08, 0.12)))

    async def flow(self, lat: float, lng: float) -> FlowSegment | None:
        best, best_d = None, 1e9
        for seg in self.segments:
            (alat, alng), (blat, blng) = seg[3]
            mlat, mlng = (alat + blat) / 2, (alng + blng) / 2
            dy = (mlat - lat) * M_PER_DEG_LAT
            dx = (mlng - lng) * M_PER_DEG_LAT * math.cos(math.radians(lat))
            d = math.hypot(dx, dy)
            if d < best_d:
                best, best_d = seg, d
        if best is None or best_d > 450:
            return None
        seg_id, frc, ff, coords = best
        c = self._congestion(seg_id, frc)
        cur = ff * (1 - c)
        length_m = 350
        return FlowSegment(
            current_speed=round(cur, 1), free_flow_speed=ff,
            current_travel_time=int(length_m / max(cur, 1) * 3.6), free_flow_travel_time=int(length_m / ff * 3.6),
            confidence=0.95, road_closure=False, frc=frc, coordinates=coords,
        )

    def all_points(self) -> list[tuple[float, float]]:
        """Midpoint of every synthetic segment, so the demo shows every road."""
        return [((s[3][0][0] + s[3][1][0]) / 2, (s[3][0][1] + s[3][1][1]) / 2) for s in self.segments]
