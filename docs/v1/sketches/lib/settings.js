// Round 7: Settings, behind the gear. The pages and what they hold are the spec's (feedback.md Quality and Calibrate,
// pipeline.md sections 9 and 10, models.md budgets, integrations.md, data.md configuration); the numbers are made up.
// Loads after paper.js. The app keeps st.set (the page) and st.sub (a page inside it: a topic's editor, Jobs, an
// integration's events, a past Calibrate session); setV(st, phone) renders either. 7b: Overview holds how the pipeline
// is doing and Needs you only what waits on the owner; the same open states show on the paper's page (pgNotes and
// PSTATE in paper.js).

const SET_PAGES = [
  ["Reading", [["overview", "Overview"], ["needs", "Needs you"], ["calibrate", "Calibrate"], ["misses", "Misses"]]],
  ["Following", [["topics", "Topics"], ["taxonomy", "Taxonomy"]]],
  ["System", [["runs", "Runs and cost"], ["integrations", "Integrations"], ["config", "Configuration"]]],
];
const SET_NAME = Object.fromEntries(SET_PAGES.flatMap(([, ps]) => ps));

/* ── sample state ── */
const SETTLED = {}; // a miss ("m0") or an integration's failed event ("i0") -> what was done, shown in place
const CAL = []; // Calibrate answers per screen
const CAL_N = 20;
const RR = new Set(["labels", "members"]); // the scoped re-runs ticked in the offer
let FORM_DIRTY = false; // an editor's form has changed since it opened; until then Save does nothing
const saveBtn = (more) => `<div class="nact"><a class="btn pri${FORM_DIRTY ? "" : " dis"}" data-saved="1"${FORM_DIRTY ? "" : ' title="Nothing changed"'}>Save</a>${more}</div>`;

// what waits on the owner; .done once settled. The same items show on their paper's page through PSTATE.
const NEED = [
  { k: "split", id: "2610.04780", t: "cotm", judge: "central", check: "adjacent" },
  { k: "split", id: "2610.05230", t: "cotm", judge: "adjacent", check: "none" },
  { k: "esc", id: "2610.04990", t: "interp", judge: "related", check: "adjacent" },
  { k: "await", aid: "2610.03312", ti: "On the Limits of Self-Correction (Extended Abstract)", why: "Classification failed 3 runs: the abstract is one sentence and names no method." },
  { k: "ungraded", id: "2610.04411", t: "reason", why: "Grading failed 3 runs: the grader's reply did not parse." },
  { k: "fulltext", id: "2610.03877", why: "The LaTeX source failed to build 3 times (a missing style file). Its Deep card waits on the text." },
  { k: "digest", t: "cotm", day: "Thu 8 Oct", why: "The writer timed out twice." },
];
NEED.forEach((x, i) => { x.i = i; const key = x.k === "await" ? x.aid : x.k === "fulltext" ? x.id : x.id && x.id + "|" + x.t; if (key) PSTATE[key] = x; });
// the paper awaiting classification is in the archive and in no class
ARCH.push({ id: "2610.03312", t: "On the Limits of Self-Correction (Extended Abstract)", au: "Brandt, Ito", y: 2026 });
AX["2610.03312"] = { ver: [["v1", "10 Oct"]], abs: "Self-correction does not help without external feedback." };
// per kind: the group's name, a line on what it means, and whether it asks for the owner's word (yellow) or reports a failure (red)
const NEED_KIND = {
  split: ["Focus splits", "Judge and check disagree. The more inclusive Fit stands until it is labelled.", "ask"],
  esc: ["Area escalations", "The check disagreed with the judge. The less inclusive Fit stands until it is labelled.", "ask"],
  await: ["Awaiting classification", "Classification failed three runs; the paper is in no class.", "fail"],
  ungraded: ["Ungraded", "Grading failed three runs; the paper shows ungraded on its topic.", "fail"],
  fulltext: ["Full text", "A full-text pass failed or is stuck.", "fail"],
  digest: ["Digests", "A digest could not be written.", "fail"],
};
const FITS = ["central", "related", "adjacent", "none", "unsure"];
// [classification, membership, grading, end-to-end, labelled positives]
const RECALL = { cotf: [0.93, 0.97, 0.94, 0.91, 64], cotm: [0.84, 0.95, 0.9, 0.86, 41], circ: [0.78, 0.88, 0.96, 0.85, 37] };
// [precision at relevant, upper n, wanted below, lower n]
const PREC = { cotf: [0.86, 45, 0.04, 48], cotm: [0.71, 38, 0.09, 44], circ: [0.8, 30, 0.13, 41] };
// reactions over 30 days: wrong topic (classification), overrated, underrated (grading), not interesting or already knew, great find
const REACTS = { cotf: [1, 2, 3, 4, 6], cotm: [3, 1, 0, 2, 4], circ: [0, 0, 2, 1, 3], interp: [2, 4, 1, 9, 5], align: [1, 2, 0, 3, 1] };
const NOTOPIC = [
  { ti: "Tokenizer Choice Shapes Arithmetic Generalisation", d: "9 Oct", cls: "Pretraining, data and scaling" },
  { ti: "A Field Guide to Evaluation Awareness in Frontier Models", d: "6 Oct", cls: "Deception, sandbagging and scheming" },
];
// [provider, spent, budget, projected]
const SPEND = [["anthropic", 38.2, 100, 71], ["openrouter", 26.4, 40, 47]];
const KW = { cotf: [["faithful*", 41], ["unfaithful", 17], ["post-hoc rationali*", 6], ["omitted step*", 3]],
  cotm: [["CoT monitor*", 22], ["monitorab*", 9], ["obfuscat* reasoning", 4]], circ: [["activation patching", 31], ["circuit*", 88], ["attribution graph*", 7]] };
const RUBRIC = { cotf: "Central: measures, tests or improves whether a trace states the computation behind the answer. Related: uses faithfulness as a tool or result inside another question. Adjacent: reasoning traces studied for other reasons (length, accuracy).",
  cotm: "Central: reads traces for oversight, or studies whether that keeps working under optimisation pressure. Related: monitoring of actions, or encoded reasoning without a monitor. Adjacent: other uses of reasoning traces in safety." };
