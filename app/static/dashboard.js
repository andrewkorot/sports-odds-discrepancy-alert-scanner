const state = { health: null, providers: [], opportunities: [], candidates: [], markets: [], settings: {}, refreshTimer: null };

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = (value, digits = 1) => Number(value ?? 0).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
const money = (value) => `$${Number(value ?? 0).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
const pct = (value, digits = 1) => `${number(Number(value) * 100, digits)}%`;
const title = (value) => String(value ?? "").replaceAll("_", " ").replace(/\b\w/g, c => c.toUpperCase());
const age = (date) => {
  if (!date) return "Never";
  const seconds = Math.max(0, Math.round((Date.now() - new Date(date).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return new Date(date).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
};

async function get(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${path} returned ${response.status}`);
  return response.json();
}

async function refresh(showToast = false) {
  const icon = $("#refreshIcon");
  icon.classList.add("rotating");
  try {
    const [health, providers, opportunities, candidates, markets, settings] = await Promise.all([
      get("/health"), get("/health/providers"), get("/opportunities"),
      get("/market-candidates"), get("/markets"), get("/settings")
    ]);
    Object.assign(state, { health, providers, opportunities, candidates, markets, settings });
    render();
    if (showToast) toast("Dashboard refreshed");
  } catch (error) {
    console.error(error);
    toast("Could not refresh scanner data", true);
  } finally {
    icon.classList.remove("rotating");
  }
}

function render() {
  const accepted = state.candidates.filter(c => c.accepted);
  const online = state.providers.filter(p => p.connected);
  $("#modePill").textContent = `${state.settings.app_mode || "mock"} · ${state.settings.live_dry_run ? "dry run" : "alerts"}`;
  $("#lastScan").textContent = age(state.health.last_successful_update);
  $("#opportunityCount").textContent = state.opportunities.length.toLocaleString();
  $("#marketCount").textContent = state.markets.length.toLocaleString();
  $("#acceptedCount").textContent = accepted.length.toLocaleString();
  $("#providerCount").textContent = `${online.length}/${state.providers.length}`;
  const best = Math.max(0, ...state.opportunities.map(o => Number(o.edge_percentage_points)));
  $("#bestEdge").textContent = `Best edge +${number(best, 2)} pp`;
  $("#acceptanceRate").textContent = `${number(accepted.length / Math.max(1, state.candidates.length) * 100, 0)}% qualification rate`;
  $("#providerSummary").textContent = online.length === state.providers.length ? "All systems operational" : "Provider attention required";
  renderTopOpportunities();
  renderProviders();
  renderRejections();
  populateFilters();
  renderOpportunityTable();
  renderCandidates();
  renderHealth();
  renderSettings();
}

function selectionLabel(o) {
  const base = o.participant || title(o.selection);
  if (o.line == null) return base;
  const line = Number(o.line);
  return `${base} ${o.market_type === "spread" && line > 0 ? "+" : ""}${line}`;
}

function opportunityCard(o) {
  return `<article class="opportunity-card">
    <div class="card-top"><div><div class="match-name">${esc(o.home_team)} <span class="muted">vs</span> ${esc(o.away_team)}</div><div class="competition">${esc(o.competition)} · ${new Date(o.kickoff_time_utc).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"})}</div></div>
    <div class="edge-value">+${number(o.edge_percentage_points, 2)}<small>percentage points</small></div></div>
    <div class="market-line"><span class="badge">${title(o.sport)}</span><span class="badge">${title(o.market_type)}</span><span class="badge">${esc(selectionLabel(o))}</span><span class="badge provider">${title(o.prediction_market_provider)}</span></div>
    <div class="price-route"><div class="venue"><small>Executable ask</small><strong>${pct(o.prediction_market_best_ask)}</strong></div><span class="arrow">→</span><div class="venue right"><small>${esc(o.bookmaker_display_name)}</small><strong>${number(o.sportsbook_decimal_odds, 2)}</strong></div></div>
    <div class="quality-row"><div><small>Spread</small><strong>${number(o.spread_cents, 1)}¢</strong></div><div><small>Depth</small><strong>${money(o.total_depth_within_window_usd)}</strong></div><div><small>24h volume</small><strong>${money(o.trailing_24h_volume_usd)}</strong></div></div>
  </article>`;
}

function renderTopOpportunities() {
  $("#topOpportunities").innerHTML = state.opportunities.slice(0, 4).map(opportunityCard).join("") ||
    `<div class="empty-state"><span>◇</span><h3>No qualified opportunities</h3><p>The scanner is watching for the next edge.</p></div>`;
}

