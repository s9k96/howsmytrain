/* Self-check for the outage grouping on health.html -- run with:
 *     node docs/health.test.js
 *
 * Grouping is where this goes quietly wrong. Too tight a tolerance and one
 * two-hour outage is listed as nine, burying it; too loose and two unrelated
 * nights merge into one. Neither looks broken on screen, which is why the
 * boundaries are pinned here rather than eyeballed.
 *
 * The page's script is pulled out of the HTML and run against a stub document,
 * rather than duplicated here -- a copy would keep passing after the page
 * changed. */

const fs = require("fs");
const vm = require("vm");
const path = require("path");
const assert = require("assert");

const stub = () => new Proxy({}, { get: () => stub, set: () => true, apply: () => stub });
const ctx = {
  console,
  document: { addEventListener() {}, getElementById: () => stub(), querySelectorAll: () => [] },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, "common.js"), "utf8"), ctx);
const html = fs.readFileSync(path.join(__dirname, "health.html"), "utf8");
const inline = html.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(inline, "health.html should contain one inline <script> block");
vm.runInContext(inline[1], ctx);

const { findIncidents, failuresByHour, missedIn, pollsByArrivalDate } = ctx;
const GAP = vm.runInContext("INCIDENT_GAP_MIN", ctx);

// Heartbeats every 15 min from a base time. `bad` marks the ones where every
// poll failed, which is what the page treats as an outage.
const BASE = new Date("2026-08-08T22:00:00+05:30");
const at = (min) => new Date(BASE.getTime() + min * 6e4).toISOString();
const run = (min, { due = 0, ok = 0, failed = 0, reason = null } = {}) =>
  ({ ran_at: at(min), due_count: due, ok_count: ok, failed_count: failed, failure_reason: reason });

const ok = (min) => run(min, { due: 1, ok: 1 });
const idle = (min) => run(min, { due: 0 });
const bad = (min, reason) => run(min, { due: 2, failed: 2, reason });

// ---- nothing wrong --------------------------------------------------------
const healthy = [0, 15, 30, 45, 60].map(m => (m === 30 ? idle(m) : ok(m)));
assert.deepStrictEqual([...findIncidents(healthy, new Date(at(65)))], [],
  "a clean run of heartbeats is not an incident");

// A run that found nothing due cannot be a failure -- most runs find nothing
// due, and counting them would report a permanent outage.
assert.strictEqual(findIncidents([idle(0), idle(15), idle(30)], new Date(at(35))).length, 0);

// ---- one outage, not nine -------------------------------------------------
const burst = [ok(0), bad(15), bad(30), bad(45), bad(60), ok(75)];
const found = findIncidents(burst, new Date(at(80)));
assert.strictEqual(found.length, 1, "consecutive failures are one incident");
assert.strictEqual(found[0].kind, "polls failing");
assert.strictEqual(found[0].runs, 4);
assert.strictEqual(found[0].polls, 8);
assert.strictEqual((found[0].end - found[0].start) / 6e4, 45);

// ---- two outages a long way apart stay two --------------------------------
const twice = [bad(0), ok(15), ok(30), ok(45), bad(60)];
assert.strictEqual(findIncidents(twice, new Date(at(65))).length, 2,
  `failures more than ${GAP} min apart are separate incidents`);

// A tick with nothing due says nothing either way, so it must not split an
// outage in half -- during a long one, plenty of ticks find nothing due.
// Exactly at the tolerance still counts as one; a minute past it does not.
// (Heartbeats stay 15 min apart here, or the gap itself becomes a silence.)
assert.strictEqual(findIncidents([bad(0), idle(15), bad(GAP)], new Date(at(GAP + 5))).length, 1);
assert.strictEqual(findIncidents([bad(0), idle(15), bad(GAP + 1)], new Date(at(GAP + 6))).length, 2);

// But a poll that got through is recovery, not a lull: two failures either
// side of a success are two incidents however close together they are.
const recovered = findIncidents([bad(0), ok(15), bad(30)], new Date(at(35)));
assert.strictEqual(recovered.length, 2,
  "a successful poll between two failures ends the first outage");

// ---- the scheduler going quiet is a different failure ---------------------
const silence = [ok(0), ok(15), ok(150), ok(165)];
const gaps = findIncidents(silence, new Date(at(170))).filter(i => i.kind === "scheduler silent");
assert.strictEqual(gaps.length, 1);
assert.strictEqual((gaps[0].end - gaps[0].start) / 6e4, 135);
assert.strictEqual(gaps[0].polls, 0, "a run that never happened loses no polls");

// The silence we care about most is the one still going on. Without this the
// only outage that never appears is the one happening right now.
const stopped = findIncidents([ok(0), ok(15)], new Date(at(200)));
assert.strictEqual(stopped.length, 1);
assert.strictEqual(stopped[0].ongoing, true);

