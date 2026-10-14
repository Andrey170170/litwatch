// Round 4: the paper page. Hand-written text for five papers that show the page's states (a landmark with its Deep
// card, a card being written, a Brief from an older revision, a paper with no Brief, an archive-only paper), filler
// for the rest, and the page's pieces. Loads after papers.js.

const FIT = ["", "none", "adjacent", "related", "central", "central"];
const PX = {
  "2610.04321": {
    au: ["Ada Okafor", "Erik Lindqvist", "Haruto Sato", "Claire Moreau", "Rami Haddad", "Priya Iyer"],
    ver: [["v2", "13 Oct"], ["v1", "9 Oct"]], weight: "landmark", by: "two labs agree",
    brief: { over: "The abstract overstates: the 31% holds for traces over 2k tokens; under 500 tokens it is 9%.",
      text: "The authors run four open model families (Llama 3.3 70B, Qwen 3 32B, Gemma 3 27B, OLMo 2 32B) on 6,400 multi-step math and logic problems and compare each trace with the intermediate values linear probes read from the residual stream. A correct answer counts as resting on omitted computation when a probe reads a value with high confidence, patching that value flips the answer, and the trace never states it. 31% of correct answers on traces over 2k tokens meet this bar, against 9% under 500 tokens, and the rate rises with length in every family. The probes are validated against patching on 400 held-out items (0.91 agreement). The main limit: only values a linear probe can read are seen, so the rate is a lower bound, and steps the model takes without a linear trace are invisible." },
    card: { state: "ready", read: "arXiv v2 · LaTeX", at: "13 Oct, 09:12",
      claim: "Correct answers from reasoning models often rest on intermediate results the chain of thought never states, and the share grows with trace length.",
      summary: ["Reasoning traces are read as a record of how a model reached its answer. This paper measures how often that record leaves out a step the answer depended on. Linear probes read intermediate values (partial sums, chosen branches, retrieved facts) from the residual stream of four open model families; a correct answer is unfaithful by omission when a probe reads a value with high confidence, patching that value flips the answer, and the trace never states it.",
        "Across 6,400 problems, 31% of correct answers on traces over 2k tokens rest on such a value, against 9% under 500 tokens. The rise with length holds in every family, at levels from 18% (OLMo 2) to 41% (Qwen 3). Asking the model to show every step lowers the rate by a third but leaves the length effect. What is new is the scale and the causal check: earlier omission results relied on hints planted in the prompt."],
      models: ["Llama 3.3 70B", "Qwen 3 32B", "Gemma 3 27B", "OLMo 2 32B"], tasks: ["GSM-Hard", "MATH levels 4–5", "FOLIO", "synthetic multi-hop arithmetic"],
      gen: "Four families and three task types; the length effect holds in all, the level differs by family. Not tested on closed models or on code.",
      ext: { topic: "cotf", facts: [["Mismatch", "omission"], ["Ground truth", "internals"], ["Monitor", "white-box"], ["Signals", "probes, activation patching"]],
        text: [["Detection", "A linear probe per intermediate quantity, trained on synthetic arithmetic and checked against patching on 400 held-out items (0.91 agreement). An omission is a value read at over 0.9 probability, used causally (patching flips the answer in over 80% of resamples), and absent from the trace by string and paraphrase match."],
          ["Definition", "A step is faithful when it states a value the model computed and used. Only omission is measured: computation that took place and that the trace leaves out. Whether stated steps took place (commission) is not tested."],
          ["Generality", "Meant as a general measurement; transfer is shown across four families and three task types, within what a linear probe can read."]] },
      limits: ["Only linearly decodable values are seen, so 31% is a lower bound on omitted computation.", "String and paraphrase matching can miss a value the trace states indirectly, which would inflate the rate.", "The probes are trained on synthetic arithmetic; their accuracy on FOLIO's logic steps is not reported on its own.", "No closed or RL-trained reasoning models; the length effect may differ there."],
      prior: [["2305.04388", "Language Models Don't Always Say What They Think: Unfaithful Explanations in Chain-of-Thought Prompting", "Showed unfaithfulness with biasing features planted in the prompt; this paper measures omission without planted cues."],
        ["2307.13702", "Measuring Faithfulness in Chain-of-Thought Reasoning", "Truncated and corrupted traces to test reliance on them; this paper reads internals and patches instead."],
        [null, "Chain-of-Thought Reasoning in the Wild Is Not Always Faithful", "Found unfaithfulness on natural prompts; this paper adds the causal check and the trend with length."]],
      among: [["agrees", "2610.05112", "Self-reports of decision rules fail more often as rules grow complex, the same direction as the length effect."],
        ["extends", /^Hint Use/, "Uses planted hints on short traces; this paper finds the same gap without hints and at length."],
        ["contradicts", /^Counterfactual/, "Finds traces mostly faithful under counterfactual edits on short math problems, the range where this paper's effect is smallest."],
        ["supersedes", /Omitted Steps/, "An earlier estimate of omitted steps from one model family, with no causal check."]] } },
  "2610.04780": {
    au: ["Lena Kowalski", "Minh Nguyen", "Tomás Ferreira", "Jonas Brandt", "Ama Mensah"],
    ver: [["v1", "12 Oct"]], weight: "solid", by: "two labs agree",
    brief: { text: "The authors train Qwen 3 8B and 32B with RL on coding tasks while a monitor model reads each chain of thought and penalises any mention of special-casing the tests. Within 2k RL steps the rate of special-casing stays at 38% while the monitor's catch rate falls from 91% to 12%: the step is still taken, but described in innocuous wording (\"adjust for the edge case\"). A paraphrasing defence, where a second model rewrites the trace before the monitor reads it, brings the catch rate back to 64% for 8B and 40% for 32B. The main limit is one task family and one monitor; whether a stronger monitor or a probe on activations would still catch the step is not tested." },
    card: { state: "running", read: "arXiv v1 · LaTeX", at: "started 6 min ago" } },
  "2610.05112": {
    au: ["Yuki Sato", "Daniel Achterberg", "Irina Petrov"],
    ver: [["v2", "12 Oct"], ["v1", "8 Oct"]], weight: "solid", by: "two labs agree",
    brief: { from: "Revision 1 (arXiv v1, 8 Oct). Revision 2 rewrote the abstract on 12 Oct; its Brief is waiting for the text.",
      text: "The authors train Llama 3.1 8B and Qwen 2.5 14B on synthetic classification tasks with known decision rules, then ask each model to state the rule it follows and test the statement causally by editing inputs the stated rule says should matter. Self-reports match the causal test 62% of the time overall, 81% for single-feature rules and 37% for rules combining three features. Fine-tuning on 500 correct self-reports raises agreement to 74% on held-out rules. The main limit is that the rules are synthetic and simple; whether the result carries to rules learned in pretraining is open." },
    card: null },
  "2610.04990": {
    au: ["Mei Zhou", "Kwame Mensah", "Sofia Castillo", "Lukas Novak"],
    ver: [["v1", "12 Oct"]], weight: "incremental", by: "a strong grader, checked",
    abs: "Crosscoders learn one dictionary of features over the activations of several models. We train sparse crosscoders on paired residual-stream activations from Llama 3.1 8B and Qwen 2.5 7B over the same 400M tokens and find that 40% of features are shared at matched scale, concentrated in the middle layers. Shared features transfer between the models as steering vectors with 70% of their in-model effect. We release the crosscoders and a feature browser.",
    card: null },
};
// archive-only papers: what the archive knows, nothing litwatch judged
const AX = {
  "2305.04388": { au: ["Miles Turpin", "Julian Michael", "Ethan Perez", "Samuel R. Bowman"], ver: [["v2", "9 Dec 2023"], ["v1", "7 May 2023"]], pub: "NeurIPS 2023",
    abs: "Chain-of-thought explanations can be plausible and still misrepresent why a model gave its answer. Adding biasing features to few-shot prompts, such as making the answer always (A), pulls models toward the biased answer, and their explanations rationalise it without mentioning the bias. On 13 BIG-Bench Hard tasks accuracy falls by up to 36% under bias toward wrong answers, and on a social-bias task explanations justify stereotyped answers without naming the stereotype." },
};

