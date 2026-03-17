"""Main orchestration pipeline for Manim educational video generation.

Ties together context processing, scene planning, code generation,
rendering, TTS, and stitching into a single pipeline.
"""

import json
import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .code_generator import (
    edit_scene_code,
    generate_all_scenes,
    generate_scene_code,
    recover_from_error,
)
from .context_processor import Context, process_context
from .planner import plan_scenes
from .renderer import RenderResult, render_all_scenes, render_scene
from .stitcher import stitch_video
from .tts import generate_tts_for_scenes

logger = logging.getLogger(__name__)

BASE_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "manim_output"
BASE_WORKSPACE_DIR = Path(__file__).resolve().parent.parent / "manim_workspace"

MAX_RENDER_RETRIES = 3
MAX_PARALLEL_SCENE_PIPELINES = 3


# ---------------------------------------------------------------------------
# Video Presets
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VideoPreset:
    name: str
    width: int
    height: int
    aspect_ratio: str
    frame_height: float
    layout_hint: str      # Spatial layout guidance for codegen
    style_hint: str       # Animation pacing and style guidance for codegen
    planner_hint: str     # Scene count, duration, and narrative guidance for planner

VIDEO_PRESETS: dict[str, VideoPreset] = {
    "youtube_deep_dive": VideoPreset(
        name="YouTube Deep Dive",
        width=1920, height=1080,
        aspect_ratio="16:9",
        frame_height=8.0,
        layout_hint="Wide 16:9 layout. The Manim visible frame is ~14.2 units wide × 8.0 units tall "
                     "(x: -7.1 to +7.1, y: -4.0 to +4.0). Use the full horizontal width for graphs, "
                     "equations, and diagrams. Side-by-side comparisons work well. "
                     "CRITICAL: All content MUST stay within x∈[-6.5, 6.5] and y∈[-3.5, 3.5] to avoid cropping. "
                     "Use to_edge() and to_corner() with buff=0.5 to keep elements safely inside the frame.",
        style_hint="Paced for deep understanding. Use deliberate, smooth animations with run_time=2-3s. "
                   "Add self.wait(2-4) after key reveals so the viewer can absorb the idea. "
                   "Build up visuals layer by layer. Show intermediate steps in derivations. "
                   "Use Indicate() and FlashAround() to draw attention to critical parts.",
        planner_hint="Plan 6-12 scenes for a detailed educational video (3-6 minutes total). "
                     "Each scene should have 4-6 sentences of narration (~25-40 seconds per scene). "
                     "Follow a full 3-act narrative: hook with a compelling question, "
                     "build understanding step by step, and synthesize with a satisfying takeaway. "
                     "Allow space for mathematical rigor and thorough explanations.",
    ),
    "youtube_explainer": VideoPreset(
        name="YouTube Explainer",
        width=1920, height=1080,
        aspect_ratio="16:9",
        frame_height=8.0,
        layout_hint="Wide 16:9 layout. The Manim visible frame is ~14.2 units wide × 8.0 units tall "
                     "(x: -7.1 to +7.1, y: -4.0 to +4.0). Use the full horizontal width for graphs, "
                     "equations, and diagrams. Side-by-side comparisons work well. "
                     "CRITICAL: All content MUST stay within x∈[-6.5, 6.5] and y∈[-3.5, 3.5] to avoid cropping. "
                     "Use to_edge() and to_corner() with buff=0.5 to keep elements safely inside the frame.",
        style_hint="Moderately paced — keep visuals engaging but not rushed. "
                   "Use run_time=1.5-2.5s for animations. Add self.wait(1.5-3) after key moments. "
                   "Favor visual intuition over formal proofs. Use color and motion to guide the eye.",
        planner_hint="Plan 4-8 scenes for a focused explainer video (2-4 minutes total). "
                     "Each scene should have 3-5 sentences of narration (~20-30 seconds per scene). "
                     "Follow a clear narrative arc: interesting hook, core explanation, clear conclusion. "
                     "Prioritize visual intuition — the audience wants to *get it*, not see every proof step.",
    ),
    "youtube_short": VideoPreset(
        name="YouTube Short",
        width=1080, height=1920,
        aspect_ratio="9:16",
        frame_height=14.2,
        layout_hint="Tall 9:16 PORTRAIT layout for vertical video. The Manim visible frame is ~8.0 units wide × 14.2 units tall "
                     "(x: -4.0 to +4.0, y: -7.1 to +7.1). THIS IS A NARROW, TALL FRAME. "
                     "Stack elements VERTICALLY — title at y≈5-6, main visual centered around y≈0, "
                     "supporting text or labels at y≈-4 to -5. "
                     "CRITICAL: The frame is only 8 units wide! All content MUST stay within x∈[-3.5, 3.5] and y∈[-6.5, 6.5]. "
                     "Use set_width(6) or smaller on wide elements (Axes, NumberPlane) to fit the narrow frame. "
                     "For Axes, use x_range with a narrow span and set unit_size < 1 to prevent horizontal overflow. "
                     "Use font_size=56 or larger for mobile readability. "
                     "NEVER use side-by-side layouts — always stack top-to-bottom. "
                     "After creating any element, verify its width fits: mob.set_width(min(mob.get_width(), 7)).",
        style_hint="FAST-PACED and punchy. Animations should be quick: run_time=0.5-1.5s. "
                   "Use self.wait(0.5-1.5) — just enough to register, never lingering. "
                   "Favor snappy transitions: FadeIn(shift=UP*0.5), rapid Create. "
                   "Use bold colors and large text. Every second must have visual motion. "
                   "No slow builds — get to the payoff fast. Use Flash() and Indicate() liberally.",
        planner_hint="Plan 2-3 scenes for a YouTube Short (30-50 seconds TOTAL). "
                     "Each scene: 1-2 sentences of narration (~10-15 seconds per scene). "
                     "Structure: hook with a surprising visual in the first 2 seconds, "
                     "one core insight delivered fast, one memorable takeaway. "
                     "No introductions, no filler. Start with the most visually striking element. "
                     "The narration should be punchy and conversational — like telling a friend a cool fact.",
    ),
    "tiktok": VideoPreset(
        name="TikTok / Instagram Reel",
        width=1080, height=1920,
        aspect_ratio="9:16",
        frame_height=14.2,
        layout_hint="Tall 9:16 PORTRAIT layout. The Manim visible frame is ~8.0 units wide × 14.2 units tall "
                     "(x: -4.0 to +4.0, y: -7.1 to +7.1). THIS IS A NARROW, TALL FRAME. "
                     "Stack EVERYTHING vertically. Use font_size=60+ for maximum mobile readability. "
                     "Keep everything centered — avoid edges (platform UI overlaps). "
                     "CRITICAL: The frame is only 8 units wide! All content MUST stay within x∈[-3.0, 3.0] and y∈[-5.0, 5.0] "
                     "(leave bottom 15% clear for captions/comments). "
                     "Use set_width(6) or smaller on wide elements (Axes, NumberPlane). "
                     "For Axes, use x_range with a narrow span and set unit_size < 1 to prevent horizontal overflow. "
                     "NEVER use side-by-side layouts — always stack top-to-bottom. "
                     "After creating any element, verify its width fits: mob.set_width(min(mob.get_width(), 6)).",
        style_hint="MAXIMUM energy and speed. Animations should be rapid: run_time=0.3-1.0s. "
                   "Minimal self.wait() — at most 0.5-1.0s, keep constant visual motion. "
                   "Use dramatic reveals: scale-up FadeIn, quick Create, Flash on key numbers. "
                   "Bold, high-contrast colors. Oversized text and numbers. "
                   "Every frame must be visually interesting — viewers will swipe away in 1 second if bored. "
                   "Use LaggedStartMap for rapid sequential reveals. Favor wow-factor over precision.",
        planner_hint="Plan 1-3 scenes for a TikTok/Reel (15-45 seconds TOTAL). "
                     "Each scene: 1-2 sentences MAX of narration (~8-15 seconds per scene). "
                     "Structure: instant hook (\"Did you know...\", \"Watch what happens when...\"), "
                     "one mind-blowing visual, optional punchline/takeaway. "
                     "The first scene MUST grab attention in under 2 seconds with a striking animation. "
                     "Narration should feel like someone excitedly sharing a discovery, not lecturing.",
    ),
    "instagram_post": VideoPreset(
        name="Instagram / LinkedIn Post",
        width=1080, height=1080,
        aspect_ratio="1:1",
        frame_height=8.0,
        layout_hint="Square 1:1 layout. The Manim visible frame is 8.0 units wide × 8.0 units tall "
                     "(x: -4.0 to +4.0, y: -4.0 to +4.0). "
                     "Center the main visual. Use compact, centered arrangements — avoid edges. "
                     "CRITICAL: All content MUST stay within x∈[-3.5, 3.5] and y∈[-3.5, 3.5] to avoid cropping. "
                     "Use font_size=48+ for readability. "
                     "Use set_width(6) or smaller on wide elements. "
                     "Works well for single-concept demonstrations.",
        style_hint="Clean and polished, medium pace. run_time=1.0-2.0s for animations. "
                   "self.wait(1-2) between key steps. Favor elegant, satisfying animations. "
                   "Use smooth transforms and clean geometry. "
                   "The video should feel like a well-crafted GIF — loopable and shareable.",
        planner_hint="Plan 2-4 scenes for a social media post video (30-90 seconds total). "
                     "Each scene: 2-3 sentences of narration (~15-20 seconds per scene). "
                     "Focus on ONE concept and show it beautifully. "
                     "Minimal text narration — let the visuals do the talking. "
                     "The video should be satisfying to watch even on mute with auto-captions.",
    ),
    "doubt_clearer": VideoPreset(
        name="Doubt Clearer",
        width=1920, height=1080,
        aspect_ratio="16:9",
        frame_height=8.0,
        layout_hint="Wide 16:9 layout. The Manim visible frame is ~14.2 units wide × 8.0 units tall "
                     "(x: -7.1 to +7.1, y: -4.0 to +4.0). "
                     "Center the single visual concept. Keep the scene uncluttered. "
                     "CRITICAL: All content MUST stay within x∈[-6.5, 6.5] and y∈[-3.5, 3.5] to avoid cropping. "
                     "Use large font sizes (font_size=56+) so the answer is immediately readable. "
                     "Place the doubt/question at the top and the visual answer in the center.",
        style_hint="Direct and efficient. run_time=1.0-2.0s for the key reveal animation. "
                   "self.wait(1-2) to let the answer sink in. No unnecessary flourishes. "
                   "One clear visual that answers the doubt — show the 'aha' moment cleanly. "
                   "Use Indicate() or FlashAround() once on the key insight. "
                   "The entire scene should feel like a crisp, confident answer.",
        planner_hint="Plan exactly 1 scene for a quick doubt-clearing clip (10-15 seconds TOTAL). "
                     "The single scene should have 1-2 sentences of narration (~10-15 seconds). "
                     "Structure: state the doubt/misconception in one line, then immediately "
                     "show the correct answer with a clear visual. No build-up, no backstory. "
                     "Think of it as a teacher answering a student's raised hand — quick, precise, done. "
                     "The narration should directly address the doubt: 'The reason is...', 'This works because...'.",
    ),
}

