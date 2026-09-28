const $ = (selector) => document.querySelector(selector);
const notice = $("#notice");
let noticeTimer;
let queuedAutoTradeSymbols = new Set();
let autoDiscoveryDraft = false;
let autoDiscoveryBusy = false;
const investmentChart = new InvestmentChart($("#investment-card"));

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

function renderAutoDiscovery(discovery, items, configured) {
  if (!autoDiscoveryDraft && !autoDiscoveryBusy) {
    if (discovery.keyword) $("#auto-discovery-keyword").value = discovery.keyword;
    $("#auto-discovery-market").value = discovery.market;
    $("#auto-discovery-count").value = String(discovery.max_symbols);
    $("#auto-discovery-quantity").value = discovery.order_quantity;
    $("#auto-discovery-sizing").value = discovery.total_investment != null || !discovery.keyword ? "budget" : "quantity";
    if (discovery.total_investment != null) $("#auto-discovery-budget").value = discovery.total_investment;
    updateInvestmentMode();
  }
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
  if (selected.length) parts.push("선정 종목: " + selected.join(", "));
  if (retiring.length) parts.push("신규 매수 중단·매도 신호 감시: " + retiring.join(", "));
  if (discovery.last_scan_at) parts.push("최근 선정: " + date(discovery.last_scan_at));
  if (discovery.error) parts.push("발굴 오류: " + discovery.error);
  if (discovery.enabled && !discovery.trading_enabled) parts.push("자동 발굴·매매 시작을 누르면 자동화를 이어갑니다.");
  $("#auto-discovery-summary").textContent = parts.join(" · ")
    || "시작 후 자동 선정된 종목의 시세를 수집하고, 이평선 교차 신호가 발생하면 PAPER 주문을 실행합니다.";
  $("#auto-discovery-summary").classList.toggle("error", Boolean(discovery.error));
  $("#auto-discovery-start").disabled = autoDiscoveryBusy || discovery.scanning || !configured;
  $("#auto-discovery-stop").disabled = !autoDiscoveryBusy && !discovery.scanning && !discovery.enabled;
  renderAllocation(discovery.allocation);
}

function updateInvestmentMode() {
  const budgetMode = $("#auto-discovery-sizing").value === "budget";
  $("#auto-discovery-budget-field").hidden = !budgetMode;
  $("#auto-discovery-budget").disabled = !budgetMode;
  $("#auto-discovery-budget").required = budgetMode;
  $("#auto-discovery-quantity-field").hidden = budgetMode;
  $("#auto-discovery-quantity").disabled = budgetMode;
  $("#auto-discovery-quantity").required = !budgetMode;
  $("#auto-discovery-budget-help").textContent = budgetMode
    ? "금액 배분은 국내 주식 기준입니다. 기존 보유금액과 매수 수수료도 투자 한도에 포함하며, 1주를 살 수 없는 잔액은 현금으로 남깁니다."
    : "각 종목에 입력한 동일 수량을 적용합니다. 미국 주식은 현재 수량 지정 방식으로 이용할 수 있습니다.";
}

