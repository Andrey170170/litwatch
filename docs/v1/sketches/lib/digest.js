// Digest sample data and fragments, from round 2 on: three weeks of days with their digests' Marks,
// today's digests in full, last week's weeklies, and the History list and colour grid built from them.

/* ── sample data for this round ── */
const DAILY = ["cotf", "cotm", "circ", "interp", "align"];
PAPERS.push(
  { id: "2610.04877", r: 1, t: "Process Rewards Favour Verbose Steps Over Faithful Ones", g: "Process reward models score longer steps higher whether or not the step was used.", d: "9 Oct", tp: ["cotf"] },
  ...[["SAE Reconstruction Loss Does Not Predict Downstream Faithfulness", "Across 40 SAEs, lower reconstruction loss buys little on causal faithfulness benchmarks.", 2],
      ["A Benchmark of Feature Interpretability Judgements With Human Agreement", "1,800 SAE features rated by people and by LLM judges; the judges agree with people 71% of the time.", 2],
      ["Mechanistic Signatures of Memorised Versus Generalised Facts", "Memorised facts route through fewer MLP layers than facts the model can generalise.", 2],
      ["Automated Circuit Discovery With Edge Pruning at Scale", "Edge pruning recovers known circuits in a 7B model in under an hour.", 2],
      ["Probing for Truthfulness Directions Across Fine-Tuning Stages", "Truth directions found in base models survive instruction tuning at cosine 0.8.", 1],
      ["Logit Lens Variants Compared on Multilingual Models", "The tuned lens beats the raw logit lens most on non-English prompts.", 1],
      ["Feature Splitting Is Predictable From Dictionary Size", "How a feature splits as the dictionary grows follows a power law in three models.", 1],
      ["Attention Head Taxonomies Do Not Transfer Across Scales", "Head roles named at 1B rarely keep their role at 70B.", 1],
      ["Steering Vectors Degrade Gracefully Under Quantisation", "4-bit quantisation keeps 90% of a steering vector's effect.", 1],
      ["Linear Representations of Time in Video-Language Models", "Event order is linearly decodable from one middle layer.", 1]]
    .map(([t, g, r], i) => ({ id: "2610.045" + String(i).padStart(2, "0"), r, t, g, d: "13 Oct", tp: ["interp"] })));
PAPERS.forEach((p) => (P[p.id] = p));
const ALSO_INTERP = PAPERS.filter((p) => p.id.startsWith("2610.045")).map((p) => p.id);

