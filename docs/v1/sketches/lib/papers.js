// Round 3+ data and fragments: a collection of a few hundred papers over the last eight weeks, each with a level per
// topic (the topic page's rarity), a mixed rarity, facets and a saved flag; a few archive-only hits for search; and the
// pieces the topic page and Papers are built from. Loads after wire.js and digest.js.

const KINDS = ["Method", "Analysis", "Benchmark", "Dataset", "Position", "Survey"];
const SUBJ = {
  cotf: ["Chain-of-Thought Faithfulness", "Post-hoc Rationalisation in Reasoning Traces", "Omitted Steps in Long Chains of Thought", "Counterfactual Tests of Reasoning Traces", "Hint Use in Reasoning Models"],
  cotm: ["Chain-of-Thought Monitors", "Obfuscated Reasoning Under RL Pressure", "Monitorability of Reasoning Traces", "Encoded Reasoning", "Trace-Based Misbehaviour Detection"],
  circ: ["Attribution Graphs", "Transcoder Circuits", "Activation Patching", "Edge Attribution", "Induction Circuits"],
  interp: ["Sparse Autoencoders", "Crosscoders", "Linear Probes", "Steering Vectors", "Feature Splitting", "Logit Lens Variants"],
  align: ["Weak-to-Strong Generalisation", "Reward Hacking", "Sycophancy", "Scalable Oversight", "Refusal Training", "Deception Evaluations"],
  lm: ["Long-Context Pretraining", "Tokenizer-Free Language Models", "Multilingual Scaling", "Instruction Tuning Data", "Data Mixing Laws"],
  reason: ["Process Reward Models", "RL from Verifiable Rewards", "Length Penalties", "Test-Time Compute Scaling", "Self-Verification"],
  agents: ["Web Agents", "Tool-Use Benchmarks", "Multi-Agent Debate", "Computer-Use Agents", "Agentic Coding"],
  eff: ["KV-Cache Eviction", "Speculative Decoding", "Mixture-of-Experts Routing", "4-bit Quantisation", "Linear Attention"],
};
const TAIL = ["at Scale", "Under Distribution Shift", "in Small Transformers", "Across Model Families", "Without Labels", "Revisited", "in Open-Weight Models", "over Long Horizons", "on a Budget"];
const CLAIM = ["What Changes and What Does Not", "A Closer Look", "Lessons from Forty Models", "Evidence from Ablations", "A Benchmark and a Baseline", "When They Fail"];
const FIND = ["the effect shrinks with scale", "most gains vanish against stronger baselines", "the result holds across four model families", "a simple baseline matches the best method", "the effect sits in the middle layers", "two of three claims survive replication"];
const GAIN = ["halves the error of the best prior method", "runs at a tenth of the cost", "needs no labelled data", "holds up under distribution shift"];
const NAMES = ["Okafor", "Lindqvist", "Sato", "Moreau", "Haddad", "Kowalski", "Nguyen", "Ferreira", "Iyer", "Brandt", "Castillo", "Novak", "Achterberg", "Zhou", "Petrov", "Mensah"];
const PARENT = { cotf: "interp", cotm: "align", circ: "interp" };
const SUBJECT = {
  cotf: "Whether a model's chain of thought reflects the computation behind its answer.",
  cotm: "Reading a model's reasoning to catch misbehaviour, and whether that keeps working under pressure.",
  circ: "Finding the mechanisms behind a behaviour and testing them causally.",
  interp: "Understanding what models represent and compute, from features to circuits.",
  align: "Making models do what their developers intend, and checking that they do.",
  lm: "How language models are built, trained and scaled.",
  reason: "Training models to reason, mostly by reinforcement learning after pretraining.",
  agents: "Models that act: tools, browsers, code and other agents.",
  eff: "Making training and inference cheaper: memory, attention, routing, precision.",
};
const LEVEL = ["", "Common", "Uncommon", "Rare", "Epic", "Legendary"];
const GRADE = ["", "none", "adjacent", "relevant", "must read", "must read · landmark"];