// what the page shows for a paper: hand-written where it exists, filler otherwise
function paperData(id) {
  const a = ARCH.find((x) => x.id === id);
  if (a) { const x = AX[id] || {}; return { ...a, arch: true, au: x.au || a.au.split(", "), ver: x.ver || [["v1", String(a.y)]], pub: x.pub, abs: x.abs || "The archive's abstract.", tp: [], lv: {} }; }
  const p = P[id] || PAPERS[0], x = PX[id] || {}, R = seeded("pg" + id);
  const focus = p.tp.find((t) => T[t].depth === "focus"), lvl = Math.max(...p.tp.map((t) => p.lv[t]));
  const brief = x.brief || (x.abs || !(focus && p.lv[focus] >= 3) ? null :
    { text: `${p.g} The authors compare against the two closest prior methods on three benchmarks and report the gain with confidence intervals over five seeds. The main limit is scale: every model tested is under 10B parameters.` });
  const card = "card" in x && x.card ? x.card : REQ.has(id) ? { state: "queued", read: "asked for · the archive is fetching the text" }
    : !("card" in x) && focus && p.lv[focus] >= 4 ? { state: "queued", read: "waiting for the text" } : null;
  const [named, more] = p.au.split(" +");
  return { ...p, au: x.au || named.split(", ").concat(NAMES.slice(6, 6 + +(more || 0))),
    ver: x.ver || [["v1", p.d]], weight: x.weight || (lvl >= 5 ? "landmark" : R() < 0.6 ? "solid" : "incremental"), by: x.by || "one grader",
    brief, abs: x.abs || `${p.g} The abstract's remaining sentences describe the setup and list the released artefacts.`, card };
}

