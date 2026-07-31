/* Self-check for the branching logic in common.js -- run with:
 *     node docs/common.test.js
 *
 * Two things are covered, both places where a wrong answer is silent:
 *   - journeyState: the departure-day mapping and the overnight rollback.
 *     Getting it wrong shows a train as "arrived" while it's still out there.
 *   - the delay scale and its formatters. These decide the colour and the
 *     wording of every number on the register, so an off-by-one at a band
 *     edge mislabels a late train as punctual and nothing else notices. */

const fs = require("fs");
const vm = require("vm");
const path = require("path");
const assert = require("assert");

const ctx = { console };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, "common.js"), "utf8"), ctx);
const { journeyState, fmtDelay, scaleOf, shiftTime, runDaysText, band,
        arrivalGap, arrivalMinutes, fmtDuration,
        journeyMinutes, delayShare, dayOffset, scheduleStack } = ctx;

const DAILY = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const day   = { scheduled_departure: "06:00:00", scheduled_arrival: "12:00:00", arrival_day_offset: 0, run_days: DAILY };
const over  = { scheduled_departure: "22:00:00", scheduled_arrival: "06:00:00", arrival_day_offset: 1, run_days: DAILY };
const noOff = { ...over, arrival_day_offset: 0 };

// 2026-07-28 is a Tuesday.
const cases = [
  ["before departure",       day,                          "2026-07-28T05:00", "before/0"],
  ["halfway",                day,                          "2026-07-28T09:00", "running/50"],
  ["just departed",          day,                          "2026-07-28T06:00", "running/0"],
  ["after arrival",          day,                          "2026-07-28T13:00", "done/100"],
  ["overnight midway",       over,                         "2026-07-28T02:00", "running/50"],
  ["overnight, no offset",   noOff,                        "2026-07-28T02:00", "running/50"],
  ["no schedule",            { run_days: DAILY },          "2026-07-28T09:00", "null"],
  ["unknown run_days = yes", { ...day, run_days: null },   "2026-07-28T09:00", "running/50"],
  ["not running today",      { ...day, run_days: ["thu"] },"2026-07-28T09:00", "null"],
  ["+1 maps to dep day",     { ...over, run_days: ["mon"] },"2026-07-28T02:00", "running/50"],
  ["+1 wrong dep day",       { ...over, run_days: ["tue"] },"2026-07-28T02:00", "null"],
];

for (const [name, train, now, want] of cases) {
  const j = journeyState(train, new Date(now));
  assert.strictEqual(j ? `${j.key}/${Math.round(j.pct)}` : "null", want, name);
}

// ---- Delay scale and formatting -------------------------------------------

// The three forms the handoff specifies, plus the no-data case.
assert.strictEqual(fmtDelay(42), "+42");
assert.strictEqual(fmtDelay(59), "+59");
assert.strictEqual(fmtDelay(60), "+1:00");
assert.strictEqual(fmtDelay(138), "+2:18");
assert.strictEqual(fmtDelay(0), "ON TIME");
assert.strictEqual(fmtDelay(-4), "ON TIME");     // early is not "-4 late"
assert.strictEqual(fmtDelay(null), "—");

// Band edges belong to the gentler step: 5/20/60 are the last minute of each.
assert.strictEqual(scaleOf(5), "var(--ontime)");
assert.strictEqual(scaleOf(6), "var(--minor)");
assert.strictEqual(scaleOf(20), "var(--minor)");
assert.strictEqual(scaleOf(21), "var(--late)");
assert.strictEqual(scaleOf(60), "var(--late)");
assert.strictEqual(scaleOf(61), "var(--severe)");
assert.strictEqual(scaleOf(null), "var(--text-3)");

// Observed arrival = timetable + delay, wrapping past midnight.
assert.strictEqual(shiftTime("10:40:00", 138), "12:58");
assert.strictEqual(shiftTime("23:50:00", 20), "00:10");
assert.strictEqual(shiftTime("06:25:00", 0), "06:25");
assert.strictEqual(shiftTime(null, 10), "–");

// Chips read Mon-first however the array arrived from Postgres.
assert.strictEqual(runDaysText(["fri", "tue"]), "runs Tue, Fri");
assert.strictEqual(runDaysText(["mon", "tue", "wed", "thu", "fri", "sat", "sun"]), "daily");
assert.strictEqual(runDaysText(null), "schedule unknown");

