import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { onUnauthorised } from "./api/client";
import { TokenPrompt } from "./components/TokenPrompt";
import { router } from "./router";
import "./styles.css";

const client = new QueryClient({ defaultOptions: { queries: { retry: 1, staleTime: 10_000 } } });

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

// Enough of a service worker to be installable; nothing is cached (the PWA proper comes with W4).
if ("serviceWorker" in navigator && import.meta.env.PROD) {
  window.addEventListener("load", () => void navigator.serviceWorker.register("/sw.js"));
}
