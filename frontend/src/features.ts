import { useEffect, useState } from "react";
import { api, type DiscordStatus, type FeatureOut } from "./api";

/** Bot features and whether the bot's Discord permissions allow them. */
export function useDiscordFeatures() {
  const [features, setFeatures] = useState<FeatureOut[] | null>(null);
  useEffect(() => {
    api.get<DiscordStatus>("/api/v1/discord/status").then((s) => setFeatures(s.features ?? []), () => setFeatures([]));
  }, []);
  const get = (key: string) => features?.find((f) => f.key === key);
  const why = (key: string): string | null => {
    const f = get(key);
    if (!f) return "Discord isn't connected";
    if (f.available) return null;
    const parts = [...(f.missing.length ? [`missing ${f.missing.join(", ")}`] : []), ...f.notes];
    return `${f.label} unavailable: ${parts.join("; ")}`;
  };
  return { features, available: (key: string) => !!get(key)?.available, why };
}
