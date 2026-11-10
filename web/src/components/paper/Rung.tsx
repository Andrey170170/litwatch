import type { HTMLAttributes, ReactNode } from "react";

type Props = { name: ReactNode; extra?: ReactNode; className?: string; children: ReactNode } & Omit<HTMLAttributes<HTMLElement>, "className" | "children">;

/** One rung of the ladder (DESIGN.md, "The ladder"): its name in the narrow side column, its body beside it. Anything
 * else given (an id, a style) goes on the rung's element. */
export function Rung({ name, extra, className, children, ...rest }: Props) {
  return (
    <section className={`rung${className ? ` ${className}` : ""}`} {...rest}>
      <div className="rl">
        {name}
        {extra}
      </div>
      <div className="rb">{children}</div>
    </section>
  );
}