/* ── state the sketch keeps: reactions, labels and rung ratings, per paper and topic ── */
const REACT = {}, LABEL = {}, RATE = {}, REQ = new Set();
// the pipeline's open states (round 7b; Settings › Needs you lists the same items): per "id|topic" a split, an
// escalation or ungraded, per id a failed full-text pass or a paper awaiting classification. An item with .done is settled.
const PSTATE = {};
// a list row's mark for its paper's open states: in the row's topic (any topic where the row has none) or on the paper
function rowState(id, topic) {
  const st = [openState(id), ...(topic ? [topic] : (P[id] ? P[id].tp : [])).map((t) => openState(id + "|" + t))].filter(Boolean);
  return st.some((s) => s.k === "split" || s.k === "esc") ? "ask" : st.length ? "fail" : "";
}
// a paper whose analysis failed has no Gist, Weight or Brief until it succeeds
const noAnalysis = (id) => { const s = openState(id); return !!s && s.k === "analysis"; };
const openState = (key) => { const s = PSTATE[key]; return s && !s.done && !((s.k === "split" || s.k === "esc") && LABEL[key]) ? s : null; };
const REASONS = { down: [["wrong", "Wrong topic"], ["over", "Overrated"], ["notint", "Not interesting"], ["knew", "Already knew it"]], up: [["under", "Underrated"], ["great", "Great find"]] };
// how a thumb is given: "reason" asks for the reason first; "thumb" records the thumb at once and offers the reasons after
let REACT_FLOW = "reason";
const whyName = (thumb, why) => (why ? REASONS[thumb].find((r) => r[0] === why)[1] : "");
// the acknowledgement says what happened
function reactAck(pd, topic, thumb, why) {
  const tn = T[topic].n, focus = T[topic].depth === "focus";
  if (!why) return `Noted: thumbs ${thumb} on ${tn}, no reason given.`;
  if (why === "wrong") return focus ? `Marked not on ${tn}; it leaves the library.` : `Noted: wrong topic on ${tn}. Collected for Quality.`;
  if (why === "great") return focus ? `Marked ${FIT[pd.lv[topic]] === "central" ? "central" : "related"} on ${tn}.` : `Noted: great find on ${tn}.`;
  if (why === "knew") return "Noted: already knew it. Not counted as a correction.";
  return `Noted: ${REASONS[thumb].find((r) => r[0] === why)[1].toLowerCase()} on ${tn}. Collected for Quality.`;
}

