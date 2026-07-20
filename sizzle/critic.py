"""Critic (PRD §9.2): watch the rendered cut, score it against the rubric, emit typed
revision directives. The scorecard is the hackathon's four judging criteria verbatim,
plus hard constraints."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from .config import Config
from .ffmpeg import extract_frames, probe_duration
from .qwen import vision_json
from .schema import BeatSheet, CriticVerdict, Reshoot, Retime

RUBRIC = {
    "technical_depth": "Technical depth & engineering quality (30%)",
    "innovation": "Innovation & AI creativity (30%)",
    "problem_value": "Problem value & impact (25%)",
    "presentation": "Presentation & documentation quality (15%)",
}

SYSTEM = """You are a ruthless demo-video critic. You see frames sampled evenly from a cut,
in order, plus the VO transcript and the beat sheet it was built from. Score the cut and
emit typed revision directives — not prose notes.

Scorecard (score each 0.0-1.0):
- technical_depth: {technical_depth}
- innovation: {innovation}
- problem_value: {problem_value}
- presentation: {presentation}

Hard constraints (violations demand directives):
- total duration between {min_duration}s and {max_duration}s
- the money shot (product visibly working) lands before 0:{money_deadline}
- no dead air longer than {dead_air}s
- prefer a tight, natural-length video over padding to fill time

Return ONLY JSON:
{{
  "score": <weighted overall 0.0-1.0>,
  "rubric_scores": {{"technical_depth": 0.0, "innovation": 0.0, "problem_value": 0.0, "presentation": 0.0}},
  "notes": "<=3 sentences of what is weakest>",
  "directives": [
    {{"op": "RETIME", "beat_id": "...", "delta_s": -3.0}},
    {{"op": "RESHOOT", "shot_id": "...", "revised_spec": {{...partial spec updates...}}}},
    {{"op": "CUT", "shot_id": "..."}},
    {{"op": "REORDER", "beat_ids": ["..."]}},
    {{"op": "REWRITE_VO", "beat_id": "...", "text": "..."}}
  ]
}}

Emit at most 4 directives. Emit none if the cut is genuinely done."""


def review(cfg: Config, cut: Path, sheet: BeatSheet, work_dir: Path,
           iteration: int, reshoot_counts: dict[str, int]) -> tuple[CriticVerdict, int]:
    """One critic pass over a rendered cut. Applies the PRD's guards to the raw verdict."""
    if cfg.dry_run:
        return _stub_verdict(sheet, iteration), 0

    frames = extract_frames(cut, work_dir / f"critic_frames_{iteration}", n=12)
    transcript = "\n".join(f"[{s.beat_id}/{s.id}] {s.vo}" for s in sheet.shots if s.vo)
    beats = "\n".join(f"{b.id} {b.name} ({b.duration_s}s): {b.intent}" for b in sheet.beats)

    system = SYSTEM.format(
        min_duration=cfg.min_duration_s,
        max_duration=cfg.max_duration_s,
        money_deadline=cfg.money_shot_deadline_s,
        dead_air=cfg.max_dead_air_s,
        **RUBRIC,
    )
    user = f"Beat sheet:\n{beats}\n\nVO transcript:\n{transcript}\n\nFrames follow in playback order."
    raw, tokens = vision_json(cfg, cfg.models.critic, system, user, [str(f) for f in frames])
    try:
        verdict = CriticVerdict.model_validate(raw)
    except ValidationError:
        verdict = CriticVerdict(score=float(raw.get("score", 0.5)), notes=str(raw.get("notes", "")))
    return _apply_guards(cfg, verdict, sheet, reshoot_counts), tokens


def _apply_guards(cfg: Config, verdict: CriticVerdict, sheet: BeatSheet,
                  reshoot_counts: dict[str, int]) -> CriticVerdict:
    """Duration invariant + no-oscillation (PRD §9.2). Monotonic retention lives in the loop."""
    kept = []
    projected = sheet.total_duration()
    for d in verdict.directives:
        if isinstance(d, Retime):
            projected += d.delta_s
        if isinstance(d, Reshoot) and reshoot_counts.get(d.shot_id, 0) >= 1:
            continue  # a shot may be RESHOOT once; second failure demotes its type upstream
        kept.append(d)
    if projected > cfg.max_duration_s:
        kept = [d for d in kept if not (isinstance(d, Retime) and d.delta_s > 0)]
    verdict.directives = kept
    return verdict


def hard_constraints_ok(cfg: Config, cut: Path) -> bool:
    dur = probe_duration(cut)
    return cfg.min_duration_s <= dur <= cfg.max_duration_s


def _stub_verdict(sheet: BeatSheet, iteration: int) -> CriticVerdict:
    """Dry-run: iteration 0 finds pacing flat and retimes; iteration 1 passes.
    Exercises the loop, the directive application path, and monotonic retention."""
    if iteration == 0:
        first = sheet.beats[0]
        return CriticVerdict(
            score=0.62,
            rubric_scores={k: 0.6 for k in RUBRIC},
            notes="Pacing flat 0:20-0:50. Value prop unclear. Recut.",
            directives=[Retime(beat_id=first.id, delta_s=-2.0)],
        )
    return CriticVerdict(score=0.81, rubric_scores={k: 0.8 for k in RUBRIC}, notes="Ship it.")
