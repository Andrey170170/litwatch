/**
 * Numbers, as the paper writes them and as a read gives them (docs/plans/deep-read.md, R2). A read's number is the
 * paper's when the paper's text holds it in any common form: at the read's precision or finer (0.3412 for 0.34), as
 * a fraction or a percentage (0.34 for 34%), with a thousands separator, a suffix (1.2k, 3.4M), an exponent
 * (5e-5, 5 × 10^-5) or spelled out (twelve). Signs are not compared.
 */

/** A number found in a text: its value as written, the decimal place it was written to (0.34: 2; 1.2 × 10^3: -2),
 * and where. */
export interface Found {
  value: number;
  decimals: number;
  /** What a suffix multiplies it by (1.2k: 1000); 1 without one. */
  scale: number;
  percent: boolean;
  /** Written as a bare whole number: no decimals, percent sign, suffix or exponent. */
  bare: boolean;
  text: string;
  index: number;
}

const SCALES: Record<string, number> = { k: 1e3, K: 1e3, M: 1e6, B: 1e9, thousand: 1e3, million: 1e6, billion: 1e9, trillion: 1e12 };
const SUPERSCRIPTS: Record<string, string> = { "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9", "⁻": "-", "⁺": "+" };

// A power of ten on its own (10^{-4}), or a number: digits with thousands separators or plain, or a decimal
// written from its point (.95), an optional exponent (e-5, × 10^{-5}), then a percent sign or a suffix. Digits
// glued to a letter before them (Qwen3, L4H3) are part of a name, not a number.
const NUMBER = new RegExp(
  "(?<![\\w.])(?:" +
    "10\\s*\\^\\s*\\{?\\s*([-+−]?\\d+)\\s*\\}?" +
    "|(\\d{1,3}(?:,\\d{3})+(?:\\.\\d+)?|\\d+(?:\\.\\d+)?|\\.\\d+)" +
    "(?:\\s*(?:×|x|\\*|·)\\s*10\\s*\\^\\s*\\{?\\s*([-+−]?\\d+)\\s*\\}?|[eE]([-+−]?\\d+)(?![\\w.]))?" +
    "(?:\\s?(%|percent\\b)|\\s?(thousand|million|billion|trillion)\\b|([kKMB])(?![A-Za-z0-9]))?" +
    ")",
  "g",
);

/** A text with TeX's ways of writing a number made plain: a brace or thin space as thousands separator, \% and
 * \times, a superscript exponent. `numbersIn` reads the result, and its indexes are into it. */
export function plainText(text: string): string {
  return text
    .replace(/[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+/g, (s) => `^${[...s].map((c) => SUPERSCRIPTS[c]).join("")}`)
    .replace(/(\d)\{,\}(?=\d{3})/g, "$1,")
    .replace(/(\d)(?:\\[,;!]|~| | )(?=\d{3}(?!\d))/g, "$1,")
    .replace(/\\%/g, "%")
    .replace(/\\(?:times|cdot)\b\s*/g, "× ")
    .replace(/\$/g, " ");
}

/** Every number in a plain text (`plainText`), in order. */
export function numbersIn(plain: string): Found[] {
  const out: Found[] = [];
  for (const m of plain.matchAll(NUMBER)) {
    const [whole, power, digits, times, exp, percent, word, letter] = m;
    const exponent = Number((power ?? times ?? exp ?? "0").replace("−", "-"));
    const mantissa = power !== undefined ? 1 : Number(digits!.replaceAll(",", ""));
    const written = power !== undefined ? 0 : (digits!.split(".")[1]?.length ?? 0);
    const suffix = word ?? letter;
    out.push({
      value: mantissa * 10 ** exponent,
      decimals: written - exponent,
      scale: suffix ? SCALES[suffix]! : 1,
      percent: percent !== undefined,
      bare: written === 0 && exponent === 0 && power === undefined && !percent && !suffix,
      text: whole.trim(),
      index: m.index,
    });
  }
  return out;
}

const UNITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"];
const TENS = ["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"];
const WORDS = new RegExp(`\\b(?:(${TENS.join("|")})(?:[-\\s](${UNITS.slice(1, 10).join("|")}))?|(${UNITS.join("|")}))(?:\\s+(hundred|thousand|million|billion))?\\b|\\b(?:a|one)\\s+(hundred|thousand|million|billion|half|quarter|third)\\b|\\b(half|quarter)\\b`, "gi");
const MULTIPLES: Record<string, number> = { hundred: 100, thousand: 1e3, million: 1e6, billion: 1e9, half: 0.5, quarter: 0.25, third: 1 / 3 };

/** The numbers a text spells out: twelve, twenty-two, three hundred, a half. */
function spelled(text: string): number[] {
  const out: number[] = [];
  for (const m of text.matchAll(WORDS)) {
    const [, tens, unit, small, times, single, bare] = m.map((g) => g?.toLowerCase());
    if (single) out.push(MULTIPLES[single]!);
    else if (bare) out.push(MULTIPLES[bare]!);
    else {
      const base = tens ? (TENS.indexOf(tens) + 2) * 10 + (unit ? UNITS.indexOf(unit) : 0) : UNITS.indexOf(small!);
      out.push(times ? base * MULTIPLES[times]! : base);
    }
  }
  return out;
}

/** The numbers a paper's text holds, for `PaperNumbers.has`. */
export class PaperNumbers {
  private readonly values: Float64Array;

  /** `text` is the paper's text, a LaTeX source's without its comments. */
  constructor(text: string) {
    const values: number[] = spelled(text);
    for (const n of numbersIn(plainText(text))) {
      values.push(n.value);
      if (n.scale !== 1) values.push(n.value * n.scale);
    }
    this.values = Float64Array.from(values).sort();
  }

  /** Whether any of the paper's numbers lies in [low, high], a hair of tolerance for floating point included. */
  private within(low: number, high: number): boolean {
    const slack = Math.max(Math.abs(low), Math.abs(high)) * 1e-9;
    let a = 0;
    let b = this.values.length;
    while (a < b) {
      const mid = (a + b) >> 1;
      if (this.values[mid]! < low - slack) a = mid + 1;
      else b = mid;
    }
    return a < this.values.length && this.values[a]! <= high + slack;
  }

  /** Whether the paper holds a read's number: the same value at the read's precision, in the paper's own unit, or
   * with the read's suffix applied, or as the fraction of a percentage, or as the percentage of a fraction. */
  has(n: Found): boolean {
    // what the paper's number may be for it to round to the read's: half a unit of the read's last place either way
    const half = 0.5 * 10 ** -n.decimals;
    const [low, high] = [n.value - half, n.value + half];
    const forms = [1, ...(n.scale !== 1 ? [n.scale] : []), ...(n.percent ? [0.01] : []), ...(!n.percent && n.decimals > 0 && n.value <= 1 ? [100] : [])];
    return forms.some((f) => this.within(low * f, high * f));
  }
}
