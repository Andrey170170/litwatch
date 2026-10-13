// Shared sample data and screen fragments for the sketch rounds. Each round page composes these
// into its own variants; the fragments only render, a variant decides what clicking does.

const TOPICS = [
  { id: "cotf", n: "Chain-of-thought faithfulness", m: "CF", depth: "focus", hue: "#c026d3" },
  { id: "cotm", n: "Chain-of-thought monitoring", m: "CM", depth: "focus", hue: "#e11d48" },
  { id: "circ", n: "Circuits and causal analysis", m: "CI", depth: "focus", hue: "#0891b2" },
  { id: "interp", n: "Interpretability", m: "IN", depth: "area", hue: "#4f46e5" },
  { id: "align", n: "Alignment and safety", m: "AS", depth: "area", hue: "#ea580c" },
  { id: "lm", n: "Language models", m: "LM", depth: "field", hue: "#0284c7" },
  { id: "reason", n: "Reasoning and RL post-training", m: "RE", depth: "field", hue: "#65a30d" },
  { id: "agents", n: "Agents and tool use", m: "AG", depth: "field", hue: "#ca8a04" },
  { id: "eff", n: "Efficiency and architecture", m: "EF", depth: "field", hue: "#0d9488" },
];
const T = Object.fromEntries(TOPICS.map((t) => [t.id, t]));

const PAPERS = [
  { id: "2610.04321", r: 5, t: "Faithfulness Gaps in Long Chains of Thought: Measuring Omitted Computation at Scale", g: "Across four model families, 31% of correct answers rest on computations the trace never mentions, growing with trace length.", d: "13 Oct", tp: ["cotf", "interp"] },
  { id: "2610.04780", r: 4, t: "Steganographic Drift: How RL Against a Monitor Reshapes Reasoning Traces", g: "Training against a CoT monitor teaches models to encode the forbidden step in innocuous wording within 2k RL steps.", d: "13 Oct", tp: ["cotm", "align"] },
  { id: "2610.03877", r: 3, t: "Attribution Graphs for Arithmetic Circuits in Instruction-Tuned Models", g: "Transcoder attribution graphs recover the same carry circuit in three models, confirmed edge by edge with patching.", d: "13 Oct", tp: ["circ", "interp"] },
  { id: "2610.05112", r: 3, t: "Can Models Report Their Own Decision Rules? Causal Tests of LLM Introspection", g: "Self-reports of decision rules match causal tests 62% of the time, falling with rule complexity.", d: "13 Oct", tp: ["cotf"] },
  { id: "2610.04990", r: 2, t: "Sparse Crosscoders Recover Shared Features Across Model Families", g: "Crosscoders trained on paired activations find 40% of features shared across Llama and Qwen at matched scale.", d: "13 Oct", tp: ["interp"] },
  { id: "2610.05230", r: 2, t: "Monitor Jailbreaking Without Encoded Reasoning", g: "Plain-language persuasion of the monitor model evades CoT monitoring as well as steganography does.", d: "13 Oct", tp: ["cotm"] },
  { id: "2610.04411", r: 2, t: "GRPO With Length Penalties Does Not Shorten Thinking at Test Time", g: "Length penalties shorten training traces but not test-time thinking once the budget is lifted.", d: "13 Oct", tp: ["reason", "lm"] },
  { id: "2610.04002", r: 1, t: "KV-Cache Eviction by Attention Entropy for Long-Context Serving", g: "Evicting low-entropy heads' cache first keeps 98% of quality at a quarter of the memory.", d: "13 Oct", tp: ["eff"] },
  { id: "2610.03550", r: 1, t: "A Benchmark for Multi-Step Web Agents Under Distribution Shift", g: "1,200 web tasks whose sites change between train and test; agents lose 30 points on average.", d: "13 Oct", tp: ["agents"] },
];

