// UI for the ORR 3D viewer: map, deck.gl layers, playback controls and ride-along camera.
// Data handling lives in core.js (window.OrrCore).
(function () {
  "use strict";

  const { decode, metres, bearing, prepareEdges, prepareScenario, buildTrips, locate, roadColor } = window.OrrCore;
  const $ = (id) => document.getElementById(id);
  const S_COLORS = { habitual: [167, 176, 191], waze: [78, 161, 255], coordinated: [43, 217, 165] };
  const ROAD_WIDTH = { motorway: 14, trunk: 12, primary: 10, secondary: 8, tertiary: 6, unclassified: 4, residential: 3 };
  const OVERVIEW = { zoom: 12.6, pitch: 58, bearing: -20 };
  const BASEMAP = "https://tiles.openfreemap.org/styles/dark";
  const FALLBACK_STYLE = { version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#070b12" } }] };
  const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  let data, edges, map, overlay, sc;
  let t = 2700, playing = !REDUCED_MOTION, speed = 60, last = performance.now();
  let followId = null, followBearing = 0, speedBeforeFollow = null;

  function el(tag, className, text) {
    const n = document.createElement(tag);
    if (className) n.className = className;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  const clock = (s) => window.OrrCore.clock(s, data.meta.clock_start);
  const fmt = (n) => n >= 10000 ? `${(n / 1000).toFixed(1)}k` : Math.round(n).toLocaleString();
  const mins = (s) => `${Math.round(s / 60)} min`;
  const binAt = (time) => Math.min(data.meta.end_s / data.meta.bin_s - 1, Math.floor(time / data.meta.bin_s));
  const trips = (s) => buildTrips(s, edges, data.meta.end_s);

  function setPressed(id, on) {
    $(id).classList.toggle("on", on);
    $(id).setAttribute("aria-pressed", String(on));
  }

  function layers() {
    const nBins = data.meta.end_s / data.meta.bin_s, bin = binAt(t);
    const f = Math.min(1, t / data.meta.bin_s - bin), bin2 = Math.min(nBins - 1, bin + 1);
    const nCong = data.congestion_edges.length, nQ = sc.queues.edges.length;
    const tripData = trips(sc);

    const heads = [];
    let followed = null;
    for (const d of tripData) {
      const loc = locate(d, t);
      if (!loc) continue;
      const h = { id: d.id, position: loc.pos, color: d.color };
      heads.push(h);
      if (d.id === followId) followed = { ...h, d, next: loc.next };
    }
    if (followId !== null) updateFollow(followed);

    return [
      new deck.PathLayer({
        id: "roads", data: data.congestion_edges, pickable: true,
        getPath: (ei) => edges[ei].pts,
        getColor: (ei, { index }) => roadColor(sc.speedArr[bin * nCong + index]),
        getWidth: (ei) => (ROAD_WIDTH[edges[ei].cls] || 3) * (edges[ei].orr ? 1.6 : 1),
        widthUnits: "meters", widthMinPixels: 1, capRounded: true, jointRounded: true,
        updateTriggers: { getColor: [sc.key, bin] },
      }),
      new deck.TripsLayer({
        id: "trails", data: tripData,
        getPath: (d) => d.path, getTimestamps: (d) => d.ts, getColor: (d) => d.color,
        currentTime: t, trailLength: 150, fadeTrail: true,
        widthUnits: "meters", getWidth: 4, widthMinPixels: 1.5, capRounded: true, jointRounded: true,
        opacity: 0.75,
      }),
      new deck.ScatterplotLayer({
        id: "vehicles", data: heads, pickable: true,
        getPosition: (d) => d.position, getFillColor: (d) => d.color,
        radiusUnits: "meters", getRadius: 6, radiusMinPixels: 1.8, radiusMaxPixels: 9,
        stroked: true, getLineColor: [10, 14, 22], lineWidthMinPixels: 0.5,
      }),
      new deck.ColumnLayer({
        id: "queues", data: sc.queueData, pickable: true, extruded: true, diskResolution: 12,
        radius: 22, elevationScale: 0.5, opacity: 0.85,
        getPosition: (d) => d.position,
        getElevation: (d) => sc.queuePcu[bin * nQ + d.k] * (1 - f) + sc.queuePcu[bin2 * nQ + d.k] * f,
        getFillColor: (d) => {
          const q = sc.queuePcu[bin * nQ + d.k];
          return q > 300 ? [232, 56, 79, 230] : q > 80 ? [242, 131, 43, 210] : q >= 5 ? [244, 197, 66, 170] : [0, 0, 0, 0];
        },
        updateTriggers: { getElevation: [sc.key, t], getFillColor: [sc.key, bin] },
      }),
      new deck.ColumnLayer({
        id: "beacon", data: followed ? [followed] : [], extruded: true, diskResolution: 16,
        radius: 4, getPosition: (d) => d.position, getElevation: 160, getFillColor: [255, 255, 255, 120],
      }),
      new deck.ScatterplotLayer({
        id: "job-centres", data: data.meta.job_centres,
        getPosition: (d) => [d.lng, d.lat], getRadius: 90, radiusUnits: "meters", radiusMaxPixels: 24,
        getFillColor: [78, 161, 255, 40], stroked: true, getLineColor: [78, 161, 255, 200], lineWidthMinPixels: 1,
      }),
      new deck.TextLayer({
        id: "labels", data: data.meta.job_centres,
        getPosition: (d) => [d.lng, d.lat], getText: (d) => d.name.split(",")[0],
        getSize: 12, getColor: [220, 230, 245], getPixelOffset: [0, -16],
        fontFamily: "Inter, sans-serif", fontWeight: 600, outlineWidth: 3, outlineColor: [7, 11, 18, 230],
        fontSettings: { sdf: true },
      }),
    ];
  }

  function tooltip({ layer, object, index }) {
    if (!object) return null;
    if (layer.id === "vehicles") return `Commuter #${object.id}\nClick to ride along`;
    if (layer.id === "queues") {
      const q = Math.round(layer.props.getElevation(object));
      return `Queue of ${q.toLocaleString()} PCU\n${object.edge.cls} road${object.edge.orr ? " · ORR" : ""}`;
    }
    if (layer.id === "roads") {
      const v = sc.speedArr[binAt(t) * data.congestion_edges.length + index], g = edges[object];
      const s = v === window.OrrCore.NO_DATA ? "no vehicles entered in this 5 min" : `${Math.round(100 * v / 254)}% of free-flow speed`;
      return `${g.cls}${g.orr ? " · Outer Ring Road" : ""}\n${s}`;
    }
    return null;
  }

  function updateFollow(f) {
    if (!f) {
      const d = sc.tripById.get(followId);
      $("f-status").textContent = d && t > d.end ? "arrived" : d && t < d.depart ? "not left yet" : "";
      if (d && t < d.depart) map.jumpTo({ center: d.path[0], zoom: 16, pitch: 65 });
      return;
    }
    // Chase camera: look towards a point ~15 m ahead on the route, turning smoothly.
    const d = f.d;
    let ahead = f.next;
    while (ahead < d.path.length - 1 && metres(f.position, d.path[ahead]) < 15) ahead++;
    if (metres(f.position, d.path[ahead]) > 1) {
      const diff = ((bearing(f.position, d.path[ahead]) - followBearing + 540) % 360) - 180;
      followBearing += diff * 0.06;
    }
    map.jumpTo({ center: f.position, zoom: 16.4, pitch: 68, bearing: followBearing });
    $("f-status").textContent = `${mins(t - d.depart)} driving`;
  }

  function showFollowCard() {
    const i = followId;
    $("f-title").textContent = `Commuter #${i}`;
    $("f-sub").textContent = `Leaves ${clock(sc.depart[i])} · same commuter in every scenario`;
    $("f-rows").replaceChildren(...data.scenarios.map((s) => {
      const d = s.duration[i], row = el("div", s === sc ? "row on" : "row");
      const ff = `free-flow ${mins(s.trips.free_flow_s[i])}`;
      row.append(el("span", "", s.label), el("span", "mono", isNaN(d) ? `not arrived by 11:30 · ${ff}` : `${mins(d)} · ${ff}`));
      return row;
    }));
    $("follow-card").style.display = "block";
  }

  function follow(id) {
    if (id === undefined) {
      // Pick a commuter on the road now with a reasonably long trip ahead.
      const live = trips(sc).filter((d) => t > d.depart + 60 && t < d.end - 600);
      if (!live.length) return;
      id = live[Math.floor(Math.random() * live.length)].id;
    }
    followId = id;
    const d = sc.tripById.get(id), loc = d && locate(d, t);
    if (loc) followBearing = bearing(loc.pos, d.path[loc.next]);
    if (speedBeforeFollow === null) { speedBeforeFollow = speed; setSpeed(10); }
    for (const b of ["v-overview", "v-top", "v-follow"]) setPressed(b, b === "v-follow");
    showFollowCard();
  }

  function unfollow() {
    if (followId === null) return;
    followId = null;
    $("follow-card").style.display = "none";
    setPressed("v-follow", false);
    if (speedBeforeFollow !== null) { setSpeed(speedBeforeFollow); speedBeforeFollow = null; }
  }

  function setSpeed(v) { speed = v; $("speed").value = String(v); }

  function setView(v) {
    unfollow();
    for (const b of ["v-overview", "v-top"]) setPressed(b, b === `v-${v}`);
    map.easeTo({
      center: data.meta.center, ...(v === "top" ? { zoom: 12.4, pitch: 0, bearing: 0 } : OVERVIEW),
      duration: REDUCED_MOTION ? 0 : 1200,
    });
  }

  function setScenario(key) {
    sc = data.scenarios.find((s) => s.key === key);
    document.querySelectorAll("#scenarios button").forEach((b) => {
      b.classList.toggle("on", b.dataset.key === key);
      b.setAttribute("aria-pressed", String(b.dataset.key === key));
    });
    $("desc").textContent = sc.description;
    $("s-trip").textContent = mins(sc.summary.avg_trip_min * 60);
    $("s-delay").textContent = fmt(sc.summary.delay_veh_h);

    const head = el("tr");
    for (const h of ["Rule", "Arrived", "Avg trip", "Delay veh-h"]) head.append(el("th", "", h));
    $("compare").replaceChildren(head, ...data.scenarios.map((s) => {
      const tr = el("tr", s === sc ? "on" : "");
      tr.append(el("td", "", s.label), el("td", "", `${s.summary.finished_pct.toFixed(1)}%`),
        el("td", "", `${s.summary.avg_trip_min.toFixed(1)}m`), el("td", "", fmt(s.summary.delay_veh_h)));
      return tr;
    }));
    if (followId !== null) {
      trips(sc);
      if (sc.tripById.has(followId)) showFollowCard(); else unfollow();
    }
  }

  function drawChart() {
    const c = $("chart"), dpr = window.devicePixelRatio || 1, w = c.clientWidth, h = c.clientHeight;
    if (!w) return;
    if (c.width !== w * dpr) { c.width = w * dpr; c.height = h * dpr; }
    const g = c.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    const series = data.scenarios.map((s) => s.summary.timeline.on_road);
    const max = Math.max(...series.flat()) || 1, n = series[0].length, pad = 14;
    const x = (i) => (i / (n - 1)) * w, y = (v) => h - pad - (v / max) * (h - pad * 2);
    g.strokeStyle = "rgba(140,160,190,0.18)";
    g.lineWidth = 1;
    g.beginPath(); g.moveTo(0, y(0)); g.lineTo(w, y(0)); g.stroke();
    g.font = "10px Inter, sans-serif"; g.fillStyle = "#8d9ab0";
    g.fillText(`${fmt(max)} PCU`, 2, 10);
    for (const s of [0, 7200, 14400]) g.fillText(clock(s), Math.min(x(s / data.meta.bin_s), w - 28), h - 1);
    data.scenarios.forEach((s, k) => {
      const on = s === sc;
      g.strokeStyle = `rgba(${S_COLORS[s.key].join(",")},${on ? 1 : 0.45})`;
      g.lineWidth = on ? 2.2 : 1.2;
      g.beginPath();
      series[k].forEach((v, i) => (i ? g.lineTo(x(i), y(v)) : g.moveTo(x(i), y(v))));
      g.stroke();
    });
    g.strokeStyle = "rgba(230,236,245,0.7)"; g.lineWidth = 1;
    const cx = x(t / data.meta.bin_s);
    g.beginPath(); g.moveTo(cx, 12); g.lineTo(cx, h - pad); g.stroke();
  }

  function updateHud() {
    $("clock").textContent = clock(t);
    $("scrub").value = String(Math.round(t));
    $("scrub").setAttribute("aria-valuetext", clock(t));
    const tl = sc.summary.timeline, i = binAt(t);
    $("s-onroad").textContent = fmt(tl.on_road[i]);
    $("s-arrived").textContent = fmt(tl.arrived[i]);
    const hr = t / 3600;
    $("day-phase").textContent = hr < 0.75 ? "Early commute" : hr < 2.5 ? "Peak departures"
      : hr < 3 ? "Last departures" : "Clearing the backlog";
    drawChart();
  }

  function setPlaying(on) {
    playing = on;
    $("play").textContent = on ? "❚❚" : "▶";
    $("play").setAttribute("aria-label", on ? "Pause" : "Play");
  }

  let lastHud = 0;
  function frame(now) {
    const dt = Math.min(0.1, (now - last) / 1000);
    last = now;
    if (playing) {
      t += dt * speed;
      if (t >= data.meta.end_s) { t = data.meta.end_s; setPlaying(false); }
    }
    overlay.setProps({ layers: layers() });
    if (now - lastHud > 120) { updateHud(); lastHud = now; }
    requestAnimationFrame(frame);
  }

  function wireControls() {
    $("scenarios").replaceChildren(...data.scenarios.map((s, k) => {
      const b = el("button"), dot = el("span", "dot");
      dot.style.background = `rgb(${S_COLORS[s.key]})`;
      b.type = "button";
      b.dataset.key = s.key;
      b.title = `${s.description} (${k + 1})`;
      b.append(dot, s.label);
      b.onclick = () => setScenario(s.key);
      return b;
    }));
    const togglePlay = () => {
      if (!playing && t >= data.meta.end_s) t = 0;
      setPlaying(!playing);
    };
    setPlaying(playing);
    $("play").onclick = togglePlay;
    $("scrub").max = String(data.meta.end_s);
    $("scrub").oninput = (e) => { t = Number(e.target.value); };
    $("speed").onchange = (e) => { speed = Number(e.target.value); };
    $("v-overview").onclick = () => setView("overview");
    $("v-top").onclick = () => setView("top");
    $("v-follow").onclick = () => follow();
    $("toggle-side").onclick = () => {
      const open = $("side").classList.toggle("open");
      $("toggle-side").setAttribute("aria-expanded", String(open));
    };
    map.on("dragstart", unfollow);
    window.addEventListener("keydown", (e) => {
      const tag = e.target.tagName;
      if (tag === "INPUT" || tag === "SELECT") return;
      if (e.code === "Space") {
        if (tag === "BUTTON") return;   // the focused button handles its own activation
        e.preventDefault();
        togglePlay();
      }
      if (e.key === "f") follow();
      if (e.key === "Escape") setView("overview");
      const k = Number(e.key);
      if (k >= 1 && k <= data.scenarios.length) setScenario(data.scenarios[k - 1].key);
    });
    window.addEventListener("resize", drawChart);
  }

  function addBuildings() {
    if (!map.getSource("openmaptiles") || map.getLayer("buildings-3d")) return;
    const firstSymbol = map.getStyle().layers.find((l) => l.type === "symbol");
    map.addLayer({
      id: "buildings-3d", type: "fill-extrusion", source: "openmaptiles", "source-layer": "building", minzoom: 13,
      paint: {
        "fill-extrusion-color": ["interpolate", ["linear"], ["coalesce", ["get", "render_height"], 8], 0, "#151c29", 60, "#24304a"],
        "fill-extrusion-height": ["coalesce", ["get", "render_height"], 8],
        "fill-extrusion-base": ["coalesce", ["get", "render_min_height"], 0],
        "fill-extrusion-opacity": 0.85,
      },
    }, firstSymbol && firstSymbol.id);
  }

  function fail(message) {
    $("loading-msg").textContent = message;
    document.body.dataset.state = "error";
  }

  async function main() {
    if (!document.createElement("canvas").getContext("webgl2")) {
      fail("This viewer needs WebGL 2. Try a recent Chrome, Edge, Firefox or Safari with hardware acceleration on.");
      return;
    }
    try {
      const res = await fetch("data.json");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      $("loading-msg").textContent = "Preparing vehicles";
      data = await res.json();
    } catch (err) {
      fail(`Could not load data.json (${err.message}). Serve this folder over HTTP, ` +
        "e.g. python -m http.server -d output/sim3d 8000");
      return;
    }
    edges = prepareEdges(data.edges);
    data.scenarios.forEach((s) => prepareScenario(s, edges));
    const m = data.meta;
    $("meta-line").textContent = `${m.agents.toLocaleString()} simulated vehicles × ${m.pcu_per_agent} PCU · ` +
      `${m.shown.toLocaleString()} animated · built ${m.generated}`;

    let styleLoaded = false;
    map = new maplibregl.Map({
      container: "map", style: BASEMAP, center: m.center, ...OVERVIEW,
      maxPitch: 75, attributionControl: { compact: true },
    });
    map.on("error", (e) => {
      if (styleLoaded) return;
      styleLoaded = true;
      console.warn("Basemap unavailable, using a plain background", e.error);
      map.setStyle(FALLBACK_STYLE);
    });
    map.on("style.load", () => { styleLoaded = true; addBuildings(); });
    overlay = new deck.MapboxOverlay({
      interleaved: true, layers: [], getTooltip: tooltip,
      onClick: ({ layer, object }) => { if (layer && layer.id === "vehicles" && object) follow(object.id); },
    });
    map.addControl(overlay);
    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");

    wireControls();
    setScenario("waze");
    $("loading").hidden = true;
    document.body.dataset.state = "ready";
    requestAnimationFrame(frame);
  }

  main();
})();
