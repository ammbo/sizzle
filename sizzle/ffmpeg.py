"""Shared ffmpeg helpers. All video plumbing goes through here so the assembler stays readable."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


class FfmpegError(RuntimeError):
    pass


def run(args: list[str]) -> None:
    proc = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise FfmpegError(proc.stderr[-2000:])


def probe_duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(proc.stdout)["format"]["duration"])


def still_to_clip(image: Path, out: Path, duration_s: float, size: tuple[int, int], fps: int,
                  zoom: bool = True) -> Path:
    """Turn a still into a video clip with a slow push-in so RENDER shots don't feel dead."""
    w, h = size
    frames = max(int(duration_s * fps), 1)
    if zoom:
        # zoompan needs an upscaled source to avoid jitter
        vf = (
            f"scale={w * 4}:{h * 4},"
            f"zoompan=z='min(zoom+0.0006,1.12)':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps},"
            f"format=yuv420p"
        )
    else:
        vf = f"scale={w}:{h},format=yuv420p"
    run(["-loop", "1", "-i", str(image), "-t", f"{duration_s:.3f}", "-vf", vf,
         "-r", str(fps), "-an", str(out)])
    return out


def conform_clip(src: Path, out: Path, duration_s: float, size: tuple[int, int], fps: int) -> Path:
    """Normalize any clip to the house format.  Trim to *duration_s* if the
    source is longer, but never pad / stretch a shorter clip — dead air is
    worse than a slightly shorter slot.  The actual duration is the minimum
    of the source length and *duration_s*."""
    w, h = size
    actual = probe_duration(src)
    vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={fps},format=yuv420p"
    trim = min(actual, duration_s)
    run(["-i", str(src), "-t", f"{trim:.3f}", "-vf", vf, "-an", str(out)])
    return out


def extract_frames(video: Path, out_dir: Path, n: int) -> list[Path]:
    """Sample n frames evenly across the clip for the verifier / critic."""
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = probe_duration(video)
    paths = []
    for i in range(n):
        t = duration * (i + 0.5) / n
        p = out_dir / f"frame_{i:03d}.png"
        run(["-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", str(p)])
        paths.append(p)
    return paths


def last_frame(video: Path, out: Path) -> Path:
    run(["-sseof", "-0.1", "-i", str(video), "-frames:v", "1", "-update", "1", str(out)])
    return out
