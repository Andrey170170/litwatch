import assert from "node:assert/strict";
import { test } from "node:test";
import dns from "node:dns";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { fauxAssistantMessage, type Models } from "@earendil-works/pi-ai";
import type { AgentMessage } from "@earendil-works/pi-agent-core";
import { detailedSummaries, processPaper, withoutOldImages, type CardSpec, type RunOptions } from "../src/agent.ts";
import { PROVIDER } from "../src/auth.ts";
import type { HostConfig } from "../src/hosts.ts";
import { RenderUnavailable, type Imaging } from "../src/imaging.ts";
import type { PaperInput } from "../src/prompt.ts";
import type { Draft } from "../src/read.ts";
import { passes, type Verdict } from "../src/review.ts";
import { BODY, blocks, cell, changed, draft, fakeImaging, HOSTS, imagesOf, models, OPEN, OPTS, PAPER, PASSING, png, reviews, seeing, submits, textOf, WEBP, withSite, type Sent } from "./support.ts";

const NONE: CardSpec = { extensions: [], selections: [] };
interface Run {
  paper?: PaperInput;
  spec?: CardSpec;
  hosts?: Partial<HostConfig>;
  opts?: Partial<RunOptions>;
  imaging?: Imaging;
}
const run = (m: Models, r: Run = {}) => processPaper(r.paper ?? PAPER, r.spec ?? NONE, m, { ...HOSTS, ...r.hosts }, { ...OPTS, ...r.opts }, r.imaging ?? fakeImaging().imaging);
/** Hosts whose arXiv and litwatch are the test's own server. */
const local = (url: string): Partial<HostConfig> => ({ litwatchUrl: url, arxivUrl: url, guard: OPEN });

const FAILING: Verdict = {
  ...PASSING,
  scores: { ...PASSING.scores, clarity: 3 },
  findings: [{ about: "clarity", where: "method.0", text: 'Hidden actor: "a probe is trained" does not say who trains it.', rewrite: "The authors train a probe." }],
};
/** The draft as the writer's n-th revision leaves it. */
const revised = (n: number) => changed((d) => (blocks(d, "method")[1]!.text = `A linear probe, because it adds little of its own (revision ${n}).`));
const why = (read: { sections: unknown[] } | null) => blocks(read as Draft, "method")[1]!.text;
const stopping = [fauxAssistantMessage("Done."), fauxAssistantMessage("Done."), fauxAssistantMessage("Done.")];
/** `BODY` with `chars` more characters of appendix before its last sentence. */
const long = (chars: number) => BODY.replace("The learning rate is 0.003", `${"The probe is linear. ".repeat(Math.ceil(chars / 21))}\nThe learning rate is 0.003`);

// ── the run's failures ──────────────────────────────────────────────────────────────────────────────

test("a failed model call is the service's failure", async () => {
  const { models: m, faux } = models();
  faux.setResponses([fauxAssistantMessage("", { stopReason: "error", errorMessage: "usage limit reached" })]);
  const outcome = await run(m);
  assert.equal(outcome.error, "usage limit reached");
  assert.equal(outcome.failure, "service");
  assert.deepEqual([outcome.result, outcome.figures, outcome.reviews], [null, [], null]);
});

test("a response the content filter stopped is the paper's failure", async () => {
  const { models: m, faux } = models();
  const filtered = fauxAssistantMessage("", { stopReason: "error", errorMessage: "Response incomplete: content_filter" });
  filtered.rawStopReason = "incomplete.content_filter";
  faux.setResponses([filtered]);
  const outcome = await run(m);
  assert.equal(outcome.error, "Response incomplete: content_filter");
  assert.equal(outcome.failure, "paper");
});

test("no read submitted is the paper's failure", async () => {
  const { models: m, faux } = models();
  faux.setResponses(stopping);
  const outcome = await run(m);
  assert.equal(outcome.error, "the model stopped without submitting");
  assert.equal(outcome.failure, "paper");
});

test("the wall clock ends the paper without waiting on its host calls", async () => {
  const server = createServer((_req, res) => void setTimeout(() => res.end('{"rows":[]}'), 3000));
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  try {
    const { models: m, faux } = models();
    faux.setResponses([cell('await store.search("x")')]);
    const litwatchUrl = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
    const started = Date.now();
    const outcome = await run(m, { hosts: { litwatchUrl }, opts: { wallClockMs: 150 } });
    assert.match(String(outcome.error), /wall clock/);
    assert.ok(Date.now() - started < 2000, "the search under way was aborted, not waited out");
  } finally {
    server.closeAllConnections();
    server.close();
  }
});

test("the trace keeps why a host call failed, what a fetch did, and which cell timed out", async () => {
  const { models: m, faux } = models();
  faux.setResponses([cell(`await fetch("${"x".repeat(500)}")`), cell('await fetch("http://10.0.0.1/")'), cell("while (true) {}"), cell("1")]);
  const outcome = await run(m, { opts: { maxCells: 4, cellTimeoutMs: 200 } });
  const hosts = outcome.trace.filter((e) => e.type === "host");
  assert.equal(hosts.length, 2);
  assert.equal(hosts[0]!.ok, false);
  assert.equal(hosts[0]!.error, `not a URL: ${"x".repeat(500)}`.slice(0, 300), "the reason, cut to 300 characters");
  assert.equal(hosts[1]!.error, "10.0.0.1 is not a public address");
  assert.deepEqual(hosts[1]!.fetch, { hops: [] }, "a fetch refused before any answer has no hops and no page");
  const cells = outcome.trace.filter((e) => e.type === "cell");
  assert.deepEqual(cells.map((c) => c.timedOut), [undefined, undefined, true, undefined], "only the cell that ran too long");
});

