const form = document.querySelector("#create-form");
const message = document.querySelector("#form-message");

function showMessage(text, isError = false) {
  message.textContent = text;
  message.classList.toggle("error", isError);
  message.classList.add("visible");
}

async function watchRun(runId) {
  const deadline = Date.now() + 20 * 60 * 1000;
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, 5000));
    if (Date.now() > deadline) {
      showMessage("This run timed out. Please try again.", true);
      return;
    }
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}`);
    if (!response.ok) return;
    const run = await response.json();
    if (run.status === "completed") {
      message.replaceChildren(
        document.createTextNode("Final cut ready. "),
        Object.assign(document.createElement("a"), {
          href: run.final_cut_url,
          textContent: "Watch and download →",
          target: "_blank",
          rel: "noopener",
        }),
      );
      return;
    }
    if (run.status === "failed") {
      showMessage("This run failed. Please try again.", true);
      return;
    }
    showMessage(`Run ${runId}: ${run.status}…`);
  }
}

// ── Login flow for authenticated app capture ──────────────────

function showLoginOption(runId) {
  const loginBtn = document.createElement("button");
  loginBtn.type = "button";
  loginBtn.className = "login-btn";
  loginBtn.textContent = "Login to your app (optional)";
  loginBtn.addEventListener("click", () => startLogin(runId, loginBtn));
  message.after(loginBtn);
}

async function startLogin(runId, loginBtn) {
  loginBtn.disabled = true;
  loginBtn.textContent = "Starting login session…";

  try {
    const resp = await fetch(`/api/runs/${encodeURIComponent(runId)}/login`, {
      method: "POST",
      headers: { "content-type": "application/json" },
    });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.message || "Failed to start login session");
    }

    showLiveViewModal(runId, data);
  } catch (err) {
    loginBtn.textContent = err instanceof Error ? err.message : "Login failed";
    loginBtn.disabled = false;
  }
}

async function loadRfb() {
  const mod = await import(
    "https://cdn.jsdelivr.net/npm/@novnc/novnc@1.5.0/lib/rfb.js"
  );
  return mod.default;
}

function showLiveViewModal(runId, session) {
  const overlay = document.createElement("div");
  overlay.className = "live-view-overlay";
  overlay.innerHTML = `
    <div class="live-view-modal" role="dialog" aria-modal="true" aria-label="Log in to your app">
      <div class="live-view-header">
        <h3>Log in to your app</h3>
        <p>Complete your login below. Handles SSO, MFA, CAPTCHAs, and passkeys.</p>
      </div>
      <div class="live-view-canvas-wrap">
        <p class="live-view-loading">Connecting to browser session…</p>
      </div>
      <div class="live-view-footer">
        <button type="button" class="live-view-done">I'm logged in</button>
        <button type="button" class="live-view-cancel">Cancel</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  const wrap = overlay.querySelector(".live-view-canvas-wrap");
  const loading = overlay.querySelector(".live-view-loading");
  const doneBtn = overlay.querySelector(".live-view-done");
  const cancelBtn = overlay.querySelector(".live-view-cancel");

  let rfb = null;
  let closed = false;

  function setStatus(text, isError = false) {
    if (!loading.isConnected) return;
    loading.textContent = text;
    loading.classList.toggle("error", isError);
  }

  async function closeSession({ complete = false } = {}) {
    if (closed) return;
    closed = true;

    try {
      rfb?.disconnect();
    } catch {
      /* ignore */
    }
    rfb = null;

    if (!complete) {
      try {
        await fetch(`/api/runs/${encodeURIComponent(runId)}/login/cancel`, {
          method: "POST",
          headers: { "content-type": "application/json" },
        });
      } catch {
        /* best-effort */
      }
    }

    overlay.remove();
    window.removeEventListener("keydown", onKey);
  }

  function onKey(event) {
    if (event.key === "Escape") {
      event.preventDefault();
      void closeSession();
      document.querySelector(".login-btn")?.remove();
    }
  }
  window.addEventListener("keydown", onKey);

  doneBtn.addEventListener("click", async () => {
    doneBtn.disabled = true;
    cancelBtn.disabled = true;
    doneBtn.textContent = "Capturing session…";
    setStatus("Capturing authenticated session…");
    try {
      const resp = await fetch(
        `/api/runs/${encodeURIComponent(runId)}/login/complete`,
        { method: "POST", headers: { "content-type": "application/json" } },
      );
      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.message || "Failed to capture login state");
      }
      showMessage("Login captured. Capture shots will use your authenticated session.");
      document.querySelector(".login-btn")?.remove();
      await closeSession({ complete: true });
    } catch (err) {
      doneBtn.disabled = false;
      cancelBtn.disabled = false;
      doneBtn.textContent = "I'm logged in";
      setStatus(
        err instanceof Error ? err.message : "Failed to capture login",
        true,
      );
    }
  });

  cancelBtn.addEventListener("click", () => {
    void closeSession();
    document.querySelector(".login-btn")?.remove();
  });

  // Connect live-view through the Cloudflare DO proxy
  void (async () => {
    try {
      if (!session.token || !session.backend) {
        throw new Error("Login session is missing connection details");
      }

      const RFB = await loadRfb();
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const liveUrl =
        `${proto}://${location.host}/live/${encodeURIComponent(session.session_id)}` +
        `?token=${encodeURIComponent(session.token)}` +
        `&backend=${encodeURIComponent(session.backend)}`;

      wrap.innerHTML = "";
      rfb = new RFB(wrap, liveUrl, { shared: true });
      rfb.scaleViewport = true;
      rfb.resizeSession = false;
      rfb.focusOnClick = true;

      rfb.addEventListener("connect", () => {
        setStatus("");
      });
      rfb.addEventListener("disconnect", (e) => {
        if (closed) return;
        if (!e.detail?.clean) {
          wrap.replaceChildren(
            Object.assign(document.createElement("p"), {
              className: "live-view-loading error",
              textContent: "Browser session disconnected. Cancel and try again.",
            }),
          );
        }
      });
      rfb.addEventListener("credentialsrequired", () => {
        rfb.sendCredentials({ password: session.vnc_password || "" });
      });
    } catch (err) {
      setStatus(
        err instanceof Error ? err.message : "Failed to connect to browser session",
        true,
      );
    }
  })();
}

// ── Form submission ───────────────────────────────────────────

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = form.querySelector("button");
  const data = new FormData(form);
  const payload = {
    repo_url: data.get("repo_url"),
    app_url: data.get("app_url") || null,
  };

  button.disabled = true;
  showMessage("Handing your project to the showrunner…");

  try {
    const response = await fetch("/api/runs", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.message || "Unable to start this run.");
    }
    showMessage(`Run ${result.run_id} started. The first cut is now in production.`);
    form.reset();

    if (result.login_available) {
      showLoginOption(result.run_id);
    }

    void watchRun(result.run_id);
  } catch (error) {
    showMessage(
      error instanceof Error ? error.message : "Unable to start this run.",
      true,
    );
  } finally {
    button.disabled = false;
  }
});
