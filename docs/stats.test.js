/* Self-check for the statistics on stats.html -- run with:
 *     node docs/stats.test.js
 *
 * Two things are covered, both silent when wrong:
 *   - build(): every figure for a train comes off one array of journeys, so a
 *     mistake here shows a median that disagrees with its own average and
 *     nothing flags it.
 *   - sortRows(): nulls must sink in BOTH directions. A train with no data
 *     that floats to the top of an ascending "avg delay" sort reads as the
 *     most punctual service in the fleet.
 *
 * The page's script is pulled out of the HTML and run against a stub document,
 * rather than duplicated here -- a copy would keep passing after the page
 * changed. */

const fs = require("fs");
const vm = require("vm");
const path = require("path");
const assert = require("assert");

// Enough DOM for the script's top level and its DOMContentLoaded registration;
// nothing here renders, so the stubs only have to not throw.
const stub = () => new Proxy({}, { get: () => stub, set: () => true, apply: () => stub });
const ctx = {
  console,
  document: { addEventListener() {}, getElementById: () => stub(), querySelectorAll: () => [] },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, "common.js"), "utf8"), ctx);

const html = fs.readFileSync(path.join(__dirname, "stats.html"), "utf8");
const inline = html.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(inline, "stats.html should contain one inline <script> block");
vm.runInContext(inline[1], ctx);

// Function declarations land on the vm's global object; `const`/`let` do not,
// so COLS has to be read by evaluating its name inside the context.
const { build, sortRows, median, binOf } = ctx;
const COLS = vm.runInContext("COLS", ctx);
const SHARE_BINS = vm.runInContext("SHARE_BINS", ctx);

// ---- median ---------------------------------------------------------------
assert.strictEqual(median([]), null);
assert.strictEqual(median([7]), 7);
assert.strictEqual(median([9, 1, 5]), 5);          // sorts numerically, not lexically
assert.strictEqual(median([10, 2]), 6);
assert.strictEqual(median([100, 20, 3]), 20);      // "100" < "20" as strings -- the trap

// ---- build ----------------------------------------------------------------
const shatabdi = {
  train_number: "12005", name: "Kalka Shatabdi",
  scheduled_departure: "17:15:00", scheduled_arrival: "21:15:00", arrival_day_offset: 0,
};
const longHaul = {
  train_number: "12621", name: "Tamil Nadu Express",
  scheduled_departure: "22:00:00", scheduled_arrival: "06:30:00", arrival_day_offset: 2,
};

const [a, b] = build([shatabdi, longHaul], [
  { train_number: "12005", journey_date: "2026-07-26", delay_minutes: 5 },
  { train_number: "12005", journey_date: "2026-07-27", delay_minutes: 20 },
  { train_number: "12005", journey_date: "2026-07-28", delay_minutes: null },
  { train_number: "12621", journey_date: "2026-07-27", delay_minutes: 30 },
]);

assert.strictEqual(a.runs, 2, "a null delay is not a run we can average");
assert.strictEqual(a.unusable, 1, "but it is still a journey we saw");
assert.strictEqual(a.avg, 12.5);
assert.strictEqual(a.med, 12.5);
assert.strictEqual(a.worst, 20);
assert.strictEqual(a.onTime, 50);                  // 5 min is on time, 20 is not
assert.strictEqual(a.span, 240);                   // 17:15 -> 21:15

// The whole reason DELAY % exists: the same 30 minutes is an eighth of the
// Shatabdi's afternoon and a rounding error on a two-night run.
assert.strictEqual(b.span, 1950);                  // 22:00 -> 06:30 +2 = 32h30m
assert.strictEqual(Math.round(b.share * 1e4) / 1e4, 0.0154);
const short = build([shatabdi], [{ train_number: "12005", delay_minutes: 30 }])[0];
assert.strictEqual(short.share, 0.125);
assert.ok(short.share / b.share > 8, "the same delay must rank far worse on the shorter run");

// A train with no journeys is listed, not dropped, and claims nothing.
const [none] = build([shatabdi], []);
assert.strictEqual(none.runs, 0);
assert.strictEqual(none.avg, null);
assert.strictEqual(none.med, null);
assert.strictEqual(none.worst, null);
assert.strictEqual(none.onTime, null);
assert.strictEqual(none.share, null);

// ---- sortRows -------------------------------------------------------------
const rows = [
  { t: { train_number: "1", name: "Bravo" }, avg: 40,   share: 0.10, runs: 2, span: 100, med: 40, worst: 40, onTime: 0 },
  { t: { train_number: "2", name: "Alpha" }, avg: null, share: null, runs: 0, span: 100, med: null, worst: null, onTime: null },
  { t: { train_number: "3", name: "Charlie" }, avg: 10, share: 0.02, runs: 5, span: 500, med: 10, worst: 10, onTime: 100 },
];
const order = (key, dir) => sortRows(rows, key, dir).map(r => r.t.train_number).join("");

