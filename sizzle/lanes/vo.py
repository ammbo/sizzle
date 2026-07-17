"""VO lane (PRD §7.1): narration from the beat sheet's VO lines. CosyVoice v3-plus.

PRD R6: shots with native generated audio have vo=None and are skipped here; the
assembler ducks nothing because the two sources never coexist on one shot.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..ffmpeg import run
from ..schema import Shot


def synthesize_vo(cfg: Config, shot: Shot, out_dir: Path) -> Path | None:
    """Synthesize one shot's VO line to an mp3. Returns None when the shot has no VO."""
    if not shot.vo:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{shot.id}_vo.mp3"

    if cfg.dry_run:
        # silent bed sized to spoken-word pace so the timeline math is exercised
        est_s = max(len(shot.vo.split()) / 2.5, 1.0)
        run(["-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", f"{est_s:.2f}",
             "-b:a", "64k", str(out)])
        return out

    from dashscope.audio.tts_v2 import SpeechSynthesizer

    cfg.apply_endpoints()
    synthesizer = SpeechSynthesizer(model=cfg.models.tts, voice=cfg.models.tts_voice)
    audio = synthesizer.call(shot.vo)
    if not audio:
        raise RuntimeError(f"CosyVoice returned no audio for shot {shot.id}")
    out.write_bytes(audio)
    return out
