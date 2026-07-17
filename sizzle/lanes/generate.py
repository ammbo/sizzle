"""GENERATE lane (PRD §7.1): dramatized clips for beats with no product to film.
HappyHorse 1.1 t2v/i2v, with r2v for continuity; Wan 2.7 as fallback."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

import httpx

from ..config import Config
from ..ffmpeg import conform_clip, still_to_clip
from ..schema import GenerateSpec, Shot


class GenerateError(RuntimeError):
    pass


def _download(url: str, dest: Path) -> Path:
    with httpx.stream("GET", url, timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    return dest


def _call_video_model(cfg: Config, model: str, spec: GenerateSpec, duration_s: float) -> str:
    """Submit an async t2v/i2v task and block until the video URL is ready."""
    from dashscope import VideoSynthesis

    cfg.apply_endpoints()
    kwargs: dict = {
        "model": model,
        "prompt": spec.prompt,
        "parameters": {
            "resolution": "720P",
            "ratio": "16:9",
            "duration": max(3, min(int(duration_s), 15)),  # provider clip range: 3-15s
            "prompt_extend": True,
        },
    }
    if spec.ref_image:
        kwargs["img_url"] = spec.ref_image
    task = VideoSynthesis.async_call(**kwargs)
    rsp = VideoSynthesis.wait(task)
    if rsp.status_code != HTTPStatus.OK:
        raise GenerateError(f"{model}: {rsp.code} {rsp.message}")
    return rsp.output.video_url


def generate_shot(cfg: Config, shot: Shot, out_dir: Path,
                  continuity_frame: Path | None = None) -> tuple[Path, str]:
    """Produce a conformed clip for a GENERATE shot. Returns (clip_path, model_used).

    Continuity: if a previous shot's last frame is supplied, use the i2v model seeded
    from that frame so consecutive dramatized shots hold visual continuity.
    """
    assert isinstance(shot.spec, GenerateSpec)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / f"{shot.id}_raw.mp4"
    clip = out_dir / f"{shot.id}.mp4"

    if cfg.dry_run:
        _stub_clip(cfg, shot, out_dir, raw)
        conform_clip(raw, clip, shot.duration_s, cfg.resolution, cfg.fps)
        return clip, "stub"

    spec = shot.spec
    if continuity_frame is not None and not spec.ref_image:
        spec = spec.model_copy(update={"ref_image": str(continuity_frame)})

    model = cfg.models.i2v if spec.ref_image else cfg.models.t2v
    try:
        url = _call_video_model(cfg, model, spec, shot.duration_s)
    except GenerateError:
        model = cfg.models.t2v_fallback
        url = _call_video_model(cfg, model, spec.model_copy(update={"ref_image": None}), shot.duration_s)

    _download(url, raw)
    conform_clip(raw, clip, shot.duration_s, cfg.resolution, cfg.fps)
    return clip, model


def _stub_clip(cfg: Config, shot: Shot, out_dir: Path, dest: Path) -> None:
    """Dry-run placeholder: a labeled card so the assembly is visually traceable."""
    from PIL import Image, ImageDraw

    from .render import _font  # reuse font discovery

    img = Image.new("RGB", cfg.resolution, (30, 18, 40))
    draw = ImageDraw.Draw(img)
    draw.text((60, 60), f"[GENERATE stub] {shot.id}", font=_font(40), fill=(255, 200, 120))
    prompt = shot.spec.prompt if isinstance(shot.spec, GenerateSpec) else ""
    draw.text((60, 140), prompt[:90], font=_font(24), fill=(220, 220, 230))
    still = out_dir / f"{shot.id}_stub.png"
    img.save(still)
    still_to_clip(still, dest, shot.duration_s, cfg.resolution, cfg.fps, zoom=False)
