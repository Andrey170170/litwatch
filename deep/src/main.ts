/**
 * litwatch-deep entry point.
 *   serve          run the HTTP service (default)
 *   login          Sign in with ChatGPT (paste the final redirect URL back)
 *   run FILE.json  process one paper from a JSON file and print the outcome (development);
 *                  the file is a PaperInput, optionally with `extensions` and `selections` as in POST /process
 */

import { readFileSync, writeFileSync } from "node:fs";
import { processPaper, type RunOptions } from "./agent.ts";
import { authState, buildModels, login } from "./auth.ts";
import type { HostConfig } from "./hosts.ts";
import { JobQueue } from "./jobs.ts";
import type { PaperInput, SelectionContext } from "./prompt.ts";
import { buildServer } from "./server.ts";

const env = (name: string, fallback: string) => process.env[name] ?? fallback;
/** A numeric setting in its unit, as a whole number from 1 to `max` once multiplied by `scale`, refused at start
 * otherwise: a limit set to Infinity, a negative, a typo, or past what its consumer holds would quietly lift it. */
const whole = (name: string, fallback: number, scale = 1, max = Number.MAX_SAFE_INTEGER): number => {
  const raw = env(name, String(fallback));
  const value = Math.floor(Number(raw) * scale);
  if (!(value >= 1 && value <= max)) throw new Error(`${name} is out of range: ${JSON.stringify(raw)}`);
  return value;
};
/** Node's timers fire at once past this many milliseconds. */
const MAX_TIMER_MS = 2 ** 31 - 1;
const secret = (name: string): string | undefined => {
  if (process.env[name]) return process.env[name];
  const file = process.env[`${name}_FILE`];
  return file ? readFileSync(file, "utf8").trim() : undefined;
};

const config = {
  port: whole("DEEP_PORT", 8081, 1, 65535),
  authFile: env("DEEP_AUTH_FILE", "/var/lib/litwatch-deep/auth.json"),
  hosts: {
    litwatchUrl: env("LITWATCH_URL", "http://litwatch:8080"),
    litwatchToken: secret("LITWATCH_TOKEN"),
    s2ApiKey: secret("S2_API_KEY"),
    maxFetchChars: whole("DEEP_MAX_FETCH_CHARS", 200000),
    maxFetches: whole("DEEP_MAX_FETCHES", 60),
    maxFetchBytes: whole("DEEP_MAX_FETCH_MB", 2, 1024 * 1024),
  } satisfies HostConfig,
  run: {
    model: env("DEEP_MODEL", "gpt-6.1-sol"),
    effort: env("DEEP_EFFORT", "medium") as RunOptions["effort"],
    maxCells: whole("DEEP_MAX_CELLS", 40),
    cellTimeoutMs: whole("DEEP_CELL_TIMEOUT_S", 90, 1000, MAX_TIMER_MS),
    wallClockMs: whole("DEEP_WALL_CLOCK_MIN", 30, 60_000, MAX_TIMER_MS),
    maxOutputChars: whole("DEEP_MAX_OUTPUT_CHARS", 8000),
  } satisfies RunOptions,
};

const log = (msg: string) => console.log(`${new Date().toISOString()} ${msg}`);

async function main(): Promise<void> {
  const [command = "serve", arg] = process.argv.slice(2);
  const models = buildModels(config.authFile);

  if (command === "login") {
    await login(models, config.authFile, console.log);
    return;
  }

  if (command === "run") {
    if (!arg) throw new Error("usage: run FILE.json");
    const { extensions = [], selections = [], ...paper } = JSON.parse(readFileSync(arg, "utf8")) as PaperInput & {
      extensions?: string[];
      selections?: SelectionContext[];
    };
    const outcome = await processPaper(paper, { extensions, selections }, models, config.hosts, config.run, (event, stats) =>
      log(`${event.type} cells=${stats.cells} turns=${stats.turns} tokens=${stats.input}/${stats.output} $${stats.list_cost.toFixed(2)}`),
    );
    const out = arg.replace(/\.json$/, "") + ".outcome.json";
    writeFileSync(out, JSON.stringify(outcome, null, 2));
    console.log(JSON.stringify({ error: outcome.error, stats: outcome.stats, result: outcome.result }, null, 2));
    console.log(`trace written to ${out}`);
    return;
  }

  if (command !== "serve") throw new Error(`unknown command ${command}`);
  const jobs = new JobQueue(({ paper, extensions, selections }, progress) =>
    processPaper(paper, { extensions, selections }, models, config.hosts, config.run, (event, stats) => {
      progress(event, stats);
      if (event.type === "cell" || event.type === "submit" || event.type === "system")
        log(`${paper.id} ${event.type}${event.type === "cell" ? ` ${event.n}` : ""} cells=${stats.cells} turns=${stats.turns} $${stats.list_cost.toFixed(2)}`);
    }),
  );
  const server = buildServer({
    jobs,
    health: async () => ({ ok: true, model: `${config.run.model}@${config.run.effort}`, auth: await authState(models) }),
  });
  server.listen(config.port, () => log(`litwatch-deep listening on ${config.port} (auth file ${config.authFile})`));
  const stop = () => server.close(() => process.exit(0));
  process.on("SIGTERM", stop);
  process.on("SIGINT", stop);
}

main().catch((e) => {
  console.error(e instanceof Error ? e.message : e);
  process.exit(1);
});
