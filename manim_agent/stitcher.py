"""FFmpeg video stitching — compose scene clips with narration audio.

For each scene: merge scene .mp4 with narration .wav.
Then concatenate all scene clips into a final video with crossfades.
"""

import json
import logging
import subprocess
import time
from pathlib import Path

import requests

from .renderer import FFMPEG_BIN, FFPROBE_BIN

logger = logging.getLogger(__name__)


def _download_video(url: str, dest: Path) -> None:
    """Download a video file from a URL."""
    logger.info("Stitcher: downloading %s -> %s", url, dest)
    resp = requests.get(url, timeout=120, stream=True)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    logger.info("Stitcher: downloaded %d bytes", dest.stat().st_size)

CROSSFADE_DURATION = 0.5  # seconds


def _run_ffmpeg(args: list[str], description: str, timeout: int = 120) -> None:
    """Run an ffmpeg command and log output."""
    cmd = [FFMPEG_BIN, "-y", *args]
    logger.debug("FFmpeg [%s]: %s", description, " ".join(cmd))

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    if result.returncode != 0:
        logger.error("FFmpeg [%s] failed:\n%s", description, result.stderr[-2000:])
        raise RuntimeError(f"FFmpeg {description} failed: {result.stderr[-500:]}")


def _merge_scene_with_audio(
    video_path: Path,
    audio_path: Path | None,
    output_path: Path,
) -> None:
    """Merge a scene video with its narration audio.

    If audio is longer than video, the last video frame is frozen until
    the narration finishes — no audio is cut off.
    If video is longer than audio, the audio is padded with silence so
    the full animation plays out — no video is cut off.
    """
    if audio_path and audio_path.exists():
        video_dur = _get_duration(video_path)
        audio_dur = _get_duration(audio_path)

        if audio_dur > video_dur + 0.5:
            # Audio is longer — freeze the last frame to cover the narration.
            # -stream_loop -1 loops the video; -shortest then trims to audio length.
            logger.info(
                "Stitcher: audio (%.1fs) longer than video (%.1fs) for %s, freezing last frame",
                audio_dur, video_dur, video_path.stem,
            )
            _run_ffmpeg(
                [
                    "-i", str(video_path),
                    "-i", str(audio_path),
                    "-filter_complex",
                    f"[0:v]tpad=stop_mode=clone:stop_duration={audio_dur - video_dur + 1}[v]",
                    "-map", "[v]", "-map", "1:a:0",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "128k",
                    "-shortest",
                    str(output_path),
                ],
                f"merge-extend {video_path.stem}",
            )
        else:
            # Video is at least as long as audio — mux together and let the
            # video play out fully.  Do NOT use -shortest here: that would cut
            # the animation when the narration ends.  Instead, pad the audio
            # with silence so both streams have the same duration.
            pad_dur = max(0, video_dur - audio_dur + 0.1)
            _run_ffmpeg(
                [
                    "-i", str(video_path),
                    "-i", str(audio_path),
                    "-filter_complex",
                    f"[1:a]apad=pad_dur={pad_dur}[a]",
                    "-map", "0:v:0", "-map", "[a]",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "128k",
                    "-shortest",
                    str(output_path),
                ],
                f"merge {video_path.stem}",
            )
    else:
        _run_ffmpeg(
            [
                "-i", str(video_path),
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-an",
                str(output_path),
            ],
            f"copy {video_path.stem}",
        )


