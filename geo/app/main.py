"""GeoPulse - live foot-traffic & road-flow intelligence for one city area.

Run:  uvicorn app.main:app --reload     then open http://localhost:8000
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import metrics
from .collector import Collector
from .config import get_settings
from .db import SessionLocal, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("geopulse")

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
AREA_FILE = ROOT / "area.json"          # area chosen in the dashboard; overrides .env and survives restarts
AREA_FIELDS = ("area_name", "area_lat", "area_lng", "area_radius_m", "timezone", "place_type_groups", "max_venues")

settings = get_settings()
if AREA_FILE.exists():
    try:
        for k, v in json.loads(AREA_FILE.read_text(encoding="utf-8")).items():
            if k in AREA_FIELDS:
                setattr(settings, k, v)
    except Exception as e:  # noqa: BLE001
        log.warning("ignoring unreadable %s: %s", AREA_FILE.name, e)
collector = Collector(settings)
scheduler = AsyncIOScheduler(timezone=settings.timezone)
_tasks: dict[str, asyncio.Task] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    scheduler.add_job(collector.discover, "interval", hours=settings.discovery_refresh_hours, id="discover")
    scheduler.add_job(collector.refresh_forecasts, "interval", hours=6, id="forecasts")   # only stale ones are refreshed
    scheduler.add_job(collector.poll_live, "interval", minutes=settings.live_poll_minutes, id="live")
    scheduler.add_job(collector.poll_traffic, "interval", minutes=settings.traffic_poll_minutes, id="traffic")
    scheduler.add_job(collector.snapshot, "interval", minutes=5, id="snapshot")
    scheduler.start()
    _tasks["boot"] = asyncio.create_task(collector.bootstrap())
    log.info("Sources -> Places: %s | BestTime: %s | TomTom: %s",
             *("LIVE" if x else "DEMO" for x in (settings.places_live, settings.besttime_live, settings.tomtom_live)))
    yield
    if "boot" in _tasks:
        _tasks["boot"].cancel()
    scheduler.shutdown(wait=False)


app = FastAPI(title="GeoPulse", version="1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
def config():
    return {
        "area_name": settings.area_name, "center": [settings.area_lat, settings.area_lng],
        "radius_m": settings.area_radius_m, "timezone": settings.timezone, "map_style_url": settings.map_style_url,
        "refresh_seconds": 60,
        "place_type_groups": settings.place_type_groups, "max_venues": settings.max_venues,
        "collecting": "boot" in _tasks and not _tasks["boot"].done(),
    }


class AreaIn(BaseModel):
    area_name: str = Field(min_length=1, max_length=120)
    area_lat: float = Field(ge=-90, le=90)
    area_lng: float = Field(ge=-180, le=180)
    area_radius_m: int = Field(ge=300, le=5000)
    place_type_groups: str = Field(min_length=1)
    max_venues: int = Field(ge=5, le=60)
    timezone: str


@app.post("/api/area")
async def set_area(a: AreaIn):
    """Switch the monitored area: wipes the old area's data and re-collects everything for the new one."""
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(a.timezone)
    except Exception:  # noqa: BLE001
        raise HTTPException(422, f"unknown timezone {a.timezone!r}")
    old = _tasks.get("boot")
    if old and not old.done():
        old.cancel()
    for k in AREA_FIELDS:
        setattr(settings, k, getattr(a, k))
    AREA_FILE.write_text(json.dumps({k: getattr(a, k) for k in AREA_FIELDS}, indent=2), encoding="utf-8")
    collector.reset_area()
    _tasks["boot"] = asyncio.create_task(collector.bootstrap())
    log.info("area changed -> %s (%.5f, %.5f) r=%dm", a.area_name, a.area_lat, a.area_lng, a.area_radius_m)
    return {"ok": True, "usage_estimate": collector.usage_estimate()}


@app.get("/api/geocode")
async def geocode(q: str):
    """Place search for the area picker: Google Places Text Search when a key is set, else OpenStreetMap Nominatim."""
    q = q.strip()
    if len(q) < 2:
        return []
    async with httpx.AsyncClient(timeout=10) as http:
        try:
            if settings.places_live:
                r = await http.post(
                    "https://places.googleapis.com/v1/places:searchText",
                    json={"textQuery": q, "pageSize": 6},
                    headers={"X-Goog-Api-Key": settings.google_places_api_key,
                             "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.location"},
                )
                r.raise_for_status()
                return [{"name": p.get("displayName", {}).get("text", ""), "address": p.get("formattedAddress", ""),
                         "lat": p["location"]["latitude"], "lng": p["location"]["longitude"]}
                        for p in r.json().get("places", []) if p.get("location")]
            r = await http.get("https://nominatim.openstreetmap.org/search",
                               params={"q": q, "format": "jsonv2", "limit": 6},
                               headers={"User-Agent": "GeoPulse/1.0 (area picker)"})
            r.raise_for_status()
            return [{"name": p.get("name") or p["display_name"].split(",")[0], "address": p["display_name"],
                     "lat": float(p["lat"]), "lng": float(p["lon"])} for p in r.json()]
        except httpx.HTTPError as e:
            raise HTTPException(502, f"place search failed: {e}") from e


@app.get("/api/status")
def status():
    return {
        "sources": {
            "google_places": "live" if settings.places_live else "demo",
            "besttime": "live" if settings.besttime_live else "demo",
            "tomtom": "live" if settings.tomtom_live else "demo",
        },
        "last_run": collector.last_run, "errors": collector.errors,
        "usage_estimate": collector.usage_estimate(),
    }


@app.get("/api/live")
def live():
    """Everything the dashboard needs in one call."""
    with SessionLocal() as db:
        venues = metrics.venue_states(db, settings)
        segments = metrics.latest_segments(db)
        return {
            "summary": metrics.area_summary(venues, segments),
            "venues": venues,
            "segments": segments,
            "hotspots": metrics.hotspots(venues, segments, settings),
            "trend": metrics.trend(db, 24),
            "updated_at": collector.last_run,
        }


@app.get("/api/venues/{venue_id}")
def venue(venue_id: str):
    with SessionLocal() as db:
        for v in metrics.venue_states(db, settings):
            if v["id"] == venue_id:
                return v
    raise HTTPException(404, "venue not found")


@app.post("/api/refresh/{job}")
async def refresh(job: str):
    jobs = {"discover": collector.discover, "forecasts": collector.refresh_forecasts,
            "live": lambda: collector.poll_live(force=True), "traffic": collector.poll_traffic}
    if job not in jobs:
        raise HTTPException(404, f"job must be one of {list(jobs)}")
    result = await jobs[job]()
    collector.snapshot()
    return {"job": job, "result": result}


@app.get("/api/export/venues.csv")
def export_csv():
    with SessionLocal() as db:
        venues = metrics.venue_states(db, settings)
    buf = io.StringIO()
    cols = ["name", "type", "address", "lat", "lng", "rating", "reviews", "busyness", "forecast", "delta",
            "source", "dwell_min", "dwell_max", "dwell_avg", "stay_load"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(sorted(venues, key=lambda v: v["busyness"] or -1, reverse=True))
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=geopulse_venues.csv"})
