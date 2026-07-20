"""Budget allocator (PRD §9.1): assign each shot to GENERATE / CAPTURE / RENDER under a token budget.

Default every shot to the cheapest type that can carry it; promote to GENERATE only when the
beat is unfilmable and not just static information. Then run a greedy knapsack over the
GENERATE candidates, demoting losers to a stylized RENDER fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import Config
from .schema import BeatSheet, CaptureSpec, CostEstimate, GenerateSpec, RenderSpec, Shot, ShotType


@dataclass
class AllocationReport:
    budget_tokens: int
    spent_tokens: int = 0
    assignments: dict[str, str] = field(default_factory=dict)
    demoted: list[str] = field(default_factory=list)


def _generate_cost(cfg: Config, shot: Shot) -> int:
    return int(shot.duration_s * cfg.tokens_per_generate_second)


def _demote_to_render(shot: Shot) -> None:
    """Stylized motion-graphics fallback for a GENERATE shot that lost the knapsack."""
    prompt = shot.spec.prompt if isinstance(shot.spec, GenerateSpec) else ""
    if re.search(r"(?i)RESHOOT|directive|acceptance\s+predicate|critic\s+vlm|error|traceback", str(prompt)):
        prompt = ""
    shot.type = ShotType.RENDER
    shot.spec = RenderSpec(
        template="title_card",
        payload={"title": (prompt[:80] or " "), "subtitle": "", "style": "kinetic"},
    )
    shot.cost_estimate = CostEstimate(value=0)


def allocate(cfg: Config, sheet: BeatSheet, live_app_available: bool) -> AllocationReport:
    report = AllocationReport(budget_tokens=cfg.generate_budget_tokens)

    generate_candidates: list[Shot] = []
    for shot in sheet.shots:
        beat = sheet.beat(shot.beat_id)
        if isinstance(shot.spec, CaptureSpec):
            if beat.filmable and live_app_available:
                shot.type = ShotType.CAPTURE
                shot.cost_estimate = CostEstimate(value=0)
            else:
                # no app to film: this beat's information has to be rendered
                _demote_to_render(shot)
                report.demoted.append(shot.id)
        elif isinstance(shot.spec, RenderSpec) or beat.is_static_information:
            if not isinstance(shot.spec, RenderSpec):
                _demote_to_render(shot)
            shot.type = ShotType.RENDER
            shot.cost_estimate = CostEstimate(value=0)
        else:
            shot.type = ShotType.GENERATE
            shot.cost_estimate = CostEstimate(value=_generate_cost(cfg, shot))
            generate_candidates.append(shot)

    # Greedy knapsack: maximize Σ narrative_weight s.t. Σ cost ≤ B.
    # Sort by weight density (weight per token) so cheap high-weight shots win first.
    def density(s: Shot) -> float:
        w = sheet.beat(s.beat_id).narrative_weight
        return w / max(s.cost_estimate.value, 1)

    remaining = cfg.generate_budget_tokens
    for shot in sorted(generate_candidates, key=density, reverse=True):
        cost = shot.cost_estimate.value
        if cost <= remaining:
            remaining -= cost
            report.spent_tokens += cost
        else:
            _demote_to_render(shot)
            report.demoted.append(shot.id)

    report.assignments = {s.id: s.type.value for s in sheet.shots}
    return report
