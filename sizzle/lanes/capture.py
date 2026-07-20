"""Capture lane: screenshot the public website, then animate with HappyHorse i2v.

Loads the app URL in headless Playwright, asks Qwen where to scroll or click
to find the content described in the shot goal, takes a screenshot, then feeds
it to HappyHorse image-to-video for cinematic animation. Falls back to a
static zoom-in if i2v fails.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..config import Config
from ..ffmpeg import conform_clip, still_to_clip
from ..qwen import chat_json, vision_json
from ..schema import CaptureSpec, Shot

SCOUT_SYSTEM = """You see a screenshot of a public website. The user wants to capture a specific part of the site.

Given the goal, decide ONE action. Return only JSON:

{{"action": "screenshot" | "scroll" | "click",
 "selector": "css selector (for click only)",
 "dy": 600,
 "reason": "one short sentence"}}

Use "screenshot" when the current view already shows what the goal describes.
For goals about the hero, landing page, headline, value prop, or top of the site: prefer
"screenshot" immediately — do not scroll past the fold.
Use "scroll" only when the goal explicitly names below-the-fold content (features, pricing,
docs, footer).
Use "click" to click a navigation link or tab to reach a different page or section.
Do not navigate away from the site's domain."""

MOTION_SYSTEM = """You are writing a short cinematic motion prompt for an image-to-video AI model.
You will be shown a screenshot of a website. Write a prompt describing subtle, professional motion
to apply to this image — like a product reveal or UI walkthrough.

Return only JSON:
{{"prompt": "one sentence describing the motion"}}

Good motions: slow zoom into the hero headline, gentle parallax on the top of the page, a soft
spotlight sweep highlighting the primary CTA, the cursor gliding to a button already in frame.
Bad motions: scrolling the page, panning to mid-page content not in the screenshot, wild camera
moves, adding people or objects not in the image, changing the content."""

_HERO_GOAL = re.compile(
    r"(?i)\b(hero|landing|home\s*page|top\s+of|headline|value\s*prop|above\s+the\s+fold|"
    r"main\s+(?:cta|call)|brand(?:ing)?|splash)\b"
)


def _animate_screenshot(
    cfg: Config, screenshot: Path, duration_s: float, out_dir: Path, clip: Path,
) -> tuple[Path, str, int]:
    """Try HappyHorse i2v on the screenshot; fall back to still_to_clip."""
    from http import HTTPStatus

    # Ask Qwen to write a motion prompt for this specific screenshot
    motion, tokens = vision_json(
        cfg, cfg.models.capture, MOTION_SYSTEM,
        "Write a cinematic motion prompt for this website screenshot.",
        [str(screenshot)],
    )
    prompt = motion.get("prompt", "slow cinematic zoom into the hero headline and primary CTA")

    # Call HappyHorse i2v
    try:
        from dashscope import VideoSynthesis

        cfg.apply_endpoints()
        task = VideoSynthesis.async_call(
            model=cfg.models.i2v,
            prompt=prompt,
            img_url=f"file://{screenshot}",
            parameters={
                "resolution": "720P",
                "ratio": "16:9",
                "duration": max(3, min(int(duration_s), 15)),
                "prompt_extend": True,
            },
        )
        rsp = VideoSynthesis.wait(task)
        if rsp.status_code != HTTPStatus.OK:
            raise RuntimeError(f"i2v: {rsp.code} {rsp.message}")

        # Download and conform
        import httpx

        raw = out_dir / "i2v_raw.mp4"
        with httpx.stream("GET", rsp.output.video_url, timeout=120, follow_redirects=True) as r:
            r.raise_for_status()
            with open(raw, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        conform_clip(raw, clip, duration_s, cfg.resolution, cfg.fps)
        return clip, cfg.models.i2v, tokens
    except Exception:
        # Fall back to static zoom
        still_to_clip(screenshot, clip, duration_s, cfg.resolution, cfg.fps)
        return clip, "still_fallback", tokens


def capture_shot(
    cfg: Config,
    shot: Shot,
    app_url: str,
    out_dir: Path,
) -> tuple[Path | None, int, int, str]:
    """Screenshot the public site, animate with i2v, return the clip.

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
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(400)

            screenshot = out_dir / "capture_final.png"
            max_scout_steps = min(spec.max_steps, 3)
            hero_goal = bool(_HERO_GOAL.search(spec.goal or "")) or not (spec.goal or "").strip()

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
                if hero_goal and action in ("scroll", "click"):
                    # Establishing / hero shots must stay at the top of the page.
                    action = "screenshot"

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
                            pass
                else:
                    screenshot = step_img
                    break
            else:
                final_img = out_dir / "scout_final.png"
                if hero_goal:
                    page.evaluate("window.scrollTo(0, 0)")
                    page.wait_for_timeout(300)
                page.screenshot(path=str(final_img))
                screenshot = final_img

            # Final safety: hero/establishing shots always capture from the top.
            if hero_goal:
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(300)
                top_img = out_dir / "capture_top.png"
                page.screenshot(path=str(top_img))
                screenshot = top_img

            context.close()
            browser.close()

        # Animate the screenshot with HappyHorse i2v
        clip = out_dir / f"{shot.id}.mp4"
        clip, model_used, anim_tokens = _animate_screenshot(
            cfg, screenshot, shot.duration_s, out_dir, clip,
        )
        tokens_total += anim_tokens
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