const INTEREST = { cotf: "Large-scale measurement over model families; causal checks of stated steps; the link to monitoring.", cotm: "Evasion under RL pressure; monitors that read internals as well as text." };
const SWATCH = ["#c026d3", "#e11d48", "#ea580c", "#ca8a04", "#65a30d", "#0d9488", "#0891b2", "#0284c7", "#4f46e5"];
// the tree from taxonomy.md: [depth, name, threshold, followed topic or null, members, folded at first]
const TREE = [
  [0, "Language models", 0.5, "lm", 5180, true],
  [1, "Pretraining, data and scaling", 0.5, null, 610], [1, "Instruction tuning and preference training", 0.5, null, 420],
  [1, "Reasoning and RL post-training", 0.5, "reason", 980, true], [2, "Chain-of-thought prompting and test-time compute", 0.5, null, 310],
  [2, "Reinforcement learning for reasoning", 0.5, null, 402], [2, "Analyses of reasoning models", 0.5, null, 268],
  [1, "Agents and tool use", 0.5, "agents", 760, true], [2, "Coding and software engineering agents", 0.5, null, 240], [2, "Web, GUI and computer-use agents", 0.5, null, 180],
  [2, "Multi-agent systems", 0.5, null, 150], [2, "Agent benchmarks and evaluation", 0.5, null, 190],
  [1, "Efficiency and architecture", 0.5, "eff", 890, true], [2, "Model architectures", 0.5, null, 260], [2, "Long context and memory", 0.5, null, 170],
  [2, "Inference efficiency", 0.5, null, 280], [2, "Training efficiency and systems", 0.5, null, 180],
  [1, "Retrieval-augmented generation", 0.5, null, 330], [1, "Hallucination, calibration and uncertainty", 0.5, null, 290], [1, "Language model evaluation and benchmarks", 0.5, null, 410],
  [1, "Multimodal language models", 0.5, null, 520], [1, "Multilingual and low-resource languages", 0.5, null, 210], [1, "Language tasks and applications", 0.5, null, 380],
  [0, "Interpretability", 0.5, "interp", 1757], [1, "Features and representations", 0.5, null, 412], [1, "Sparse autoencoders and dictionary learning", 0.5, null, 288],
  [1, "Circuits and causal analysis", 0.45, "circ", 236], [1, "Steering and model editing", 0.5, null, 301], [1, "Automated interpretability", 0.5, null, 97],
  [1, "Interpretability methodology and evaluation", 0.5, null, 145], [1, "Training dynamics and mechanisms of learning", 0.5, null, 120],
  [1, "Chain-of-thought faithfulness", 0.4, "cotf", 158],
  [0, "Alignment and safety", 0.5, "align", 1557], [1, "Safety-motivated alignment training", 0.5, null, 260], [1, "Jailbreaks, red-teaming and robustness", 0.5, null, 410],
  [1, "Prompt injection and model security", 0.5, null, 220], [1, "Deception, sandbagging and scheming", 0.5, null, 133], [1, "Reward hacking and specification gaming", 0.5, null, 98],
  [1, "Scalable oversight and AI control", 0.5, null, 140], [1, "Dangerous capabilities and safety evaluations", 0.5, null, 175], [1, "Chain-of-thought monitoring", 0.4, "cotm", 121],
  [0, "Machine learning beyond language models", 0.5, null, 3940, true], [1, "Learning theory and optimization", 0.5, null, 520], [1, "Reinforcement learning", 0.5, null, 410],
  [1, "Generative models for images, video and audio", 0.5, null, 610], [1, "Computer vision", 0.5, null, 700], [1, "Graph and geometric learning", 0.5, null, 260],
  [1, "Time series and tabular data", 0.5, null, 190], [1, "Probabilistic and causal inference", 0.5, null, 230], [1, "Federated and privacy-preserving learning", 0.5, null, 150],
  [1, "Robotics and embodied AI", 0.5, null, 340], [1, "AI for science and health", 0.5, null, 420], [1, "Classical AI, planning and logic", 0.5, null, 110],
  [0, "AI and society", 0.5, null, 610, true], [1, "Fairness, bias and social impact", 0.5, null, 240], [1, "AI policy, governance and economics", 0.5, null, 180], [1, "Human-AI interaction", 0.5, null, 190],
  [0, "Other", 0.5, null, 640],
];
const MISSES = [
  { ti: "Probing for Unverbalized Plans in Reasoning Traces", t: "cotf", d: "11 Oct", at: "classification",
    trace: [["Classifier", "0.12 under 0.40", "bad"], ["Uncertain band", "under 0.20", "bad"], ["Keywords", "no match", "bad"], ["Judge", "not graded", "off"], ["Check", "not graded", "off"]] },
  { ti: "When Monitors Read Too Little: Truncated Traces and Oversight", t: "cotm", d: "8 Oct", at: "grading",
    trace: [["Classifier", "0.71, a member", "ok"], ["Uncertain band", "—", "off"], ["Keywords", "CoT monitor*", "ok"], ["Judge", "adjacent", "bad"], ["Check", "adjacent", "bad"]] },
  { ti: "Faithfulness of Self-Explanations Under Fine-Tuning", t: "cotf", d: "29 Sep", at: "classification", done: "Threshold lowered to 0.40 (version 3); the replay of 11 Oct admits it.",
    trace: [["Classifier", "0.44 under 0.50", "bad"], ["Uncertain band", "member", "ok"], ["Keywords", "faithful*", "ok"], ["Judge", "related", "ok"], ["Check", "central", "ok"]] },
];
const RUNS = [
  ["Tue 13 Oct", 530, 212, "5", "41 min", 2.1, ""], ["Mon 12 Oct", 2480, 905, "5 + 9 weekly", "2 h 05", 8.9, ""],
  ["Fri 9 Oct", 1210, 466, "5", "58 min", 3.4, ""], ["Thu 8 Oct", 1105, 431, "4 of 5", "1 h 02", 3.1, "Writer timed out on Chain-of-thought monitoring"],
  ["Wed 7 Oct", 1320, 502, "5", "1 h 04", 3.6, ""], ["Tue 6 Oct", 980, 377, "5", "49 min", 2.7, ""],
];
// work outside the nightly run. offered: saved edits whose re-run was put off ("Not now"); need: the Needs you item it mirrors
const JOBS = [
  { k: "Re-run", what: "Circuits and causal analysis · version 3", scope: "Labelled papers and reported misses", n: 37, cost: 0.06, st: "offered", at: "saved 10 Oct, not run" },
  { k: "Deep cards", what: "Asked for · 2 papers", n: 2, done: 1, cost: 0.9, st: "running", at: "started 6 min ago" },
  { k: "Digest", what: "Chain-of-thought monitoring · Thu 8 Oct", st: "failed", at: "8 Oct", why: "The writer timed out twice.", need: 6 },
  { k: "Re-run", what: "Chain-of-thought faithfulness · version 4", scope: "The topic's existing members", n: 412, cost: 0.61, st: "done", at: "12 Oct, 14:20" },
  { k: "Replay", what: "All Focus topics", scope: "Labelled papers and reported misses", n: 142, cost: 0.31, st: "done", at: "11 Oct, 03:10" },
];
const INTEG = [
  { n: "zotero", on: "library \"litwatch\"", d: "Keeps a Zotero item, tags and notes in step for every saved paper.", st: "1 event failed after 3 tries · last success 06:12", fail: "paper.saved 2610.04780 → 412 Precondition Failed: library version changed" },
  { n: "webdav", on: "Nextcloud", d: "Stores the PDF of must-read and saved papers for Zotero's file sync.", st: "Up to date · last file 06:11" },
  { n: "deep", on: "Pi", d: "Requests a Deep card for Focus must reads, Area picks and papers asked for.", st: "Up to date · 2 cards last night, 11 this month" },
  { n: "webhook", d: "POSTs chosen events to a URL; n8n and the Notion export hang off it.", st: "Not loaded. Enable it in litwatch.toml under [integrations.webhook]." },
];
// per integration: [when, event, paper, ok | failed | waiting, what came of it]
const EVENTS = {
  zotero: [["06:12", "paper.saved", "2610.04780", "failed", "412 Precondition Failed: library version changed · 3 tries"], ["06:12", "paper.saved", "2610.04321", "ok", "item created, 2 tags"],
    ["06:11", "paper.labelled", "2610.05112", "ok", "tag updated"], ["Mon 23:58", "paper.saved", "2610.05230", "ok", "item created"],
    ["Mon 23:40", "paper.unsaved", "2610.04990", "ok", "item moved to the bin"], ["Sun 21:02", "card.ready", "2610.04321", "ok", "note attached"]],
  webdav: [["06:11", "pdf.stored", "2610.04321", "ok", "2.1 MB"], ["06:11", "pdf.stored", "2610.04780", "ok", "1.4 MB"], ["Mon 23:58", "pdf.stored", "2610.05230", "ok", "0.9 MB"]],
  deep: [["06:20", "card.requested", "2610.04780", "waiting", "being written · started 6 min ago"], ["06:02", "card.ready", "2610.04321", "ok", "card written from v2"],
    ["Mon 06:10", "card.requested", "2610.04321", "ok", "Focus must read"]],
};
const ROLES = [["classify", "openrouter", "openai/gpt-6-luna-decisions"], ["analyse", "anthropic", "claude-haiku-5-5"], ["judge", "anthropic", "claude-opus-5-5"],
  ["check", "openrouter", "meta/muse-spark-1.3-contributor"], ["area_check", "openrouter", "openai/gpt-6-luna-decisions"], ["field_grade", "anthropic", "claude-haiku-5-5"],
  ["brief", "anthropic", "claude-opus-5-5"], ["writer", "anthropic", "claude-opus-5-5"]];
