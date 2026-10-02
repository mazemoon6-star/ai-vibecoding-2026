const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const vm = require("node:vm");

const html = readFileSync(join(__dirname, "../auto_trader/static/index.html"), "utf8");
const css = readFileSync(join(__dirname, "../auto_trader/static/styles.css"), "utf8");
const script = readFileSync(join(__dirname, "../auto_trader/static/app.js"), "utf8");

class Element {
  constructor() {
    this.value = "";
    this.textContent = "";
    this.innerHTML = "";
    this.dataset = {};
    this.listeners = {};
    this.hidden = false;
    this.disabled = false;
    this.scrollLeft = 0;
    this.scrollWidth = 0;
    this.clientWidth = 0;
    this.classList = { toggle() {}, add() {}, remove() {} };
  }
  addEventListener(name, callback) {
    (this.listeners[name] ||= []).push(callback);
  }
  async emit(name, extra = {}) {
    const event = { currentTarget: this, preventDefault() { this.prevented = true; }, ...extra };
    for (const callback of this.listeners[name] || []) await callback(event);
    return event;
  }
  reportValidity() { return true; }
  appendChild() {}
  setAttribute() {}
  focus() {}
  scrollTo({ left }) { this.scrollLeft = left; }
  querySelector() { return new Element(); }
}

async function fixture() {
  const elements = new Map();
  const find = (selector) => {
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  };
  const allocation = (symbols, total = 10000000) => ({
    total_investment: String(total), committed: "0", available_for_investment: String(total),
    items: symbols.map((symbol) => ({ symbol, name: symbol === "066570" ? "LG전자" : "GST",
      budget: String(total / symbols.length), estimated_quantity: "10", estimated_total: "1000" }))
  });
  const state = {
    enabled: true, trading_enabled: true, keyword: "냉각", market: "KR", max_symbols: 5,
    order_quantity: "1", total_investment: "10000000", selected_symbols: ["066570", "083450"],
    retiring_symbols: [], allocation: allocation(["066570", "083450"]),
    planner: { configured: false, message: "OpenAI 키가 없습니다." }
  };
  const calls = [];
  let searchError = false;
  let searchItems = [{ symbol: "083450", name: "GST", price: "50000" }];
  let startGate = null;
  const context = vm.createContext({
    document: { querySelector: find, querySelectorAll: (selector) => selector === "[data-sector-keyword]"
      ? [...html.matchAll(/data-sector-keyword="([^"]+)"/g)].map((match) => {
        const button = find("sector:" + match[1]);
        button.dataset.sectorKeyword = match[1];
        return button;
      }) : [], addEventListener() {},
      createElement: () => new Element() },
    InvestmentChart: class { render() {} },
    setInterval() {}, setTimeout() {}, clearTimeout() {},
    fetch: async (path, options) => {
      calls.push({ path, ...options });
      let body;
      let ok = true;
      if (path.startsWith("/api/v1/market/related-stocks?")) {
        ok = !searchError;
        body = ok ? { items: searchItems,
          search_type: "related", expanded_keywords: ["반도체"] }
          : { error: { message: "시세 연결 실패" } };
      } else if (path === "/api/v1/auto-discovery/start") {
        if (startGate) await startGate;
        const settings = JSON.parse(options.body);
        const total = 10000000 * settings.cash_percentage / 100;
        const selected = settings.chosen_symbols;
        const preview = allocation(selected, total);
        Object.assign(state, settings, { selected_symbols: selected, total_investment: String(total),
          allocation: preview });
        body = { config: state, status: { selected_symbols: state.selected_symbols } };
      } else if (path === "/api/v1/auto-discovery") body = state;
      else if (path === "/api/v1/account") body = { mode: "PAPER", trading_enabled: true, available_cash: "10000000" };
      else if (path === "/api/v1/positions") body = { items: [] };
      else if (path === "/api/v1/auto-trade-symbols") body = { items: [
        { symbol: "066570", name: "LG전자" }, { symbol: "083450", name: "GST" }
      ] };
      else if (path === "/api/v1/broker/status") body = { market_data: { enabled: true, credentials_configured: true } };
      else body = {};
      return { ok, status: ok ? 200 : 503, json: async () => JSON.parse(JSON.stringify(body)) };
    }
  });
  find("#auto-discovery-form").requestSubmit = (submitter) => find("#auto-discovery-form").emit("submit", { submitter });
  vm.runInContext(script, context);
  await new Promise(setImmediate);
  calls.length = 0;
  return { find, state, calls, context,
    setSearchError: () => { searchError = true; },
    setSearchItems: (items) => { searchItems = items; },
    delayStart: () => {
      let release;
      startGate = new Promise((resolve) => { release = resolve; });
      return release;
    },
    editKeyword: async (keyword) => {
      find("#auto-discovery-keyword").value = keyword;
      await find("#auto-discovery-form").emit("input");
    },
    chooseSymbol: async (symbol, checked = true) => {
      await find("#auto-discovery-search-results").emit("change", {
        target: { dataset: { selectionSymbol: symbol }, checked }
      });
    }
  };
}

