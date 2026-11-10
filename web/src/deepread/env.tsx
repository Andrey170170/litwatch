import { createContext, useContext, type ComponentType, type CSSProperties, type ReactNode } from "react";

/** What a Deep read needs from the page it is drawn on, so the same components serve the app and the render page. */
export type ReadEnv = {
  /** A figure's image address by its name: the API's in the app, a data URL on the render page. Undefined: none. */
  figureSrc: (name: string) => string | undefined;
  /** A topic's colour by its slug, for a lens. */
  topicColour: (slug: string) => string | undefined;
  /** A link to another paper's page. */
  PaperLink: ComponentType<{ id: string; className?: string; style?: CSSProperties; children: ReactNode }>;
  /** Load every figure at once instead of as it nears the view: the render page is screenshot whole. */
  eager?: boolean;
};

const Env = createContext<ReadEnv | null>(null);
export const ReadEnvProvider = Env.Provider;

export function useReadEnv(): ReadEnv {
  const env = useContext(Env);
  if (!env) throw new Error("a Deep read is drawn inside a ReadEnvProvider");
  return env;
}
