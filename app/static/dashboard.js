const DISPLAY_TIME_ZONE = "America/Los_Angeles";
const state = { health: null, providers: [], events: [], eventMatches: [], bookmakers: [], opportunities: [], candidates: [], markets: [], settings: {}, matchedPage: 1, matchedPageSize: 8, unmatchedPage: 1, unmatchedPageSize: 8, refreshTimer: null };

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = (value, digits = 1) => Number(value ?? 0).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
const money = (value) => `$${Number(value ?? 0).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
const pct = (value, digits = 1) => `${number(Number(value) * 100, digits)}%`;
const title = (value) => String(value ?? "").replaceAll("_", " ").replace(/\b\w/g, c => c.toUpperCase());
const searchable = (...values) => values.flat(Infinity).filter(value => value != null).join(" ").toLocaleLowerCase();
const dateTime = (value) => value ? new Date(value).toLocaleString([], {
  timeZone: DISPLAY_TIME_ZONE,
  year: "numeric",
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  timeZoneName: "short"
}) : "—";
const timeOnly = (value) => value ? new Date(value).toLocaleTimeString([], {
  timeZone: DISPLAY_TIME_ZONE,
  hour: "2-digit",
  minute: "2-digit",
  timeZoneName: "short"
}) : "—";
const age = (date) => {
  if (!date) return "Never";
  const seconds = Math.max(0, Math.round((Date.now() - new Date(date).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return timeOnly(date);
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
    const [health, providers, events, eventMatches, bookmakers, opportunities, candidates, markets, settings] = await Promise.all([
      get("/health"), get("/health/providers"), get("/events"),
      get("/event-matches"), get("/bookmakers"), get("/opportunities"), get("/market-candidates"), get("/markets"), get("/settings")
    ]);
    Object.assign(state, { health, providers, events, eventMatches, bookmakers, opportunities, candidates, markets, settings });
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
  const discovered = state.providers.reduce((total, provider) => total + Number(provider.events_discovered || 0), 0);
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
  $("#discoveredCount").textContent = discovered.toLocaleString();
  $("#matchedEventCount").textContent = state.events.length.toLocaleString();
  $("#pricedMarketCount").textContent = state.markets.length.toLocaleString();
  $("#qualifiedCount").textContent = state.opportunities.length.toLocaleString();
  renderPipeline(discovered);
  renderTopOpportunities();
  renderProviders();
  renderRejections();
  renderMatchedEvents();
  renderUnmatchedEvents();
  populateFilters();
  renderOpportunityTable();
  renderCandidates();
  renderHealth();
  renderSettings();
}

function renderMatchedEvents() {
  const allMatched = state.eventMatches.filter(item => item.matched && item.provider !== "oddspapi");
  const providerFilter = $("#matchedProviderFilter");
  const selectedProvider = providerFilter.value;
  const providers = [...new Set(allMatched.map(item => item.provider))].sort();
  providerFilter.innerHTML = `<option value="">All prediction providers</option>${providers.map(provider => `<option value="${esc(provider)}">${title(provider)}</option>`).join("")}`;
  providerFilter.value = providers.includes(selectedProvider) ? selectedProvider : "";

  const search = $("#matchedSearch").value.trim().toLocaleLowerCase();
  const matched = allMatched.filter(item =>
    (!providerFilter.value || item.provider === providerFilter.value) &&
    (!search || searchable(
      item.provider, item.provider_event_id, item.title, item.competition,
      item.home_team, item.away_team, item.participant_one, item.participant_two,
      item.sportsbook_event_id, item.sportsbook_title, item.sportsbook_competition,
      item.sportsbook_home_team, item.sportsbook_away_team, item.match_confidence
    ).includes(search))
  );
  const pageCount = Math.max(1, Math.ceil(matched.length / state.matchedPageSize));
  state.matchedPage = Math.min(state.matchedPage, pageCount);
  const pageStart = (state.matchedPage - 1) * state.matchedPageSize;
  const pageRows = matched.slice(pageStart, pageStart + state.matchedPageSize);

  $("#matchedCount").textContent = matched.length.toLocaleString();
  $("#matchedList").innerHTML = pageRows.map(item => `
    <article class="unmatched-row">
      <div class="unmatched-summary">
        <div class="provider-logo">${esc(item.provider.slice(0, 2).toUpperCase())}</div>
        <div class="unmatched-event">
          <strong>${esc(item.title)}</strong>
          <small>${title(item.provider)} · ${esc(item.competition || "Competition unknown")} · ${item.kickoff_time_utc ? dateTime(item.kickoff_time_utc) : "Kickoff unknown"}</small>
        </div>
        <div class="unmatched-event">
          <strong>${esc(item.sportsbook_title || `${item.sportsbook_home_team || "Unknown"} vs ${item.sportsbook_away_team || "Unknown"}`)}</strong>
          <small>OddsPapi · ${esc(item.sportsbook_competition || "Competition unknown")} · score ${esc(item.weighted_score ?? "—")}</small>
        </div>
        <span class="decision accepted">${title(item.match_confidence)}</span>
      </div>
      <details class="audit-details">
        <summary>Inspect mapping</summary>
        <div class="audit-comparison">
          <section>
            <p class="eyebrow">${title(item.provider)} event</p>
            ${auditField("Provider event ID", item.provider_event_id)}
            ${auditField("Raw title", item.title)}
            ${auditField("Home team", item.home_team)}
            ${auditField("Away team", item.away_team)}
            ${auditField("Participant one", item.participant_one)}
            ${auditField("Participant two", item.participant_two)}
            ${auditField("Orientation known", item.orientation_known)}
            ${auditField("Kickoff Pacific", item.kickoff_time_utc ? dateTime(item.kickoff_time_utc) : null)}
          </section>
          <section>
            <p class="eyebrow">Matched OddsPapi fixture</p>
            ${auditField("Sportsbook event ID", item.sportsbook_event_id)}
            ${auditField("Raw title", item.sportsbook_title)}
            ${auditField("Home team", item.sportsbook_home_team)}
            ${auditField("Away team", item.sportsbook_away_team)}
            ${auditField("Competition", item.sportsbook_competition)}
            ${auditField("Kickoff Pacific", item.sportsbook_kickoff_time_utc ? dateTime(item.sportsbook_kickoff_time_utc) : null)}
            ${auditField("Confidence", item.match_confidence)}
            ${auditField("Weighted score", item.weighted_score)}
          </section>
        </div>
      </details>
    </article>`).join("") || `
    <div class="empty-state compact"><span>◇</span><h3>No matched events yet</h3><p>Approved Kalshi or Polymarket mappings will appear here during discovery.</p></div>`;

  const pagination = $("#matchedPagination");
  pagination.classList.toggle("hidden", matched.length === 0);
  $("#matchedPageSummary").textContent = matched.length
    ? `Showing ${pageStart + 1}–${Math.min(pageStart + state.matchedPageSize, matched.length)} of ${matched.length}`
    : "No events";
  $("#matchedPrevious").disabled = state.matchedPage <= 1;
  $("#matchedNext").disabled = state.matchedPage >= pageCount;
}

function renderUnmatchedEvents() {
  const allUnmatched = state.eventMatches.filter(item => !item.matched);
  const providerFilter = $("#unmatchedProviderFilter");
  const selectedProvider = providerFilter.value;
  const providers = [...new Set(allUnmatched.map(item => item.provider))].sort();
  providerFilter.innerHTML = `<option value="">All providers</option>${providers.map(provider => `<option value="${esc(provider)}">${title(provider)}</option>`).join("")}`;
  providerFilter.value = providers.includes(selectedProvider) ? selectedProvider : "";

  const search = $("#unmatchedSearch").value.trim().toLocaleLowerCase();
  const unmatched = allUnmatched.filter(item =>
    (!providerFilter.value || item.provider === providerFilter.value) &&
    (!search || searchable(
      item.provider, item.provider_event_id, item.title, item.competition,
      item.normalized_competition, item.home_team, item.normalized_home_team,
      item.away_team, item.normalized_away_team, item.participant_one,
      item.participant_two, item.sportsbook_event_id, item.sportsbook_title,
      item.sportsbook_competition, item.sportsbook_home_team,
      item.sportsbook_away_team, item.match_confidence, item.rejection_reasons
    ).includes(search))
  );
  const pageCount = Math.max(1, Math.ceil(unmatched.length / state.unmatchedPageSize));
  state.unmatchedPage = Math.min(state.unmatchedPage, pageCount);
  const pageStart = (state.unmatchedPage - 1) * state.unmatchedPageSize;
  const pageRows = unmatched.slice(pageStart, pageStart + state.unmatchedPageSize);

  $("#unmatchedCount").textContent = unmatched.length.toLocaleString();
  $("#unmatchedList").innerHTML = pageRows.map(item => `
    <article class="unmatched-row">
      <div class="unmatched-summary">
        <div class="provider-logo">${esc(item.provider.slice(0, 2).toUpperCase())}</div>
        <div class="unmatched-event">
          <strong>${esc(item.home_team && item.away_team ? `${item.home_team} vs ${item.away_team}` : item.title)}</strong>
          <small>${title(item.provider)} · ${esc(item.competition || "Competition unknown")} · ${item.kickoff_time_utc ? dateTime(item.kickoff_time_utc) : "Kickoff unknown"}</small>
        </div>
        <div class="audit-reasons">${item.rejection_reasons.map(reason => `<span class="reason">${title(reason)}</span>`).join("")}</div>
        <span class="decision ${item.match_confidence === "manual_review" ? "review" : ""}">${title(item.match_confidence)}</span>
      </div>
      <details class="audit-details">
        <summary>Inspect extracted data</summary>
        <div class="audit-comparison">
          <section>
            <p class="eyebrow">${title(item.provider)} event</p>
            ${auditField("Provider event ID", item.provider_event_id)}
            ${auditField("Raw title", item.title)}
            ${auditField("Extracted competition", item.competition)}
            ${auditField("Normalized competition", item.normalized_competition)}
            ${auditField("Extracted home", item.home_team)}
            ${auditField("Normalized home", item.normalized_home_team)}
            ${auditField("Extracted away", item.away_team)}
            ${auditField("Normalized away", item.normalized_away_team)}
            ${auditField("Participant one", item.participant_one)}
            ${auditField("Normalized participant one", item.normalized_participant_one)}
            ${auditField("Participant two", item.participant_two)}
            ${auditField("Normalized participant two", item.normalized_participant_two)}
            ${auditField("Orientation known", item.orientation_known)}
            ${auditField("Extraction source", item.extraction_source)}
            ${auditField("Kickoff Pacific", item.kickoff_time_utc ? dateTime(item.kickoff_time_utc) : null)}
          </section>
          <section>
            <p class="eyebrow">Closest OddsPapi candidate</p>
            ${auditField("Sportsbook event ID", item.sportsbook_event_id)}
            ${auditField("Raw title", item.sportsbook_title)}
            ${auditField("Competition", item.sportsbook_competition)}
            ${auditField("Home team", item.sportsbook_home_team)}
            ${auditField("Away team", item.sportsbook_away_team)}
            ${auditField("Kickoff Pacific", item.sportsbook_kickoff_time_utc ? dateTime(item.sportsbook_kickoff_time_utc) : null)}
            ${auditField("Match confidence", item.match_confidence)}
            ${auditField("Weighted score", item.weighted_score)}
            ${auditField("Runner-up score", item.runner_up_score)}
            ${auditField("Score breakdown", item.score_breakdown ? Object.entries(item.score_breakdown).map(([key, value]) => `${title(key)}: ${value}`).join(", ") : null)}
            ${auditField("Rejection reasons", item.rejection_reasons.join(", "))}
          </section>
        </div>
      </details>
    </article>`).join("") || `
    <div class="empty-state compact"><span>✓</span><h3>No unmatched events</h3><p>Every discovered provider event currently has an approved counterpart.</p></div>`;

  const pagination = $("#unmatchedPagination");
  pagination.classList.toggle("hidden", unmatched.length === 0);
  $("#unmatchedPageSummary").textContent = unmatched.length
    ? `Showing ${pageStart + 1}–${Math.min(pageStart + state.unmatchedPageSize, unmatched.length)} of ${unmatched.length}`
    : "No events";
  $("#unmatchedPrevious").disabled = state.unmatchedPage <= 1;
  $("#unmatchedNext").disabled = state.unmatchedPage >= pageCount;
}

function auditField(label, value) {
  const rendered = value == null || value === "" ? "Not extracted" : String(value);
  return `<div class="audit-field"><small>${esc(label)}</small><strong class="${rendered === "Not extracted" ? "missing-value" : ""}">${esc(rendered)}</strong></div>`;
}

function renderPipeline(discovered) {
  const titleEl = $("#pipelineTitle");
  const descriptionEl = $("#pipelineDescription");
  if (state.health.scan_in_progress) {
    titleEl.textContent = "Scanning for updated prices";
    descriptionEl.textContent = "The previous completed scan remains visible until discovery, matching, qualification, and persistence finish.";
    return;
  }
  if (state.health.last_scan_error) {
    titleEl.textContent = "The latest scan did not complete";
    descriptionEl.textContent = state.health.last_scan_error;
    return;
  }
  if (discovered > 0 && state.events.length === 0) {
    titleEl.textContent = "Providers are connected; no fixtures matched";
    descriptionEl.textContent = `${discovered} provider events were found, but none currently agree on teams, competition, kickoff time, and settlement rules.`;
    return;
  }
  if (state.events.length > 0 && state.markets.length === 0) {
    titleEl.textContent = "Fixtures matched; executable markets unavailable";
    descriptionEl.textContent = "Matched fixtures were found, but no eligible open order book was normalized during this scan.";
    return;
  }
  if (state.markets.length > 0 && state.opportunities.length === 0) {
    titleEl.textContent = "Markets are priced; no edge qualifies";
    descriptionEl.textContent = "Live comparisons are available, but none pass the configured edge, freshness, and liquidity requirements.";
    return;
  }
  if (state.opportunities.length > 0) {
    titleEl.textContent = `${state.opportunities.length} qualified ${state.opportunities.length === 1 ? "opportunity" : "opportunities"} detected`;
    descriptionEl.textContent = "Every displayed edge passed executable-price, freshness, liquidity, matching, and settlement checks.";
    return;
  }
  titleEl.textContent = "Waiting for provider events";
  descriptionEl.textContent = "The scanner is online and will update this workspace after the next polling cycle.";
}

function selectionLabel(o) {
  const base = o.participant || title(o.selection);
  if (o.line == null) return base;
  const line = Number(o.line);
  return `${base} ${o.market_type === "spread" && line > 0 ? "+" : ""}${line}`;
}

function opportunityCard(o) {
  return `<article class="opportunity-card">
    <div class="card-top"><div><div class="match-name">${esc(o.home_team)} <span class="muted">vs</span> ${esc(o.away_team)}</div><div class="competition">${esc(o.competition)} · ${timeOnly(o.kickoff_time_utc)}</div></div>
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
  const filtered = state.opportunities.length > 0 && rows.length === 0;
  $("#opportunitiesEmptyCopy").textContent = filtered
    ? "No results match the selected filters."
    : "No live comparison currently passes every qualification rule.";
  $("#opportunitiesEmpty").classList.toggle("hidden", rows.length > 0);
  $("#opportunitiesTable").innerHTML = rows.length ? `<table><thead><tr><th>Event</th><th>Market</th><th>Prediction</th><th>Sportsbook</th><th>Ask</th><th>Implied</th><th>Edge</th><th>Quality</th></tr></thead><tbody>
  ${rows.map(o => `<tr><td data-label="Match"><span class="cell-main">${esc(o.home_team)} vs ${esc(o.away_team)}</span><span class="cell-sub">${esc(o.competition)}</span></td>
  <td data-label="Market"><span class="cell-main">${title(o.market_type)}</span><span class="cell-sub">${title(o.sport)} · ${esc(selectionLabel(o))}</span></td>
  <td data-label="Prediction"><span class="cell-main">${title(o.prediction_market_provider)}</span><span class="cell-sub">${number(o.prediction_quote_age_seconds,0)}s old</span></td>
  <td data-label="Sportsbook"><span class="cell-main">${esc(o.bookmaker_display_name)}</span><span class="cell-sub">${number(o.sportsbook_decimal_odds,2)} decimal</span></td>
  <td data-label="Ask">${pct(o.prediction_market_best_ask)}</td><td data-label="Implied">${pct(o.sportsbook_implied_probability)}</td>
  <td data-label="Edge" class="positive">+${number(o.edge_percentage_points,2)} pp</td><td data-label="Quality"><span class="cell-main">${number(o.spread_cents,1)}¢ spread</span><span class="cell-sub">${money(o.total_depth_within_window_usd)} depth</span></td></tr>`).join("")}</tbody></table>` : "";
}