const COLL = [];
(function () {
  const days = LOG.filter((d) => !d.none).slice(-40), di = (key) => LOG.findIndex((d) => d.key === key);
  const add = (p) => { COLL.push(p); P[p.id] = p; if (!PAPERS.includes(p)) PAPERS.push(p); };
  const mixed = (lv) => Math.max(1, ...Object.entries(lv).map(([t, l]) => l - (T[t].depth === "field" ? 1 : 0)));
  // the hand-written papers from earlier rounds first, at their own rarity for every topic they carry
  for (const p of PAPERS.slice()) {
    if (!p.tp || !p.d) continue;
    const R = seeded("x" + p.id), key = LOG.find((d) => d.key.endsWith(" " + p.d) && !d.none);
    Object.assign(p, { lv: Object.fromEntries(p.tp.map((t) => [t, p.r])), di: key ? di(key.key) : LOG.length - 1, kind: KINDS[Math.floor(R() * 3)],
      code: R() < 0.6, wts: R() < 0.3, au: `${NAMES[Math.floor(R() * 16)]}, ${NAMES[Math.floor(R() * 16)]} +${2 + Math.floor(R() * 6)}`, sv: p.r >= 3 && p.tp.some((t) => T[t].depth === "focus") });
    add(p);
  }
  const weight = { cotf: 3, cotm: 3, circ: 3, interp: 14, align: 10, lm: 9, reason: 8, agents: 8, eff: 9 }, total = 67;
  // cumulative shares of common, uncommon, rare, epic per depth; the rest is legendary
  const levels = { focus: [0.15, 0.4, 0.75, 0.96], area: [0.38, 0.7, 0.9, 0.98], field: [0.45, 0.77, 0.94, 0.995] };
  const level = (R, d) => 1 + levels[d].filter((c) => R() >= c).length;
  for (let i = 0; i < 420; i++) {
    const R = seeded("p" + i), pick = (a) => a[Math.floor(R() * a.length)];
    let x = R() * total, t0 = TOPICS[0].id;
    for (const t of TOPICS) { x -= weight[t.id]; if (x < 0) { t0 = t.id; break; } }
    const tp = [t0];
    if (PARENT[t0] && R() < 0.8) tp.push(PARENT[t0]);
    else if (R() < 0.2) { const t1 = pick(TOPICS).id; if (!tp.includes(t1)) tp.push(t1); }
    const lv = Object.fromEntries(tp.map((t) => { const u = R(); return [t, level(() => u, T[t].depth)]; }));
    const s = pick(SUBJ[t0]), kind = pick(KINDS), form = i % 3;
    const title = form === 0 ? `${s} ${pick(TAIL)}` : form === 1 ? `${s}: ${pick(CLAIM)}` : `Rethinking ${s} ${pick(TAIL.filter((x) => x !== "Revisited"))}`;
    const g = kind === "Method" ? `Proposes a method for ${s.toLowerCase()} that ${pick(GAIN)}.`
      : kind === "Benchmark" || kind === "Dataset" ? `Introduces a ${kind.toLowerCase()} of ${2 + Math.floor(R() * 40)}00 items for ${s.toLowerCase()}; ${pick(FIND)}.`
      : kind === "Survey" ? `Surveys ${40 + Math.floor(R() * 160)} papers on ${s.toLowerCase()} and sorts their claims by the evidence behind them.`
      : kind === "Position" ? `Argues that work on ${s.toLowerCase()} measures the wrong thing, and proposes what to measure instead.`
      : `Studies ${s.toLowerCase()} in ${3 + Math.floor(R() * 20)} models and finds that ${pick(FIND)}.`;
    const day = pick(days), mo = { Aug: "08", Sep: "09", Oct: "10" }[day.key.split(" ")[2]] || "07";
    add({ id: `26${mo}.${String(5100 + i * 7).padStart(5, "0")}`, t: title, g, tp, lv, r: mixed(lv), d: day.key.slice(4), di: di(day.key), kind,
      code: R() < 0.45, wts: R() < 0.2, au: `${pick(NAMES)}, ${pick(NAMES)} +${1 + Math.floor(R() * 8)}`,
      sv: tp.some((t) => T[t].depth === "focus" && lv[t] >= 3) || R() < 0.04 });
  }
})();

