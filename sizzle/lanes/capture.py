"""Capture lane: screenshot the public website and convert to video clips.

Loads the app URL in headless Playwright, optionally asks Qwen where to
scroll or click to find the content described in the shot goal, then
converts the screenshot to a clip with a slow zoom-in effect.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..ffmpeg import still_to_clip
from ..qwen import vision_json
from ..schema import CaptureSpec, Shot

SCOUT_SYSTEM = """You see a screenshot of a public website. The user wants to capture a specific part of the site.

Given the goal, decide ONE action. Return only JSON:

{{"action": "screenshot" | "scroll" | "click",
 "selector": "css selector (for click only)",
 "dy": 600,
 "reason": "one short sentence"}}

Use "screenshot" when the current view already shows what the goal describes.
Use "scroll" to scroll down and reveal content below the fold.
Use "click" to click a navigation link or tab to reach a different page or section.
Do not navigate away from the site's domain."""


def capture_shot(
    cfg: Config,
    shot: Shot,
    app_url: str,
    out_dir: Path,
) -> tuple[Path | None, int, int, str]:
    """Screenshot the public site, guided by the shot goal.

    Returns (clip or None, attempts, tokens, outcome).
    """
    assert isinstance(shot.spec, CaptureSpec)
    out_dir.mkdir(parents=True, exist_ok=True)
    spec = shot.spec if shot.spec.start_url else shot.spec.model_copy(update={"start_url": app_url})

    if cfg.dry_run:
        clip = out_dir / f"{shot.id}.mp4"
        _stub_capture(cfg, shot, out_dir, clip)
        return clip, 1, 0, "pass"

    from playwright.sync_api import sync_playwright

    tokens_total = 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": spec.viewport[0], "height": spec.viewport[1]},
            )
            page = context.new_page()
            page.goto(spec.start_url, wait_until="networkidle", timeout=30000)

            screenshot = out_dir / "capture_final.png"
            max_scout_steps = min(spec.max_steps, 3)

            for step in range(max_scout_steps):
                step_img = out_dir / f"scout_{step:02d}.png"
                page.screenshot(path=str(step_img))

                decision, tokens = vision_json(
                    cfg, cfg.models.capture, SCOUT_SYSTEM,
                    f"Goal: {spec.goal}",
                    [str(step_img)],
                )
                tokens_total += tokens
                action = decision.get("action", "screenshot")

                if action == "screenshot":
                    screenshot = step_img
                    break
                elif action == "scroll":
                    dy = int(decision.get("dy", 600))
                    page.mouse.wheel(0, dy)
                    page.wait_for_timeout(800)
                elif action == "click":
                    selector = decision.get("selector", "")
                    if selector:
                        try:
                            page.click(selector, timeout=5000)
                            page.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            pass  # couldn't click; take what we have
                else:
                    screenshot = step_img
                    break
            else:
                # Took all scout steps without a "screenshot" action — use last screenshot
                final_img = out_dir / "scout_final.png"
                page.screenshot(path=str(final_img))
                screenshot = final_img

            context.close()
            browser.close()

        clip = out_dir / f"{shot.id}.mp4"
        still_to_clip(screenshot, clip, shot.duration_s, cfg.resolution, cfg.fps)
        return clip, 1, tokens_total, "pass"

    except Exception as e:
        return None, 1, tokens_total, f"capture_failed: {e}"


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