// The on-time *verdict* stays on the DB's <= 10 rule, not the colour step --
// the two are deliberately different and must not drift into each other.
assert.strictEqual(band(10).cls, "good");
assert.strictEqual(band(11).cls, "warn");

// ---- Timetable arithmetic for unpolled runs --------------------------------
// Sign is load-bearing: positive drives the "at least this late" floor and
// negative drives the countdown, so a flip would assert a delay for every
// train still on its way.
const at = (clock) => new Date(`2026-07-29T${clock}:00`);
const arr2055 = { scheduled_arrival: "20:55:00" };

assert.strictEqual(arrivalGap(arr2055, at("20:25")), -30);   // due in 30 min
assert.strictEqual(arrivalGap(arr2055, at("20:55")), 0);     // due exactly now
assert.strictEqual(arrivalGap(arr2055, at("21:25")), 30);    // 30 min overdue
assert.strictEqual(arrivalGap({ scheduled_arrival: "17:35:00" }, at("20:25")), 170);
assert.strictEqual(arrivalGap({ scheduled_arrival: null }, at("20:25")), null);

// Unknown schedules sort last rather than to the top of the day.
assert.strictEqual(arrivalMinutes({ scheduled_arrival: "05:05:00" }), 305);
assert.strictEqual(arrivalMinutes({ scheduled_arrival: "23:10:00" }), 1390);
assert.strictEqual(arrivalMinutes({ scheduled_arrival: null }), Infinity);

assert.strictEqual(fmtDuration(30), "30M");
assert.strictEqual(fmtDuration(59), "59M");
assert.strictEqual(fmtDuration(60), "1H00");
assert.strictEqual(fmtDuration(155), "2H35");

// ---- Journey length and relative delay -------------------------------------
// journeyMinutes is the denominator for every share on the page and is also
// what journeyState derives its departure from, so an error here moves the
// progress track as well as the metric.
assert.strictEqual(journeyMinutes(day), 360);            // 06:00 -> 12:00
assert.strictEqual(journeyMinutes(over), 480);           // 22:00 -> 06:00 (+1)
assert.strictEqual(journeyMinutes(noOff), 480);          // same run, offset missing
assert.strictEqual(journeyMinutes({ ...day, arrival_day_offset: 1 }), 1800);   // 30h
assert.strictEqual(journeyMinutes({ scheduled_arrival: "12:00:00" }), null);   // no departure

// 05580: ANVT 05:15 -> PRNC 11:30 next day = 30h15m, and 630 min late is a
// quarter of the run -- the case that motivated the metric.
const purnia = { scheduled_departure: "05:15:00", scheduled_arrival: "11:30:00", arrival_day_offset: 1 };
assert.strictEqual(journeyMinutes(purnia), 1815);
assert.strictEqual(Math.round(delayShare(630, purnia) * 1000) / 10, 34.7);

// Same absolute delay, very different facts.
assert.ok(delayShare(30, day) > delayShare(30, purnia));
assert.strictEqual(delayShare(0, day), 0);               // on time is a real 0%
assert.strictEqual(delayShare(null, day), null);
assert.strictEqual(delayShare(30, { scheduled_arrival: "12:00:00" }), null);

// ---- Day offset ------------------------------------------------------------
// Rows are anchored on the ARRIVAL day, so the offset belongs on the departure
// as a negative. Flipping it back to "+1" on the arrival is silent and reads
// as "arrives tomorrow" on a train that finished this morning.
assert.strictEqual(dayOffset(day), "");                          // same-day run
assert.strictEqual(dayOffset({ scheduled_arrival: "06:00:00", arrival_day_offset: 1 }), "");
assert.match(dayOffset(over), /&minus;1/);
assert.match(dayOffset({ ...day, arrival_day_offset: 2 }), /&minus;2/);
assert.match(dayOffset(over), /1 day before/);                   // singular
assert.match(dayOffset({ ...day, arrival_day_offset: 2 }), /2 days before/);
assert.ok(!/\+/.test(dayOffset(over)), "offset must not read as +1");

// Placement is the whole point: the marker sits with 22:00, not with 06:00.
const stack = scheduleStack(over, null);
assert.ok(stack.indexOf("&minus;1") < stack.indexOf("06:00"),
          "offset must precede the arrival time");
assert.ok(stack.indexOf("22:00") < stack.indexOf("&minus;1"),
          "offset must follow the departure time");

console.log(`${cases.length} journeyState cases + 56 scale/format/timetable/share/offset cases pass`);
