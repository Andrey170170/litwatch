/** Generated prose (a Brief, a digest's text): a paragraph for each block between blank lines. */
export function Prose({ text }: { text: string }) {
  return (
    <>
      {text
        .split(/\n\s*\n/)
        .filter((p) => p.trim())
        .map((p, i) => (
          <p key={i} className="prose">
            {p}
          </p>
        ))}
    </>
  );
}