// past Calibrate sessions: [date, answered, seed]
const SESS = [["6 Oct", 20, "4411"], ["22 Sep", 18, "9020"], ["8 Sep", 20, "1377"]];

/* ── pieces ── */
const usd = (x) => "$" + x.toFixed(2);
const seg = (vals, cur, attr, { auto = null, names = vals } = {}) =>
  `<div class="fitseg">${vals.map((v, i) => `<a class="${cur === v ? "on" : ""}${!cur && auto === v ? " auto" : ""}" ${attr}="${v}">${names[i]}</a>`).join("")}</div>`;
// a share as a bar and its number, coloured by how good it is (hi: higher is better)
function share(x, { hi = true, good = 0.9, ok = 0.8 } = {}) {
  const lvl = hi ? (x >= good ? "ok" : x >= ok ? "warn" : "bad") : (x <= good ? "ok" : x <= ok ? "warn" : "bad");
  return `<span class="sh-b ${lvl}"><i style="width:${Math.round(x * 100)}%"></i></span><span class="sh-n ${lvl}">${Math.round(x * 100)}%</span>`;
}
function spendRows() {
  return SPEND.map(([p, s, b, pr]) => { const over = pr > b, top = Math.max(b, pr);
    return `<div class="spend${over ? " past" : ""}"><span class="pn">${p}</span><span class="sbar"><i class="sp1" style="width:${(s / top) * 100}%"></i><i class="sp2" style="width:${(pr / top) * 100}%"></i><i class="bud" style="left:${(b / top) * 100}%"></i></span>
      <span class="sv2">${usd(s)} of ${usd(b)} · <span class="${over ? "warnt" : "faint"}">projected ${usd(pr)}</span></span></div>`; }).join("");
}
const pageHead = (title, sub, right = "") => `<div class="sethd"><div><div class="h1">${title}</div>${sub ? `<div class="tsub">${sub}</div>` : ""}</div>${right}</div>`;
const fitName = (x) => (x.k === "split" ? x.judge : x.check);

