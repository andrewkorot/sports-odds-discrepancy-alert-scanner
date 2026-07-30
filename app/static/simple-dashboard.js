const PAGE_SIZE = 20;
const state = {
  health: null,
  opportunities: [],
  audits: [],
  pages: { opportunities: 1, matched: 1, unmatched: 1 },
  refreshTimer: null
};

const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? "—").replace(/[&<>"']/g, character => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
})[character]);
const label = value => String(value ?? "—").replaceAll("_", " ");
const kickoff = value => value ? new Date(value).toLocaleString() : "—";
const percent = value => `${(Number(value) * 100).toFixed(1)}%`;
const normalized = value => String(value ?? "").toLowerCase();

async function get(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
}

function setRows(id, emptyId, rows) {
  $(id).innerHTML = rows.join("");
  $(emptyId).classList.toggle("hidden", rows.length > 0);
}

function populateSelect(selector, values, defaultLabel) {
  const element = $(selector);
  const selected = element.value;
  const unique = [...new Set(values.filter(Boolean))].sort();
  element.innerHTML = `<option value="">${esc(defaultLabel)}</option>${unique.map(value =>
    `<option value="${esc(value)}">${esc(label(value))}</option>`
  ).join("")}`;
  element.value = unique.includes(selected) ? selected : "";
}

function pagination(selector, collection, pageKey, render) {
  const pages = Math.max(1, Math.ceil(collection.length / PAGE_SIZE));
  state.pages[pageKey] = Math.min(state.pages[pageKey], pages);
  const page = state.pages[pageKey];
  const start = (page - 1) * PAGE_SIZE;
  const visible = collection.slice(start, start + PAGE_SIZE);
  $(selector).innerHTML = collection.length ? `
    <span>${start + 1}–${Math.min(start + PAGE_SIZE, collection.length)} of ${collection.length}</span>
    <button type="button" data-direction="previous" ${page === 1 ? "disabled" : ""}>Previous</button>
    <button type="button" data-direction="next" ${page === pages ? "disabled" : ""}>Next</button>` : "";
  $(selector).querySelectorAll("button").forEach(button => button.addEventListener("click", () => {
    state.pages[pageKey] += button.dataset.direction === "next" ? 1 : -1;
    render();
  }));
  return visible;
}

function renderOpportunities() {
  const search = normalized($("#opportunitySearch").value);
  const provider = $("#opportunityProvider").value;
  const bookmaker = $("#opportunityBookmaker").value;
  const filtered = state.opportunities.filter(item => {
    const text = normalized(`${item.home_team} ${item.away_team} ${item.competition}`);
    return (!search || text.includes(search))
      && (!provider || item.prediction_market_provider === provider)
      && (!bookmaker || item.bookmaker_id === bookmaker);
  });
  $("#opportunityCount").textContent = filtered.length;
  const visible = pagination("#opportunityPagination", filtered, "opportunities", renderOpportunities);
  setRows("#opportunities", "#opportunitiesEmpty", visible.map(item => `<tr>
    <td>${esc(item.home_team)} vs ${esc(item.away_team)}<br>${esc(item.competition)}</td>
    <td>${esc(label(item.market_type))} · ${esc(label(item.selection))}</td>
    <td>${esc(label(item.prediction_market_provider))}</td>
    <td>${esc(item.bookmaker_display_name)}</td>
    <td>${esc(percent(item.prediction_market_best_ask))}</td>
    <td>${esc(Number(item.sportsbook_decimal_odds).toFixed(2))}</td>
    <td class="edge">+${esc(Number(item.edge_percentage_points).toFixed(2))} pp</td>
  </tr>`));
}