// archive-only hits: in the archive, never processed, so no rarity
const ARCH = [
  { id: "1706.03762", t: "Attention Is All You Need", au: "Vaswani, Shazeer +6", y: 2017 },
  { id: "2209.11895", t: "In-context Learning and Induction Heads", au: "Olsson, Elhage +24", y: 2022 },
  { id: "2301.05217", t: "Progress Measures for Grokking via Mechanistic Interpretability", au: "Nanda, Chan +3", y: 2023 },
  { id: "2310.01405", t: "Representation Engineering: A Top-Down Approach to AI Transparency", au: "Zou, Phan +20", y: 2023 },
  { id: "2202.05262", t: "Locating and Editing Factual Associations in GPT", au: "Meng, Bau +2", y: 2022 },
  { id: "2211.00593", t: "Interpretability in the Wild: a Circuit for Indirect Object Identification in GPT-2 Small", au: "Wang, Variengien +3", y: 2022 },
  { id: "2305.04388", t: "Language Models Don't Always Say What They Think: Unfaithful Explanations in Chain-of-Thought Prompting", au: "Turpin, Michael +2", y: 2023 },
  { id: "2307.13702", t: "Measuring Faithfulness in Chain-of-Thought Reasoning", au: "Lanham, Chen +28", y: 2023 },
];

/* ── fragments ── */
const lvOf = (p, topic) => (topic ? p.lv[topic] || 1 : p.r);
// does a paper match a query: every word in the title or the Gist
const matches = (p, q) => !q || q.toLowerCase().split(/\s+/).filter(Boolean).every((w) => (p.t + " " + (p.g || "")).toLowerCase().includes(w));

// the list a topic page or Papers shows, filtered and sorted by the state
function listFor(st, topic) {
  let L = COLL.filter((p) => (!topic || p.lv[topic]) && matches(p, st.q) && (!st.rset.length || st.rset.includes(lvOf(p, topic)))
    && (!st.saved || p.sv) && (!st.code || p.code) && (!st.tf.length || st.tf.some((t) => p.lv[t])));
  const byNew = (a, b) => b.di - a.di || lvOf(b, topic) - lvOf(a, topic);
  const byRare = (a, b) => lvOf(b, topic) - lvOf(a, topic) || b.di - a.di;
  const byMatch = (a, b) => (b.t.toLowerCase().includes(st.q.toLowerCase()) - a.t.toLowerCase().includes(st.q.toLowerCase())) || byRare(a, b);
  return L.sort(st.q && st.sort === "match" ? byMatch : st.sort === "rare" ? byRare : byNew);
}

// rarity as a bar of five segments, widths by count of what the other filters leave; a segment filters to that
// level (several may be on)
function rarityBar(st, topic) {
  const n = [0, 0, 0, 0, 0, 0];
  for (const p of listFor({ ...st, rset: [] }, topic)) n[lvOf(p, topic)]++;
  const tot = n.reduce((a, b) => a + b, 0) || 1;
  return `<div class="rbar">${[5, 4, 3, 2, 1].map((l) => `<a class="rseg${st.rset.includes(l) ? " on" : ""}${st.rset.length && !st.rset.includes(l) ? " off" : ""}" data-rl="${l}" style="--rc:var(--r${l});flex:${Math.max(n[l] / tot, 0.07)}" title="${LEVEL[l]}${topic ? ` (${GRADE[l]})` : ""}: ${n[l]}"><b>${n[l]}</b><span>${LEVEL[l]}</span></a>`).join("")}</div>`;
}
const chip = (st, k, label) => `<a class="fchip2${st[k] ? " on" : ""}" data-chip="${k}">${label}</a>`;
const sortSeg = (st) => `<span class="sortseg">${[["new", "Newest"], ["rare", "Rarest"], ...(st.q ? [["match", "Best match"]] : [])].map(([k, n]) => `<a class="${st.sort === k ? "on" : ""}" data-sort="${k}">${n}</a>`).join("")}</span>`;
const topicToggles = (st) => `<div class="tfs">${TOPICS.map((t) => `<a class="tf${st.tf.includes(t.id) ? " on" : ""}${st.tf.length && !st.tf.includes(t.id) ? " off" : ""}" data-tf="${t.id}" title="${t.n}">${badge(t.id)}</a>`).join("")}</div>`;
const facets = (p) => `<span class="fx">${p.code ? '<i title="Code released">⟨/⟩</i>' : ""}${p.wts ? '<i title="Weights released">◆</i>' : ""}</span>`;
const star = (p) => `<a class="sv${p.sv ? " on" : ""}" data-save="${p.id}" title="${p.sv ? "Saved · tap to unsave" : "Save"}">${p.sv ? "★" : "☆"}</a>`;