/* ── Needs you, and the same items on a paper's page ── */
const needOpen = () => NEED.filter((x) => !x.done && !(x.id && x.t && LABEL[x.id + "|" + x.t])).length;
// a failed item's actions; each settles the item wherever it shows
function needActs(x, st) {
  const b = (label, res) => `<a class="btn" data-settle="${x.i}|${res}">${label}</a>`;
  if (x.k === "await") return st.nopen === x.i ? classifyForm(x.i) : `<div class="nact">${b("Dismiss", "Dismissed; it stays in the archive.")}<a class="btn" data-nopen="${x.i}">Classify by hand</a>${b("Fix the text", "Opened the text: title and abstract.")}</div>`;
  if (x.k === "ungraded") return `<div class="nact">${b("Grade it now", "Grading again; the result shows on its topic.")}</div>`;
  if (x.k === "fulltext") return `<div class="nact">${b("Try again", "Queued for another full-text pass.")}${b("Use the PDF", "Queued to read the PDF instead.")}</div>`;
  return `<div class="nact">${b("Write it again", "Writing it again; it appears on Thu 8 Oct when done.")}</div>`;
}
function needItem(x, st) {
  const kind = NEED_KIND[x.k][2], pid = x.id || x.aid;
  const ttl = pid ? `<a class="nt" data-paper="${pid}">${x.ti || P[pid].t}</a>${x.aid ? `<span class="mono faint">${x.aid}</span>` : ""}` : `<span class="nt">${T[x.t].n}, ${x.day}</span>`;
  const head = `<div class="nh">${x.t ? badge(x.t) : ""}${ttl}</div>`;
  if (x.k === "split" || x.k === "esc") {
    const k = x.id + "|" + x.t, lab = LABEL[k];
    const body = `<div class="nb">${T[x.t].n} · judge <b>${x.judge}</b> · check <b>${x.check}</b> · <span class="faint">${lab ? `labelled <b>${lab}</b>` : `${fitName(x)} stands`}</span></div>`;
    return `<div class="need ${kind}${lab ? " settled" : ""}">${head}${body}${seg(FITS, lab, "data-lab", { auto: fitName(x) }).replace(/data-lab="/g, `data-lab="${k}|`)}</div>`;
  }
  const body = `<div class="nb">${x.why}</div>`;
  if (x.done) return `<div class="need ${kind} settled">${head}${body}<div class="nres">${x.done}</div></div>`;
  return `<div class="need ${kind}">${head}${body}${needActs(x, st)}</div>`;
}
function classifyForm(i) {
  const pick = ["Analyses of reasoning models", "Chain-of-thought faithfulness", "Interpretability methodology and evaluation", "Other"];
  return `<div class="nform"><div class="cap2">Its classes</div><div class="row">${pick.map((c) => `<a class="fchip2" data-tog>${c}</a>`).join("")}<a class="fchip2 dash">Find a class…</a></div>
    <div class="nact"><a class="btn pri" data-settle="${i}|Classified by hand; grading follows tonight.">Save</a><a class="btn" data-nopen="-1">Cancel</a></div></div>`;
}
function needsV(st) {
  const open = needOpen();
  const groups = Object.keys(NEED_KIND).map((k) => { const items = NEED.filter((x) => x.k === k);
    return items.length ? `<div class="ngrp"><div class="cap">${NEED_KIND[k][0]} · ${items.length}</div><div class="nsub">${NEED_KIND[k][1]}</div>${items.map((x) => needItem(x, st)).join("")}</div>` : ""; }).join("");
  return pageHead("Needs you", open ? `${open} ${open === 1 ? "thing waits" : "things wait"} on the owner. Each also shows on its paper's page.` : "Nothing waits on the owner.") + groups;
}
// on a paper's page, under its head: the paper's open items in the same colours. A split's Fit control is in the rail on
// a desktop and in the ⋯ sheet on a phone.
function pgNotes(pd, st, phone) {
  const items = [openState(pd.id), ...pd.tp.map((t) => openState(pd.id + "|" + t))].filter(Boolean);
  if (!items.length) return "";
  return `<div class="pnotes">${items.map((x) => {
    const ask = NEED_KIND[x.k][2] === "ask", tn = x.t ? T[x.t].n : "";
    const txt = ask ? `<b>${x.k === "split" ? "Split" : "Escalated"} on ${tn}.</b> Judge ${x.judge}, check ${x.check}; ${fitName(x)} stands until it is labelled.`
      : `<b>${x.k === "ungraded" ? `Ungraded on ${tn}.` : x.k === "fulltext" ? "Full text failed." : "Awaiting classification."}</b> ${x.why}`;
    const act = ask ? (phone ? `<div class="nact"><a class="btn" data-ps="open">Label</a></div>` : "") : needActs(x, st);
    return `<div class="need ${ask ? "ask" : "fail"}"><div class="nb">${txt}</div>${act}</div>`;
  }).join("")}</div>`;
}

/* ── Overview: how the pipeline is doing ── */
function overviewV(st, phone) {
  const open = needOpen(), [night, papers, , dg, , cost, fail] = RUNS[0], spent = SPEND.reduce((s, x) => s + x[1], 0), bud = SPEND.reduce((s, x) => s + x[2], 0);
  const over = SPEND.filter((x) => x[3] > x[2]).map((x) => x[0]);
  const tile = (to, cls, big, cap) => `<a class="tile ${cls}" data-sset="${to}"><span class="big">${big}</span><span class="tc">${cap}</span></a>`;
  const tiles = `<div class="tiles">${tile("needs", open ? "ask" : "ok", open || "✓", open ? `${open === 1 ? "thing needs" : "things need"} you` : "Nothing needs you")}
    ${tile("runs", fail ? "fail" : "ok", night.split(" ").slice(1).join(" "), `Last night · ${papers.toLocaleString("en")} papers · ${dg} digests · ${usd(cost)}`)}
    ${tile("runs", over.length ? "warn" : "ok", usd(spent), `of ${usd(bud)} this month${over.length ? ` · ${over.join(", ")} projected over` : ""}`)}
    ${tile("calibrate", "", SESS[0][0], "Last Calibrate session")}</div>`;
  const focus = Object.keys(RECALL), tn = (t) => `<span class="tn">${badge(t)}${phone ? "" : `<span>${T[t].n}</span>`}</span>`;
  const rec = `<div class="mtbl r5"><div class="mh"><span></span><span>Classification</span><span>Membership</span><span>Grading</span><span>End to end</span><span>n</span></div>
    ${focus.map((t) => `<div class="mr">${tn(t)}${RECALL[t].slice(0, 4).map((x) => `<span>${share(x)}</span>`).join("")}<span class="mono faint">${RECALL[t][4]}</span></div>`).join("")}</div>`;
  const prec = `<div class="mtbl r3"><div class="mh"><span></span><span>Relevant or above, owner agrees</span><span>Below, owner wanted it</span></div>
    ${focus.map((t) => { const [p, n, w, m] = PREC[t]; return `<div class="mr">${tn(t)}<span>${share(p)} <span class="faint mono">of ${n}</span></span><span>${share(w, { hi: false, good: 0.05, ok: 0.1 })} <span class="faint mono">of ${m}</span></span></div>`; }).join("")}</div>`;
  const rx = `<div class="mtbl r6"><div class="mh"><span></span><span>Wrong topic</span><span>Overrated</span><span>Underrated</span><span>Not for me</span><span>Great find</span></div>
    ${Object.entries(REACTS).map(([t, v]) => `<div class="mr">${tn(t)}${v.map((x, j) => `<span class="mono ${x ? (j < 2 && x >= 3 ? "warnt" : "") : "faint"}">${x}</span>`).join("")}</div>`).join("")}</div>`;
  const nt = `<div class="list">${NOTOPIC.map((x) => `<div class="prow" style="--rc:var(--acc)"><span class="t">${x.ti}<span class="g">Marked as mattering · in ${x.cls}, which no topic follows</span></span><span class="d">${x.d}</span></div>`).join("")}</div>`;
  return pageHead("Overview", "How the pipeline is doing. Recall from the replay of 11 Oct; precision from Calibrate over 90 days.") + tiles +
    `<div class="cap">Recall per Focus topic</div><div class="nsub">Over the papers labelled central or related. <a class="inl" data-toast="Replay queued; the numbers update when it is done.">Run replay</a></div>${rec}
    <div class="cap">Precision at relevant</div><div class="nsub">From Calibrate's draws over the last 90 days. <a class="inl" data-sset="calibrate">Calibrate</a></div>${prec}
    <div class="cap">Reactions, last 30 days</div><div class="nsub">Wrong topic points at classification, over- and underrated at grading.</div>${rx}
    <div class="cap">Matters, no topic</div><div class="nsub">Papers marked as mattering that no followed topic holds: a missing topic, or a class too narrow.</div>${nt}`;
}

