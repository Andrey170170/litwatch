// Deep read sketches: renders a card (cards/*.js, a block document) in a chosen layout. Throwaway.
(function () {
  const KIND_COLOUR = {
    glance: "var(--acc)", problem: "var(--fg2)", method: "var(--cy)", finding: "var(--blue)", claims: "var(--warn)",
    topic: "var(--tc)", limits: "var(--sig)", related: "var(--purple)", repro: "var(--ok)",
  };
  const VERDICT = {
    holds: ["✓", "holds"], partly: ["◐", "partly"], unshown: ["∅", "not shown"], over: ["✕", "overclaimed"],
  };
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const h = (tag, attrs, html) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") e.className = v;
      else if (k === "style") e.setAttribute("style", v);
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v);
    }
    if (html !== undefined) e.innerHTML = html;
    return e;
  };

  // ── blocks ────────────────────────────────────────────────────────────────
  const BLOCK = {
    p: (b) => h("p", { class: "p" }, b.html),
    h3: (b) => h("div", { class: "h3" }, b.html),
    why: (b) => h("div", { class: "why" }, b.html),
    note: (b) => h("div", { class: "note" }, b.html),
    list: (b) => h("ul", { class: "ul" }, b.items.map((i) => `<li>${i}</li>`).join("")),
    eq: (b) => {
      const e = h("div", { class: "eq" });
      const m = h("div");
      e.append(m);
      if (window.katex) window.katex.render(b.tex, m, { displayMode: true, throwOnError: false });
      else m.textContent = b.tex;
      if (b.note) e.append(h("div", { class: "en" }, b.note));
      return e;
    },
    fig: (b) => {
      const e = h("figure", { class: "fig", style: "margin:0 0 14px" });
      const fi = h("div", { class: "fi" });
      const img = h("img", { src: b.src, alt: b.alt || b.cap, loading: "lazy" });
      fi.append(img);
      fi.addEventListener("click", () => {
        const z = h("div", { class: "zoom" });
        z.append(h("img", { src: b.src }));
        z.addEventListener("click", () => z.remove());
        document.body.append(z);
      });
      e.append(fi, h("figcaption", { class: "fc" }, `<b>${esc(b.n)}</b> · ${b.cap}`));
      return e;
    },
    xp: (b) => {
      const e = h("div", { class: "xp" });
      e.append(h("div", { class: "xq" }, `${b.n ? `<span class="n">${esc(b.n)}</span>` : ""}<span>${b.q}</span>`));
      const r = h("div", { class: "xr" });
      const rows = [["did", "Did", b.did], ["got", "Found", b.got], ["", "Means", b.means], ["not", "Not shown", b.not]];
      for (const [cls, name, v] of rows) if (v) r.append(h("span", { class: cls }, name), h("div", {}, v));
      e.append(r);
      return e;
    },
    table: (b) => {
      const w = h("div", { class: "tw" });
      const t = h("table", { class: "t" });
      t.append(h("thead", {}, `<tr>${b.cols.map((c) => `<th>${c}</th>`).join("")}</tr>`));
      const tb = h("tbody");
      for (const r of b.rows) {
        if (r.grp) { tb.append(h("tr", { class: "grp" }, `<td colspan="${b.cols.length}">${r.grp}</td>`)); continue; }
        const cells = r.cells || r;
        const tr = h("tr", { class: r.sum ? "sum" : "" });
        cells.forEach((c, i) => {
          const cell = typeof c === "object" && c !== null ? c : { v: c };
          const td = h("td", {}, cell.v === null || cell.v === undefined || cell.v === "" ? "<span class='faint'>·</span>" : cell.v);
          if (cell.tip) td.dataset.tip = cell.tip;
          if (b.heat && i > 0 && typeof cell.v === "number" && cell.v > 0) {
            const pct = Math.min(1, cell.v / b.heat.max) * (b.heat.strength || 55);
            td.style.background = `color-mix(in srgb, ${b.heat.colour} ${pct.toFixed(0)}%, transparent)`;
          }
          tr.append(td);
        });
        tb.append(tr);
      }
      t.append(tb);
      // highlight the column under the pointer, so a number reads against its header
      t.addEventListener("mouseover", (ev) => {
        const td = ev.target.closest("td,th");
        t.querySelectorAll(".hl").forEach((x) => x.classList.remove("hl"));
        if (!td || td.cellIndex === 0) return;
        t.querySelectorAll(`tr > :nth-child(${td.cellIndex + 1})`).forEach((x) => x.classList.add("hl"));
      });
      t.addEventListener("mouseleave", () => t.querySelectorAll(".hl").forEach((x) => x.classList.remove("hl")));
      w.append(t);
      const frag = document.createDocumentFragment();
      frag.append(w);
      if (b.cap) frag.append(h("div", { class: "tcap" }, b.cap));
      return frag;
    },
    diagram: (b) => diagram(b),
  };

  // ── diagrams: nodes and edges, each with a short explanation shown on click ─
  function diagram(d) {
    const box = h("div", { class: "dg" });
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${d.w} ${d.h}`);
    const mk = (tag, attrs, text) => {
      const e = document.createElementNS(NS, tag);
      for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
      if (text !== undefined) e.textContent = text;
      return e;
    };
    const defs = mk("defs", {});
    const marker = mk("marker", { id: `ah-${d.id}`, viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" });
    marker.append(mk("path", { d: "M0,0 L10,5 L0,10 z", fill: "currentColor" }));
    defs.append(marker);
    svg.append(defs);
    for (const l of d.lanes || []) svg.append(mk("text", { x: l.x, y: l.y, class: "lane" }, l.text));
    const byId = Object.fromEntries(d.nodes.map((n) => [n.id, n]));
    const tip = h("div", { class: "tipbox" }, `<span class="hint">${d.hint || "Click a box or an arrow for what it does."}</span>`);
    const items = [];
    const select = (el, item) => {
      items.forEach((i) => i.el.classList.remove("on"));
      const related = new Set([item.id]);
      if (item.kind === "node") d.edges.forEach((e) => { if (e.from === item.id || e.to === item.id) { related.add(e.id); related.add(e.from); related.add(e.to); } });
      else { related.add(item.from); related.add(item.to); }
      items.forEach((i) => i.el.classList.toggle("dim", !related.has(i.id)));
      el.classList.add("on");
      tip.innerHTML = `<span class="tt">${item.label || item.tipTitle || ""}</span>${item.tip}${item.where ? `<span class="where">${item.where}</span>` : ""}`;
    };
    const clear = () => {
      items.forEach((i) => i.el.classList.remove("on", "dim"));
      tip.innerHTML = `<span class="hint">${d.hint || "Click a box or an arrow for what it does."}</span>`;
    };
    d.edges.forEach((e, k) => {
      e.id = e.id || `e${k}`;
      const g = mk("g", { class: `eg ${e.style || ""}`, style: "color: var(--fg3)" });
      const a = byId[e.from], b = byId[e.to];
      const pts = e.pts || [[a.x + a.w, a.y + a.h / 2], [b.x, b.y + b.h / 2]];
      const dstr = pts.map((p, i) => `${i ? "L" : "M"}${p[0]},${p[1]}`).join(" ");
      g.append(mk("path", { d: dstr, class: "hit" }));
      g.append(mk("path", { d: dstr, "marker-end": `url(#ah-${d.id})` }));
      if (e.label) g.append(mk("text", { x: e.lx, y: e.ly, "text-anchor": e.anchor || "middle" }, e.label));
      const item = { id: e.id, kind: "edge", from: e.from, to: e.to, el: g, label: e.tipTitle || e.label, tip: e.tip, where: e.where };
      g.addEventListener("click", (ev) => { ev.stopPropagation(); select(g, item); });
      items.push(item);
      svg.append(g);
    });
    for (const n of d.nodes) {
      const g = mk("g", { class: "nd", style: `--nc: ${n.c}` });
      g.append(mk("rect", { x: n.x, y: n.y, width: n.w, height: n.h, rx: 2 }));
      const cx = n.x + n.w / 2;
      g.append(mk("text", { x: cx, y: n.y + (n.sub ? n.h / 2 - 2 : n.h / 2 + 4), "text-anchor": "middle" }, n.label));
      if (n.sub) g.append(mk("text", { x: cx, y: n.y + n.h / 2 + 12, "text-anchor": "middle", class: "s" }, n.sub));
      const item = { id: n.id, kind: "node", el: g, label: n.label, tip: n.tip, where: n.where };
      g.addEventListener("click", (ev) => { ev.stopPropagation(); select(g, item); });
      items.push(item);
      svg.append(g);
    }
    svg.addEventListener("click", clear);
    const sv = h("div", { class: "sv" });
    sv.append(svg);
    box.append(sv, tip);
    const frag = document.createDocumentFragment();
    frag.append(box);
    if (d.cap) frag.append(h("div", { class: "tcap" }, d.cap));
    return frag;
  }

  // ── claims ledger ─────────────────────────────────────────────────────────
  function claimRow(c) {
    const [icon, word] = VERDICT[c.v];
    const row = h("div", { class: "cl", "data-v": c.v });
    const btn = h("button", {}, `<span class="chip${c.v === "holds" ? "" : " solid"}">${icon} ${word}</span><span>${c.text}</span>`);
    btn.addEventListener("click", () => row.classList.toggle("open"));
    row.append(btn, h("div", { class: "ev" }, `${c.ev}${c.src ? ` <span class="src">· ${c.src}</span>` : ""}`));
    return row;
  }
  function claims(list, mode) {
    const box = h("div", { class: "claims" });
    const order = { over: 0, partly: 1, unshown: 2, holds: 3 };
    if (mode === "all") list.forEach((c) => box.append(claimRow(c)));
    else {
      const doubt = list.filter((c) => c.v !== "holds").sort((a, b) => order[a.v] - order[b.v]);
      const held = list.filter((c) => c.v === "holds");
      doubt.forEach((c) => box.append(claimRow(c)));
      if (held.length) {
        const row = h("div", { class: "cl heldrow" });
        const inner = h("div", { style: "display:none" });
        held.forEach((c) => inner.append(claimRow(c)));
        const btn = h("button", {}, `<span class="chip">✓ ${held.length} hold</span><span>${held.map((c) => c.short || c.text).join(" · ")}</span>`);
        btn.addEventListener("click", () => { inner.style.display = inner.style.display === "none" ? "block" : "none"; });
        row.append(btn, inner);
        box.append(row);
      }
    }
    return box;
  }

  // ── the card ──────────────────────────────────────────────────────────────
  // ── the card ──────────────────────────────────────────────────────────────
  /** The card's sections as built elements, each with its id, title and colour; used whole (render) or in a page. */
  function sections(card, opts) {
    const list = [];
    // At a glance: always first, always open
    list.push({ id: "glance", k: "glance", title: "At a glance", open: true, build: (sb) => {
      sb.append(h("p", { class: "claim" }, card.glance.claim));
      sb.append(h("ul", { class: "tl" }, card.glance.points.map((p) => `<li>${p}</li>`).join("")));
      sb.append(h("div", { class: "facts", style: "margin-top:12px" }, card.glance.facts.map(([k, v]) => `<span>${k}</span><div>${v}</div>`).join("")));
    } });
    for (const s of card.sections) list.push({ ...s, build: (sb) => s.blocks.forEach((b) => sb.append(BLOCK[b.t](b))) });
    list.push({ id: "claims", k: "claims", title: "Claims", takeaway: card.claimsTakeaway, open: true, build: (sb) => sb.append(claims(card.claims, opts.claims || "all")) });
    for (const t of card.lenses || []) list.push({ id: `topic-${t.slug}`, k: "topic", tc: t.c, title: t.name, takeaway: t.takeaway, build: (sb) => t.blocks.forEach((b) => sb.append(BLOCK[b.t](b))) });
    list.push({ id: "limits", k: "limits", title: "Limitations", takeaway: card.limitsTakeaway, build: (sb) =>
      sb.append(h("ul", { class: "lim" }, card.limitations.map((l) => `<li><span class="who ${l.who}">${l.who === "card" ? "card" : "paper"}</span><span>${l.text}</span></li>`).join(""))) });
    list.push({ id: "related", k: "related", title: "Related work", takeaway: card.relatedTakeaway, build: (sb) => {
      for (const p of card.related) sb.append(h("div", { class: "pw" }, `<span class="ti">${p.title}</span><span class="rel">${p.rel}</span><span class="id">${p.id || "no arXiv id"}${p.among ? " · in the collection" : ""}</span>`));
    } });
    list.push({ id: "repro", k: "repro", title: "Reproducibility", takeaway: card.reproTakeaway, build: (sb) =>
      sb.append(h("div", { class: "facts" }, card.repro.map(([k, v]) => `<span>${k}</span><div>${v}</div>`).join(""))) });
    return list.map((s) => {
      const colour = s.k === "topic" ? s.tc : KIND_COLOUR[s.k];
      const el = h("section", { class: `sec ${s.k === "glance" ? "glance" : "fold"}${s.open ? " open" : ""}`, id: s.id, "data-k": s.k, style: s.k === "topic" ? `--tc:${s.tc}` : "" });
      const sh = h("div", { class: "sh" }, `<span class="sn">${s.title}</span>${s.takeaway ? `<span class="tk">${s.takeaway}</span>` : ""}`);
      if (s.k !== "glance") sh.addEventListener("click", () => { if (opts.layout === "folded") el.classList.toggle("open"); });
      const sb = h("div", { class: "sb" });
      s.build(sb);
      el.append(sh, sb);
      return { id: s.id, k: s.k, title: s.title, takeaway: s.takeaway, colour, el };
    });
  }

  /** Links to each section that light up while it is in view: for a rail (vertical) or a strip (horizontal). */
  function toc(secs, container, cls) {
    for (const s of secs) {
      const a = h("a", { href: `#${s.id}`, class: cls || "", style: `--sc:${s.colour}` }, `<span class="sw"></span>${s.title}`);
      a.addEventListener("click", () => s.el.classList.add("open"));
      container.append(a);
    }
    const links = [...container.querySelectorAll("a")];
    const io = new IntersectionObserver((entries) => {
      for (const en of entries) if (en.isIntersecting) {
        links.forEach((a) => a.classList.toggle("cur", a.getAttribute("href") === `#${en.target.id}`));
        const cur = container.querySelector(".cur");
        if (cur && container.scrollWidth > container.clientWidth) container.scrollTo({ left: cur.offsetLeft - 16, behavior: "smooth" });
      }
    }, { rootMargin: "-30% 0px -60% 0px" });
    secs.forEach((s) => io.observe(s.el));
  }

  function render(root, card, opts) {
    root.innerHTML = "";
    root.className = `wrap ${opts.layout === "folded" ? "folded" : "open-doc"}`;
    const rail = h("nav", { class: "rail" }, `<div class="rh">Deep read</div>`);
    const main = h("main");
    root.append(rail, main);
    main.append(h("div", { class: "crumb" }, `Paper · arXiv ${esc(card.arxiv)}`));
    main.append(h("h1", { class: "t" }, card.title));
    main.append(h("div", { class: "by" }, `${esc(card.authors)} · ${esc(card.published)}`));
    main.append(h("div", { class: "topics" }, card.topics.map((t) =>
      `<span class="tp" style="--tc:${t.c}"><span class="badge" data-depth="${t.depth}">${t.mono}</span>${t.name} · ${t.grade}</span>`).join("")));
    const strip = h("div", { class: "strip" });
    main.append(strip);
    const secs = sections(card, opts);
    secs.forEach((s) => main.append(s.el));
    main.append(h("div", { class: "foot" }, card.footer));
    toc(secs, rail);
    toc(secs, strip);
  }

  // floating tips for table cells and inline refs: hover on desktop, tap on phone
  let ftip;
  const showTip = (el) => {
    if (!ftip) { ftip = h("div", { class: "ftip" }); document.body.append(ftip); }
    ftip.textContent = el.dataset.tip;
    ftip.style.display = "block";
    const r = el.getBoundingClientRect();
    const x = Math.min(window.innerWidth - 310, Math.max(8, r.left));
    ftip.style.left = `${x}px`;
    ftip.style.top = `${r.bottom + 6}px`;
  };
  const hideTip = () => { if (ftip) ftip.style.display = "none"; };
  document.addEventListener("mouseover", (e) => { const t = e.target.closest("[data-tip]"); if (t) showTip(t); else hideTip(); });
  document.addEventListener("click", (e) => { const t = e.target.closest("[data-tip]"); if (t) showTip(t); else hideTip(); });
  window.addEventListener("scroll", hideTip, { passive: true });

  window.Deep = { render, sections, toc, h };
})();
