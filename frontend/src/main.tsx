import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { getHealth, type Health } from "./api";

function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getHealth().then(setHealth, (e: Error) => setError(e.message));
  }, []);

  return (
    <main style={{ fontFamily: "system-ui", padding: 32 }}>
      <h1 style={{ color: "#7c3aed" }}>WooSarr</h1>
      <p>Setup and dashboard are coming in a later phase.</p>
      <p>
        {health ? `Backend ${health.version} (API ${health.api}): ${health.status}` : error ?? "Checking backend…"}
      </p>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
