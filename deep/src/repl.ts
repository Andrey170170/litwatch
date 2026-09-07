/**
 * One paper's JavaScript session: a V8 isolate that lives for the run, with notebook
 * semantics (state persists across cells, the last expression is echoed, an exception is
 * output). The isolate reaches the outside world only through the host functions given here.
 */

import { readFileSync } from "node:fs";
import ivm from "isolated-vm";
import { rewriteCell } from "./rewrite.ts";

const PRELUDE = readFileSync(new URL("./prelude.js", import.meta.url), "utf8");

export type HostFn = (args: unknown[]) => Promise<unknown>;

export interface ReplOptions {
  memoryMb: number;
  cellTimeoutMs: number;
  maxOutputChars: number;
}

export interface CellResult {
  output: string;
  error?: string;
  ms: number;
  truncated: boolean;
}

export const DEFAULT_REPL_OPTIONS: ReplOptions = {
  memoryMb: 256,
  cellTimeoutMs: 90_000,
  maxOutputChars: 8_000,
};

export class Repl {
  private isolate: ivm.Isolate;
  private context!: ivm.Context;
  private prints: string[] = [];
  private cells = 0;
  private onDone: ((error: string) => void) | undefined;
  /** Set once a cell timed out: the isolate is gone and so is every variable. */
  dead = false;

  private readonly globals: Record<string, string>;
  private readonly hosts: Record<string, HostFn>;
  private readonly opts: ReplOptions;

  private constructor(globals: Record<string, string>, hosts: Record<string, HostFn>, opts: ReplOptions) {
    this.globals = globals;
    this.hosts = hosts;
    this.opts = opts;
    this.isolate = new ivm.Isolate({ memoryLimit: opts.memoryMb });
  }

  /** `globals` are string-valued (the paper markdown and JSON metadata); see prelude.js. */
  static async create(
    globals: Record<string, string>,
    hosts: Record<string, HostFn>,
    opts: Partial<ReplOptions> = {},
  ): Promise<Repl> {
    const repl = new Repl(globals, hosts, { ...DEFAULT_REPL_OPTIONS, ...opts });
    await repl.init();
    return repl;
  }

  private async init(): Promise<void> {
    this.context = await this.isolate.createContext();
    const jail = this.context.global;
    await jail.set("_print", new ivm.Callback((s: string) => void this.prints.push(String(s))));
    await jail.set("_done", new ivm.Callback((error: string) => this.onDone?.(String(error))));
    await jail.set(
      "_host",
      new ivm.Reference(async (name: string, argsJson: string): Promise<string> => {
        const fn = this.hosts[name];
        if (!fn) return JSON.stringify({ error: `unknown host function ${name}` });
        try {
          const value = await fn(JSON.parse(argsJson));
          return JSON.stringify({ value: value ?? null });
        } catch (e) {
          return JSON.stringify({ error: e instanceof Error ? e.message : String(e) });
        }
      }),
    );
    for (const [k, v] of Object.entries(this.globals)) await jail.set(k, v);
    await this.context.eval(PRELUDE, { filename: "prelude.js" });
  }

  async run(code: string): Promise<CellResult> {
    const t0 = performance.now();
    const done = (output: string, error?: string) => ({
      ...truncate(output, this.opts.maxOutputChars),
      error,
      ms: Math.round(performance.now() - t0),
    });
    if (this.dead) return done("", "the session was terminated by an earlier timeout; all state is gone");
    this.prints = [];
    let rewritten: string;
    try {
      rewritten = rewriteCell(code);
    } catch (e) {
      return done("", `SyntaxError: ${e instanceof Error ? e.message : String(e)}`);
    }
    // The cell runs inside an async function and reports completion through `_done` rather
    // than through the evaluation promise: isolated-vm resolves a module's top-level-await
    // promise early when the chain crosses into the host, and the late rejection would then
    // surface as an uncaught exception in this process.
    const finished = new Promise<string>((resolve) => (this.onDone = resolve));
    const wrapped =
      `void (async () => { try {\n${rewritten}\n} catch (e) { _done(__errText(e)); return; } _done(""); })();`;
    let error: string | undefined;
    try {
      const failure = await this.withTimeout(
        this.context
          .eval(wrapped, { filename: `cell-${++this.cells}.js`, timeout: this.opts.cellTimeoutMs })
          .then(() => finished),
      );
      if (failure) error = failure;
    } catch (e) {
      // isolated-vm's own timeout can win the race against the host timer; either way the
      // contract is the same: the session is gone.
      error = /timed out/i.test(String(e)) ? this.kill() : formatError(e);
    }
    let last = "";
    if (!this.dead) {
      try {
        last = String(await this.context.eval("__takeLast()"));
      } catch {
        // nothing to echo
      }
    }
    return done([...this.prints, last].filter(Boolean).join("\n"), error);
  }

  /** Marks the session dead and frees the isolate; returns the message cells report. */
  private kill(): string {
    if (!this.dead) {
      this.dead = true;
      this.isolate.dispose();
    }
    return `cell ran longer than ${this.opts.cellTimeoutMs / 1000}s; the session was terminated and all state is lost`;
  }

  private async withTimeout<T>(work: Promise<T>): Promise<T> {
    let timer: NodeJS.Timeout | undefined;
    const limit = new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new Error(this.kill())), this.opts.cellTimeoutMs);
    });
    work.catch(() => undefined); // the loser of the race must not surface as unhandled
    try {
      return await Promise.race([work, limit]);
    } finally {
      clearTimeout(timer);
    }
  }

  dispose(): void {
    if (!this.dead && !this.isolate.isDisposed) this.isolate.dispose();
    this.dead = true;
  }
}

function truncate(text: string, max: number): { output: string; truncated: boolean } {
  if (text.length <= max) return { output: text, truncated: false };
  const head = text.slice(0, max);
  return {
    output: `${head}\n… [output truncated at ${max} characters of ${text.length}; print less or slice it]`,
    truncated: true,
  };
}

function formatError(e: unknown): string {
  return e instanceof Error ? `${e.name}: ${e.message}` : String(e);
}
