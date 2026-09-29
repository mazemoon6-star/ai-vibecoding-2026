const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const vm = require("node:vm");

const html = readFileSync(join(__dirname, "../auto_trader/static/index.html"), "utf8");
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
    planner: { configured: true, model: "gpt-test" }
  };
  const calls = [];
  let searchError = false;
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
        body = ok ? { items: [{ symbol: "083450", name: "GST", price: "50000" }],
          search_type: "related", expanded_keywords: ["반도체"] }
          : { error: { message: "시세 연결 실패" } };
      } else if (path === "/api/v1/auto-discovery/start") {
        if (startGate) await startGate;
        const settings = JSON.parse(options.body);
        const total = 10000000 * settings.cash_percentage / 100;
        Object.assign(state, settings, { selected_symbols: ["083450"], total_investment: String(total),
          allocation: allocation(["083450"], total) });
        body = { config: state, status: { selected_symbols: state.selected_symbols } };
      } else if (path === "/api/v1/auto-discovery") body = state;
      else if (path === "/api/v1/account") body = { mode: "PAPER", trading_enabled: true, available_cash: "10000000" };
      else if (path === "/api/v1/positions") body = { items: [] };
      else if (path === "/api/v1/auto-trade-symbols") body = { items: [
        { symbol: "066570", name: "LG전자" }, { symbol: "083450", name: "GST" }
      ] };
      else if (path === "/api/v1/broker/status") body = { market_data: { enabled: true, credentials_configured: true } };
      else if (path === "/api/v1/assistant/status") body = { configured: false };
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
    delayStart: () => {
      let release;
      startGate = new Promise((resolve) => { release = resolve; });
      return release;
    },
    editKeyword: async (keyword) => {
      find("#auto-discovery-keyword").value = keyword;
      await find("#auto-discovery-form").emit("input");
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
  const { find, state, calls, editKeyword } = await fixture();
  await editKeyword("반도체");
  await find("#auto-discovery-start").emit("click");
  const writes = calls.filter(({ method }) => method === "POST");
  assert.equal(writes.length, 1);
  assert.equal(writes[0].path, "/api/v1/auto-discovery/start");
  assert.equal(JSON.parse(writes[0].body).keyword, "반도체");
  assert.equal(state.keyword, "반도체");
  assert.match(find("#auto-discovery-summary").textContent, /적용 섹터: 반도체/);
  assert.match(find("#allocation-body").innerHTML, /1,000,000/);
  assert.doesNotMatch(find("#allocation-body").innerHTML, /LG전자/);
});

test("cash allocation uses only 10-percent steps and sends no count, fixed amount or quantity", async () => {
  assert.doesNotMatch(html, /id="auto-discovery-(count|budget|quantity|sizing)"/);
  const options = html.match(/id="auto-discovery-percentage">([\s\S]*?)<\/select>/)[1];
  assert.deepEqual([...options.matchAll(/value="(\d+)"/g)].map((match) => Number(match[1])),
    [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]);
  const { find, calls, editKeyword } = await fixture();
  await editKeyword("우주");
  find("#auto-discovery-percentage").value = "30";
  await find("#auto-discovery-percentage").emit("change");
  assert.match(find("#auto-discovery-cash-preview").textContent, /3,000,000/);
  await find("#auto-discovery-start").emit("click");
  const body = JSON.parse(calls.find(({ method }) => method === "POST").body);
  assert.deepEqual(body, { keyword: "우주", market: "KR", cash_percentage: 30 });
});

test("missing AI configuration disables start but allows sector search", async () => {
  const { find, state, calls, context, editKeyword } = await fixture();
  state.planner.configured = false;
  await context.refresh();
  assert.equal(find("#auto-discovery-start").disabled, true);
  assert.match(find("#auto-discovery-planner-status").textContent, /OPENAI_API_KEY/);
  await editKeyword("의약");
  await find("#auto-discovery-form").emit("submit");
  assert.match(find("#auto-discovery-search-summary").textContent, /의약/);
  assert.ok(calls.every(({ method }) => method !== "POST"));
});

test("searching another sector during a pending start preserves the new search draft", async () => {
  const { find, state, calls, editKeyword, delayStart } = await fixture();
  await editKeyword("냉각");
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
