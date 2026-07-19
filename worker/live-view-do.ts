/**
 * Cloudflare Durable Object that proxies noVNC WebSocket connections between
 * the user's browser and the websockify endpoint on the FC login worker.
 *
 * Flow:
 * 1. User connects via WebSocket to /live/{session_id}?token={auth_token}
 * 2. DO validates the token against the session metadata in OSS
 * 3. DO opens a WebSocket to the FC container's websockify port
 * 4. Messages are proxied bidirectionally
 * 5. Both sides close when either disconnects
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
    const [clientWs, serverWs] = Object.values(new WebSocketPair());

    // Connect to the backend websockify
    try {
      this.backendWs = new WebSocket(this.state.backendWsUrl);
    } catch {
      return new Response("Cannot connect to backend", { status: 502 });
    }

    this.state.connected = true;

    // Proxy messages: client -> backend
    serverWs.addEventListener("message", (event) => {
      if (this.backendWs?.readyState === WebSocket.OPEN) {
        this.backendWs.send(event.data);
      }
    });

    // Proxy messages: backend -> client
    this.backendWs.addEventListener("message", (event) => {
      if (serverWs.readyState === WebSocket.OPEN) {
        serverWs.send(event.data);
      }
    });

    // Handle close from either side
    serverWs.addEventListener("close", () => {
      this.cleanup();
    });

    this.backendWs.addEventListener("close", () => {
      if (serverWs.readyState === WebSocket.OPEN) {
        serverWs.close(1000, "Backend disconnected");
      }
      this.state!.connected = false;
    });

    // Handle errors
    serverWs.addEventListener("error", () => {
      this.cleanup();
    });

    this.backendWs.addEventListener("error", () => {
      if (serverWs.readyState === WebSocket.OPEN) {
        serverWs.close(1011, "Backend error");
      }
      this.state!.connected = false;
    });

    serverWs.accept();

    return new Response(null, { status: 101, webSocket: clientWs });
  }

  private cleanup(): void {
    if (this.backendWs?.readyState === WebSocket.OPEN) {
      this.backendWs.close(1000, "Client disconnected");
    }
    this.backendWs = null;
    if (this.state) {
      this.state.connected = false;
    }
  }
}
