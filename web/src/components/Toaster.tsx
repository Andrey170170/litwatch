import { useToasts } from "@/lib/toast";

/** The acknowledgement toasts, newest last. */
export function Toaster() {
  const toasts = useToasts();
  return (
    <>
      {toasts.map((t) => (
        <div key={t.id} className={`toast cut-tr${t.tone === "error" ? " error" : ""}`} role="status">
          {t.text}
        </div>
      ))}
    </>
  );
}
