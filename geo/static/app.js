/* GeoPulse dashboard - MapLibre map + live panel. Polls /api/live every minute. */
(() => {
  "use strict";

  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const C = {
    seq: ["--seq-1", "--seq-2", "--seq-3", "--seq-4", "--seq-5"].map(css),
    divLow: css("--div-low"), divMid: css("--div-mid"), divHigh: css("--div-high"),
    good: css("--good"), warning: css("--warning"), serious: css("--serious"), critical: css("--critical"),
    surface: css("--surface-1"), border: css("--border"), muted: css("--text-muted"), text: css("--text-primary"),
    zones: { "Crowd gathering": css("--series-1"), "Hot zone": css("--series-2"), "Pass-through traffic": css("--series-3") },
  };
  const ROAD_LEVELS = [
    { max: 0.15, label: "Free flow", color: C.good },
    { max: 0.35, label: "Slow", color: C.warning },
    { max: 0.6, label: "Heavy", color: C.serious },
    { max: 1.01, label: "Jammed", color: C.critical },
  ];
  const ZONE_INFO = {
    "Hot zone": "busy venues and jammed roads",
    "Crowd gathering": "people staying at venues",
    "Pass-through traffic": "busy roads, few people stopping",
  };
  const TYPE_LABEL = (t) => (t || "venue").replace(/_/g, " ");
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  let cfg, map, data = null, popup = null, sources = {};
  let fmtTime;

  // ------------------------------------------------------------------ boot
  async function boot() {
    cfg = await (await fetch("/api/config")).json();
    showAreaName();
    fmtTime = new Intl.DateTimeFormat("en-IN", { hour: "numeric", minute: "2-digit", timeZone: cfg.timezone });

    let style = cfg.map_style_url;
    try { const r = await fetch(style); if (!r.ok) throw new Error(); style = await r.json(); }
    catch { style = rasterFallback(); }

    map = new maplibregl.Map({ container: "map", style, center: [cfg.center[1], cfg.center[0]], zoom: 14, attributionControl: { compact: true } });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.on("load", () => {
      addLayers();
      cfg.collecting ? watchCollection() : refresh();
      setInterval(refresh, (cfg.refresh_seconds || 60) * 1000);
    });

    document.querySelectorAll("#layers input").forEach((el) => el.addEventListener("change", applyLayerToggles));
    $("venue-sort").addEventListener("change", () => data && renderVenues(data.venues));
    $("btn-refresh").addEventListener("click", manualRefresh);
    initAreaEditor();
    loadStatus();
  }

  function showAreaName() {
    $("area-name").textContent = `${cfg.area_name} · ${(cfg.radius_m / 1000).toFixed(1)} km radius`;
  }

  function rasterFallback() {
    return {
      version: 8,
      sources: { base: { type: "raster", tileSize: 256, attribution: "© OpenStreetMap contributors © CARTO",
        tiles: ["a", "b", "c"].map((s) => `https://${s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png`) } },
      layers: [{ id: "base", type: "raster", source: "base" }],
    };
  }

  // ------------------------------------------------------------------ map layers
  function addLayers() {
    const empty = { type: "FeatureCollection", features: [] };
    ["venues", "roads", "zones"].forEach((id) => map.addSource(id, { type: "geojson", data: empty }));
    map.addSource("area", { type: "geojson", data: circle(cfg.center, cfg.radius_m) });

    map.addLayer({ id: "area-line", type: "line", source: "area", paint: { "line-color": C.muted, "line-width": 1, "line-dasharray": [3, 3] } });
    map.addSource("area-preview", { type: "geojson", data: empty });
    map.addLayer({ id: "area-preview", type: "line", source: "area-preview", filter: ["==", ["geometry-type"], "LineString"],
      paint: { "line-color": C.seq[3], "line-width": 2.5 } });
    map.addLayer({ id: "area-preview-center", type: "circle", source: "area-preview", filter: ["==", ["geometry-type"], "Point"],
      paint: { "circle-radius": 6, "circle-color": C.seq[3], "circle-stroke-color": C.surface, "circle-stroke-width": 2 } });
    map.on("click", (e) => { if (picking()) setCenter(e.lngLat.lat, e.lngLat.lng, true); });

    map.addLayer({ id: "zones-fill", type: "fill", source: "zones", layout: { visibility: "none" },
      paint: { "fill-color": ["get", "color"], "fill-opacity": ["interpolate", ["linear"], ["get", "score"], 0, 0.12, 1, 0.38] } });
    map.addLayer({ id: "zones-line", type: "line", source: "zones", layout: { visibility: "none" },
      paint: { "line-color": ["get", "color"], "line-width": 1.5 } });

    map.addLayer({ id: "heat", type: "heatmap", source: "venues", maxzoom: 18,
      paint: {
        "heatmap-weight": ["get", "heat"],
        "heatmap-intensity": ["interpolate", ["linear"], ["zoom"], 11, 1.2, 16, 2.4],
        "heatmap-radius": ["interpolate", ["linear"], ["zoom"], 11, 18, 14, 45, 17, 90],
        "heatmap-opacity": 0.8,
        "heatmap-color": ["interpolate", ["linear"], ["heatmap-density"],
          0, "rgba(16,66,129,0)", 0.15, C.seq[0], 0.4, C.seq[1], 0.65, C.seq[2], 0.85, C.seq[3], 1, C.seq[4]],
      } });

    map.addLayer({ id: "roads-casing", type: "line", source: "roads", layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": C.surface, "line-width": ["+", ["get", "width"], 3] } });
    map.addLayer({ id: "roads", type: "line", source: "roads", layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": ["get", "color"], "line-width": ["get", "width"] } });

    map.addLayer({ id: "venues", type: "circle", source: "venues",
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["get", "stay_load"], 0, 4, 1, 7, 3, 12, 6, 16],
        "circle-color": ["case", ["==", ["get", "source"], "live"],
          ["interpolate", ["linear"], ["get", "delta"], -30, C.divLow, 0, C.divMid, 30, C.divHigh], C.divMid],
        "circle-opacity": ["case", ["==", ["get", "source"], "live"], 0.95, 0.45],
        "circle-stroke-color": ["case", ["==", ["get", "source"], "live"], C.surface, C.text],
        "circle-stroke-width": ["case", ["==", ["get", "source"], "live"], 2, 1],
      } });

    map.on("click", "venues", (e) => { if (!picking()) openVenue(e.features[0].properties.id); });
    map.on("mouseenter", "venues", () => (map.getCanvas().style.cursor = "pointer"));
    map.on("mouseleave", "venues", () => (map.getCanvas().style.cursor = ""));
    map.on("mousemove", "roads", (e) => {
      const p = e.features[0].properties;
      showTip(e.originalEvent, `<b>${p.level}</b> · ${p.speed} km/h <span class="tip-sub">(free flow ${p.ff})</span><br><span class="tip-sub">+${p.delay}s delay on this stretch</span>`);
    });
    map.on("mouseleave", "roads", hideTip);
    map.on("mousemove", "zones-fill", (e) => {
      const p = e.features[0].properties;
      if (map.queryRenderedFeatures(e.point, { layers: ["venues", "roads"] }).length) return;
      showTip(e.originalEvent, `<b>${p.label}</b><br><span class="tip-sub">crowd ${Math.round(p.crowd * 100)}% · roads ${Math.round(p.congestion * 100)}% congested</span>`);
    });
    map.on("mouseleave", "zones-fill", hideTip);
    applyLayerToggles();
  }

  function applyLayerToggles() {
    const on = {};
    document.querySelectorAll("#layers input").forEach((el) => (on[el.dataset.layer] = el.checked));
    const vis = (ids, v) => ids.forEach((id) => map.getLayer(id) && map.setLayoutProperty(id, "visibility", v ? "visible" : "none"));
    vis(["heat"], on.heat); vis(["venues"], on.venues); vis(["roads", "roads-casing"], on.roads); vis(["zones-fill", "zones-line"], on.zones);
    renderLegend(on);
  }

  function circle([lat, lng], r, n = 72) {
    const pts = [];
    for (let i = 0; i <= n; i++) {
      const a = (i / n) * 2 * Math.PI;
      pts.push([lng + (r * Math.cos(a)) / (111320 * Math.cos((lat * Math.PI) / 180)), lat + (r * Math.sin(a)) / 111320]);
    }
    return { type: "Feature", geometry: { type: "LineString", coordinates: pts } };
  }

  // ------------------------------------------------------------------ data refresh
  async function refresh() {
    try {
      const r = await fetch("/api/live");
      data = await r.json();
      updateMap(data);
      renderPanel(data);
      $("updated").textContent = `updated ${fmtTime.format(new Date())}`;
    } catch (err) {
      $("updated").textContent = "offline - retrying";
      console.error(err);
    }
  }

  async function manualRefresh() {
    const b = $("btn-refresh");
    b.disabled = true; b.textContent = "Refreshing…";
    try { await Promise.all([fetch("/api/refresh/live", { method: "POST" }), fetch("/api/refresh/traffic", { method: "POST" })]); await refresh(); }
    finally { b.disabled = false; b.textContent = "Refresh live data"; loadStatus(); }
  }

  async function loadStatus() {
    try {
      const s = await (await fetch("/api/status")).json();
      sources = s.sources;
      const names = { google_places: "Places", besttime: "BestTime", tomtom: "TomTom" };
      $("sources").innerHTML = Object.entries(s.sources)
        .map(([k, v]) => `<span class="chip ${v}" title="${v === "demo" ? "No API key - synthetic demo data" : "Live API"}">${names[k]} <b>${v.toUpperCase()}</b></span>`)
        .join("") + (Object.keys(s.errors || {}).length ? `<span class="chip" title="${esc(JSON.stringify(s.errors))}">⚠ errors</span>` : "");
    } catch { /* ignore */ }
  }

  function sizeWeight(n) { return 1 + Math.log10(1 + (n || 0)); }

  function updateMap(d) {
    const maxW = Math.max(1, ...d.venues.map((v) => sizeWeight(v.reviews)));
    map.getSource("venues").setData({
      type: "FeatureCollection",
      features: d.venues.filter((v) => v.busyness != null).map((v) => ({
        type: "Feature", geometry: { type: "Point", coordinates: [v.lng, v.lat] },
        properties: { id: v.id, source: v.source, delta: v.delta ?? 0, stay_load: v.stay_load,
          heat: (Math.min(v.busyness, 150) / 100) * (sizeWeight(v.reviews) / maxW) },
      })),
    });
    const frcW = { FRC0: 6, FRC1: 6, FRC2: 5, FRC3: 4 };
    map.getSource("roads").setData({
      type: "FeatureCollection",
      features: d.segments.map((s) => {
        const lvl = s.road_closure ? ROAD_LEVELS[3] : ROAD_LEVELS.find((l) => s.congestion < l.max);
        return { type: "Feature", geometry: { type: "LineString", coordinates: s.coords.map(([la, ln]) => [ln, la]) },
          properties: { color: lvl.color, level: s.road_closure ? "Closed" : lvl.label, width: frcW[s.frc] || 3,
            speed: Math.round(s.current_speed), ff: Math.round(s.free_flow_speed), delay: s.delay_s } };
      }),
    });
    map.getSource("zones").setData({
      type: "FeatureCollection",
      features: d.hotspots.filter((h) => h.label !== "Quiet").map((h) => ({
        type: "Feature", geometry: { type: "Polygon", coordinates: [h.polygon] },
        properties: { label: h.label, color: C.zones[h.label], score: h.score, crowd: h.crowd, congestion: h.congestion },
      })),
    });
  }

  // ------------------------------------------------------------------ panel
  function renderPanel(d) {
    const s = d.summary;
    $("k-busy").textContent = s.busyness_index != null ? Math.round(s.busyness_index) : "–";
    $("k-busy-sub").textContent = `out of 100 · ${s.venues_live}/${s.venues_total} venues live`;
    $("k-surge").textContent = s.surge_venues;
    $("k-surge-sub").textContent = `${s.quieter_venues} quieter than usual`;
    $("k-cong").textContent = s.congestion_pct != null ? `${Math.round(s.congestion_pct)}%` : "–";
    $("k-cong-sub").textContent = s.avg_speed_kmh != null ? `${Math.round(s.avg_speed_kmh)} km/h vs ${Math.round(s.free_flow_kmh)} free flow` : "no road data yet";
    $("k-dwell").textContent = s.avg_dwell_min != null ? `${s.avg_dwell_min} min` : "–";
    $("k-dwell-sub").textContent = `${s.jammed_segments} of ${s.segments} road stretches jammed`;

    document.querySelectorAll(".spark").forEach((el) => renderSpark(el, d.trend));
    renderHotspots(d.hotspots);
    renderVenues(d.venues);
  }

  function renderHotspots(hs) {
    const top = hs.filter((h) => h.label !== "Quiet").slice(0, 5);
    $("hotspots").innerHTML = top.length ? top.map((h) => `
      <li tabindex="0" data-lat="${h.center[0]}" data-lng="${h.center[1]}">
        <div class="hs-top"><span class="zone-tag"><span class="swatch" style="background:${C.zones[h.label]}"></span>${h.label}</span>
          <span class="muted">${ZONE_INFO[h.label]}</span></div>
        <div class="hs-meta">${h.top_venues.length ? esc(h.top_venues.join(", ")) + " · " : ""}crowd ${Math.round(h.crowd * 100)}% · roads ${Math.round(h.congestion * 100)}% congested${h.dwell_min ? ` · stay ~${h.dwell_min} min` : ""}</div>
      </li>`).join("") : `<li class="muted">No hotspots right now - the area is quiet.</li>`;
    $("hotspots").querySelectorAll("li[data-lat]").forEach((li) => {
      const go = () => {
        map.flyTo({ center: [+li.dataset.lng, +li.dataset.lat], zoom: 16 });
        const z = document.querySelector('#layers input[data-layer="zones"]');
        if (!z.checked) { z.checked = true; applyLayerToggles(); }
      };
      li.addEventListener("click", go);
      li.addEventListener("keydown", (e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), go()));
    });
  }

  function renderVenues(venues) {
    const key = $("venue-sort").value;
    const rows = venues.filter((v) => v.busyness != null)
      .sort((a, b) => (b[key] ?? -999) - (a[key] ?? -999)).slice(0, 15);
    $("venue-rows").innerHTML = rows.map((v) => {
      const dcol = v.delta == null ? C.divMid : v.delta > 0 ? C.divHigh : C.divLow;
      const delta = v.delta == null ? `<span class="muted">–</span>`
        : `<span class="${v.delta >= 0 ? "delta-up" : "delta-down"}"><span class="dot" style="background:${dcol}"></span>${v.delta > 0 ? "+" : ""}${v.delta}</span>`;
      return `<tr data-id="${esc(v.id)}">
        <td><div class="v-name" title="${esc(v.name)}">${esc(v.name)}</div><div class="v-type">${TYPE_LABEL(v.type)}${v.source === "forecast" ? '<span class="tag">forecast</span>' : ""}</div></td>
        <td><span class="bar"><i style="width:${Math.min(100, v.busyness)}%"></i></span>${v.busyness}</td>
        <td>${delta}</td>
        <td>${v.dwell_avg ? v.dwell_avg + "m" : "–"}</td></tr>`;
    }).join("");
    $("venue-rows").querySelectorAll("tr").forEach((tr) => tr.addEventListener("click", () => openVenue(tr.dataset.id, true)));
  }

  // ------------------------------------------------------------------ venue popup
  function openVenue(id, fly = false) {
    const v = data && data.venues.find((x) => x.id === id);
    if (!v) return;
    if (fly) map.flyTo({ center: [v.lng, v.lat], zoom: 16 });
    popup && popup.remove();
    const now = v.busyness != null ? `${v.busyness}${v.source === "live" ? " (live)" : " (forecast)"}` : "–";
    const vs = v.delta == null ? (v.source === "live" ? "as usual" : "no live signal") : `${v.delta > 0 ? "+" : ""}${v.delta} pts ${v.delta >= 0 ? "busier" : "quieter"} than usual`;
    const html = `<div class="pop">
      <h4>${esc(v.name)}</h4>
      <div class="sub">${TYPE_LABEL(v.type)}${v.rating ? ` · ★ ${v.rating}` : ""}${v.reviews ? ` (${v.reviews.toLocaleString("en-IN")} reviews)` : ""}</div>
      <dl>
        <dt>Busy now</dt><dd>${now}</dd>
        <dt>vs usual</dt><dd>${vs}</dd>
        <dt>People stay</dt><dd>${v.dwell_min && v.dwell_max ? `${v.dwell_min}–${v.dwell_max} min` : v.dwell_avg ? `~${v.dwell_avg} min` : "–"}</dd>
      </dl>
      ${v.today_curve ? `<div class="curve-title">Typical busyness today (BestTime forecast)</div>${curveSvg(v.today_curve)}` : ""}
    </div>`;
    popup = new maplibregl.Popup({ offset: 12, maxWidth: "300px" }).setLngLat([v.lng, v.lat]).setHTML(html).addTo(map);
  }

  function localHour() {
    return +new Intl.DateTimeFormat("en-GB", { hour: "numeric", hourCycle: "h23", timeZone: cfg.timezone }).format(new Date());
  }

  /* 24 thin bars, index 0 = 6 AM. Current hour highlighted, others recede. */
  function curveSvg(vals) {
    const W = 260, H = 64, base = 50, bw = W / 24, cur = (localHour() - 6 + 24) % 24;
    const max = Math.max(100, ...vals);
    const bars = vals.map((v, i) => {
      const h = Math.max(v > 0 ? 2 : 0, (v / max) * (base - 4));
      const x = i * bw + 1, w = bw - 2, y = base - h, r = Math.min(2, w / 2, h);
      const d = h ? `M${x},${base}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${base}Z` : "";
      return `<path d="${d}" fill="${i === cur ? C.seq[3] : C.seq[1]}" opacity="${i === cur ? 1 : 0.55}"><title>${hourLabel(i)}: ${v}</title></path>`;
    }).join("");
    const ticks = [0, 6, 12, 18].map((i) => `<text x="${i * bw + 1}" y="${H - 2}" fill="${C.muted}" font-size="10">${hourLabel(i)}</text>`).join("");
    return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Hourly busyness forecast for today">
      <line x1="0" x2="${W}" y1="${base + 0.5}" y2="${base + 0.5}" stroke="${C.border}"/>${bars}${ticks}</svg>`;
  }
  function hourLabel(i) { const h = (i + 6) % 24; return h === 0 ? "12a" : h === 12 ? "12p" : h < 12 ? `${h}a` : `${h - 12}p`; }

  // ------------------------------------------------------------------ sparklines (single series, crosshair tooltip)
  function renderSpark(el, trend) {
    const key = el.dataset.key, unit = el.dataset.unit, title = el.dataset.title;
    const pts = trend.filter((t) => t[key] != null).map((t) => ({ t: new Date(t.ts), v: t[key] }));
    const last = pts.length ? pts[pts.length - 1].v : null;
    const head = `<div class="spark-head"><span>${title}</span><b>${last != null ? Math.round(last) + unit : "–"}</b></div>`;
    if (pts.length < 2) { el.innerHTML = head + `<div class="empty">Collecting history - the trend appears after a few polling cycles.</div>`; return; }

    const W = el.clientWidth || 360, H = 56, pad = 4;
    const t0 = +pts[0].t, t1 = +pts[pts.length - 1].t || t0 + 1;
    const vmax = Math.max(key === "busyness_index" ? 60 : 40, ...pts.map((p) => p.v)) * 1.1;
    const x = (t) => pad + ((+t - t0) / Math.max(1, t1 - t0)) * (W - pad * 2);
    const y = (v) => H - pad - (v / vmax) * (H - pad * 2);
    const d = pts.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
    el.innerHTML = head + `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${title} over the last 24 hours">
      <line x1="0" x2="${W}" y1="${H - pad}" y2="${H - pad}" stroke="${C.border}"/>
      <path d="${d}" fill="none" stroke="${C.seq[2]}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>
      <line class="xh" y1="0" y2="${H}" stroke="${C.muted}" stroke-width="1" visibility="hidden"/>
      <circle class="xd" r="4" fill="${C.seq[2]}" stroke="${C.surface}" stroke-width="2" visibility="hidden"/>
      <rect x="0" y="0" width="${W}" height="${H}" fill="transparent"/></svg>`;
    const svg = el.querySelector("svg"), xh = svg.querySelector(".xh"), xd = svg.querySelector(".xd");
    svg.addEventListener("mousemove", (e) => {
      const r = svg.getBoundingClientRect(), mx = ((e.clientX - r.left) / r.width) * W;
      let best = pts[0];
      for (const p of pts) if (Math.abs(x(p.t) - mx) < Math.abs(x(best.t) - mx)) best = p;
      const px = x(best.t);
      xh.setAttribute("x1", px); xh.setAttribute("x2", px); xh.setAttribute("visibility", "visible");
      xd.setAttribute("cx", px); xd.setAttribute("cy", y(best.v)); xd.setAttribute("visibility", "visible");
      showTip(e, `<b>${Math.round(best.v)}${unit}</b> <span class="tip-sub">${fmtTime.format(best.t)}</span>`);
    });
    svg.addEventListener("mouseleave", () => { xh.setAttribute("visibility", "hidden"); xd.setAttribute("visibility", "hidden"); hideTip(); });
  }

  // ------------------------------------------------------------------ legend & tooltip
  function renderLegend(on) {
    const parts = [];
    if (on.heat) parts.push(`<div><h3>Crowd density (venue busyness × size)</h3>
      <div class="ramp" style="background:linear-gradient(90deg,${C.seq.join(",")})"></div><div class="ends"><span>Low</span><span>High</span></div></div>`);
    if (on.venues) parts.push(`<div><h3>Venues · size = staying crowd</h3>
      <div class="ramp" style="background:linear-gradient(90deg,${C.divLow},${C.divMid},${C.divHigh})"></div>
      <div class="ends"><span>Quieter</span><span>As usual</span><span>Busier</span></div>
      <div class="ends" style="margin-top:4px"><span><span class="dot" style="background:${C.divMid};opacity:.5;outline:1px solid ${C.text}"></span>Faded = forecast only (no live signal)</span></div></div>`);
    if (on.roads) parts.push(`<div><h3>Road flow (TomTom live)</h3><ul>${ROAD_LEVELS.map((l) => `<li><span class="line" style="background:${l.color}"></span>${l.label}</li>`).join("")}</ul></div>`);
    if (on.zones) parts.push(`<div><h3>Zones</h3><ul style="grid-template-columns:1fr">${Object.entries(C.zones).map(([k, c]) => `<li><span class="swatch" style="background:${c}"></span>${k}</li>`).join("")}</ul></div>`);
    $("legend").innerHTML = parts.join("");
    $("legend").hidden = !parts.length;
  }

  const tip = $("tooltip");
  function showTip(e, html) {
    tip.innerHTML = html; tip.hidden = false;
    const w = tip.offsetWidth;
    tip.style.left = Math.min(window.innerWidth - w - 8, e.clientX + 14) + "px";
    tip.style.top = e.clientY + 14 + "px";
  }
  function hideTip() { tip.hidden = true; }

  // ------------------------------------------------------------------ area editor
  const ed = $("area-editor");
  const picking = () => !ed.hidden;
  const groupBoxes = () => [...document.querySelectorAll(".ae-group")];

  function initAreaEditor() {
    try { $("ae-tz-list").innerHTML = Intl.supportedValuesOf("timeZone").map((z) => `<option value="${z}">`).join(""); } catch { /* old browser */ }
    $("btn-area").addEventListener("click", openEditor);
    $("ae-close").addEventListener("click", closeEditor);
    $("ae-cancel").addEventListener("click", closeEditor);
    ["ae-lat", "ae-lng", "ae-radius", "ae-max"].forEach((id) => $(id).addEventListener("input", updatePreview));
    groupBoxes().forEach((b) => b.addEventListener("change", updatePreview));
    let t;
    $("ae-search").addEventListener("input", () => { clearTimeout(t); t = setTimeout(searchPlaces, 450); });
    $("ae-search").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); clearTimeout(t); searchPlaces(); } });
    ed.addEventListener("submit", applyArea);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && picking()) closeEditor(); });
  }

  function openEditor() {
    $("ae-name").value = cfg.area_name;
    $("ae-lat").value = cfg.center[0].toFixed(5);
    $("ae-lng").value = cfg.center[1].toFixed(5);
    $("ae-radius").value = cfg.radius_m;
    $("ae-max").value = cfg.max_venues;
    $("ae-tz").value = cfg.timezone;
    const active = new Set(cfg.place_type_groups.split(";").map((g) => g.trim()));
    groupBoxes().forEach((b) => (b.checked = active.has(b.value)));
    $("ae-search").value = ""; $("ae-results").innerHTML = ""; $("ae-error").hidden = true;
    popup && popup.remove();
    ed.hidden = false; document.body.classList.add("picking");
    updatePreview();
    $("ae-search").focus();
  }

  function closeEditor() {
    ed.hidden = true; document.body.classList.remove("picking");
    map.getSource("area-preview").setData({ type: "FeatureCollection", features: [] });
  }

  function setCenter(lat, lng, keepName = false, name = "") {
    $("ae-lat").value = lat.toFixed(5); $("ae-lng").value = lng.toFixed(5);
    if (name) $("ae-name").value = name;
    else if (!keepName) $("ae-name").value = `${lat.toFixed(4)}, ${lng.toFixed(4)}`;
    updatePreview();
  }

  function updatePreview() {
    const lat = +$("ae-lat").value, lng = +$("ae-lng").value, r = +$("ae-radius").value;
    $("ae-radius-val").textContent = `${(r / 1000).toFixed(1)} km`;
    if (!isFinite(lat) || !isFinite(lng)) return;
    map.getSource("area-preview").setData({ type: "FeatureCollection", features: [
      circle([lat, lng], r), { type: "Feature", geometry: { type: "Point", coordinates: [lng, lat] } }] });
    const groups = groupBoxes().filter((b) => b.checked).length, maxV = +$("ae-max").value || 0;
    const costs = [];
    if (sources.google_places === "live") costs.push(`${groups} Google Places call${groups === 1 ? "" : "s"}`);
    if (sources.besttime === "live") costs.push(`up to ${maxV * 2} BestTime credits (forecasts)`);
    $("ae-cost").textContent = costs.length
      ? `Applying wipes the current area's data and re-collects. Uses about ${costs.join(" + ")}.`
      : "Applying wipes the current area's data and re-collects (demo sources cost nothing).";
  }

  async function searchPlaces() {
    const q = $("ae-search").value.trim(), ul = $("ae-results");
    if (q.length < 2) { ul.innerHTML = ""; return; }
    ul.innerHTML = `<li class="muted">Searching…</li>`;
    try {
      const r = await fetch(`/api/geocode?q=${encodeURIComponent(q)}`);
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
      const res = await r.json();
      ul.innerHTML = res.length ? res.map((p, i) => `<li tabindex="0" data-i="${i}">${esc(p.name)}<small>${esc(p.address)}</small></li>`).join("")
        : `<li class="muted">No matches</li>`;
      ul.querySelectorAll("li[data-i]").forEach((li) => {
        const pick = () => {
          const p = res[+li.dataset.i];
          setCenter(p.lat, p.lng, false, p.name);
          map.flyTo({ center: [p.lng, p.lat], zoom: 13.5 });
          ul.innerHTML = "";
        };
        li.addEventListener("click", pick);
        li.addEventListener("keydown", (e) => e.key === "Enter" && (e.preventDefault(), pick()));
      });
    } catch (err) { ul.innerHTML = `<li class="muted">Search failed: ${esc(err.message)}</li>`; }
  }

  async function applyArea(e) {
    e.preventDefault();
    const groups = groupBoxes().filter((b) => b.checked).map((b) => b.value);
    const err = $("ae-error");
    if (!groups.length) { err.textContent = "Pick at least one venue type."; err.hidden = false; return; }
    const body = {
      area_name: $("ae-name").value.trim(), area_lat: +$("ae-lat").value, area_lng: +$("ae-lng").value,
      area_radius_m: +$("ae-radius").value, max_venues: +$("ae-max").value, timezone: $("ae-tz").value.trim(),
      place_type_groups: groups.join(";"),
    };
    const btn = $("ae-apply");
    btn.disabled = true; btn.textContent = "Applying…";
    try {
      const r = await fetch("/api/area", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(Array.isArray(d.detail) ? d.detail.map((x) => `${x.loc.at(-1)}: ${x.msg}`).join("; ") : d.detail || r.statusText);
      }
      cfg = await (await fetch("/api/config")).json();
      fmtTime = new Intl.DateTimeFormat("en-IN", { hour: "numeric", minute: "2-digit", timeZone: cfg.timezone });
      showAreaName();
      map.getSource("area").setData(circle(cfg.center, cfg.radius_m));
      map.flyTo({ center: [cfg.center[1], cfg.center[0]], zoom: cfg.radius_m > 2500 ? 12.5 : 14 });
      closeEditor();
      watchCollection();
    } catch (ex) {
      err.textContent = `Could not apply: ${ex.message}`; err.hidden = false;
    } finally { btn.disabled = false; btn.textContent = "Apply & collect"; }
  }

  /* After an area change, poll quickly until the server finishes collecting the new area. */
  async function watchCollection() {
    for (let i = 0; i < 40; i++) {
      await refresh();
      $("updated").textContent = "collecting new area…";
      const c = await (await fetch("/api/config")).json();
      if (!c.collecting) break;
      await new Promise((res) => setTimeout(res, 3000));
    }
    await refresh();
    loadStatus();
  }

  boot();
})();
