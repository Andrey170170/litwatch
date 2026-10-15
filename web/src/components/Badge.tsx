import type { Schemas } from "@/api/client";

type Look = Pick<Schemas["Badge"], "name" | "depth" | "monogram" | "colour">;

/** A topic's badge: its monogram in a square of its colour, the depth by shape (DESIGN.md, anatomy). */
export function Badge({ topic, size }: { topic: Look; size?: "lg" | "sm" }) {
  return (
    <span className={`badge ${topic.depth}${size ? ` ${size}` : ""}`} style={{ "--tc": topic.colour } as React.CSSProperties} title={topic.name}>
      {topic.monogram}
    </span>
  );
}
