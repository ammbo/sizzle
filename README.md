# Sizzle

An agent that reads your repo, operates your app, and ships the ≤3-minute demo video your hackathon requires — then grades its own cut and recuts until it passes. See [PRD.md](PRD.md).

A visual acceptance test on autonomously-produced footage is a closed perception-action loop.

## Architecture

![Sizzle architecture](docs/architecture.svg)

```
SizzleVideoAI.com (Cloudflare Worker + static assets)
        │ authenticated API proxy
        ▼
Alibaba Cloud Function Compute API ──> OSS job state + assets
        │ asynchronous invocation
        ▼
Alibaba Cloud pipeline worker
  repo + git history ─┐
  live app instance ──┴─> story engine ──> budget allocator ──┬─> HappyHorse / Wan  (GENERATE)
                            ^                                 ├─> capture agent     (CAPTURE)
                            │                                 └─> deterministic     (RENDER)
                            │                                            │
                            │                                    CosyVoice (VO)
                            │                                            │
                            └──────── critic (Qwen3.7-Plus) <── assembler
                                                │
                             3-minute cut + manifest + cost report ──> OSS
```

- **Story engine** (`qwen3.7-max`) writes a beat sheet: beats, typed shots, VO lines, ≤180s.
- **Budget allocator** defaults every shot to the cheapest lane, then runs a greedy knapsack over GENERATE candidates; losers demote to stylized RENDER.
- **Capture agent** (`qwen3.7-plus` + Playwright) drives the live app toward a goal, records, and a VLM verifier checks each take against the shot's acceptance predicate (3 attempts, then demote).
- **Critic** (`qwen3.7-plus`) watches sampled frames of each cut, scores against the judging rubric, and emits typed directives (`RETIME`, `RESHOOT`, `CUT`, `REORDER`, `REWRITE_VO`). Guards: max 3 iterations, monotonic best-cut retention, duration invariant, single-reshoot rule.
- **Assembler** (ffmpeg, deterministic) conforms shots, lays VO, burns captions, writes an SRT.

Every run emits `manifest.json`: per-shot model, tokens, wall time, attempts, verifier outcome, plus a cost report with `cost_per_finished_second` and the demotion list.

## Usage

```bash
uv venv && uv pip install -e .
uv run playwright install chromium        # only needed for the capture lane
export DASHSCOPE_API_KEY=sk-...           # Qwen Cloud, intl endpoint

sizzle make https://github.com/you/repo --app-url https://your-app.example
sizzle make https://github.com/you/repo --dry-run   # full pipeline offline, all lanes stubbed
```

MCP server (`make_demo_video(repo_url, app_url)`): `sizzle-mcp`.

Requires `ffmpeg` on PATH.

## Alibaba Cloud

The backend runs on Alibaba Cloud; Cloudflare is the public edge and never performs inference.

- [`infra/alicloud/api.py`](infra/alicloud/api.py): authenticated Function Compute HTTP API, OSS job state, async pipeline invocation.
- [`infra/alicloud/pipeline_worker.py`](infra/alicloud/pipeline_worker.py): complete Qwen Cloud pipeline worker and OSS artifact upload.
- [`infra/alicloud/render_worker.py`](infra/alicloud/render_worker.py): parallel deterministic render worker using OSS and DashScope.
