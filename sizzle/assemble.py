"""Assembler (PRD §7.1): conform shots to the duration budget, mix audio, burn captions.
Deterministic ffmpeg; zero tokens.

Captions are burned by overlaying PIL-rendered transparent PNGs (the `overlay` filter is
core ffmpeg; `subtitles`/`drawtext` are optional build flags we can't rely on). An SRT is
also written next to the cut for upload-time closed captions."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from .config import Config
from .ffmpeg import probe_duration, run
from .lanes.render import _font, _wrap
from .schema import BeatSheet, Shot


def _srt_timestamp(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _write_srt(ordered: list[tuple[Shot, Path]], out: Path) -> Path:
    entries, t, idx = [], 0.0, 1
    for shot, _ in ordered:
        if shot.vo:
            entries.append(f"{idx}\n{_srt_timestamp(t)} --> {_srt_timestamp(t + shot.duration_s - 0.2)}\n{shot.vo}\n")
            idx += 1
        t += shot.duration_s
    out.write_text("\n".join(entries))
    return out


def _caption_overlay(cfg: Config, text: str, out: Path) -> Path:
    """Transparent full-frame PNG with the caption in the lower third."""
    w, h = cfg.resolution
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    f = _font(26)
    lines = _wrap(draw, text, f, int(w * 0.8))[:2]
    line_h = 36
    y = h - 40 - len(lines) * line_h
    for line in lines:
        tw = draw.textlength(line, font=f)
        x = (w - tw) / 2
        draw.rectangle([x - 12, y - 4, x + tw + 12, y + line_h - 4], fill=(0, 0, 0, 150))
        draw.text((x, y), line, font=f, fill=(255, 255, 255, 255))
        y += line_h
    img.save(out)
    return out


def _shot_audio(cfg: Config, shot: Shot, vo_path: Path | None, out: Path) -> Path:
    """A fixed-length audio segment per shot: VO padded/trimmed to the slot, or silence."""
    d = shot.duration_s
    if vo_path is None:
        run(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", f"{d:.3f}",
             "-c:a", "aac", str(out)])
        return out
    vo_len = probe_duration(vo_path)
    if vo_len > d:
        # speed up slightly rather than clipping the line mid-word (cap at 1.25x)
        tempo = min(vo_len / d, 1.25)
        af = f"atempo={tempo:.3f},apad,aresample=44100"
    else:
        af = "apad,aresample=44100"
    run(["-i", str(vo_path), "-af", af, "-t", f"{d:.3f}", "-ac", "2", "-c:a", "aac", str(out)])
    return out


def assemble(
    cfg: Config,
    sheet: BeatSheet,
    clips: dict[str, Path],          # shot_id -> conformed video clip
    vo_tracks: dict[str, Path | None],  # shot_id -> vo audio or None
    beat_order: list[str],
    out_dir: Path,
    label: str = "cut",
) -> Path:
    """Concat shots in beat order, lay VO under each shot, burn captions. Returns the cut."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / f"{label}_parts"
    tmp.mkdir(parents=True, exist_ok=True)

    ordered: list[tuple[Shot, Path]] = []
    for beat_id in beat_order:
        for shot in sheet.shots_for_beat(beat_id):
            if shot.id in clips:
                ordered.append((shot, clips[shot.id]))
    if not ordered:
        raise RuntimeError("nothing to assemble: no clips survived")

    # per-shot mux (video + slot-sized audio + burned caption), then a stream-copy concat
    segments: list[Path] = []
    for shot, clip in ordered:
        audio = _shot_audio(cfg, shot, vo_tracks.get(shot.id), tmp / f"{shot.id}_audio.m4a")
        seg = tmp / f"{shot.id}_seg.mp4"
        args = ["-i", str(clip), "-i", str(audio)]
        if shot.vo:
            overlay = _caption_overlay(cfg, shot.vo, tmp / f"{shot.id}_caption.png")
            args += ["-i", str(overlay),
                     "-filter_complex", "[0:v][2:v]overlay=0:0:format=auto[v]",
                     "-map", "[v]", "-map", "1:a"]
        else:
            args += ["-map", "0:v", "-map", "1:a"]
        args += ["-c:v", "libx264", "-preset", "fast", "-crf", "20",
                 "-c:a", "aac", "-shortest", str(seg)]
        run(args)
        segments.append(seg)

    concat_list = tmp / "concat.txt"
    concat_list.write_text("\n".join(f"file '{p.resolve()}'" for p in segments))
    final = out_dir / f"{label}.mp4"
    run(["-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(final)])

    _write_srt(ordered, out_dir / f"{label}.srt")
    return final