test("the provider is asked for detailed reasoning summaries", async () => {
  const { models: m, faux } = models();
  let asked: unknown;
  faux.setResponses([
    async (_context, options) => {
      asked = await options?.onPayload?.({ reasoning: { effort: "medium", summary: "auto" } }, m.getModel(PROVIDER, OPTS.model)!);
      return fauxAssistantMessage("Done.");
    },
    ...stopping,
  ]);
  await run(m);
  assert.deepEqual(asked, { reasoning: { effort: "medium", summary: "detailed" } });
  assert.deepEqual(detailedSummaries({ reasoning: { effort: "none" } }), { reasoning: { effort: "none" } }, "no summary where none is asked for");
});

// ── the first message ───────────────────────────────────────────────────────────────────────────────

/** The writer's first message for a paper's text. */
async function firstMessage(body: string): Promise<string> {
  const { models: m, faux } = models();
  const sent: Sent[] = [];
  faux.setResponses([seeing(fauxAssistantMessage("Done."), sent), ...stopping]);
  await run(m, { paper: { ...PAPER, body } });
  assert.deepEqual(sent[0]!.messages.map((x) => x.role), ["system", "user"]);
  return textOf(sent[0]!.messages[1]!);
}

test("the first message carries the main text and leaves out the bibliography and the appendix after it", async () => {
  const message = await firstMessage(BODY);
  assert.ok(message.includes("The probe reaches 71.3\\% accuracy"), "the results are in it");
  assert.ok(!message.includes("Turpin"), "no bibliography entry");
  assert.ok(!message.includes("learning rate"), "no appendix text");
  // the outline is the session's sections(), each with its number, marked where a part was left out
  assert.match(message, /^- 3 Results \(\d+ characters\)$/m);
  assert.match(message, /^- References \(\d+ characters, bibliography: in the session only\)$/m);
  assert.match(message, /^- A Probe training details \(\d+ characters, appendix: in the session only\)$/m);
  assert.match(message, /Left out of this message and kept in `paper` in the session: the bibliography \(\d+ characters\) and the appendix \(\d+ characters\)/);
});

test("the first message of a paper with no appendix leaves out its bibliography alone, and one with neither is whole", async () => {
  const noAppendix = `${BODY.slice(0, BODY.indexOf("\\appendix"))}\\section{Conclusion}\nProbes read the trace.\n\\end{document}\n`;
  const message = await firstMessage(noAppendix);
  assert.ok(!message.includes("Turpin"));
  assert.ok(message.includes("Probes read the trace.\n\\end{document}"), "what follows the bibliography stays");
  assert.match(message, /kept in `paper` in the session: the bibliography \(\d+ characters\); read/);
  assert.ok(!/appendix/i.test(message), "no appendix is spoken of");

  const whole = "# Probes\n\n## Method\n\nWe probe 1,200 traces.\n\n## Results\n\nThe probe reaches 71.3%.\n";
  const all = await firstMessage(whole);
  assert.ok(all.endsWith(whole));
  assert.ok(all.includes(`Full text (${whole.length} characters; the paper has no separate appendix or bibliography in it):`));
});

// ── submit ──────────────────────────────────────────────────────────────────────────────────────────

