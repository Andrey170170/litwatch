import { Fill } from "@/components/slots";

/** The Digest tab's place; its Day, Week and History come with W3. */
export function DigestPage() {
  return (
    <>
      <Fill name="bar">
        <span className="t">Digest</span>
      </Fill>
      <div className="h1 mb-3">Digest</div>
      <p className="empty">The Digest is not built yet. Topics and Papers are.</p>
    </>
  );
}
