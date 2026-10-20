import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import dns from "node:dns";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { after, before, test } from "node:test";
import { buildHosts, fetchPublic, guardedLookup, htmlToText, isPublic, pacer, makeGuard, rewriteAnonymous, s2Id, storeHits } from "../src/hosts.ts";

test("store.search asks v1's papers table with the read token and keeps the old hit shape", async () => {
  const asked: { url: string; auth: string | null }[] = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    asked.push({ url: String(url), auth: new Headers(init?.headers).get("authorization") });
    return Response.json({
      rows: [
        { id: "2610.00001", title: "Hidden steps", authors: ["Ann Lee"], published: "2026-10-01T00:00:00Z", badges: [{ grade: "must_read" }, { grade: "adjacent" }], gist: "It finds them." },
        { id: "2610.00002", title: "Near it", authors: [], published: null, badges: [], gist: null },
      ],
      total: 2,
      outside: [{ id: "2401.00009", title: "Only in the archive", authors: ["Bo Chen"], published: "2024-01-05T00:00:00Z", source: "archive" }],
      left_out: [],
    });
  }) as typeof fetch;
  try {
    const hosts = buildHosts({ litwatchUrl: "http://litwatch.test", litwatchToken: "r3ad", maxFetchChars: 100, maxFetches: 60, maxFetchBytes: 1 << 20 });
    const hits = (await hosts["store.search"](["hidden steps", 3])) as ReturnType<typeof storeHits>;
    assert.deepEqual(asked, [{ url: "http://litwatch.test/api/papers?q=hidden+steps&limit=3", auth: "Bearer r3ad" }]);
    assert.deepEqual(
      hits.map((h) => [h.id, h.tier, h.reason, h.in_collection]),
      [
        ["2610.00001", 1, "It finds them.", true],
        ["2610.00002", null, null, true],
        ["2401.00009", null, null, false],
      ],
    );
    assert.equal(hits[2].authors[0], "Bo Chen");
  } finally {
    globalThis.fetch = realFetch;
  }
});

test("the highest grade across the badges maps to the tier the agent knows", () => {
  const tiers = ["must_read", "relevant", "adjacent", "none"].map(
    (grade) => storeHits({ rows: [{ id: "1", title: "t", badges: [{ grade }] }] })[0].tier,
  );
  assert.deepEqual(tiers, [1, 2, 3, 4]);
  // A Focus relevant outranks a Field must read lowered by its depth and comes first; ungraded is no tier.
  const tied = [{ grade: "relevant" }, { grade: null }, { grade: "must_read" }];
  assert.equal(storeHits({ rows: [{ id: "1", title: "t", badges: tied }] })[0].tier, 1);
  assert.equal(storeHits({ rows: [{ id: "1", title: "t", badges: [{ grade: null }] }] })[0].tier, null);
  assert.deepEqual(storeHits({}), []);
});

test("Semantic Scholar is asked by arXiv id for a modern or a legacy id, any version", () => {
  assert.equal(s2Id("2305.04388v2"), "arXiv:2305.04388");
  assert.equal(s2Id("arXiv:cs/9901001"), "arXiv:cs/9901001");
  assert.equal(s2Id("math.AG/0601001v3"), "arXiv:math.AG/0601001");
  assert.equal(s2Id("cs/990100"), null);
});

// One address of every class the fetch must refuse: private, loopback, unspecified, link-local, CGNAT, reserved,
// multicast, broadcast, unique-local, site-local, IPv4-mapped, NAT64, 6to4, Teredo.
const refused = [
  "10.0.0.1", "172.16.5.4", "192.168.0.1", "127.0.0.1", "0.0.0.0", "169.254.169.254", "100.64.0.1",
  "192.0.0.8", "198.18.0.1", "224.0.0.1", "255.255.255.255",
  "::1", "::", "fe80::1", "fc00::1", "fd12:3456::1", "fec0::1", "ff02::1",
  "::ffff:192.168.0.1", "::ffff:127.0.0.1", "64:ff9b::c0a8:1", "2002:c0a8:101::1", "2001::1",
];

test("only public unicast addresses are public, whatever form they come in", () => {
  for (const a of [...refused, "not an address"]) assert.equal(isPublic(a), false, a);
  for (const a of ["8.8.8.8", "140.82.112.3", "2606:4700::1111", "::ffff:8.8.8.8"]) assert.equal(isPublic(a), true, a);
});