const FULL = {
  cotf: ["Two measurement papers, both about how much of a model's reasoning reaches its trace. The larger one instruments four open model families and compares each trace with the computation recovered from activations: 31% of correct answers rest on steps the trace never states, and the share grows with trace length. The recovery method is validated on toy tasks only, which bounds how far the number can be trusted.",
         "The second asks models to state their decision rules on synthetic classification tasks and checks each statement causally by editing the inputs. Statements match the causal test 62% of the time, less as the rules get more complex. A related paper from Friday, held back until its grading finished, finds process rewards favour verbose steps."],
  cotm: ["Two ways a chain-of-thought monitor fails. Under RL against a monitor, models learn within about 2k steps to put the forbidden step in innocuous wording, and the monitor's recall falls to chance while task reward holds.",
         "The second paper needs no training: plain-language persuasion aimed at the monitor model evades it about as often as the encoded reasoning does, which suggests the monitor's own robustness is the weaker link."],
  circ: ["One paper, and a careful one: transcoder attribution graphs recover the same carry circuit for multi-digit addition in three instruction-tuned models, and every edge is checked against activation patching. Graph and patching agree on 94% of edges."],
  interp: ["A quieter day. The pick is crosscoder work: trained on paired activations from Llama and Qwen at matched scale, crosscoders find about 40% of features shared, with the shared ones concentrated in middle layers.",
           "Two evaluation papers push against reconstruction loss as the measure of a good SAE, one with causal benchmarks and one with human ratings. The day's strongest interpretability results are in the focus digests: the CoT faithfulness measurement and the arithmetic circuit."],
};
const MENTIONED = { interp: ["2610.04321", "2610.03877"] };
const RELATED = { cotf: ["2610.04877"] };
const WEEK = {
  cotf: ["The week's thread was measurement: three papers put a number on how much of a model's computation its trace leaves out, from 18% to 40% depending on the probe. September's hint-following paper is the natural baseline for all three.", ["2610.04321", "2610.05112"], 7],
  cotm: ["Monitors were attacked from two sides: RL pressure that teaches encoding, and prompts that persuade the monitor directly.", ["2610.04780", "2610.05230"], 5],
  circ: ["Attribution graphs moved from single models to comparisons across models; two papers check graphs against patching edge by edge.", ["2610.03877"], 6],
  interp: ["Crosscoders for features shared across models, and a pushback on reconstruction loss as the measure of a good SAE.", ["2610.04990", "2610.04500", "2610.04501", "2610.04502", "2610.04503"], 64],
  align: ["Weak-to-strong generalisation returned with two replications; one finds the gains vanish against better baselines.", ["2610.04780", "2610.05230"], 31],
  lm: ["Long context: three papers on positional extrapolation and one large data-mixture ablation.", ["2610.04411", "2610.04002", "2610.03550"], 420],
  reason: ["RL post-training papers agree that length penalties shape training traces more than test-time behaviour.", ["2610.04411"], 88],
  agents: ["Benchmarks under distribution shift dominated; web agents lose 20–30 points when sites change.", ["2610.03550"], 74],
  eff: ["KV-cache eviction and speculative decoding, mostly incremental, and one strong serving paper at a quarter of the memory.", ["2610.04002"], 95],
};
const WEEK_R = { cotf: 5, cotm: 4, circ: 4, interp: 4, align: 3, lm: 4, reason: 3, agents: 2, eff: 3 };

