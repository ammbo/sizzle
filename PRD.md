# Sizzle — PRD

**Track:** 2 (AI Showrunner) · **Status:** draft · **Owner:** Ammon Brown

> An agent that reads your repo, operates your app, and ships the three-minute demo video your hackathon requires — then grades its own cut and recuts until it passes.

---

## 1. Problem

Every hackathon on earth requires a ~3-minute demo video. It is the single highest-leverage artifact in the submission (it is what judges actually watch) and it is universally made last, at 3am, by an exhausted builder with no editing skill and no time. The result is a screen recording with mumbled narration, and a good project loses to a worse project with a better video.

The pain is real, recurring, universal within the target population, and — critically — **the judges of this hackathon have personally experienced it.**

## 2. Insight

A demo video is not a recording. It is a **narrative** with a fixed 180-second budget: establish a pain, raise stakes, reveal the mechanism, prove it works, land the ask. That is a beat sheet. Beat sheets can be written, storyboarded, shot, and cut — which is precisely the pipeline Track 2 describes.

The subject happening to be software rather than a soap opera does not make it less of a show. It makes it a show with a **verifiable** ground truth, which is what turns a video generator into an agent: it can watch its own output and tell whether it worked.

## 3. Goals

| | |
|---|---|
| G1 | Given a repo URL and a live app URL, emit a ≤180s demo video with zero human input. |
| G2 | Every cut is scored by a multimodal critic against a rubric and revised until it plateaus. |
| G3 | Spend expensive video-generation tokens only where narrative demands; emit a cost report per run. |
| G4 | Prove generality: run on 13 real repos, not one cherry-picked demo. |
| G5 | The submission's own demo video is produced by Sizzle. |

## 4. Non-goals

- Not a general-purpose video editor. There is no timeline UI. Fire-and-forget is the product.
- Not a screen recorder. If a human has to drive the app, we have failed.
- Not multi-language in v1. English VO only.
- No licensed music. Generated or CC0 beds only.
- No human-in-the-loop editing pass. (This is a deliberate v1 constraint, not an oversight — see §12 R5.)

## 5. Users

- **Primary:** hackathon builders at T-minus-4-hours. Technical, time-poor, allergic to editing software.
- **Secondary:** OSS maintainers who need a project trailer; devrel teams shipping launch videos on a cadence.
- **Judge (this weekend):** a user who is watching the product's output *as* the submission. This is the only user whose experience is scored.

## 6. Submission thesis

The video is the product is the proof. We do not describe what Sizzle does — we play what Sizzle made.

The cut opens on the critic's notes about an earlier cut of itself ("Pacing flat 0:20–0:50. Value prop unclear. Recut.") and then plays the recut. This is not a gag; it is the shortest possible demonstration of the closed loop, and it pre-empts the "this looks AI-generated" reflex by naming it first.

---

## 7. Architecture

```
repo + git history ─┐
live app instance ──┴─> story engine ──> budget allocator ──┬─> HappyHorse / Wan  (GENERATE)
                          ^                                 ├─> capture agent     (CAPTURE)
                          │                                 └─> deterministic     (RENDER)
                          │                                            │
                          │                                    CosyVoice (VO)
                          │                                            │
                          │                                        assembler
                          │                                            │
                          └────────── critic (Qwen3.7-Plus) ───────────┘
                                              │
                                       3-minute cut
                                    + shot manifest
                                    + cost report
```

### 7.1 Components

| Component | Responsibility | Determinism |
|---|---|---|
| **Ingest** | Clone repo; parse README, manifests, issue titles; mine commit history for the build's arc. | Deterministic |
| **Story engine** | Emit a beat sheet: named beats, durations summing to ≤180s, typed shots, VO lines. | Qwen3.7-Max |
| **Budget allocator** | Assign each shot to GENERATE / CAPTURE / RENDER under a token budget. | Deterministic solver over model-scored inputs |
| **Capture agent** | Drive the live app to produce real footage; visually verify the money shot landed. | Qwen3.7-Plus (GUI operation) |
| **Generate lane** | Dramatized clips for beats with no product to film. | HappyHorse 1.1 / Wan 2.7 |
| **Render lane** | Title cards, code snippets, architecture stills, metric plates. | Deterministic (ffmpeg + templates) |
| **VO lane** | Narration from the beat sheet's VO lines. | CosyVoice v3-plus |
| **Assembler** | Conform shots to the duration budget, mix audio, burn captions. | Deterministic (ffmpeg) |
| **Critic** | Watch the rendered cut. Score against rubric. Emit typed revision directives. | Qwen3.7-Plus |