function candidateFieldValue(key, value) {
  if (value == null || value === "") return null;
  if (Array.isArray(value) || typeof value === "object") return JSON.stringify(value);
  if (/(?:_at|timestamp|time_utc|close_time)$/.test(key) && !Number.isNaN(Date.parse(value))) {
    return `${dateTime(value)} · UTC ${value}`;
  }
  return value;
}

function candidateSection(heading, object) {
  return `<section><p class="eyebrow">${esc(heading)}</p>${Object.entries(object || {}).map(([key, value]) =>
    auditField(title(key), candidateFieldValue(key, value))
  ).join("")}</section>`;
}

function candidateDetails(candidate) {
  const decision = {
    accepted: candidate.accepted,
    edge_percentage_points: candidate.edge_percentage_points,
    configured_threshold: candidate.configured_threshold,
    evaluated_at: candidate.evaluated_at,
    rejection_reasons: candidate.rejection_reasons,
  };
  return `<details class="audit-details candidate-details">
    <summary>Inspect all candidate data</summary>
    <div class="audit-comparison candidate-audit">
      ${candidateSection("Decision", decision)}
      ${candidateSection("Liquidity qualification", candidate.liquidity)}
      ${candidateSection("Prediction-market quote", candidate.prediction_quote)}
      ${candidateSection("Sportsbook quote", candidate.sportsbook_quote)}
      ${candidateSection("Prediction order book", candidate.order_book)}
    </div>
  </details>`;
}

