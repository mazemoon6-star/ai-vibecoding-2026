// Dependency-free checks for portfolio calculations and tooltip interaction.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tag = "div") {
    this.tag = tag;
    this.attributes = {};
    this.dataset = {};
    this.children = [];
    this.style = {};
    this.textContent = "";
    this.hidden = false;
    this.listeners = {};
    this.clientWidth = 900;
    this.clientHeight = 400;
    this.offsetWidth = 300;
    this.offsetHeight = 210;
    this.classes = new Set();
    this.classList = {
      toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name),
      remove: (name) => this.classes.delete(name),
    };
  }
  setAttribute(key, value) {
    this.attributes[key] = String(value);
    if (key === "data-investment-symbol") this.dataset.investmentSymbol = value;
  }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  append(child) { this.children.push(child); }
  replaceChildren(fragment) { this.children = fragment.children; }
  querySelectorAll(tag) { return this.children.filter((child) => child.tag === tag); }
  contains(node) { return this.children.includes(node) || this.children.some((child) => child.contains(node)); }
  closest() { return this.dataset.investmentSymbol ? this : null; }
  getBoundingClientRect() { return { left: 0, top: 0 }; }
}

function fixture() {
  const refs = new Map();
  const find = (selector) => {
    if (!refs.has(selector)) refs.set(selector, new Element());
    return refs.get(selector);
  };
  const root = new Element();
  root.querySelector = find;
  find(".investment-tooltip").querySelector = find;
  find(".investment-svg").append(find(".investment-segments"));
  const document = { activeElement: null, addEventListener() {},
    createElementNS: (_, tag) => new Element(tag), createDocumentFragment: () => new Element("fragment") };
  const context = vm.createContext({ document, window: { addEventListener() {} }, Intl });
  vm.runInContext(readFileSync(join(__dirname, "../auto_trader/static/investment_chart.js"), "utf8") + "\nthis.Chart = InvestmentChart;", context);
  return { chart: new context.Chart(root), find, document };
}

const position = (symbol, quantity, average, pnl = null, currency = "KRW") => ({ symbol, quantity: String(quantity),
  average_price: String(average), unrealized_pnl: pnl == null ? null : String(pnl), currency });

test("empty and sold positions show an empty ring and no tooltip", () => {
  const { chart, find } = fixture();
  chart.render([position("SOLD", 0, 120)], new Map());
  assert.equal(find("#investment-total").textContent, "₩0");
  assert.equal(find(".investment-segments").children.length, 0);
  assert.equal(find(".investment-tooltip").hidden, true);
});

test("allocation uses remaining cost basis; one holding creates a complete ring", () => {
  const { chart, find } = fixture();
  chart.render([position("AAA", 2, 120000, 12000)], new Map([["AAA", "Company A"]]));
  assert.equal(find("#investment-total").textContent, "₩240,000");
  const paths = find(".investment-segments").querySelectorAll("path");
  assert.equal(paths.length, 1);
  assert.equal((paths[0].attributes.d.match(/ A /g) || []).length, 4);
  assert.ok(!/NaN|Infinity/.test(paths[0].attributes.d));
  assert.match(paths[0].attributes["aria-label"], /100.0%/);
  chart.select({ target: paths[0], type: "pointerover", clientX: 350, clientY: 80 });
  assert.equal(find("[data-investment-cost]").textContent, "₩240,000");
  assert.equal(find("[data-investment-average]").textContent, "₩120,000");
  assert.equal(find("[data-investment-pnl]").textContent, "+₩12,000 (+5.00%)");
});

test("multiple stocks keep individual values and safe text names", () => {
  const { chart, find } = fixture();
  chart.render([position("AAA", 2, 100, -20), position("BBB", 6, 100)], new Map([["AAA", "<script>alert(1)</script>"]]));
  assert.equal(find("#investment-total").textContent, "₩800");
  const path = find(".investment-segments").querySelectorAll("path").find((node) => node.dataset.investmentSymbol === "AAA");
  assert.match(path.attributes["aria-label"], /25.0%/);
  chart.select({ target: path, type: "focusin" });
  assert.equal(find("[data-investment-name]").textContent, "<script>alert(1)</script>");
  assert.equal(find("[data-investment-pnl]").textContent, "-₩20 (-10.00%)");
  assert.equal(find("[data-investment-pnl]").classes.has("negative"), true);
  chart.show("BBB", null);
  assert.equal(find("[data-investment-pnl]").textContent, "-");
  assert.equal(find("[data-investment-pnl]").classes.has("negative"), false);
});

test("refresh updates the open tooltip without replacing unchanged slices", () => {
  const { chart, find } = fixture();
  chart.render([position("AAA", 2, 100, 10)], new Map());
  const originalPath = find(".investment-segments").querySelectorAll("path")[0];
  chart.select({ target: originalPath, type: "pointerover", clientX: 350, clientY: 80 });
  chart.render([position("AAA", 2, 100, -30)], new Map());
  assert.equal(find(".investment-segments").querySelectorAll("path")[0], originalPath);
  assert.equal(find("[data-investment-pnl]").textContent, "-₩30 (-15.00%)");
  assert.equal(find(".investment-tooltip").hidden, false);
  chart.render([position("AAA", 0, 100, 0)], new Map());
  assert.equal(find(".investment-tooltip").hidden, true);
});

test("mixed KRW and USD holdings use FX for allocation and keep native tooltip amounts", () => {
  const { chart, find } = fixture();
  chart.render([
    position("KR", 1, 1000, 100, "KRW"),
    position("US", 1, 10, -0.001, "USD")
  ], new Map(), new Map([["KRW", 1], ["USD", 100]]));
  assert.equal(find("#investment-total").textContent, "₩2,000");
  const path = find(".investment-segments").querySelectorAll("path").find((node) => node.dataset.investmentSymbol === "US");
  assert.match(path.attributes["aria-label"], /50.0%/);
  chart.select({ target: path, type: "focusin" });
  assert.equal(find("[data-investment-cost]").textContent, "$10.00");
  assert.equal(find("[data-investment-pnl]").textContent, "$0.00 (-0.01%)");
});

test("tap and keyboard tooltip fit a narrow chart and Escape dismisses it", () => {
  const { chart, find } = fixture();
  const stage = find(".investment-stage");
  stage.clientWidth = 260;
  stage.clientHeight = 260;
  find(".investment-tooltip").offsetWidth = 260;
  chart.render([position("AAA", 1, 100, 0)], new Map());
  const path = find(".investment-segments").querySelectorAll("path")[0];
  chart.select({ target: path, type: "click", clientX: 250, clientY: 250 });
  assert.equal(find(".investment-tooltip").style.left, "0px");
  assert.equal(find(".investment-tooltip").style.top, "50px");
  find(".investment-svg").listeners.keydown({ key: "Escape" });
  assert.equal(find(".investment-tooltip").hidden, true);
  find(".investment-svg").listeners.keydown({ key: "Enter", type: "keydown", target: path, preventDefault() {} });
  assert.equal(find(".investment-tooltip").hidden, false);
});