function renderAllocation(allocation) {
  $("#allocation-preview").hidden = !allocation;
  if (!allocation) return;
  const parts = [
    "적용된 총 투자 한도 " + currencyMoney(allocation.total_investment),
    "기존 보유·매수 예약 " + currencyMoney(allocation.committed),
    "추가 투자 가능 " + currencyMoney(allocation.available_for_investment)
  ];
  if (Number(allocation.retiring_committed) > 0) {
    parts.push("선정 제외 보유분 " + currencyMoney(allocation.retiring_committed) + "은 배분에서 차감");
  }
  $("#allocation-summary").textContent = parts.join(" · ");
  $("#allocation-body").innerHTML = allocation.items.map((item) =>
    '<tr><td><strong>' + esc(item.name) + '</strong><small class="volume-symbol">' + esc(item.symbol)
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
    queuedAutoTradeSymbols = new Set(autoSymbols.items.map((item) => item.symbol));
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

async function postControl(action) {
  try {
    const options = { method: "POST" };
    if (action === "resume") {
      options.body = JSON.stringify({ settings: {} });
    }
    const result = await api(`/api/v1/controls/${action}`, options);
    const count = result.auto_strategies?.length || 0;
    show(action === "resume" && count
      ? `거래 재개: ${count}개 종목의 이동평균 PAPER 전략을 시작했습니다.`
      : `${action} 제어가 반영되었습니다.`);
    await refresh();
  }
  catch (error) { show(error.message, true); }
}

$("#auto-discovery-form").addEventListener("input", () => { autoDiscoveryDraft = true; });
$("#auto-discovery-form").addEventListener("change", () => { autoDiscoveryDraft = true; });
$("#auto-discovery-sizing").addEventListener("change", updateInvestmentMode);
$("#auto-discovery-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (autoDiscoveryBusy) return;
  const button = $("#auto-discovery-start");
  autoDiscoveryBusy = true;
  button.disabled = true;
  button.textContent = "섹터 종목 발굴 중…";
  $("#auto-discovery-stop").disabled = false;
  try {
    const budgetMode = $("#auto-discovery-sizing").value === "budget";
    const amount = $(budgetMode ? "#auto-discovery-budget" : "#auto-discovery-quantity").value;
    if (!Number.isFinite(Number(amount)) || Number(amount) <= 0) {
      throw new Error(budgetMode ? "총 투자금액은 0보다 큰 금액으로 입력하세요." : "주문 수량은 0보다 큰 숫자로 입력하세요.");
    }
    if (budgetMode && $("#auto-discovery-market").value !== "KR") {
      throw new Error("금액 기준 분산투자는 국내 주식에서 지원합니다. 미국 주식은 수량 직접 지정을 선택하세요.");
    }
    const result = await api("/api/v1/auto-discovery/start", {
      method: "POST",
      body: JSON.stringify({
        keyword: $("#auto-discovery-keyword").value.trim(),
        market: $("#auto-discovery-market").value,
        max_symbols: Number($("#auto-discovery-count").value),
        ...(budgetMode ? { total_investment: amount } : { order_quantity: amount })
      })
    });
    autoDiscoveryDraft = false;
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
$("#volume-results").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-add-search-symbol]");
  if (!button || button.disabled) return;
  button.disabled = true;
  try {
    if (button.dataset.priceSource !== "paper") {
      await api("/api/v1/market/sync", {
        method: "POST",
        body: JSON.stringify({ symbols: [button.dataset.addSearchSymbol] })
      });
    }
    await api("/api/v1/auto-trade-symbols", {
      method: "POST",
      body: JSON.stringify({ symbol: button.dataset.addSearchSymbol })
    });
    show(`${button.dataset.stockName}을 자동매매 목록에 담았습니다. 수동 검색 영역의 ‘담은 종목 실행’을 누르면 저장된 주문 수량으로 전략이 시작됩니다.`);
    button.textContent = "담김";
    queuedAutoTradeSymbols.add(button.dataset.addSearchSymbol);
    await refresh();
  } catch (error) {
    button.disabled = false;
    show(error.message, true);
  }
});

$("#volume-search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button[type="submit"]');
  const keyword = $("#volume-keyword").value.trim();
  const market = $("#volume-market").value;
  const resultBody = $("#volume-results");
  button.disabled = true;
  $("#volume-ranked-at").textContent = "검색 중";
  resultBody.innerHTML = '<tr><td colspan="7" class="empty">종목명·코드와 산업 연관 정보를 검색하고 있습니다.</td></tr>';
  try {
    const result = await api(`/api/v1/market/related-stocks?keyword=${encodeURIComponent(keyword)}&market=${encodeURIComponent(market)}`);
    resultBody.innerHTML = result.items.length
      ? result.items.map((item) => {
        const label = item.match_type === "direct" ? "high" : item.relevance_label === "높음" ? "high" : item.relevance_label === "보통" ? "medium" : "low";
        const industries = (item.related_industries || []).map(esc).join(" · ");
        const source = item.source_url ? `<a class="profile-source" href="${esc(item.source_url)}" target="_blank" rel="noopener noreferrer">사업 정보 출처 ↗</a>` : "";
        const priceMeta = item.price_updated_at ? `기준 ${date(item.price_updated_at, true)}` : item.price_source === "paper" ? "PAPER 시세" : "시세 미조회";
        const alreadyQueued = queuedAutoTradeSymbols.has(item.symbol);
        return `<tr><td><strong>${esc(item.name)}</strong></td><td><span class="volume-symbol">${esc(item.symbol)}</span></td><td><span class="search-price">${money(item.price, 2)}</span><small class="search-price-meta">${priceMeta}</small></td><td><span class="relevance-badge ${label}">${esc(item.relevance_label)}${item.match_type === "direct" ? "" : ` · ${esc(item.relevance_score)}점`}</span></td><td class="related-industries">${industries || "-"}</td><td class="reason-cell">${esc(item.reason)}${source}</td><td><button class="button ghost add-auto-symbol" type="button" data-add-search-symbol="${esc(item.symbol)}" data-stock-name="${esc(item.name)}" data-price-source="${esc(item.price_source || "")}" ${alreadyQueued ? "disabled" : ""}>${alreadyQueued ? "담김" : "담기"}</button></td></tr>`;
      }).join("")
      : '<tr><td colspan="7" class="empty">관련 종목을 찾을 수 없습니다.</td></tr>';
    $("#volume-ranked-at").textContent = result.items.length
      ? result.search_type === "direct"
        ? `${result.items.length}개 종목 · 종목 직접 검색`
        : `${result.items.length}개 종목 · 연관 검색어 ${result.expanded_keywords.length}개`
      : `확장어 ${result.expanded_keywords.length}개 검색 완료`;
  } catch (error) {
    resultBody.innerHTML = `<tr><td colspan="7" class="empty">${esc(error.message)}</td></tr>`;
    $("#volume-ranked-at").textContent = "조회 실패";
  } finally {
    button.disabled = false;
  }
});
document.querySelectorAll("[data-control]").forEach((button) => button.addEventListener("click", () => postControl(button.dataset.control)));