function renderCandidates() {
  const sport = $("#candidateSportFilter").value, status = $("#candidateStatusFilter").value, reason = $("#candidateReasonFilter").value, market = $("#candidateMarketFilter").value;
  const search = $("#candidateSearch").value.trim().toLocaleLowerCase();
  const rows = state.candidates.filter(c => (!sport || c.prediction_quote.sport === sport) && (!status || (status === "accepted") === c.accepted) && (!reason || c.rejection_reasons.includes(reason)) && (!market || c.prediction_quote.market_type === market) && (!search || searchable(
    c.prediction_quote.home_team, c.prediction_quote.away_team,
    c.prediction_quote.competition, c.prediction_quote.provider,
    c.prediction_quote.provider_event_id, c.prediction_quote.provider_market_id,
    c.prediction_quote.market_type, c.rejection_reasons
  ).includes(search))).slice(0, 120);
  $("#candidateList").innerHTML = rows.map(c => `<article class="candidate-card">
    <div><span class="cell-main">${esc(c.prediction_quote.home_team)} vs ${esc(c.prediction_quote.away_team)}</span><span class="cell-sub">${title(c.prediction_quote.sport)} · ${title(c.prediction_quote.provider)} · ${title(c.prediction_quote.market_type)} · ${esc(selectionLabel(c.prediction_quote))}</span></div>
    <div><span class="cell-sub">Spread</span><span class="cell-main">${c.liquidity.spread_cents == null ? "—" : `${number(c.liquidity.spread_cents,1)}¢`}</span></div>
    <div><span class="cell-sub">Depth</span><span class="cell-main">${money(c.liquidity.total_depth_within_window_usd)}</span></div>
    <div class="reasons">${c.rejection_reasons.length ? c.rejection_reasons.map(r => `<span class="reason">${title(r)}</span>`).join("") : `<span class="cell-sub">All checks passed</span>`}</div>
    <span class="decision ${c.accepted ? "accepted" : ""}">${c.accepted ? "Accepted" : "Rejected"}</span>
    <div class="candidate-edge-inputs">
      <div><small>Prediction ask</small><strong>${pct(c.prediction_quote.best_ask_probability, 2)}</strong></div>
      <div><small>Sportsbook odds</small><strong>${number(c.sportsbook_quote.decimal_odds, 2)}</strong></div>
      <div><small>Implied probability</small><strong>${pct(c.sportsbook_quote.implied_probability, 2)}</strong></div>
      <div><small>Calculated edge</small><strong class="${Number(c.edge_percentage_points) >= 0 ? "positive" : ""}">${Number(c.edge_percentage_points) >= 0 ? "+" : ""}${number(c.edge_percentage_points, 2)} pp</strong></div>
      <div><small>Configured threshold</small><strong>${number(c.configured_threshold, 2)} pp</strong></div>
    </div>
    ${candidateDetails(c)}
  </article>`).join("") ||
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
  const keys = ["client_timezone","discovery_calendar_days","enabled_sports","edge_threshold_pp","max_bid_ask_spread_cents","depth_window_from_midpoint_cents","min_depth_within_window_usd","min_trailing_24h_volume_usd","price_poll_interval_seconds","enabled_market_types","live_dry_run","alerts_enabled"];
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

function scheduleRefresh() {
  clearTimeout(state.refreshTimer);
  const delay = state.health?.scan_in_progress || !state.eventMatches.length ? 5000 : 30000;
  state.refreshTimer = setTimeout(async () => {
    await refresh();
    scheduleRefresh();
  }, delay);
}

$$("[data-view]").forEach(el => el.addEventListener("click", () => switchView(el.dataset.view)));
$$("[data-jump]").forEach(el => el.addEventListener("click", () => switchView(el.dataset.jump)));
$("#refreshButton").addEventListener("click", () => refresh(true));
["opSportFilter","opMarketFilter","opProviderFilter","opBookFilter"].forEach(id => $(`#${id}`).addEventListener("change", renderOpportunityTable));
$("#opSearch").addEventListener("input", renderOpportunityTable);
["candidateSportFilter","candidateStatusFilter","candidateReasonFilter","candidateMarketFilter"].forEach(id => $(`#${id}`).addEventListener("change", renderCandidates));
$("#unmatchedProviderFilter").addEventListener("change", () => {
  state.unmatchedPage = 1;
  renderUnmatchedEvents();
});
$("#matchedProviderFilter").addEventListener("change", () => {
  state.matchedPage = 1;
  renderMatchedEvents();
});
$("#matchedSearch").addEventListener("input", () => {
  state.matchedPage = 1;
  renderMatchedEvents();
});
$("#unmatchedSearch").addEventListener("input", () => {
  state.unmatchedPage = 1;
  renderUnmatchedEvents();
});
$("#candidateSearch").addEventListener("input", renderCandidates);
$("#matchedPrevious").addEventListener("click", () => {
  state.matchedPage = Math.max(1, state.matchedPage - 1);
  renderMatchedEvents();
});
$("#matchedNext").addEventListener("click", () => {
  state.matchedPage += 1;
  renderMatchedEvents();
});
$("#unmatchedPrevious").addEventListener("click", () => {
  state.unmatchedPage = Math.max(1, state.unmatchedPage - 1);
  renderUnmatchedEvents();
});
$("#unmatchedNext").addEventListener("click", () => {
  state.unmatchedPage += 1;
  renderUnmatchedEvents();
});
refresh().finally(scheduleRefresh);
