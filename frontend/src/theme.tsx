// Theme: light/dark (follows the OS by default), colour-vision palettes and
// extra contrast. Stored per browser; applied as data-* attributes on <html>.
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export type Mode = "system" | "light" | "dark";
export type Palette = "default" | "redgreen" | "blueyellow";
export interface ThemePrefs {
  mode: Mode;
  palette: Palette;
  highContrast: boolean;
}

const KEY = "woosarr.theme";
const DEFAULTS: ThemePrefs = { mode: "system", palette: "default", highContrast: false };

function load(): ThemePrefs {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? { ...DEFAULTS, ...(JSON.parse(raw) as Partial<ThemePrefs>) } : DEFAULTS;
  } catch {
    return DEFAULTS;
  }
}

function systemDark(): boolean {
  return typeof window !== "undefined" && !!window.matchMedia?.("(prefers-color-scheme: dark)").matches;
}

interface ThemeCtx {
  prefs: ThemePrefs;
  resolved: "light" | "dark";
  setPrefs: (p: Partial<ThemePrefs>) => void;
}

const Ctx = createContext<ThemeCtx | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [prefs, setState] = useState<ThemePrefs>(load);
  const [osDark, setOsDark] = useState(systemDark);

  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!mq) return;
    const onChange = () => setOsDark(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  const resolved = prefs.mode === "system" ? (osDark ? "dark" : "light") : prefs.mode;

  useEffect(() => {
    const root = document.documentElement;
    root.dataset.theme = resolved;
    root.dataset.palette = prefs.palette;
    root.dataset.contrast = prefs.highContrast ? "high" : "normal";
  }, [resolved, prefs.palette, prefs.highContrast]);

  const value = useMemo<ThemeCtx>(
    () => ({
      prefs,
      resolved,
      setPrefs: (p) =>
        setState((prev) => {
          const next = { ...prev, ...p };
          try {
            localStorage.setItem(KEY, JSON.stringify(next));
          } catch {
            /* storage blocked: keep in memory only */
          }
          return next;
        }),
    }),
    [prefs, resolved],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useTheme(): ThemeCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useTheme outside ThemeProvider");
  return ctx;
}
