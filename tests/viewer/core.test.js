// Unit tests for viewer/core.js. Run with: node --test tests/viewer/
const test = require("node:test");
const assert = require("node:assert/strict");
const core = require("../../viewer/core.js");

const b64 = (typed) => Buffer.from(typed.buffer).toString("base64");

// Two links at the equator: link 0 runs east (2 points), link 1 east then north (3 points).
// Trip 0 drives both, passing its nodes at 100, 200 and 400 s; trip 1 never left its first link.
function scenario() {
  const edges = core.prepareEdges({
    coords: [[0, 0, 0.001, 0], [0.001, 0, 0.002, 0, 0.002, 0.001]],
    cls: ["primary", "residential"], orr: [1, 0],
  });
  const s = {
    trips: {
      offsets: b64(new Uint32Array([0, 2, 2])),
      edges: b64(new Uint16Array([0, 1])),
      times: b64(new Uint16Array([100, 200, 400, 50])),
      free_flow_s: [250, 90], arrived: [1, 0],
    },
    speed: b64(new Uint8Array([254, 255])),
    queues: { edges: [1], pcu: b64(new Uint16Array([12])) },
  };
  return { edges, s: core.prepareScenario(s, edges) };
}

test("decode restores typed arrays", () => {
  assert.deepEqual([...core.decode(b64(new Uint16Array([1, 65535])), Uint16Array)], [1, 65535]);
  assert.deepEqual([...core.decode(b64(new Uint32Array([70000])), Uint32Array)], [70000]);
});

test("clock adds seconds to the start time and wraps at midnight", () => {
  assert.equal(core.clock(0, "07:00"), "07:00");
  assert.equal(core.clock(5459, "07:00"), "08:30");
  assert.equal(core.clock(3600, "23:30"), "00:30");
});

test("bearing is a compass bearing", () => {
  assert.ok(Math.abs(core.bearing([0, 0], [0.001, 0]) - 90) < 1e-9);
  assert.ok(Math.abs(core.bearing([0, 0], [0, 0.001])) < 1e-9);
});

test("prepareEdges measures each point's share of the link length", () => {
  const { edges } = scenario();
  assert.deepEqual(edges[0].frac, [0, 1]);
  assert.ok(Math.abs(edges[1].frac[1] - 0.5) < 0.01);
  assert.equal(edges[0].orr, 1);
});

test("prepareScenario exposes per-commuter departure and duration", () => {
  const { s } = scenario();
  assert.deepEqual([...s.depart], [100, 50]);
  assert.equal(s.duration[0], 300);
  assert.ok(Number.isNaN(s.duration[1]));
  assert.equal(s.queueData[0].position[1], 0.001);   // queue sits at the end of its link
});

test("buildTrips spreads link times along each link and skips trips with no links", () => {
  const { edges, s } = scenario();
  const trips = core.buildTrips(s, edges, 16200);
  assert.equal(trips.length, 1);
  const [d] = trips;
  assert.equal(d.path.length, 4);                     // shared junction counted once
  assert.deepEqual(d.ts.map(Math.round), [100, 200, 300, 400]);
  assert.equal(d.end, 400);
  assert.deepEqual(d.color, core.TRAIL.onTime);       // 300 s vs 250 s free-flow
  assert.equal(core.buildTrips(s, edges, 16200), trips, "result is cached");
});

test("locate interpolates position and stays null off the road", () => {
  const { edges, s } = scenario();
  const [d] = core.buildTrips(s, edges, 16200);
  assert.equal(core.locate(d, 99), null);
  assert.equal(core.locate(d, 401), null);
  const mid = core.locate(d, 150);
  assert.ok(Math.abs(mid.pos[0] - 0.0005) < 1e-9 && mid.next === 1);
  assert.deepEqual(core.locate(d, 400).pos, [0.002, 0.001]);
});

test("a trip still driving at the end waits at its last junction", () => {
  const d = { depart: 0, end: 16200, ts: [0, 10], path: [[0, 0], [1, 1]] };
  assert.deepEqual(core.locate(d, 5000).pos, [1, 1]);
});

test("trip colours reflect delay against free-flow time", () => {
  assert.deepEqual(core.tripColor(true, 100, 100), core.TRAIL.onTime);
  assert.deepEqual(core.tripColor(true, 200, 100), core.TRAIL.late);
  assert.deepEqual(core.tripColor(true, 300, 100), core.TRAIL.veryLate);
  assert.deepEqual(core.tripColor(false, 100, 100), core.TRAIL.veryLate);
});

test("roadColor bands speed bytes", () => {
  assert.equal(core.roadColor(core.NO_DATA)[3], 70);
  assert.deepEqual(core.roadColor(254).slice(0, 3), core.ROAD.free);
  assert.deepEqual(core.roadColor(150).slice(0, 3), core.ROAD.moderate);
  assert.deepEqual(core.roadColor(100).slice(0, 3), core.ROAD.slow);
  assert.deepEqual(core.roadColor(50).slice(0, 3), core.ROAD.jam);
});
