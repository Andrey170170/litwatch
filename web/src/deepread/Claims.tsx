import { useId, useState } from "react";
import { Inline } from "./inline";
import type { Claim } from "./types";

const VERDICT: Record<Claim["verdict"], { key: string; icon: string }> = {
  holds: { key: "holds", icon: "✓" },
  partly: { key: "partly", icon: "◐" },
  "not shown": { key: "unshown", icon: "∅" },
  overclaimed: { key: "over", icon: "✕" },
};

/** One claim: its verdict chip first, then the claim; its evidence and where in the paper it rests open on the
 * chip, or on a click anywhere on the line but a link in the claim. Holds is quiet and the other verdicts are
 * filled, so the eye goes to them. The chip is the button, named by its verdict and described by its claim, so the
 * claim's own links are not inside it. */
function ClaimRow({ claim }: { claim: Claim }) {
  const [open, setOpen] = useState(false);
  const text = useId();
  const v = VERDICT[claim.verdict];
  return (
    <div className="dr-cl" data-v={v.key}>
      <div className="row" onClick={(e) => !(e.target as Element).closest("a, button") && setOpen(!open)}>
        <button className={`chip${claim.verdict === "holds" ? "" : " solid"}`} onClick={() => setOpen(!open)} aria-expanded={open} aria-describedby={text}>
          <span aria-hidden>{v.icon}</span> {claim.verdict}
        </button>
        <span id={text}>
          <Inline text={claim.text} />
        </span>
      </div>
      {open && (
        <div className="ev">
          <Inline text={claim.evidence} /> <span className="src">· {claim.where}</span>
        </div>
      )}
    </div>
  );
}

/** The claims ledger: every claim of the read, one line each, in the read's order. */
export function Claims({ claims }: { claims: Claim[] }) {
  return (
    <div className="dr-claims">
      {claims.map((c, i) => (
        <ClaimRow key={i} claim={c} />
      ))}
    </div>
  );
}