// A: a table, one line a paper
function tableRows(list, topic, { badges = !topic } = {}) {
  return `<div class="tbl"><div class="th"><span></span><span>Title</span>${badges ? "<span>Topics</span>" : ""}<span>Kind</span><span></span><span>Date</span><span></span></div>` +
    list.map((p) => `<div class="tr${badges ? " wb" : ""}" data-paper="${p.id}" style="--rc:var(--r${lvOf(p, topic)})" title="${p.g}"><span class="stripe"></span><span class="ti">${p.t}</span>${badges ? `<span class="bd">${p.tp.map((t) => badge(t)).join("")}</span>` : ""}<span class="kd">${p.kind}</span>${facets(p)}<span class="dt">${p.d}</span>${star(p)}</div>`).join("") + `</div>`;
}
// B: a reading list, title and Gist, grouped by week
function readingRows(list, topic, { badges = !topic, group = true } = {}) {
  let h = "", wk = null;
  for (const p of list) {
    const mon = weekOf(p.di);
    if (group && mon !== wk) { wk = mon; h += `<div class="wkh">${mon === THIS_WEEK ? "This week" : "Week of " + mon.slice(4)}</div>`; }
    h += `<div class="br" data-paper="${p.id}" style="--rc:var(--r${lvOf(p, topic)})"><div class="l1"><span class="ti">${p.t}</span>${star(p)}</div>
      <div class="g">${p.g}</div><div class="m">${badges ? p.tp.map((t) => badge(t)).join("") : ""}<span>${p.kind}</span>${facets(p)}<span class="sp"></span><span class="dt">${p.d}</span></div></div>`;
  }
  return `<div class="blist">${h}</div>`;
}
const weekOf = (i) => { while (i > 0 && LOG[i].dow !== 1) i--; return LOG[i].key; };
// C: shelves by rarity, the top ones open with Gists, the rest folded to a count until opened
function shelfRows(list, topic, st, { badges = !topic } = {}) {
  const band = (ls, l) => list.filter((p) => ls.includes(lvOf(p, topic)));
  const bands = [[[5, 4], topic ? "Must read" : "Epic and legendary"], [[3], topic ? "Relevant" : "Rare"], [[2], topic ? "Adjacent" : "Uncommon"], [[1], topic ? "In the topic, not graded up" : "Common"]];
  return bands.map(([ls, name], bi) => {
    const L = band(ls), open = bi < 2 || st.open.includes(bi);
    if (!L.length) return "";
    const head = `<div class="shh${open ? "" : " fold"}" ${bi >= 2 ? `data-unfold="${bi}"` : ""} style="--rc:var(--r${ls[0]})"><span class="sq"></span>${name}<span class="k">${L.length}</span>${bi >= 2 ? `<span class="tg">${open ? "fold" : "show"}</span>` : ""}</div>`;
    if (!open) return head;
    return head + (bi === 0 ? readingRows(L, topic, { badges, group: false }) : tableRows(L.slice(0, bi === 1 ? 60 : st.n), topic, { badges }).replace('<div class="th">', '<div class="th hide">'));
  }).join("");
}

// a topic's recent digests as a strip of squares, oldest left; daily for Focus and Area, weekly for Field
function digestStrip(id) {
  const daily = T[id].depth !== "field";
  const items = daily ? LOG.filter((d) => !d.none).slice(-15).map((d) => [`${d.key}|${id}`, d.marks[id], d.key])
    : LOG.filter((d) => d.week).slice(-8).map((d) => [`${d.key}|${id}|w`, d.week.marks[id], "Week " + d.week.range]);
  return `<div class="dstrip"><span class="lab">${daily ? "Daily digests" : "Weekly digests"}</span>${items.map(([k, m, n]) => `<span class="dsq${m.r === 0 ? " h" : m.r === -1 ? " x" : ""}" style="${rcs(m.r)}" data-cell="${k}" title="${n} · ${m.c}"></span>`).join("")}</div>`;
}
