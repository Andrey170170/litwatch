// Round 7: Settings, behind the gear. The pages and what they hold are the spec's (feedback.md Quality and Calibrate,
// pipeline.md sections 9 and 10, models.md budgets, integrations.md, data.md configuration); the numbers are made up.
// Loads after paper.js. The app keeps st.set (the page) and st.sub (a page inside it: a topic's editor, a Calibrate
// screen); setV(st, phone, look) renders either.

const SET_PAGES = [
  ["Reading", [["quality", "Quality"], ["calibrate", "Calibrate"], ["misses", "Misses"]]],
  ["Following", [["topics", "Topics"], ["taxonomy", "Taxonomy"]]],
  ["System", [["runs", "Runs and cost"], ["integrations", "Integrations"], ["config", "Configuration"]]],
];
const SET_NAME = Object.fromEntries(SET_PAGES.flatMap(([, ps]) => ps));
// a line under each page's name on the phone's list, and the count beside it in the desktop index
const SET_HINT = { quality: [], calibrate: ["Last session 6 Oct"], misses: ["2 open", 2], topics: ["9 followed"],
  taxonomy: ["56 classes"], runs: ["$64.60 of $140 this month"], integrations: ["zotero: 1 failed", "!"], config: ["litwatch.toml, read-only"] };

/* ── sample state ── */
const SETTLED = {}; // need index -> what was done, shown in place
const CAL = []; // Calibrate answers per screen
const CAL_N = 20;
const RR = new Set(["labels", "members"]); // the scoped re-runs ticked in the offer