// every day since the archive started, Monday 1 Jun to today, oldest first; generated marks, with today, Mon 12 and
// Fri 9 as in round 1
const MON = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"], MONTH = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const START = Date.UTC(2026, 5, 1), DAYMS = 864e5, NDAYS = (Date.UTC(2026, 9, 13) - START) / DAYMS + 1;
function seeded(s) { let h = 2166136261; for (const c of s) h = Math.imul(h ^ c.charCodeAt(0), 16777619); return () => { h = Math.imul(h ^ (h >>> 15), 2246822507); h ^= h >>> 13; return (h >>> 0) / 4294967296; }; }
const FIXED = {
  "Tue 13 Oct": { cotf: [2, 5, "2 new · 1 must read"], cotm: [2, 4, "2 new · 1 must read"], circ: [1, 3, "1 new"], interp: [11, 3, "11 new · 1 pick"], align: [0, 0, "Nothing new"] },
  "Mon 12 Oct": { cotf: [4, 3, "4 new"], cotm: [0, 0, "Nothing new"], circ: [3, 4, "3 new · 1 must read"], interp: [19, 4, "19 new · 2 picks"], align: [7, 3, "7 new · 1 pick"] },
  "Fri 9 Oct": { cotf: [1, 3, "1 new"], cotm: [1, 5, "1 new · 1 must read"], circ: [2, 2, "2 new"], interp: [14, 3, "14 new · 1 pick"], align: [5, 3, "5 new · 1 pick"] },
  "Thu 8 Oct": { cotm: [0, -1, "Could not be written"] },
};
const LOG = [];
for (let i = 0; i < NDAYS; i++) {
  const dt = new Date(START + i * DAYMS), dow = dt.getUTCDay(), dom = dt.getUTCDate();
  const key = `${MON[dow]} ${dom} ${MONTH[dt.getUTCMonth()]}`;
  const day = { key, dom, dow, none: dow === 0 || dow === 6, marks: {} };
  if (!day.none) for (const id of DAILY) {
    const fx = FIXED[key] && FIXED[key][id];
    if (fx) { day.marks[id] = { n: fx[0], r: fx[1], c: fx[2] }; continue; }
    const R = seeded(key + id), area = T[id].depth === "area";
    if (R() < (area ? 0.12 : 0.3)) { day.marks[id] = { n: 0, r: 0, c: "Nothing new" }; continue; }
    const n = area ? 5 + Math.floor(R() * 15) : 1 + Math.floor(R() * 4), r = [1, 1, 1, 2, 2, 2, 3, 3, 4, 5][Math.floor(R() * 10)];
    const k = area && r >= 3 ? 1 + (R() < 0.3) : 0;
    day.marks[id] = { n, r, c: `${n} new` + (area ? (k ? ` · ${k} pick${k > 1 ? "s" : ""}` : "") : r >= 4 ? " · 1 must read" : "") };
  }
  if (dow === 1 && i > 0) {
    const from = new Date(START + (i - 7) * DAYMS), to = new Date(START + (i - 1) * DAYMS);
    const f = (d) => `${d.getUTCDate()} ${MONTH[d.getUTCMonth()]}`;
    day.week = { range: `${from.getUTCMonth() === to.getUTCMonth() ? from.getUTCDate() : f(from)}–${f(to)}`, marks: {} };
    for (const t of TOPICS) {
      const [, picks, n0] = WEEK[t.id], R = seeded(key + "w" + t.id), now = key === "Mon 12 Oct", r = now ? WEEK_R[t.id] : 2 + Math.floor(R() * 4);
      const n = now ? n0 : Math.max(1, Math.round(n0 * (0.6 + R() * 0.8)));
      day.week.marks[t.id] = { n, r, c: t.depth === "focus" ? `Week · ${n} papers` : `Week · ${picks.length >= 5 ? 5 : 3} picks` };
    }
  }
  LOG.push(day);
}
const DAY = Object.fromEntries(LOG.map((d) => [d.key, d]));
const TODAY = "Tue 13 Oct", THIS_WEEK = "Mon 12 Oct";
const NEWEST = [...LOG].reverse();

/* ── fragments ── */
const rcs = (r) => (r > 0 ? `--rc:var(--r${r})` : "");
function markRow(key, id, m, act) {
  if (m.r === -1) return `<div class="drow quiet" style="color:var(--bad)">${badge(id)}<span class="nm">${T[id].n}</span><span class="c" style="color:var(--bad)">${m.c}</span></div>`;
  return `<div class="drow${m.r ? "" : " quiet"}" style="${rcs(m.r)}" ${act(key, 1)} title="${T[id].n} · ${m.c}">${badge(id)}<span class="nm">${T[id].n}</span><span class="c">${m.c}</span></div>`;
}
function src(key) { const [day, id, w] = key.split("|"); return { day, id, w: !!w, m: w ? DAY[day].week.marks[id] : DAY[day].marks[id] }; }
// today's quiet topics have no sample text, so other days fall back to placeholders for them
function gistOf(id, w) { return w ? WEEK[id][0] : (DIGEST[id] && DIGEST[id].p) || "Sample text: the writer's short paragraph on what the day brought in this topic."; }
function picksOf(id, w) { return w ? WEEK[id][1] : (DIGEST[id] && DIGEST[id].picks) || PAPERS.filter((p) => p.tp.includes(id)).slice(0, 2).map((p) => p.id); }
function failedLine(id) { return `<div class="failed">${badge(id)}<span>${T[id].n}: this digest could not be written. The next run writes it again.</span></div>`; }