### 7.2 Model routing

| Role | Model | Why this one |
|---|---|---|
| Story engine, allocator scoring | `qwen3.7-max` | Long-horizon agentic reasoning, text-only is sufficient |
| Capture agent | `qwen3.7-plus` | Reads screens and operates GUIs; end-to-end navigation |
| Critic | `qwen3.7-plus` | Needs vision to watch frames; same model, different scorecard |
| Cold open / dramatized shots | `happyhorse-1.1-t2v`, `-i2v`, `-r2v` | Named in the track; r2v is our continuity tool |
| VO | `cosyvoice-v3-plus` | Voice cloning from 5–20s reference |

All served via Qwen Cloud. No non-Qwen model appears on the critical path (see §10.3 for where they *do* appear).

---

## 8. Data contracts

The beat sheet is the spine. Everything downstream is filling slots. Get this schema right on day one and the rest is plumbing.

### 8.1 Beat sheet

```jsonc
{
  "target_duration_s": 180,
  "project": { "name": "...", "one_liner": "...", "pain": "...", "mechanism": "..." },
  "beats": [
    {
      "id": "b01",
      "name": "cold_open",
      "intent": "establish the pain before the product exists",
      "duration_s": 15,
      "narrative_weight": 0.9,     // allocator input: how much this beat carries
      "filmable": false,           // no product to point a camera at -> GENERATE candidate
      "shots": ["s01"]
    }
  ],
  "shots": [
    {
      "id": "s01",
      "beat_id": "b01",
      "type": "GENERATE",          // assigned by allocator, not the story engine
      "duration_s": 15,
      "spec": { /* type-specific, see 8.2 */ },
      "acceptance": "frame shows a person at a desk at night, screen visible",
      "vo": "It's 3am. The deadline is in an hour. The video does not exist.",
      "cost_estimate": { "unit": "tokens", "value": 0 }
    }
  ]
}
```

### 8.2 Shot specs by type

```jsonc
// GENERATE
{ "prompt": "...", "ref_image": "oss://.../s00_last_frame.png", "continuity_ref": "s00" }

// CAPTURE  — a goal, not a script. The agent figures out the clicks.
{ "goal": "create a new project and show the generated output appearing",
  "start_url": "https://...", "viewport": [1280, 720], "max_steps": 25 }

// RENDER
{ "template": "code_snippet" | "architecture" | "title_card" | "metric_plate",
  "payload": { ... } }
```

### 8.3 Shot manifest (emitted per run)

Every finished video ships with a manifest: shot id, type, model called, tokens spent, wall time, attempts, verifier outcome, final duration. This is the artifact that makes the cost report and the eval possible. It is also what a judge reads when they want to know whether this is real.

---

## 9. Key algorithms

### 9.1 Budget allocator

The track says *maximize output quality under a limited token budget*. So make that a solver, not a vibe.

**Default every shot to the cheapest type that can carry it.** Promote to GENERATE only when the beat is unfilmable — either the product doesn't exist yet in the narrative (cold open, pain, stakes) or the shot depicts something no camera could reach.

```
for shot in shots:
    if beat.filmable and live_app_available:  candidate = CAPTURE
    elif shot.is_static_information:          candidate = RENDER
    else:                                     candidate = GENERATE

# then: greedy knapsack over GENERATE candidates
#   maximize  Σ narrative_weight
#   s.t.      Σ cost(shot) ≤ B
#   demote losers to RENDER with a stylized motion-graphics fallback
```

Emit: `cost_per_finished_second`, spend by shot type, and the demotion list. Chart it in the README.

