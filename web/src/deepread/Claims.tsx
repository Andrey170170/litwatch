import { useState } from "react";
import { Inline } from "./inline";
import type { Claim } from "./types";

const VERDICT: Record<Claim["verdict"], { key: string; icon: string }> = {
  holds: { key: "holds", icon: "✓" },
  partly: { key: "partly", icon: "◐" },
  "not shown": { key: "unshown", icon: "∅" },
  overclaimed: { key: "over", icon: "✕" },
};

/** One claim: its verdict chip first, then the claim; its evidence and where in the paper it rests open on a click.
 * Holds is quiet and the other verdicts are filled, so the eye goes to them. */
function ClaimRow({ claim }: { claim: Claim }) {
  const [open, setOpen] = useState(false);
  const v = VERDICT[claim.verdict];
  return (
    <div className="dr-cl" data-v={v.key}>
      <button onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className={`chip${claim.verdict === "holds" ? "" : " solid"}`}>
          <span aria-hidden>{v.icon}</span> {claim.verdict}
        </span>
        <span>
          <Inline text={claim.text} />
        </span>
      </button>
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
