const $ = (selector) => document.querySelector(selector);
const notice = $("#notice");
let noticeTimer;
let autoDiscoveryDraft = false;
let autoDiscoveryDraftRevision = 0;
let autoDiscoveryBusy = false;
let autoDiscoverySearchBusy = false;
let latestAvailableCash = null;
let marketDataReady = false;
let discoveryScanning = false;
let searchedSector = null;
let searchCandidates = [];
const chosenSymbols = new Set();
const investmentChart = new InvestmentChart($("#investment-card"));
const dashboardPanels = $("#dashboard-panels");

$("#dashboard-panel-nav").addEventListener("wheel", (event) => {
  if (event.ctrlKey || Math.abs(event.deltaY) <= Math.abs(event.deltaX)) return;
  const maxScroll = Math.max(0, dashboardPanels.scrollWidth - dashboardPanels.clientWidth);
  const destination = event.deltaY > 0 ? maxScroll : 0;
  if (maxScroll === 0 || Math.abs(dashboardPanels.scrollLeft - destination) < 2) return;
  event.preventDefault();
  dashboardPanels.scrollTo({ left: destination });
}, { passive: false });

function esc(value) {
  return String(value ?? "-").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  }[char]));
}

function money(value, maximumFractionDigits = 4) {
  if (value === null || value === undefined) return "-";
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString("ko-KR", { maximumFractionDigits }) : esc(value);
}

function currencyMoney(value, currency = "KRW", signed = false) {
  if (value === null || value === undefined) return "-";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return esc(value);
  const digits = currency === "USD" ? 2 : 0;
  const threshold = 0.5 * (10 ** -digits);
  const number = Math.abs(parsed) < threshold ? 0 : parsed;
  const sign = signed && number > 0 ? "+" : number < 0 ? "-" : "";
  const prefix = currency === "USD" ? "$" : "₩";
  return `${sign}${prefix}${Math.abs(number).toLocaleString("ko-KR", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  })}`;
}

function positionCurrency(position) {
  if (position.currency === "KRW" || position.currency === "USD") return position.currency;
  return /^\d{6}$/.test(String(position.symbol || "")) ? "KRW" : "USD";
}

function date(value, omitSeconds = false) {
  if (!value) return "-";
  const parsed = new Date(value);
  const options = {
    year: "numeric", month: "numeric", day: "numeric",
    hour: "2-digit", minute: "2-digit", hour12: false
  };
  if (!omitSeconds) options.second = "2-digit";
  return Number.isNaN(parsed.getTime()) ? esc(value) : parsed.toLocaleString("ko-KR", options);
}

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body?.error?.message || `요청 실패 (${response.status})`);
  return body;
}

function show(message, isError = false, source = "action") {
  clearTimeout(noticeTimer);
  notice.hidden = false;
  notice.dataset.source = source;
  notice.textContent = message;
  notice.classList.toggle("error", isError);
  if (!isError) noticeTimer = setTimeout(() => { notice.hidden = true; }, 5000);
}

function updateManualStartState() {
  const matchesSearch = searchedSector?.keyword === $("#auto-discovery-keyword").value.trim()
    && searchedSector?.market === $("#auto-discovery-market").value;
  const count = chosenSymbols.size;
  const cashPercent = Number($("#auto-discovery-percentage").value);
  if (searchedSector) {
    $("#auto-discovery-selection-summary").textContent = count
      ? `선택 ${count}개 종목 · 가용현금의 ${cashPercent}% 안에서 자동 배분${matchesSearch ? "" : " · 검색 조건이 바뀌어 다시 검색해야 합니다."}`
      : "자동매매할 종목을 하나 이상 선택하세요.";
  }
  $("#auto-discovery-start").disabled = autoDiscoveryBusy || autoDiscoverySearchBusy || discoveryScanning
    || !marketDataReady || !matchesSearch || $("#auto-discovery-market").value !== "KR"
    || !count || !Number.isInteger(cashPercent) || cashPercent < 10 || cashPercent > 100 || cashPercent % 10 !== 0;
}