const NEED = [
  { k: "split", id: "2610.04780", t: "cotm", judge: "central", check: "adjacent" },
  { k: "split", id: "2610.05230", t: "cotm", judge: "related", check: "none" },
  { k: "esc", id: "2610.04990", t: "interp", judge: "related", check: "adjacent" },
  { k: "await", ti: "On the Limits of Self-Correction (Extended Abstract)", aid: "2610.03312", why: "Classification failed 3 runs: the abstract is one sentence and names no method." },
  { k: "ungraded", id: "2610.04411", t: "reason", why: "Grading failed 3 runs: the grader's reply did not parse." },
  { k: "fulltext", id: "2610.03877", why: "The LaTeX source failed to build 3 times (a missing style file). The Brief and Deep card wait on it." },
  { k: "digest", t: "cotm", day: "Thu 8 Oct", why: "The writer timed out twice." },
];
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
// the tree as the Taxonomy page draws it: [depth, name, threshold, followed topic or null, members]; other branches folded
const TREE = [
  [0, "Language models", 0.5, "lm", "14 classes", true],
  [0, "Interpretability", 0.5, "interp", 2310], [1, "Features and representations", 0.5, null, 412], [1, "Sparse autoencoders and dictionary learning", 0.5, null, 288],
  [1, "Circuits and causal analysis", 0.45, "circ", 236], [1, "Steering and model editing", 0.5, null, 301], [1, "Automated interpretability", 0.5, null, 97],
  [1, "Interpretability methodology and evaluation", 0.5, null, 145], [1, "Training dynamics and mechanisms of learning", 0.5, null, 120],
  [1, "Chain-of-thought faithfulness", 0.4, "cotf", 158],
  [0, "Alignment and safety", 0.5, "align", 1840], [1, "Safety-motivated alignment training", 0.5, null, 260], [1, "Jailbreaks, red-teaming and robustness", 0.5, null, 410],
  [1, "Deception, sandbagging and scheming", 0.5, null, 133], [1, "Reward hacking and specification gaming", 0.5, null, 98], [1, "Chain-of-thought monitoring", 0.4, "cotm", 121],
  [0, "Machine learning beyond language models", 0.5, null, "12 classes", true], [0, "AI and society", 0.5, null, "4 classes", true], [0, "Other", 0.5, null, 640],
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
const INTEG = [
  { n: "zotero", on: "library \"litwatch\"", d: "Keeps a Zotero item, tags and notes in step for every saved paper.", st: "1 event failed after 3 tries · last success 06:12", fail: "paper.saved 2610.04780 → 412 Precondition Failed: library version changed" },
  { n: "webdav", on: "Nextcloud", d: "Stores the PDF of must-read and saved papers for Zotero's file sync.", st: "Up to date · last file 06:11" },
  { n: "deep", on: "Pi", d: "Requests a Deep card for Focus must reads, Area picks and papers asked for.", st: "Up to date · 2 cards last night, 11 this month" },
  { n: "webhook", d: "POSTs chosen events to a URL; n8n and the Notion export hang off it.", st: "Not loaded. Enable it in litwatch.toml under [integrations.webhook]." },
];
const ROLES = [["classify", "openrouter", "openai/gpt-6-luna-decisions"], ["analyse", "anthropic", "claude-haiku-5-5"], ["judge", "anthropic", "claude-opus-5-5"],
  ["check", "openrouter", "meta/muse-spark-1.3-contributor"], ["area_check", "openrouter", "openai/gpt-6-luna-decisions"], ["field_grade", "anthropic", "claude-haiku-5-5"],
  ["brief", "anthropic", "claude-opus-5-5"], ["writer", "anthropic", "claude-opus-5-5"]];

/* ── pieces ── */
const usd = (x) => "$" + x.toFixed(2);
const seg = (vals, cur, attr, { auto = null, names = vals } = {}) =>
  `<div class="fitseg">${vals.map((v, i) => `<a class="${cur === v ? "on" : ""}${!cur && auto === v ? " auto" : ""}" ${attr}="${v}">${names[i]}</a>`).join("")}</div>`;
// a share as a number or a bar, coloured by how good it is (hi: higher is better)
function share(x, look, { hi = true, good = 0.9, ok = 0.8 } = {}) {
  const lvl = hi ? (x >= good ? "ok" : x >= ok ? "warn" : "bad") : (x <= good ? "ok" : x <= ok ? "warn" : "bad");
  const n = `<span class="sh-n ${lvl}">${Math.round(x * 100)}%</span>`;
  return look === "bars" ? `<span class="sh-b ${lvl}"><i style="width:${Math.round(x * 100)}%"></i></span>${n}` : n;
}
function spendRows(phone) {
  return SPEND.map(([p, s, b, pr]) => { const over = pr > b;
    return `<div class="spend${over ? " past" : ""}"><span class="pn">${p}</span><span class="sbar"><i class="sp1" style="width:${(s / Math.max(b, pr)) * 100}%"></i><i class="sp2" style="width:${(pr / Math.max(b, pr)) * 100}%"></i><i class="bud" style="left:${(b / Math.max(b, pr)) * 100}%"></i></span>
      <span class="sv2">${usd(s)} of ${usd(b)}${phone ? "<br>" : " · "}<span class="${over ? "warnt" : "faint"}">projected ${usd(pr)}</span></span></div>`; }).join("");
}
const pageHead = (title, sub, right = "") => `<div class="sethd"><div><div class="h1">${title}</div>${sub ? `<div class="tsub">${sub}</div>` : ""}</div>${right}</div>`;

/* ── Quality ── */
const needOpen = () => NEED.filter((x, i) => !(SETTLED[i] || (x.id && x.t && LABEL[x.id + "|" + x.t]))).length;
function needItem(x, i, st) {
  const done = SETTLED[i], kind = NEED_KIND[x.k][2];
  const ttl = x.id ? `<a class="nt" data-paper="${x.id}">${P[x.id].t}</a>` : x.ti ? `<span class="nt">${x.ti} <span class="mono faint">${x.aid}</span></span>` : `<span class="nt">${T[x.t].n}, ${x.day}</span>`;
  const head = `<div class="nh">${x.t ? badge(x.t) : ""}${ttl}</div>`;
  let body = "", act = "";
  if (x.k === "split" || x.k === "esc") {
    const k = x.id + "|" + x.t, lab = LABEL[k], stands = x.k === "split" ? x.judge : x.check;
    body = `<div class="nb">${T[x.t].n} · judge <b>${x.judge}</b> · check <b>${x.check}</b> · <span class="faint">${lab ? `labelled <b>${lab}</b>` : `${stands} stands`}</span></div>`;
    act = seg(FITS, lab, "data-lab", { auto: stands }).replace(/data-lab="/g, `data-lab="${k}|`);
    return `<div class="need ${kind}${lab ? " settled" : ""}">${head}${body}${act}</div>`;
  }
  body = `<div class="nb">${x.why}</div>`;
  if (done) return `<div class="need ${kind} settled">${head}${body}<div class="nres">${done}</div></div>`;
  const b = (label, res) => `<a class="btn" data-settle="${i}|${res}">${label}</a>`;
  if (x.k === "await") act = st.nopen === i ? classifyForm(i) : `<div class="nact">${b("Dismiss", "Dismissed; it stays in the archive.")}<a class="btn" data-nopen="${i}">Classify by hand</a>${b("Fix the text", "Opened the text: title and abstract.")}</div>`;
  if (x.k === "ungraded") act = `<div class="nact">${b("Grade it now", "Grading again; the result shows on its topic.")}</div>`;
  if (x.k === "fulltext") act = `<div class="nact">${b("Try again", "Queued for another full-text pass.")}${b("Use the PDF", "Queued to read the PDF instead.")}</div>`;
  if (x.k === "digest") act = `<div class="nact">${b("Write it again", "Writing it again; it appears on Thu 8 Oct when done.")}</div>`;
  return `<div class="need ${kind}">${head}${body}${act}</div>`;
}
function classifyForm(i) {
  const pick = ["Analyses of reasoning models", "Chain-of-thought faithfulness", "Interpretability methodology and evaluation", "Other"];
  return `<div class="nform"><div class="cap2">Its classes</div><div class="row">${pick.map((c) => `<a class="fchip2" data-tog>${c}</a>`).join("")}<a class="fchip2 dash">Find a class…</a></div>
    <div class="nact"><a class="btn pri" data-settle="${i}|Classified by hand; grading follows tonight.">Save</a><a class="btn" data-nopen="-1">Cancel</a></div></div>`;
}
function qualityV(st, phone, look) {
  const open = needOpen();
  const groups = Object.keys(NEED_KIND).map((k) => { const items = NEED.map((x, i) => [x, i]).filter(([x]) => x.k === k);
    return items.length ? `<div class="ngrp"><div class="cap">${NEED_KIND[k][0]} · ${items.length}</div><div class="nsub">${NEED_KIND[k][1]}</div>${items.map(([x, i]) => needItem(x, i, st)).join("")}</div>` : ""; }).join("");
  const focus = Object.keys(RECALL);
  const rec = `<div class="mtbl r5"><div class="mh"><span></span><span>Classification</span><span>Membership</span><span>Grading</span><span>End to end</span><span>n</span></div>
    ${focus.map((t) => `<div class="mr"><span class="tn">${badge(t)}${phone ? "" : `<span>${T[t].n}</span>`}</span>${RECALL[t].slice(0, 4).map((x) => `<span>${share(x, look)}</span>`).join("")}<span class="mono faint">${RECALL[t][4]}</span></div>`).join("")}</div>`;
  const prec = `<div class="mtbl r3"><div class="mh"><span></span><span>Relevant or above, owner agrees</span><span>Below, owner wanted it</span></div>
    ${focus.map((t) => { const [p, n, w, m] = PREC[t]; return `<div class="mr"><span class="tn">${badge(t)}${phone ? "" : `<span>${T[t].n}</span>`}</span><span>${share(p, look)} <span class="faint mono">of ${n}</span></span><span>${share(w, look, { hi: false, good: 0.05, ok: 0.1 })} <span class="faint mono">of ${m}</span></span></div>`; }).join("")}</div>`;
  const rx = `<div class="mtbl r6"><div class="mh"><span></span><span>Wrong topic</span><span>Overrated</span><span>Underrated</span><span>Not for me</span><span>Great find</span></div>
    ${Object.entries(REACTS).map(([t, v]) => `<div class="mr"><span class="tn">${badge(t)}${phone ? "" : `<span>${T[t].n}</span>`}</span>${v.map((x, j) => `<span class="mono ${x ? (j < 2 && x >= 3 ? "warnt" : "") : "faint"}">${x}</span>`).join("")}</div>`).join("")}</div>`;
  const nt = `<div class="list">${NOTOPIC.map((x) => `<div class="prow" style="--rc:var(--acc)"><span class="t">${x.ti}<span class="g">Marked as mattering · in ${x.cls}, which no topic follows</span></span><span class="d">${x.d}</span></div>`).join("")}</div>`;
  const cal = `<a class="btn pri" data-sset="calibrate">Calibrate · ${CAL_N} papers</a>`;
  return pageHead("Quality", open ? `${open} ${open === 1 ? "thing needs" : "things need"} you. Recall from the replay of 11 Oct; precision from Calibrate over 90 days.` : "Nothing needs you.", phone ? "" : cal) +
    (phone ? `<div style="margin:-4px 0 14px">${cal}</div>` : "") +
    `<div class="h2">Needs you</div>${groups}
    <div class="h2">How it is doing</div>
    <div class="cap">Recall per Focus topic</div><div class="nsub">Over the papers labelled central or related. <a class="inl" data-toast="Replay queued; the numbers update when it is done.">Run replay</a></div>${rec}
    <div class="cap">Precision at relevant</div><div class="nsub">From Calibrate's draws over the last 90 days.</div>${prec}
    <div class="cap">Reactions, last 30 days</div><div class="nsub">Wrong topic points at classification, over- and underrated at grading.</div>${rx}
    <div class="cap">Matters, no topic</div><div class="nsub">Papers marked as mattering that no followed topic holds: a missing topic, or a class too narrow.</div>${nt}
    <div class="cap" style="margin-top:18px">Cost this month</div>${spendRows(phone)}`;
}

/* ── Calibrate ── */
function calV(st, phone) {
  if (st.cal == null) return { body: pageHead("Calibrate", `One paper per screen, ${CAL_N} in a session: half from papers graded relevant or above and half from below, for each Focus topic. Grades, rarity and topic marks are hidden, so the answers are the owner's own.`) +
    `<a class="btn pri" data-cal="0">Start a session</a><div class="cap" style="margin-top:22px">Past sessions</div><div class="list">
    ${[["6 Oct", 20, "4411"], ["22 Sep", 18, "9020"], ["8 Sep", 20, "1377"]].map(([d, n, s]) => `<div class="drow quiet" style="--rc:var(--line)"><span>${d}</span><span class="c">${n} answered · seed ${s}</span></div>`).join("")}</div>` };
  if (st.cal >= CAL_N) {
    const n = CAL.filter(Boolean).length, dis = CAL.filter((a) => a && a.fit && Object.values(a.fit).some((f) => f === "none" || f === "adjacent")).length;
    return { body: pageHead("Session done", `${n} of ${CAL_N} answered; ${dis} where a Focus topic was answered adjacent or none. Precision on Quality now includes them.`) + `<a class="btn pri" data-sset="quality">Back to Quality</a>` };
  }
  const i = st.cal, pd = paperData(PAPERS[i % PAPERS.length].id), a = CAL[i] || {};
  const prog = `<div class="calprog">${Array.from({ length: CAL_N }, (_, j) => `<i class="${j === i ? "cur" : CAL[j] ? "done" : ""}"></i>`).join("")}</div>`;
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
  const foot = `<div class="pfoot pgf calf"><a data-cal="${i - 1}"${i ? "" : ' class="dim" style="pointer-events:none"'}><small>‹ Back</small></a><span class="lbl">${i + 1}/${CAL_N}</span><a class="on" data-cal="${i + 1}"><small>${i + 1 === CAL_N ? "Done" : "Next ›"}</small></a></div>`;
  return { body: (phone ? prog : "") + body, foot: phone ? foot : "", title: `Calibrate · ${i + 1} of ${CAL_N}` };
}

/* ── Topics and the topic editor ── */
function topicsSetV(phone) {
  const grp = (dep, label) => `<div class="cap" style="margin-top:16px">${label}</div><div class="list">${TOPICS.filter((t) => t.depth === dep).map((t) =>
    `<div class="trow" data-sub="t:${t.id}"><span class="grip" title="Drag to reorder">⋮⋮</span>${badge(t.id)}<span class="nm">${t.n}<span class="g">${PARENT[t.id] ? `${T[PARENT[t.id]].n} › ` : ""}${t.n}</span></span><span class="mono faint">v${(t.n.length % 5) + 1}</span></div>`).join("")}</div>`;
  return pageHead("Topics", "Each follows one class of the taxonomy. The order here is the order of the sidebar and the Digest.", `<a class="btn">New topic</a>`) +
    grp("focus", "Focus") + grp("area", "Area") + grp("field", "Field");
}
function offer(st, what) {
  const opts = [["labels", "Labelled papers and reported misses", "does the edit help", 64, 0.09], ["members", `The ${what}'s existing members`, "", 412, 0.58],
    ["kw", "Papers matching its keywords", "last 90 days", 41, 0.06], ["range", "A date range", "1 Sep – 13 Oct", 3120, 4.4]];
  const n = opts.filter((o) => RR.has(o[0])).reduce((s, o) => s + o[3], 0), c = opts.filter((o) => RR.has(o[0])).reduce((s, o) => s + o[4], 0);
  return `<div class="offer"><div class="oh">Saved as a new version. It applies to papers classified or graded from now on.</div>
    <div class="nsub" style="margin:2px 0 8px">Re-run it now on:</div>
    ${opts.map(([k, l, s, cnt, cost]) => `<a class="orow${RR.has(k) ? " on" : ""}" data-rr="${k}"><span class="box">${RR.has(k) ? "✓" : ""}</span><span class="ol">${l}${s ? ` <span class="faint">· ${s}</span>` : ""}</span><span class="mono">${cnt.toLocaleString("en")}</span><span class="mono faint">≈ ${usd(cost)}</span></a>`).join("")}
    <div class="nact" style="margin-top:10px"><span class="faint" style="font-size:12.5px">Stop at</span><span class="tin sm mono" contenteditable="plaintext-only">$5.00</span><span class="sp"></span>
    <a class="btn" data-saved="0">Not now</a><a class="btn pri" data-toast="Re-run queued: ${n.toLocaleString("en")} papers, stops at $5.00. Progress in Runs and cost." data-saved="0">${n ? `Run · ${n.toLocaleString("en")} papers ≈ ${usd(c)}` : "Run"}</a></div></div>`;
}
function editorV(st, id, phone) {
  const t = T[id], f = (label, html, hint = "") => `<div class="fld"><div class="fl">${label}${hint ? `<span class="faint"> · ${hint}</span>` : ""}</div>${html}</div>`;
  const ta = (txt, rows = 3) => `<div class="tin" contenteditable="plaintext-only" style="min-height:${rows * 1.5}em">${txt}</div>`;
  const kw = (KW[id] || [[t.n.split(" ")[0].toLowerCase() + "*", 120]]).map(([k, n]) => `<span class="kw">${k}<span class="mono faint">${n}</span><a class="x">×</a></span>`).join("") + `<span class="kw add">+ keyword</span>`;
  const body = (st.saved ? offer(st, "topic") : "") + pageHead(t.n, `Version ${(t.n.length % 5) + 1} · follows ${PARENT[id] ? `${T[PARENT[id]].n} › ` : ""}${t.n}`) +
    `<div class="form">${f("Class", `<div class="tin one">${PARENT[id] ? `${T[PARENT[id]].n} › ` : ""}${t.n} <span class="faint">▾</span></div>`)}
    ${f("Depth", seg(["focus", "area", "field"], t.depth, "data-x"), "Focus is graded by two labs, Area checked, Field graded once")}
    ${f("Subject", ta(SUBJECT[id] || ""), "what the topic is about; the graders read it")}
    ${f("Rubric", ta(RUBRIC[id] || "Central, related, adjacent: what each means here.", 4))}
    ${f("Interests", ta(INTEREST[id] || "", 2), "what makes a paper matter more")}
    ${f("Keywords", `<div class="kws">${kw}</div>`, "matches in the last 90 days")}
    ${f("Look", `<div class="look"><span class="badge ${t.depth} lg" style="--tc:${t.hue}">${t.m}</span><span class="tin sm mono" contenteditable="plaintext-only">${t.m}</span><span class="sws">${SWATCH.map((c) => `<i style="background:${c}"${c === t.hue ? ' class="on"' : ""}></i>`).join("")}</span></div>`)}
    ${f("Status", seg(["active", "paused"], "active", "data-x"), "a paused topic keeps its papers and gets no new ones")}</div>
    <div class="nact" style="margin-top:16px"><a class="btn pri" data-saved="1">Save</a><a class="btn" data-back>Cancel</a></div>`;
  return { body, title: t.n };
}

/* ── Taxonomy ── */
function taxV(st) {
  const rows = TREE.map(([d, n, th, tp, m, fold], i) => {
    const ed = st.cls === i ? `<div class="clsed"><div class="form">
      <div class="fld"><div class="fl">Name</div><div class="tin one" contenteditable="plaintext-only">${n}</div></div>
      <div class="fld"><div class="fl">Description<span class="faint"> · says what is out as well as what is in</span></div><div class="tin" contenteditable="plaintext-only" style="min-height:3em">The class's one or two sentences from the taxonomy.</div></div>
      <div class="fld"><div class="fl">Threshold<span class="faint"> · a paper is a member at or above it</span></div><div class="tin one sm mono" contenteditable="plaintext-only">${th.toFixed(2)}</div></div></div>
      <div class="nact"><a class="btn pri" data-clsave="${i}">Save</a><a class="btn" data-cls="-1">Cancel</a></div></div>` : "";
    return `<div class="crow d${d}${st.cls === i ? " on" : ""}" data-cls="${i}">${fold ? `<span class="car">▸</span>` : d ? "" : `<span class="car">▾</span>`}<span class="nm">${n}</span>${tp ? badge(tp) : ""}<span class="sp"></span><span class="mono faint">${typeof m === "number" ? m.toLocaleString("en") : m}</span><span class="mono th">${th.toFixed(2)}</span></div>${ed}`;
  }).join("");
  return (st.saved ? offer(st, "class") : "") + pageHead("Taxonomy", "Every paper is classified into this tree. A topic follows one class; edits apply forward.", `<span class="nact"><a class="btn" data-toast="taxonomy.yaml downloaded">Export YAML</a><a class="btn">Import YAML</a></span>`) +
    `<div class="list ctree"><div class="crow hd"><span class="nm">Class</span><span class="sp"></span><span>Members</span><span class="th">Threshold</span></div>${rows}</div>`;
}

/* ── Misses ── */
function missesV(st) {
  return pageHead("Misses", "Papers reported as missed, with what each stage said. A trace shows where the pipeline let the paper go.") +
    MISSES.map((m, i) => `<div class="miss${m.done || SETTLED["m" + i] ? " settled" : ""}"><div class="nh">${badge(m.t)}<span class="nt">${m.ti}</span><span class="faint" style="margin-left:auto;font-size:12px;white-space:nowrap">${m.d}</span></div>
      <div class="nb">Missed at <b class="${m.done ? "" : "badt"}">${m.at}</b></div>
      <div class="trace">${m.trace.map(([s, v, c]) => `<div class="stg ${c}"><span>${s}</span><b>${v}</b></div>`).join("")}</div>
      ${m.done || SETTLED["m" + i] ? `<div class="nres">${m.done || SETTLED["m" + i]}</div>` : `<div class="nact"><a class="btn" data-sub="t:${m.t}">Edit the topic</a><a class="btn" data-sset="taxonomy">Edit the class</a><a class="btn" data-settle="m${i}|Marked resolved.">Resolved</a></div>`}</div>`).join("");
}

/* ── Runs and cost, Integrations, Configuration ── */
function runsV(phone) {
  const cols = phone ? [0, 1, 5, 6] : [0, 1, 2, 3, 4, 5, 6], hd = ["Night", "Papers", "Graded", "Digests", "Took", "Cost", ""];
  return pageHead("Runs and cost", "Month to date per provider, the projection to the month's end, and each night's run.") + spendRows(phone) +
    `<div class="cap" style="margin-top:18px">Nights</div><div class="mtbl runs c${cols.length}"><div class="mh">${cols.map((c) => `<span>${hd[c]}</span>`).join("")}</div>
    ${RUNS.map((r) => `<div class="mr${r[6] ? " bad" : ""}">${cols.map((c) => `<span class="${c ? "mono" : ""}">${c === 5 ? usd(r[5]) : c === 1 || c === 2 ? r[c].toLocaleString("en") : c === 6 ? (r[6] ? `<span class="badt">${phone ? "1 failed" : r[6]}</span>` : "") : r[c]}</span>`).join("")}</div>`).join("")}</div>
    <div class="cap" style="margin-top:18px">Jobs</div><div class="list"><div class="drow quiet" style="--rc:var(--line)"><span>Re-run · Chain-of-thought faithfulness, existing members</span><span class="c">412 papers · $0.61 · 12 Oct</span></div></div>`;
}
function integV() {
  return pageHead("Integrations", "Each reacts to the pipeline's events on its own schedule. Which are loaded is set in litwatch.toml.") +
    INTEG.map((x, i) => `<div class="icard${x.fail && !SETTLED["i" + i] ? " fail" : ""}${x.on ? "" : " off"}"><div class="nh"><span class="mono in">${x.n}</span><span class="faint">${x.on ? `on · ${x.on}` : "off"}</span></div>
      <div class="nb">${x.d}</div><div class="ist">${SETTLED["i" + i] || x.st}</div>
      ${x.fail && !SETTLED["i" + i] ? `<div class="ierr mono">${x.fail}</div><div class="nact"><a class="btn" data-toast="Retrying the event.">Retry now</a><a class="btn" data-settle="i${i}|Event skipped · up to date">Skip this event</a><a class="btn">All events</a></div>` : x.on ? `<div class="nact"><a class="btn">All events</a></div>` : ""}</div>`).join("") +
    `<div class="cap" style="margin-top:18px">Outbox</div><div class="mtbl c4"><div class="mh"><span>Integration</span><span>Waiting</span><span>Failed</span><span>Last success</span></div>
    ${[["zotero", SETTLED.i0 ? 0 : 1, SETTLED.i0 ? 0 : 1, "06:12"], ["webdav", 0, 0, "06:11"], ["deep", 0, 0, "06:20"]].map(([n, w, f, l]) => `<div class="mr"><span class="mono">${n}</span><span class="mono${w ? "" : " faint"}">${w}</span><span class="mono ${f ? "badt" : "faint"}">${f}</span><span class="mono">${l}</span></div>`).join("")}</div>`;
}
function configV() {
  const toml = `[archive]\nurl = "http://archive.lan"\ntoken = "••••••••"\n\n[models.judge]\nprovider = "anthropic"\nmodel = "claude-opus-5-5"\n\n[models.check]\nprovider = "openrouter"\nmodel = "meta/muse-spark-1.3-contributor"\n\n[budgets]\nanthropic = 100\nopenrouter = 40\n\n[integrations.zotero]\nlibrary = "litwatch"\napi_key = "••••••••"`;
  return pageHead("Configuration", "litwatch.toml as loaded; read-only here, the file is the source of truth. Secrets are masked.") +
    `<div class="cap">Models per role</div><div class="mtbl c3">${ROLES.map(([r, p, m]) => `<div class="mr"><span class="mono">${r}</span><span class="faint">${p}</span><span class="mono">${m}</span></div>`).join("")}</div>
    <div class="nres" style="margin:8px 0 18px">judge and check come from different labs (Anthropic, Meta).</div>
    <div class="cap">litwatch.toml</div><pre class="toml">${toml}</pre>`;
}

/* ── the index: the desktop's sidebar or a column in the page, the phone's list ── */
function setIndex(st, cls) {
  const cur = st.set;
  return SET_PAGES.map(([g, ps]) => `<div class="sec">${g}</div>` + ps.map(([id, n]) => { const h = id === "quality" ? [needOpen() ? `${needOpen()} need you` : "All settled", needOpen()] : SET_HINT[id], k = h[1];
    return `<div class="it${cur === id ? " on" : ""}" data-sset="${id}"><span class="nm">${n}</span>${cls === "list" ? `<span class="hint">${h[0]}</span>` : k ? `<span class="k${k === "!" ? " bad" : " hot"}">${k === "!" ? "1" : k}</span>` : ""}</div>`; }).join("")).join("");
}
function setPage(st, phone, look) {
  if (st.sub && st.sub.startsWith("t:")) return editorV(st, st.sub.slice(2), phone);
  const s = st.set;
  if (s === "calibrate") return calV(st, phone);
  const body = s === "quality" ? qualityV(st, phone, look) : s === "topics" ? topicsSetV(phone) : s === "taxonomy" ? taxV(st) : s === "misses" ? missesV(st)
    : s === "runs" ? runsV(phone) : s === "integrations" ? integV() : configV();
  return { body, title: SET_NAME[s] };
}
// layout "side": Settings takes the sidebar, with a way back to reading at its top; "col": the sidebar stays and an
// index column sits in the page
function setV(st, phone, { look = "numbers", layout = "side" } = {}) {
  if (phone && st.set === "index") return { body: `<div class="setlist">${setIndex(st, "list")}</div>`, title: "Settings" };
  const v = setPage(st, phone, look), wide = `<div class="setpg">${v.body}</div>`;
  if (phone) return { ...v, body: `<div class="setpg">${v.body}</div>` };
  if (layout === "side") return { ...v, body: wide, noBack: !st.sub, side: `<div class="wm">${shellWM()}</div><div class="it leave" data-leave>‹ Back to reading</div><div class="sec setttl">⚙ Settings</div>${setIndex(st, "side")}` };
  return { ...v, noBack: !st.sub, body: `<div class="setcols"><nav class="setidx">${setIndex(st, "side")}</nav>${wide}</div>` };
}