/* ── Calibrate ── */
const calPaper = (j) => paperData(PAPERS[j % PAPERS.length].id);
// a past session as it was answered: the owner's Fit against the pipeline's, per Focus topic the paper was drawn for
function sessAnswers(s) {
  const R = seeded("cal" + s), focus = Object.keys(RECALL);
  return Array.from({ length: SESS[s][1] }, (_, j) => {
    const p = PAPERS[(j * 3 + s * 7) % PAPERS.length], t = p.tp.find((x) => focus.includes(x)) || focus[j % 3];
    const pipe = p.lv[t] ? FIT[p.lv[t]] : "none", i = FITS.indexOf(pipe), r = R();
    return { p, t, pipe, you: r < 0.78 ? pipe : FITS[Math.min(3, Math.max(0, i + (r < 0.9 ? 1 : -1)))], val: r < 0.3 ? "matters" : r < 0.85 ? "fine" : "not for me" };
  });
}
function sessV(s) {
  const [d, n, seed] = SESS[s], A = sessAnswers(s), off = A.filter((a) => a.you !== a.pipe);
  const rows = A.map((a) => `<div class="srow${a.you !== a.pipe ? " off" : ""}" data-paper="${a.p.id}"><span class="t">${a.p.t}</span>${badge(a.t)}
    <span class="fa"><span class="faint">you</span> <b>${a.you}</b></span><span class="fa"><span class="faint">pipeline</span> ${a.pipe}</span><span class="fv faint">${a.val}</span></div>`).join("");
  return { body: pageHead(`Calibrate · ${d}`, `${n} answered · seed ${seed}. The pipeline's Fit shows here: the session is over. ${off.length} answers differ from it; they count against precision on Overview.`) +
    `<div class="list">${rows}</div>`, title: `Calibrate · ${d}` };
}
function calV(st, phone) {
  if (st.cal == null) return { body: pageHead("Calibrate", `One paper per screen, ${CAL_N} in a session: half from papers graded relevant or above and half from below, for each Focus topic. Grades, rarity and topic marks are hidden, so the answers are the owner's own.`) +
    `<a class="btn pri" data-cal="0">Start a session</a><div class="cap" style="margin-top:22px">Past sessions</div><div class="list">
    ${SESS.map(([d, n, s], j) => { const off = sessAnswers(j).filter((a) => a.you !== a.pipe).length;
      return `<div class="srow" data-sub="cs:${j}"><span class="t">${d}</span><span class="fa faint">${n} answered · seed ${s}</span><span class="fa ${off ? "warnt" : "faint"}">${off} differ from the pipeline</span><span class="fv faint">›</span></div>`; }).join("")}</div>` };
  if (st.cal >= CAL_N) {
    const n = CAL.filter(Boolean).length, dis = CAL.filter((a) => a && a.fit && Object.values(a.fit).some((f) => f === "none" || f === "adjacent")).length;
    return { body: pageHead("Session done", `${n} of ${CAL_N} answered; ${dis} where a Focus topic was answered adjacent or none. Precision on Overview now includes them.`) + `<a class="btn pri" data-sset="overview">Back to Overview</a>` };
  }
  const i = st.cal, pd = calPaper(i), a = CAL[i] || {};
  // the strip jumps: a square per paper, answered ones cyan
  const prog = `<div class="calprog">${Array.from({ length: CAL_N }, (_, j) => `<a class="${j === i ? "cur" : CAL[j] ? "done" : ""}" data-cal="${j}" title="Paper ${j + 1}"><i></i></a>`).join("")}</div>`;
  const q = (label, html) => `<div class="calq"><div class="cql">${label}</div><div>${html}</div></div>`;
  const fits = Object.keys(RECALL).map((t) => `<div class="calfit">${badge(t)}<span class="nm">${T[t].n}</span>${seg(FITS, a.fit && a.fit[t], "data-cf", {}).replace(/data-cf="/g, `data-cf="${t}|`)}</div>`).join("");
  const classes = pd.tp.map((t) => T[t].n);
  const clsOff = a.cls === "off" ? `<div class="row" style="margin-top:6px">${classes.map((c) => `<a class="fchip2${(a.wrong || []).includes(c) ? " on strike" : ""}" data-cw="${c}">${c}</a>`).join("")}<a class="fchip2 dash">A class is missing…</a></div>` : "";
  const body = `${phone ? "" : prog}<div class="calp"><div class="h1">${pd.t}</div><div class="muted" style="font-size:12.5px;margin-bottom:12px">${pd.au.slice(0, 3).join(", ")}${pd.au.length > 3 ? ` +${pd.au.length - 3}` : ""} · <span class="mono">${pd.id}</span> · ${pd.d}</div>
    <div class="rgist" style="--rc:var(--fg3)"><div class="rl">Gist</div><p>${pd.g}</p></div>
    <div class="rgist" style="--rc:var(--fg3)"><div class="rl">${pd.brief ? "Brief" : "Abstract"}</div><p>${pd.brief ? pd.brief.text : pd.abs}</p></div>
    <div class="faint" style="font-size:12px;margin-bottom:16px">Classes: ${classes.join(" · ")}</div></div>
    <div class="calqs">${q("Fit, for each Focus topic", fits)}
    ${q("Classes right?", seg(["yes", "off"], a.cls, "data-cc", { names: ["yes", "something is off"] }) + clsOff)}
    ${q("Value, whatever the topic", seg(["matters", "fine", "not for me"], a.val, "data-cv"))}
    ${q("Gist and Brief", seg(["accurate", "off"], a.gb, "data-cg"))}
    ${q("Note", `<div class="tin" contenteditable="plaintext-only" data-ph="Optional"></div>`)}</div>
    ${phone ? "" : `<div class="calnav"><a class="btn" data-cal="${i - 1}"${i ? "" : ' style="visibility:hidden"'}>‹ Back</a><span class="mono faint">${i + 1} of ${CAL_N}</span><a class="btn pri" data-cal="${i + 1}">${i + 1 === CAL_N ? "Done" : "Next ›"}</a></div>`}`;
  // on a phone the number in the bar opens a list of the session's papers to jump to
  const pick = !st.calpick ? "" : `<div class="fsheet enter"><div class="shade2" data-calpick="0"></div><div class="sheetp"><div class="cap2">Go to a paper</div>
    <div class="cpk">${Array.from({ length: CAL_N }, (_, j) => `<a class="${j === i ? "cur" : CAL[j] ? "done" : ""}" data-cal="${j}"><span class="n">${j + 1}</span><span class="t">${calPaper(j).t}</span><span class="ck">${CAL[j] ? "✓" : ""}</span></a>`).join("")}</div>
    <button class="done" data-calpick="0">Close</button></div></div>`;
  const foot = `<div class="pfoot pgf calf"><a data-cal="${i - 1}"${i ? "" : ' class="dim" style="pointer-events:none"'}><small>‹ Back</small></a><a class="lbl" data-calpick="1">${i + 1}/${CAL_N} ▴</a><a class="on" data-cal="${i + 1}"><small>${i + 1 === CAL_N ? "Done" : "Next ›"}</small></a></div>${pick}`;
  return { body: (phone ? prog : "") + body, foot: phone ? foot : "", title: `Calibrate · ${i + 1} of ${CAL_N}` };
}

/* ── Topics, the topic editor and the re-run offer ── */
function topicsSetV() {
  const grp = (dep, label) => `<div class="cap" style="margin-top:16px">${label}</div><div class="list" data-order="${dep}">${TOPICS.filter((t) => t.depth === dep).map((t) =>
    `<div class="trow" data-sub="t:${t.id}" data-tid="${t.id}"><span class="grip" title="Drag to reorder">⋮⋮</span>${badge(t.id)}<span class="nm">${t.n}<span class="g">${PARENT[t.id] ? `${T[PARENT[t.id]].n} › ` : ""}${t.n}</span></span><span class="mono faint">v${(t.n.length % 5) + 1}</span></div>`).join("")}</div>`;
  return pageHead("Topics", "Each follows one class of the taxonomy. The order here is the order of the sidebar and the Digest; drag a row by ⋮⋮ to move it.", `<a class="btn">New topic</a>`) +
    grp("focus", "Focus") + grp("area", "Area") + grp("field", "Field");
}
// after a save, in place of Save and Cancel: what the edit changes, and a scoped re-run with its counts, cost and ceiling
function offer(what, name, ver) {
  const opts = [["labels", "Labelled papers and reported misses", "does the edit help", 64, 0.09], ["members", `The ${what}'s existing members`, "", 412, 0.58],
    ["kw", "Papers matching its keywords", "last 90 days", 41, 0.06], ["range", "A date range", "1 Sep – 13 Oct", 3120, 4.4]];
  const on = opts.filter((o) => RR.has(o[0])), n = on.reduce((s, o) => s + o[3], 0), c = on.reduce((s, o) => s + o[4], 0);
  return `<div class="offer"><div class="oh">Saved as version ${ver}. It applies to papers classified or graded from now on.</div>
    <div class="nsub" style="margin:2px 0 8px">Re-run it now on:</div>
    ${opts.map(([k, l, s, cnt, cost]) => `<a class="orow${RR.has(k) ? " on" : ""}" data-rr="${k}"><span class="box">${RR.has(k) ? "✓" : ""}</span><span class="ol">${l}${s ? ` <span class="faint">· ${s}</span>` : ""}</span><span class="mono">${cnt.toLocaleString("en")}</span><span class="mono faint">≈ ${usd(cost)}</span></a>`).join("")}
    <div class="nact" style="margin-top:10px"><span class="faint" style="font-size:12.5px">Stop at</span><span class="tin sm mono" contenteditable="plaintext-only">$5.00</span><span class="sp"></span>
    <a class="btn" data-notnow="${name} · version ${ver}|${n}|${c}">Not now</a><a class="btn pri" data-rerun="${name} · version ${ver}|${n}|${c}">${n ? `Run · ${n.toLocaleString("en")} papers ≈ ${usd(c)}` : "Run"}</a></div></div>`;
}
const topicVer = (id) => (T[id].n.length % 5) + 1;
function editorV(st, id) {
  const t = T[id], f = (label, html, hint = "") => `<div class="fld"><div class="fl">${label}${hint ? `<span class="faint"> · ${hint}</span>` : ""}</div>${html}</div>`;
  const ta = (txt, rows = 3) => `<div class="tin" contenteditable="plaintext-only" style="min-height:${rows * 1.5}em">${txt}</div>`;
  const kw = (KW[id] || [[t.n.split(" ")[0].toLowerCase() + "*", 120]]).map(([k, n]) => `<span class="kw">${k}<span class="mono faint">${n}</span><a class="x">×</a></span>`).join("") + `<span class="kw add">+ keyword</span>`;
  const body = pageHead(t.n, `Version ${topicVer(id)} · follows ${PARENT[id] ? `${T[PARENT[id]].n} › ` : ""}${t.n}`) +
    `<div class="form">${f("Class", `<div class="tin one">${PARENT[id] ? `${T[PARENT[id]].n} › ` : ""}${t.n} <span class="faint">▾</span></div>`)}
    ${f("Depth", seg(["focus", "area", "field"], t.depth, "data-x"), "Focus is graded by two labs, Area checked, Field graded once")}
    ${f("Subject", ta(SUBJECT[id] || ""), "what the topic is about; the graders read it")}
    ${f("Rubric", ta(RUBRIC[id] || "Central, related, adjacent: what each means here.", 4))}
    ${f("Interests", ta(INTEREST[id] || "", 2), "what makes a paper matter more")}
    ${f("Keywords", `<div class="kws">${kw}</div>`, "matches in the last 90 days")}
    ${f("Look", `<div class="look"><span class="badge ${t.depth} lg" style="--tc:${t.hue}">${t.m}</span><span class="tin sm mono" contenteditable="plaintext-only">${t.m}</span><span class="sws">${SWATCH.map((c) => `<i style="background:${c}"${c === t.hue ? ' class="on"' : ""}></i>`).join("")}</span></div>`)}
    ${f("Status", seg(["active", "paused"], "active", "data-x"), "a paused topic keeps its papers and gets no new ones")}</div>
    <div style="margin-top:16px">${st.saved ? offer("topic", t.n, topicVer(id) + 1) : saveBtn(`<a class="btn" data-back>Cancel</a>`)}</div>`;
  return { body, title: t.n };
}

/* ── Taxonomy ── */
function taxV(st) {
  const folded = new Set(st.fold || TREE.flatMap((r, i) => (r[5] ? [i] : [])));
  let hide = null;
  const rows = TREE.map(([d, n, th, tp, m], i) => {
    if (hide != null && d > hide) return "";
    hide = null;
    const kids = TREE[i + 1] && TREE[i + 1][0] > d, shut = kids && folded.has(i);
    if (shut) hide = d;
    const ed = st.cls !== i ? "" : `<div class="clsed"><div class="form">
      <div class="fld"><div class="fl">Name</div><div class="tin one" contenteditable="plaintext-only">${n}</div></div>
      <div class="fld"><div class="fl">Description<span class="faint"> · says what is out as well as what is in</span></div><div class="tin" contenteditable="plaintext-only" style="min-height:3em">The class's one or two sentences from the taxonomy.</div></div>
      <div class="fld"><div class="fl">Threshold<span class="faint"> · a paper is a member at or above it</span></div><div class="tin one sm mono" contenteditable="plaintext-only">${th.toFixed(2)}</div></div></div>
      <div style="margin-top:12px">${st.saved ? offer("class", n, 2) : saveBtn(`<a class="btn" data-cls="-1">Cancel</a>`)}</div></div>`;
    // the caret folds the branch; the rest of the row opens the class's editor
    return `<div class="crow d${d}${st.cls === i ? " on" : ""}" data-cls="${i}">${kids ? `<a class="car" data-fold="${i}">${shut ? "▸" : "▾"}</a>` : `<span class="car"></span>`}<span class="nm">${n}</span>${tp ? badge(tp) : ""}<span class="mono faint mem">${m.toLocaleString("en")}</span><span class="mono th">${th.toFixed(2)}</span></div>${ed}`;
  }).join("");
  return pageHead("Taxonomy", "Every paper is classified into this tree. A topic follows one class; edits apply forward.", `<span class="nact"><a class="btn" data-toast="taxonomy.yaml downloaded">Export YAML</a><a class="btn">Import YAML</a></span>`) +
    `<div class="list ctree"><div class="crow hd"><span class="car"></span><span class="nm">Class</span><span class="mem">Members</span><span class="th">Threshold</span></div>${rows}</div>`;
}

/* ── Misses ── */
function missesV() {
  return pageHead("Misses", "Papers reported as missed, with what each stage said. A trace shows where the pipeline let the paper go.") +
    MISSES.map((m, i) => `<div class="miss${m.done || SETTLED["m" + i] ? " settled" : ""}"><div class="nh">${badge(m.t)}<span class="nt">${m.ti}</span><span class="faint" style="margin-left:auto;font-size:12px;white-space:nowrap">${m.d}</span></div>
      <div class="nb">Missed at <b class="${m.done ? "" : "badt"}">${m.at}</b></div>
      <div class="trace">${m.trace.map(([s, v, c]) => `<div class="stg ${c}"><span>${s}</span><b>${v}</b></div>`).join("")}</div>
      ${m.done || SETTLED["m" + i] ? `<div class="nres">${m.done || SETTLED["m" + i]}</div>` : `<div class="nact"><a class="btn" data-sub="t:${m.t}">Edit the topic</a><a class="btn" data-sset="taxonomy">Edit the class</a><a class="btn" data-settle="m${i}|Marked resolved.">Resolved</a></div>`}</div>`).join("");
}

/* ── Runs and cost, Jobs ── */
// a failed job that mirrors a Needs you item runs again once that item is settled there
const jobState = (j) => (j.need != null && NEED[j.need].done ? "running" : j.st);
function jobRow(j, i, full) {
  const s = jobState(j), cls = { offered: "ask", running: "run", queued: "run", failed: "fail", done: "settled" }[s];
  const prog = s === "running" && j.n ? `<span class="jbar"><i style="width:${((j.done || 0) / j.n) * 100}%"></i></span>` : "";
  const what = `<span class="jk">${j.k}</span><span class="jw">${j.what}${j.scope ? ` <span class="faint">· ${j.scope}</span>` : ""}</span>`;
  const meta = [j.n ? `${j.done != null && s === "running" ? `${j.done} of ` : ""}${j.n.toLocaleString("en")} ${j.n === 1 ? "paper" : "papers"}` : "", j.cost ? `≈ ${usd(j.cost)}` : "", s === "running" && j.need != null ? "writing again" : j.at].filter(Boolean).join(" · ");
  const acts = !full ? "" : s === "offered" ? `<div class="nact"><a class="btn pri" data-job="${i}|run">Run now</a><a class="btn" data-job="${i}|tonight">With tonight's run</a><a class="btn" data-job="${i}|discard">Discard</a></div>`
    : s === "failed" ? `<div class="nact"><a class="btn" data-job="${i}|run">Try again</a></div>` : "";
  return `<div class="job ${cls}"${full ? "" : ` data-sub="jobs"`}><div class="jh">${what}<span class="jst">${{ offered: "not run", running: "running", queued: "with tonight's run", failed: "failed", done: "done" }[s]}</span></div>
    <div class="nb">${meta}${s === "failed" ? ` · <span class="badt">${j.why}</span>` : ""}</div>${prog}${acts}</div>`;
}
function runsV(phone) {
  const cols = phone ? [0, 1, 5, 6] : [0, 1, 2, 3, 4, 5, 6], hd = ["Night", "Papers", "Graded", "Digests", "Took", "Cost", ""];
  return pageHead("Runs and cost", "Month to date per provider, the projection to the month's end, each night's run and the jobs outside it.") + spendRows() +
    `<div class="cap" style="margin-top:18px">Nights</div><div class="mtbl runs c${cols.length}"><div class="mh">${cols.map((c) => `<span>${hd[c]}</span>`).join("")}</div>
    ${RUNS.map((r) => `<div class="mr${r[6] ? " bad" : ""}">${cols.map((c) => `<span class="${c ? "mono" : ""}">${c === 5 ? usd(r[5]) : c === 1 || c === 2 ? r[c].toLocaleString("en") : c === 6 ? (r[6] ? `<span class="badt">${phone ? "1 failed" : r[6]}</span>` : "") : r[c]}</span>`).join("")}</div>`).join("")}</div>
    <div class="caprow"><div class="cap">Jobs</div><a class="inl" data-sub="jobs">All jobs ›</a></div>${JOBS.slice(0, 3).map((j, i) => jobRow(j, i, false)).join("")}`;
}
function jobsV() {
  const g = (label, pick) => { const js = JOBS.map((j, i) => [j, i]).filter(([j]) => pick(jobState(j)));
    return js.length ? `<div class="cap">${label} · ${js.length}</div>${js.map(([j, i]) => jobRow(j, i, true)).join("")}` : ""; };
  return { body: pageHead("Jobs", "Work outside the nightly run: re-runs after an edit, replays, Deep cards asked for, digests written again. Each stops at its ceiling.") +
    g("Not run", (s) => s === "offered") + g("Running and queued", (s) => s === "running" || s === "queued") + g("Failed", (s) => s === "failed") + g("Done", (s) => s === "done"), title: "Jobs" };
}

/* ── Integrations and their events, Configuration ── */
function integV() {
  return pageHead("Integrations", "Each reacts to the pipeline's events on its own schedule. Which are loaded is set in litwatch.toml.") +
    INTEG.map((x, i) => `<div class="icard${x.fail && !SETTLED["i" + i] ? " fail" : ""}${x.on ? "" : " off"}"><div class="nh"><span class="mono in">${x.n}</span><span class="faint">${x.on ? `on · ${x.on}` : "off"}</span></div>
      <div class="nb">${x.d}</div><div class="ist">${SETTLED["i" + i] || x.st}</div>
      ${x.fail && !SETTLED["i" + i] ? `<div class="ierr mono">${x.fail}</div><div class="nact"><a class="btn" data-toast="Retrying the event.">Retry now</a><a class="btn" data-settle="i${i}|Event skipped · up to date">Skip this event</a><a class="btn" data-sub="ev:${x.n}">All events</a></div>` : x.on ? `<div class="nact"><a class="btn" data-sub="ev:${x.n}">All events</a></div>` : ""}</div>`).join("") +
    `<div class="cap" style="margin-top:18px">Outbox</div><div class="mtbl c4"><div class="mh"><span>Integration</span><span>Waiting</span><span>Failed</span><span>Last success</span></div>
    ${[["zotero", 0, SETTLED.i0 ? 0 : 1, "06:12"], ["webdav", 0, 0, "06:11"], ["deep", 1, 0, "06:02"]].map(([n, w, f, l]) => `<div class="mr" data-sub="ev:${n}"><span class="mono">${n}</span><span class="mono${w ? "" : " faint"}">${w}</span><span class="mono ${f ? "badt" : "faint"}">${f}</span><span class="mono">${l}</span></div>`).join("")}</div>`;
}
function eventsV(st, name) {
  const i = INTEG.findIndex((x) => x.n === name), f = st.evf || "all";
  const state = (e) => (e[3] === "failed" && SETTLED["i" + i] ? "skipped" : e[3]);
  const rows = EVENTS[name].filter((e) => f === "all" || state(e) === f).map((e) => { const s = state(e), p = P[e[2]];
    return `<div class="evrow ${s}"><span class="mono faint">${e[0]}</span><span class="mono">${e[1]}</span><a class="et" data-paper="${e[2]}">${p ? p.t : e[2]}</a><span class="es">${s === "skipped" ? "skipped" : s}</span>
      <span class="ed">${e[4]}</span>${s === "failed" ? `<span class="nact"><a class="btn" data-toast="Retrying the event.">Retry</a><a class="btn" data-settle="i${i}|Event skipped · up to date">Skip</a></span>` : ""}</div>`; }).join("");
  return { body: pageHead(`${name} · events`, `${INTEG[i].d} Newest first, the last 7 days.`) + `<div style="max-width:300px;margin-bottom:12px">${seg(["all", "failed", "waiting"], f, "data-evf")}</div>` +
    (rows ? `<div class="list">${rows}</div>` : `<div class="empty">No ${f} events.</div>`), title: `${name} · events` };
}
function configV() {
  const toml = `[archive]\nurl = "http://archive.lan"\ntoken = "••••••••"\n\n[models.judge]\nprovider = "anthropic"\nmodel = "claude-opus-5-5"\n\n[models.check]\nprovider = "openrouter"\nmodel = "meta/muse-spark-1.3-contributor"\n\n[budgets]\nanthropic = 100\nopenrouter = 40\n\n[integrations.zotero]\nlibrary = "litwatch"\napi_key = "••••••••"`;
  return pageHead("Configuration", "litwatch.toml as loaded; read-only here, the file is the source of truth. Secrets are masked.") +
    `<div class="cap">Models per role</div><div class="mtbl c3">${ROLES.map(([r, p, m]) => `<div class="mr"><span class="mono">${r}</span><span class="faint">${p}</span><span class="mono">${m}</span></div>`).join("")}</div>
    <div class="nres" style="margin:8px 0 18px">judge and check come from different labs (Anthropic, Meta).</div>
    <div class="cap">litwatch.toml</div><pre class="toml">${toml}</pre>`;
}

/* ── the index: the desktop's sidebar, the phone's list ── */
function setHint(id) {
  const open = needOpen();
  return { overview: ["Recall, precision, reactions"], needs: [open ? `${open} waiting` : "All settled", open], calibrate: [`Last session ${SESS[0][0]}`], misses: ["2 open", 2],
    topics: [`${TOPICS.length} followed`], taxonomy: [`${TREE.length} classes`], runs: ["$64.60 of $140 this month"],
    integrations: SETTLED.i0 ? ["Up to date"] : ["zotero: 1 failed", "!"], config: ["litwatch.toml, read-only"] }[id];
}
function setIndex(st, cls) {
  return SET_PAGES.map(([g, ps]) => `<div class="sec">${g}</div>` + ps.map(([id, n]) => { const [h, k] = setHint(id);
    return `<div class="it${st.set === id ? " on" : ""}" data-sset="${id}"><span class="nm">${n}</span>${cls === "list" ? `<span class="hint">${h}</span>` : k ? `<span class="k${k === "!" ? " bad" : " hot"}">${k === "!" ? "1" : k}</span>` : ""}</div>`; }).join("")).join("");
}
// the desktop's sidebar while Settings is open: its index, with a way back to reading at the top
const setSide = (st) => `<div class="wm">${shellWM()}</div><div class="it leave" data-leave>‹ Back to reading</div><div class="sec setttl">⚙ Settings</div>${setIndex(st, "side")}`;
function setPage(st, phone) {
  const sub = st.sub || "";
  if (sub.startsWith("t:")) return editorV(st, sub.slice(2));
  if (sub === "jobs") return jobsV();
  if (sub.startsWith("ev:")) return eventsV(st, sub.slice(3));
  if (sub.startsWith("cs:")) return sessV(+sub.slice(3));
  const s = st.set;
  if (s === "calibrate") return calV(st, phone);
  const body = { overview: () => overviewV(st, phone), needs: () => needsV(st), topics: topicsSetV, taxonomy: () => taxV(st), misses: missesV,
    runs: () => runsV(phone), integrations: integV, config: configV }[s]();
  return { body, title: SET_NAME[s] };
}
function setV(st, phone) {
  if (phone && st.set === "index") return { body: `<div class="setlist">${setIndex(st, "list")}</div>`, title: "Settings" };
  const v = setPage(st, phone), body = `<div class="setpg">${v.body}</div>`;
  return phone ? { ...v, body } : { ...v, body, noBack: !st.sub, side: setSide(st) };
}