function renderMatched() {
  const search = normalized($("#matchedSearch").value);
  const provider = $("#matchedProvider").value;
  const filtered = state.audits.filter(item => item.matched && item.provider !== "oddspapi")
    .filter(item => {
      const text = normalized(`${item.title} ${item.sportsbook_title} ${item.competition}`);
      return (!search || text.includes(search)) && (!provider || item.provider === provider);
    });
  $("#matchedCount").textContent = filtered.length;
  const visible = pagination("#matchedPagination", filtered, "matched", renderMatched);
  setRows("#matched", "#matchedEmpty", visible.map(item => `<tr>
    <td>${esc(label(item.provider))}</td>
    <td>${esc(item.title)}</td>
    <td>${esc(item.sportsbook_title)}</td>
    <td>${esc(item.competition || item.sportsbook_competition)}</td>
    <td>${esc(kickoff(item.kickoff_time_utc))}</td>
    <td>${esc(label(item.match_confidence))}</td>
  </tr>`));
}

function renderUnmatched() {
  const search = normalized($("#unmatchedSearch").value);
  const provider = $("#unmatchedProvider").value;
  const filtered = state.audits.filter(item => !item.matched).filter(item => {
    const text = normalized(`${item.title} ${item.competition} ${item.rejection_reasons.join(" ")}`);
    return (!search || text.includes(search)) && (!provider || item.provider === provider);
  });
  $("#unmatchedCount").textContent = filtered.length;
  const visible = pagination("#unmatchedPagination", filtered, "unmatched", renderUnmatched);
  setRows("#unmatched", "#unmatchedEmpty", visible.map(item => `<tr>
    <td>${esc(label(item.provider))}</td>
    <td>${esc(item.title)}</td>
    <td>${esc(item.competition)}</td>
    <td>${esc(kickoff(item.kickoff_time_utc))}</td>
    <td>${esc(item.sportsbook_title)}</td>
    <td class="reason">${esc(item.rejection_reasons.map(label).join(", "))}</td>
  </tr>`));
}

function renderAll() {
  populateSelect("#opportunityProvider", state.opportunities.map(item => item.prediction_market_provider), "All prediction markets");
  populateSelect("#opportunityBookmaker", state.opportunities.map(item => item.bookmaker_id), "All sportsbooks");
  populateSelect("#matchedProvider", state.audits.filter(item => item.matched && item.provider !== "oddspapi").map(item => item.provider), "All providers");
  populateSelect("#unmatchedProvider", state.audits.filter(item => !item.matched).map(item => item.provider), "All providers");
  renderOpportunities();
  renderMatched();
  renderUnmatched();
}

async function refresh(forceSnapshot = false) {
  const button = $("#refresh");
  button.disabled = true;
  try {
    const health = await get("/health");
    const completedScanChanged = health.last_successful_update !== state.health?.last_successful_update;
    const needSnapshot = forceSnapshot || !state.health || completedScanChanged;
    state.health = health;
    if (needSnapshot) {
      [state.opportunities, state.audits] = await Promise.all([
        get("/opportunities"), get("/event-matches")
      ]);
      state.pages = { opportunities: 1, matched: 1, unmatched: 1 };
      renderAll();
    }
    $("#status").textContent = health.scan_in_progress
      ? "Scanning now — showing the previous completed scan."
      : health.last_scan_error || `Last completed update: ${health.last_successful_update ? new Date(health.last_successful_update).toLocaleString() : "none"}`;
  } catch (error) {
    $("#status").textContent = `Dashboard refresh failed: ${error.message}`;
  } finally {
    button.disabled = false;
    scheduleRefresh();
  }
}

function scheduleRefresh() {
  clearTimeout(state.refreshTimer);
  const delay = state.health?.scan_in_progress || !state.health?.last_successful_update ? 3000 : 15000;
  state.refreshTimer = setTimeout(refresh, delay);
}

["#opportunitySearch", "#matchedSearch", "#unmatchedSearch"].forEach(selector =>
  $(selector).addEventListener("input", () => {
    state.pages = { opportunities: 1, matched: 1, unmatched: 1 };
    renderAll();
  })
);
["#opportunityProvider", "#opportunityBookmaker", "#matchedProvider", "#unmatchedProvider"].forEach(selector =>
  $(selector).addEventListener("change", () => {
    state.pages = { opportunities: 1, matched: 1, unmatched: 1 };
    renderAll();
  })
);
$("#refresh").addEventListener("click", () => refresh(true));
refresh();
