const $ = (selector) => document.querySelector(selector);
const notice = $("#notice");
let noticeTimer;
let queuedAutoTradeSymbols = new Set();
const autoStrategyDrafts = new Map();
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

function percent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  const percentage = Math.abs(number * 100) < 0.005 ? 0 : number * 100;
  const formatted = Math.abs(percentage).toLocaleString("ko-KR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${percentage > 0 ? "+" : percentage < 0 ? "-" : ""}${formatted}%`;
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

function rows(target, values, template, empty = "데이터가 없습니다.") {
  target.innerHTML = values.length ? values.map(template).join("") : `<tr><td class="empty" colspan="8">${empty}</td></tr>`;
}

async function refresh() {
  try {
    const [account, positions, strategies, broker, autoSymbols, exchangeRate] = await Promise.all([
      api("/api/v1/account"), api("/api/v1/positions"),
      api("/api/v1/strategies"), api("/api/v1/broker/status"),
      api("/api/v1/auto-trade-symbols"),
      api("/api/v1/market/exchange-rate").catch(() => null)
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
    const heldQuantities = new Map(positions.items
      .filter((position) => Number(position.quantity) > 0)
      .map((position) => [position.symbol, position.quantity]));
    const stockNames = new Map(autoSymbols.items.map((item) => [item.symbol, item.name || item.symbol]));
    const fxRates = new Map([["KRW", 1]]);
    if (exchangeRate?.baseCurrency === "USD" && exchangeRate?.quoteCurrency === "KRW" && Number(exchangeRate.rate) > 0) {
      fxRates.set("USD", Number(exchangeRate.rate));
    }
    investmentChart.render(positions.items, stockNames, fxRates);
    rows($("#positions-body"), positions.items, (position) => {
      const currency = positionCurrency(position);
      const pnl = Number(position.unrealized_pnl);
      const costBasis = Number(position.cost_basis ?? Number(position.average_price) * Number(position.quantity));
      const pnlReturn = position.unrealized_return ?? (Number.isFinite(pnl) && costBasis > 0 ? pnl / costBasis : null);
      const pnlClass = pnl > 0 ? "positive" : pnl < 0 ? "negative" : "neutral";
      return `<tr>
        <td><strong>${esc(position.symbol)}</strong></td>
        <td>${esc(position.name || stockNames.get(position.symbol) || position.symbol)}</td>
        <td>${money(position.quantity)}</td>
        <td>${currencyMoney(position.average_price, currency)}</td>
        <td>${currencyMoney(position.market_price, currency)}</td>
        <td><span class="position-pnl ${pnlClass}">${currencyMoney(position.unrealized_pnl, currency, true)}</span><small class="position-return ${pnlClass}">${percent(pnlReturn)}</small></td>
        <td><span class="price-time">${date(position.price_updated_at, true)}</span><small class="currency-code">${currency}</small></td>
      </tr>`;
    });
    rows($("#auto-trade-symbols-body"), autoSymbols.items, (item) => {
      const draft = autoStrategyDrafts.get(item.symbol);
      const settings = { ...item, ...(draft || {}) };
      return `<tr data-symbol="${esc(item.symbol)}">
        <td data-label="심볼"><strong>${esc(item.symbol)}</strong></td>
        <td data-label="종목명">${esc(item.name)}</td>
        <td data-label="상태">${esc(item.status)}</td>
        <td data-label="현재 보유">${money(heldQuantities.get(item.symbol) || 0)}주</td>
        <td data-label="1회 주문 수량"><div class="auto-setting-control"><input class="auto-setting-input" aria-label="${esc(item.name)} 1회 주문 수량" type="number" min="0.00000001" step="any" data-auto-setting="order_quantity" value="${esc(settings.order_quantity ?? 1)}" /><small class="setting-state${draft ? " pending" : ""}" data-setting-state>${draft ? "변경됨 · 재개 시 적용" : "적용됨"}</small></div></td>
        <td class="auto-symbol-action">${heldQuantities.has(item.symbol) ? '<span class="pill" title="보유 포지션을 먼저 정리해야 제외할 수 있습니다.">보유 중</span>' : `<button class="button ghost remove-auto-symbol" type="button" data-remove-auto-symbol="${esc(item.symbol)}">제외</button>`}</td>
      </tr>`;
    });
    const activeStrategies = strategies.items.filter((strategy) => strategy.enabled && queuedAutoTradeSymbols.has(strategy.symbol));
    rows($("#strategies-body"), activeStrategies, (strategy) => `<tr><td>${esc(stockNames.get(strategy.symbol) || strategy.symbol)} 자동매매</td><td>${esc(strategy.symbol)}</td><td><span class="badge">운영 중</span></td></tr>`, "현재 실행 중인 전략이 없습니다.");
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
      const settings = {};
      for (const row of document.querySelectorAll("#auto-trade-symbols-body tr[data-symbol]")) {
        const symbol = row.dataset.symbol;
        const orderQuantity = row.querySelector('[data-auto-setting="order_quantity"]').value;
        if (!Number.isFinite(Number(orderQuantity)) || !(Number(orderQuantity) > 0)) {
          throw new Error(`${symbol}: 주문 수량은 0보다 큰 숫자로 입력하세요.`);
        }
        settings[symbol] = { order_quantity: orderQuantity };
        autoStrategyDrafts.set(symbol, settings[symbol]);
      }
      options.body = JSON.stringify({ settings });
    }
    const result = await api(`/api/v1/controls/${action}`, options);
    if (action === "resume") autoStrategyDrafts.clear();
    const count = result.auto_strategies?.length || 0;
    show(action === "resume" && count
      ? `거래 재개: ${count}개 종목의 이동평균 PAPER 전략을 시작했습니다.`
      : `${action} 제어가 반영되었습니다.`);
    await refresh();
  }
  catch (error) { show(error.message, true); }
}

$("#refresh").addEventListener("click", refresh);
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
    show(`${button.dataset.stockName}을 자동매매 목록에 담았습니다. 거래 재개를 누르면 전략이 시작됩니다.`);
    button.textContent = "담김";
    queuedAutoTradeSymbols.add(button.dataset.addSearchSymbol);
    await refresh();
  } catch (error) {
    button.disabled = false;
    show(error.message, true);
  }
});
$("#auto-trade-symbols-body").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-remove-auto-symbol]");
  if (!button) return;
  try {
    await api(`/api/v1/auto-trade-symbols/${encodeURIComponent(button.dataset.removeAutoSymbol)}`, { method: "DELETE" });
    autoStrategyDrafts.delete(button.dataset.removeAutoSymbol);
    show(`${button.dataset.removeAutoSymbol}을 자동매매 목록에서 제외했습니다.`);
    await refresh();
  } catch (error) { show(error.message, true); }
});
$("#auto-trade-symbols-body").addEventListener("input", (event) => {
  const input = event.target.closest("[data-auto-setting]");
  if (!input) return;
  const row = input.closest("tr[data-symbol]");
  const current = autoStrategyDrafts.get(row.dataset.symbol) || {};
  current[input.dataset.autoSetting] = input.value;
  autoStrategyDrafts.set(row.dataset.symbol, current);
  const state = row.querySelector("[data-setting-state]");
  if (state) {
    state.textContent = "변경됨 · 재개 시 적용";
    state.classList.add("pending");
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
