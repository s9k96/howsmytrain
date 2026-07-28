/* Self-check for the date logic in common.js -- run with: node docs/common.test.js
 *
 * Only journeyState is covered: it's the one place with real branching (the
 * departure-day mapping and the overnight rollback), and getting it wrong
 * shows a train as "arrived" while it's still out there. */

const fs = require("fs");
const vm = require("vm");
const path = require("path");
const assert = require("assert");

const ctx = { console };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, "common.js"), "utf8"), ctx);
const { journeyState } = ctx;

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
console.log(`${cases.length} journeyState cases pass`);