DEFAULT_PRESET = "youtube_explainer"


def get_preset(name: str) -> VideoPreset:
    """Get a video preset by name, falling back to landscape."""
    return VIDEO_PRESETS.get(name, VIDEO_PRESETS[DEFAULT_PRESET])


@dataclass
class PipelineResult:
    video_id: str
    success: bool
    video_path: str | None = None
    plan: dict = field(default_factory=dict)
    references: list[dict] = field(default_factory=list)
    error: str | None = None
    elapsed_seconds: float = 0.0


StatusCallback = Callable[..., None]


def _noop_status(msg: str, **kwargs) -> None:
    pass


QUALITY_SCALE: dict[str, float] = {
    "l": 480 / 1080,
    "m": 720 / 1080,
    "h": 1.0,
    "k": 2.0,
}


def _scaled_resolution(preset: VideoPreset | None, quality: str) -> tuple[int, int] | None:
    """Scale the preset's pixel resolution based on quality level.

    The coordinate system (frame_height) stays unchanged — only pixels shrink.
    This keeps the correct aspect ratio while dramatically reducing render time.
    """
    if preset is None:
        return None
    scale = QUALITY_SCALE.get(quality, 1.0)
    w = max(2, int(preset.width * scale) // 2 * 2)   # keep even
    h = max(2, int(preset.height * scale) // 2 * 2)
    return (w, h)


def _process_single_scene(
    scene: dict,
    plan: dict,
    context: Context,
    code_dir: Path,
    workspace: Path,
    video_id: str,
    quality: str,
    status: StatusCallback,
    preset: VideoPreset | None = None,
) -> tuple[str, str, RenderResult]:
    """Run the full codegen -> render -> error-recovery cycle for one scene.

    Returns:
        (scene_id, final_code, RenderResult)
    """
    scene_id = scene["id"]
    resolution = _scaled_resolution(preset, quality)
    frame_height = preset.frame_height if preset else None

    status(f"generating_code_{scene_id}")
    logger.info("Pipeline %s: codegen for %s", video_id, scene_id)
    code = generate_scene_code(scene, plan, context)
    (code_dir / f"{scene_id}.py").write_text(code, encoding="utf-8")

    status(f"rendering_{scene_id}")
    logger.info("Pipeline %s: rendering %s", video_id, scene_id)
    result = render_scene(
        scene_id, code, workspace, video_id=video_id, quality=quality,
        resolution=resolution, frame_height=frame_height,
    )

    for retry in range(1, MAX_RENDER_RETRIES + 1):
        if result.success:
            break
        logger.info("Pipeline %s: retry %d/%d for %s", video_id, retry, MAX_RENDER_RETRIES, scene_id)
        status(f"fixing_{scene_id}_retry_{retry}")

        try:
            code = recover_from_error(scene_id, code, result.error or "Unknown error")
            (code_dir / f"{scene_id}.py").write_text(code, encoding="utf-8")
            result = render_scene(
                scene_id, code, workspace, video_id=video_id, quality=quality,
                resolution=resolution, frame_height=frame_height,
            )
        except Exception as e:
            logger.error("Pipeline %s: error recovery failed for %s: %s", video_id, scene_id, e)

    if result.success:
        logger.info("Pipeline %s: scene %s completed successfully", video_id, scene_id)
    else:
        logger.warning("Pipeline %s: scene %s failed after all retries", video_id, scene_id)

    return scene_id, code, result


def run_pipeline(
    video_id: str,
    topic: str,
    files: list[Path] | None = None,
    urls: list[str] | None = None,
    on_status: StatusCallback | None = None,
    quality: str = "l",
    preset_name: str = DEFAULT_PRESET,
) -> PipelineResult:
    """Run the complete video generation pipeline.

    Args:
        video_id: Unique identifier for this generation.
        topic: Educational topic from the teacher.
        files: Optional uploaded file paths (PDFs, images).
        urls: Optional reference URLs.
        on_status: Callback for progress updates.
        quality: Render quality — "l" (low 480p), "m" (medium 720p),
                 "h" (HD 1080p), "k" (4K). Defaults to "l".
        preset_name: Video preset — "youtube_deep_dive", "youtube_explainer",
                     "youtube_short", "tiktok", "instagram_post".
                     Defaults to "youtube_explainer".

    Returns:
        PipelineResult with video path and metadata.
    """
    status = on_status or _noop_status
    preset = get_preset(preset_name)
    t0 = time.time()
    render_res = _scaled_resolution(preset, quality)
    logger.info(
        "Pipeline %s: preset '%s' (%dx%d %s), render at %dx%d (quality=%s)",
        video_id, preset.name, preset.width, preset.height,
        preset.aspect_ratio, render_res[0] if render_res else 0, render_res[1] if render_res else 0, quality,
    )

    output_dir = BASE_OUTPUT_DIR / video_id
    output_dir.mkdir(parents=True, exist_ok=True)
    workspace = BASE_WORKSPACE_DIR / video_id
    workspace.mkdir(parents=True, exist_ok=True)
    audio_dir = output_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Step 1: Process context
        status("processing_context")
        logger.info("Pipeline %s: processing context", video_id)
        context = process_context(topic, files, urls)

        context_path = output_dir / "context.json"
        context_path.write_text(
            json.dumps(context.to_dict(), indent=2), encoding="utf-8"
        )

        # Step 2: Plan scenes
        status("planning_scenes")
        logger.info("Pipeline %s: planning scenes", video_id)
        plan = plan_scenes(context, format_instructions=preset.planner_hint)

        plan_path = output_dir / "plan.json"
        plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

        # Step 3: Generate TTS audio first to get exact narration durations
        status("generating_audio")
        logger.info("Pipeline %s: generating TTS audio", video_id)
        durations = generate_tts_for_scenes(plan["scenes"], audio_dir)

        # Inject preset info and TTS durations into each scene dict
        plan["preset"] = {
            "name": preset.name,
            "aspect_ratio": preset.aspect_ratio,
            "resolution": f"{preset.width}x{preset.height}",
        }
        for scene in plan["scenes"]:
            sid = scene["id"]
            if sid in durations:
                scene["audio_duration"] = durations[sid]
                scene["estimated_duration"] = round(durations[sid], 1)
            scene["layout_hint"] = preset.layout_hint
            scene["style_hint"] = preset.style_hint
            scene["aspect_ratio"] = preset.aspect_ratio

        plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

        # Steps 4-5: Per-scene (codegen -> render -> retry) in parallel
        scene_ids = [s["id"] for s in plan["scenes"]]
        status("generating_scenes",
               scenes_total=len(plan["scenes"]),
               scene_ids=scene_ids)
        logger.info(
            "Pipeline %s: launching %d per-scene pipelines (%d workers)",
            video_id, len(plan["scenes"]), MAX_PARALLEL_SCENE_PIPELINES,
        )

        code_dir = output_dir / "scene_code"
        code_dir.mkdir(parents=True, exist_ok=True)

        total_scenes = len(plan["scenes"])

        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_SCENE_PIPELINES) as pool:
            scene_futures = {
                pool.submit(
                    _process_single_scene,
                    scene, plan, context,
                    code_dir, workspace, video_id, quality, status,
                    preset=preset,
                ): scene["id"]
                for scene in plan["scenes"]
            }

            scene_codes: dict[str, str] = {}
            render_results: dict[str, RenderResult] = {}

            for future in as_completed(scene_futures):
                sid = scene_futures[future]
                try:
                    _, code, result = future.result()
                    scene_codes[sid] = code
                    render_results[sid] = result
                except Exception as e:
                    logger.error("Pipeline %s: scene %s pipeline failed: %s", video_id, sid, e)
                    render_results[sid] = RenderResult(success=False, scene_id=sid, error=str(e))

        succeeded = sum(1 for r in render_results.values() if r.success)
        if succeeded == 0:
            raise RuntimeError("All scenes failed to render")

        logger.info(
            "Pipeline %s: %d/%d scenes rendered", video_id, succeeded, len(render_results)
        )

        # Step 6: Stitch video
        status("stitching")
        logger.info("Pipeline %s: stitching video", video_id)
        final_video_path = output_dir / "video.mp4"
        stitch_video(plan["scenes"], audio_dir, render_results, final_video_path)

        elapsed = time.time() - t0
        status("completed")
        logger.info("Pipeline %s: completed in %.1fs", video_id, elapsed)

        references_list = [
            {
                "source": ref.source,
                "content": ref.content[:200],
                "source_type": ref.source_type,
                "page_or_section": ref.page_or_section,
            }
            for ref in context.references
        ]

        return PipelineResult(
            video_id=video_id,
            success=True,
            video_path=str(final_video_path),
            plan=plan,
            references=references_list,
            elapsed_seconds=round(elapsed, 1),
        )

    except Exception as e:
        elapsed = time.time() - t0
        logger.error("Pipeline %s: failed after %.1fs: %s", video_id, elapsed, e)
        status("failed")
        return PipelineResult(
            video_id=video_id,
            success=False,
            error=str(e),
            elapsed_seconds=round(elapsed, 1),
        )


def edit_pipeline(
    video_id: str,
    scene_id: str,
    edit_prompt: str,
    on_status: StatusCallback | None = None,
    quality: str = "l",
    preset_name: str = DEFAULT_PRESET,
) -> PipelineResult:
    """Re-generate a single scene with an edit instruction, re-render, and re-stitch.

    Args:
        video_id: Existing video ID.
        scene_id: Scene to edit.
        edit_prompt: Teacher's edit instruction.
        on_status: Callback for progress updates.
        quality: Render quality — "l", "m", "h", or "k". Defaults to "l".
        preset_name: Video preset — "landscape", "portrait", or "square".

    Returns:
        PipelineResult with updated video.
    """
    status = on_status or _noop_status
    t0 = time.time()

    output_dir = BASE_OUTPUT_DIR / video_id
    workspace = BASE_WORKSPACE_DIR / video_id
    audio_dir = output_dir / "audio"
    code_dir = output_dir / "scene_code"

    plan_path = output_dir / "plan.json"
    if not plan_path.exists():
        return PipelineResult(
            video_id=video_id,
            success=False,
            error="Video not found",
        )

    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    # Recover preset from saved plan, or use the one passed in
    saved_preset = plan.get("preset", {}).get("name", "").lower()
    if saved_preset and saved_preset != preset_name:
        for key, p in VIDEO_PRESETS.items():
            if p.name.lower() == saved_preset or key == saved_preset:
                preset_name = key
                break
    preset = get_preset(preset_name)
    resolution = _scaled_resolution(preset, quality)
    frame_height = preset.frame_height

    current_code_path = code_dir / f"{scene_id}.py"
    if not current_code_path.exists():
        return PipelineResult(
            video_id=video_id,
            success=False,
            error=f"Scene {scene_id} code not found",
        )

    current_code = current_code_path.read_text(encoding="utf-8")

    try:
        status("editing_code")
        logger.info("Edit pipeline %s/%s: generating edited code", video_id, scene_id)
        new_code = edit_scene_code(current_code, edit_prompt)
        current_code_path.write_text(new_code, encoding="utf-8")

        status("rendering")
        logger.info("Edit pipeline %s/%s: rendering", video_id, scene_id)
        result = render_scene(
            scene_id, new_code, workspace, video_id=video_id, quality=quality,
            resolution=resolution, frame_height=frame_height,
        )

        for retry in range(1, MAX_RENDER_RETRIES + 1):
            if result.success:
                break
            logger.info("Edit pipeline: retry %d for %s", retry, scene_id)
            status(f"fixing_retry_{retry}")
            fixed = recover_from_error(scene_id, new_code, result.error or "")
            new_code = fixed
            current_code_path.write_text(fixed, encoding="utf-8")
            result = render_scene(
                scene_id, fixed, workspace, video_id=video_id, quality=quality,
                resolution=resolution, frame_height=frame_height,
            )

        if not result.success:
            raise RuntimeError(f"Scene {scene_id} failed after all retries: {result.error}")

        # Re-stitch all scenes
        status("stitching")
        logger.info("Edit pipeline %s: re-stitching", video_id)

        all_codes = {}
        for code_file in code_dir.glob("*.py"):
            sid = code_file.stem
            all_codes[sid] = code_file.read_text(encoding="utf-8")

        all_render_results = render_all_scenes(
            {sid: code for sid, code in all_codes.items() if sid != scene_id},
            workspace,
            video_id=video_id,
            quality=quality,
            resolution=resolution,
            frame_height=frame_height,
        )
        all_render_results[scene_id] = result

        final_path = output_dir / "video.mp4"
        stitch_video(plan["scenes"], audio_dir, all_render_results, final_path)

        elapsed = time.time() - t0
        status("completed")
        logger.info("Edit pipeline %s: completed in %.1fs", video_id, elapsed)

        return PipelineResult(
            video_id=video_id,
            success=True,
            video_path=str(final_path),
            plan=plan,
            elapsed_seconds=round(elapsed, 1),
        )

    except Exception as e:
        elapsed = time.time() - t0
        logger.error("Edit pipeline %s: failed: %s", video_id, e)
        status("failed")
        return PipelineResult(
            video_id=video_id,
            success=False,
            error=str(e),
            elapsed_seconds=round(elapsed, 1),
        )