test("Enter and sector search only query candidates, preserving the running sector and allocation", async () => {
  assert.match(html, /id="auto-discovery-search"[^>]*type="submit"[^>]*formnovalidate/);
  assert.match(html, /id="auto-discovery-start"[^>]*type="button"/);
  const { find, state, calls, context, editKeyword } = await fixture();
  const activeSummary = find("#auto-discovery-summary").textContent;
  const activeAllocation = find("#allocation-body").innerHTML;
  await editKeyword("반도체");
  const event = await find("#auto-discovery-form").emit("submit");
  assert.equal(event.prevented, true);
  assert.deepEqual(calls.map(({ path, method }) => ({ path, method })), [{
    path: "/api/v1/market/related-stocks?keyword=%EB%B0%98%EB%8F%84%EC%B2%B4&market=KR", method: undefined
  }]);
  assert.equal(state.keyword, "냉각");
  assert.deepEqual(state.selected_symbols, ["066570", "083450"]);
  assert.equal(find("#auto-discovery-summary").textContent, activeSummary);
  assert.equal(find("#allocation-body").innerHTML, activeAllocation);
  assert.match(find("#auto-discovery-search-summary").textContent, /반도체.*1개 후보/);
  assert.equal(find("#auto-discovery-start").disabled, true);
  await context.refresh();
  assert.equal(find("#auto-discovery-keyword").value, "반도체");
  assert.match(find("#auto-discovery-summary").textContent, /적용 섹터: 냉각/);
  assert.equal(find("#allocation-body").innerHTML, activeAllocation);
});

test("a sector shortcut searches the new sector without applying it to active trading", async () => {
  const { find, state, calls, context } = await fixture();
  const before = JSON.stringify(state);
  await find("sector:의약").emit("click");
  await new Promise(setImmediate);
  assert.equal(find("#auto-discovery-keyword").value, "의약");
  assert.equal(JSON.stringify(state), before);
  assert.ok(calls.some(({ path }) => path.includes(encodeURIComponent("의약"))));
  assert.ok(calls.every(({ method }) => method !== "POST"));
  await context.refresh();
  assert.equal(find("#auto-discovery-keyword").value, "의약");
  assert.match(find("#auto-discovery-summary").textContent, /적용 섹터: 냉각/);
});

test("a search failure preserves active trading and investment allocation", async () => {
  const { find, state, calls, editKeyword, setSearchError } = await fixture();
  const before = JSON.stringify(state);
  const allocation = find("#allocation-body").innerHTML;
  setSearchError();
  await editKeyword("반도체");
  await find("#auto-discovery-form").emit("submit");
  assert.equal(JSON.stringify(state), before);
  assert.equal(find("#allocation-body").innerHTML, allocation);
  assert.match(find("#auto-discovery-search-results").innerHTML, /시세 연결 실패/);
  assert.ok(calls.every(({ method }) => method !== "POST"));
});

