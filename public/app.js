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
