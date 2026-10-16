/** Configuration (DESIGN.md, "Configuration"): the models per role and litwatch.toml as loaded, read-only. */
import { useQuery } from "@tanstack/react-query";
import { configQuery } from "@/api/queries";
import { toToml } from "@/lib/settings";
import { Load, PageHead } from "./parts";

type Role = { provider?: string; model?: string; effort?: string | null };

export function ConfigurationPage() {
  const config = useQuery(configQuery);
  const models = (config.data?.models ?? {}) as Record<string, Role>;
  const roles = Object.entries(models).filter(([, r]) => r && typeof r === "object" && "model" in r);
  // The lab that trained a role's model, as the backend reads it: Anthropic's own, else the model's prefix on
  // OpenRouter ("openai/gpt-6.1-sol" is OpenAI's).
  const lab = (role: string) => (models[role]?.provider === "anthropic" ? "anthropic" : (models[role]?.model ?? "").split("/")[0] ?? "");
  const judge = lab("judge");
  const check = lab("check");
  return (
    <>
      <PageHead title="Configuration" sub="litwatch.toml as loaded; read-only here, the file is the source of truth. Secrets are masked." />
      <Load q={config} />
      {config.data && (
        <>
          <div className="cap">Models per role</div>
          <div className="mtbl c3">
            {roles.map(([role, r]) => (
              <div className="mr" key={role}>
                <span className="mono">{role}</span>
                <span className="faint">{r.provider}</span>
                <span className="mono">
                  {r.model}
                  {r.effort ? <span className="faint"> · {r.effort}</span> : null}
                </span>
              </div>
            ))}
          </div>
          {judge && check && (
            <div className={judge === check ? "nb badt" : "nres"} style={{ margin: "8px 0 18px" }}>
              {judge === check ? `judge and check both come from ${judge}; the spec asks for two labs.` : `judge and check come from different labs (${judge}, ${check}).`}
            </div>
          )}
          <div className="cap">litwatch.toml</div>
          <pre className="toml">{toToml(config.data)}</pre>
        </>
      )}
    </>
  );
}