### 9.2 Critic loop

Input: the rendered cut (sampled frames + VO transcript), the beat sheet, the scorecard.

**The scorecard is this hackathon's four judging criteria, verbatim, plus hard constraints** (duration ≤180s, money shot before 0:45, no dead air >2s).

Output is **typed directives, not prose** — this is what makes it a loop instead of a chat:

```
RETIME(beat_id, delta_s)
RESHOOT(shot_id, revised_spec)
CUT(shot_id)
REORDER([beat_id, ...])
REWRITE_VO(beat_id, text)
```

Termination: `max_iterations = 3` OR score delta < ε OR budget exhausted.

Guards:
- **Monotonic:** keep the highest-scoring cut ever produced, not the last one. Prevents a bad final iteration from shipping.
- **Duration invariant:** reject any directive set whose projected duration exceeds 180s.
- **No oscillation:** a shot may be RESHOOT once. Second failure demotes its type.

### 9.3 Capture verification

This is the difference between an agent and a screen recorder, and it's the thing nobody else in this track will have.

After recording, sample N frames and ask the VLM a closed question against the shot's `acceptance` predicate:

```jsonc
{ "satisfied": false,
  "failure_mode": "SPINNER" | "ERROR_STATE" | "WRONG_SCREEN" | "OCCLUDED" | "TIMEOUT",
  "evidence_frame": 41,
  "suggested_fix": "wait for network idle before capture" }
```

On failure: replan with the failure mode in context. Max 3 attempts, then fall back to RENDER. A visual acceptance test on autonomously-produced footage is a closed perception-action loop — say that in the README in exactly those words.

---

## 10. Evaluation

### 10.1 Corpus

Thirteen real hackathon repos: the ElevenHacks back catalog (DunningCall, CallWiz, OpenCawl, Quantext, VoiceMeet, HeckleBot, FUTURE YOU, WikiSounds, Mad Lib Music, Voisurf, RiffRoll, +2). Not cherry-picked. Each one already has a hand-made demo video, which means **we have human baselines for free.**

### 10.2 Metrics

| Metric | Definition | Target |
|---|---|---|
| Capture success rate | % shots passing the verifier within 3 attempts | >70% |
| Cost per finished second | Total token spend / 180 | Report + trend |
| Critic lift | Final score − iteration-0 score | >0, monotone |
| Duration conformance | \|actual − 180\| | <5s, 13/13 |
| Head-to-head | Blind A/B: Sizzle cut vs. the original hand-made video | Not embarrassing |

### 10.3 Baseline comparison — where non-Qwen models belong

