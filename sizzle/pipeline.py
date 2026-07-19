"""Orchestrator: repo URL + app URL in, three-minute cut + shot manifest + cost report out.

produce -> assemble -> critique -> revise, keeping the best cut ever produced (monotonic)."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from rich.console import Console

from .allocator import allocate
from .assemble import assemble
from .config import Config
from .critic import hard_constraints_ok, review
from .ffmpeg import conform_clip, last_frame, probe_duration
from .ingest import ingest
from .lanes.capture import capture_shot
from .lanes.generate import generate_shot
from .lanes.render import render_shot
from .lanes.vo import synthesize_vo
from .schema import (
    BeatSheet,
    CostReport,
    Cut,
    GenerateSpec,
    RenderSpec,
    Reorder,
    Reshoot,
    Retime,
    RewriteVO,
    RunManifest,
    Shot,
    ShotRecord,
    ShotType,
)

console = Console()


class Run:
    def __init__(self, cfg: Config, repo_url: str, app_url: str | None):
        self.cfg = cfg
        self.repo_url = repo_url
        self.app_url = app_url
        self.run_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        self.dir = cfg.work_dir / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifest = RunManifest(run_id=self.run_id, repo_url=repo_url, app_url=app_url)
        self.clips: dict[str, Path] = {}
        self.vo_tracks: dict[str, Path | None] = {}
        self.records: dict[str, ShotRecord] = {}
        self.reshoot_counts: dict[str, int] = {}
        self.demoted: list[str] = []
        self.planning_tokens = 0
        self.critic_tokens = 0
        self.storage_state: dict | None = None

    # ------------------------------------------------------------- production

    def produce_shot(self, sheet: BeatSheet, shot: Shot) -> None:
        """Run one shot through its lane; fall back to RENDER on lane failure (PRD §9.3)."""
        t0 = time.monotonic()
        shot_dir = self.dir / "shots" / shot.id
        rec = ShotRecord(shot_id=shot.id, type=shot.type, final_duration_s=shot.duration_s)

        if shot.type == ShotType.RENDER:
            clip = render_shot(self.cfg, shot, shot_dir)
            rec.model = None
        elif shot.type == ShotType.GENERATE:
            continuity = self._continuity_frame(sheet, shot)
            try:
                clip, model = generate_shot(self.cfg, shot, shot_dir, continuity)
                rec.model = model
                rec.tokens_spent = shot.cost_estimate.value
            except Exception as e:
                console.print(f"[yellow]GENERATE failed for {shot.id} ({e}); demoting to RENDER[/]")
                self._demote(shot)
                clip = render_shot(self.cfg, shot, shot_dir)
                rec.type = ShotType.RENDER
        else:  # CAPTURE
            clip_opt, attempts, tokens, outcome = capture_shot(
                self.cfg, shot, self.app_url or "", shot_dir,
                storage_state=self.storage_state,
            )
            rec.model = self.cfg.models.capture
            rec.attempts = attempts
            rec.tokens_spent = tokens
            rec.verifier_outcome = outcome
            if clip_opt is None:
                console.print(f"[yellow]capture failed for {shot.id} after {attempts} attempts; demoting to RENDER[/]")
                self._demote(shot)
                clip = render_shot(self.cfg, shot, shot_dir)
                rec.type = ShotType.RENDER
            else:
                clip = clip_opt

        rec.wall_time_s = round(time.monotonic() - t0, 2)
        rec.output_path = str(clip)
        self.clips[shot.id] = clip
        self.records[shot.id] = rec
        self.vo_tracks[shot.id] = synthesize_vo(self.cfg, shot, self.dir / "vo")

    def _continuity_frame(self, sheet: BeatSheet, shot: Shot) -> Path | None:
        """Last frame of the previous GENERATE shot, if adjacent, for i2v continuity."""
        assert isinstance(shot.spec, GenerateSpec)
        ref_id = shot.spec.continuity_ref
        if ref_id and ref_id in self.clips:
            return last_frame(self.clips[ref_id], self.dir / "shots" / shot.id / "continuity.png")
        return None

    def _demote(self, shot: Shot) -> None:
        prompt = getattr(shot.spec, "prompt", None) or getattr(shot.spec, "goal", "")
        shot.type = ShotType.RENDER
        shot.spec = RenderSpec(template="title_card", payload={"title": "", "subtitle": str(prompt)[:120]})
        self.demoted.append(shot.id)

    # ------------------------------------------------------------- directives

    def apply_directives(self, sheet: BeatSheet, directives, beat_order: list[str]) -> tuple[set[str], list[str]]:
        """Mutate the beat sheet per the critic's typed directives.
        Returns (shot ids needing re-production, new beat order)."""
        dirty: set[str] = set()
        for d in directives:
            if isinstance(d, Retime):
                shots = sheet.shots_for_beat(d.beat_id)
                if not shots:
                    continue
                per = d.delta_s / len(shots)
                for s in shots:
                    s.duration_s = max(2.0, round(s.duration_s + per, 1))
                    # retime re-conforms the existing footage; no regeneration
                    old = self.clips.get(s.id)
                    if old:
                        retimed = old.with_name(f"{s.id}_retimed_{int(time.time())}.mp4")
                        conform_clip(old, retimed, s.duration_s, self.cfg.resolution, self.cfg.fps)
                        self.clips[s.id] = retimed
                        self.records[s.id].final_duration_s = s.duration_s
            elif isinstance(d, Reshoot):
                shot = sheet.shot(d.shot_id)
                self.reshoot_counts[shot.id] = self.reshoot_counts.get(shot.id, 0) + 1
                try:
                    shot.spec = shot.spec.model_copy(update=d.revised_spec)
                except Exception:
                    pass  # bad partial spec from the model: reshoot with the original spec
                dirty.add(shot.id)
            elif isinstance(d, Cut):
                sheet.shots = [s for s in sheet.shots if s.id != d.shot_id]
                for b in sheet.beats:
                    b.shots = [sid for sid in b.shots if sid != d.shot_id]
                self.clips.pop(d.shot_id, None)
            elif isinstance(d, Reorder):
                valid = [b for b in d.beat_ids if any(bt.id == b for bt in sheet.beats)]
                beat_order = valid + [b for b in beat_order if b not in valid]
            elif isinstance(d, RewriteVO):
                for s in sheet.shots_for_beat(d.beat_id):
                    if s.vo:
                        s.vo = d.text
                        self.vo_tracks[s.id] = synthesize_vo(self.cfg, s, self.dir / "vo")
                        break
        return dirty, beat_order

    # ------------------------------------------------------------- accounting

    def finalize(self, sheet: BeatSheet, final_cut: Path, scores: list[float]) -> RunManifest:
        self.manifest.shots = list(self.records.values())
        self.manifest.critic_scores = scores
        self.manifest.final_cut = str(final_cut)
        self.manifest.final_duration_s = round(probe_duration(final_cut), 2)

        spend_by_type: dict[str, int] = {}
        total = 0
        for rec in self.records.values():
            spend_by_type[rec.type.value] = spend_by_type.get(rec.type.value, 0) + rec.tokens_spent
            total += rec.tokens_spent
        spend_by_type["PLANNING"] = self.planning_tokens
        spend_by_type["CRITIC"] = self.critic_tokens
        total += self.planning_tokens + self.critic_tokens

        self.manifest.cost = CostReport(
            total_tokens=total,
            spend_by_type=spend_by_type,
            cost_per_finished_second=round(total / max(self.manifest.final_duration_s, 1), 1),
            demoted_shots=self.demoted,
        )
        (self.dir / "manifest.json").write_text(self.manifest.model_dump_json(indent=2))
        (self.dir / "beat_sheet.json").write_text(sheet.model_dump_json(indent=2))
        return self.manifest


def make_demo_video(
    cfg: Config,
    repo_url: str,
    app_url: str | None = None,
    browser_state_key: str | None = None,
) -> RunManifest:
    """The whole product: G1 through G3 in one call."""
    run = Run(cfg, repo_url, app_url)
    console.print(f"[bold]sizzle[/] run {run.run_id} -> {run.dir}")

    # 0. acquire GitHub token for private repos
    github_token: str | None = None
    if not cfg.dry_run:
        from .github_auth import (
            AppNotInstalledError,
            GitHubAuthError,
            InsufficientPermissionsError,
            create_installation_token,
            find_installation,
            is_repo_accessible,
            parse_owner_repo,
        )

        if not is_repo_accessible(repo_url):
            try:
                owner, repo_name = parse_owner_repo(repo_url)
                installation_id = find_installation(owner, repo_name)
                token_result = create_installation_token(installation_id, owner, repo_name)
                github_token = token_result.token
                run.manifest.is_private_repo = True
                console.print(
                    f"[dim]private repo detected; acquired installation token "
                    f"(expires {token_result.expires_at})[/]"
                )
            except (AppNotInstalledError, InsufficientPermissionsError):
                raise
            except GitHubAuthError as e:
                console.print(f"[yellow]GitHub auth failed ({e}); attempting public clone[/]")

    # 1. ingest + story
    repo = ingest(repo_url, run.dir, github_token=github_token)
    console.print(f"ingested [cyan]{repo.name}[/]: {len(repo.commit_log)} commits, README {len(repo.readme)} chars")
    from .story import write_beat_sheet

    sheet, planning_tokens = write_beat_sheet(cfg, repo)
    run.planning_tokens = planning_tokens
    console.print(f"beat sheet: {len(sheet.beats)} beats, {len(sheet.shots)} shots, {sheet.total_duration():.0f}s")

    # 1b. load authenticated browser state if available
    if browser_state_key:
        from .browser_auth import EncryptedBrowserState, decrypt_and_load

        import json

        try:
            import oss2
            from oss2.credentials import EnvironmentVariableCredentialsProvider

            auth = oss2.ProviderAuthV4(EnvironmentVariableCredentialsProvider())
            bucket = oss2.Bucket(
                auth,
                os.environ["OSS_ENDPOINT"],
                os.environ["OSS_BUCKET"],
                region=os.environ.get("ALIBABA_CLOUD_REGION", "ap-southeast-1"),
            )
            blob = json.loads(bucket.get_object(browser_state_key).read())
            encrypted = EncryptedBrowserState.from_json(json.dumps(blob))
            run.storage_state = decrypt_and_load(encrypted)
            run.manifest.authenticated_capture = True
            console.print("[dim]loaded authenticated browser state for capture[/]")
        except Exception as e:
            console.print(f"[yellow]failed to load browser state ({e}); capture will be unauthenticated[/]")

    # 2. allocate
    alloc = allocate(cfg, sheet, live_app_available=bool(app_url))
    run.demoted.extend(alloc.demoted)
    console.print(f"allocation: {alloc.assignments} | GENERATE spend {alloc.spent_tokens}/{alloc.budget_tokens}")

    # 3. produce every shot
    for shot in sheet.shots:
        console.print(f"  producing {shot.id} [{shot.type.value}] {shot.duration_s:.0f}s")
        run.produce_shot(sheet, shot)

    # 4. assemble + critic loop with monotonic best-cut retention
    beat_order = [b.id for b in sheet.beats]
    run.critic_tokens = 0
    scores: list[float] = []
    best_score, best_cut = -1.0, None

    for iteration in range(cfg.max_critic_iterations):
        cut = assemble(cfg, sheet, run.clips, run.vo_tracks, beat_order, run.dir, label=f"cut_v{iteration}")
        if not hard_constraints_ok(cfg, cut):
            console.print(f"[yellow]cut_v{iteration} violates the duration invariant[/]")
        verdict, tokens = review(cfg, cut, sheet, run.dir, iteration, run.reshoot_counts)
        run.critic_tokens += tokens
        scores.append(verdict.score)
        console.print(f"critic v{iteration}: score={verdict.score:.2f} — {verdict.notes}")

        if verdict.score > best_score:
            best_score, best_cut = verdict.score, cut

        if not verdict.directives:
            break
        if iteration > 0 and abs(scores[-1] - scores[-2]) < cfg.score_epsilon:
            console.print("score plateaued; stopping")
            break
        if iteration == cfg.max_critic_iterations - 1:
            break

        dirty, beat_order = run.apply_directives(sheet, verdict.directives, beat_order)
        for shot_id in dirty:
            run.produce_shot(sheet, sheet.shot(shot_id))

    assert best_cut is not None
    final = run.dir / "final_cut.mp4"
    final.write_bytes(best_cut.read_bytes())

    manifest = run.finalize(sheet, final, scores)
    console.print(f"[green]done[/]: {final} ({manifest.final_duration_s}s), "
                  f"{manifest.cost.total_tokens} tokens, "
                  f"{manifest.cost.cost_per_finished_second} tok/s finished")
    return manifest
