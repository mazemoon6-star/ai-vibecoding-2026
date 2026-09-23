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

function percent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  const formatted = (number * 100).toLocaleString("ko-KR", { maximumFractionDigits: 2, signDisplay: "always" });
  return `${formatted}%`;
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
    const [account, positions, strategies, broker, autoSymbols] = await Promise.all([
      api("/api/v1/account"), api("/api/v1/positions"),
      api("/api/v1/strategies"), api("/api/v1/broker/status"),
      api("/api/v1/auto-trade-symbols")
    ]);
    const a = account;
    $("#mode").textContent = a.mode || "PAPER";
    $("#trading-status").textContent = a.trading_enabled ? "거래 활성" : "거래 정지";
    $("#available-cash").textContent = money(a.available_cash, 0);
    $("#equity").textContent = money(a.equity, 0);
    $("#realized-pnl").textContent = money(a.realized_pnl, 0);
    $("#realized-pnl-before-fees").textContent = money(a.realized_pnl_before_fees, 0);
    $("#as-of").textContent = `기준 ${date(a.as_of)}`;
    const configured = broker.market_data?.enabled && broker.market_data?.credentials_configured;
    $("#broker-badge").textContent = configured ? "토스 시세 연동 준비됨" : "토스 시세 미설정";
    $("#broker-badge").classList.toggle("muted", !configured);
    queuedAutoTradeSymbols = new Set(autoSymbols.items.map((item) => item.symbol));
    const heldSymbols = new Set(positions.items
      .filter((position) => Number(position.quantity) > 0)
      .map((position) => position.symbol));
    const stockNames = new Map(autoSymbols.items.map((item) => [item.symbol, item.name || item.symbol]));
    investmentChart.render(positions.items, stockNames);
    rows($("#positions-body"), positions.items, (position) => `<tr><td><strong>${esc(position.symbol)}</strong></td><td>${esc(stockNames.get(position.symbol) || position.symbol)}</td><td>${money(position.quantity)}</td><td>${money(position.average_price, 0)}</td><td>${money(position.market_price, 0)}</td><td>${money(position.unrealized_pnl, 0)}</td></tr>`);
    rows($("#auto-trade-symbols-body"), autoSymbols.items, (item) => {
      const settings = { ...item, ...(autoStrategyDrafts.get(item.symbol) || {}) };
      return `<tr data-symbol="${esc(item.symbol)}">
        <td data-label="심볼"><strong>${esc(item.symbol)}</strong></td>
        <td data-label="종목명">${esc(item.name)}</td>
        <td data-label="상태">${esc(item.status)}</td>
        <td data-label="단기 창"><input class="auto-setting-input" aria-label="${esc(item.name)} 단기 창" type="number" min="2" max="200" step="1" data-auto-setting="short_window" value="${esc(settings.short_window ?? 3)}" /></td>
        <td data-label="장기 창"><input class="auto-setting-input" aria-label="${esc(item.name)} 장기 창" type="number" min="3" max="500" step="1" data-auto-setting="long_window" value="${esc(settings.long_window ?? 8)}" /></td>
        <td data-label="주문 수량"><input class="auto-setting-input" aria-label="${esc(item.name)} 주문 수량" type="number" min="0.00000001" step="any" data-auto-setting="order_quantity" value="${esc(settings.order_quantity ?? 1)}" /></td>
        <td class="auto-symbol-action">${heldSymbols.has(item.symbol) ? '<span class="pill">보유 중</span>' : `<button class="button ghost remove-auto-symbol" type="button" data-remove-auto-symbol="${esc(item.symbol)}">제외</button>`}</td>
      </tr>`;
    });
    rows($("#strategies-body"), strategies.items, (strategy) => `<tr><td>${esc(strategy.name)}</td><td>${esc(strategy.symbol)}</td><td>${strategy.short_window} / ${strategy.long_window}</td><td>${strategy.enabled ? "활성" : "중지"}</td></tr>`);
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
        const shortWindow = Number(row.querySelector('[data-auto-setting="short_window"]').value);
        const longWindow = Number(row.querySelector('[data-auto-setting="long_window"]').value);
        const orderQuantity = row.querySelector('[data-auto-setting="order_quantity"]').value;
        if (!Number.isInteger(shortWindow) || !Number.isInteger(longWindow) || shortWindow < 2 || longWindow < 3 || shortWindow >= longWindow || !Number.isFinite(Number(orderQuantity)) || !(Number(orderQuantity) > 0)) {
          throw new Error(`${symbol}: \uB2E8\uAE30\u00B7\uC7A5\uAE30 \uCC3D\uACFC \uC8FC\uBB38 \uC218\uB7C9\uC744 \uD655\uC778\uD558\uC138\uC694. (\uB2E8\uAE30 \uCC3D\uC740 \uC7A5\uAE30 \uCC3D\uBCF4\uB2E4 \uC791\uC544\uC57C \uD569\uB2C8\uB2E4.)`);
        }
        settings[symbol] = { short_window: shortWindow, long_window: longWindow, order_quantity: orderQuantity };
        autoStrategyDrafts.set(symbol, settings[symbol]);
      }
      options.body = JSON.stringify({ settings });
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

refresh();
setInterval(refresh, 5000);