Capture verification is a hand-labelable task. Build a ~100-frame labeled set (does this frame satisfy the predicate? what's the failure mode?) and score `qwen3.7-plus` against alternative VLMs on it.

This is the right home for any non-Qwen model: **as a measured baseline, not a dependency.** It converts a compliance liability into a technical-depth exhibit, it's honest, and it's the most on-brand possible move given the ModelMash lineage.

---

## 11. Infrastructure

| Layer | Service | Note |
|---|---|---|
| Inference | Qwen Cloud (Singapore region) | All models §7.2 |
| Asset store | Alibaba Cloud OSS | Shots, audio, renders, final cut, manifests |
| Render workers | Alibaba Cloud Function Compute | ffmpeg; stateless, parallel across shots |
| Capture workers | Alibaba Cloud ECS + xvfb | Needs a real display; **will not fit in FC** |
| Interface | MCP server: `make_demo_video(repo_url, app_url)` | Rubric names MCP explicitly |
| Ingest | GitHub MCP | Consume rather than shell out to git |

**Deployment proof file:** `infra/alicloud/render_worker.py` — instantiates the OSS client, submits the FC job, calls DashScope. One file, all three services, linkable from the submission form.

> The requirement says the backend must be *running* on Alibaba Cloud; the stated proof bar is only a linked code file. The letter is weaker than the spirit. Meet the spirit — 30% of the score is architecture quality, and "he technically linked a file" does not survive a tiebreak.

---

## 12. Risks

| # | Risk | Mitigation |
|---|---|---|
| R0 | **Alibaba Cloud account verification stalls.** The classic hackathon killer — no code, no clock, just KYC. | Do this hour zero, before writing a line. Blocking. |
| R1 | **Track fit.** A judge reads "short drama" and says a demo video isn't one. | The dramatized cold open makes it unarguable. Lead the pitch with "showrunner for software," not "video tool." |
| R2 | **HappyHorse quality at our budget is unknown.** | Spike it hour one, before designing a single beat around it. 3–15s clips at 720p/1080p is the known constraint; stitching is our problem, and r2v is the continuity tool. If quality disappoints → cold open becomes stylized motion graphics via RENDER. |
| R3 | **Capture agent flakiness on arbitrary repos.** | Scope v1 to web apps with a dev server (Next.js/Vite adapters). Fallback: user-supplied footage. Don't chase generality. |
| R4 | **Critic loop oscillates or degrades.** | Hard cap 3, monotonic best-cut retention, single-reshoot rule. |
| R5 | **The meta-move backfires** — Sizzle's own cut is bad, torpedoing the 15% presentation score. | Human-cut fallback built in parallel. **Decision point at T-6h, not T-1h.** Committing to the bit is worth points; shipping a bad video is not. |
| R6 | **Audio double-source.** HappyHorse generates native audio; CosyVoice generates VO. They will fight in the mix. | Decide per shot in the beat sheet schema (`vo: null` means native audio wins). Settle this on day one. |

---

## 13. Build order

Ugly-but-complete on day one is the whole game. Every hour after that is quality, and the critic loop means quality compounds without supervision.

| Phase | Deliverable | Gate |
|---|---|---|
| **0** | Alibaba account live; HappyHorse spike; one DashScope call from a deployed worker | Nothing starts until this passes |
| **1** | Beat sheet schema + story engine | Valid 180s beat sheet from a real repo |
| **2** | Assembler + RENDER lane only | **End-to-end ugly video.** Ship-able from here on. |
| **3** | Critic loop | It's an agent now, not a script |
| **4** | Capture agent + verifier | The moat. Timebox hard; fallback is ready. |
| **5** | GENERATE lane / cold open | Last, because it's the most likely to disappoint |
| **6** | Eval sweep across 13 repos; cost charts; README | The technical-depth exhibit |
| **7** | Generate the submission video with Sizzle | T-6h decision point |

## 14. Rubric mapping

| Criterion | Weight | What earns it |
|---|---|---|
| Technical depth & engineering | 30% | Critic loop (multimodal verifier in a closed cycle); capture verification; MCP server + GitHub MCP; budget solver |
| Innovation & AI creativity | 30% | Rubric-conditioned generation; typed directive protocol; three-lane shot orchestration with continuity refs; clean type-driven modularity |
| Problem value & impact | 25% | Universal, recurring pain the judges have felt; 13-repo generality proof; obvious productization for devrel |
| Presentation & documentation | 15% | The video made itself; this PRD; architecture diagram; cost charts |

## 15. Submission checklist

- [ ] Public repo, OSS license **visible in the About section** (detectable — not just a LICENSE file)
- [ ] Alibaba Cloud proof: link to `infra/alicloud/render_worker.py`
- [ ] Architecture diagram in README
- [ ] ~3-min video on YouTube, public — **generated by Sizzle**
- [ ] Text description + track identified (Track 2)
- [ ] Blog post: *"I built an agent to make my hackathon demo video, then used it to make my hackathon demo video"* — separate $500 + $500 prize, 10 winners, and it writes itself

## 16. Open questions

1. What's the actual submission deadline? Phase gates above are relative; they need real dates.
2. HappyHorse vs. Wan 2.7 for the cold open — resolve empirically in the Phase 0 spike, don't argue about it.
3. Does the capture agent get the repo's test suite as a hint for the happy path? Probably yes, and probably cheap.
4. Voice: clone Ammon's (reference audio already exists from Quantext) or use a stock CosyVoice speaker? The clone is a better story; a stock voice is a better product demo. Possibly both — clone for the submission cut, stock as default.