function renderProviders() {
  $("#providerHealth").innerHTML = state.providers.map(p => `<div class="provider-item">
    <div class="provider-logo">${esc(p.provider.slice(0,2).toUpperCase())}</div>
    <div><strong>${title(p.provider)}</strong><small>${title(p.mode)} · ${p.events_discovered || 0} events · ${p.markets_discovered || 0} markets</small></div>
    <span class="connection ${p.connected ? "online" : ""}" title="${p.connected ? "Online" : "Offline"}"></span>
  </div>`).join("");
  const okay = state.providers.every(p => p.connected || !p.enabled);
  $("#systemStatus").textContent = okay ? "Operational" : "Degraded";
  $("#systemStatus").style.color = okay ? "var(--lime)" : "var(--warning)";
}

function rejectionCounts() {
  const counts = {};
  state.candidates.filter(c => !c.accepted).forEach(c => c.rejection_reasons.forEach(r => counts[r] = (counts[r] || 0) + 1));
  return Object.entries(counts).sort((a,b) => b[1] - a[1]);
}

function renderRejections() {
  const items = rejectionCounts().slice(0, 6);
  const max = Math.max(1, ...items.map(x => x[1]));
  $("#rejectionChart").innerHTML = items.map(([reason, count]) => `<div class="rejection-item">
    <div class="rejection-meta"><span>${title(reason)}</span><strong>${count}</strong></div>
    <div class="bar"><i style="width:${count/max*100}%"></i></div></div>`).join("") || "<p class='competition'>No rejected candidates.</p>";
}

function populateFilters() {
  const sports = [...new Set([
    ...state.opportunities.map(o => o.sport),
    ...state.candidates.map(c => c.prediction_quote.sport),
    ...(state.settings.enabled_sports || [])
  ])].filter(Boolean).sort();
  ["opSportFilter", "candidateSportFilter"].forEach(id => {
    const select = $(`#${id}`);
    const currentSport = select.value;
    select.innerHTML = `<option value="">All sports</option>${sports.map(s => `<option value="${esc(s)}">${title(s)}</option>`).join("")}`;
    select.value = currentSport;
  });
  const book = $("#opBookFilter");
  const current = book.value;
  const books = [...new Set(state.opportunities.map(o => o.bookmaker_id))].sort();
  book.innerHTML = `<option value="">All sportsbooks</option>${books.map(b => `<option value="${esc(b)}">${title(b)}</option>`).join("")}`;
  book.value = current;
  const reason = $("#candidateReasonFilter");
  const reasonCurrent = reason.value;
  reason.innerHTML = `<option value="">All reasons</option>${rejectionCounts().map(([r]) => `<option value="${esc(r)}">${title(r)}</option>`).join("")}`;
  reason.value = reasonCurrent;
}

function filteredOpportunities() {
  const sport = $("#opSportFilter").value, market = $("#opMarketFilter").value, provider = $("#opProviderFilter").value;
  const book = $("#opBookFilter").value, search = $("#opSearch").value.toLowerCase().trim();
  return state.opportunities.filter(o =>
    (!sport || o.sport === sport) && (!market || o.market_type === market) && (!provider || o.prediction_market_provider === provider) &&
    (!book || o.bookmaker_id === book) &&
    (!search || `${o.home_team} ${o.away_team} ${o.competition}`.toLowerCase().includes(search)));
}

function renderOpportunityTable() {
  const rows = filteredOpportunities();
  $("#opportunitiesEmpty").classList.toggle("hidden", rows.length > 0);
  $("#opportunitiesTable").innerHTML = rows.length ? `<table><thead><tr><th>Event</th><th>Market</th><th>Prediction</th><th>Sportsbook</th><th>Ask</th><th>Implied</th><th>Edge</th><th>Quality</th></tr></thead><tbody>
  ${rows.map(o => `<tr><td data-label="Match"><span class="cell-main">${esc(o.home_team)} vs ${esc(o.away_team)}</span><span class="cell-sub">${esc(o.competition)}</span></td>
  <td data-label="Market"><span class="cell-main">${title(o.market_type)}</span><span class="cell-sub">${title(o.sport)} · ${esc(selectionLabel(o))}</span></td>
  <td data-label="Prediction"><span class="cell-main">${title(o.prediction_market_provider)}</span><span class="cell-sub">${number(o.prediction_quote_age_seconds,0)}s old</span></td>
  <td data-label="Sportsbook"><span class="cell-main">${esc(o.bookmaker_display_name)}</span><span class="cell-sub">${number(o.sportsbook_decimal_odds,2)} decimal</span></td>
  <td data-label="Ask">${pct(o.prediction_market_best_ask)}</td><td data-label="Implied">${pct(o.sportsbook_implied_probability)}</td>
  <td data-label="Edge" class="positive">+${number(o.edge_percentage_points,2)} pp</td><td data-label="Quality"><span class="cell-main">${number(o.spread_cents,1)}¢ spread</span><span class="cell-sub">${money(o.total_depth_within_window_usd)} depth</span></td></tr>`).join("")}</tbody></table>` : "";
}

