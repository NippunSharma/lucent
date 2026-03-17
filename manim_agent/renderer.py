"""Manim Community Edition rendering via Cloud Run service.

Sends scene code to a remote Manim renderer running on Cloud Run,
which renders headless on Linux with Cairo.
Falls back to local rendering if MANIM_RENDER_BACKEND=local.
"""

import hashlib
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import google.auth.transport.requests
import google.oauth2.id_token
import requests

logger = logging.getLogger(__name__)

RENDER_BACKEND = os.environ.get("MANIM_RENDER_BACKEND", "cloudrun")
CLOUDRUN_URL = os.environ.get(
    "MANIMGL_CLOUDRUN_URL",
    "https://manimgl-renderer-271738835587.us-east1.run.app",
)
RENDER_TIMEOUT = int(os.environ.get("MANIM_RENDER_TIMEOUT", "120"))
DEFAULT_QUALITY = os.environ.get("MANIM_RENDER_QUALITY", "l")

_BUNDLED_FFMPEG_DIR = Path(__file__).resolve().parent.parent / "ffmpeg-full" / "ffmpeg-master-latest-win64-gpl" / "bin"


def _find_ffprobe() -> str:
    bundled = _BUNDLED_FFMPEG_DIR / "ffprobe.exe"
    if bundled.exists():
        return str(bundled)
    import shutil
    system = shutil.which("ffprobe")
    if system:
        return system
    return "ffprobe"


FFPROBE_BIN = _find_ffprobe()


def _find_ffmpeg() -> str:
    bundled = _BUNDLED_FFMPEG_DIR / "ffmpeg.exe"
    if bundled.exists():
        return str(bundled)
    import shutil
    system = shutil.which("ffmpeg")
    if system:
        return system
    return "ffmpeg"


FFMPEG_BIN = _find_ffmpeg()


def _get_id_token(audience: str) -> str | None:
    """Get a Google OIDC identity token for Cloud Run auth."""
    try:
        auth_req = google.auth.transport.requests.Request()
        token = google.oauth2.id_token.fetch_id_token(auth_req, audience)
        return token
    except Exception as e:
        logger.warning("Could not get ID token (running locally?): %s", e)
        return None


@dataclass
class RenderResult:
    success: bool
    scene_id: str
    mp4_path: str | None = None
    public_url: str | None = None
    gcs_uri: str | None = None
    duration: float | None = None
    error: str | None = None
    code_hash: str | None = None