// a digest at Gist level: its Gist, its picks' Marks, a count of the rest; the whole card opens the digest
function gistBlock(key, act, extra = "") {
  const { id, w, m } = src(key);
  if (m.r === -1) return failedLine(id);
  if (!m.r) return `<div class="dblock quiet click ${extra}" ${act(key, 2)}><div class="hd">${badge(id)}<span class="n">${T[id].n}</span><span class="c">Nothing new</span>${foldBtn(key, act)}</div></div>`;
  const picks = picksOf(id, w).slice(0, 3);
  const rest = Math.max(0, m.n - picks.length);
  return `<div class="dblock click ${extra}" ${act(key, 2)}><div class="hd">${badge(id)}<span class="n">${T[id].n}</span><span class="c" style="color:var(--r${m.r})">${m.c}</span>${foldBtn(key, act)}</div>
    <p class="p">${gistOf(id, w)}</p><div class="list">${picks.map((pid) => prow(P[pid], { badges: false })).join("")}</div>
    ${rest ? `<div class="more">+${rest} paper${rest > 1 ? "s" : ""}</div>` : ""}</div>`;
}
const foldBtn = (key, act) => (act.fold ? `<button class="ib fold" ${act(key, 0)} title="Fold">–</button>` : "");

// a whole digest: text, then its papers by role
function digestBody(key) {
  const { id, w, m } = src(key), t = T[id];
  if (m.r === -1) return `<p class="prose" style="color:var(--bad)">This digest could not be written. Its papers stay due, and the next run writes it again.</p>`;
  if (!m.r) return `<p class="prose faint">Nothing was due for this topic that day. The run looked and found nothing new.</p>`;
  const text = (!w && FULL[id]) || [gistOf(id, w), "The rest of the digest goes here: the writer's longer text on the same papers, in the same voice."];
  const rows = (ids, gist = true) => `<div class="list">${ids.map((pid) => prow(P[pid], { badges: false, gist })).join("")}</div>`;
  let lists;
  if (w) lists = `<div class="cap">${t.depth === "focus" ? "Central this week" : "Picks"}</div>` + rows(picksOf(id, w));
  else if (t.depth === "focus") lists = `<div class="cap">Central</div>` + rows(picksOf(id)) + (RELATED[id] ? `<div class="cap">Related</div>` + rows(RELATED[id], false) : "");
  else lists = `<div class="cap">Picks</div>` + rows(picksOf(id)) + (MENTIONED[id] ? `<div class="cap">Mentioned</div>` + rows(MENTIONED[id]) : "") +
    `<div class="cap">Also due</div>` + rows(id === "interp" ? ALSO_INTERP : []);
  return text.map((p) => `<p class="prose">${p}</p>`).join("") + lists;
}
function digestPage(key) {
  const { day, id, w, m } = src(key);
  return `<div class="dgfull"><div class="dghead">${badge(id, "lg")}<div><div class="h1">${T[id].n}</div><div class="sub">${w ? `Weekly digest · ${DAY[day].week.range}` : `Daily digest · ${day}`} · <span style="color:var(--r${m.r || 1})">${m.c}</span></div></div></div>${digestBody(key)}</div>`;
}
const dgTitle = (key) => { const { id, w } = src(key); return T[id].n + (w ? " · week" : ""); };

function daystrip(day, history) {
  const i = NEWEST.findIndex((d) => d.key === day);
  return `<div class="daystrip"><button class="ib" data-step="1">‹</button><span class="day">${day === TODAY ? "<small>Today</small>" : ""}${day}</span><button class="ib" data-step="-1" ${i === 0 ? "disabled" : ""}>›</button><span class="sp"></span>${history ? '<a class="chipbtn" data-go="history">History</a>' : ""}</div>`;
}
const DG = (key) => `data-dg="${key}"`; DG.fold = false;
function dayBody(day, act = DG, { weekly = true } = {}) {
  const d = DAY[day];
  if (d.none) return `<div class="none">No papers released.</div>`;
  let h = DAILY.map((id) => gistBlock(`${day}|${id}`, act)).join("");
  if (d.week && weekly) h += `<div class="grp"><span class="t">Weekly</span><span class="s">${d.week.range}</span></div>` + TOPICS.map((t) => gistBlock(`${day}|${t.id}|w`, act)).join("");
  return h;
}
function stepDay(day, s) { // s=+1 older, -1 newer, skipping days with nothing released
  let i = NEWEST.findIndex((d) => d.key === day);
  do i += s; while (NEWEST[i] && NEWEST[i].none);
  return NEWEST[i] ? NEWEST[i].key : day;
}

