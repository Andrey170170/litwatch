// The shell chosen in round 1, shared by every later round: the desktop sidebar (B) and the phone drawer (C).
// A round passes an app: { start, view(st) -> { body, title?, overlay?, bar?, barRight?, foot? }, click?(e, nav) -> true
// when handled, sub?(st) -> drawer items under Digest, swipe?(st) -> { older, newer } patches or null }. On the
// phone, bar replaces the wordmark on a top-level screen, barRight goes before the run status and ⚙ at the right,
// foot is pinned under the scroll, and a horizontal swipe away from the edge steps to swipe's older or newer page.
// Navigation is a stack: nav.go() pushes a step, nav.back() undoes one, nav.set() changes the page in place
// (scroll kept), nav.top() jumps to a top-level screen. On a phone-sized page the steps are real history
// entries, so the OS back gesture walks them too.

const SHELL_TABS = ["Digest", "Topics", "Papers"];
// The run status sits in the wordmark: last night's count when all went well, the progress while it runs,
// and the problem when something failed. SHELL_RUN is a sketch switch.
let SHELL_RUN = "ok";
function shellStatus() {
  if (SHELL_RUN === "running") return `<span class="st run" title="Tonight's run: 212 of 530 papers read">212/530</span>`;
  if (SHELL_RUN === "failed") return `<span class="st bad" title="2 digests failed last night · open Quality">2 failed</span>`;
  return `<span class="st" title="530 papers read last night · all quiet">530</span>`;
}
const shellWM = () => `litwatch${shellStatus()}`;

function makeNav(app, draw, useHistory) {
  let skipPop = 0;
  const nav = {
    st: { ...app.start }, stack: [],
    go(patch) { nav.stack.push(nav.st); nav.st = { ...nav.st, ...patch }; if (useHistory) history.pushState({}, ""); draw("go"); },
    set(patch, how = "keep") { nav.st = { ...nav.st, ...patch }; draw(how); },
    top(patch) { nav.stack = []; nav.st = { ...app.start, ...patch }; draw("top"); },
    back(how = "back") {
      if (!nav.stack.length) return;
      nav.st = nav.stack.pop(); draw(how);
      if (useHistory) { skipPop++; history.back(); }
    },
  };
  if (useHistory) addEventListener("popstate", () => {
    if (skipPop) { skipPop--; return; }
    if (nav.stack.length) { nav.st = nav.stack.pop(); draw("back"); }
  });
  return nav;
}

// clicks every round shares; a round's own handler runs first
function shellClick(e, nav, app) {
  if (app.click && app.click(e, nav)) return;
  const f = (s) => e.target.closest(s), inNav = f(".side, .drawer .panel");
  let n;
  if ((n = f("[data-tab]"))) nav.top({ tab: n.dataset.tab });
  else if ((n = f("[data-topic]"))) inNav ? nav.top({ tab: null, topic: n.dataset.topic }) : nav.go({ topic: n.dataset.topic, paper: null });
  else if ((n = f("[data-paper]"))) nav.go({ paper: n.dataset.paper });
  else if (f("[data-back]")) nav.back();
}

function shellNavItems(st, cls, app) {
  const tabOn = (t) => st.tab === t && !st.topic;
  const sub = (t) => (t === "Digest" && cls === "drawer" && app && app.sub ? app.sub(st) : "");
  return SHELL_TABS.map((t) => `<div class="it${tabOn(t) ? " on" : ""}" data-tab="${t}">${t}</div>${sub(t)}`).join("") +
    `<div class="sec">Following</div>` +
    TOPICS.map((t) => { const x = DIGEST[t.id]; const n = cls === "side" && x && !x.quiet ? x.c.split(" ")[0] : "";
      return `<div class="it${st.topic === t.id ? " on" : ""}" data-topic="${t.id}">${badge(t.id)}<span class="nm">${t.n}</span>${n ? `<span class="k${t.depth === "focus" ? " hot" : ""}">${n}</span>` : ""}</div>`; }).join("");
}

