// Typed client for the WooSarr API. Types come from the OpenAPI snapshot
// (frontend/openapi.json -> src/generated/api.ts, see `make openapi`).

import type { components } from "./generated/api";

export type Schemas = components["schemas"];
export type AuthStatus = Schemas["AuthStatus"];
export type StatusOut = Schemas["StatusOut"];
export type AppSettings = Schemas["AppSettings"];
export type UserOut = Schemas["UserOut"];
export type RoleOut = Schemas["RoleOut"];
export type DiscordMemberRow = Schemas["DiscordMemberRow"];
export type RoleImpactOut = Schemas["RoleImpactOut"];
export type MemberRolesResult = Schemas["MemberRolesResult"];
export type LibraryOut = Schemas["LibraryOut"];
export type ImpactOut = Schemas["ImpactOut"];
export type AuditOut = Schemas["AuditOut"];
export type AuditPage = Schemas["AuditPage"];
export type LogEntry = Schemas["LogEntryOut"];
export type LogPage = Schemas["LogPage"];
export type AdminOut = Schemas["AdminOut"];
export type ApiKeyOut = Schemas["ApiKeyOut"];
export type ApiKeyCreated = Schemas["ApiKeyCreated"];
export type MemberOut = Schemas["MemberOut"];
export type PlanOut = Schemas["PlanOut"];
export type LoginOut = Schemas["LoginOut"];
export type PlexStatus = Schemas["PlexStatus"];
export type PlexServerOut = Schemas["PlexServerOut"];
export type PlexLoginStart = Schemas["PlexLoginStart"];
export type PlexLoginPoll = Schemas["PlexLoginPoll"];
export type DiscordStatus = Schemas["DiscordStatus"];
export type GuildOut = Schemas["GuildOut"];
export type InviteOut = Schemas["InviteOut"];
export type FeatureOut = Schemas["FeatureOut"];
export type ChannelOut = Schemas["ChannelOut"];
export type ReactionPostOut = Schemas["ReactionPostOut"];
export type ReactionPostIn = Schemas["ReactionPostIn"];
export type PublishOut = Schemas["PublishOut"];
export type RoleCreated = Schemas["RoleCreated"];
export type ProviderOut = Schemas["ProviderOut"];
export type ProviderIn = Schemas["ProviderIn"];
export type PresetOut = Schemas["PresetOut"];
export type PresetsOut = Schemas["PresetsOut"];
export type DiscoveryOut = Schemas["DiscoveryOut"];
export type TestResult = Schemas["TestResult"];
export type PublicProvider = Schemas["PublicProvider"];
export type SeerrStatus = Schemas["SeerrStatus"];
export type SeerrTest = Schemas["SeerrTest"];
export type RequestOut = Schemas["RequestOut"];

export interface Health {
  status: string;
  version: string;
  api: string;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public requestId?: string | null,
  ) {
    super(message);
  }
}

let csrfToken: string | null = null;
let onUnauthorized: (() => void) | null = null;

export function setCsrfToken(token: string | null | undefined): void {
  csrfToken = token ?? null;
}

export function setUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn;
}

function detailMessage(body: unknown, status: number): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((d) => {
          const item = d as { loc?: unknown[]; msg?: string };
          const where = item.loc ? item.loc.filter((p) => p !== "body").join(".") : "";
          return where ? `${where}: ${item.msg}` : item.msg;
        })
        .join("; ");
    }
  }
  if (body && typeof body === "object" && "error" in body) {
    return `Server error (request id ${(body as { request_id?: string }).request_id ?? "?"})`;
  }
  return `HTTP ${status}`;
}

export async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  fetchImpl: typeof fetch = fetch,
): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const res = await fetchImpl(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  const text = await res.text();
  const parsed: unknown = text ? safeJson(text) : null;
  if (!res.ok) {
    if (res.status === 401 && onUnauthorized && !path.startsWith("/api/v1/auth/")) onUnauthorized();
    throw new ApiError(res.status, detailMessage(parsed, res.status), res.headers.get("X-Request-ID"));
  }
  return parsed as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body ?? {}),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body ?? {}),
  del: <T>(path: string) => request<T>("DELETE", path),
};

export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== null && v !== undefined && v !== "") sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

export async function getHealth(fetchImpl: typeof fetch = fetch): Promise<Health> {
  const res = await fetchImpl("/api/v1/health");
  if (!res.ok) {
    throw new Error(`health check failed: HTTP ${res.status}`);
  }
  return (await res.json()) as Health;
}
