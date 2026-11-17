"""Database models (SQLAlchemy 2). SQLite by default; set DATABASE_URL for PostgreSQL."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Venue(Base):
    __tablename__ = "venues"
    id: Mapped[str] = mapped_column(String, primary_key=True)        # Google place id
    name: Mapped[str] = mapped_column(String)
    address: Mapped[str] = mapped_column(String, default="")
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    primary_type: Mapped[str] = mapped_column(String, default="")
    types_json: Mapped[str] = mapped_column(Text, default="[]")
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    user_rating_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    besttime_venue_id: Mapped[str | None] = mapped_column(String, nullable=True)
    besttime_status: Mapped[str] = mapped_column(String, default="pending")   # pending | ok | failed
    forecast_json: Mapped[str | None] = mapped_column(Text, nullable=True)    # 7x24, BestTime layout
    forecast_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dwell_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dwell_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dwell_avg: Mapped[int | None] = mapped_column(Integer, nullable=True)

    live_misses: Mapped[int] = mapped_column(Integer, default=0)       # consecutive "no live data" answers
    live_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def week(self) -> list[list[int]] | None:
        return json.loads(self.forecast_json) if self.forecast_json else None


class LiveReading(Base):
    __tablename__ = "live_readings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    venue_id: Mapped[str] = mapped_column(ForeignKey("venues.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    live_available: Mapped[bool] = mapped_column(Boolean, default=False)
    live_busyness: Mapped[int | None] = mapped_column(Integer, nullable=True)
    forecast_busyness: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delta: Mapped[int | None] = mapped_column(Integer, nullable=True)


class TrafficReading(Base):
    __tablename__ = "traffic_readings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    batch: Mapped[str] = mapped_column(String, index=True)            # one polling cycle
    segment_key: Mapped[str] = mapped_column(String)
    frc: Mapped[str] = mapped_column(String, default="")
    current_speed: Mapped[float] = mapped_column(Float)
    free_flow_speed: Mapped[float] = mapped_column(Float)
    current_travel_time: Mapped[int] = mapped_column(Integer)
    free_flow_travel_time: Mapped[int] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float)
    road_closure: Mapped[bool] = mapped_column(Boolean, default=False)
    congestion: Mapped[float] = mapped_column(Float)
    coords_json: Mapped[str] = mapped_column(Text)                    # [[lat,lng], ...]


class AreaSnapshot(Base):
    __tablename__ = "area_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    busyness_index: Mapped[float | None] = mapped_column(Float, nullable=True)
    congestion_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_speed: Mapped[float | None] = mapped_column(Float, nullable=True)
    surge_venues: Mapped[int] = mapped_column(Integer, default=0)
    live_venues: Mapped[int] = mapped_column(Integer, default=0)


_settings = get_settings()
engine = create_engine(
    _settings.database_url,
    connect_args={"check_same_thread": False} if _settings.database_url.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
