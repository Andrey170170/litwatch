// Injected into every REPL session before the first cell. Plain script, no module syntax:
// top-level `var`/`function` here become globals the cells can see. `paper`, `__meta` and
// `__headings` are set by the host before this runs; `_print` and `_host` are the only bridges out.

var __last;
var meta = JSON.parse(__meta);

function repr(v) {
  if (v === undefined) return 'undefined';
  if (typeof v === 'string') return v;
  if (typeof v === 'function') return '[function ' + (v.name || 'anonymous') + ']';
  try {
    var s = JSON.stringify(v, function (k, x) { return typeof x === 'bigint' ? String(x) : x; }, 1);
    return s === undefined ? String(v) : s;
  } catch (e) {
    return String(v);
  }
}

var console = {
  log: function () {
    _print(Array.prototype.map.call(arguments, function (x) { return typeof x === 'string' ? x : repr(x); }).join(' '));
  },
};
console.info = console.log;
console.warn = console.log;
console.error = console.log;
console.debug = console.log;

// Error text for the cell wrapper; the wrapper adds one line above the cell's code.
function __errText(e) {
  if (!(e instanceof Error)) return String(e);
  var m = /cell-\d+\.js:(\d+):(\d+)/.exec(e.stack || '');
  var loc = m ? ' (line ' + (Number(m[1]) - 1) + ', column ' + m[2] + ')' : '';
  return e.name + ': ' + e.message + loc;
}

function __takeLast() {
  var v = __last;
  __last = undefined;
  return v === undefined ? '' : repr(v);
}

async function __call(name, args) {
  var raw = await _host.apply(undefined, [name, JSON.stringify(args)], { result: { promise: true } });
  var r = JSON.parse(raw);
  if (r.error) throw new Error(r.error);
  return r.value;
}

// -- text helpers over `paper` -------------------------------------------------------------------
// `paper` is LaTeX source (inputs inlined, the bibliography in place), or the PDF's text as markdown.

// The paper's headings, found by the service (src/paper.ts) and the same ones its checks look places up in:
// {level, title, number?, start, end, chars}. A fresh copy each call, so a cell cannot change them for the next.
function sections() { return JSON.parse(__headings); }

function section(title) {
  var t = String(title).toLowerCase(), all = sections();
  var hit = all.find(function (s) { return s.number !== undefined && s.number.toLowerCase() === t; }) ||
    all.find(function (s) { return s.title.toLowerCase() === t; }) ||
    all.find(function (s) { return s.title.toLowerCase().indexOf(t) !== -1; });
  if (!hit) throw new Error('no section matching ' + JSON.stringify(title) + '; see sections()');
  return paper.slice(hit.start, hit.end);
}

function find(pattern, context, limit) {
  context = context === undefined ? 200 : context;
  limit = limit || 20;
  var re = pattern instanceof RegExp
    ? new RegExp(pattern.source, pattern.flags.indexOf('g') === -1 ? pattern.flags + 'g' : pattern.flags)
    : new RegExp(String(pattern).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
  var out = [], m, all = sections();
  while ((m = re.exec(paper)) !== null && out.length < limit) {
    var sec = '';
    for (var i = all.length - 1; i >= 0; i--) if (all[i].start <= m.index) { sec = all[i].title; break; }
    out.push({ index: m.index, section: sec, text: paper.slice(Math.max(0, m.index - context), m.index + m[0].length + context) });
    if (m[0].length === 0) re.lastIndex++;
  }
  return out;
}

// The bibliography's entries: each \bibitem of a thebibliography environment, else each "- " line
// under a References or Bibliography heading. Empty for a bibliography in another form (biblatex).
// LaTeX comments are dropped first, so a commented-out \bibitem is no entry; an escaped \% stays.
function refs() {
  var tex = /\\begin\{thebibliography\}/.exec(paper);
  if (tex) {
    var close = paper.indexOf('\\end{thebibliography}', tex.index);
    return paper.slice(tex.index, close === -1 ? paper.length : close)
      .replace(/(^|[^\\])%.*$/gm, '$1')
      .split(/\\bibitem\s*(?:\[[^\]]*\])?\s*\{[^}]*\}/).slice(1)
      .map(function (e) { return e.replace(/\\newblock\s*/g, '').replace(/\s+/g, ' ').trim(); })
      .filter(function (e) { return e.length > 0; });
  }
  var hit = sections().find(function (s) { return /^(references|bibliography)$/i.test(s.title); });
  if (!hit) return [];
  return paper.slice(hit.start, hit.end).split('\n')
    .filter(function (l) { return /^- /.test(l); })
    .map(function (l) { return l.slice(2).trim(); });
}

var store = { search: function (query, limit) { return __call('store.search', [query, limit || 10]); } };
var s2 = { lookup: function (q) { return __call('s2.lookup', [q]); } };
function fetch(url) { return __call('fetch', [url]); }

// -- the paper's figures, and looking at the draft -----------------------------------------------
// Each image these make comes back with the output of the cell that called them.

function figures() { return __call('figures', []); }
function figure(file, name, number) { return __call('figure', [file, name, number]); }
function pdfPage(n) { return __call('pdfPage', [n]); }
function crop(page, box, name, number) { return __call('crop', [page, box, name, number]); }
function render(read, target, width) { return __call('render', [read, target, width]); }
function check(read, standBy) { return __call('check', standBy === undefined ? [read] : [read, standBy]); }
function submit(read, standBy) { return __call('submit', standBy === undefined ? [read] : [read, standBy]); }
