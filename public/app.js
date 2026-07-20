const form = document.querySelector("#create-form");
const message = document.querySelector("#form-message");

function showMessage(text, isError = false) {
  message.textContent = text;
  message.classList.toggle("error", isError);
  message.classList.add("visible");
}

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
    window.location.href = `/runs/${encodeURIComponent(result.run_id)}`;
  } catch (error) {
    showMessage(
      error instanceof Error ? error.message : "Unable to start this run.",
      true,
    );
    button.disabled = false;
  }
});