const answers: Record<string, string[]> = {
  "site.test": ["127.0.0.1"],
  "private.test": ["192.168.0.1"],
  "mixed.test": ["127.0.0.1", "10.0.0.7"],
};
type Lookup = Parameters<typeof guardedLookup>[1];
const stubResolve: Lookup = (hostname, _options, cb) => {
  const found = answers[hostname];
  if (!found) return cb(Object.assign(new Error(`ENOTFOUND ${hostname}`), { code: "ENOTFOUND" }), []);
  cb(null, found.map((address) => ({ address, family: address.includes(":") ? 6 : 4 })));
};
// The test server is on loopback, so the tests' policy lets 127.0.0.1 through and nothing else private.
const testPublic = (a: string) => a === "127.0.0.1" || isPublic(a);

test("a name resolving to any private address is refused before the socket connects", async () => {
  const lookup = guardedLookup(testPublic, stubResolve);
  const ask = (name: string, all: boolean) =>
    new Promise<unknown>((resolve, reject) =>
      lookup(name, { all }, (err, address) => (err ? reject(err) : resolve(address))),
    );
  assert.deepEqual(await ask("site.test", true), [{ address: "127.0.0.1", family: 4 }]);
  assert.equal(await ask("site.test", false), "127.0.0.1");
  await assert.rejects(ask("private.test", false), /resolves to 192\.168\.0\.1, not a public address/);
  await assert.rejects(ask("mixed.test", true), /resolves to 10\.0\.0\.7/);
  await assert.rejects(ask("nowhere.test", false), /ENOTFOUND/);
});

test("a private literal address is refused without a connection, however it is written", async () => {
  const opts = { maxChars: 100, maxBytes: 1000 };
  for (const url of [
    "http://127.0.0.1:9/",
    "http://[::1]:9/",
    "http://[::ffff:192.168.0.1]/",
    "http://0x7f.1/",
    "http://2130706433/",
    "http://10.0.0.1/",
    "http://169.254.169.254/latest/meta-data/",
    ...refused.map((a) => (a.includes(":") ? `http://[${a}]/` : `http://${a}/`)),
  ]) {
    await assert.rejects(fetchPublic(url, opts), /is not a public address/, url);
  }
  await assert.rejects(fetchPublic("file:///etc/passwd", opts), /only http\(s\) URLs/);
  await assert.rejects(fetchPublic("not a url", opts), /not a URL/);
});

/** Waits for the test server to have done something, failing after two seconds. */
async function until(done: () => boolean): Promise<void> {
  for (const started = Date.now(); !done(); await new Promise((r) => setTimeout(r, 5)))
    if (Date.now() - started > 2000) throw new Error("the test server never got there");
}

