const runId = location.pathname.split("/").filter(Boolean)[1];
const statusEl = document.querySelector("#run-status");
const skeleton = document.querySelector("#run-skeleton");
const playerWrap = document.querySelector("#run-player-wrap");
const player = document.querySelector("#run-player");
const metaEl = document.querySelector("#run-meta");
const downloadEl = document.querySelector("#run-download");
const titleEl = document.querySelector("#run-title");
const barFill = document.querySelector(".run-skeleton-bar-fill");

function show(el) {
  el?.classList.remove("hidden");
}

function hide(el) {
  el?.classList.add("hidden");
}

function setStatus(text, isError = false) {
  if (!statusEl) return;
  statusEl.textContent = text;
  statusEl.classList.toggle("error", isError);
  if (isError && barFill) barFill.style.animation = "none";
}

function renderMeta(run) {
  if (!metaEl) return;
  const bits = [];
  if (run.duration_s) bits.push(`${Math.round(run.duration_s)}s`);
  if (run.critic_scores?.length) {
    const best = Math.max(...run.critic_scores);
    bits.push(`critic ${(best * 100).toFixed(0)}/100`);
  }
  if (run.total_tokens) bits.push(`${run.total_tokens.toLocaleString()} tokens`);
  metaEl.textContent = bits.join(" · ");
  show(metaEl);
}

const STATUS_LABELS = {
  queued: "Queued — waiting for a slot…",
  running: "Generating your demo video…",
};

async function poll() {
  if (!runId) {
    setStatus("Invalid run URL.", true);
    return;
  }

  const deadline = Date.now() + 25 * 60 * 1000;
  for (;;) {
    try {
      const resp = await fetch(`/api/runs/${encodeURIComponent(runId)}`);
      if (resp.status === 404) {
        setStatus("Run not found.", true);
        return;
      }
      if (!resp.ok) {
        setStatus("Unable to load run status.", true);
        return;
      }
      const run = await resp.json();

      if (run.status === "completed") {
        const src = run.video_url || run.final_cut_url;
        if (src) {
          hide(skeleton);
          if (player) {
            player.src = src;
            show(player);
          }
          show(downloadEl);
          if (downloadEl) {
            downloadEl.href = src;
          }
        } else {
          setStatus("Run completed but video is not available yet.", true);
          return;
        }
        renderMeta(run);
        return;
      }

      if (run.status === "failed") {
        setStatus(run.error ? `Run failed: ${run.error}` : "Run failed.", true);
        return;
      }

      setStatus(STATUS_LABELS[run.status] || `Status: ${run.status}…`);
    } catch {
      setStatus("Connection error — retrying…");
    }

    if (Date.now() > deadline) {
      setStatus("This run is taking longer than expected. Check back shortly.", true);
      return;
    }
    await new Promise((r) => setTimeout(r, 5000));
  }
}

void poll();
