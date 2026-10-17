// Thin client for the WooSarr API. Response types will come from the generated
// OpenAPI types once P1-07 lands.

export interface Health {
  status: string;
  version: string;
  api: string;
}

export async function getHealth(fetchImpl: typeof fetch = fetch): Promise<Health> {
  const res = await fetchImpl("/api/v1/health");
  if (!res.ok) {
    throw new Error(`health check failed: HTTP ${res.status}`);
  }
  return (await res.json()) as Health;
}