let server: Server;
let port = 0;
let bigFinished: boolean | undefined;
let served: string[] = [];
let agents: (string | undefined)[] = [];
before(async () => {
  server = createServer((req, res) => {
    served.push(req.url ?? "");
    agents.push(req.headers["user-agent"]);
    if (req.url?.startsWith("/slow/")) return void setTimeout(() => res.end('{"rows":[]}'), 300);
    const to = (location: string) => res.writeHead(302, { location }).end();
    switch (req.url) {
      case "/page":
        return res.writeHead(200, { "content-type": "text/html" }).end("<p>Code</p><script>x()</script><p>here</p>");
      case "/hop":
        return to(`http://site.test:${port}/page`);
      case "/to-private-name":
        return to(`http://private.test:${port}/page`);
      case "/to-private-literal":
        return to("http://10.0.0.1/");
      case "/to-file":
        return to("file:///etc/passwd");
      case "/loop":
        return to("/loop");
      case "/open-at-cap":
        // exactly the cap, then the connection stays open
        res.writeHead(200, { "content-type": "text/plain" });
        return void res.write("y".repeat(1000));
      case "/pdf":
        return res.writeHead(200, { "content-type": "Application/PDF" }).end("%PDF-1.7 body");
      case "/self":
        return res.writeHead(302, { location: "" }).end();
      case "/big": {
        res.writeHead(200, { "content-type": "text/plain" });
        const chunk = "x".repeat(64 * 1024);
        let sent = 0;
        res.on("close", () => (bigFinished = res.writableFinished));
        const pump = () => {
          while (sent < 5 * 1024 * 1024) {
            sent += chunk.length;
            if (!res.write(chunk)) return res.once("drain", pump);
          }
          res.end();
        };
        return pump();
      }
      default:
        return res.writeHead(404).end("missing");
    }
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  port = (server.address() as AddressInfo).port;
});
after(() => server.close());

const local = {
  maxChars: 1000,
  maxBytes: 2 * 1024 * 1024,
  pace: <T>(_host: string, work: () => Promise<T>) => work(),
  guard: makeGuard(testPublic, stubResolve),
};

test("a public page is fetched as text through redirects, with the URL it ended at", async () => {
  const page = (await fetchPublic(`http://site.test:${port}/hop`, local)) as Record<string, unknown>;
  assert.equal(page.status, 200);
  assert.equal(page.url, `http://site.test:${port}/page`);
  assert.equal(page.text, "Code\nhere");
  assert.equal(page.truncated, false);
  const missing = (await fetchPublic(`http://site.test:${port}/nothing`, local)) as Record<string, unknown>;
  assert.equal(missing.status, 404, "an HTTP status is a value for the agent, not an error");
});

test("fetch names the operator's contact in the User-Agent only when one is configured", async () => {
  agents = [];
  await fetchPublic(`http://site.test:${port}/page`, { ...local, contact: "ops@example.org" });
  await fetchPublic(`http://site.test:${port}/page`, local);
  assert.deepEqual(agents, ["litwatch-deep (mailto:ops@example.org)", "litwatch-deep"]);
});

test("every redirect hop is checked again", async () => {
  await assert.rejects(fetchPublic(`http://site.test:${port}/to-private-name`, local), /private\.test resolves to 192\.168\.0\.1/);
  await assert.rejects(fetchPublic(`http://site.test:${port}/to-private-literal`, local), /10\.0\.0\.1 is not a public address/);
  await assert.rejects(fetchPublic(`http://site.test:${port}/to-file`, local), /only http\(s\) URLs/);
  await assert.rejects(fetchPublic(`http://site.test:${port}/loop`, local), /more than 5 redirects/);
  await assert.rejects(fetchPublic(`http://site.test:${port}/self`, local), /more than 5 redirects/, "an empty Location is the page itself");
});

test("the production fetch asks DNS where it connects and refuses a private answer before any request", async () => {
  const realLookup = dns.lookup;
  const fake: Record<string, string[]> = {
    "loop.example": ["127.0.0.1"],
    "loop6.example": ["::1"],
    "mapped.example": ["::ffff:127.0.0.1"],
    "lan.example": ["192.168.0.1"],
    "mixed.example": ["93.184.215.14", "127.0.0.1"],
    ...Object.fromEntries(refused.map((a, i) => [`class${i}.example`, [a]])),
  };
  (dns as { lookup: unknown }).lookup = (host: string, _opts: unknown, cb: (e: Error | null, a: unknown) => void) =>
    cb(null, (fake[host] ?? []).map((address) => ({ address, family: address.includes(":") ? 6 : 4 })));
  served = [];
  try {
    const hosts = buildHosts({ litwatchUrl: "http://litwatch.test", maxFetchChars: 100, maxFetches: 60, maxFetchBytes: 1000 });
    for (const name of Object.keys(fake)) {
      await assert.rejects(hosts.fetch!([`http://${name}:${port}/page`]), /not a public address/, name);
    }
    assert.deepEqual(served, [], "the listener on 127.0.0.1 was never reached");
  } finally {
    (dns as { lookup: unknown }).lookup = realLookup;
  }
});

test("a body is read only up to the byte cap", async () => {
  bigFinished = undefined;
  const page = (await fetchPublic(`http://site.test:${port}/big`, { ...local, maxChars: 3 * 1024 * 1024 })) as Record<string, unknown>;
  assert.equal((page.text as string).length, 2 * 1024 * 1024);
  assert.equal(page.truncated, true);
  await until(() => bigFinished !== undefined);
  assert.equal(bigFinished, false, "the server never got to send the whole 5 MB");
  const fractional = (await fetchPublic(`http://site.test:${port}/big`, { ...local, maxChars: 1 << 20, maxBytes: 0.1 * 1024 * 1024 })) as Record<string, unknown>;
  assert.equal((fractional.text as string).length, 104857, "a fractional cap is a whole number of bytes");
  assert.equal(fractional.truncated, true);
});

test("a body left open at exactly the cap is not waited on", async () => {
  const page = (await fetchPublic(`http://site.test:${port}/open-at-cap`, { ...local, maxBytes: 1000 })) as Record<string, unknown>;
  assert.equal((page.text as string).length, 1000);
  assert.equal(page.truncated, true);
});

test("a PDF body is never read, whatever the case of its media type", async () => {
  const page = (await fetchPublic(`http://site.test:${port}/pdf`, local)) as Record<string, unknown>;
  assert.equal(page.text, "");
  assert.match(String(page.note), /PDF bodies are not fetched/);
});

test("when a paper's session ends, its requests under way are aborted and its queued ones never start", async () => {
  const session = new AbortController();
  const hosts = buildHosts({ litwatchUrl: `http://127.0.0.1:${port}/slow`, maxFetchChars: 100, maxFetches: 60, maxFetchBytes: 1000 }, session.signal);
  served = [];
  const underWay = hosts["store.search"]!(["a"]);
  const queued = hosts["store.search"]!(["b"]);
  await until(() => served.length === 1);
  session.abort();
  await assert.rejects(underWay, /abort/i);
  await assert.rejects(queued, /abort/i);
  await new Promise((r) => setTimeout(r, 400));
  assert.equal(served.length, 1, "the queued search never reached the server");
});

test("an ended session's lanes drain at once instead of at the pace", async () => {
  const session = new AbortController();
  const pace = pacer(session.signal);
  const ran: string[] = [];
  const underWay = pace("slow.test", () => new Promise((_, reject) => session.signal.addEventListener("abort", () => reject(new Error("aborted")))), 10_000);
  const queued = [1, 2].map((n) => pace("slow.test", async () => ran.push(`queued ${n}`), 10_000));
  const started = Date.now();
  session.abort();
  for (const call of [underWay, ...queued]) await assert.rejects(call, /abort/i);
  assert.deepEqual(ran, []);
  assert.ok(Date.now() - started < 1000, "not ten seconds a call");
});

test("a paper's session stops fetching once its budget is used", async () => {
  const hosts = buildHosts({ litwatchUrl: "http://litwatch.test", maxFetchChars: 100, maxFetches: 2, maxFetchBytes: 1000 });
  for (let i = 0; i < 2; i++) await assert.rejects(hosts.fetch!(["http://10.0.0.1/"]), /not a public address/);
  await assert.rejects(hosts.fetch!(["https://example.com/"]), /fetch budget of 2 per paper is used up/);
});

test("HTML becomes its readable text", () => {
  const page = `<html><head><style>p{x}</style><SCRIPT type="x">if (a < b) go()</SCRIPT></head><body>
<NAV><a href="/">Home</a></NAV><header>Site</header><!-- note <p>hidden</p> -->
<h1>Title &amp; more</h1><p>One&nbsp;line<br/>two\t\t lines</p>


<div>Code &lt;here&gt; &quot;q&quot; &#39;s&#39;</div><svg><path d="M0"/></svg><footer>(c)</footer>
<p title="a < b">Visible</p><>kept<script>never closed`;
  assert.equal(htmlToText(page), "Title & more\nOne line\ntwo lines\nCode <here> \"q\" 's'\nVisible\n<>kept never closed");
});

test("hostile HTML up to the byte cap is read in linear time", () => {
  // In a child, so a regression fails at the deadline instead of holding the suite for hours.
  const script = `
    import { htmlToText } from ${JSON.stringify(new URL("../src/hosts.ts", import.meta.url).href)};
    const n = 2 * 1024 * 1024;
    for (const page of ["<".repeat(n), "<nav".repeat(n / 4), "<!--".repeat(n / 4), "<a".repeat(n / 2),
      ("<br" + " ".repeat(29)).repeat(n / 32), "\\r".repeat(n)]) htmlToText(page);`;
  const run = spawnSync(process.execPath, ["--input-type=module", "-e", script], { timeout: 10_000, encoding: "utf8" });
  assert.equal(run.signal, null, "the quadratic version took minutes per page; linear takes well under a second");
  assert.equal(run.status, 0, run.stderr);
});

test("anonymous.4open.science links go to its JSON API", () => {
  const r = (u: string) => rewriteAnonymous(new URL(u)).href;
  const api = "https://anonymous.4open.science/api/repo";
  assert.equal(r("https://anonymous.4open.science/r/cot-monitorability"), `${api}/cot-monitorability/files/`);
  assert.equal(r("https://anonymous.4open.science/r/cot-monitorability/"), `${api}/cot-monitorability/files/`);
  assert.equal(r("https://anonymous.4open.science/r/cot-monitorability/src/run.py"), `${api}/cot-monitorability/file/src/run.py`);
  assert.equal(r("https://anonymous.4open.science/r/x-1/a%20b/c&d%25e.py"), `${api}/x-1/file/a%20b/c%26d%25e.py`);
  const listing = rewriteAnonymous(new URL("https://anonymous.4open.science/r/x-1/data&more%25/"));
  assert.equal(listing.pathname, "/api/repo/x-1/files/");
  assert.equal(listing.searchParams.get("path"), "data&more%");
  assert.equal(r("https://anonymous.4open.science/api/repo/x/files/"), "https://anonymous.4open.science/api/repo/x/files/");
  assert.equal(r("https://github.com/r/x"), "https://github.com/r/x");
});