function mountDesk(el, app) {
  let hadOverlay = false;
  const nav = makeNav(app, draw, false);
  function draw(how) {
    const main = el.querySelector(".main"), keep = how === "keep" && main ? main.scrollTop : 0;
    const v = app.view(nav.st, nav);
    const backLink = nav.stack.length && !v.overlay && !v.noBack ? `<a class="back" data-back>‹ Back</a>` : "";
    // the main column says how it was reached (nav-go, nav-back, nav-top, nav-keep) so a round can animate it;
    // opening or closing a slide-over leaves the page under it as it was
    const came = v.overlay || hadOverlay ? "keep" : how;
    el.innerHTML = `<div class="body"><div class="side"><div class="wm">${shellWM()}</div>${shellNavItems(nav.st, "side")}<div class="foot"><div class="it">⚙ Settings</div></div></div>
      <div class="main scroll nav-${came}"><div class="inner">${backLink}${v.body}</div></div>
      ${v.overlay ? `<div class="over${hadOverlay ? "" : " enter"}"><div class="shade" data-back></div><div class="sheet scroll">${v.overlay}</div></div>` : ""}</div>`;
    hadOverlay = !!v.overlay;
    el.querySelector(".main").scrollTop = keep;
  }
  el.addEventListener("click", (e) => shellClick(e, nav, app));
  addEventListener("keydown", (e) => { if (e.key === "Escape" && hadOverlay) nav.back(); });
  draw("top");
  return nav;
}

