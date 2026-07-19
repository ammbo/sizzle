/**
 * Cloudflare Durable Object that proxies noVNC WebSocket connections between
 * the user's browser and the websockify/VNC endpoint on the FC login worker.
 *
 * Flow:
 * 1. User connects via WebSocket to /live/{session_id}?token={auth_token}&backend={fc_ws_url}
 * 2. DO validates the token is present and opens a WebSocket to the FC backend
 * 3. Messages are proxied bidirectionally
 * 4. Both sides close when either disconnects
 */

import { DurableObject } from "cloudflare:workers";

interface SessionState {
  backendWsUrl: string;
  authToken: string;
  connected: boolean;
}

export class LiveViewSession extends DurableObject {
  private state: SessionState | null = null;
  private backendWs: WebSocket | null = null;

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const upgradeHeader = request.headers.get("Upgrade");

    if (upgradeHeader !== "websocket") {
      return new Response("Expected WebSocket upgrade", { status: 426 });
    }

    const token = url.searchParams.get("token");
    const backendWsUrl = url.searchParams.get("backend");

    if (!token || !backendWsUrl) {
      return new Response("Missing token or backend URL", { status: 400 });
    }

    // Store session state on first connection
    if (!this.state) {
      this.state = {
        backendWsUrl,
        authToken: token,
        connected: false,
      };
    }

    // Validate token
    if (token !== this.state.authToken) {
      return new Response("Unauthorized", { status: 401 });
    }

    // Only allow one connection at a time
    if (this.state.connected) {
      return new Response("Session already in use", { status: 409 });
    }

    // Create WebSocket pair for the client
    const pair = new WebSocketPair();
    const clientWs = pair[0];
    const serverWs = pair[1];

    // Connect to the backend (FC login HTTP trigger) via fetch upgrade.
    // Workers require an http(s) URL here — not ws(s).
    let backendUrl = this.state.backendWsUrl;
    if (backendUrl.startsWith("wss://")) {
      backendUrl = "https://" + backendUrl.slice("wss://".length);
    } else if (backendUrl.startsWith("ws://")) {
      backendUrl = "http://" + backendUrl.slice("ws://".length);
    }

    let backend: WebSocket;
    try {
      const resp = await fetch(backendUrl, {
        headers: {
          Upgrade: "websocket",
          Connection: "Upgrade",
          "Sec-WebSocket-Protocol": "binary",
        },
      });
      if (resp.status !== 101 || !resp.webSocket) {
        return new Response("Cannot connect to backend", { status: 502 });
      }
      backend = resp.webSocket;
      backend.accept();
    } catch {
      return new Response("Cannot connect to backend", { status: 502 });
    }

    this.backendWs = backend;
    this.state.connected = true;
    serverWs.accept();

    // Proxy messages: client -> backend
    serverWs.addEventListener("message", (event) => {
      if (this.backendWs?.readyState === WebSocket.OPEN) {
        try {
          this.backendWs.send(event.data);
        } catch {
          /* ignore */
        }
      }
    });

    // Proxy messages: backend -> client
    backend.addEventListener("message", (event) => {
      if (serverWs.readyState === WebSocket.OPEN) {
        try {
          serverWs.send(event.data);
        } catch {
          /* ignore */
        }
      }
    });

    serverWs.addEventListener("close", () => {
      this.cleanup();
    });

    backend.addEventListener("close", () => {
      if (serverWs.readyState === WebSocket.OPEN) {
        serverWs.close(1000, "Backend disconnected");
      }
      if (this.state) this.state.connected = false;
    });

    serverWs.addEventListener("error", () => {
      this.cleanup();
    });

    backend.addEventListener("error", () => {
      if (serverWs.readyState === WebSocket.OPEN) {
        serverWs.close(1011, "Backend error");
      }
      if (this.state) this.state.connected = false;
    });

    return new Response(null, {
      status: 101,
      webSocket: clientWs,
      headers: { "Sec-WebSocket-Protocol": "binary" },
    });
  }

  private cleanup(): void {
    if (this.backendWs?.readyState === WebSocket.OPEN) {
      try {
        this.backendWs.close(1000, "Client disconnected");
      } catch {
        /* ignore */
      }
    }
    this.backendWs = null;
    if (this.state) {
      this.state.connected = false;
    }
  }
}