/* A · History as a page of Marks */
function historyList() {
  const wk = DAY[THIS_WEEK].week;
  let h = `<div class="grp pin"><span class="t">This week</span><span class="s">weekly digests · ${wk.range}</span></div><div class="list">${TOPICS.map((t) => markRow(`${THIS_WEEK}|${t.id}|w`, t.id, wk.marks[t.id], DG)).join("")}</div>`;
  for (const d of NEWEST) {
    h += `<div class="grp click" data-day="${d.key}"><span class="t">${d.key}</span>${d.key === TODAY ? '<span class="s">today</span>' : ""}</div>`;
    if (d.none) { h += `<div class="none">No papers released.</div>`; continue; }
    h += `<div class="list">${DAILY.map((id) => markRow(`${d.key}|${id}`, id, d.marks[id], DG)).join("")}</div>`;
    if (d.week) h += `<div class="grp"><span class="s">Weekly · ${d.week.range}</span></div><div class="list">${TOPICS.map((t) => markRow(`${d.key}|${t.id}|w`, t.id, d.week.marks[t.id], DG)).join("")}</div>`;
  }
  return h;
}

/* C · the colour grid */
function gridView(sel, { weeks = 3, lg = false } = {}) {
  const weeksShown = weeks;
  let from = LOG.length - 1, left = weeks; // back to the Monday that starts the oldest week shown
  while (from > 0 && !(LOG[from].dow === 1 && --left === 0)) from--;
  const cols = [];
  // weeks run Monday to Sunday: a weekly column closes the week it covers, before the Monday that wrote it
  for (const d of LOG.slice(from)) { if (d.week && d !== LOG[from]) cols.push({ k: "w", d }); cols.push({ k: d.none ? "we" : "day", d }); }
  const tpl = "auto " + cols.map((c) => (c.k === "we" ? "4px" : "var(--cw)")).join(" ");
  const ci = cols.findIndex((c) => c.k === "day" && c.d.key === sel) + 2;
  // every item is placed explicitly, so the highlight column can sit under the cells
  const at = (r, c) => `grid-row:${r};grid-column:${c}`;
  let h = ci > 1 ? `<span class="colhl" style="grid-column:${ci};grid-row:1 / span ${TOPICS.length + 1}"></span>` : "";
  cols.forEach((c, j) => {
    if (c.k === "day") h += `<span class="gh${c.d.key === sel ? " on" : ""}${c.d.key === TODAY ? " today" : ""}" style="${at(1, j + 2)}" data-gday="${c.d.key}" title="${c.d.key}">${c.d.dom}</span>`;
    else if (c.k === "w") h += `<span class="gh w" style="${at(1, j + 2)}" data-gweek="${c.d.key}" title="Weekly digests · ${c.d.week.range}">W</span>`;
  });
  TOPICS.forEach((t, i) => {
    h += `<span class="gl" style="${at(i + 2, 1)}">${badge(t.id)}</span>`;
    cols.forEach((c, j) => {
      const m = c.k === "day" ? c.d.marks[t.id] : c.k === "w" ? c.d.week.marks[t.id] : null;
      if (!m) return;
      const key = `${c.d.key}|${t.id}${c.k === "w" ? "|w" : ""}`, tip = `${c.k === "w" ? "Week " + c.d.week.range : c.d.key} · ${t.n} · ${m.c}`;
      h += `<span class="cell${m.r === 0 ? " h" : m.r === -1 ? " x" : ""}${c.k === "w" ? " wk" : ""}" style="${rcs(m.r)};${at(i + 2, j + 2)}" data-cell="${key}" title="${tip}"></span>`;
    });
  });
  const leg = [1, 2, 3, 4, 5].map((r) => `<span><i style="--rc:var(--r${r})"></i>${["", "Common", "Uncommon", "Rare", "Epic", "Legendary"][r]}</span>`).join("");
  return `<div class="gridwrap"><div class="ghead"><span class="cap">${LOG[from].key.slice(4)} – ${TODAY.slice(4)}</span><span class="legend">${leg}<span><i class="h"></i>Nothing new</span></span></div><div class="calscroll"><div class="grid${lg ? " lg" : weeksShown > 4 ? " wide" : ""}" style="grid-template-columns:${tpl}">${h}</div></div></div>`;
}