function renderAutoDiscovery(discovery, items, configured) {
  if (!autoDiscoveryDraft && !autoDiscoveryBusy) {
    if (discovery.keyword) $("#auto-discovery-keyword").value = discovery.keyword;
    $("#auto-discovery-market").value = discovery.market;
    $("#auto-discovery-percentage").value = String(discovery.cash_percentage || 10);
  }
  marketDataReady = Boolean(configured);
  discoveryScanning = Boolean(discovery.scanning);
  updateInvestmentPreview();
  const state = $("#auto-discovery-state");
  const running = discovery.enabled && discovery.trading_enabled;
  state.textContent = discovery.scanning ? "종목 발굴 중" : !discovery.enabled
    ? "자동 발굴 꺼짐" : running ? "자동 발굴·매매 중" : "자동매매 일시 정지";
  state.classList.toggle("badge", running);
  state.classList.toggle("pill", !running);
  const names = new Map(items.map((item) => [item.symbol, item.name || item.symbol]));
  const selected = (discovery.selected_symbols || []).map((symbol) => names.get(symbol) || symbol);
  const retiring = (discovery.retiring_symbols || []).map((symbol) => names.get(symbol) || symbol);
  const parts = [];
  if (!configured) parts.push("토스 시세 연동을 설정하고 서버를 다시 시작하면 자동 발굴을 사용할 수 있습니다.");
  if (discovery.active_sectors?.length) {
    parts.push("운영 섹터: " + discovery.active_sectors.map((sector) =>
      sector.keyword + " (" + sector.symbols.length + "종목, " + sector.cash_percentage + "% 적용)"
    ).join(" / "));
  } else {
    if (discovery.keyword) parts.push("적용 섹터: " + discovery.keyword + (discovery.market === "US" ? " (미국 주식)" : " (국내 주식)"));
    if (discovery.cash_percentage != null) parts.push("적용 투자 비율: " + discovery.cash_percentage + "%");
  }
  if (discovery.planner_reason) parts.push("AI 배분 근거: " + discovery.planner_reason);
  if (selected.length) parts.push("선정 종목: " + selected.join(", "));
  if (retiring.length) parts.push("신규 매수 중단·매도 신호 감시: " + retiring.join(", "));
  if (discovery.last_scan_at) parts.push("최근 선정: " + date(discovery.last_scan_at));
  if (discovery.error) parts.push("발굴 오류: " + discovery.error);
  if (discovery.enabled && !discovery.trading_enabled) parts.push("자동 발굴·매매 시작을 누르면 자동화를 이어갑니다.");
  $("#auto-discovery-summary").textContent = parts.join(" · ")
    || "시작 후 자동 선정된 종목의 시세를 수집하고, 이평선 교차 신호가 발생하면 PAPER 주문을 실행합니다.";
  $("#auto-discovery-summary").classList.toggle("error", Boolean(discovery.error));
  updateManualStartState();
  $("#auto-discovery-stop").disabled = !autoDiscoveryBusy && !discovery.scanning && !discovery.enabled;
  renderAllocation(discovery.allocation, discovery.active_sectors || []);
}

function updateInvestmentPreview() {
  if (latestAvailableCash == null) return;
  const percentage = Number($("#auto-discovery-percentage").value);
  const amount = Math.floor(latestAvailableCash * percentage) / 100;
  $("#auto-discovery-cash-preview").textContent = "현재 가용현금 " + currencyMoney(latestAvailableCash)
    + " × " + percentage + "% = 신규 투자 예상 한도 " + currencyMoney(amount) + " · 실제 한도는 시작 시 확정";
}

function renderAllocation(allocation, sectors = []) {
  $("#allocation-preview").hidden = !allocation;
  if (!allocation) return;
  const parts = [
    "적용된 총 투자 한도 " + currencyMoney(allocation.total_investment),
    "기존 보유·매수 예약 " + currencyMoney(allocation.committed),
    "추가 투자 가능 " + currencyMoney(allocation.available_for_investment)
  ];
  if (allocation.cash_percentage != null && !allocation.items.some((item) => item.sectors?.length)) {
    parts.unshift("시작 시 가용현금 " + currencyMoney(allocation.cash_base) + " · 투자 비율 " + allocation.cash_percentage + "%");
  }
  if (sectors.length) {
    parts.push("섹터별 한도: " + sectors.map((sector) =>
      sector.keyword + " " + sector.cash_percentage + "% = " + currencyMoney(sector.investment_budget)
      + " (" + sector.symbols.length + "종목)"
    ).join(" / "));
  }
  if (Number(allocation.retiring_committed) > 0) {
    parts.push("선정 제외 보유분 " + currencyMoney(allocation.retiring_committed) + "은 배분에서 차감");
  }
  $("#allocation-summary").textContent = parts.join(" · ");
  $("#allocation-body").innerHTML = allocation.items.map((item) =>
    '<tr><td><strong>' + esc(item.name) + '</strong><small class="volume-symbol">' + esc(item.symbol)
    + (item.sectors?.length ? ' · ' + esc(item.sectors.join(', ')) : '')
    + '</small></td><td>' + currencyMoney(item.budget) + '</td><td>'
    + money(item.estimated_quantity, 0) + '주</td><td>' + currencyMoney(item.estimated_total) + '</td></tr>'
  ).join("");
}

