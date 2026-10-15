import { useState, type FormEvent } from "react";
import { signIn } from "@/api/client";

/** The token prompt on first use. It covers the page and keeps the route underneath. */
export function TokenPrompt({ onSignedIn }: { onSignedIn: () => void }) {
  const [token, setToken] = useState("");
  const [problem, setProblem] = useState<"wrong" | "offline" | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!token.trim()) return;
    setBusy(true);
    try {
      if (await signIn(token.trim())) onSignedIn();
      else setProblem("wrong");
    } catch {
      setProblem("offline");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="gate" role="dialog" aria-modal>
      <form onSubmit={submit} className="w-full max-w-sm">
        <div className="wm justify-center">litwatch</div>
        <p className="mt-6 text-[14px] text-fg2">This copy of litwatch is yours. Paste its token once; the browser keeps it.</p>
        <input
          type="password"
          autoFocus
          autoComplete="current-password"
          value={token}
          onChange={(e) => {
            setToken(e.target.value);
            setProblem(null);
          }}
          placeholder="Token"
          aria-label="Token"
          aria-invalid={problem === "wrong"}
          className="field mt-4 h-11"
        />
        {problem === "wrong" && <p className="mt-2 text-[13px] text-sig">That token was not accepted.</p>}
        {problem === "offline" && <p className="mt-2 text-[13px] text-sig">Could not reach litwatch. Check the connection and try again.</p>}
        <button type="submit" disabled={busy || !token.trim()} className="btn pri mt-4 h-11 w-full">
          Open
        </button>
      </form>
    </div>
  );
}