assert.strictEqual(order("avg", "desc"), "132");
assert.strictEqual(order("avg", "asc"), "312", "the train with no data must not lead an ascending sort");
assert.strictEqual(order("share", "desc"), "132");
assert.strictEqual(order("runs", "desc"), "312");
assert.strictEqual(order("no", "asc"), "123");
assert.strictEqual(order("svc", "asc"), "213");    // Alpha, Bravo, Charlie
assert.strictEqual(order("svc", "desc"), "312");

// Sorting must not mutate the source array -- the filter re-renders from it.
assert.strictEqual(rows.map(r => r.t.train_number).join(""), "123");

// Every sortable column must actually have a value accessor, or clicking its
// header throws instead of sorting.
for (const c of COLS) {
  if (c.sort === false) continue;
  assert.strictEqual(typeof c.val, "function", `column ${c.key} has no val()`);
  assert.doesNotThrow(() => order(c.key, "desc"), `column ${c.key} failed to sort`);
}

// ---- minimum-journeys threshold -------------------------------------------
// The threshold counts USABLE runs, not journeys seen. A train polled four
// times that never returned a delay has observed nothing, and "at least 3
// journeys" must not admit it on the strength of polls we couldn't read.
const thin = build([shatabdi], [
  { train_number: "12005", delay_minutes: 40 },
  { train_number: "12005", delay_minutes: null },
  { train_number: "12005", delay_minutes: null },
  { train_number: "12005", delay_minutes: null },
])[0];
assert.strictEqual(thin.runs, 1);
assert.strictEqual(thin.unusable, 3);
assert.ok(!(thin.runs >= 3), "3 unreadable polls must not clear a 3-journey threshold");

// The buttons offered must be ascending and start at ALL, or "hidden" counts
// against a threshold the user never picked.
const html2 = fs.readFileSync(path.join(__dirname, "stats.html"), "utf8");
const mins = [...html2.matchAll(/data-min="(\d+)"/g)].map(m => Number(m[1]));
assert.deepStrictEqual(mins, [...mins].sort((x, y) => x - y), "thresholds must ascend");
assert.strictEqual(mins[0], 0, "the first option must be ALL (no threshold)");
assert.ok(mins.length >= 3);

// ---- histogram bands ------------------------------------------------------
// Every service with a share must land in exactly one band, and the bands must
// cover the line without a gap -- a train that falls through is simply absent
// from the chart, and a chart that quietly undercounts is worse than none.
assert.strictEqual(binOf(null), -1, "no observed journeys means no band");
assert.strictEqual(binOf(undefined), -1);
assert.strictEqual(binOf(0), 0, "a perfectly punctual service belongs in the first band");

// Boundaries are half-open [lo, hi): the value at a boundary belongs upward.
assert.strictEqual(binOf(0.0099), 0);
assert.strictEqual(binOf(0.01), 1);
assert.strictEqual(binOf(0.02), 2);
assert.strictEqual(binOf(0.05), 3);
assert.strictEqual(binOf(0.10), 4);
assert.strictEqual(binOf(0.20), 5);
assert.strictEqual(binOf(12), 5, "an absurd share still lands in the top band, not off the end");

// Spread into this realm first: arrays created inside the vm carry the vm's
// Array.prototype, and deepStrictEqual compares prototypes as well as contents.
const his = [...SHARE_BINS].map(b => b.hi);
assert.deepStrictEqual(his, [...his].sort((x, y) => x - y),
  "bands must ascend, or findIndex picks the wrong one");
assert.strictEqual(SHARE_BINS[SHARE_BINS.length - 1].hi, Infinity, "the top band must be open");

// The bars have to account for every row the table shows. Anything with a
// share lands in a band; anything without one is the "no data" tally, and the
// two must add back up to the scope.
const scope = build([shatabdi, longHaul], [
  { train_number: "12005", delay_minutes: 40 },   // 40/240  = 16.7% -> band 4
  { train_number: "12621", delay_minutes: 30 },   // 30/1950 =  1.5% -> band 1
]).concat(build([{ train_number: "99999", name: "Never polled" }], [])); // no share
const binned = [...scope].filter(r => binOf(r.share) >= 0);
assert.strictEqual(binned.length, 2);
assert.strictEqual(scope.length - binned.length, 1, "the un-binned service must be reported, not dropped");
assert.deepStrictEqual(binned.map(r => binOf(r.share)).sort(), [1, 4]);

console.log(`stats: 46 build/median/sort/threshold/band assertions pass `
  + `across ${COLS.length} columns and ${SHARE_BINS.length} bands`);
