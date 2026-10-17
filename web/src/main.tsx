import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { ApiError, onUnauthorised } from "./api/client";
import { TokenPrompt } from "./components/TokenPrompt";
import { applyAppearance, readAppearance } from "./lib/theme";
import { router } from "./router";
import "./styles.css";

// index.html put the forced side on the document before paint; the bar colour follows it here
applyAppearance(readAppearance());

// One retry, except that a 404 is an answer (a paper outside litwatch and the archive), not a failure.
const retry = (failures: number, error: unknown) => failures < 1 && !(error instanceof ApiError && error.status === 404);
const client = new QueryClient({ defaultOptions: { queries: { retry, staleTime: 10_000 } } });

function App() {
  const [gate, setGate] = useState(false);
  useEffect(() => onUnauthorised(() => setGate(true)), []);
  return (
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
      {gate && (
        <TokenPrompt
          onSignedIn={() => {
            setGate(false);
            void client.invalidateQueries();
          }}
        />
      )}
    </QueryClientProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

// The service worker makes the app installable and a share target (`public/sw.js`); it caches nothing, the app is
// always read live. Registered on the built app only: the dev server has no `/sw.js`.
if ("serviceWorker" in navigator && import.meta.env.PROD) {
  window.addEventListener("load", () => void navigator.serviceWorker.register("/sw.js"));
}