def _get_video_duration(mp4_path: Path) -> float | None:
    try:
        result = subprocess.run(
            [
                FFPROBE_BIN, "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "csv=p=0",
                str(mp4_path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Cloud Run rendering
# ---------------------------------------------------------------------------

def _render_scene_cloudrun(
    scene_id: str,
    code: str,
    video_id: str = "",
    quality: str = DEFAULT_QUALITY,
    resolution: tuple[int, int] | None = None,
    frame_height: float | None = None,
) -> RenderResult:
    """Render a scene via the Cloud Run Manim service."""
    code_hash = hashlib.sha256(code.encode()).hexdigest()[:16]
    t0 = time.time()

    if not video_id:
        video_id = f"vid_{code_hash}"

    headers = {"Content-Type": "application/json"}
    token = _get_id_token(CLOUDRUN_URL)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    payload: dict = {
        "manim_code": code,
        "scene_id": scene_id,
        "video_id": video_id,
        "quality": quality,
    }
    if resolution:
        payload["resolution"] = list(resolution)
    if frame_height is not None:
        payload["frame_height"] = frame_height

    logger.info("Renderer %s: sending to Cloud Run (%s)", scene_id, CLOUDRUN_URL)

    max_429_retries = 5
    for attempt in range(max_429_retries + 1):
        try:
            resp = requests.post(
                f"{CLOUDRUN_URL}/render",
                json=payload,
                headers=headers,
                timeout=RENDER_TIMEOUT,
            )
        except requests.Timeout:
            return RenderResult(
                success=False, scene_id=scene_id,
                error=f"Cloud Run request timed out after {RENDER_TIMEOUT}s",
                code_hash=code_hash,
            )
        except Exception as e:
            return RenderResult(
                success=False, scene_id=scene_id,
                error=f"Cloud Run request failed: {e}",
                code_hash=code_hash,
            )

        if resp.status_code == 429 and attempt < max_429_retries:
            delay = min(10 * (2 ** attempt), 60)
            logger.warning("Renderer %s: got 429, retrying in %ds (attempt %d/%d)",
                           scene_id, delay, attempt + 1, max_429_retries)
            time.sleep(delay)
            continue
        break

    elapsed = time.time() - t0

    if resp.status_code != 200:
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        error_msg = body.get("error", resp.text[:1000])
        traceback_text = body.get("traceback", "")
        stderr = body.get("stderr", "")
        if traceback_text:
            error_msg += f"\n--- traceback ---\n{traceback_text}"
        elif stderr:
            error_msg += f"\n--- stderr ---\n{stderr}"
        logger.error("Renderer %s: Cloud Run error (HTTP %d): %s", scene_id, resp.status_code, error_msg[:2000])
        return RenderResult(
            success=False, scene_id=scene_id,
            error=error_msg, code_hash=code_hash,
        )

    data = resp.json()
    public_url = data.get("public_url")
    gcs_uri = data.get("gcs_uri")

    logger.info(
        "Renderer %s: Cloud Run success in %.1fs, url=%s",
        scene_id, elapsed, public_url,
    )

    return RenderResult(
        success=True,
        scene_id=scene_id,
        public_url=public_url,
        gcs_uri=gcs_uri,
        code_hash=code_hash,
    )


MAX_PARALLEL_RENDERS = int(os.environ.get("MAX_PARALLEL_RENDERS", "6"))


def _render_parallel_cloudrun(
    scene_codes: dict[str, str],
    video_id: str = "",
    quality: str = DEFAULT_QUALITY,
    resolution: tuple[int, int] | None = None,
    frame_height: float | None = None,
) -> dict[str, RenderResult]:
    """Render multiple scenes in parallel, each as a separate Cloud Run request.

    Each scene hits the /render endpoint individually, allowing Cloud Run to
    scale out to separate instances for true parallelism.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    t0 = time.time()

    if not video_id:
        combined = "".join(scene_codes.values())
        video_id = f"batch_{hashlib.sha256(combined.encode()).hexdigest()[:12]}"

    logger.info(
        "Renderer: sending %d scenes to Cloud Run in parallel (max %d concurrent)",
        len(scene_codes), MAX_PARALLEL_RENDERS,
    )

    results: dict[str, RenderResult] = {}

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_RENDERS) as pool:
        futures = {
            pool.submit(
                _render_scene_cloudrun, sid, code,
                video_id=video_id, quality=quality,
                resolution=resolution, frame_height=frame_height,
            ): sid
            for sid, code in scene_codes.items()
        }
        for future in as_completed(futures):
            sid = futures[future]
            try:
                results[sid] = future.result()
            except Exception as e:
                results[sid] = RenderResult(
                    success=False, scene_id=sid,
                    error=f"Render request failed: {e}",
                )

    succeeded = sum(1 for r in results.values() if r.success)
    elapsed = time.time() - t0
    logger.info(
        "Renderer: parallel Cloud Run done in %.1fs — %d/%d succeeded",
        elapsed, succeeded, len(scene_codes),
    )
    return results


# ---------------------------------------------------------------------------
# Public API — dispatches to Cloud Run or local
# ---------------------------------------------------------------------------

def render_scene(
    scene_id: str,
    code: str,
    workspace: Path,
    video_id: str = "",
    quality: str = DEFAULT_QUALITY,
    resolution: tuple[int, int] | None = None,
    frame_height: float | None = None,
) -> RenderResult:
    """Render a single Manim scene.

    Args:
        quality: "l" (low 480p), "m" (medium 720p), "h" (HD 1080p), "k" (4K).
                 Defaults to MANIM_RENDER_QUALITY env var or "l".
        resolution: Custom (width, height) tuple, overrides quality presets.
        frame_height: Manim coordinate frame height for custom aspect ratios.

    Uses Cloud Run by default. Set MANIM_RENDER_BACKEND=local for local rendering.
    """
    if RENDER_BACKEND == "cloudrun":
        return _render_scene_cloudrun(
            scene_id, code, video_id=video_id, quality=quality,
            resolution=resolution, frame_height=frame_height,
        )

    return _render_scene_local(scene_id, code, workspace)


def render_all_scenes(
    scene_codes: dict[str, str],
    workspace: Path,
    video_id: str = "",
    quality: str = DEFAULT_QUALITY,
    resolution: tuple[int, int] | None = None,
    frame_height: float | None = None,
) -> dict[str, RenderResult]:
    """Render all scenes.

    Args:
        quality: "l" (low 480p), "m" (medium 720p), "h" (HD 1080p), "k" (4K).
                 Defaults to MANIM_RENDER_QUALITY env var or "l".
        resolution: Custom (width, height) tuple, overrides quality presets.
        frame_height: Manim coordinate frame height for custom aspect ratios.

    Uses Cloud Run batch endpoint by default.
    """
    if RENDER_BACKEND == "cloudrun":
        return _render_parallel_cloudrun(
            scene_codes, video_id=video_id, quality=quality,
            resolution=resolution, frame_height=frame_height,
        )

    results: dict[str, RenderResult] = {}
    for scene_id, code in scene_codes.items():
        results[scene_id] = _render_scene_local(scene_id, code, workspace)
    succeeded = sum(1 for r in results.values() if r.success)
    logger.info("Renderer: %d/%d scenes rendered successfully", succeeded, len(results))
    return results


# ---------------------------------------------------------------------------
# Local rendering fallback (kept for dev/testing)
# ---------------------------------------------------------------------------

def _find_manim() -> str:
    override = os.environ.get("MANIM_CMD")
    if override:
        return override
    import shutil
    found = shutil.which("manim")
    if found:
        return found
    return "manim"


def _render_scene_local(
    scene_id: str,
    code: str,
    workspace: Path,
) -> RenderResult:
    """Render a single scene locally via manim subprocess."""
    logger.info("Renderer: starting local render %s", scene_id)
    t0 = time.time()

    code_hash = hashlib.sha256(code.encode()).hexdigest()[:16]
    manim_cmd = _find_manim()

    scene_dir = workspace / scene_id
    scene_dir.mkdir(parents=True, exist_ok=True)

    scene_file = scene_dir / f"scene_{scene_id}.py"
    scene_file.write_text(code, encoding="utf-8")

    cmd = [
        manim_cmd, "render",
        f"scene_{scene_id}.py",
        "GeneratedScene",
        "-qh",
        "--media_dir", str(scene_dir / "media"),
        "--disable_caching",
    ]

    env = os.environ.copy()
    ffmpeg_dir = str(Path(FFMPEG_BIN).parent)
    env["PATH"] = ffmpeg_dir + os.pathsep + env.get("PATH", "")

    logger.info("Renderer %s: running %s", scene_id, " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=RENDER_TIMEOUT,
            cwd=str(scene_dir),
            env=env,
        )

        mp4_files = list(scene_dir.rglob("*.mp4"))
        mp4_files = [p for p in mp4_files if p.stat().st_size > 1024]
        if mp4_files:
            mp4_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            mp4_path = mp4_files[0]
        else:
            mp4_path = None

        if mp4_path is None:
            error_msg = result.stderr[-2000:] if result.stderr else "No output video found"
            logger.error("Renderer %s: no output video. Error: %s", scene_id, error_msg[:500])
            return RenderResult(
                success=False, scene_id=scene_id,
                error=error_msg, code_hash=code_hash,
            )

        duration = _get_video_duration(mp4_path)
        elapsed = time.time() - t0
        logger.info(
            "Renderer %s: success in %.1fs, duration=%.1fs, path=%s",
            scene_id, elapsed, duration or 0, mp4_path,
        )

        return RenderResult(
            success=True,
            scene_id=scene_id,
            mp4_path=str(mp4_path),
            duration=duration,
            code_hash=code_hash,
        )

    except subprocess.TimeoutExpired:
        logger.error("Renderer %s: timed out after %ds", scene_id, RENDER_TIMEOUT)
        return RenderResult(
            success=False, scene_id=scene_id,
            error=f"Rendering timed out after {RENDER_TIMEOUT}s",
            code_hash=code_hash,
        )
    except Exception as e:
        logger.error("Renderer %s: unexpected error: %s", scene_id, e)
        return RenderResult(
            success=False, scene_id=scene_id,
            error=str(e), code_hash=code_hash,
        )