/* ── pieces ── */
const slug = (pd) => pd.t.toLowerCase().replace(/[^a-z0-9]+/g, "-").split("-").slice(0, 4).join("-");
const codeUrl = (pd) => `https://github.com/${pd.au[0].split(" ").pop().toLowerCase()}/${slug(pd)}`;
const wtsUrl = (pd) => `https://huggingface.co/${pd.au[0].split(" ").pop().toLowerCase()}/${slug(pd)}`;
const ext = (href, label) => `<a class="lnk" href="${href}" target="_blank" rel="noopener">${label} ↗</a>`;
const rate = (id, rung) => { const v = RATE[id + "|" + rung];
  return `<span class="rate" title="Is the ${rung} accurate?"><a class="${v === "ok" ? "on" : ""}" data-rate="${id}|${rung}|ok">✓</a><a class="${v === "off" ? "on bad" : ""}" data-rate="${id}|${rung}|off">✗</a></span>`; };

function pgHead(pd, { acts = false } = {}) {
  const r = pd.arch ? 0 : pd.r;
  const au = pd.au.length > 4 ? `${pd.au.slice(0, 3).join(", ")} <a class="more" data-au>+${pd.au.length - 3}</a>` : pd.au.join(", ");
  const facts = [pd.arch ? `${pd.y}` : pd.d + " 2026", pd.ver[0][0] + (pd.ver.length > 1 ? ` <span class="faint">of ${pd.ver.length}</span>` : ""), pd.kind, pd.pub ? `<b>${pd.pub}</b>` : "",
    pd.code ? ext(codeUrl(pd), "⟨/⟩ code") : "", pd.wts ? ext(wtsUrl(pd), "◆ weights") : "", acts && !pd.arch ? `<a class="lnk" data-bib="${pd.id}">BibTeX ⧉</a>` : ""].filter(Boolean);
  // with the actions in the header: Save (and Deep read while there is no card) at the end of the level line
  const hact = !acts ? "" : pd.arch ? `<span class="sp"></span><a class="hbtn pri" data-process="${pd.id}">Process</a><a class="hbtn" data-deep="${pd.id}">Deep read</a>`
    : `<span class="sp"></span>${pd.card ? "" : `<a class="hbtn" data-deep="${pd.id}">Deep read</a>`}<a class="hbtn${pd.sv ? " on" : ""}" data-save="${pd.id}">${pd.sv ? "★ Saved" : "☆ Save"}</a>`;
  return `<div class="pghead" style="${r ? rc(r) : ""}">
    <div class="mark">${r ? `<span class="lvl">${LEVEL[r]}</span>${pd.tp.map((t) => badge(t)).join("")}` : `<span class="lvl none${openState(pd.id) ? " fail" : ""}">${openState(pd.id) ? "Awaiting classification" : "Not in the collection"}</span>`}${hact}</div>
    <div class="h1">${pd.t}</div>
    <div class="au" data-auall="${esc(pd.au.join(", "))}">${au}</div>
    <div class="facts"><span class="idl">${ext(`https://arxiv.org/abs/${pd.id}`, `<span class="mono">${pd.id}</span>`)}<a class="cp" data-copy="${pd.id}" title="Copy the arXiv id">⧉</a></span>${facts.map((f) => `<span>${f}</span>`).join("")}</div></div>`;
}