// ---- newest first, so the last night is the first row ---------------------
const ordered = findIncidents([bad(0), ok(15), ok(30), ok(45), bad(60)], new Date(at(70)));
assert.ok(ordered[0].start > ordered[1].start, "incidents are listed newest first");

// ---- the reason, once the column exists -----------------------------------
const reasoned = findIncidents([bad(0, "rate-limited"), bad(15, "rate-limited"), bad(30, "http-503")],
                               new Date(at(40)));
assert.deepStrictEqual([...reasoned[0].reasons].sort(), ["http-503", "rate-limited"]);
// Rows written before the column existed carry none, and must not invent one.
assert.strictEqual(findIncidents([bad(0)], new Date(at(10)))[0].reasons.size, 0);

// ---- failuresByHour -------------------------------------------------------
const hours = failuresByHour([bad(0), bad(15), ok(120)]);   // 22:00, 22:15, 24:00 IST
assert.strictEqual(hours.length, 24);
assert.strictEqual(hours.reduce((a, h) => a + h.failed, 0), 4);
assert.strictEqual(hours.reduce((a, h) => a + h.attempts, 0), 5, "attempts count ok and failed");
const busiest = hours.indexOf(hours.reduce((a, b) => (b.failed > a.failed ? b : a)));
assert.strictEqual(busiest, new Date(at(0)).getHours(), "failures land in the hour they happened");

// ---- what an outage actually cost ----------------------------------------
// A train arriving 23:00 has a window of 22:50..00:30. The question is whether
// any poll got through while it was open -- not whether the window sits inside
// the incident's timestamps, which is the same thing right up until it isn't:
// a heartbeat is stamped to the second, and 22469's window opening at 23:00:00
// fell "outside" an outage whose first failing run is stamped 23:00:47.
const DAILY = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const train = { train_number: "22469", scheduled_arrival: "23:00:00",
                arrival_day_offset: 0, run_days: DAILY };
const outage = { start: new Date(at(0)), end: new Date(at(240)) };   // 22:00..02:00

// Nothing succeeded while the window was open: the journey is gone, because
// RailRadar only ever serves current runs.
assert.strictEqual(missedIn(outage, [train], [bad(50), bad(80), bad(110)]), 1);

// One poll got through inside the window, so the journey was read.
assert.strictEqual(missedIn(outage, [train], [bad(50), ok(80), bad(110)]), 0);

// Off by 47 seconds at the boundary must not change the answer.
const late = { start: new Date(BASE.getTime() + 50 * 6e4 + 47000), end: new Date(at(240)) };
assert.strictEqual(missedIn(late, [train], [bad(51), bad(110)]), 1,
  "seconds on a heartbeat must not decide whether a journey counts as lost");

// An outage that never overlaps the window costs nothing.
assert.strictEqual(missedIn({ start: new Date(at(0)), end: new Date(at(20)) },
                            [train], [bad(10)]), 0);
// And a train that doesn't run that day cannot be missed on it.
assert.strictEqual(missedIn(outage, [{ ...train, run_days: ["mon"] }], [bad(50)]), 0,
  "2026-08-08 is a Saturday");
assert.strictEqual(missedIn(outage, [{ ...train, scheduled_arrival: null }], [bad(50)]), 0);

// ---- polls are filed by departure, the page counts arrivals ---------------
// 04014 departs 15:40 and arrives 20:15 the next day, so the journey arriving
// Aug 8 is filed under Aug 7. Looking it up by arrival date found nothing and
// reported the train missed -- while it was being polled correctly every day.
const overnight = { train_number: "04014", arrival_day_offset: 1 };
const sameDay = { train_number: "12005", arrival_day_offset: 0 };
const keyed = pollsByArrivalDate(
  [{ train_number: "04014", journey_date: "2026-08-07" },
   { train_number: "12005", journey_date: "2026-08-07" }],
  [overnight, sameDay],
);
assert.ok(keyed["2026-08-08"].has("04014"), "an overnight run counts on the day it arrives");
assert.ok(!(keyed["2026-08-07"] || new Set()).has("04014"),
  "and not on the day it departed, or it vouches for a journey it is not");
assert.ok(keyed["2026-08-07"].has("12005"), "a same-day train is unaffected");

// Month boundaries have to roll over, not produce "2026-07-32".
const rolled = pollsByArrivalDate([{ train_number: "04014", journey_date: "2026-07-31" }], [overnight]);
assert.ok(rolled["2026-08-01"].has("04014"));

// A two-day run lands two days later; a poll with no journey_date is dropped
// rather than keyed to NaN.
const twoDay = pollsByArrivalDate([{ train_number: "12423", journey_date: "2026-08-06" },
                                   { train_number: "12423", journey_date: null }],
                                  [{ train_number: "12423", arrival_day_offset: 2 }]);
assert.ok(twoDay["2026-08-08"].has("12423"));
assert.strictEqual(Object.keys(twoDay).length, 1);


console.log(`health: 34 outage and coverage-keying assertions pass `
  + `(tolerance ${GAP} min, both failure kinds, cost in missed windows)`);