function renderCandidates() {
  const sport = $("#candidateSportFilter").value, status = $("#candidateStatusFilter").value, reason = $("#candidateReasonFilter").value, market = $("#candidateMarketFilter").value;
  const rows = state.candidates.filter(c => (!sport || c.prediction_quote.sport === sport) && (!status || (status === "accepted") === c.accepted) && (!reason || c.rejection_reasons.includes(reason)) && (!market || c.prediction_quote.market_type === market)).slice(0, 120);
  $("#candidateList").innerHTML = rows.map(c => `<article class="candidate-card">
    <div><span class="cell-main">${esc(c.prediction_quote.home_team)} vs ${esc(c.prediction_quote.away_team)}</span><span class="cell-sub">${title(c.prediction_quote.sport)} · ${title(c.prediction_quote.provider)} · ${title(c.prediction_quote.market_type)} · ${esc(selectionLabel(c.prediction_quote))}</span></div>
    <div><span class="cell-sub">Spread</span><span class="cell-main">${c.liquidity.spread_cents == null ? "—" : `${number(c.liquidity.spread_cents,1)}¢`}</span></div>
    <div><span class="cell-sub">Depth</span><span class="cell-main">${money(c.liquidity.total_depth_within_window_usd)}</span></div>
    <div class="reasons">${c.rejection_reasons.length ? c.rejection_reasons.map(r => `<span class="reason">${title(r)}</span>`).join("") : `<span class="cell-sub">All checks passed</span>`}</div>
    <span class="decision ${c.accepted ? "accepted" : ""}">${c.accepted ? "Accepted" : "Rejected"}</span></article>`).join("") ||
    `<div class="empty-state"><span>◇</span><h3>No candidates match</h3><p>Adjust the decision filters.</p></div>`;
}

function renderHealth() {
  $("#healthGrid").innerHTML = state.providers.map(p => `<article class="health-card">
    <div class="health-card-top"><div><p class="eyebrow">${title(p.mode)} mode</p><h3>${title(p.provider)}</h3></div><span class="connection ${p.connected ? "online" : ""}"></span></div>
    <div class="health-stats"><div class="health-stat"><small>Last success</small><strong>${age(p.last_success_at)}</strong></div><div class="health-stat"><small>Latency</small><strong>${p.latency_ms == null ? "—" : `${number(p.latency_ms,0)} ms`}</strong></div><div class="health-stat"><small>Books updated</small><strong>${p.books_updated || 0}</strong></div><div class="health-stat"><small>Failures</small><strong>${p.consecutive_failures || 0}</strong></div></div>
    ${p.sanitized_latest_error ? `<p class="competition">${esc(p.sanitized_latest_error)}</p>` : ""}
  </article>`).join("");
}

function renderSettings() {
  const keys = ["client_timezone","enabled_sports","edge_threshold_pp","max_bid_ask_spread_cents","depth_window_from_midpoint_cents","min_depth_within_window_usd","min_trailing_24h_volume_usd","price_poll_interval_seconds","enabled_market_types","live_dry_run","alerts_enabled","telegram_enabled"];
  $("#settingsGrid").innerHTML = keys.map(k => `<div class="setting"><small>${title(k)}</small><strong>${esc(Array.isArray(state.settings[k]) ? state.settings[k].map(title).join(", ") : state.settings[k])}</strong></div>`).join("");
}

function switchView(view) {
  $$(".view").forEach(el => el.classList.toggle("active", el.id === `view-${view}`));
  $$("[data-view]").forEach(el => el.classList.toggle("active", el.dataset.view === view));
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function toast(message, error = false) {
  const el = $("#toast"); el.textContent = message; el.style.color = error ? "var(--danger)" : "var(--text)";
  el.classList.remove("hidden"); setTimeout(() => el.classList.add("hidden"), 2600);
}

$$("[data-view]").forEach(el => el.addEventListener("click", () => switchView(el.dataset.view)));
$$("[data-jump]").forEach(el => el.addEventListener("click", () => switchView(el.dataset.jump)));
$("#refreshButton").addEventListener("click", () => refresh(true));
["opSportFilter","opMarketFilter","opProviderFilter","opBookFilter"].forEach(id => $(`#${id}`).addEventListener("change", renderOpportunityTable));
$("#opSearch").addEventListener("input", renderOpportunityTable);
["candidateSportFilter","candidateStatusFilter","candidateReasonFilter","candidateMarketFilter"].forEach(id => $(`#${id}`).addEventListener("change", renderCandidates));
refresh();
state.refreshTimer = setInterval(refresh, 30000);