async function refresh() {
  try {
    const [account, positions, broker, autoSymbols, exchangeRate, discovery] = await Promise.all([
      api("/api/v1/account"), api("/api/v1/positions"),
      api("/api/v1/broker/status"),
      api("/api/v1/auto-trade-symbols"),
      api("/api/v1/market/exchange-rate").catch(() => null),
      api("/api/v1/auto-discovery")
    ]);
    const a = account;
    latestAvailableCash = Math.max(0, Number(a.available_cash) || 0);
    const hasUsdPositions = positions.items.some((position) => positionCurrency(position) === "USD" && Number(position.quantity) > 0);
    $("#mode").textContent = a.mode || "PAPER";
    $("#trading-status").textContent = a.trading_enabled ? "거래 활성" : "거래 정지";
    $("#available-cash").textContent = hasUsdPositions ? money(a.available_cash, 0) : currencyMoney(a.available_cash);
    $("#cash-note").textContent = hasUsdPositions ? "PAPER 장부 잔액 · 혼합 통화" : "예약 금액 제외";
    $("#equity-label").textContent = hasUsdPositions ? "PAPER 장부 평가액" : "총 평가금액";
    $("#equity").textContent = hasUsdPositions ? money(a.equity, 0) : currencyMoney(a.equity);
    $("#realized-pnl").textContent = currencyMoney(a.realized_pnl, "KRW", true);
    $("#realized-pnl-before-fees").textContent = currencyMoney(a.realized_pnl_before_fees, "KRW", true);
    $("#as-of").textContent = `${hasUsdPositions ? "KRW·USD 단순 합산 · " : ""}기준 ${date(a.as_of)}`;
    const configured = broker.market_data?.enabled && broker.market_data?.credentials_configured;
    $("#broker-badge").textContent = configured ? "토스 시세 연동 준비됨" : "토스 시세 미설정";
    $("#broker-badge").classList.toggle("muted", !configured);
    renderAutoDiscovery(discovery, autoSymbols.items, configured);
    const stockNames = new Map(autoSymbols.items.map((item) => [item.symbol, item.name || item.symbol]));
    const fxRates = new Map([["KRW", 1]]);
    if (exchangeRate?.baseCurrency === "USD" && exchangeRate?.quoteCurrency === "KRW" && Number(exchangeRate.rate) > 0) {
      fxRates.set("USD", Number(exchangeRate.rate));
    }
    investmentChart.render(positions.items, stockNames, fxRates);
    const refreshedAt = new Date();
    $("#last-refreshed").textContent = `마지막 갱신 ${date(refreshedAt)}`;
    $("#last-refreshed").dateTime = refreshedAt.toISOString();
    if (notice.dataset.source === "refresh") notice.hidden = true;
  } catch (error) {
    show(error.message, true, "refresh");
  }
}

