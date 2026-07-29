/* Shared across the dashboard pages: Supabase access, formatting, and the
   run-day logic that decides when a train is expected.

   Loaded as a plain script before each page's inline block, so everything
   here is a global. */

// ---------------------------------------------------------------------------
// Fill these in after creating your Supabase project (Settings -> API).
// The anon/publishable key is meant to be public: supabase/schema.sql grants
// it SELECT only, and every write uses the service key from Actions secrets.
// ---------------------------------------------------------------------------
const SUPABASE_URL = "https://frmcmbfiyvwtixvngqde.supabase.co";
const SUPABASE_ANON_KEY = "sb_publishable_EIpHf86JIDEvUgSijFv4DQ_n9Qcjik0";

function configured() {
  return !SUPABASE_URL.includes("YOUR-PROJECT") && !SUPABASE_ANON_KEY.includes("YOUR_ANON");
}

async function sb(view, query = "select=*") {
  const res = await fetch(`${SUPABASE_URL}/rest/v1/${view}?${query}`, {
    headers: { apikey: SUPABASE_ANON_KEY, Authorization: `Bearer ${SUPABASE_ANON_KEY}` },
  });
  if (!res.ok) throw new Error(`${view} -> HTTP ${res.status}: ${await res.text()}`);
  return res.json();
}

// API-supplied strings end up in innerHTML.
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

const hhmm = (t) => (t ? String(t).slice(0, 5) : "–");
const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

const SERIES_SLOTS = [
  "--series-1", "--series-2", "--series-3", "--series-4",
  "--series-5", "--series-6", "--series-7", "--series-8",
];
const seriesColor = (i) => cssVar(SERIES_SLOTS[i % SERIES_SLOTS.length]);

// Delay bands are status (good/bad), never identity -- they wear status tokens.
// Thresholds match ON_TIME_THRESHOLD_MINUTES (10) in app/aggregate.py and the
// `on_time` expression in supabase/schema.sql, so a train counted as on time
// here is on time everywhere.
function band(m) {
  if (m === null || m === undefined) return { cls: "idle", text: "No data" };
  if (m <= 10) return { cls: "good", text: "On time" };
  if (m <= 30) return { cls: "warn", text: "Slight delay" };
  return { cls: "bad", text: "Late" };
}

// ---- Delay scale ----------------------------------------------------------
// Four steps sharing lightness and chroma, differing only in hue, so no step
// shouts louder than the others. This is magnitude, not the on-time verdict:
// the on-time *count* still comes from band() above and the DB's <= 10 rule.
const DELAY_SCALE = [
  [5, "var(--ontime)"],
  [20, "var(--minor)"],
  [60, "var(--late)"],
  [Infinity, "var(--severe)"],
];

/** CSS colour for a delay, for `style="--scale: ..."` on a row. */
function scaleOf(m) {
  if (m === null || m === undefined) return "var(--text-3)";
  return DELAY_SCALE.find(([cap]) => m <= cap)[1];
}

/**
 * "+42" under an hour, "+2:18" at or above it, and the word ON TIME instead of
 * "+0" -- a zero here is a fact worth reading, not a number worth scanning.
 * Early arrivals fold into ON TIME; there is no "-3 min late".
 */
function fmtDelay(m) {
  if (m === null || m === undefined) return "—";
  if (m <= 0) return "ON TIME";
  if (m < 60) return `+${m}`;
  return `+${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}`;
}