/* History as a month: a row per week, Monday to Friday, then the week's W. A day cell holds its daily digests as
   bars in the fixed topic order; the W cell holds the weekly ones in two columns, Focus and Area first (level with
   the days' bars), Fields beside them, so Field topics need no rows of their own. With text (desktop), a bar carries
   its badge and count and opens its digest; without (phone), bars are too thin to tap, so a cell opens its Day or Week. */
function monthView({ weeks = 6, text = false, older = false } = {}) {
  let from = LOG.length - 1, left = weeks;
  while (from > 0 && !(LOG[from].dow === 1 && --left === 0)) from--;
  const rows = [];
  for (const d of LOG.slice(from)) { if (d.dow === 1) rows.push({ mon: d, days: {} }); if (!d.none) rows.at(-1).days[d.dow] = d; }
  const nextMon = (mon) => { const i = LOG.indexOf(mon) + 7; return LOG[i] && LOG[i].week ? LOG[i] : null; };
  const short = (m) => (m.r === -1 ? "failed" : m.r ? m.n : "–");
  const bar = (key, id, m) => `<span class="mbar${m.r === 0 ? " h" : m.r === -1 ? " x" : ""}" style="${rcs(m.r)}" ${text ? `data-cell="${key}"` : ""} title="${T[id].n} · ${m.c}">${text ? `${badge(id)}<span class="mc">${short(m)}</span>` : ""}</span>`;
  let h = ["Mon", "Tue", "Wed", "Thu", "Fri", "Week"].map((n) => `<div class="mh">${n}</div>`).join("");
  rows.forEach((row, ri) => {
    for (let dow = 1; dow <= 5; dow++) {
      const d = row.days[dow];
      if (!d) { h += `<div class="mday empty"></div>`; continue; }
      const num = d.dom === 1 || (ri === 0 && dow === 1) ? `${d.dom} ${d.key.split(" ")[2]}` : d.dom;
      h += `<div class="mday${d.key === TODAY ? " today" : ""}" ${text ? "" : `data-gday="${d.key}"`}><span class="mnum" data-gday="${d.key}" title="Open ${d.key}">${num}</span>${DAILY.map((id) => bar(`${d.key}|${id}`, id, d.marks[id])).join("")}</div>`;
    }
    const wm = nextMon(row.mon);
    if (!wm) { h += `<div class="mweek empty"><span class="mnum">W · Monday</span></div>`; return; }
    h += `<div class="mweek" ${text ? "" : `data-gweek="${wm.key}"`}><span class="mnum" data-gweek="${wm.key}" title="Open the week ${wm.week.range}">W ${wm.week.range}</span>` +
      TOPICS.map((t) => bar(`${wm.key}|${t.id}|w`, t.id, wm.week.marks[t.id])).join("") + `</div>`;
  });
  // with older, the top says whether scrolling up loads more weeks or the archive starts here
  const top = !older ? "" : from > 0 ? `<div class="loadmore">Loading older weeks…</div>` : `<div class="archstart">The archive starts on ${LOG[0].key}.</div>`;
  return `${top}<div class="month${text ? " text" : ""}">${h}</div>`;
}