function markAutoDiscoveryDraft() {
  autoDiscoveryDraft = true;
  autoDiscoveryDraftRevision += 1;
  updateManualStartState();
}
$("#auto-discovery-form").addEventListener("input", markAutoDiscoveryDraft);
$("#auto-discovery-form").addEventListener("change", markAutoDiscoveryDraft);
document.querySelectorAll("[data-sector-keyword]").forEach((button) => button.addEventListener("click", () => {
  $("#auto-discovery-keyword").value = button.dataset.sectorKeyword;
  markAutoDiscoveryDraft();
  $("#auto-discovery-form").requestSubmit($("#auto-discovery-search"));
}));
$("#auto-discovery-percentage").addEventListener("change", () => {
  updateInvestmentPreview();
  updateManualStartState();
});
$("#auto-discovery-search-results").addEventListener("change", (event) => {
  const symbol = event.target?.dataset?.selectionSymbol;
  if (!symbol || !searchCandidates.some((item) => item.symbol === symbol)) return;
  if (event.target.checked) chosenSymbols.add(symbol);
  else chosenSymbols.delete(symbol);
  updateManualStartState();
});
$("#auto-discovery-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (autoDiscoverySearchBusy) return;
  const keyword = $("#auto-discovery-keyword").value.trim();
  if (!keyword) {
    show("검색할 섹터 키워드를 입력하세요.", true);
    return;
  }
  const market = $("#auto-discovery-market").value;
  const button = $("#auto-discovery-search");
  const resultBody = $("#auto-discovery-search-results");
  autoDiscoverySearchBusy = true;
  searchedSector = null;
  searchCandidates = [];
  chosenSymbols.clear();
  updateManualStartState();
  button.disabled = true;
  $("#auto-discovery-search-preview").hidden = false;
  $("#auto-discovery-search-summary").textContent = keyword + " · 검색 중";
  $("#auto-discovery-selection-summary").textContent = "검색 후 자동매매할 종목을 선택하세요.";
  resultBody.innerHTML = '<tr><td colspan="4" class="empty">관련 종목을 검색하고 있습니다.</td></tr>';
  try {
    const result = await api(`/api/v1/market/related-stocks?keyword=${encodeURIComponent(keyword)}&market=${encodeURIComponent(market)}`);
    searchCandidates = result.items || [];
    searchedSector = { keyword, market };
    resultBody.innerHTML = searchCandidates.length
      ? searchCandidates.map((item) => '<tr><td><strong>' + esc(item.name) + '</strong></td><td>'
        + esc(item.symbol) + '</td><td>' + currencyMoney(item.price, market === "US" ? "USD" : "KRW")
        + '</td><td><input class="sector-select-input" type="checkbox"'
        + ' data-selection-symbol="' + esc(item.symbol) + '" aria-label="' + esc(item.name) + ' 자동매매 종목 선택" /></td></tr>').join("")
      : '<tr><td colspan="4" class="empty">관련 종목을 찾을 수 없습니다.</td></tr>';
    $("#auto-discovery-search-summary").textContent = `검색 섹터: ${keyword} (${market === "US" ? "미국 주식" : "국내 주식"}) · ${searchCandidates.length}개 후보 · 검색만 완료`;
  } catch (error) {
    resultBody.innerHTML = '<tr><td colspan="4" class="empty">' + esc(error.message) + '</td></tr>';
    $("#auto-discovery-search-summary").textContent = keyword + " · 조회 실패";
  } finally {
    autoDiscoverySearchBusy = false;
    button.disabled = false;
    updateManualStartState();
  }
});
$("#auto-discovery-start").addEventListener("click", async () => {
  if (autoDiscoveryBusy) return;
  if (!$("#auto-discovery-form").reportValidity()) return;
  const draftRevision = autoDiscoveryDraftRevision;
  const button = $("#auto-discovery-start");
  autoDiscoveryBusy = true;
  button.disabled = true;
  button.textContent = "섹터 종목 발굴 중…";
  $("#auto-discovery-stop").disabled = false;
  try {
    const percentage = Number($("#auto-discovery-percentage").value);
    if (!Number.isInteger(percentage) || percentage < 10 || percentage > 100 || percentage % 10 !== 0) {
      throw new Error("투자 비율은 10%부터 100%까지 10% 단위로 선택하세요.");
    }
    if ($("#auto-discovery-market").value !== "KR") {
      throw new Error("가용현금 비율 투자는 국내 주식에서 지원합니다.");
    }
    if (!searchedSector || searchedSector.keyword !== $("#auto-discovery-keyword").value.trim()
        || searchedSector.market !== $("#auto-discovery-market").value) {
      throw new Error("현재 섹터를 검색한 뒤 자동매매할 종목을 선택하세요.");
    }
    if (!chosenSymbols.size) throw new Error("자동매매할 종목을 하나 이상 선택하세요.");
    if (!marketDataReady) throw new Error("토스 시세 연동을 설정해야 자동매매를 시작할 수 있습니다.");
    const result = await api("/api/v1/auto-discovery/start", {
      method: "POST",
      body: JSON.stringify({
        keyword: $("#auto-discovery-keyword").value.trim(),
        market: $("#auto-discovery-market").value,
        cash_percentage: percentage,
        chosen_symbols: [...chosenSymbols]
      })
    });
    if (autoDiscoveryDraftRevision === draftRevision) autoDiscoveryDraft = false;
    show("자동 발굴·매매 시작: " + result.status.selected_symbols.length + "개 종목의 시세 수집과 PAPER 전략을 실행합니다.");
  } catch (error) {
    show(error.message, true);
  } finally {
    autoDiscoveryBusy = false;
    button.textContent = "자동 발굴·매매 시작";
    await refresh();
  }
});
$("#auto-discovery-stop").addEventListener("click", async (event) => {
  event.currentTarget.disabled = true;
  try {
    await api("/api/v1/auto-discovery/stop", { method: "POST" });
    show("자동 발굴과 PAPER 매매를 정지했습니다. 보유 포지션은 유지됩니다.");
  } catch (error) {
    show(error.message, true);
  } finally {
    await refresh();
  }
});
refresh();
setInterval(refresh, 5000);