/** Scheduled clock time shifted by a delay, as the arrival actually observed. */
function shiftTime(clock, minutes) {
  if (!clock) return "–";
  const [h, m] = String(clock).split(":").map(Number);
  const t = ((h * 60 + m + Math.round(minutes || 0)) % 1440 + 1440) % 1440;
  return `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
}

/**
 * Minutes between now and this train's scheduled arrival today: positive once
 * it is overdue, negative while it is still due to come.
 *
 * Timetable arithmetic only. It says nothing about where the train actually
 * is, which is why callers must label anything derived from it as scheduled
 * rather than observed -- an unpolled train is unknown, not punctual.
 */
function arrivalGap(t, now = new Date()) {
  if (!t.scheduled_arrival) return null;
  const [h, m] = String(t.scheduled_arrival).split(":").map(Number);
  const arr = new Date(now);
  arr.setHours(h, m, 0, 0);
  return Math.round((now - arr) / 6e4);
}

/** Minutes-of-day, for ordering a day's services by when they arrive. */
function arrivalMinutes(t) {
  if (!t.scheduled_arrival) return Infinity;   // unknown schedule sorts last
  const [h, m] = String(t.scheduled_arrival).split(":").map(Number);
  return h * 60 + m;
}

/** Compact duration: "45M", "6H10", "30H15". */
function fmtDuration(m) {
  return m < 60 ? `${m}M` : `${Math.floor(m / 60)}H${String(m % 60).padStart(2, "0")}`;
}

/** The poller and every schedule in the DB are IST; say so rather than imply it. */
function istTime(d) {
  return d.toLocaleTimeString("en-GB", {
    timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

// ---- Schedule ------------------------------------------------------------

const WEEK = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_INITIAL = { mon: "M", tue: "T", wed: "W", thu: "T", fri: "F", sat: "S", sun: "S" };

// JS getDay() is Sun=0; WEEK is Mon-first.
const weekKey = (d) => WEEK[(d.getDay() + 6) % 7];

const isoDate = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

/**
 * Does this train arrive on `date`?
 *
 * Mirrors _runs_today in scripts/poll_due.py: run_days are DEPARTURE days,
 * but polls (and therefore journey_date) land at arrival, so an arrival on
 * Friday for a +1 train belongs to a Thursday departure. Unknown run_days
 * counts as expected -- that's the poll that teaches us them.
 */
function arrivesOn(train, date) {
  if (!train.run_days || !train.run_days.length) return true;
  const dep = new Date(date);
  dep.setDate(dep.getDate() - (train.arrival_day_offset || 0));
  return train.run_days.includes(weekKey(dep));
}

/**
 * Scheduled journey length in minutes, source to destination.
 *
 * Also the denominator for delayShare(): 30 minutes on a 30-hour run and 30
 * minutes on a 2-hour Shatabdi are not the same fact, and an absolute figure
 * cannot tell them apart.
 */
function journeyMinutes(t) {
  if (!t.scheduled_departure || !t.scheduled_arrival) return null;
  const mins = (c) => {
    const [h, m] = String(c).split(":").map(Number);
    return h * 60 + m;
  };
  const span = mins(t.scheduled_arrival) - mins(t.scheduled_departure)
             + (t.arrival_day_offset || 0) * 1440;
  if (!Number.isFinite(span)) return null;
  // An overnight run with no recorded offset would otherwise appear to arrive
  // before it departs; roll it forward a day so the span stays positive.
  return span <= 0 ? span + 1440 : span;
}

/** Delay as a fraction of the scheduled run; null if either side is unknown. */
function delayShare(delayMinutes, t) {
  const span = journeyMinutes(t);
  if (delayMinutes === null || delayMinutes === undefined || !span) return null;
  return delayMinutes / span;
}

const JOURNEY_STATES = { before: "Not departed", running: "En route", done: "Completed" };

/**
 * Where a train is in today's journey, from the timetable alone.
 *
 * Returns null unless the train arrives today, which keeps it honest when
 * "Today only" is off and the table mixes in trains that aren't running.
 *
 * ponytail: schedule-based, not live -- a train 40 minutes late still reads
 * "Arrived" once its scheduled arrival passes. It answers "should this be
 * done by now?", which is what decides whether a delay figure is final. Swap
 * in the last poll's status if that stops being good enough.
 */
function journeyState(t, now = new Date()) {
  if (!t.scheduled_departure || !t.scheduled_arrival || !arrivesOn(t, now)) return null;

  const [h, m] = String(t.scheduled_arrival).split(":").map(Number);
  const arr = new Date(now);
  arr.setHours(h, m, 0, 0);
  // Departure is derived from the arrival and the scheduled span, so the
  // overnight rollback lives in exactly one place (journeyMinutes).
  const dep = new Date(arr.getTime() - journeyMinutes(t) * 60000);

  const key = now < dep ? "before" : now < arr ? "running" : "done";
  const pct = Math.max(0, Math.min(100, (100 * (now - dep)) / (arr - dep)));
  return { key, label: JOURNEY_STATES[key], pct };
}

// Seven fixed slots so the shape of the week is scannable down a column.
function runDaysDots(days) {
  if (!days || !days.length) return '<span class="dim">unknown</span>';
  const set = new Set(days);
  const title = runDaysText(days);
  return `<span class="days-dots" title="${title}" aria-label="Runs ${title}">` +
    WEEK.map(d => `<i class="${set.has(d) ? "on" : ""}">${DAY_INITIAL[d]}</i>`).join("") +
    `</span>`;
}

/** The same fact as prose, for the chips in the awaiting-data block. */
function runDaysText(days) {
  if (!days || !days.length) return "schedule unknown";
  if (days.length === 7) return "daily";
  const cap = (d) => d[0].toUpperCase() + d.slice(1);
  return "runs " + WEEK.filter(d => days.includes(d)).map(cap).join(", ");
}

/**
 * Route over departure/arrival times, as one stacked block.
 *
 * These were two columns, which was what tipped the fleet table into
 * horizontal scrolling -- and "NDLS → LKO" and "16:10 → 22:30" are one fact
 * read together anyway, so stacking them costs nothing and saves a column.
 *
 * Returns the inner markup, so the register's grid rows and the tables on the
 * other pages can share it; scheduleCell() below wraps it in a <td>.
 */
function scheduleStack(t, j = journeyState(t)) {
  const route = (t.source_code || t.destination_code)
    ? `${esc(t.source_code || "?")}<span class="arrow">→</span>${esc(t.destination_code || "?")}`
    : '<span class="dim">unknown</span>';
  // Between the two times sits either a plain arrow or, for a train running
  // today, a track filled to where the timetable says it should be by now.
  const link = j
    ? `<span class="jrny ${j.key}" role="img" aria-label="${j.label}" title="${j.label}"
        ><i style="width:${j.pct.toFixed(1)}%"></i></span>`
    : '<span class="arrow">→</span>';
  const timing = (t.scheduled_departure || t.scheduled_arrival)
    ? `${hhmm(t.scheduled_departure)}${link}${hhmm(t.scheduled_arrival)}`
      + (t.arrival_day_offset ? `<sup class="plus">+${t.arrival_day_offset}</sup>` : "")
    : "";
  // One wrapper span so the mobile card layout sees a single flex item.
  return `<span class="stack"><span class="r">${route}</span><span class="t">${timing}</span></span>`;
}

function scheduleCell(t, j = journeyState(t)) {
  return `<td class="cell-sched" data-label="Route">${scheduleStack(t, j)}</td>`;
}

// ---- Chrome ---------------------------------------------------------------

/** Whole row clickable; the inner link stays real for keyboard/middle-click. */
function wireRows(container) {
  container.querySelectorAll("tr[data-href]").forEach(tr => {
    tr.addEventListener("click", (e) => {
      if (e.target.closest("a")) return;
      location.href = tr.dataset.href;
    });
  });
}

/**
 * The register's header band: wordmark, section nav, and the poll clock.
 *
 * The clock is filled in by setPolled() rather than here, because a stale view
 * that looks live is the one failure this page cannot afford -- so the slot
 * exists on every page and stays visibly empty until real data arrives.
 */
function appbar(current) {
  // Skip link first so it's the first tab stop on the page.
  return `
  <a class="skip-link" href="#main">Skip to content</a>
  <header class="topbar">
    <a class="brand" href="./index.html" translate="no">
      <span class="wordmark">HowsMyTrain</span>
      <span class="tagline">INDIAN RAILWAYS · PUNCTUALITY</span>
    </a>
    <nav class="nav" aria-label="Main">
      <a href="./index.html"${current === "fleet" ? ' aria-current="page"' : ""}>Fleet</a>
      <a href="./health.html"${current === "health" ? ' aria-current="page"' : ""}>Health</a>
      <a href="./db.html"${current === "db" ? ' aria-current="page"' : ""}>Raw data</a>
    </nav>
    <div class="polled" id="polled" role="status" aria-live="polite">
      <i aria-hidden="true"></i><span>—</span>
    </div>
  </header>`;
}

/**
 * Show when the pipeline last ran. The dot goes amber then coral as the gap
 * widens: the cron fires every 15 minutes, so an hour of silence is a fault
 * and the page should say so rather than keep showing a confident figure.
 *
 * `neutral` turns that judgement off, for pages where the stamp is one train's
 * last sighting rather than the fleet's heartbeat -- a train polled at its
 * arrival and not since is working exactly as intended, and colouring that
 * coral would cry wolf on every train page after breakfast.
 */
function setPolled(at, label, neutral) {
  const el = document.getElementById("polled");
  if (!el) return;
  if (!at) {
    el.className = "polled dead";
    el.querySelector("span").textContent = label || "NO DATA";
    return;
  }
  const mins = (Date.now() - at.getTime()) / 6e4;
  el.className = "polled" +
    (neutral ? " muted" : mins > 120 ? " dead" : mins > 45 ? " stale" : "");
  el.querySelector("span").textContent = label || `POLLED ${istTime(at)} IST`;
}

/** Non-breaking space before the unit so "59 min" never wraps mid-value. */
const mins = (n) => `${n} min`;


/**
 * Inline SVG sparkline. No library, no canvas -- scales with the row and
 * takes its colour from CSS.
 *
 * Deliberately unlabelled and unaxed: it shows shape, not values. The exact
 * numbers live in the adjacent columns and on the train's own page.
 */
function sparkline(values, { w = 88, h = 24, pad = 3 } = {}) {
  const pts = values.filter(v => v !== null && v !== undefined);
  if (pts.length < 2) {
    return `<svg class="spark" width="${w}" height="${h}" aria-hidden="true">`
      + `<line x1="0" y1="${h / 2}" x2="${w}" y2="${h / 2}" class="spark-flat"/></svg>`;
  }
  const min = Math.min(...pts, 0);
  const max = Math.max(...pts, min + 1);
  const stepX = (w - pad * 2) / (pts.length - 1);
  const y = (v) => h - pad - ((v - min) / (max - min)) * (h - pad * 2);
  const coords = pts.map((v, i) => [pad + i * stepX, y(v)]);
  const line = coords.map(([x, yy]) => `${x.toFixed(1)},${yy.toFixed(1)}`).join(" ");
  const area = `${pad},${h} ${line} ${(pad + (pts.length - 1) * stepX).toFixed(1)},${h}`;
  const [lx, ly] = coords[coords.length - 1];

  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"
      role="img" aria-label="Trend across the last ${pts.length} journeys">
    <polygon class="spark-area" points="${area}"/>
    <polyline class="spark-line" points="${line}"/>
    <circle class="spark-dot" cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="2.5"/>
  </svg>`;
}
