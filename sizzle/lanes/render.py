"""RENDER lane (PRD §7.1): deterministic title cards, code snippets, architecture stills,
and metric plates. PIL draws a still; ffmpeg turns it into a clip with a slow push-in."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..config import Config
from ..ffmpeg import still_to_clip
from ..schema import RenderSpec, Shot

BG = (14, 15, 20)
FG = (238, 238, 240)
ACCENT = (255, 94, 58)
MUTED = (140, 144, 156)
CODE_BG = (22, 24, 32)

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_MONO_CANDIDATES = [
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]


def _font(size: int, mono: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _MONO_CANDIDATES if mono else _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        words, cur = para.split(), ""
        for w in words:
            trial = f"{cur} {w}".strip()
            if draw.textlength(trial, font=font) <= max_width:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def _canvas(size: tuple[int, int]) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", size, BG)
    return img, ImageDraw.Draw(img)


def _title_card(size: tuple[int, int], payload: dict) -> Image.Image:
    img, draw = _canvas(size)
    w, h = size
    title = payload.get("title") or ""
    subtitle = payload.get("subtitle") or ""
    draw.rectangle([w // 2 - 30, h // 2 - 110, w // 2 + 30, h // 2 - 102], fill=ACCENT)
    if title:
        f = _font(72)
        tw = draw.textlength(title, font=f)
        draw.text(((w - tw) / 2, h / 2 - 80), title, font=f, fill=FG)
    if subtitle:
        f = _font(30)
        for i, line in enumerate(_wrap(draw, subtitle, f, int(w * 0.7))):
            tw = draw.textlength(line, font=f)
            draw.text(((w - tw) / 2, h / 2 + 30 + i * 42), line, font=f, fill=MUTED)
    return img


def _code_snippet(size: tuple[int, int], payload: dict) -> Image.Image:
    img, draw = _canvas(size)
    w, h = size
    code = payload.get("code") or ""
    caption = payload.get("caption") or payload.get("language") or ""
    margin = 80
    draw.rounded_rectangle([margin, margin, w - margin, h - margin - 60], radius=16, fill=CODE_BG)
    for i, color in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        draw.ellipse([margin + 24 + i * 28, margin + 22, margin + 40 + i * 28, margin + 38], fill=color)
    f = _font(22, mono=True)
    y = margin + 60
    for line in code.split("\n")[:18]:
        draw.text((margin + 30, y), line[:110], font=f, fill=FG)
        y += 32
    if caption:
        fc = _font(24)
        tw = draw.textlength(caption, font=fc)
        draw.text(((w - tw) / 2, h - margin - 40), caption, font=fc, fill=MUTED)
    return img


def _architecture(size: tuple[int, int], payload: dict) -> Image.Image:
    """Boxes-and-arrows from a list of 'a -> b -> c' lines."""
    img, draw = _canvas(size)
    w, h = size
    lines = payload.get("lines") or payload.get("nodes") or []
    if isinstance(lines, str):
        lines = [lines]
    f = _font(26, mono=True)
    fh = _font(34)
    draw.text((80, 60), "architecture", font=fh, fill=ACCENT)
    y = 150
    row_h = min(90, (h - 220) // max(len(lines), 1))
    for line in lines[:8]:
        parts = [p.strip() for p in str(line).split("->")]
        x = 100
        for j, part in enumerate(parts):
            tw = draw.textlength(part, font=f)
            box_w = tw + 40
            draw.rounded_rectangle([x, y, x + box_w, y + 56], radius=10, outline=MUTED, width=2)
            draw.text((x + 20, y + 14), part, font=f, fill=FG)
            x += box_w
            if j < len(parts) - 1:
                draw.line([x + 8, y + 28, x + 40, y + 28], fill=ACCENT, width=3)
                draw.polygon([(x + 40, y + 22), (x + 50, y + 28), (x + 40, y + 34)], fill=ACCENT)
                x += 58
        y += row_h
    return img


def _metric_plate(size: tuple[int, int], payload: dict) -> Image.Image:
    img, draw = _canvas(size)
    w, h = size
    metrics = payload.get("metrics") or []
    title = payload.get("title") or ""
    if title:
        fh = _font(34)
        draw.text((80, 60), title, font=fh, fill=ACCENT)
    n = max(len(metrics), 1)
    cell_w = (w - 160) // min(n, 3)
    for i, m in enumerate(metrics[:6]):
        col, row = i % 3, i // 3
        x = 80 + col * cell_w
        y = 180 + row * 220
        fv = _font(64)
        fl = _font(24)
        draw.text((x, y), str(m.get("value", "")), font=fv, fill=FG)
        draw.text((x, y + 90), str(m.get("label", "")), font=fl, fill=MUTED)
    return img


_TEMPLATES = {
    "title_card": _title_card,
    "code_snippet": _code_snippet,
    "architecture": _architecture,
    "metric_plate": _metric_plate,
}


def render_shot(cfg: Config, shot: Shot, out_dir: Path) -> Path:
    """Render a RENDER-lane shot to a conformed clip. Deterministic; zero tokens."""
    assert isinstance(shot.spec, RenderSpec)
    out_dir.mkdir(parents=True, exist_ok=True)
    img = _TEMPLATES[shot.spec.template](cfg.resolution, shot.spec.payload)
    still = out_dir / f"{shot.id}.png"
    img.save(still)
    clip = out_dir / f"{shot.id}.mp4"
    return still_to_clip(still, clip, shot.duration_s, cfg.resolution, cfg.fps)
