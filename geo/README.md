# GeoPulse: live foot traffic and road flow for one city area

GeoPulse combines three live data sources on one map for a neighbourhood (default: Saheed Nagar, Bhubaneswar):

| Source | What it gives you | Answers |
|---|---|---|
| **Google Places API (New)** | Venues and competitors: name, category, rating, review count, location | *Who's here?* |
| **BestTime.app API** | Weekly hourly forecast, **live busyness vs usual**, **dwell time** for each venue | *Where are people **staying**, and for how long?* |
| **TomTom Traffic Flow API** | Live speed vs free-flow speed for each road segment | *How is traffic **moving**?* |

The dashboard shows:
- **Crowd density heatmap**: venue busyness weighted by venue size.
- **Venue markers**: colour shows how busy a venue is compared with usual (quieter → busier). Size shows how big the staying crowd is (busyness × dwell time).
- **Road flow**: lines coloured Free flow / Slow / Heavy / Jammed.
- **Zones**: a 300 m grid. Each cell is labelled **Hot zone** (crowd + jam), **Crowd gathering**, **Pass-through traffic** or Quiet.
- **KPIs, 24-hour trends, top hotspots, a venue table and CSV export.**

> **Runs with no API keys.** If a key is missing, that source switches to realistic synthetic **DEMO** data: lunch and dinner peaks, rush hours, weekend malls. You can demo the product today and add keys one at a time. The chips at the top of the panel show which sources are LIVE and which are DEMO.

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # add your keys here (optional)
uvicorn app.main:app --reload
```
Open **http://localhost:8000**. The first data appears within a few seconds.

Docker: `docker build -t geopulse . && docker run -p 8000:8000 --env-file .env geopulse`

Tests: `pytest` (HTTP calls are mocked, so no keys are needed).

## Getting the keys

1. **Google Places**: Google Cloud Console → enable **Places API (New)** → create an API key and restrict it to that API.
   GeoPulse uses Nearby Search with the `rating` and `userRatingCount` fields, which are billed at the Enterprise SKU. That's one call per type group per day (3 by default).
2. **BestTime**: sign up at besttime.app → copy the **private** API key (`pri_...`).
3. **TomTom**: developer.tomtom.com → create an app → API key (free tier: 2,500 requests/day).

## Cost and quota control (defaults)

`GET /api/status` shows the estimated daily usage for your current settings.

| API | Default usage | Knobs in `.env` |
|---|---|---|
| Google Places | 3 calls/day | `PLACE_TYPE_GROUPS`, `DISCOVERY_REFRESH_HOURS` |
| BestTime | ~386 credits/day (25 venues × hourly live, 08:00–23:00, plus weekly forecasts at 2 credits per venue) | `MAX_LIVE_VENUES`, `LIVE_POLL_MINUTES`, `LIVE_HOURS_START/END`, `MAX_VENUES` |
| TomTom | ~1,150 requests/day (12 grid points every 15 min), inside the free tier | `TRAFFIC_GRID_N`, `TRAFFIC_POLL_MINUTES` |

Built-in savings:
- BestTime live data only changes once an hour, so polling faster wastes credits.
- A venue that returns "no live data" 3 times in a row is only rechecked once a day.
- A failed forecast isn't retried until the next weekly refresh.

## How it works

```
Google Places ──discover (24h)──► venues table
                                     │
BestTime ──forecast (weekly)─────────┤  week curve + dwell time
         ──live (hourly)─────────────┤  live busyness, delta vs usual
                                     ▼
TomTom ──flow (15 min, grid)──► traffic readings ─► metrics.py ─► /api/live ─► MapLibre dashboard
                                                         │
                                         area snapshot every 5 min (24h trend)
```

| File | Role |
|---|---|
| `app/clients/` | One async client per API, plus `demo.py` (same interface, synthetic data) |
| `app/collector.py` | Scheduled jobs (APScheduler): discover → forecasts → live → traffic → snapshot |
| `app/metrics.py` | Area busyness index, surge detection, congestion, hotspot grid, trend |
| `app/main.py` | FastAPI app and endpoints |
| `static/` | Dashboard (MapLibre GL, bundled locally; plain JS, no build step) |

**API endpoints**

| Endpoint | Returns |
|---|---|
| `GET /api/live` | Everything the dashboard needs: summary, venues, segments, hotspots, trend |
| `GET /api/status` | Source modes, last job runs, errors, usage estimate |
| `GET /api/venues/{id}` | One venue |
| `POST /api/refresh/{discover\|forecasts\|live\|traffic}` | Runs a job now |
| `GET /api/export/venues.csv` | Venue snapshot for client reports |

### Metric definitions
- **Area busyness index** (0–100+): mean venue busyness, weighted by `1 + log10(1 + reviews)` as a proxy for venue size. It uses live data where available and the forecast for the current hour otherwise.
- **Busier than usual**: venues where live busyness is ≥ 15 points above forecast.
- **Staying crowd** (marker size): busyness × average dwell time × size weight.
- **Road congestion**: `1 − current_speed / free_flow_speed`. The average is weighted by road class (FRC).
- **Zones**: crowd ≥ 45% of the busiest cell, roads ≥ 35% congested, or both.

## Watch-outs before you sell this
- **BestTime coverage**: live data only exists for popular venues (in the demo, about 30% of venues have no live signal, mirroring the real API). Check coverage for your target area before promising live numbers to a client.
- **Values are relative**: BestTime busyness is relative to each venue's own weekly peak (100 = its busiest hour), not a headcount. Present it as an index, not as "number of people". If clients need real footfall counts, add camera or LiDAR counting.
- **Terms of service**: read each provider's terms on storing data and showing it to clients, especially Google's caching rules for Places content. GeoPulse stores Places IDs, names and coordinates for tracking; check that this suits your use.
- **Basemap**: the default CARTO style is free for non-commercial use. For a paid product, use a commercial tile plan (MapTiler, Stadia, TomTom Maps) via `MAP_STYLE_URL`.

## Change the area
**From the dashboard (recommended):** click **Change area & settings** under the area name. Search for a place (Google Places when a key is set, OpenStreetMap otherwise) or click on the map to set the centre, then choose the radius, venue types, max venues and timezone, and press **Apply & collect**. The old area's data is wiped and the new area is collected in about 10-30 seconds. Your choice is saved to `area.json`, which overrides `.env` and survives restarts. Delete `area.json` to go back to the `.env` values.

Each change re-runs discovery, which with live keys costs one Google call per venue-type group plus up to 2 BestTime credits per venue.

**From `.env`:** set `AREA_NAME`, `AREA_LAT`, `AREA_LNG` and `AREA_RADIUS_M`, delete `geopulse.db` and `area.json`, and restart.