def _get_duration(path: Path) -> float:
    """Get media file duration via ffprobe."""
    try:
        result = subprocess.run(
            [
                FFPROBE_BIN, "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return 0.0


def stitch_video(
    scenes: list[dict],
    audio_dir: Path,
    render_results: dict,
    output_path: Path,
) -> Path:
    """Stitch scene clips and audio into a final video.

    Args:
        scenes: Scene plan list (ordered).
        audio_dir: Directory containing .wav files named by scene_id.
        render_results: Dict of scene_id -> RenderResult from renderer.
        output_path: Path for the final output video.

    Returns:
        Path to the final stitched video.
    """
    logger.info("Stitcher: starting with %d scenes", len(scenes))
    t0 = time.time()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = output_path.parent / "temp_stitch"
    temp_dir.mkdir(parents=True, exist_ok=True)

    merged_clips: list[Path] = []
    scene_metadata: list[dict] = []
    current_time = 0.0

    for scene in scenes:
        scene_id = scene["id"]
        render = render_results.get(scene_id)

        if not render or not render.success:
            logger.warning("Stitcher: skipping scene %s (no render)", scene_id)
            continue

        if render.mp4_path:
            video_path = Path(render.mp4_path)
        elif render.public_url:
            video_path = temp_dir / f"{scene_id}_downloaded.mp4"
            if not video_path.exists():
                _download_video(render.public_url, video_path)
        else:
            logger.warning("Stitcher: skipping scene %s (no mp4_path or public_url)", scene_id)
            continue
        audio_path = audio_dir / f"{scene_id}.wav"
        merged_path = temp_dir / f"{scene_id}_merged.mp4"

        try:
            _merge_scene_with_audio(
                video_path,
                audio_path if audio_path.exists() else None,
                merged_path,
            )
        except RuntimeError as e:
            logger.error("Stitcher: merge failed for %s: %s", scene_id, e)
            try:
                _merge_scene_with_audio(video_path, None, merged_path)
            except RuntimeError:
                logger.error("Stitcher: copy also failed for %s, skipping", scene_id)
                continue

        duration = _get_duration(merged_path)
        merged_clips.append(merged_path)
        scene_metadata.append({
            "id": scene_id,
            "title": scene.get("title", scene_id),
            "startTime": round(current_time, 2),
            "endTime": round(current_time + duration, 2),
            "duration": round(duration, 2),
            "narration": scene.get("narration", ""),
            "references": scene.get("references", []),
        })
        current_time += duration

    if not merged_clips:
        raise RuntimeError("No scenes were successfully merged")

    if len(merged_clips) == 1:
        import shutil
        shutil.copy2(str(merged_clips[0]), str(output_path))
    else:
        concat_file = temp_dir / "concat.txt"
        with open(concat_file, "w") as f:
            for clip in merged_clips:
                f.write(f"file '{clip}'\n")

        _run_ffmpeg(
            [
                "-f", "concat", "-safe", "0",
                "-i", str(concat_file),
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                str(output_path),
            ],
            "concatenate all scenes",
            timeout=300,
        )

    total_duration = sum(s["duration"] for s in scene_metadata)

    screenshots = _extract_screenshots(output_path, scene_metadata, total_duration)

    composition = {
        "videoId": output_path.parent.name,
        "title": scenes[0].get("title", "Educational Video") if scenes else "Video",
        "videoFile": "video.mp4",
        "duration": round(total_duration, 2),
        "total_scenes": len(scene_metadata),
        "sections": scene_metadata,
        "screenshots": screenshots,
    }

    composition_path = output_path.parent / "composition.json"
    composition_path.write_text(json.dumps(composition, indent=2), encoding="utf-8")

    metadata_path = output_path.parent / "metadata.json"
    metadata_path.write_text(json.dumps(composition, indent=2), encoding="utf-8")

    elapsed = time.time() - t0
    logger.info(
        "Stitcher: done in %.1fs — %d scenes, %.1fs total, output=%s",
        elapsed, len(merged_clips), total_duration, output_path,
    )

    return output_path


SCREENSHOT_INTERVAL = 3.0


def _extract_screenshots(
    video_path: Path,
    scene_metadata: list[dict],
    total_duration: float,
) -> list[dict]:
    """Extract screenshots from the final video at regular intervals and section boundaries."""
    screenshots_dir = video_path.parent / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    times: set[float] = set()
    for t in _frange(0, total_duration, SCREENSHOT_INTERVAL):
        times.add(round(t, 1))
    for s in scene_metadata:
        times.add(round(s["startTime"], 1))

    screenshots: list[dict] = []
    for t in sorted(times):
        fname = f"{t:.1f}.jpg"
        out_path = screenshots_dir / fname
        try:
            _run_ffmpeg(
                [
                    "-ss", str(t),
                    "-i", str(video_path),
                    "-frames:v", "1",
                    "-q:v", "5",
                    str(out_path),
                ],
                f"screenshot at {t}s",
                timeout=15,
            )
            if out_path.exists() and out_path.stat().st_size > 0:
                screenshots.append({"time": t, "file": f"screenshots/{fname}"})
        except Exception as e:
            logger.warning("Stitcher: screenshot at %.1fs failed: %s", t, e)

    logger.info("Stitcher: extracted %d screenshots", len(screenshots))
    return screenshots


def _frange(start: float, stop: float, step: float):
    """Float range generator."""
    t = start
    while t < stop:
        yield t
        t += step
