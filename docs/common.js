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

// Seven fixed slots so the shape of the week is scannable down a column.
function runDaysDots(days) {
  if (!days || !days.length) return '<span class="dim">unknown</span>';
  const set = new Set(days);
  const title = days.length === 7 ? "Daily" : days.join(", ");
  return `<span class="days" title="${title}" aria-label="Runs ${title}">` +
    WEEK.map(d => `<i class="${set.has(d) ? "on" : ""}">${DAY_INITIAL[d]}</i>`).join("") +
    `</span>`;
}

function routeText(t) {
  if (!t.source_code && !t.destination_code) return '<span class="dim">–</span>';
  return `${esc(t.source_code || "?")}<span class="arrow">→</span>${esc(t.destination_code || "?")}`;
}

function timingText(t) {
  if (!t.scheduled_departure && !t.scheduled_arrival) return '<span class="dim">–</span>';
  const plus = t.arrival_day_offset ? `<sup class="plus">+${t.arrival_day_offset}</sup>` : "";
  return `${hhmm(t.scheduled_departure)}<span class="arrow">→</span>${hhmm(t.scheduled_arrival)}${plus}`;
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
  return `
  <div class="appbar">
    <div class="appbar-inner">
      <a class="brand" href="./index.html"><span class="mark">🚆</span>HowsMyTrain</a>
      <nav class="nav">
        <a href="./index.html"${current === "fleet" ? ' aria-current="page"' : ""}>Fleet</a>
        <a href="./health.html"${current === "health" ? ' aria-current="page"' : ""}>Health</a>
        <a href="./db.html"${current === "db" ? ' aria-current="page"' : ""}>Raw data</a>
        <button id="theme-toggle">🌙 Dark</button>
      </nav>
    </div>
  </div>`;
}
