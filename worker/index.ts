const RUN_ID = /^run_[a-f0-9]{24}$/;

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

function runsPage(runId: string): Response {
  const html = `<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Run ${runId} — Sizzle</title>
    <link rel="icon" href="/favicon.ico" sizes="48x48" />
    <link rel="stylesheet" href="/styles.css" />
  </head>
  <body>
    <div class="grain" aria-hidden="true"></div>
    <header>
      <a class="brand" href="/" aria-label="Sizzle home">
        <img class="brand-mark" src="/logo.png" alt="" width="32" height="32" />
        <span>Sizzle</span>
      </a>
      <nav aria-label="Main navigation">
        <a href="/">New run</a>
      </nav>
    </header>
    <main class="run-page">
      <p class="eyebrow">DEMO RUN</p>
      <h1 id="run-title">${runId}</h1>

      <div id="run-player-wrap" class="run-player-wrap">
        <div id="run-skeleton" class="run-skeleton">
          <div class="run-skeleton-icon">
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>
          </div>
          <p id="run-status" class="run-skeleton-status" role="status">Checking run status…</p>
          <div class="run-skeleton-bar"><div class="run-skeleton-bar-fill"></div></div>
        </div>
        <video id="run-player" class="hidden" controls playsinline preload="metadata"></video>
      </div>

      <div id="run-meta" class="run-meta hidden"></div>
      <div class="run-actions">
        <a id="run-download" class="run-download hidden" href="/runs/${runId}/video.mp4" download="final_cut.mp4">Download MP4</a>
        <a class="run-back" href="/">Start another run</a>
      </div>
    </main>
    <script type="module" src="/run.js"></script>
  </body>
</html>`;
  return new Response(html, {
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

async function serveVideo(_request: Request, env: SizzleEnv, runId: string): Promise<Response> {
  const key = `runs/${runId}/final_cut.mp4`;
  const object = await env.VIDEOS.get(key);
  if (!object) {
    return new Response("Video not found", { status: 404 });
  }

  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set("accept-ranges", "bytes");
  headers.set("cache-control", "public, max-age=31536000, immutable");
  if (!headers.has("content-type")) {
    headers.set("content-type", "video/mp4");
  }
  if (object.httpEtag) {
    headers.set("etag", object.httpEtag);
  }

  return new Response(object.body, { status: 200, headers });
}

export default {
  async fetch(request, env): Promise<Response> {
    const url = new URL(request.url);
    const parts = url.pathname.split("/").filter(Boolean);

    // /runs/{run_id}/video.mp4
    if (
      request.method === "GET" &&
      parts.length === 3 &&
      parts[0] === "runs" &&
      parts[2] === "video.mp4" &&
      RUN_ID.test(parts[1])
    ) {
      return serveVideo(request, env, parts[1]);
    }

    // /runs/{run_id}
    if (request.method === "GET" && parts.length === 2 && parts[0] === "runs" && RUN_ID.test(parts[1])) {
      return runsPage(parts[1]);
    }

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