function mountPhone(el, app, { edge = () => 40 } = {}) {
  const nav = makeNav(app, draw, matchMedia("(max-width: 640px)").matches);
  el.innerHTML = `<div class="under"></div><div class="screen"></div><div class="edgehint"></div><div class="drawer"><div class="shade"></div><div class="panel"></div></div>`;
  const scr = el.querySelector(".screen"), under = el.querySelector(".under"), drawer = el.querySelector(".drawer"),
    panel = drawer.querySelector(".panel"), shade = drawer.querySelector(".shade");

  function screenHTML(st, depth) {
    const v = app.view(st, nav);
    const left = depth ? `<button class="ib" data-back>‹</button><span class="ttl">${v.title || ""}</span>`
      : `<button class="ib" data-menu>☰</button>${v.bar || `<span class="wm">litwatch</span>`}`;
    return `<div class="pbar">${left}<span class="sp"></span>${v.barRight || ""}${shellStatus()}<button class="ib">⚙</button></div><div class="scroll"><div class="pp">${v.body}${v.overlay || ""}</div></div>${v.foot || ""}`;
  }
  function draw(how) {
    const sc = scr.querySelector(".scroll"), keep = how === "keep" && sc ? sc.scrollTop : 0;
    scr.innerHTML = screenHTML(nav.st, nav.stack.length);
    scr.querySelector(".scroll").scrollTop = keep;
    panel.innerHTML = `<div class="wm">${shellWM()}</div>${shellNavItems(nav.st, "drawer", app)}<div class="it" style="margin-top:auto">⚙ Settings</div>`;
    scr.classList.remove("in-go", "in-back"); void scr.offsetWidth;
    if (how === "go" || how === "back") scr.classList.add("in-" + how);
    if (how === "older" || how === "newer") scr.querySelectorAll(".pp, .pfoot .lbl").forEach((x) => x.classList.add("in-" + how));
    if (pos > 0) settle(0);
  }

  // Two edge gestures. On a top-level screen the drawer follows the finger; on a deeper screen the page does,
  // with the previous one under it, iOS style. Either settles by where the finger let go and how fast.
  // Away from the edge, a horizontal swipe on a page with swipe() steps it: right to older, left to newer.
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let pos = 0, g = null, clickBlockUntil = 0;
  const ease = "cubic-bezier(.2, .85, .25, 1)";
  const tr = (ms) => (reduce || !ms ? "none" : `transform ${ms}ms ${ease}, opacity ${ms}ms ${ease}`);
  function place(p, ms = 0) {
    pos = Math.max(0, Math.min(1, p));
    panel.style.transition = shade.style.transition = tr(ms);
    panel.style.transform = `translateX(${(pos - 1) * 100}%)`;
    shade.style.opacity = pos;
    drawer.classList.toggle("live", pos > 0);
  }
  const dur = (px, v) => (Math.abs(v) > 0.3 ? Math.min(300, Math.max(120, (px / Math.abs(v)) * 1.6)) : 280);
  function settle(to, v = 0) { place(to, dur(Math.abs(to - pos) * panel.offsetWidth, v)); }
  function slide(p, ms = 0) {
    scr.style.transition = under.style.transition = tr(ms);
    scr.style.transform = p ? `translateX(${p * 100}%)` : "";
    under.style.transform = `translateX(${(p - 1) * 30}%)`;
    scr.classList.toggle("lifted", p > 0);
  }
  // the page content (and the bottom bar's label) follow the finger; past a quarter, or flicked, the next one comes in
  const stepEls = () => scr.querySelectorAll(".pp, .pfoot .lbl");
  function step(px, ms = 0) { stepEls().forEach((x) => { x.style.transition = tr(ms); x.style.transform = px ? `translateX(${px}px)` : ""; x.style.opacity = 1 - Math.min(0.6, Math.abs(px) / el.offsetWidth); }); }
  function endStep(d, v, cancel) {
    const W = el.offsetWidth, dir = d > 0 ? "older" : "newer", to = g.sw[dir];
    const commit = !cancel && to && (Math.abs(d) > W / 4 || (Math.abs(v) > 0.35 && Math.sign(v) === Math.sign(d)));
    if (!commit) return step(0, 200);
    const ms = reduce ? 0 : 150;
    step(Math.sign(d) * W, ms);
    setTimeout(() => nav.set(to, dir), ms);
  }
  function endSlide(commit, v) {
    const p = parseFloat(scr.style.transform.slice(11)) / 100 || 0, ms = dur(Math.abs((commit ? 1 : 0) - p) * el.offsetWidth, v);
    slide(commit ? 1 : 0, ms);
    setTimeout(() => { if (commit) nav.back("swiped"); slide(0); under.style.display = "none"; }, reduce ? 0 : ms);
  }

  el.addEventListener("pointerdown", (e) => {
    if (e.button > 0 || g) return;
    const x = e.clientX - el.getBoundingClientRect().left, shut = pos === 0;
    let mode, sw = null;
    if (!shut) { if (!drawer.contains(e.target)) return; mode = "drawer"; }
    else if (x <= edge()) mode = nav.stack.length ? "page" : "drawer";
    else if (app.swipe && (sw = app.swipe(nav.st)) && scr.contains(e.target) && !e.target.closest(".pbar, .calscroll")) mode = "step";
    else return;
    g = { id: e.pointerId, x0: e.clientX, y0: e.clientY, p0: pos, mode, sw, drag: false, s: [[e.timeStamp, e.clientX]] };
  });
  el.addEventListener("pointermove", (e) => {
    if (!g || e.pointerId !== g.id) return;
    const dx = e.clientX - g.x0, dy = e.clientY - g.y0;
    if (!g.drag) {
      if (Math.hypot(dx, dy) < 8) return;
      const horizontal = Math.abs(dx) > Math.abs(dy) * 1.2, rightWay = g.mode === "step" || (g.p0 === 0 ? dx > 0 : dx < 0);
      if (!horizontal || !rightWay) { g = null; return; }
      g.drag = true; g.x0 = e.clientX;
      try { el.setPointerCapture(e.pointerId); } catch {}
      if (g.mode === "page") { under.innerHTML = screenHTML(nav.stack.at(-1), nav.stack.length - 1); under.style.display = "flex"; }
    }
    g.s.push([e.timeStamp, e.clientX]);
    while (g.s.length > 2 && e.timeStamp - g.s[0][0] > 90) g.s.shift();
    const d = e.clientX - g.x0;
    if (g.mode === "drawer") place(g.p0 + d / panel.offsetWidth);
    else if (g.mode === "step") step((d > 0 ? g.sw.older : g.sw.newer) ? d : d * 0.25); // resist where nothing lies
    else slide(Math.max(0, d / el.offsetWidth));
  });
  function end(e) {
    if (!g || e.pointerId !== g.id) return;
    if (g.drag) {
      const [t0, x0] = g.s[0], [t1, x1] = g.s.at(-1), v = t1 > t0 ? (x1 - x0) / (t1 - t0) : 0;
      const cancel = e.type === "pointercancel";
      if (g.mode === "drawer") settle(cancel ? Math.round(pos) : v > 0.35 ? 1 : v < -0.35 ? 0 : pos > 0.5 ? 1 : 0, v);
      else if (g.mode === "step") endStep(e.clientX - g.x0, v, cancel);
      else { const p = parseFloat(scr.style.transform.slice(11)) / 100 || 0; endSlide(!cancel && (v > 0.35 || (v > -0.35 && p > 0.4)), v); }
      clickBlockUntil = performance.now() + 350;
    }
    g = null;
  }
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
  el.addEventListener("click", (e) => {
    if (performance.now() < clickBlockUntil) return;
    if (e.target === shade) return settle(0);
    if (e.target.closest("[data-menu]")) return settle(1);
    shellClick(e, nav, app);
  });
  addEventListener("keydown", (e) => { if (e.key === "Escape" && pos > 0) settle(0); });
  draw("top"); place(0);
  return nav;
}