test("only explicitly clicking start applies a new sector and investment allocation", async () => {
  const { find, state, calls, editKeyword, chooseSymbol } = await fixture();
  await editKeyword("반도체");
  await find("#auto-discovery-form").emit("submit");
  await chooseSymbol("083450");
  assert.equal(find("#auto-discovery-start").disabled, false);
  await find("#auto-discovery-start").emit("click");
  const writes = calls.filter(({ method }) => method === "POST");
  assert.equal(writes.length, 1);
  assert.equal(writes[0].path, "/api/v1/auto-discovery/start");
  assert.equal(JSON.parse(writes[0].body).keyword, "반도체");
  assert.deepEqual(JSON.parse(writes[0].body).chosen_symbols, ["083450"]);
  assert.equal(state.keyword, "반도체");
  assert.match(find("#auto-discovery-summary").textContent, /적용 섹터: 반도체/);
  assert.match(find("#allocation-body").innerHTML, /1,000,000/);
  assert.doesNotMatch(find("#allocation-body").innerHTML, /LG전자/);
});

test("multiple running sectors and all selected stocks remain visible", async () => {
  const { find, state, context } = await fixture();
  state.active_sectors = [
    { keyword: "우주", symbols: ["066570"], cash_percentage: 40, investment_budget: "4000000" },
    { keyword: "냉각", symbols: ["083450"], cash_percentage: 40, investment_budget: "4000000" }
  ];
  state.selected_symbols = ["066570", "083450"];
  state.allocation.items[0].sectors = ["우주"];
  state.allocation.items[1].sectors = ["냉각"];
  await context.refresh();
  assert.match(find("#auto-discovery-summary").textContent, /운영 섹터: 우주.*냉각/);
  assert.match(find("#auto-discovery-summary").textContent, /선정 종목: LG전자, GST/);
  assert.match(find("#allocation-body").innerHTML, /LG전자.*우주/);
  assert.match(find("#allocation-body").innerHTML, /GST.*냉각/);
  assert.match(find("#allocation-summary").textContent, /섹터별 한도: 우주 40% = ₩4,000,000.*냉각 40% = ₩4,000,000/);
  assert.doesNotMatch(find("#allocation-body").innerHTML, /%/);
});

test("cash allocation uses only 10-percent steps and sends no count, fixed amount or quantity", async () => {
  assert.doesNotMatch(html, /id="auto-discovery-(count|budget|quantity|sizing)"/);
  const options = html.match(/id="auto-discovery-percentage">([\s\S]*?)<\/select>/)[1];
  assert.deepEqual([...options.matchAll(/value="(\d+)"/g)].map((match) => Number(match[1])),
    [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]);
  const { find, calls, editKeyword, chooseSymbol } = await fixture();
  await editKeyword("우주");
  await find("#auto-discovery-form").emit("submit");
  await chooseSymbol("083450");
  find("#auto-discovery-percentage").value = "30";
  await find("#auto-discovery-percentage").emit("change");
  assert.match(find("#auto-discovery-cash-preview").textContent, /3,000,000/);
  await find("#auto-discovery-start").emit("click");
  const body = JSON.parse(calls.find(({ method }) => method === "POST").body);
  assert.deepEqual(body, { keyword: "우주", market: "KR", cash_percentage: 30,
    chosen_symbols: ["083450"] });
});

test("missing OpenAI key does not block manually selected PAPER trading", async () => {
  const { find, state, calls, editKeyword, chooseSymbol } = await fixture();
  assert.equal(state.planner.configured, false);
  assert.equal(find("#auto-discovery-start").disabled, true);
  await editKeyword("의약");
  await find("#auto-discovery-form").emit("submit");
  await chooseSymbol("083450");
  assert.match(find("#auto-discovery-search-summary").textContent, /의약/);
  assert.equal(find("#auto-discovery-start").disabled, false);
  await find("#auto-discovery-start").emit("click");
  assert.equal(calls.filter(({ method }) => method === "POST").length, 1);
});