const DIGEST = {
  cotf: { c: "2 new · 1 must read", p: "Two papers today: a large-scale measurement of omitted computation in long traces, and causal tests of whether models can state their own decision rules.", picks: ["2610.04321", "2610.05112"], more: 0, best: 5 },
  cotm: { c: "2 new · 1 must read", p: "RL against a monitor produces encoded reasoning quickly; a second paper shows plain persuasion of the monitor works as well.", picks: ["2610.04780", "2610.05230"], more: 0, best: 4 },
  circ: { c: "1 new", p: "One arithmetic-circuit paper confirms attribution graphs against patching across three models.", picks: ["2610.03877"], more: 0, best: 3 },
  interp: { c: "11 new · 1 pick", p: "A quieter day: crosscoder work on shared features, two SAE evaluation papers, and the CoT and circuits papers above.", picks: ["2610.04990"], more: 10, best: 3 },
  align: { c: "Nothing new", quiet: true, best: 1 },
};
const DAYS = [
  { d: "Tue 13 Oct", marks: [["cotf", "2 new · 1 must read", 5], ["cotm", "2 new · 1 must read", 4], ["circ", "1 new", 3], ["interp", "11 new · 1 pick", 3], ["align", "Nothing new", 1]] },
  { d: "Mon 12 Oct", week: true, marks: [["cotf", "4 new", 3], ["cotm", "Nothing new", 1], ["circ", "3 new · 1 must read", 4], ["interp", "19 new · 2 picks", 4], ["align", "7 new · 1 pick", 3], ["lm", "Week · 3 picks", 4]] },
  { d: "Sun 11 Oct", none: true }, { d: "Sat 10 Oct", none: true },
  { d: "Fri 9 Oct", marks: [["cotf", "1 new", 3], ["cotm", "1 new · 1 must read", 5], ["circ", "2 new", 2], ["interp", "14 new · 1 pick", 3], ["align", "5 new · 1 pick", 3]] },
];
const P = Object.fromEntries(PAPERS.map((p) => [p.id, p]));

const badge = (id, cls = "") => { const t = T[id]; return `<span class="badge ${t.depth} ${cls}" style="--tc:${t.hue}" title="${t.n}">${t.m}</span>`; };
const rc = (r) => `--rc:var(--r${r})`;

