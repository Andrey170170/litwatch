/**
 * Cell rewriting for Jupyter semantics on top of V8 modules.
 *
 * A cell runs as an ES module so that top-level `await` works, but module scope does not
 * persist between cells. Every top-level declaration is therefore rewritten into an
 * assignment on `globalThis`, and the final expression statement is captured into
 * `globalThis.__last` so the REPL can echo it, the way a notebook does.
 */

import { parse, type Pattern, type Program } from "acorn";

export const LAST = "__last";

interface Edit {
  start: number;
  end: number;
  text: string;
}

export function rewriteCell(source: string): string {
  const ast: Program = parse(source, {
    ecmaVersion: "latest",
    sourceType: "module",
    allowAwaitOutsideFunction: true,
  });
  const edits: Edit[] = [];
  const hoisted: string[] = [];
  const body = ast.body;
  body.forEach((stmt, i) => {
    switch (stmt.type) {
      case "ImportDeclaration":
      case "ExportNamedDeclaration":
      case "ExportDefaultDeclaration":
      case "ExportAllDeclaration":
        throw new SyntaxError("import/export are not available; every helper is already a global");
      case "FunctionDeclaration":
        // Hoisted, so a cell may call a function defined further down, as in a script.
        hoisted.push(`globalThis.${stmt.id.name} = ${source.slice(stmt.start, stmt.end)};`);
        edits.push({ start: stmt.start, end: stmt.end, text: "" });
        break;
      case "ClassDeclaration":
        edits.push({ start: stmt.start, end: stmt.start, text: `globalThis.${stmt.id.name} = ` });
        edits.push({ start: stmt.end, end: stmt.end, text: ";" });
        break;
      case "VariableDeclaration": {
        const parts = stmt.declarations.map((d) => {
          const init = d.init ? source.slice(d.init.start, d.init.end) : "undefined";
          if (d.id.type === "Identifier") return `globalThis.${d.id.name} = ${init};`;
          return `(${globalize(source, d.id)} = ${init});`;
        });
        edits.push({ start: stmt.start, end: stmt.end, text: parts.join(" ") });
        break;
      }
      case "ExpressionStatement":
        if (i === body.length - 1) {
          const e = stmt.expression;
          edits.push({
            start: stmt.start,
            end: stmt.end,
            text: `globalThis.${LAST} = (${source.slice(e.start, e.end)});`,
          });
        }
        break;
      default:
        break;
    }
  });
  edits.sort((a, b) => b.start - a.start || b.end - a.end);
  let out = source;
  for (const e of edits) out = out.slice(0, e.start) + e.text + out.slice(e.end);
  return hoisted.length ? `${hoisted.join("\n")}\n${out}` : out;
}

/** A binding pattern as an assignment target whose leaves are `globalThis.<name>`. */
function globalize(source: string, pattern: Pattern): string {
  switch (pattern.type) {
    case "Identifier":
      return `globalThis.${pattern.name}`;
    case "ObjectPattern":
      return `{ ${pattern.properties
        .map((p) => {
          if (p.type === "RestElement") return `...${globalize(source, p.argument)}`;
          const key = p.computed
            ? `[${source.slice(p.key.start, p.key.end)}]`
            : source.slice(p.key.start, p.key.end);
          return `${key}: ${globalize(source, p.value)}`;
        })
        .join(", ")} }`;
    case "ArrayPattern":
      return `[${pattern.elements.map((el) => (el ? globalize(source, el) : "")).join(", ")}]`;
    case "AssignmentPattern":
      return `${globalize(source, pattern.left)} = ${source.slice(pattern.right.start, pattern.right.end)}`;
    case "RestElement":
      return `...${globalize(source, pattern.argument)}`;
    default:
      return source.slice(pattern.start, pattern.end);
  }
}
