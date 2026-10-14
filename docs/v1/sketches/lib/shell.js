// The shell chosen in round 1, shared by every later round: the desktop sidebar (B) and the phone drawer (C).
// A round passes an app: { start, view(st) -> { body, title?, overlay? }, click?(e, nav) -> true when handled }.
// Navigation is a stack: nav.go() pushes a step, nav.back() undoes one, nav.set() changes the page in place
// (scroll kept), nav.top() jumps to a top-level screen. On a phone-sized page the steps are real history
// entries, so the OS back gesture walks them too.

const SHELL_TABS = ["Digest", "Topics", "Papers"];
// the run status sits in the wordmark: last night's count, with a dot that turns red when something failed
const SHELL_WM = `litwatch<span class="st" title="530 papers read last night · all quiet">530</span>`;

function makeNav(app, draw, useHistory) {
  let skipPop = 0;
  const nav = {
    st: { ...app.start }, stack: [],
    go(patch) { nav.stack.push(nav.st); nav.st = { ...nav.st, ...patch }; if (useHistory) history.pushState({}, ""); draw("go"); },
    set(patch) { nav.st = { ...nav.st, ...patch }; draw("keep"); },
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

function shellNavItems(st, cls) {
  const tabOn = (t) => st.tab === t && !st.topic;
  return SHELL_TABS.map((t) => `<div class="it${tabOn(t) ? " on" : ""}" data-tab="${t}">${t}</div>`).join("") +
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
    el.innerHTML = `<div class="body"><div class="side"><div class="wm">${SHELL_WM}</div>${shellNavItems(nav.st, "side")}<div class="foot"><div class="it">⚙ Settings</div></div></div>
      <div class="main scroll"><div class="inner">${backLink}${v.body}</div></div>
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
      : `<button class="ib" data-menu>☰</button><span class="wm">${SHELL_WM}</span>`;
    return `<div class="pbar">${left}<span class="sp"></span><button class="ib">⚙</button></div><div class="scroll"><div class="pp">${v.body}${v.overlay || ""}</div></div>`;
  }
  function draw(how) {
    const sc = scr.querySelector(".scroll"), keep = how === "keep" && sc ? sc.scrollTop : 0;
    scr.innerHTML = screenHTML(nav.st, nav.stack.length);
    scr.querySelector(".scroll").scrollTop = keep;
    panel.innerHTML = `<div class="wm">${SHELL_WM}</div>${shellNavItems(nav.st, "drawer")}<div class="it" style="margin-top:auto">⚙ Settings</div>`;
    scr.classList.remove("in-go", "in-back"); void scr.offsetWidth;
    if (how === "go" || how === "back") scr.classList.add("in-" + how);
    if (pos > 0) settle(0);
  }

  // Two edge gestures. On a top-level screen the drawer follows the finger; on a deeper screen the page does,
  // with the previous one under it, iOS style. Either settles by where the finger let go and how fast.
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
  function endSlide(commit, v) {
    const p = parseFloat(scr.style.transform.slice(11)) / 100 || 0, ms = dur(Math.abs((commit ? 1 : 0) - p) * el.offsetWidth, v);
    slide(commit ? 1 : 0, ms);
    setTimeout(() => { if (commit) nav.back("swiped"); slide(0); under.style.display = "none"; }, reduce ? 0 : ms);
  }

  el.addEventListener("pointerdown", (e) => {
    if (e.button > 0 || g) return;
    const x = e.clientX - el.getBoundingClientRect().left, shut = pos === 0;
    if (shut && x > edge()) return;
    if (!shut && !drawer.contains(e.target)) return;
    const mode = !shut ? "drawer" : nav.stack.length ? "page" : "drawer";
    g = { id: e.pointerId, x0: e.clientX, y0: e.clientY, p0: pos, mode, drag: false, s: [[e.timeStamp, e.clientX]] };
  });
  el.addEventListener("pointermove", (e) => {
    if (!g || e.pointerId !== g.id) return;
    const dx = e.clientX - g.x0, dy = e.clientY - g.y0;
    if (!g.drag) {
      if (Math.hypot(dx, dy) < 8) return;
      const horizontal = Math.abs(dx) > Math.abs(dy) * 1.2, rightWay = g.p0 === 0 ? dx > 0 : dx < 0;
      if (!horizontal || !rightWay) { g = null; return; }
      g.drag = true; g.x0 = e.clientX;
      try { el.setPointerCapture(e.pointerId); } catch {}
      if (g.mode === "page") { under.innerHTML = screenHTML(nav.stack.at(-1), nav.stack.length - 1); under.style.display = "flex"; }
    }
    g.s.push([e.timeStamp, e.clientX]);
    while (g.s.length > 2 && e.timeStamp - g.s[0][0] > 90) g.s.shift();
    const d = e.clientX - g.x0;
    if (g.mode === "drawer") place(g.p0 + d / panel.offsetWidth);
    else slide(Math.max(0, d / el.offsetWidth));
  });
  function end(e) {
    if (!g || e.pointerId !== g.id) return;
    if (g.drag) {
      const [t0, x0] = g.s[0], [t1, x1] = g.s.at(-1), v = t1 > t0 ? (x1 - x0) / (t1 - t0) : 0;
      const cancel = e.type === "pointercancel";
      if (g.mode === "drawer") settle(cancel ? Math.round(pos) : v > 0.35 ? 1 : v < -0.35 ? 0 : pos > 0.5 ? 1 : 0, v);
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