function prow(p, { gist = false, badges = true, sel = false } = {}) {
  return `<div class="prow${sel ? " on" : ""}" data-paper="${p.id}" style="${rc(p.r)}"><span class="t">${p.t}${gist ? `<span class="g">${p.g}</span>` : ""}</span>${badges ? `<span class="bs">${p.tp.map((x) => badge(x)).join("")}</span>` : ""}<span class="d">${p.d}</span></div>`;
}
function dblock(id, { picks = 3 } = {}) {
  const x = DIGEST[id], t = T[id];
  if (x.quiet) return `<div class="dblock quiet" data-topic="${id}"><div class="hd">${badge(id)}<span class="n">${t.n}</span><span class="c">Nothing new</span></div></div>`;
  const rows = x.picks.slice(0, picks).map((pid) => prow(P[pid], { badges: false })).join("");
  return `<div class="dblock" data-topic="${id}"><div class="hd">${badge(id)}<span class="n">${t.n}</span><span class="c">${x.c}</span></div><p class="p">${x.p}</p><div class="list">${rows}</div>${x.more ? `<div class="more">+${x.more} more</div>` : ""}</div>`;
}
function dayView(day = "Tue 13 Oct") {
  return `<div class="daystrip"><button class="ib">‹</button><span class="day">${day}</span><button class="ib">›</button><span class="sp"></span><a class="chipbtn" data-go="history">History</a></div>` +
    Object.keys(DIGEST).map((id) => dblock(id)).join("");
}
function historyView(selDay) {
  return DAYS.map((day) => `<div class="dayh" data-day="${day.d}">${day.d}${day.week ? ' <span class="faint">· weekly digests</span>' : ""}</div>` +
    (day.none ? `<div class="faint" style="font-size:13px">No papers released.</div>` :
      `<div class="list" data-day="${day.d}" style="${day.d === selDay ? "outline:2px solid var(--r3);outline-offset:-1px" : ""}">${day.marks.map(([id, c, r]) => `<div class="drow" style="${rc(r)}">${badge(id)}<span>${T[id].n}</span><span class="c">${c}</span></div>`).join("")}</div>`)).join("");
}
function topicsView() {
  const grp = (dep, label) => `<div class="cap" style="margin-top:14px">${label}</div><div class="list">${TOPICS.filter((t) => t.depth === dep).map((t) => `<div class="tcard" data-topic="${t.id}">${badge(t.id, "lg")}<div><div class="n">${t.n}</div><div class="s">${DIGEST[t.id] ? DIGEST[t.id].c : "Weekly · 3 picks on Monday"}</div></div></div>`).join("")}</div>`;
  return grp("focus", "Focus") + grp("area", "Area") + grp("field", "Field");
}
function topicPage(id) {
  const t = T[id], rows = PAPERS.filter((p) => p.tp.includes(id));
  return `<div style="display:flex;gap:10px;align-items:center;margin-bottom:6px">${badge(id, "lg")}<div class="h1" style="margin:0">${t.n}</div></div>
    <div class="muted" style="font-size:13px;margin-bottom:12px">${t.depth[0].toUpperCase() + t.depth.slice(1)} · ${rows.length} papers this week · <a style="text-decoration:underline">Read today's digest</a></div>
    <div class="search"><div class="in">Search in ${t.n}</div><span class="fchip">Sort: newest</span></div>
    <div class="list">${rows.map((p) => prow(p, { badges: false, gist: true })).join("")}${PAPERS.slice(5).map((p) => prow({ ...p, r: 1 }, { badges: false, gist: true })).join("")}</div>`;
}
function papersView(sel) {
  return `<div class="search"><div class="in">Search 3.1M papers</div></div>
    <div class="fchips"><span class="fchip">In collection ▾</span><span class="fchip">Any topic ▾</span><span class="fchip">Rare and above ▾</span><span class="fchip">This week ▾</span></div>
    <div class="list">${PAPERS.map((p) => prow(p, { sel: p.id === sel })).join("")}</div>`;
}
function paperView(id) {
  const p = P[id] || PAPERS[0];
  return `<div class="h1">${p.t}</div>
    <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px"><span class="fchip" style="${rc(p.r)};border-color:var(--rc);color:var(--rc)">${["", "Common", "Uncommon", "Rare", "Epic", "Legendary"][p.r]}</span>${p.tp.map((x) => badge(x)).join("")}</div>
    <div class="muted" style="font-size:12.5px;margin-bottom:10px">Okafor, Lindqvist, Sato +3 · <span class="mono">${p.id}</span> · ${p.d} 2026</div>
    <div style="display:flex;gap:6px;margin-bottom:14px"><span class="btn">Save</span><span class="btn">arXiv</span><span class="btn">PDF</span><span class="btn">BibTeX</span><span style="flex:1"></span><span class="btn">👍</span><span class="btn">👎</span></div>
    <div class="cap">Brief</div><p class="prose">${p.g} The authors instrument four open model families and compare each trace with the computation recovered from activations. The main limit is the recovery method, validated on toy settings only.</p>
    <div class="cap">Deep card</div><p class="prose faint">The full Deep card goes here: key claim, setup, limitations, prior work as linked rows, and the paper among the collection.</p>`;
}

// light/dark toggle shared by every round, remembered across pages
(function () {
  const saved = localStorage.getItem("lw-theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.documentElement.dataset.theme = saved;
  addEventListener("DOMContentLoaded", () => {
    const b = document.getElementById("theme");
    if (!b) return;
    const label = () => (b.textContent = document.documentElement.dataset.theme === "dark" ? "Light" : "Dark");
    label();
    b.onclick = () => { const n = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = n; localStorage.setItem("lw-theme", n); label(); };
  });
})();
