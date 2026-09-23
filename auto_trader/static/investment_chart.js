/* SVG investment chart using the PAPER account's existing position amounts. */
class InvestmentChart {
  constructor(root) {
    this.root = root;
    this.svg = root.querySelector(".investment-svg");
    this.segments = root.querySelector(".investment-segments");
    this.stage = root.querySelector(".investment-stage");
    this.tooltip = root.querySelector(".investment-tooltip");
    this.positions = new Map();
    this.colors = new Map();
    this.signature = null;
    this.activeSymbol = null;
    this.anchor = null;
    this.format = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 });
    this.svg.addEventListener("pointerover", (event) => this.select(event));
    this.svg.addEventListener("pointermove", (event) => this.select(event));
    this.svg.addEventListener("pointerleave", () => {
      if (!this.svg.contains(document.activeElement)) this.hide();
    });
    this.svg.addEventListener("focusin", (event) => this.select(event));
    this.svg.addEventListener("focusout", () => this.hide());
    this.svg.addEventListener("click", (event) => this.select(event));
    this.svg.addEventListener("keydown", (event) => {
      if (event.key === "Escape") this.hide();
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        this.select(event);
      }
    });
    document.addEventListener("pointerdown", (event) => {
      if (!this.root.contains(event.target)) this.hide();
    });
    window.addEventListener("resize", () => this.hide());
  }

  color(symbol) {
    if (!this.colors.has(symbol)) {
      const palette = ["#007aff", "#9254f5", "#26bcd5", "#9eafc7", "#ff9f43", "#45b98c", "#df6ca7"];
      const index = this.colors.size;
      this.colors.set(symbol, palette[index] || `hsl(${(index * 137.5) % 360} 60% 52%)`);
    }
    return this.colors.get(symbol);
  }

  node(tag, attributes) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
    return element;
  }

  // Two arcs per edge also handle a single holding's complete 360-degree ring.
  arc(start, end) {
    const point = (radius, angle) => `${200 + radius * Math.cos(angle)},${200 + radius * Math.sin(angle)}`;
    const middle = (start + end) / 2;
    return `M ${point(160, start)} A 160 160 0 0 1 ${point(160, middle)} A 160 160 0 0 1 ${point(160, end)} L ${point(104, end)} A 104 104 0 0 0 ${point(104, middle)} A 104 104 0 0 0 ${point(104, start)} Z`;
  }

  render(positions, names) {
    const items = positions.map((position) => {
      const quantity = Number(position.quantity);
      const average = Number(position.average_price);
      const cost = quantity * average;
      const pnl = position.unrealized_pnl == null ? null : Number(position.unrealized_pnl);
      return { symbol: position.symbol, name: position.name || names.get(position.symbol) || position.symbol,
        quantity, average, cost, pnl: Number.isFinite(pnl) ? pnl : null };
    }).filter((item) => item.quantity > 0 && Number.isFinite(item.cost) && item.cost > 0)
      .sort((a, b) => b.cost - a.cost || a.symbol.localeCompare(b.symbol));
    const total = items.reduce((sum, item) => sum + item.cost, 0);
    this.positions = new Map(items.map((item) => [item.symbol, item]));
    this.root.querySelector("#investment-total").textContent = this.format.format(total);
    this.root.querySelector("#investment-count").textContent = `${items.length}개 종목`;
    this.root.querySelector("#investment-help").textContent = items.length
      ? "평균매수가 × 보유 수량 기준 · 차트에 마우스를 올리거나 눌러 투자 정보를 확인하세요."
      : "보유 종목이 없습니다. 매수가 체결되면 투자 현황이 표시됩니다.";

    const signature = JSON.stringify(items.map(({ symbol, name, cost }) => [symbol, name, cost]));
    if (signature !== this.signature) {
      const focusedSymbol = this.svg.contains(document.activeElement)
        ? document.activeElement.dataset.investmentSymbol : null;
      const fragment = document.createDocumentFragment();
      let start = -Math.PI / 2;
      for (const item of items) {
        const share = item.cost / total;
        const end = start + share * Math.PI * 2;
        const path = this.node("path", { d: this.arc(start, end), fill: this.color(item.symbol),
          class: "investment-slice", "data-investment-symbol": item.symbol, tabindex: "0", role: "button",
          "aria-label": `${item.name} (${item.symbol}) ${(share * 100).toFixed(1)}%, 투자 정보 보기` });
        fragment.append(path);
        if (share >= 0.07) {
          const mid = (start + end) / 2;
          const label = this.node("text", { x: 200 + 132 * Math.cos(mid), y: 200 + 132 * Math.sin(mid),
            class: "investment-share", "text-anchor": "middle", "dominant-baseline": "central", "aria-hidden": "true" });
          label.textContent = `${(share * 100).toFixed(share < 0.1 ? 1 : 0)}%`;
          fragment.append(label);
        }
        start = end;
      }
      this.segments.replaceChildren(fragment);
      this.signature = signature;
      if (focusedSymbol) {
        [...this.segments.querySelectorAll("path")].find((node) => node.dataset.investmentSymbol === focusedSymbol)?.focus();
      }
    }
    if (this.activeSymbol && this.positions.has(this.activeSymbol)) this.show(this.activeSymbol, this.anchor);
    else this.hide();
  }

  select(event) {
    const slice = event.target.closest("[data-investment-symbol]");
    if (!slice) {
      if (event.type === "pointermove" && !this.svg.contains(document.activeElement)) this.hide();
      return;
    }
    const rect = this.stage.getBoundingClientRect();
    const point = Number.isFinite(event.clientX) && event.type !== "keydown" && event.type !== "focusin"
      ? { x: event.clientX - rect.left, y: event.clientY - rect.top } : null;
    this.show(slice.dataset.investmentSymbol, point);
  }

  show(symbol, anchor) {
    const item = this.positions.get(symbol);
    if (!item) return;
    this.activeSymbol = symbol;
    this.anchor = anchor;
    for (const slice of this.segments.querySelectorAll("path")) {
      const active = slice.dataset.investmentSymbol === symbol;
      slice.classList.toggle("is-active", active);
      if (active) slice.setAttribute("aria-describedby", "investment-tooltip");
      else slice.removeAttribute("aria-describedby");
    }
    const set = (selector, value) => { this.tooltip.querySelector(selector).textContent = value; };
    set("[data-investment-name]", item.name);
    set("[data-investment-symbol-label]", item.symbol);
    set("[data-investment-cost]", this.format.format(item.cost));
    set("[data-investment-average]", this.format.format(item.average));
    const pnlText = item.pnl == null ? "-"
      : `${item.pnl > 0 ? "+" : ""}${this.format.format(item.pnl)} (${item.pnl > 0 ? "+" : ""}${(item.pnl / item.cost * 100).toFixed(2)}%)`;
    set("[data-investment-pnl]", pnlText);
    const pnlNode = this.tooltip.querySelector("[data-investment-pnl]");
    pnlNode.classList.toggle("positive", item.pnl > 0);
    pnlNode.classList.toggle("negative", item.pnl < 0);
    set("[data-investment-quantity]", `보유 수량 ${item.quantity.toLocaleString("ko-KR", { maximumFractionDigits: 8 })}주`);
    this.tooltip.querySelector(".investment-dot").style.background = this.color(symbol);
    this.tooltip.hidden = false;
    const maxLeft = Math.max(0, this.stage.clientWidth - this.tooltip.offsetWidth);
    const maxTop = Math.max(0, this.stage.clientHeight - this.tooltip.offsetHeight);
    let left = (anchor?.x ?? this.stage.clientWidth / 2) + 16;
    if (left > maxLeft && anchor) left = anchor.x - this.tooltip.offsetWidth - 16;
    this.tooltip.style.left = `${Math.max(0, Math.min(maxLeft, left))}px`;
    this.tooltip.style.top = `${Math.max(0, Math.min(maxTop, (anchor?.y ?? 0) + 12))}px`;
  }

  hide() {
    this.tooltip.hidden = true;
    this.activeSymbol = null;
    this.anchor = null;
    for (const slice of this.segments.querySelectorAll("path")) {
      slice.classList.remove("is-active");
      slice.removeAttribute("aria-describedby");
    }
  }
}
