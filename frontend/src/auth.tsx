// Session state: first-run setup, login, logout. The CSRF token from the
// server is kept in memory (api.ts) and sent on every unsafe request.
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, setCsrfToken, setUnauthorizedHandler, type AuthStatus, type LoginOut } from "./api";

interface AuthCtx {
  status: AuthStatus | null;
  error: unknown;
  refresh: () => Promise<void>;
  login: (username: string, password: string) => Promise<void>;
  setupAdmin: (username: string, password: string, setupCode: string) => Promise<void>;
  logout: () => Promise<void>;
}

const Ctx = createContext<AuthCtx | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [error, setError] = useState<unknown>(null);

  const refresh = useCallback(async () => {
    try {
      const s = await api.get<AuthStatus>("/api/v1/auth/status");
      setCsrfToken(s.csrf_token);
      setStatus(s);
      setError(null);
    } catch (e) {
      setError(e);
    }
  }, []);

  useEffect(() => {
    void refresh();
    // A 401 anywhere means the session ended: go back to the login screen.
    setUnauthorizedHandler(() => void refresh());
    return () => setUnauthorizedHandler(null);
  }, [refresh]);

  const afterLogin = useCallback(
    async (out: LoginOut) => {
      setCsrfToken(out.csrf_token);
      await refresh();
    },
    [refresh],
  );

  const value: AuthCtx = {
    status,
    error,
    refresh,
    login: async (username, password) => afterLogin(await api.post<LoginOut>("/api/v1/auth/login", { username, password })),
    setupAdmin: async (username, password, setupCode) =>
      afterLogin(
        await api.post<LoginOut>("/api/v1/setup/admin", { username, password, setup_code: setupCode || null }),
      ),
    logout: async () => {
      try {
        await api.post("/api/v1/auth/logout");
      } finally {
        setCsrfToken(null);
        await refresh();
      }
    },
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
