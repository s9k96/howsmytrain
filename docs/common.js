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
function band(m) {
  if (m === null || m === undefined) return { cls: "idle", text: "No data" };
  if (m <= 10) return { cls: "good", text: "On time" };
  if (m <= 30) return { cls: "warn", text: "Slight delay" };
  return { cls: "bad", text: "Late" };
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

  const at = (clock) => {
    const [h, m] = String(clock).split(":");
    const d = new Date(now);
    d.setHours(Number(h), Number(m), 0, 0);
    return d;
  };
  const arr = at(t.scheduled_arrival);
  const dep = at(t.scheduled_departure);
  dep.setDate(dep.getDate() - (t.arrival_day_offset || 0));
  // An overnight run with no recorded offset would otherwise depart after it
  // arrives; roll it back a day so the span stays positive.
  if (dep >= arr) dep.setDate(dep.getDate() - 1);

  const key = now < dep ? "before" : now < arr ? "running" : "done";
  const pct = Math.max(0, Math.min(100, (100 * (now - dep)) / (arr - dep)));
  return { key, label: JOURNEY_STATES[key], pct };
}

// Seven fixed slots so the shape of the week is scannable down a column.
function runDaysDots(days) {
  if (!days || !days.length) return '<span class="dim">unknown</span>';
  const set = new Set(days);
  const title = days.length === 7 ? "Daily" : days.join(", ");
  return `<span class="days" title="${title}" aria-label="Runs ${title}">` +
    WEEK.map(d => `<i class="${set.has(d) ? "on" : ""}">${DAY_INITIAL[d]}</i>`).join("") +
    `</span>`;
}

/**
 * Route and timing as one cell (returns the whole <td>).
 *
 * These were two columns, which was what tipped the fleet table into
 * horizontal scrolling -- and "NDLS → LKO" and "16:10 → 22:30" are one fact
 * read together anyway, so stacking them costs nothing and saves a column.
 */
function scheduleCell(t, j = journeyState(t)) {
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
  return `<td class="cell-sched" data-label="Route"><span class="stack"
    ><span class="r">${route}</span><span class="t">${timing}</span></span></td>`;
}

// ---- Chrome ---------------------------------------------------------------

/** Seeded from the OS preference, else the first click is a no-op in dark mode. */
function setupTheme(onChange) {
  const toggle = document.getElementById("theme-toggle");
  if (!toggle) return;
  let dark = matchMedia("(prefers-color-scheme: dark)").matches;
  toggle.textContent = dark ? "☀️ Light" : "🌙 Dark";
  toggle.addEventListener("click", () => {
    dark = !dark;
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    toggle.textContent = dark ? "☀️ Light" : "🌙 Dark";
    if (onChange) onChange();
  });
}

/** Whole row clickable; the inner link stays real for keyboard/middle-click. */
function wireRows(container) {
  container.querySelectorAll("tr[data-href]").forEach(tr => {
    tr.addEventListener("click", (e) => {
      if (e.target.closest("a")) return;
      location.href = tr.dataset.href;
    });
  });
}

function appbar(current) {
  // Skip link first so it's the first tab stop on the page.
  return `
  <a class="skip-link" href="#main">Skip to content</a>
  <div class="appbar">
    <div class="appbar-inner">
      <a class="brand" href="./index.html" translate="no"><span class="mark" aria-hidden="true">🚆</span>HowsMyTrain</a>
      <nav class="nav" aria-label="Main">
        <a href="./index.html"${current === "fleet" ? ' aria-current="page"' : ""}>Fleet</a>
        <a href="./health.html"${current === "health" ? ' aria-current="page"' : ""}>Health</a>
        <a href="./db.html"${current === "db" ? ' aria-current="page"' : ""}>Raw Data</a>
        <button id="theme-toggle" type="button">🌙 Dark</button>
      </nav>
    </div>
  </div>`;
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
