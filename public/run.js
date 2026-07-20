const runId = location.pathname.split("/").filter(Boolean)[1];
const statusEl = document.querySelector("#run-status");
const playerWrap = document.querySelector("#run-player-wrap");
const player = document.querySelector("#run-player");
const metaEl = document.querySelector("#run-meta");
const downloadEl = document.querySelector("#run-download");

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
        setStatus("Final cut ready.");
        renderMeta(run);
        if (run.video_url || run.final_cut_url) {
          const src = run.video_url || run.final_cut_url;
          if (player) player.src = src;
          show(playerWrap);
          show(downloadEl);
          if (downloadEl && run.video_url) {
            downloadEl.href = run.video_url;
          } else if (downloadEl && run.final_cut_url) {
            downloadEl.href = run.final_cut_url;
          }
        } else {
          setStatus("Run completed but video is not available yet.", true);
        }
        return;
      }

      if (run.status === "failed") {
        setStatus(run.error ? `Run failed: ${run.error}` : "Run failed.", true);
        return;
      }

      setStatus(`Status: ${run.status}…`);
    } catch {
      setStatus("Connection error while checking run status.", true);
      return;
    }

    if (Date.now() > deadline) {
      setStatus("This run is taking longer than expected. Check back shortly.", true);
      return;
    }
    await new Promise((r) => setTimeout(r, 5000));
  }
}

void poll();
