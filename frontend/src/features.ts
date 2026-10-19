import { useEffect, useState } from "react";
import { api, type DiscordStatus } from "./api";

/** Bot features and whether the bot's Discord permissions allow them. */
export function useDiscordFeatures() {
  // undefined while the first check runs, null when the status request failed.
  const [status, setStatus] = useState<DiscordStatus | null | undefined>(undefined);
  const connecting = !!status?.connecting;
  useEffect(() => {
    let live = true;
    const load = () => api.get<DiscordStatus>("/api/v1/discord/status").then((s) => live && setStatus(s), () => live && setStatus(null));
    void load();
    return () => { live = false; };
  }, []);
  // While the bot is still connecting, check again until it is ready.
  useEffect(() => {
    if (!connecting) return;
    const t = setInterval(() => {
      api.get<DiscordStatus>("/api/v1/discord/status").then(setStatus, () => undefined);
    }, 4000);
    return () => clearInterval(t);
  }, [connecting]);

  const features = status?.features ?? null;
  const get = (key: string) => features?.find((f) => f.key === key);
  /** True while WooSarr is still finding out (first check, or bot still connecting): not a problem yet. */
  const pending = status === undefined || connecting;
  const why = (key: string): string | null => {
    if (status === undefined) return "Checking the Discord connection…";
    if (status === null) return "Couldn't check the Discord connection";
    const f = get(key);
    if (f?.available) return null;
    if (!status.configured) return "Discord isn't set up";
    if (connecting) return "Connecting to Discord…";
    if (!status.connected) return `Discord isn't connected${status.detail ? `: ${status.detail}` : ""}`;
    if (!f) return "Discord isn't connected";
    const parts = [...(f.missing.length ? [`missing ${f.missing.join(", ")}`] : []), ...f.notes];
    return `${f.label} unavailable: ${parts.join("; ")}`;
  };
  return { features, available: (key: string) => !!get(key)?.available, why, pending };
}