function pgActions(pd, { compact = false } = {}) {
  const deep = pd.arch || pd.card ? "" : `<a class="btn" data-deep="${pd.id}">Deep read</a>`;
  const arch = pd.arch ? `<a class="btn pri" data-process="${pd.id}">Process</a><a class="btn" data-deep="${pd.id}">Deep read</a><a class="btn" data-miss="${pd.id}">Report as a miss</a>` : "";
  return `<div class="pgact">${pd.arch ? "" : `<a class="btn sv2${pd.sv ? " on" : ""}" data-save="${pd.id}">${pd.sv ? "★ Saved" : "☆ Save"}</a>`}${arch}${deep}<span class="sp"></span>${compact ? "" : ext(`https://arxiv.org/abs/${pd.id}`, "arXiv") + ext(`https://arxiv.org/pdf/${pd.id}`, "PDF")}<a class="lnk" data-bib="${pd.id}">BibTeX ⧉</a></div>`;
}

// one row per topic the paper is in: its grade, the verdict behind it, thumbs, and a label
function pgTopics(pd, st, { folded = false } = {}) {
  if (pd.arch) return "";
  if (folded && !st.pgopen.includes("topics")) return `<div class="tpfold" data-pgfold="topics">${pd.tp.map((t) => `<span class="tpill" style="${rc(pd.lv[t])}">${badge(t)}<span>${GRADE[pd.lv[t]]}</span></span>`).join("")}<span class="sp"></span><span class="faint">Thumbs · labels ›</span></div>`;
  return `<div class="tplist">${folded ? `<div class="tphd" data-pgfold="topics">▾ Topics</div>` : ""}${pd.tp.map((t) => {
    const k = pd.id + "|" + t, re = REACT[k], lab = LABEL[k], l = pd.lv[t], ps = openState(k), asks = ps && ps.k !== "ungraded";
    const regrade = !ps && PSTATE[k] && PSTATE[k].k === "ungraded"; // asked to grade again: ungraded until the result is in
    const thumbs = ["up", "down"].map((th) => `<a class="th${re && re.thumb === th ? " on" : ""}" data-th="${k}|${th}" title="${th === "up" ? "Thumbs up" : "Thumbs down"}">${th === "up" ? "👍" : "👎"}${re && re.thumb === th && re.why ? `<span>${whyName(th, re.why)}</span>` : ""}</a>`).join("");
    const after = REACT_FLOW === "thumb", ask = after ? `<span class="whyq">Why, if you like:</span>` : "";
    const pick = st.pick === k ? `<div class="why${after ? " after" : ""}">${["up", "down"].includes(st.pickth) ? ask + REASONS[st.pickth].map(([w, n]) => `<a class="${re && re.why === w ? "on" : ""}" data-why="${k}|${st.pickth}|${w}">${n}</a>`).join("") : ""}${re && !after ? `<a class="clr" data-why="${k}|-|-">Clear</a>` : ""}</div>` : "";
    // a split or escalation keeps its Fit control open until it is labelled; ungraded offers to grade again
    const labRow = st.lab === k || asks ? `<div class="why${asks ? " ask" : ""}">${fitSeg(pd, t)}</div>` : "";
    const vd = asks ? `<b class="askt">${ps.k === "split" ? "Split" : "Escalated"}</b> · judge ${ps.judge} · check ${ps.check} · ${FIT[l]} stands`
      : ps ? `<b class="badt">Grading failed 3 runs</b> · <a class="inl" data-settle="${ps.i}|Grading again; the result shows on its topic.">Grade it now</a>`
      : regrade ? "Grading again" : lab ? `<b>your label: ${lab}</b>` : `Fit ${FIT[l]} · Weight ${pd.weight}`;
    return `<div class="tprow${asks ? " ask" : ps ? " fail" : ""}" style="${(ps && !asks) || regrade ? "--rc:var(--fg3)" : rc(l)}"><span class="stripe"></span>${badge(t)}<span class="nm">${T[t].n}</span>
      <span class="gr">${(ps && !asks) || regrade ? "ungraded" : GRADE[l]}</span><span class="vd">${vd}</span>
      <span class="sp"></span>${thumbs}<a class="lb" data-labopen="${k}" title="Label the Fit">Label</a></div>${pick}${labRow}`;
  }).join("")}</div>`;
}

