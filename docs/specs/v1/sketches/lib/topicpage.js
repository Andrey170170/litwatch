// The topic page, Papers and Topics as chosen in round 3, shared by later rounds. Reads the page's switches from a
// global SW ({ v, bar, cards }). Loads after papers.js.
const FD = { q: "", rset: [], saved: false, code: false, tf: [], sort: "new", n: 40, open: [], fs: false };
const esc = (s) => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
const CADENCE = { focus: "daily and weekly digests", area: "daily and weekly digests", field: "weekly digest" };
const nfilt = (st, papers) => (st.rset.length ? 1 : 0) + st.saved + st.code + (papers && st.tf.length ? 1 : 0);

// the rows in the chosen form, with a loader while more exist
function rows(list, st, topic) {
  if (!list.length) return `<div class="empty">Nothing matches. <a data-clear>Clear the filters</a></div>`;
  if (SW.v === "C") return `<div class="wideview">${shelfRows(list, topic, st)}</div>`;
  const shown = list.slice(0, st.n), more = list.length > st.n ? `<div class="loadmore">Loading more…</div>` : `<div class="archstart">${list.length} papers</div>`;
  return SW.v === "A" ? `<div class="wideview">${tableRows(shown, topic)}</div>${more}` : readingRows(shown, topic, { group: st.sort === "new" }) + more;
}
function controls(st, topic, phone) {
  if (phone) return "";
  const n = listFor(st, topic).length;
  const inl = SW.bar === "inline";
  const q = `<label class="qbox${topic ? (inl ? " short" : "") : " big"}">⌕<input data-q value="${esc(st.q)}" placeholder="${topic ? "Search in " + T[topic].n : "Search the collection and the archive (3.1M papers)"}"></label>`;
  return (topic ? "" : `<div class="ctlrow">${q}</div><div class="ctlrow">${topicToggles(st)}</div>`) + (inl ? "" : rarityBar(st, topic)) +
    `<div class="ctlrow">${topic ? q : ""}${chip(st, "saved", topic ? "★ Saved" : "★ Saved · the library")}${chip(st, "code", "⟨/⟩ Code")}${inl ? rarityBar(st, topic, { inline: true }) : '<span class="sp"></span>'}<span class="cnt">${n} papers</span>${sortSeg(st)}</div>`;
}
// on the phone: search and the filter button in the bottom bar; the filters in a sheet over it, which animates only
// as it opens
let sheetOpen = false;
function phoneFoot(st, topic) {
  const nf = nfilt(st, !topic), enter = st.fs && !sheetOpen;
  sheetOpen = st.fs;
  const sheet = !st.fs ? "" : `<div class="fsheet${enter ? " enter" : ""}"><div class="shade2" data-fs></div><div class="sheetp">
    ${topic ? "" : `<div class="cap2">Topics</div>${topicToggles(st)}`}
    <div class="cap2">Rarity</div>${rarityBar(st, topic)}
    <div class="cap2">Show</div><div class="row">${chip(st, "saved", "★ Saved")}${chip(st, "code", "⟨/⟩ Code")}</div>
    <div class="cap2">Sort</div><div class="row">${sortSeg(st)}<span style="flex:1"></span><span class="cnt">${listFor(st, topic).length} papers</span></div>
    <button class="done" data-fs>Done</button></div></div>`;
  return `<div class="pfoot qbar"><label class="qbox">⌕<input data-q value="${esc(st.q)}" placeholder="${topic ? "Search in " + T[topic].n : "Search papers"}"></label><button class="fbtn" data-fs>⧩${nf ? `<i>${nf}</i>` : ""}</button></div>${sheet}`;
}

function topicV(st, phone) {
  const id = st.topic, t = T[id];
  const head = `<div class="thead">${badge(id, "lg")}<div><div class="dep">${t.depth} · ${CADENCE[t.depth]}</div><div class="h1">${t.n}</div></div></div><p class="tsub">${SUBJECT[id]}</p>${digestStrip(id)}`;
  return { body: head + controls(st, id, phone) + rows(listFor(st, id), st, id), title: t.n, bar: `<span class="ttl2">${badge(id)}<span class="t">${t.n}</span></span>`, foot: phone ? phoneFoot(st, id) : "" };
}
function papersV(st, phone) {
  const list = listFor(st, null);
  const out = st.q ? ARCH.filter((a) => matches(a, st.q)) : [];
  const band = !st.q ? "" : `<div class="outh">Outside the collection · from the archive<span class="cnt">${out.length}</span></div>` +
    (out.length ? out.map((a) => `<div class="orow"><span class="ti" data-paper="${a.id}">${a.t}<br><span class="au">${a.au} · ${a.y} · ${a.id}</span></span><a class="btn" data-process="${a.id}">Process</a></div>`).join("") : `<div class="empty">No archive hits for "${esc(st.q)}".</div>`);
  return { body: (phone ? "" : `<div class="h1">Papers</div>`) + controls(st, null, phone) + (list.length || !st.q ? rows(list, st, null) : "") + band, title: "Papers", bar: `<span class="ttl2">Papers</span>`, foot: phone ? phoneFoot(st, null) : "" };
}
function topicsV() {
  // plain: name, counts, recent digests; rich adds the topic's levels as a thin bar and its newest must read
  const card = (t) => { const L = COLL.filter((p) => p.lv[t.id]), s = L.filter((p) => p.sv).length;
    const top = L.filter((p) => p.lv[t.id] >= 4).sort((a, b) => b.di - a.di)[0];
    const rich = SW.cards === "rich" ? rarityThin(t.id) + (top ? `<div class="nm2"><span class="k2">Newest must read</span><span class="t2" data-paper="${top.id}" title="${esc(top.t)}">${top.t}</span></div>` : "") : "";
    return `<div class="tcard2" data-topic="${t.id}"><div class="hd">${badge(t.id, "lg")}<div><div class="n" title="${t.n}">${t.n}</div><div class="s">${L.length} papers · ${s} saved</div></div></div>${rich}${digestStrip(t.id)}</div>`; };
  return { body: ["focus", "area", "field"].map((d) => `<div class="cap" style="margin-top:6px">${d}</div><div class="tgrid">${TOPICS.filter((t) => t.depth === d).map(card).join("")}</div>`).join(""), title: "Topics", bar: `<span class="ttl2">Topics</span>` };
}

const scroller = (frame) => frame.querySelector(".main, .screen > .scroll");
// the list grows as you near its end
function autoLoad(frame, nav) {
  let busy = false;
  frame.addEventListener("scroll", () => {
    const sc = scroller(frame);
    if (busy || !sc || !sc.querySelector(".loadmore") || sc.scrollHeight - sc.scrollTop - sc.clientHeight > 300) return;
    busy = true;
    setTimeout(() => { nav.set({ n: nav.st.n + 40 }); busy = false; }, 350);
  }, true);
}
// typing filters as you go; the redraw replaces the input, so focus and caret are put back
function liveSearch(frame, nav) {
  frame.addEventListener("input", (e) => {
    if (!e.target.matches("[data-q]")) return;
    const q = e.target.value, at = e.target.selectionStart, st = nav.st;
    const sort = q && !st.q && st.sort === "new" && !st.topic ? "match" : !q && st.sort === "match" ? "new" : st.sort;
    nav.set({ q, sort, n: 40 });
    const inp = frame.querySelector(".screen [data-q], .main [data-q]");
    if (inp) { inp.focus(); inp.setSelectionRange(at, at); }
  });
}
