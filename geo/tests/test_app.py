"""End-to-end in demo mode: start the app, run every job, check the API and the metrics."""
import os
import tempfile
from datetime import datetime

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/test.db"
for k in ("GOOGLE_PLACES_API_KEY", "BESTTIME_API_KEY_PRIVATE", "TOMTOM_API_KEY"):
    os.environ[k] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.clients.demo import besttime_index  # noqa: E402
from app.collector import grid_points  # noqa: E402
from app.main import app, collector  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        import asyncio
        asyncio.run(collector.bootstrap())     # make sure data exists before asserting
        yield c


def test_status_reports_demo_sources(client):
    s = client.get("/api/status").json()
    assert set(s["sources"].values()) == {"demo"}
    assert s["usage_estimate"]["tomtom_requests_per_day"] <= 2500   # defaults fit the free tier


def test_live_payload(client):
    d = client.get("/api/live").json()
    assert d["summary"]["venues_total"] > 10
    assert d["segments"] and all(0 <= s["congestion"] <= 1 for s in d["segments"])
    assert {"busyness", "delta", "source", "dwell_avg", "today_curve"} <= d["venues"][0].keys()
    labels = {h["label"] for h in d["hotspots"]}
    assert labels <= {"Hot zone", "Crowd gathering", "Pass-through traffic", "Quiet"}
    assert d["trend"], "snapshot should have been recorded"


def test_refresh_and_export(client):
    assert client.post("/api/refresh/traffic").status_code == 200
    assert client.post("/api/refresh/nope").status_code == 404
    csv = client.get("/api/export/venues.csv")
    assert csv.status_code == 200 and csv.text.startswith("name,type,address")


def test_dashboard_served(client):
    assert "GeoPulse" in client.get("/").text


def test_besttime_index_handles_after_midnight():
    # 2 AM Tuesday belongs to Monday's BestTime day; index 20 = 2 AM (index 0 = 6 AM)
    assert besttime_index(datetime(2026, 11, 17, 2, 0)) == (0, 20)
    assert besttime_index(datetime(2026, 11, 17, 6, 0)) == (1, 0)


def test_grid_points_inside_circle():
    pts = grid_points(20.29, 85.82, 1500, 4)
    assert 0 < len(pts) <= 16


def test_change_area(client, tmp_path, monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "AREA_FILE", tmp_path / "area.json")
    body = {"area_name": "Patia", "area_lat": 20.35, "area_lng": 85.82, "area_radius_m": 1000,
            "place_type_groups": "restaurant,cafe", "max_venues": 10, "timezone": "Asia/Kolkata"}
    assert client.post("/api/area", json={**body, "timezone": "Mars/Base"}).status_code == 422
    assert client.post("/api/area", json={**body, "area_radius_m": 99}).status_code == 422
    assert client.post("/api/area", json=body).status_code == 200
    cfg = client.get("/api/config").json()
    assert cfg["area_name"] == "Patia" and cfg["center"] == [20.35, 85.82] and cfg["radius_m"] == 1000
    assert (tmp_path / "area.json").exists()
    import asyncio
    asyncio.run(collector.bootstrap())
    venues = client.get("/api/live").json()["venues"]
    assert venues and all(v["type"] in ("restaurant", "cafe") for v in venues)
