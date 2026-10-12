import { useState, type FormEvent } from "react";
import { signIn } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/** The token prompt on first use. It covers the page and keeps the route underneath. */
export function TokenPrompt({ onSignedIn }: { onSignedIn: () => void }) {
  const [token, setToken] = useState("");
  const [wrong, setWrong] = useState(false);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!token.trim()) return;
    setBusy(true);
    const good = await signIn(token.trim());
    setBusy(false);
    if (good) onSignedIn();
    else setWrong(true);
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-background p-6" role="dialog" aria-modal>
      <form onSubmit={submit} className="w-full max-w-sm">
        <h1 className="font-serif text-[28px] font-bold tracking-[.06em] text-center">Litwatch</h1>
        <p className="mt-6 text-[15px] text-body">This copy of litwatch is yours. Paste its token once; the browser keeps it.</p>
        <Input
          type="password"
          autoFocus
          autoComplete="current-password"
          value={token}
          onChange={(e) => {
            setToken(e.target.value);
            setWrong(false);
          }}
          placeholder="Token"
          aria-label="Token"
          aria-invalid={wrong}
          className="mt-4 h-11 text-[15px]"
        />
        {wrong && <p className="mt-2 text-[13px] text-destructive">That token was not accepted.</p>}
        <Button type="submit" disabled={busy || !token.trim()} className="mt-4 h-11 w-full text-[15px]">
          Open
        </Button>
      </form>
    </div>
  );
}
