import { useParams } from "@tanstack/react-router";
import { Back } from "@/components/Shell";
import { Fill } from "@/components/slots";

const NAMES: Record<string, string> = {
  overview: "Overview",
  needs: "Needs you",
  calibrate: "Calibrate",
  misses: "Misses",
  topics: "Topics",
  taxonomy: "Taxonomy",
  runs: "Runs and cost",
  integrations: "Integrations",
  configuration: "Configuration",
};

/** Settings' place behind the gear; its pages come with W2. */
export function SettingsPage() {
  const { page } = useParams({ from: "/settings/$page" });
  const name = NAMES[page] ?? "Settings";
  return (
    <>
      <Fill name="title">
        <span className="t">{name}</span>
      </Fill>
      <Back />
      <div className="h1 mb-3">{name}</div>
      <p className="empty">Settings are not built yet.</p>
    </>
  );
}
