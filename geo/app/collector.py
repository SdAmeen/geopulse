"""Polling jobs: discover venues (Google Places) -> forecasts + live busyness (BestTime) -> road flow (TomTom)."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select

from . import metrics
from .clients.besttime import BestTimeClient
from .clients.demo import DemoBestTimeClient, DemoPlacesClient, DemoTomTomClient, besttime_index
from .clients.google_places import GooglePlacesClient
from .clients.tomtom import TomTomClient
from .config import Settings
from .db import AreaSnapshot, LiveReading, SessionLocal, TrafficReading, Venue, utcnow

log = logging.getLogger("geopulse.collector")


def grid_points(lat: float, lng: float, radius_m: int, n: int) -> list[tuple[float, float]]:
    """n x n grid over the bounding square, keeping only points inside the circle."""
    pts = []
    step = 2 * radius_m / n
    for i in range(n):
        for j in range(n):
            dx = -radius_m + step * (i + 0.5)
            dy = -radius_m + step * (j + 0.5)
            if math.hypot(dx, dy) <= radius_m:
                pts.append((lat + dy / 111_320, lng + dx / (111_320 * math.cos(math.radians(lat)))))
    return pts


class Collector:
    def __init__(self, s: Settings):
        self.s = s
        self.last_run: dict[str, str] = {}
        self.errors: dict[str, str] = {}
        self.build_clients()

    def build_clients(self) -> None:
        """(Re)create the API clients - called again when the area or timezone changes."""
        s = self.s
        self.places = GooglePlacesClient(s.google_places_api_key) if s.places_live else DemoPlacesClient()
        self.besttime = BestTimeClient(s.besttime_api_key_private) if s.besttime_live else DemoBestTimeClient(s.tz)
        self.tomtom = (
            TomTomClient(s.tomtom_api_key, s.tomtom_zoom) if s.tomtom_live
            else DemoTomTomClient(s.area_lat, s.area_lng, s.area_radius_m, s.tz)
        )

    def reset_area(self) -> None:
        """Forget everything collected for the previous area."""
        with SessionLocal() as db:
            for model in (LiveReading, TrafficReading, AreaSnapshot, Venue):
                db.execute(delete(model))
            db.commit()
        self.last_run.clear()
        self.errors.clear()
        self.build_clients()

    def _mark(self, job: str, err: Exception | None = None):
        self.last_run[job] = utcnow().isoformat() + "Z"
        if err:
            self.errors[job] = f"{type(err).__name__}: {err}"[:300]
        else:
            self.errors.pop(job, None)

    # ------------------------------------------------------------- Google Places
    async def discover(self) -> int:
        try:
            found = {}
            for group in self.s.type_groups:
                for p in await self.places.search_nearby(self.s.area_lat, self.s.area_lng, self.s.area_radius_m, group):
                    found[p.place_id] = p
            ranked = sorted(found.values(), key=lambda p: p.user_rating_count or 0, reverse=True)[: self.s.max_venues]
            with SessionLocal() as db:
                for p in ranked:
                    v = db.get(Venue, p.place_id) or Venue(id=p.place_id)
                    v.name, v.address, v.lat, v.lng = p.name, p.address, p.lat, p.lng
                    v.primary_type, v.types_json = p.primary_type, json.dumps(p.types)
                    v.rating, v.user_rating_count, v.updated_at = p.rating, p.user_rating_count, utcnow()
                    db.merge(v)
                db.commit()
            log.info("discover: %d venues tracked", len(ranked))
            self._mark("discover")
            return len(ranked)
        except Exception as e:  # noqa: BLE001
            log.exception("discover failed")
            self._mark("discover", e)
            return 0

    # ------------------------------------------------------------- BestTime forecasts
    async def refresh_forecasts(self) -> int:
        stale = utcnow() - timedelta(days=self.s.forecast_refresh_days)
        with SessionLocal() as db:
            todo = list(db.scalars(select(Venue).where(or_(Venue.forecast_updated_at.is_(None), Venue.forecast_updated_at < stale))))
        sem = asyncio.Semaphore(4)

        async def one(v: Venue):
            async with sem:
                try:
                    return v.id, await self.besttime.forecast(v.name, v.address)
                except Exception as e:  # noqa: BLE001
                    log.warning("forecast %s failed: %s", v.name, e)
                    return v.id, None

        results = await asyncio.gather(*(one(v) for v in todo))
        ok = 0
        with SessionLocal() as db:
            for vid, f in results:
                v = db.get(Venue, vid)
                v.forecast_updated_at = utcnow()          # also on failure, so we don't burn credits retrying hourly
                if f and f.ok:
                    ok += 1
                    v.besttime_status, v.besttime_venue_id = "ok", f.venue_id
                    v.forecast_json = json.dumps(f.week)
                    v.dwell_min, v.dwell_max, v.dwell_avg = f.dwell_min, f.dwell_max, f.dwell_avg
                    v.live_misses = 0
                else:
                    v.besttime_status = "failed"
            db.commit()
        log.info("forecasts: %d/%d ok", ok, len(todo))
        self._mark("forecasts")
        return ok

    # ------------------------------------------------------------- BestTime live
    async def poll_live(self, force: bool = False) -> int:
        hour = datetime.now(self.s.tz).hour
        if self.s.besttime_live and not force and not (self.s.live_hours_start <= hour < self.s.live_hours_end):
            return 0
        recheck = utcnow() - timedelta(hours=24)
        with SessionLocal() as db:
            q = (
                select(Venue)
                .where(Venue.besttime_venue_id.is_not(None))
                .where(or_(Venue.live_misses < 3, Venue.live_checked_at.is_(None), Venue.live_checked_at < recheck))
                .order_by(Venue.user_rating_count.desc())
                .limit(self.s.max_live_venues)
            )
            todo = [(v.id, v.besttime_venue_id) for v in db.scalars(q)]
        sem = asyncio.Semaphore(5)

        async def one(vid, btid):
            async with sem:
                try:
                    return vid, await self.besttime.live(btid)
                except Exception as e:  # noqa: BLE001
                    log.warning("live %s failed: %s", vid, e)
                    return vid, None

        results = await asyncio.gather(*(one(*t) for t in todo))
        n = 0
        with SessionLocal() as db:
            for vid, lv in results:
                if not lv or not lv.ok:
                    continue
                v = db.get(Venue, vid)
                v.live_checked_at = utcnow()
                v.live_misses = 0 if lv.live_available else v.live_misses + 1
                if lv.dwell_avg and not v.dwell_avg:
                    v.dwell_avg = lv.dwell_avg
                db.add(LiveReading(venue_id=vid, live_available=lv.live_available, live_busyness=lv.live_busyness,
                                   forecast_busyness=lv.forecast_busyness, delta=lv.delta))
                n += lv.live_available
            db.commit()
        log.info("live: %d/%d venues with live data", n, len(todo))
        self._mark("live")
        return n

    # ------------------------------------------------------------- TomTom
    def traffic_points(self) -> list[tuple[float, float]]:
        if isinstance(self.tomtom, DemoTomTomClient):
            return self.tomtom.all_points()
        return grid_points(self.s.area_lat, self.s.area_lng, self.s.area_radius_m, self.s.traffic_grid_n)

    async def poll_traffic(self) -> int:
        try:
            pts = self.traffic_points()
            sem = asyncio.Semaphore(5)

            async def one(lat, lng):
                async with sem:
                    return await self.tomtom.flow(lat, lng)

            segs = {s.key: s for s in await asyncio.gather(*(one(*p) for p in pts)) if s}
            batch = uuid.uuid4().hex[:12]
            with SessionLocal() as db:
                for s in segs.values():
                    db.add(TrafficReading(
                        batch=batch, segment_key=s.key, frc=s.frc, current_speed=s.current_speed,
                        free_flow_speed=s.free_flow_speed, current_travel_time=s.current_travel_time,
                        free_flow_travel_time=s.free_flow_travel_time, confidence=s.confidence,
                        road_closure=s.road_closure, congestion=s.congestion,
                        coords_json=json.dumps([list(c) for c in s.coordinates]),
                    ))
                db.commit()
            log.info("traffic: %d segments from %d sample points", len(segs), len(pts))
            self._mark("traffic")
            return len(segs)
        except Exception as e:  # noqa: BLE001
            log.exception("traffic failed")
            self._mark("traffic", e)
            return 0

    # ------------------------------------------------------------- housekeeping
    def snapshot(self) -> None:
        with SessionLocal() as db:
            venues = metrics.venue_states(db, self.s)
            segs = metrics.latest_segments(db)
            a = metrics.area_summary(venues, segs)
            db.add(AreaSnapshot(busyness_index=a["busyness_index"], congestion_pct=a["congestion_pct"],
                                avg_speed=a["avg_speed_kmh"], surge_venues=a["surge_venues"], live_venues=a["venues_live"]))
            cutoff = utcnow() - timedelta(hours=self.s.history_hours)
            for model in (LiveReading, TrafficReading, AreaSnapshot):
                db.execute(delete(model).where(model.ts < cutoff))
            db.commit()

    async def bootstrap(self) -> None:
        await self.discover()
        await self.refresh_forecasts()
        await asyncio.gather(self.poll_live(force=True), self.poll_traffic())
        self.backfill_demo_history()
        self.snapshot()

    def backfill_demo_history(self, hours: int = 24, step_min: int = 15) -> int:
        """Demo mode only: fill the 24h trend from the synthetic curves, so a fresh demo doesn't start empty.

        Never runs when any real API key is set - real history is only ever built from real readings.
        """
        if self.s.places_live or self.s.besttime_live or self.s.tomtom_live:
            return 0
        if not isinstance(self.tomtom, DemoTomTomClient):
            return 0
        with SessionLocal() as db:
            if db.scalar(select(func.count(AreaSnapshot.id))):
                return 0
            venues = [v for v in db.scalars(select(Venue)) if v.week]
            now_local, now_utc, n = datetime.now(self.s.tz), utcnow(), 0
            for k in range(hours * 60 // step_min, 0, -1):
                at = now_local - timedelta(minutes=step_min * k)
                d, i = besttime_index(at)
                vs = [{"busyness": v.week[d][i], "reviews": v.user_rating_count, "source": "forecast",
                       "delta": None, "dwell_avg": v.dwell_avg} for v in venues]
                segs = []
                for seg_id, frc, ff, _ in self.tomtom.segments:
                    c = self.tomtom._congestion(seg_id, frc, at)
                    segs.append({"frc": frc, "congestion": c, "current_speed": ff * (1 - c),
                                 "free_flow_speed": ff, "road_closure": False})
                a = metrics.area_summary(vs, segs)
                db.add(AreaSnapshot(ts=now_utc - timedelta(minutes=step_min * k), busyness_index=a["busyness_index"],
                                    congestion_pct=a["congestion_pct"], avg_speed=a["avg_speed_kmh"],
                                    surge_venues=0, live_venues=0))
                n += 1
            db.commit()
        log.info("demo: backfilled %d trend points", n)
        return n

    def usage_estimate(self) -> dict:
        """Rough daily API usage with the current settings when real keys are used - shown on /api/status."""
        live_hours = 24 if not self.s.besttime_live else max(0, self.s.live_hours_end - self.s.live_hours_start)
        live_calls = self.s.max_live_venues * live_hours * 60 // max(1, self.s.live_poll_minutes)
        tt_points = len(grid_points(self.s.area_lat, self.s.area_lng, self.s.area_radius_m, self.s.traffic_grid_n))
        return {
            "google_places_calls_per_day": len(self.s.type_groups) * max(1, 24 // max(1, self.s.discovery_refresh_hours)),
            "besttime_credits_per_day": round(live_calls + self.s.max_venues * 2 / max(1, self.s.forecast_refresh_days)),
            "tomtom_requests_per_day": tt_points * (24 * 60 // max(1, self.s.traffic_poll_minutes)),
            "tomtom_free_tier_per_day": 2500,
        }
