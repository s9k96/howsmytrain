/* HowsMyTrain -- starter dashboard logic.
   Fetches from the FastAPI backend (same origin) and renders:
   - a KPI row (one stat tile per tracked train)
   - a multi-line weekly avg-delay trend chart
   - a table-view toggle for that chart (accessibility twin)
   - a recent-polls table

   This is a starting point -- swap in more chart types, filters, etc. as
   you extend it. */

const SERIES_SLOTS = [
  "--series-1", "--series-2", "--series-3", "--series-4",
  "--series-5", "--series-6", "--series-7", "--series-8",
];

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function seriesColor(index) {
  return cssVar(SERIES_SLOTS[index % SERIES_SLOTS.length]);
}

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} -> HTTP ${res.status}`);
  return res.json();
}

function delayClass(minutes) {
  if (minutes === null || minutes === undefined) return "";
  if (minutes <= 10) return "on-time";
  if (minutes <= 30) return "late";
  return "very-late";
}

function fmtMinutes(m) {
  return m === null || m === undefined ? "–" : `${m} min`;
}

// ---- KPI row -----------------------------------------------------------

function renderKPIs(trains) {
  const row = document.getElementById("kpi-row");
  if (!trains.length) {
    row.innerHTML = `<div class="empty-state">No trains tracked yet. Configure TRAIN_NUMBERS in .env and run the poller.</div>`;
    return;
  }
  row.innerHTML = trains.map((t, i) => `
    <div class="stat-tile">
      <div class="label"><span class="swatch" style="background:${seriesColor(i)}"></span>${t.train_number}${t.name ? " · " + t.name : ""}</div>
      <div class="value">${t.avg_delay_minutes === null ? "–" : t.avg_delay_minutes + "m"}</div>
      <div class="sub">${t.journeys_observed} journey${t.journeys_observed === 1 ? "" : "s"} observed · ${t.on_time_pct === null ? "–" : t.on_time_pct + "%"} on-time</div>
    </div>
  `).join("");
}

// ---- Trend chart --------------------------------------------------------

let chartInstance = null;

function renderTrendChart(weeklyRows, trains) {
  const canvas = document.getElementById("trend-chart");
  const weeks = [...new Set(weeklyRows.map(r => r.iso_week))].sort();
  const trainNumbers = trains.map(t => t.train_number);

  const datasets = trainNumbers.map((trainNumber, i) => {
    const byWeek = Object.fromEntries(
      weeklyRows.filter(r => r.train_number === trainNumber).map(r => [r.iso_week, r.avg_delay_minutes])
    );
    return {
      label: trainNumber,
      data: weeks.map(w => byWeek[w] ?? null),
      borderColor: seriesColor(i),
      backgroundColor: seriesColor(i),
      borderWidth: 2,
      pointRadius: 4,
      pointBackgroundColor: seriesColor(i),
      pointBorderColor: cssVar("--surface-1"),
      pointBorderWidth: 2,
      spanGaps: true,
      tension: 0.15,
    };
  });

  if (!weeks.length) {
    document.getElementById("trend-empty").classList.remove("hidden");
    canvas.classList.add("hidden");
    return;
  }
  document.getElementById("trend-empty").classList.add("hidden");
  canvas.classList.remove("hidden");

  if (chartInstance) chartInstance.destroy();
  chartInstance = new Chart(canvas.getContext("2d"), {
    type: "line",
    data: { labels: weeks, datasets },
    options: {
      responsive: true,
      interaction: { mode: "nearest", intersect: false },
      plugins: {
        legend: {
          display: datasets.length > 1,
          position: "bottom",
          labels: { color: cssVar("--text-secondary"), boxWidth: 10, boxHeight: 10 },
        },
        tooltip: {
          backgroundColor: cssVar("--surface-1"),
          titleColor: cssVar("--text-primary"),
          bodyColor: cssVar("--text-secondary"),
          borderColor: cssVar("--border"),
          borderWidth: 1,
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: ${ctx.formattedValue} min avg delay`,
          },
        },
      },
      scales: {
        x: {
          grid: { color: cssVar("--gridline") },
          ticks: { color: cssVar("--text-muted") },
        },
        y: {
          beginAtZero: true,
          title: { display: true, text: "Avg delay (minutes)", color: cssVar("--text-muted") },
          grid: { color: cssVar("--gridline") },
          ticks: { color: cssVar("--text-muted") },
        },
      },
    },
  });
}

function renderTrendTable(weeklyRows) {
  const container = document.getElementById("trend-table-container");
  if (!weeklyRows.length) {
    container.innerHTML = `<div class="empty-state">No weekly data yet.</div>`;
    return;
  }
  const rows = weeklyRows.map(r => `
    <tr>
      <td>${r.iso_week}</td>
      <td>${r.train_number}</td>
      <td class="${delayClass(r.avg_delay_minutes)}">${fmtMinutes(r.avg_delay_minutes)}</td>
      <td>${fmtMinutes(r.median_delay_minutes)}</td>
      <td>${fmtMinutes(r.max_delay_minutes)}</td>
      <td>${r.on_time_pct}%</td>
      <td>${r.runs_observed}</td>
    </tr>
  `).join("");
  container.innerHTML = `
    <table class="tabular">
      <thead><tr><th>Week</th><th>Train</th><th>Avg delay</th><th>Median</th><th>Max</th><th>On-time %</th><th>Runs</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

// ---- Recent polls table --------------------------------------------------

function renderPollsTable(polls) {
  const container = document.getElementById("polls-table-container");
  if (!polls.length) {
    container.innerHTML = `<div class="empty-state">No polls logged yet. Run <code>python scripts/poll_once.py</code> or wait for the scheduled cron job.</div>`;
    return;
  }
  const rows = polls.slice(0, 50).map(p => `
    <tr>
      <td>${new Date(p.polled_at).toLocaleString()}</td>
      <td>${p.train_number}</td>
      <td>${p.status ?? "–"}</td>
      <td class="${delayClass(p.delay_minutes)}">${fmtMinutes(p.delay_minutes)}</td>
      <td>${p.current_station_code ?? "–"}${p.current_station_status ? " (" + p.current_station_status + ")" : ""}</td>
    </tr>
  `).join("");
  container.innerHTML = `
    <table class="tabular">
      <thead><tr><th>Polled at</th><th>Train</th><th>Status</th><th>Delay</th><th>Location</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

// ---- Table-view toggle for the chart -------------------------------------

function setupTrendTableToggle() {
  const btn = document.getElementById("toggle-trend-table");
  btn.addEventListener("click", () => {
    const el = document.getElementById("trend-table-container");
    const showing = !el.classList.contains("hidden");
    el.classList.toggle("hidden", showing);
    btn.textContent = showing ? "Table view" : "Chart view";
    document.getElementById("trend-chart").classList.toggle("hidden", !showing);
    document.getElementById("trend-empty").classList.add("hidden");
  });
}

// ---- Main -----------------------------------------------------------------

async function main() {
  try {
    const [trains, weekly, polls] = await Promise.all([
      fetchJSON("/api/trains"),
      fetchJSON("/api/stats/weekly?weeks=8"),
      fetchJSON("/api/polls?limit=200"),
    ]);
    renderKPIs(trains);
    renderTrendChart(weekly, trains);
    renderTrendTable(weekly);
    renderPollsTable(polls);
    document.getElementById("last-updated").textContent =
      "Loaded " + new Date().toLocaleTimeString();
  } catch (err) {
    console.error(err);
    document.getElementById("last-updated").textContent = "Failed to load data — is the server running?";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  setupTrendTableToggle();
  main();
});
