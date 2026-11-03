// Pure data functions for the ORR 3D viewer: decoding sim3d.py's data.json and placing
// vehicles in time. No DOM or WebGL here, so it runs unchanged in the browser
// (window.OrrCore) and in Node for the unit tests (require("./core.js")).
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.OrrCore = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const ROAD = { free: [43, 217, 165], moderate: [244, 197, 66], slow: [242, 131, 43], jam: [232, 56, 79] };
  const TRAIL = { onTime: [127, 231, 255], late: [255, 181, 71], veryLate: [255, 84, 112] };
  const NO_DATA = 255;   // speed byte meaning "no vehicle entered the link in this interval"

  function decode(b64, Type) {
    // Base64 -> typed array. sim3d.py writes little-endian numpy arrays, which matches
    // every platform browsers run on.
    const bin = atob(b64), u8 = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
    return new Type(u8.buffer);
  }

  function metres(a, b) {
    const kx = 111320 * Math.cos(a[1] * Math.PI / 180);
    return Math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * 110540);
  }

  function bearing(a, b) {
    // Compass bearing in degrees (0 = north, 90 = east) from [lng, lat] a to b.
    const kx = Math.cos(a[1] * Math.PI / 180);
    return Math.atan2((b[0] - a[0]) * kx, b[1] - a[1]) * 180 / Math.PI;
  }

  function clock(seconds, start) {
    // "HH:MM" for `seconds` after the "HH:MM" clock start.
    const [h, m] = start.split(":").map(Number);
    const total = h * 60 + m + Math.floor(seconds / 60);
    return `${String(Math.floor(total / 60) % 24).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
  }

  function prepareEdges(e) {
    // Each link's points plus the fraction of its length reached at every point.
    return e.coords.map((c, i) => {
      const pts = [];
      for (let j = 0; j < c.length; j += 2) pts.push([c[j], c[j + 1]]);
      const cum = [0];
      for (let j = 1; j < pts.length; j++) cum.push(cum[j - 1] + metres(pts[j - 1], pts[j]));
      const L = cum[cum.length - 1] || 1;
      return { pts, frac: cum.map((d) => d / L), cls: e.cls[i], orr: e.orr[i] };
    });
  }

  function prepareScenario(s, edges) {
    const off = decode(s.trips.offsets, Uint32Array), E = decode(s.trips.edges, Uint16Array);
    const T = decode(s.trips.times, Uint16Array);
    s.speedArr = decode(s.speed, Uint8Array);
    s.queuePcu = decode(s.queues.pcu, Uint16Array);
    s.queueData = s.queues.edges.map((ei, k) => ({ k, edge: edges[ei], position: edges[ei].pts[edges[ei].pts.length - 1] }));
    // Per-commuter departure and duration, for comparing one commuter across scenarios.
    const n = off.length - 1;
    s.depart = new Float32Array(n);
    s.duration = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      s.depart[i] = T[off[i] + i];
      s.duration[i] = s.trips.arrived[i] ? T[off[i + 1] + i] - T[off[i] + i] : NaN;
    }
    s.raw = { off, E, T };
    s.tripData = null;
    return s;
  }

  function tripColor(arrived, durationS, freeFlowS) {
    const ratio = durationS / Math.max(freeFlowS, 1);
    return !arrived || ratio > 2.5 ? TRAIL.veryLate : ratio > 1.5 ? TRAIL.late : TRAIL.onTime;
  }

  function buildTrips(s, edges, endS) {
    // Vertex-by-vertex path and timestamps for every animated trip: each link's entry and
    // exit times are spread along its length. Trip i drove E[off[i]:off[i+1]] and passed
    // its nodes at T[off[i]+i : off[i+1]+i+1] (see sim3d.export).
    if (s.tripData) return s.tripData;
    const { off, E, T } = s.raw, out = [];
    for (let i = 0; i < off.length - 1; i++) {
      const a = off[i], b = off[i + 1];
      if (b === a) continue;   // still on its first link when the run ended
      const path = [], ts = [];
      for (let k = a; k < b; k++) {
        const g = edges[E[k]], t1 = T[k + i], t2 = T[k + i + 1];
        for (let j = k === a ? 0 : 1; j < g.pts.length; j++) {
          path.push(g.pts[j]);
          ts.push(t1 + (t2 - t1) * g.frac[j]);
        }
      }
      const arrived = Boolean(s.trips.arrived[i]);
      out.push({
        id: i, path, ts, arrived,
        color: tripColor(arrived, ts[ts.length - 1] - ts[0], s.trips.free_flow_s[i]),
        depart: ts[0], end: arrived ? ts[ts.length - 1] : endS,
      });
    }
    s.tripData = out;
    s.tripById = new Map(out.map((d) => [d.id, d]));
    return out;
  }

  function locate(d, time) {
    // Position of trip d at `time` and the index of its next vertex, or null when it is not
    // on the road. A trip still driving when the run ended waits at the last junction reached.
    if (time < d.depart || time > d.end) return null;
    const ts = d.ts, n = ts.length;
    if (time >= ts[n - 1]) return { pos: d.path[n - 1], next: n - 1 };
    let lo = 0, hi = n - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (ts[mid] <= time) lo = mid; else hi = mid;
    }
    const f = ts[hi] > ts[lo] ? (time - ts[lo]) / (ts[hi] - ts[lo]) : 0, p = d.path[lo], q = d.path[hi];
    return { pos: [p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f], next: hi };
  }

  function roadColor(v) {
    // Colour for a link's speed byte (0-254 = share of free-flow speed, 255 = no data).
    if (v === NO_DATA) return [90, 104, 130, 70];
    const r = v / 254;
    const c = r >= 0.75 ? ROAD.free : r >= 0.5 ? ROAD.moderate : r >= 0.3 ? ROAD.slow : ROAD.jam;
    return [...c, r >= 0.75 ? 150 : 220];
  }

  return { ROAD, TRAIL, NO_DATA, decode, metres, bearing, clock, prepareEdges, prepareScenario,
           tripColor, buildTrips, locate, roadColor };
});