const assistantHistory = [];
let assistantConfigured = false;
let assistantBusy = false;

function appendAssistantMessage(role, message, extraClass = "") {
  const element = document.createElement("div");
  element.className = `assistant-message assistant-message-${role}${extraClass ? ` ${extraClass}` : ""}`;
  element.textContent = message;
  $("#assistant-messages").appendChild(element);
  $("#assistant-messages").scrollTop = $("#assistant-messages").scrollHeight;
  return element;
}

function setAssistantEnabled(enabled) {
  assistantConfigured = enabled;
  $("#assistant-input").disabled = !enabled;
  $("#assistant-send").disabled = !enabled;
  document.querySelectorAll("[data-assistant-prompt]").forEach((button) => { button.disabled = !enabled; });
}

async function initializeAssistant() {
  try {
    const status = await api("/api/v1/assistant/status");
    const statusElement = $("#assistant-status");
    statusElement.textContent = status.configured ? "사용 가능" : "API 키 필요";
    statusElement.classList.toggle("ready", status.configured);
    statusElement.classList.toggle("error", !status.configured);
    setAssistantEnabled(status.configured);
    if (!status.configured) {
      appendAssistantMessage("bot", "서버의 .env에 OPENAI_API_KEY를 설정하고 앱을 다시 시작하면 채팅을 사용할 수 있습니다.", "assistant-message-error");
    }
  } catch (error) {
    $("#assistant-status").textContent = "연결 실패";
    $("#assistant-status").classList.add("error");
    setAssistantEnabled(false);
  }
}

function setAssistantOpen(open) {
  $("#assistant-panel").hidden = !open;
  $("#assistant-toggle").setAttribute("aria-expanded", String(open));
  if (open && assistantConfigured) $("#assistant-input").focus();
}

async function sendAssistantMessage(rawMessage) {
  const message = rawMessage.trim();
  if (!message || !assistantConfigured || assistantBusy) return;
  const previousHistory = assistantHistory.slice(-10);
  assistantHistory.push({ role: "user", content: message });
  appendAssistantMessage("user", message);
  assistantBusy = true;
  $("#assistant-send").disabled = true;
  $("#assistant-input").disabled = true;
  const thinking = appendAssistantMessage("bot", "답변을 준비하고 있습니다…", "assistant-message-thinking");
  try {
    const result = await api("/api/v1/assistant/chat", {
      method: "POST",
      body: JSON.stringify({ message, history: previousHistory })
    });
    thinking.remove();
    assistantHistory.push({ role: "assistant", content: result.reply });
    if (assistantHistory.length > 12) assistantHistory.splice(0, assistantHistory.length - 12);
    appendAssistantMessage("bot", result.reply);
  } catch (error) {
    thinking.remove();
    appendAssistantMessage("bot", error.message, "assistant-message-error");
  } finally {
    assistantBusy = false;
    $("#assistant-send").disabled = false;
    $("#assistant-input").disabled = false;
    $("#assistant-input").focus();
  }
}

$("#assistant-toggle").addEventListener("click", () => setAssistantOpen($("#assistant-panel").hidden));
$("#assistant-close").addEventListener("click", () => setAssistantOpen(false));
$("#assistant-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = $("#assistant-input").value;
  $("#assistant-input").value = "";
  await sendAssistantMessage(message);
});
$("#assistant-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("#assistant-form").requestSubmit();
  }
});
document.querySelectorAll("[data-assistant-prompt]").forEach((button) => button.addEventListener("click", () => sendAssistantMessage(button.dataset.assistantPrompt)));
document.addEventListener("keydown", (event) => { if (event.key === "Escape") setAssistantOpen(false); });

refresh();
initializeAssistant();
setInterval(refresh, 5000);
