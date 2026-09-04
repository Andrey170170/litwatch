// Injected into every REPL session before the first cell. Plain script, no module syntax:
// top-level `var`/`function` here become globals the cells can see. `paper` and `__meta` are
// set by the host before this runs; `_print` and `_host` are the only bridges out.

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

// -- text helpers over `paper` (markdown) --------------------------------------------------

function sections() {
  var out = [], re = /^(#{1,6})[ \t]+(.*)$/gm, m;
  while ((m = re.exec(paper)) !== null) out.push({ level: m[1].length, title: m[2].trim(), start: m.index });
  for (var i = 0; i < out.length; i++) {
    var end = paper.length;
    for (var j = i + 1; j < out.length; j++) if (out[j].level <= out[i].level) { end = out[j].start; break; }
    out[i].end = end;
    out[i].chars = end - out[i].start;
  }
  return out;
}

function section(title) {
  var t = String(title).toLowerCase(), all = sections();
  var hit = all.find(function (s) { return s.title.toLowerCase() === t; }) ||
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

function refs() {
  var hit = sections().find(function (s) { return /^(references|bibliography)$/i.test(s.title); });
  if (!hit) return [];
  return paper.slice(hit.start, hit.end).split('\n')
    .filter(function (l) { return /^- /.test(l); })
    .map(function (l) { return l.slice(2).trim(); });
}

var store = { search: function (query, limit) { return __call('store.search', [query, limit || 10]); } };
var s2 = { lookup: function (q) { return __call('s2.lookup', [q]); } };
function fetch(url) { return __call('fetch', [url]); }
function submit(result) { return __call('submit', [result]); }
