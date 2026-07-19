const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
  "x-content-type-options": "nosniff",
} as const;

interface Secrets {
  ALIBABA_BACKEND_URL?: string;
  ALIBABA_BACKEND_TOKEN?: string;
}

type SizzleEnv = Env & Secrets;

function json(body: unknown, status = 200): Response {
  return Response.json(body, { status, headers: JSON_HEADERS });
}

function requestId(request: Request): string {
  return request.headers.get("cf-ray") ?? crypto.randomUUID();
}

async function proxyToAlibaba(request: Request, env: SizzleEnv): Promise<Response> {
  if (!env.ALIBABA_BACKEND_URL || !env.ALIBABA_BACKEND_TOKEN) {
    return json(
      {
        error: "backend_not_configured",
        message: "The Alibaba Cloud production backend is awaiting configuration.",
      },
      503,
    );
  }

  const incoming = new URL(request.url);
  const upstreamBase = new URL(env.ALIBABA_BACKEND_URL);
  const upstream = new URL(incoming.pathname + incoming.search, upstreamBase);
  const headers = new Headers(request.headers);
  headers.set("authorization", `Bearer ${env.ALIBABA_BACKEND_TOKEN}`);
  headers.set("x-sizzle-request-id", requestId(request));
  headers.delete("cf-connecting-ip");
  headers.delete("cf-ray");
  headers.delete("host");

  try {
    const response = await fetch(
      new Request(upstream, {
        method: request.method,
        headers,
        body: request.body,
        redirect: "manual",
      }),
    );
    const responseHeaders = new Headers(response.headers);
    responseHeaders.set("cache-control", "no-store");
    responseHeaders.set("x-content-type-options", "nosniff");
    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: responseHeaders,
    });
  } catch (error) {
    console.error(
      JSON.stringify({
        event: "alibaba_proxy_error",
        requestId: requestId(request),
        error: error instanceof Error ? error.message : "unknown_error",
      }),
    );
    return json(
      {
        error: "backend_unavailable",
        message: "The production pipeline is temporarily unavailable.",
      },
      502,
    );
  }
}

export default {
  async fetch(request, env): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/api/health" && request.method === "GET") {
      if (!env.ALIBABA_BACKEND_URL) {
        return json({
          status: "setup_required",
          edge: "cloudflare",
          backend: "alibaba-cloud",
        });
      }
      return proxyToAlibaba(request, env);
    }

    if (url.pathname.startsWith("/api/")) {
      if (!["GET", "POST", "OPTIONS"].includes(request.method)) {
        return json({ error: "method_not_allowed" }, 405);
      }
      return proxyToAlibaba(request, env);
    }

    return env.ASSETS.fetch(request);
  },
} satisfies ExportedHandler<SizzleEnv>;