test("submit rejects a draft with what is wrong, check says the same without submitting, and a diagram is laid out", async () => {
  const { models: m, faux } = models();
  const wrong = changed((d) => (blocks(d, "method")[0]!.text = "The probe reaches 83.4% accuracy."));
  const drawn = changed((d) =>
    blocks(d, "method").push({
      type: "diagram",
      nodes: [{ id: "trace", label: "Trace", tip: "A labelled trace from the model." }, { id: "probe", label: "Linear probe", tip: "Reads layer 12 of the residual stream." }],
      edges: [{ from: "trace", to: "probe", tip: "The residual stream at each token." }],
    }),
  );
  const sent: Sent[] = [];
  faux.setResponses([
    cell(`const a = await check(${JSON.stringify(wrong)}); const b = await submit(${JSON.stringify(wrong)}); console.log(JSON.stringify([a, b]))`),
    seeing(cell(`console.log(JSON.stringify(await check(${JSON.stringify(drawn)})))`), sent),
    submits(drawn),
    reviews(PASSING),
  ]);
  const outcome = await run(m);
  const answers = textOf(sent[0]!.messages.at(-1)!);
  assert.match(answers, /^\[\{"ok":false,"errors":\["\/sections\/1\/blocks\/0\/text: 83\.4% is not in the paper's text"/, "the model is told what to fix");
  const [checked, submitted] = JSON.parse(answers.split("\n")[0]!) as { ok: boolean; errors: string[] }[];
  assert.deepEqual(checked, submitted, "check() and submit() say the same");
  const submits_ = outcome.trace.filter((e) => e.type === "submit");
  assert.deepEqual(submits_.map((s) => s.ok), [false, true], "a check is no submit");
  assert.ok(submits_[0]!.errors?.[0]?.startsWith("/sections/1/blocks/0/text: 83.4%"));
  assert.equal(outcome.error, undefined);
  const diagram = blocks(outcome.result as unknown as Draft, "method")[2] as { nodes: { x?: number; h?: number }[]; width?: number };
  assert.ok(diagram.nodes.every((n) => Number.isFinite(n.x) && n.h! >= 40) && diagram.width! > 0, "the stored diagram carries its coordinates");
});

// ── the review ──────────────────────────────────────────────────────────────────────────────────────

test("the pass rule: every score at 4 or above and every check passed", () => {
  assert.equal(passes(PASSING), true);
  assert.equal(passes({ ...PASSING, scores: { faithfulness: 4, clarity: 4, depth: 4, calibration: 4 } }), true);
  for (const name of ["faithfulness", "clarity", "depth", "calibration"] as const) assert.equal(passes({ ...PASSING, scores: { ...PASSING.scores, [name]: 3 } }), false, name);
  for (const name of ["anchors", "experiments", "why_boxes", "voice", "length"] as const)
    assert.equal(passes({ ...PASSING, scores: { faithfulness: 5, clarity: 5, depth: 5, calibration: 5 }, checks: { ...PASSING.checks, [name]: false } }), false, name);
});

test("a read that passes its review is returned with the round, and the reviewer's call counts", async () => {
  const { models: m, faux } = models();
  const sent: Sent[] = [];
  faux.setResponses([submits(draft()), seeing(reviews(PASSING), sent)]);
  const outcome = await run(m);
  assert.equal(outcome.error, undefined);
  assert.deepEqual(outcome.result, draft(), "the read as the writer gave it: no note where nothing held the run back");
  assert.deepEqual(outcome.reviews, [{ round: 1, passed: true, ...PASSING }]);
  assert.deepEqual(outcome.figures, []);
  assert.equal(outcome.stats.turns, 2, "the writer's turn and the reviewer's");
  assert.deepEqual(outcome.trace.map((e) => e.type), ["assistant", "submit", "cell", "review"]);
  // the reviewer: its own prompt, one tool, the whole paper and the read, no session
  const [system, user] = sent[0]!.messages;
  assert.equal(sent[0]!.messages.length, 2);
  assert.ok(textOf(system!).startsWith("You review a Deep read"));
  assert.deepEqual(system!.toolsAdded?.map((t) => t.name), ["review"]);
  assert.ok(textOf(user!).includes("The paper's text, whole:") && textOf(user!).includes(BODY), "the paper's whole text, the appendix included");
  assert.ok(textOf(user!).includes(JSON.stringify(draft())), "the read as JSON");
  assert.ok(!textOf(user!).includes("Not confirmed by code"), "every place the read cites was found");
});

test("the reviewer is told by name the places code could not look up", async () => {
  const { models: m, faux } = models();
  const sent: Sent[] = [];
  const read = changed((d) => ((d.sections[3] as unknown as { claims: { where: string }[] }).claims[0]!.where = "Eq. 9999 and Algorithm 9999, p. 9999"));
  faux.setResponses([submits(read), seeing(reviews(PASSING), sent)]);
  const outcome = await run(m);
  assert.deepEqual(outcome.trace.filter((e) => e.type === "submit").map((s) => s.ok), [true], "no place the source rules out: the read is taken");
  assert.ok(
    textOf(sent[0]!.messages[1]!).endsWith("Not confirmed by code, which could not tell whether the paper has these places the read cites: Eq. 9999; Algorithm 9999; p. 9999. Check each against the paper."),
    textOf(sent[0]!.messages[1]!).slice(-300),
  );
});

test("a refused place or number the writer stands by goes to the reviewer by name, and the trace keeps it", async () => {
  const { models: m, faux } = models();
  const sent: Sent[] = [];
  const read = changed((d) => {
    blocks(d, "method")[0]!.text = "The probe reaches 83.4% accuracy.";
    (d.sections[3] as unknown as { claims: { where: string }[] }).claims[0]!.where = "Table 9";
  });
  faux.setResponses([
    cell(`console.log(JSON.stringify(await submit(${JSON.stringify(read)}, ["83.4%"])))`),
    seeing(cell(`console.log(JSON.stringify(await submit(${JSON.stringify(read)}, ["table  9", "83.4%", "Section 1"])))`), sent),
    seeing(reviews(PASSING), sent),
  ]);
  const outcome = await run(m);
  assert.match(textOf(sent[0]!.messages.at(-1)!), /^\{"ok":false,"errors":\["\/sections\/3\/claims\/0\/where: cites Table 9, but the paper's source has 1 table"\]\}/, "only what is not stood by is refused");
  assert.deepEqual(outcome.trace.filter((e) => e.type === "submit").map((s) => [s.ok, s.stood]), [[false, undefined], [true, ["83.4%", "Table 9"]]], "stood by as refused, in any case or spacing; what was never refused is not stood by");
  assert.ok(
    textOf(sent[1]!.messages[1]!).endsWith("Refused by code, and stood by the writer as the paper's: 83.4%; Table 9. Check each against the paper; one it does not support is a faithfulness finding."),
    textOf(sent[1]!.messages[1]!).slice(-300),
  );
});

test("the reviewer reads the whole paper where the model's context has room, past any fixed cut", async () => {
  // 400,000 tokens of context: room for a text of 250,000 characters, whose appendix ends it
  const { models: m, faux } = models(400_000);
  const sent: Sent[] = [];
  faux.setResponses([submits(draft()), seeing(reviews(PASSING), sent)]);
  const body = long(250_000);
  const outcome = await run(m, { paper: { ...PAPER, body } });
  assert.ok(textOf(sent[0]!.messages[1]!).includes(body), "all of it, the appendix's last sentence included");
  assert.deepEqual(outcome.result, draft(), "a whole review leaves no note");
});

test("a paper past the model's context reaches the reviewer cut, with what was left out said to it and noted on the read", async () => {
  // 64,000 tokens of context leave the paper the least it is given: 10,000 characters
  const { models: m, faux } = models(64_000);
  const sent: Sent[] = [];
  faux.setResponses([submits(draft()), seeing(reviews(PASSING), sent)]);
  const body = long(40_000);
  const outcome = await run(m, { paper: { ...PAPER, body } });
  const message = textOf(sent[0]!.messages[1]!);
  const bibliography = body.indexOf("\\appendix") - body.indexOf("\\begin{thebibliography}") - 1;
  // what is left once a line stands in for the bibliography, past the first 10,000 characters
  const left = `its bibliography (${bibliography} characters) and the last ${body.length - bibliography + "[the bibliography stood here and is left out]\n".length - 10_000} characters of its text`;
  assert.ok(message.includes(`The paper's text, without ${left}, which the model's context has no room for:`), message.slice(0, 400));
  assert.ok(message.includes("The probe reaches 71.3\\% accuracy") && message.includes("[the bibliography stood here and is left out]") && !message.includes("Language models don't always say"), "the bibliography goes first");
  assert.ok(message.includes("[the paper's text is cut here;") && !message.includes("The learning rate is 0.003"), "then the end of the text");
  assert.ok(message.includes("What the read says of the parts of the paper left out above cannot be checked here"));
  // a pass on part of the paper is not a silent pass
  assert.deepEqual(outcome.reviews?.map((r) => r.passed), [true]);
  assert.deepEqual(outcome.result?.process_notes, [
    { kind: "tool", source: "service", note: `The reviewer was not given the whole paper: ${left} did not fit the model's context, so what the read says of that part was not reviewed.` },
  ]);
});

test("a failed review sends its findings into the writer's own conversation, and the revision is reviewed again", async () => {
  const { models: m, faux } = models();
  const writer: Sent[] = [];
  faux.setResponses([submits(draft()), reviews(FAILING), seeing(submits(revised(1)), writer), reviews(PASSING)]);
  const outcome = await run(m);
  assert.deepEqual(outcome.reviews?.map((r) => [r.round, r.passed]), [[1, false], [2, true]]);
  assert.equal(why(outcome.result), why(revised(1)), "the revision is the read");
  assert.equal(outcome.result?.process_notes, undefined, "a read that passed in the end carries no review note");
  // the same conversation: the first message, the writer's cell and its output, then the findings
  const messages = writer[0]!.messages;
  assert.deepEqual(messages.map((x) => x.role), ["system", "user", "assistant", "toolResult", "user"]);
  assert.ok(textOf(messages[0]!).startsWith("You write the Deep read"));
  assert.ok(textOf(messages[1]!).startsWith("arXiv 2610.00001: Probes read the trace"));
  const findings = textOf(messages[4]!);
  assert.ok(findings.startsWith("The reviewer did not pass the read (review 1). Scores: faithfulness 5, clarity 3, depth 4, calibration 5; a pass needs every score at 4 or above (below: clarity 3)."), findings);
  assert.ok(findings.includes('1. [clarity] method.0: Hidden actor: "a probe is trained" does not say who trains it.\n   Rewrite: The authors train a probe.'));
  assert.ok(findings.includes("2 revisions are left"));
  assert.ok(outcome.trace.some((e) => e.type === "system" && e.text === findings), "the trace keeps what the writer was told");
});

test("a read is written once and revised at most twice; one still failing is kept with a review note", async () => {
  const { models: m, faux } = models();
  const last: Sent[] = [];
  const voice: Verdict = { ...PASSING, scores: { faithfulness: 5, clarity: 5, depth: 5, calibration: 5 }, checks: { ...PASSING.checks, voice: false }, findings: [] };
  faux.setResponses([
    submits(draft()),
    reviews(FAILING),
    submits(revised(1)),
    reviews(FAILING),
    seeing(submits(revised(2)), last),
    reviews(voice), // every score at 5, one check failed: no pass
    submits(revised(3)), // a third revision is never asked for
    reviews(PASSING),
  ]);
  const outcome = await run(m);
  assert.equal(faux.getPendingResponseCount(), 2, "neither the writer nor the reviewer was called again");
  assert.deepEqual(outcome.reviews?.map((r) => [r.round, r.passed]), [[1, false], [2, false], [3, false]]);
  assert.ok(textOf(last[0]!.messages.at(-1)!).includes("this is the last revision"));
  assert.equal(outcome.error, undefined, "the read is kept");
  assert.equal(why(outcome.result), why(revised(2)), "as its second revision left it");
  assert.deepEqual(outcome.result?.process_notes, [{ kind: "review", source: "service", note: "The read did not pass its review: checks failed: voice. It was kept as it stood after 2 revisions." }]);
  assert.deepEqual(outcome.trace.filter((e) => e.type === "review").map((e) => e.passed), [false, false, false]);
});

test("a revision the writer does not make leaves the reviewed read, with a note saying so", async () => {
  const { models: m, faux } = models();
  faux.setResponses([submits(draft()), reviews(FAILING), ...stopping]);
  const outcome = await run(m);
  assert.equal(outcome.error, undefined);
  assert.equal(why(outcome.result), why(draft()));
  assert.deepEqual(outcome.reviews?.map((r) => r.passed), [false]);
  assert.deepEqual(outcome.result?.process_notes, [
    { kind: "review", source: "service", note: "The read did not pass its review: scores below 4: clarity 3. The revision that should have followed was not made: the writer stopped without submitting." },
  ]);
});

test("a revision whose review cannot run is kept as unreviewed, not as failing the review of the draft before it", async () => {
  const { models: m, faux } = models();
  // round 1 fails on clarity; the writer revises; the review of the revision meets a usage limit
  faux.setResponses([submits(draft()), reviews(FAILING), submits(revised(1)), fauxAssistantMessage("", { stopReason: "error", errorMessage: "usage limit reached" })]);
  const outcome = await run(m);
  assert.equal(outcome.error, undefined);
  assert.equal(why(outcome.result), why(revised(1)), "the revision is the read kept");
  assert.deepEqual(outcome.result?.process_notes, [
    { kind: "tool", source: "service", note: "The read was not reviewed: the review could not run (usage limit reached). It is a revision: the reviews on record are of the drafts before it." },
  ]);
  assert.deepEqual(outcome.reviews, [{ round: 1, passed: false, ...FAILING }], "the round that was completed stays as history");
  assert.deepEqual(outcome.trace.filter((e) => e.type === "review").map((e) => [e.round, e.passed, e.error]), [[1, false, undefined], [2, undefined, "usage limit reached"]]);

  // what round 1's reviewer was not given of a long paper is no note on a revision no reviewer read
  const small = models(64_000);
  small.faux.setResponses([submits(draft()), reviews(FAILING), submits(revised(1)), fauxAssistantMessage("", { stopReason: "error", errorMessage: "usage limit reached" })]);
  const cut = await run(small.models, { paper: { ...PAPER, body: long(40_000) } });
  assert.deepEqual(cut.result?.process_notes?.map((n) => n.note.slice(0, 28)), ["The read was not reviewed: t"]);
});

test("a review that cannot run keeps the read, unreviewed; a verdict that does not fit is asked for once more", async () => {
  const failed = models();
  failed.faux.setResponses([submits(draft()), fauxAssistantMessage("", { stopReason: "error", errorMessage: "usage limit reached" })]);
  const unreviewed = await run(failed.models);
  assert.equal(unreviewed.error, undefined);
  assert.equal(unreviewed.reviews, null);
  assert.deepEqual(unreviewed.result?.process_notes, [{ kind: "tool", source: "service", note: "The read was not reviewed: the review could not run (usage limit reached)." }]);

  const thrown = models();
  thrown.faux.setResponses([submits(draft())]);
  // the reviewer's call is the only one made this way, and it throws where the sign-in is gone
  thrown.models.completeSimple = (async () => {
    throw new Error("no credential stored");
  }) as Models["completeSimple"];
  const lost = await run(thrown.models);
  assert.equal(lost.error, undefined);
  assert.deepEqual(lost.result?.process_notes, [{ kind: "tool", source: "service", note: "The read was not reviewed: the review could not run (no credential stored)." }]);

  // a first answer that is no verdict, then a retry that throws: the first call was made and is counted
  const half = models();
  half.faux.setResponses([submits(draft()), reviews({ ...PASSING, scores: { ...PASSING.scores, clarity: 6 } })]);
  const complete = half.models.completeSimple.bind(half.models);
  let asked = 0;
  half.models.completeSimple = (async (...args: Parameters<Models["completeSimple"]>) => {
    if (++asked > 1) throw new Error("no credential stored");
    return complete(...args);
  }) as Models["completeSimple"];
  const cut = await run(half.models);
  assert.equal(asked, 2);
  assert.equal(cut.stats.turns, 2, "the writer's turn and the reviewer's first call");
  const judged = cut.trace.find((e) => e.type === "review")!;
  assert.ok(judged.usage.output > 0 && judged.usage.input > 0, "the usage of the call that was answered is kept");
  assert.equal(cut.stats.output, cut.trace.filter((e) => e.type === "assistant").reduce((n, e) => n + e.usage.output, 0) + judged.usage.output);
  assert.deepEqual(cut.result?.process_notes, [{ kind: "tool", source: "service", note: "The read was not reviewed: the review could not run (no credential stored)." }]);

  const repaired = models();
  const sent: Sent[] = [];
  repaired.faux.setResponses([submits(draft()), reviews({ ...PASSING, scores: { ...PASSING.scores, clarity: 6 } }), seeing(reviews(PASSING), sent)]);
  const outcome = await run(repaired.models);
  assert.deepEqual(outcome.reviews, [{ round: 1, passed: true, ...PASSING }]);
  assert.match(textOf(sent[0]!.messages.at(-1)!), /The verdict does not fit the tool's schema:\n\/scores\/clarity: /);
  assert.equal(outcome.stats.turns, 3, "both of the reviewer's calls count");

  const never = models();
  never.faux.setResponses([submits(draft()), fauxAssistantMessage("It reads well."), fauxAssistantMessage("It reads well.")]);
  const none = await run(never.models);
  assert.equal(none.reviews, null);
  assert.match(none.result!.process_notes![0]!.note, /^The read was not reviewed: the review could not run \(the reviewer gave no valid verdict: no call of the review tool\)\.$/);
});

// ── images ──────────────────────────────────────────────────────────────────────────────────────────

/** The draft showing the paper's Figure 1 as `probe`, with a value read off it. */
const showing = (...names: string[]) =>
  changed((d) => {
    blocks(d, "method")[0]!.text = "Accuracy peaks near 0.84 at layer 9 (Fig. 1). The probe is linear.";
    for (const name of names) blocks(d, "method").push({ type: "fig", name, number: "Figure 1", caption: "Probe accuracy by layer." });
  });
const view = (bytes: number) => Buffer.from(`view of ${bytes} bytes`).toString("base64");

test("figure(), crop() and render() each return their image with the output of the cell that called them", async () => {
  await withSite(async ({ url }) => {
    const { models: m, faux } = models();
    const { imaging, calls } = fakeImaging();
    const sent: Sent[] = [];
    faux.setResponses([
      cell('await figure("figs/probe.png", "probe")'),
      cell('await crop(2, [0, 0.1, 1, 0.6], "fig-2")'),
      cell(`await render(${JSON.stringify(showing("probe", "ghost"))}, "method", "phone")`),
      cell("1 + 1"),
      seeing(fauxAssistantMessage("Done."), sent),
      ...stopping,
    ]);
    const outcome = await run(m, { hosts: local(url), imaging });
    const results = sent[0]!.messages.filter((x) => x.role === "toolResult");
    assert.deepEqual(results.map((r) => imagesOf(r)), [[view(png(8, 8).length)], [view("page 2 at 2200".length)], [Buffer.from("render").toString("base64")], []]);
    assert.match(textOf(results[0]!), /"name": "probe"/, "the cell's own output comes with the image");
    assert.match(textOf(results[2]!), /"target": "method"[\s\S]*"width": "phone"[\s\S]*"images": 1/);
    assert.deepEqual(outcome.trace.filter((e) => e.type === "cell").map((c) => c.images), [1, 1, 1, undefined]);
    // the render draws the figures this session made, and no other: a name it did not make is the page's placeholder
    const drawn = calls.renders[0]!;
    assert.deepEqual(drawn.figures, { probe: `data:image/webp;base64,${WEBP.toString("base64")}` });
    assert.deepEqual([drawn.target, drawn.width], [{ section: "method" }, "phone"]);
  });
});

test("the reviewer is shown the figures the read cites for values read off a plot, and the job returns the figures the read shows", async () => {
  await withSite(async ({ url }) => {
    const { models: m, faux } = models();
    const sent: Sent[] = [];
    faux.setResponses([
      cell(`await figure("figs/probe.png", "probe"); await figure("a/setup.png", "setup"); await submit(${JSON.stringify(showing("probe"))})`),
      seeing(reviews(PASSING), sent),
    ]);
    const outcome = await run(m, { hosts: local(url) });
    assert.equal(outcome.error, undefined);
    const user = sent[0]!.messages[1]!;
    assert.deepEqual(imagesOf(user), [view(png(8, 8).length)], "the figure as the writer saw it, and not the one the read does not cite");
    assert.ok(textOf(user).includes('The writer\'s figure "probe", which the read gives values read off:'));
    assert.deepEqual(outcome.figures, [{ name: "probe", mime: "image/webp", data: WEBP.toString("base64") }], "a figure made and not shown is not returned");
    assert.equal(outcome.result?.process_notes, undefined, "the e-print was the version's: nothing to note");
  });
});

test("a plot value is refused until the session has its figure's image, which then reaches the reviewer though the read shows no fig block", async () => {
  await withSite(async ({ url }) => {
    const { models: m, faux } = models();
    const sent: Sent[] = [];
    faux.setResponses([
      submits(showing()),
      cell(`await figure("figs/probe.png", "probe", "Figure 1"); await submit(${JSON.stringify(showing())})`),
      seeing(reviews(PASSING), sent),
    ]);
    const outcome = await run(m, { hosts: local(url) });
    const submitted = outcome.trace.filter((e) => e.type === "submit");
    assert.deepEqual(submitted.map((s) => s.ok), [false, true]);
    assert.equal(submitted[0]!.errors?.[0], "/sections/1/blocks/0/text: 0.84 is not in the paper's text, and this session has no image of Figure 1 to check it against");
    const user = sent[0]!.messages[1]!;
    assert.deepEqual(imagesOf(user), [view(png(8, 8).length)]);
    assert.ok(textOf(user).includes('The writer\'s figure "probe", made as the paper\'s Figure 1, which the read gives values read off:'));
    assert.deepEqual(outcome.figures, [], "the read shows no figure, so the job returns none");
  });
});

test("a revision that fails its submit leaves the accepted read its figures as they were when it was accepted", async () => {
  await withSite(async ({ url }) => {
    const { models: m, faux } = models();
    // an imaging whose stored picture differs with what it was made from
    const base = fakeImaging().imaging;
    const stored = (bytes: number) => Buffer.concat([WEBP, Buffer.from(String(bytes))]).toString("base64");
    const imaging: Imaging = { ...base, picture: async (data, mime, crop) => ({ ...(await base.picture(data, mime, crop)), webp: Buffer.from(stored(data.length), "base64") }) };
    const refused = changed((d) => (blocks(d, "method")[0]!.text = "The probe reaches 99.9% accuracy."));
    const second: Sent[] = [];
    faux.setResponses([
      cell(`await figure("figs/probe.png", "probe"); await submit(${JSON.stringify(showing("probe"))})`),
      reviews(FAILING),
      // the revision makes another picture under the same name, and its submit is refused
      cell(`await figure("a/setup.png", "probe"); await submit(${JSON.stringify(refused)})`),
      ...stopping,
    ]);
    const outcome = await run(m, { hosts: local(url), imaging });
    assert.deepEqual(outcome.trace.filter((e) => e.type === "submit").map((s) => s.ok), [true, false]);
    assert.deepEqual(outcome.result?.sections, showing("probe").sections, "the accepted read");
    assert.deepEqual(outcome.figures, [{ name: "probe", mime: "image/webp", data: stored(png(8, 8).length) }], "with the picture it was accepted and reviewed with");

    // a revision that is accepted is reviewed and returned with the picture as it then stands
    const again = models();
    again.faux.setResponses([
      cell(`await figure("figs/probe.png", "probe"); await submit(${JSON.stringify(showing("probe"))})`),
      reviews(FAILING),
      cell(`await figure("a/setup.png", "probe"); await submit(${JSON.stringify(showing("probe"))})`),
      seeing(reviews(PASSING), second),
    ]);
    const replaced = await run(again.models, { hosts: local(url), imaging });
    assert.deepEqual(imagesOf(second[0]!.messages[1]!), [view(png(4, 4).length)]);
    assert.deepEqual(replaced.figures, [{ name: "probe", mime: "image/webp", data: stored(png(4, 4).length) }]);

    // a picture made again after the submit, in the same cell, is no part of the read that was accepted
    const after = models();
    const first: Sent[] = [];
    after.faux.setResponses([
      cell(`await figure("figs/probe.png", "probe"); await submit(${JSON.stringify(showing("probe"))}); await figure("a/setup.png", "probe")`),
      seeing(reviews(PASSING), first),
    ]);
    const late = await run(after.models, { hosts: local(url), imaging });
    assert.deepEqual(imagesOf(first[0]!.messages[1]!), [view(png(8, 8).length)], "the reviewer sees the picture the read was accepted with");
    assert.deepEqual(late.figures, [{ name: "probe", mime: "image/webp", data: stored(png(8, 8).length) }]);
  });
});

test("figures made at once in one cell each reach the reviewer under their own name", async () => {
  await withSite(async ({ url }) => {
    const { models: m, faux } = models();
    const sent: Sent[] = [];
    // a paper with two figures, and a read with a value off each
    const body = BODY.replace("\\section{Results}", "\\begin{figure}\n\\includegraphics{a/setup.png}\n\\caption{Loss by step.}\n\\end{figure}\n\\section{Results}");
    const read = changed((d) => (blocks(d, "method")[0]!.text = "Accuracy peaks near 0.84 at layer 9 (Fig. 1). Loss falls to about 0.31 (Fig. 2)."));
    faux.setResponses([
      cell(`await Promise.all([figure("figs/probe.png", "probe", "Figure 1"), figure("a/setup.png", "setup", "Figure 2")]); await submit(${JSON.stringify(read)})`),
      seeing(reviews(PASSING), sent),
    ]);
    const outcome = await run(m, { paper: { ...PAPER, body }, hosts: local(url) });
    assert.equal(outcome.error, undefined);
    const content = sent[0]!.messages[1]!.content as { type: string; text?: string; data?: string }[];
    const shown = content.flatMap((c, i) => (c.type === "image" ? [[/figure "(\w+)"/.exec(content[i - 1]!.text!)![1], c.data]] : []));
    assert.deepEqual(shown, [["probe", view(png(8, 8).length)], ["setup", view(png(4, 4).length)]]);
  });
});

test("figures from the latest e-print, and a render that could not be made, are the service's notes on the read", async () => {
  await withSite(async ({ url, asked }) => {
    const { models: m, faux } = models();
    const { imaging } = fakeImaging();
    imaging.render = async () => {
      throw new RenderUnavailable("litwatch's render page answered 502");
    };
    faux.setResponses([
      cell(`await figures(); try { await render(${JSON.stringify(draft())}) } catch (e) { console.log(e.message) }`),
      submits(draft()),
      reviews(PASSING),
    ]);
    const outcome = await run(m, { paper: { ...PAPER, body_version: null }, hosts: local(url), imaging });
    assert.deepEqual(asked, ["/e-print/2610.00001"], "no version to ask for");
    assert.deepEqual(outcome.result?.process_notes, [
      { kind: "tool", source: "service", note: "An image could not be made: the browser or litwatch's render page was not available, so the writer could not look at a figure or at what it drew." },
      { kind: "input", source: "service", note: "litwatch could not say which arXiv version its text is of, so the figures were taken from the latest version arXiv or litwatch had, which may not be the text's." },
    ]);
  });
});

test("a draft that cannot be drawn is refused by render with what to fix, and is no trouble of the service's", async () => {
  const { models: m, faux } = models();
  const { imaging, calls } = fakeImaging();
  const broken = changed((d) => (blocks(d, "result")[1]!.rows as { cells: unknown[] }[])[0]!.cells.push("extra"));
  faux.setResponses([cell(`await render(${JSON.stringify(broken)})`), cell('await render({}, "Method!")'), submits(draft()), reviews(PASSING)]);
  const outcome = await run(m, { imaging });
  const cells = outcome.trace.filter((e) => e.type === "cell");
  assert.match(cells[0]!.error ?? "", /the draft cannot be drawn until these are fixed:\n\/sections\/2\/blocks\/1\/rows\/0\/cells: 3 cells for 2 columns/);
  assert.match(cells[1]!.error ?? "", /the draft cannot be drawn/);
  assert.equal(calls.renders.length, 0);
  assert.equal(outcome.result?.process_notes, undefined);
});

test("the model keeps the images of its latest cells only, dropped a batch at a time", () => {
  const image = { type: "image" as const, data: "aaaa", mimeType: "image/jpeg" };
  const result = (n: number, images: number): AgentMessage => ({ role: "toolResult", toolCallId: `c${n}`, toolName: "run_code", content: [{ type: "text", text: `cell ${n}` }, ...Array(images).fill(image)], isError: false, timestamp: n });
  const user: AgentMessage = { role: "user", content: "arXiv …", timestamp: 0 };
  const pictured = (messages: AgentMessage[]) => messages.map((x) => ("role" in x && x.role === "toolResult" ? x.content.filter((c) => c.type === "image").length : -1));
  const few = [user, result(1, 1), result(2, 0), result(3, 2), result(4, 1)];
  assert.equal(withoutOldImages(few, 2), few, "untouched up to twice the cells kept");
  const more = [...few, result(5, 1), result(6, 1)];
  const cut = withoutOldImages(more, 2);
  assert.deepEqual(pictured(cut), [-1, 0, 0, 0, 1, 1, 1], "the oldest two cells with images lose theirs");
  assert.deepEqual((cut[3] as { content: unknown[] }).content, [{ type: "text", text: "cell 3" }, { type: "text", text: "[2 images shown here earlier; call the function again to see them]" }]);
  // a batch is as many cells as are kept: the next two lose theirs together, and the cell after changes nothing
  const next = [...more, result(7, 1)];
  assert.deepEqual(pictured(withoutOldImages(next, 2)), [-1, 0, 0, 0, 0, 0, 1, 1]);
  assert.deepEqual(pictured(withoutOldImages([...next, result(8, 1)], 2)), [-1, 0, 0, 0, 0, 0, 1, 1, 1], "what the model was sent before stays as it was");
  assert.deepEqual(pictured(more), [-1, 1, 0, 2, 1, 1, 1], "the conversation itself keeps them");
});

// ── the service's notes ─────────────────────────────────────────────────────────────────────────────

const MODEL_NOTE = { kind: "input" as const, source: "model" as const, note: "The appendix tables came through the PDF unreadable." };
/** A cell that fetches and carries on whatever the fetch did. */
const fetches = (...urls: string[]) => urls.map((u) => `try { await fetch(${JSON.stringify(u)}) } catch {}`).join("\n");

/** DNS as the production guard asks it: `lan.example` is on the LAN, every other name is unknown. */
async function withDns<T>(work: () => Promise<T>): Promise<T> {
  const real = dns.lookup;
  (dns as { lookup: unknown }).lookup = (host: string, _opts: unknown, cb: (e: Error | null, a: unknown) => void) =>
    host === "lan.example" ? cb(null, [{ address: "192.168.0.1", family: 4 }]) : cb(Object.assign(new Error(`getaddrinfo ENOTFOUND ${host}`), { code: "ENOTFOUND" }), []);
  try {
    return await work();
  } finally {
    (dns as { lookup: unknown }).lookup = real;
  }
}

test("the service's notes follow the model's, one per kind with a count, the events after the submit included", async () => {
  const { models: m, faux } = models();
  faux.setResponses([
    cell(fetches("http://10.0.0.1/", "http://lan.example/a", "http://gone.example/x")),
    // After the submit: one more refused, two past the budget of four, then a cell timeout.
    cell(`await submit(${JSON.stringify({ ...draft(), process_notes: [MODEL_NOTE] })});\n${fetches("http://10.0.0.2/", "https://example.org/", "https://example.org/b")}\nwhile (true) {}`),
    reviews(PASSING),
  ]);
  const outcome = await withDns(() => run(m, { hosts: { maxFetches: 4 }, opts: { cellTimeoutMs: 300 } }));
  assert.equal(outcome.error, undefined);
  assert.deepEqual(outcome.result?.process_notes, [
    MODEL_NOTE,
    { kind: "access", source: "service", note: "3 fetches were refused: the address is not a public one.", url: "http://10.0.0.1/" },
    { kind: "access", source: "service", note: "A fetch failed on the network: DNS, the connection, a timeout or a body cut off.", url: "http://gone.example/x" },
    { kind: "budget", source: "service", note: "The fetch budget of 4 per paper was used up; 2 more fetches were refused." },
    { kind: "tool", source: "service", note: "A cell ran past its 0.3 s limit, which ended the session and lost its state." },
  ]);
});

test("the model can neither forge a service note or a review note nor drop one, and a quiet run adds none", async () => {
  const { models: m, faux } = models();
  const forged = { kind: "access", source: "service", note: "3 fetches were refused: the address is not a public one." };
  const passed = { kind: "review", source: "model", note: "The read passed its review with every score at five." };
  faux.setResponses([
    cell(`${fetches("http://10.0.0.1/")}\nawait submit(${JSON.stringify({ ...draft(), process_notes: [forged, passed] })})`),
    submits({ ...draft(), process_notes: [] }),
    reviews(PASSING),
  ]);
  const outcome = await run(m);
  const rejected = outcome.trace.filter((e) => e.type === "submit");
  assert.equal(rejected[0]?.ok, false);
  assert.ok(rejected[0]?.errors?.some((e) => e.startsWith("/process_notes/0/source")), "the service's source is refused from the model");
  assert.ok(rejected[0]?.errors?.some((e) => e.startsWith("/process_notes/1/kind: must be one of: access, tool, budget, input")), "and so is the review's kind");
  assert.deepEqual(outcome.result?.process_notes, [{ kind: "access", source: "service", note: "A fetch was refused: the address is not a public one.", url: "http://10.0.0.1/" }], "the model's empty list does not drop it");
});