test("searching another sector during a pending start preserves the new search draft", async () => {
  const { find, state, calls, editKeyword, delayStart, chooseSymbol } = await fixture();
  await editKeyword("냉각");
  await find("#auto-discovery-form").emit("submit");
  await chooseSymbol("083450");
  const release = delayStart();
  const starting = find("#auto-discovery-start").emit("click");
  await editKeyword("반도체");
  await find("#auto-discovery-form").emit("submit");
  release();
  await starting;
  assert.equal(state.keyword, "냉각");
  assert.equal(find("#auto-discovery-keyword").value, "반도체");
  assert.match(find("#auto-discovery-summary").textContent, /적용 섹터: 냉각/);
  assert.equal(calls.filter(({ method }) => method === "POST").length, 1);
});

test("40 percent cash ratio starts with selected stocks and no per-stock percentage gate", async () => {
  const { find, calls, editKeyword, chooseSymbol, setSearchItems } = await fixture();
  setSearchItems([
    { symbol: "066570", name: "LG전자", price: "100000" },
    { symbol: "083450", name: "GST", price: "50000" }
  ]);
  await editKeyword("냉각");
  await find("#auto-discovery-form").emit("submit");
  find("#auto-discovery-percentage").value = "40";
  await find("#auto-discovery-percentage").emit("change");
  await chooseSymbol("066570");
  await chooseSymbol("083450");
  assert.equal(find("#auto-discovery-start").disabled, false);
  assert.match(find("#auto-discovery-selection-summary").textContent, /2개 종목.*40%/);
  await find("#auto-discovery-start").emit("click");
  const body = JSON.parse(calls.find(({ method }) => method === "POST").body);
  assert.equal(body.cash_percentage, 40);
  assert.deepEqual(body.chosen_symbols, ["066570", "083450"]);
  assert.match(find("#allocation-summary").textContent, /4,000,000/);
  await editKeyword("반도체");
  assert.equal(find("#auto-discovery-start").disabled, true);
});

test("investment status is a right-hand scroll panel with navigation links", () => {
  const start = html.indexOf('class="dashboard-panels"');
  const discovery = html.indexOf('id="auto-discovery-card"', start);
  const investment = html.indexOf('id="investment-card"', start);
  const end = html.indexOf("</main>", investment);
  assert.ok(start >= 0 && discovery > start && investment > discovery && end > investment);
  assert.match(html, /href="#investment-card"/);
  assert.match(html, /href="#auto-discovery-card"/);
  assert.match(css, /\.dashboard-panels\s*\{[^}]*display:\s*flex;[^}]*overflow-x:\s*auto;[^}]*scroll-snap-type:\s*x mandatory;/);
  assert.match(css, /\.dashboard-panels > \.card\s*\{[^}]*flex:\s*0 0 100%;[^}]*scroll-snap-align:\s*start;/);
});

test("mouse wheel on panel navigation switches screens without trapping vertical page scrolling", async () => {
  const { find } = await fixture();
  const panels = find("#dashboard-panels");
  const nav = find("#dashboard-panel-nav");
  panels.clientWidth = 1000;
  panels.scrollWidth = 2015;

  const down = await nav.emit("wheel", { deltaY: 120, deltaX: 0 });
  assert.equal(down.prevented, true);
  assert.equal(panels.scrollLeft, 1015);

  const downAtEnd = await nav.emit("wheel", { deltaY: 120, deltaX: 0 });
  assert.equal(downAtEnd.prevented, undefined);

  const up = await nav.emit("wheel", { deltaY: -120, deltaX: 0 });
  assert.equal(up.prevented, true);
  assert.equal(panels.scrollLeft, 0);

  const upAtStart = await nav.emit("wheel", { deltaY: -120, deltaX: 0 });
  assert.equal(upAtStart.prevented, undefined);
  const zoom = await nav.emit("wheel", { deltaY: 120, deltaX: 0, ctrlKey: true });
  assert.equal(zoom.prevented, undefined);
  assert.equal(panels.scrollLeft, 0);
  assert.equal(panels.listeners.wheel, undefined);
});
