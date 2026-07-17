"""Capture agent (PRD §7.1, §9.3): drive the live app with Playwright, let qwen3.7-plus
pick the next action from a screenshot, record video, then visually verify the money shot
landed. A visual acceptance test on autonomously-produced footage is a closed
perception-action loop.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..ffmpeg import conform_clip, extract_frames, still_to_clip
from ..qwen import vision_json
from ..schema import CaptureSpec, Shot, VerifierResult

AGENT_SYSTEM = """You operate a web app through a browser to film a demo shot.
You see a screenshot and a goal. Choose ONE next action. Return only JSON:

{"action": "click" | "type" | "press" | "scroll" | "wait" | "done",
 "selector": "css selector (for click/type)",
 "text": "text to type (for type)",
 "key": "key name (for press, e.g. Enter)",
 "dy": 400,
 "reason": "one short sentence"}

Rules: prefer visible, specific selectors (button text, aria labels, placeholders).
Use "wait" when the page is loading. Use "done" only when the goal is visibly achieved
on screen. Do not navigate away from the app."""

VERIFIER_SYSTEM = """You are a strict visual verifier for demo footage. You are shown
sampled frames from a screen recording plus an acceptance predicate. Answer the closed
question: does at least one frame satisfy the predicate? Return only JSON:

{"satisfied": true/false,
 "failure_mode": null | "SPINNER" | "ERROR_STATE" | "WRONG_SCREEN" | "OCCLUDED" | "TIMEOUT",
 "evidence_frame": <index of the deciding frame>,
 "suggested_fix": null | "one short actionable sentence"}"""


def _drive(cfg: Config, spec: CaptureSpec, video_dir: Path, hint: str | None) -> tuple[Path, int]:
    """One recorded attempt at the goal. Returns (raw video path, tokens spent)."""
    from playwright.sync_api import sync_playwright

    tokens_total = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": spec.viewport[0], "height": spec.viewport[1]},
            record_video_dir=str(video_dir),
            record_video_size={"width": spec.viewport[0], "height": spec.viewport[1]},
        )
        page = context.new_page()
        page.goto(spec.start_url, wait_until="networkidle")

        goal = spec.goal if not hint else f"{spec.goal}\n\nPrevious attempt failed; fix: {hint}"
        for step in range(spec.max_steps):
            shot_path = video_dir / f"step_{step:02d}.png"
            page.screenshot(path=str(shot_path))
            decision, tokens = vision_json(
                cfg, cfg.models.capture, AGENT_SYSTEM,
                f"Goal: {goal}\nStep {step + 1} of {spec.max_steps}. What is the next action?",
                [str(shot_path)],
            )
            tokens_total += tokens
            action = decision.get("action", "wait")
            try:
                if action == "done":
                    page.wait_for_timeout(1500)  # linger on the money shot
                    break
                elif action == "click":
                    page.click(decision["selector"], timeout=5000)
                elif action == "type":
                    page.fill(decision["selector"], decision.get("text", ""), timeout=5000)
                elif action == "press":
                    page.keyboard.press(decision.get("key", "Enter"))
                elif action == "scroll":
                    page.mouse.wheel(0, int(decision.get("dy", 400)))
                else:
                    page.wait_for_timeout(1500)
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                continue  # agent sees the unchanged screen next step and adapts

        video = page.video
        context.close()
        browser.close()
        raw = Path(video.path())
    return raw, tokens_total


def verify(cfg: Config, clip: Path, acceptance: str, work_dir: Path) -> tuple[VerifierResult, int]:
    """Sample frames and ask the VLM the closed acceptance question (PRD §9.3)."""
    if cfg.dry_run:
        return VerifierResult(satisfied=True, evidence_frame=0), 0
    frames = extract_frames(clip, work_dir / "verify_frames", n=6)
    raw, tokens = vision_json(
        cfg, cfg.models.critic, VERIFIER_SYSTEM,
        f"Acceptance predicate: {acceptance}\nFrames are in order, indexed from 0.",
        [str(f) for f in frames],
    )
    return VerifierResult.model_validate(raw), tokens


def capture_shot(cfg: Config, shot: Shot, app_url: str, out_dir: Path) -> tuple[Path | None, int, int, str]:
    """Attempt the capture up to capture_max_attempts times, verifying each take.

    Returns (clip or None on total failure, attempts, tokens, verifier_outcome).
    On failure the caller demotes the shot to RENDER (PRD §9.3).
    """
    assert isinstance(shot.spec, CaptureSpec)
    out_dir.mkdir(parents=True, exist_ok=True)
    spec = shot.spec if shot.spec.start_url else shot.spec.model_copy(update={"start_url": app_url})

    if cfg.dry_run:
        clip = out_dir / f"{shot.id}.mp4"
        _stub_capture(cfg, shot, out_dir, clip)
        return clip, 1, 0, "pass"

    tokens_total = 0
    hint: str | None = None
    for attempt in range(1, cfg.capture_max_attempts + 1):
        attempt_dir = out_dir / f"attempt_{attempt}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        try:
            raw, tokens = _drive(cfg, spec, attempt_dir, hint)
        except Exception as e:
            hint = f"browser session failed: {e}"
            continue
        tokens_total += tokens
        clip = out_dir / f"{shot.id}.mp4"
        conform_clip(raw, clip, shot.duration_s, cfg.resolution, cfg.fps)
        result, vtokens = verify(cfg, clip, shot.acceptance, attempt_dir)
        tokens_total += vtokens
        if result.satisfied:
            return clip, attempt, tokens_total, "pass"
        hint = result.suggested_fix or f"failure mode: {result.failure_mode}"
    return None, cfg.capture_max_attempts, tokens_total, hint or "TIMEOUT"


def _stub_capture(cfg: Config, shot: Shot, out_dir: Path, dest: Path) -> None:
    from PIL import Image, ImageDraw

    from .render import _font

    img = Image.new("RGB", cfg.resolution, (18, 34, 28))
    draw = ImageDraw.Draw(img)
    draw.text((60, 60), f"[CAPTURE stub] {shot.id}", font=_font(40), fill=(140, 255, 180))
    goal = shot.spec.goal if isinstance(shot.spec, CaptureSpec) else ""
    draw.text((60, 140), goal[:90], font=_font(24), fill=(220, 230, 220))
    still = out_dir / f"{shot.id}_stub.png"
    img.save(still)
    still_to_clip(still, dest, shot.duration_s, cfg.resolution, cfg.fps, zoom=False)
