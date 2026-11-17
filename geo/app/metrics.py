"""Turns raw readings into the numbers the dashboard shows.

Two different signals answer two different questions:
  * STAYING crowd  -> BestTime venue busyness x dwell time (people inside places, and for how long)
  * MOVING traffic -> TomTom live speed vs free-flow speed (how the roads are flowing)
Hotspot cells combine both so a client can see *where* people gather vs where they just pass through.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .clients.demo import besttime_index
from .config import Settings
from .db import AreaSnapshot, LiveReading, TrafficReading, Venue, utcnow

M_PER_DEG_LAT = 111_320.0
FRC_WEIGHT = {"FRC0": 3.0, "FRC1": 3.0, "FRC2": 2.0, "FRC3": 1.5}
SURGE_DELTA = 15          # live vs forecast, percentage points


def size_weight(reviews: int | None) -> float:
    """Proxy for venue capacity: a 5,000-review mall should weigh more than a 40-review kiosk."""
    return 1.0 + math.log10(1 + (reviews or 0))


def venue_states(session: Session, settings: Settings) -> list[dict]:
    now_local = datetime.now(settings.tz)
    day, idx = besttime_index(now_local)
    fresh_after = utcnow() - timedelta(minutes=settings.live_poll_minutes * 2 + 5)

    latest_ids = (
        select(LiveReading.venue_id, func.max(LiveReading.id).label("mid"))
        .where(LiveReading.ts >= fresh_after)
        .group_by(LiveReading.venue_id)
        .subquery()
    )
    latest = {
        r.venue_id: r
        for r in session.scalars(select(LiveReading).join(latest_ids, LiveReading.id == latest_ids.c.mid))
    }

    out = []
    for v in session.scalars(select(Venue)):
        week = v.week
        fc_now = week[day][idx] if week else None
        r = latest.get(v.id)
        if r is not None and r.live_available and r.live_busyness is not None:
            busy, source, delta = r.live_busyness, "live", r.delta
            fc_now = r.forecast_busyness if r.forecast_busyness is not None else fc_now
        elif fc_now is not None:
            busy, source, delta = fc_now, "forecast", None
        else:
            busy, source, delta = None, "none", None
        today = week[day] if week else None
        out.append(
            {
                "id": v.id, "name": v.name, "address": v.address, "lat": v.lat, "lng": v.lng,
                "type": v.primary_type, "rating": v.rating, "reviews": v.user_rating_count,
                "busyness": busy, "forecast": fc_now, "delta": delta, "source": source,
                "dwell_min": v.dwell_min, "dwell_max": v.dwell_max, "dwell_avg": v.dwell_avg,
                "besttime_status": v.besttime_status,
                "stay_load": round((busy or 0) / 100 * (v.dwell_avg or 30) / 60 * size_weight(v.user_rating_count), 2),
                "today_curve": today,     # 24 values, index 0 = 6 AM
            }
        )
    return out


def latest_segments(session: Session) -> list[dict]:
    batch = session.scalar(select(TrafficReading.batch).order_by(TrafficReading.id.desc()).limit(1))
    if not batch:
        return []
    rows = session.scalars(select(TrafficReading).where(TrafficReading.batch == batch))
    return [
        {
            "key": r.segment_key, "frc": r.frc, "current_speed": r.current_speed,
            "free_flow_speed": r.free_flow_speed, "congestion": round(r.congestion, 3),
            "delay_s": max(0, r.current_travel_time - r.free_flow_travel_time),
            "road_closure": r.road_closure, "confidence": r.confidence,
            "coords": json.loads(r.coords_json), "ts": r.ts.isoformat() + "Z",
        }
        for r in rows
    ]


def area_summary(venues: list[dict], segments: list[dict]) -> dict:
    scored = [v for v in venues if v["busyness"] is not None]
    wsum = sum(size_weight(v["reviews"]) for v in scored)
    busy_idx = sum(min(v["busyness"], 150) * size_weight(v["reviews"]) for v in scored) / wsum if wsum else None
    live = [v for v in venues if v["source"] == "live"]
    surge = [v for v in live if (v["delta"] or 0) >= SURGE_DELTA]
    quiet = [v for v in live if (v["delta"] or 0) <= -SURGE_DELTA]
    dw = [(v["dwell_avg"], v["busyness"]) for v in scored if v["dwell_avg"] and v["busyness"]]
    avg_dwell = sum(d * b for d, b in dw) / sum(b for _, b in dw) if dw else None

    cw = sum(FRC_WEIGHT.get(s["frc"], 1.0) for s in segments)
    congestion = sum(s["congestion"] * FRC_WEIGHT.get(s["frc"], 1.0) for s in segments) / cw if cw else None
    avg_speed = sum(s["current_speed"] for s in segments) / len(segments) if segments else None
    avg_ff = sum(s["free_flow_speed"] for s in segments) / len(segments) if segments else None
    return {
        "busyness_index": round(busy_idx, 1) if busy_idx is not None else None,
        "venues_total": len(venues), "venues_live": len(live),
        "surge_venues": len(surge), "quieter_venues": len(quiet),
        "avg_dwell_min": round(avg_dwell) if avg_dwell else None,
        "congestion_pct": round(congestion * 100, 1) if congestion is not None else None,
        "avg_speed_kmh": round(avg_speed, 1) if avg_speed is not None else None,
        "free_flow_kmh": round(avg_ff, 1) if avg_ff is not None else None,
        "segments": len(segments),
        "jammed_segments": sum(1 for s in segments if s["congestion"] >= 0.5 or s["road_closure"]),
    }


def _xy(lat: float, lng: float, lat0: float, lng0: float) -> tuple[float, float]:
    return (lng - lng0) * M_PER_DEG_LAT * math.cos(math.radians(lat0)), (lat - lat0) * M_PER_DEG_LAT


def _ll(x: float, y: float, lat0: float, lng0: float) -> tuple[float, float]:
    return lat0 + y / M_PER_DEG_LAT, lng0 + x / (M_PER_DEG_LAT * math.cos(math.radians(lat0)))


def hotspots(venues: list[dict], segments: list[dict], settings: Settings, cell_m: int = 300) -> list[dict]:
    """Grid the area and label each cell: Hot zone / Crowd gathering / Pass-through traffic / Quiet."""
    lat0, lng0 = settings.area_lat, settings.area_lng
    cells: dict[tuple[int, int], dict] = {}

    def cell(x, y):
        k = (math.floor(x / cell_m), math.floor(y / cell_m))
        return cells.setdefault(k, {"load": 0.0, "dwell_w": 0.0, "busy_w": 0.0, "venues": [], "cong": []})

    for v in venues:
        if v["busyness"] is None:
            continue
        c = cell(*_xy(v["lat"], v["lng"], lat0, lng0))
        load = v["busyness"] / 100 * size_weight(v["reviews"])
        c["load"] += load
        c["busy_w"] += load
        c["dwell_w"] += load * (v["dwell_avg"] or 30)
        c["venues"].append((load, v["name"]))
    for s in segments:
        lat = sum(p[0] for p in s["coords"]) / len(s["coords"])
        lng = sum(p[1] for p in s["coords"]) / len(s["coords"])
        cell(*_xy(lat, lng, lat0, lng0))["cong"].append(s["congestion"])

    max_load = max((c["load"] for c in cells.values()), default=0) or 1
    out = []
    for (i, j), c in cells.items():
        crowd = c["load"] / max_load
        cong = sum(c["cong"]) / len(c["cong"]) if c["cong"] else 0.0
        dwell = c["dwell_w"] / c["busy_w"] if c["busy_w"] else None
        if crowd >= 0.45 and cong >= 0.35:
            label = "Hot zone"
        elif crowd >= 0.45:
            label = "Crowd gathering"
        elif cong >= 0.35:
            label = "Pass-through traffic"
        else:
            label = "Quiet"
        corners = [_ll(x * cell_m, y * cell_m, lat0, lng0) for x, y in ((i, j), (i + 1, j), (i + 1, j + 1), (i, j + 1), (i, j))]
        clat, clng = _ll((i + 0.5) * cell_m, (j + 0.5) * cell_m, lat0, lng0)
        out.append(
            {
                "id": f"{i}_{j}", "label": label, "score": round(0.7 * crowd + 0.3 * cong, 3),
                "crowd": round(crowd, 3), "congestion": round(cong, 3),
                "dwell_min": round(dwell) if dwell else None,
                "top_venues": [n for _, n in sorted(c["venues"], reverse=True)[:3]],
                "center": [clat, clng], "polygon": [[lng, lat] for lat, lng in corners],
            }
        )
    return sorted(out, key=lambda c: c["score"], reverse=True)


def trend(session: Session, hours: int = 24) -> list[dict]:
    since = utcnow() - timedelta(hours=hours)
    rows = session.scalars(select(AreaSnapshot).where(AreaSnapshot.ts >= since).order_by(AreaSnapshot.ts))
    return [
        {"ts": r.ts.isoformat() + "Z", "busyness_index": r.busyness_index, "congestion_pct": r.congestion_pct,
         "avg_speed": r.avg_speed, "surge_venues": r.surge_venues}
        for r in rows
    ]