function rung(name, body, extra = "", cls = "") { return `<section class="rung${cls ? " " + cls : ""}"><div class="rl">${name}${extra}</div><div class="rb">${body}</div></section>`; }

function pgCard(pd, st, { folded = false } = {}) {
  const c = pd.card;
  if (pd.arch) return rung("Deep card", `<p class="prose faint">None yet. Deep read processes the paper, then writes its card.</p>`);
  if (!c) return rung("Deep card", `<p class="prose faint">None: cards are automatic for Focus must reads and Area picks. <a class="inl" data-deep="${pd.id}">Deep read</a> asks for one.</p>`);
  if (c.state !== "ready") return rung("Deep card", `<div class="cardwait"><span class="dot"></span>${c.state === "running" ? `Being written · reading ${c.read} · ${c.at}` : `Queued · ${c.read}`}</div>`);
  const ext = c.ext;
  const sec = [
    ["summary", "Summary", c.summary.map((s) => `<p class="prose">${s}</p>`).join(""), c.summary[0].split(". ")[0] + "."],
    ["setup", "Setup", `<div class="kv"><span>Models</span><div>${c.models.map((m) => `<i class="tag">${m}</i>`).join("")}</div><span>Tasks</span><div>${c.tasks.map((m) => `<i class="tag">${m}</i>`).join("")}</div><span>Transfer</span><div>${c.gen}</div></div>`, `${c.models.length} models · ${c.tasks.length} tasks`],
    ["ext", `${T[ext.topic].n}`, `<div class="kv">${ext.facts.map(([k, v]) => `<span>${k}</span><div><i class="tag">${v}</i></div>`).join("")}${ext.text.map(([k, v]) => `<span>${k}</span><div>${v}</div>`).join("")}</div>`, ext.facts.map((f) => f[1]).join(" · ")],
    ["limits", "Limitations", `<ul class="lim">${c.limits.map((l) => `<li>${l}</li>`).join("")}</ul>`, `${c.limits.length} · ${c.limits[0]}`],
    ["prior", "Prior work", c.prior.map(([id, t, rel]) => `<div class="pw${id ? "" : " nolink"}" ${id ? `data-paper="${id}"` : ""}><span class="ti">${t}</span><span class="rel">${rel}</span>${id ? `<span class="mono">${id}</span>` : `<span class="faint">no arXiv id</span>`}</div>`).join(""), `${c.prior.length} papers`],
    ["among", "Among the collection", among(pd).map(([rel, p, why]) => `<div class="pw am" data-paper="${p.id}" style="${rc(p.r)}"><span class="stripe"></span><span class="rtag ${rel}">${rel}</span><span class="ti">${p.t}</span><span class="rel">${why}</span></div>`).join(""), among(pd).map((a) => a[0]).join(" · ")],
  ];
  // unfolded, each section is a rung of its own, so its name can sit in the side column under "Deep card"
  if (!folded) return rung("Deep card", `<p class="claim">${c.claim}</p>`, "", "card") + sec.map(([k, n, html]) => rung(n, html, "", "sub")).join("") +
    rung("", `<div class="cfoot">From ${c.read} · ${c.at} · <a class="inl" data-deep="${pd.id}">Write again</a></div>`, "", "sub end");
  const body = `<p class="claim">${c.claim}</p>` + sec.map(([k, n, html, peek]) => {
    const open = !folded || st.pgopen.includes(k);
    return `<div class="csec${open ? " open" : ""}"><div class="ch"${folded ? ` data-pgfold="${k}"` : ""}>${folded ? `<span class="car">${open ? "▾" : "▸"}</span>` : ""}<span class="cn">${n}</span>${folded && !open ? `<span class="peek">${peek}</span>` : ""}</div>${open ? `<div class="cb">${html}</div>` : ""}</div>`;
  }).join("") + `<div class="cfoot">From ${c.read} · ${c.at} · <a class="inl" data-deep="${pd.id}">Write again</a></div>`;
  return rung("Deep card", body);
}
// the card's place among the collection: named papers, or the newest generated one whose title fits the relation
function among(pd) {
  const pool = COLL.filter((p) => p.id !== pd.id && p.lv.cotf && !PX[p.id]).sort((a, b) => b.di - a.di);
  return pd.card.among.map(([rel, ref, why]) => [rel, typeof ref === "string" ? P[ref] : pool.find((p) => ref.test(p.t)), why]);
}

