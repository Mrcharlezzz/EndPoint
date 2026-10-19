type FetchLike = typeof fetch;

type Options = {
  baseUrl?: string | null;
  fetchImpl?: FetchLike;
};

function normalizeBaseUrl(baseUrl: string | null | undefined): string | null {
  const trimmed = baseUrl?.trim();
  if (!trimmed) return null;
  return trimmed.replace(/\/$/, "");
}

export async function hasValidSessionToken(
  sessionToken: string | undefined,
  options: Options = {},
): Promise<boolean> {
  const token = sessionToken?.trim();
  if (!token) return false;

  const baseUrl = normalizeBaseUrl(
    options.baseUrl ?? process.env.NEXT_PUBLIC_PRESENTATION_API_URL ?? "http://localhost:8001",
  );
  if (!baseUrl) return false;

  const fetchImpl = options.fetchImpl ?? fetch;

  try {
    const response = await fetchImpl(`${baseUrl}/api/v1/users/me/`, {
      cache: "no-store",
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${token}`,
      },
    });
    return response.ok;
  } catch {
    return false;
  }
}