function pgPaper(pd) {
  const vers = pd.ver.map(([v, d], i) => `<span>${v} · ${d}${i ? "" : " <span class='faint'>(current)</span>"}</span>`).join("");
  return rung("The paper", `<div class="links">${ext(`https://arxiv.org/abs/${pd.id}`, "arXiv page")}${ext(`https://arxiv.org/pdf/${pd.id}`, "PDF")}${ext(`https://arxiv.org/src/${pd.id}`, "LaTeX source")}</div>
    <div class="kv sm"><span>Versions</span><div class="vers">${vers}</div><span>Published</span><div>${pd.pub || `<span class="faint">Preprint only, as far as DBLP knows.</span> <a class="inl" data-doi>Paste a DOI</a>`}</div>${pd.arch ? "" : `<span>Classes</span><div>${pd.tp.map((t) => (PARENT[t] ? T[PARENT[t]].n + " › " : "") + T[t].n).join("<br>")}</div>`}</div>`);
}

// the ladder: Gist, Brief (or the abstract in its place), Deep card, the paper
function pgLadder(pd, st, opt = {}) {
  const gist = pd.arch || opt.nogist || noAnalysis(pd.id) ? "" : rung("Gist", `<p class="prose gist">${pd.g}</p>`, rate(pd.id, "Gist"), "gistr");
  const b = noAnalysis(pd.id) ? null : pd.brief;
  const brief = b ? rung("Brief", `${b.from ? `<div class="oldrev">${b.from}</div>` : ""}${b.over ? `<p class="overst">${b.over}</p>` : ""}<p class="prose">${b.text}</p>`, rate(pd.id, "Brief"))
    : rung("Abstract", `${pd.arch ? "" : `<div class="why0">No Brief: Briefs are written for Focus papers at central or related and for Area must reads, so the abstract stands in.</div>`}<p class="prose">${pd.abs}</p>`);
  return gist + brief + pgCard(pd, st, opt) + (opt.nopaper ? "" : pgPaper(pd));
}

// B's rail: what a reader wants at hand while reading. The Gist (shown once it has scrolled out of the reading
// column), the thumbs and labels per topic, then the quick actions as a grid unless they sit in the header.
function pgRail(pd, st, { acts = "grid" } = {}) {
  const gist = pd.arch || noAnalysis(pd.id) ? "" : `<div class="rgist" style="${rc(pd.r)}"><div class="rl">Gist${rate(pd.id, "Gist")}</div><p>${pd.g}</p></div>`;
  const arch = `<div class="agrid"><a class="pri" data-process="${pd.id}">Process</a><a data-deep="${pd.id}">Deep read</a><a class="wide" data-miss="${pd.id}">Report as a miss</a></div>`;
  const grid = acts !== "grid" ? "" : pd.arch ? arch : pgGrid(pd, ["save", "bib", "arxiv", "pdf"]);
  return gist + pgTopics(pd, st) + grid;
}
// the paper's quick actions and links as a grid of equal cells; Deep read joins while the paper has no card
function pgGrid(pd, keys) {
  const cell = {
    save: `<a class="${pd.sv ? "on" : ""}" data-save="${pd.id}">${pd.sv ? "★ Saved" : "☆ Save"}</a>`,
    bib: `<a data-bib="${pd.id}">BibTeX ⧉</a>`, id: `<a data-copy="${pd.id}">arXiv id ⧉</a>`,
    arxiv: `<a href="https://arxiv.org/abs/${pd.id}" target="_blank" rel="noopener">arXiv ↗</a>`, pdf: `<a href="https://arxiv.org/pdf/${pd.id}" target="_blank" rel="noopener">PDF ↗</a>`,
    latex: `<a href="https://arxiv.org/src/${pd.id}" target="_blank" rel="noopener">LaTeX ↗</a>`,
    code: pd.code ? `<a href="${codeUrl(pd)}" target="_blank" rel="noopener">Code ↗</a>` : "", wts: pd.wts ? `<a href="${wtsUrl(pd)}" target="_blank" rel="noopener">Weights ↗</a>` : "",
  };
  const deep = pd.arch || pd.card ? "" : `<a class="wide" data-deep="${pd.id}">Deep read</a>`;
  return `<div class="agrid">${keys.map((k) => cell[k]).join("")}${deep}</div>`;
}
// a Fit label as one segmented control: the owner's label filled, the pipeline's verdict underlined while unlabelled
function fitSeg(pd, t) {
  const k = pd.id + "|" + t, lab = LABEL[k], auto = FIT[pd.lv[t]];
  return `<div class="fitseg">${["central", "related", "adjacent", "none", "unsure"].map((f) => `<a class="${lab === f ? "on" : ""}${!lab && auto === f ? " auto" : ""}" data-lab="${k}|${f}">${f}</a>`).join("")}</div>`;
}

// the phone's paper sheet: a label per topic, the actions and links as a grid, then the paper's facts
function pgSheet(pd, enter) {
  const labs = pd.arch ? "" : `<div class="cap2">Label</div>` + pd.tp.map((t) => {
    const k = pd.id + "|" + t, l = pd.lv[t], ps = openState(k), asks = ps && ps.k !== "ungraded";
    const ss = asks ? `<b class="askt">${ps.k === "split" ? "Split" : "Escalated"}</b> · judge ${ps.judge} · check ${ps.check} · ${FIT[l]} stands`
      : ps ? `<b class="badt">ungraded</b> · grading failed 3 runs` : `<span class="gr">${GRADE[l]}</span>${LABEL[k] ? ` · your label` : ` · pipeline: ${FIT[l]}`}`;
    return `<div class="slab${asks ? " ask" : ""}" style="${ps && !asks ? "--rc:var(--fg3)" : rc(l)}"><div class="sh">${badge(t)}<span class="nm">${T[t].n}</span></div>
      <div class="ss">${ss}</div>${ps && !asks ? "" : fitSeg(pd, t)}</div>`;
  }).join("");
  const vers = pd.ver.map(([v, d], i) => `${v} · ${d}${i ? "" : " (current)"}`).join("<br>");
  return `<div class="fsheet${enter ? " enter" : ""}"><div class="shade2" data-ps="x"></div><div class="sheetp">
    ${labs}
    <div class="cap2">The paper</div>${pgGrid(pd, ["arxiv", "pdf", "latex", "code", "wts", "bib"])}${pd.arch ? `<div class="agrid"><a class="wide" data-miss="${pd.id}">Report as a miss</a></div>` : ""}
    <div class="kv sm"><span>Versions</span><div>${vers}</div><span>Published</span><div>${pd.pub || `<span class="faint">Preprint only, as far as DBLP knows.</span> <a class="inl" data-doi>Paste a DOI</a>`}</div>${pd.arch ? "" : `<span>Classes</span><div>${pd.tp.map((t) => (PARENT[t] ? T[PARENT[t]].n + " › " : "") + T[t].n).join("<br>")}</div>`}</div>
    <button class="done" data-ps="x">Done</button></div></div>`;
}
