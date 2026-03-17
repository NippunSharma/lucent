"""FastAPI server for the Lucent Educational Video Generation Service.

Serves:
 - API endpoints for video generation
 - WebSocket proxy to Gemini Live API (replaces standalone ws_proxy.py)
 - React SPA at /lucent/
 - Static about-me page at /
"""

import asyncio
import json
import logging
import os
import re
import shutil
import ssl
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import certifi
import google.auth
import google.auth.transport.requests
import websockets as ws_lib
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

load_dotenv(Path(__file__).parent / "video_agent" / ".env")

from google.cloud import storage as gcs_storage  # noqa: E402

from manim_agent.pipeline import (  # noqa: E402
    BASE_OUTPUT_DIR,
    VIDEO_PRESETS,
    edit_pipeline,
    run_pipeline,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("manim_service")

UPLOAD_DIR = Path(__file__).parent / "manim_uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
BASE_OUTPUT_DIR.mkdir(exist_ok=True)

GCS_ARTIFACT_BUCKET = os.environ.get("GCS_ARTIFACT_BUCKET", "manim-renders-gemini-devpost")
GCS_ARTIFACT_PREFIX = "artifacts"

sse_queues: dict[str, list[asyncio.Queue]] = {}
active_jobs: dict[str, asyncio.Task] = {}


# ---------------------------------------------------------------------------
# GCS helpers — persist final video artifacts so they survive container restarts
# ---------------------------------------------------------------------------

def _gcs_bucket() -> gcs_storage.Bucket:
    client = gcs_storage.Client()
    return client.bucket(GCS_ARTIFACT_BUCKET)


def _upload_artifacts_to_gcs(video_id: str) -> None:
    """Upload final video, metadata, screenshots, status to GCS."""
    output_dir = BASE_OUTPUT_DIR / video_id
    if not output_dir.exists():
        return
    try:
        bucket = _gcs_bucket()
        for fpath in output_dir.rglob("*"):
            if not fpath.is_file():
                continue
            rel = fpath.relative_to(BASE_OUTPUT_DIR)
            blob_name = f"{GCS_ARTIFACT_PREFIX}/{rel.as_posix()}"
            content_type = "application/json"
            if fpath.suffix == ".mp4":
                content_type = "video/mp4"
            elif fpath.suffix == ".jpg":
                content_type = "image/jpeg"
            elif fpath.suffix == ".png":
                content_type = "image/png"
            blob = bucket.blob(blob_name)
            blob.upload_from_filename(str(fpath), content_type=content_type)
        logger.info("Uploaded artifacts for %s to GCS", video_id)
    except Exception as e:
        logger.warning("Failed to upload artifacts for %s to GCS: %s", video_id, e)


def _restore_artifacts_from_gcs(video_id: str) -> bool:
    """Download artifacts from GCS if they're missing locally. Returns True if restored."""
    output_dir = BASE_OUTPUT_DIR / video_id
    if (output_dir / "status.json").exists():
        return True
    try:
        bucket = _gcs_bucket()
        prefix = f"{GCS_ARTIFACT_PREFIX}/{video_id}/"
        blobs = list(bucket.list_blobs(prefix=prefix))
        if not blobs:
            return False
        for blob in blobs:
            rel = blob.name[len(f"{GCS_ARTIFACT_PREFIX}/"):]
            local_path = BASE_OUTPUT_DIR / rel
            local_path.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(str(local_path))
        logger.info("Restored artifacts for %s from GCS (%d files)", video_id, len(blobs))
        return True
    except Exception as e:
        logger.warning("Failed to restore artifacts for %s from GCS: %s", video_id, e)
        return False


def _broadcast_sse(video_id: str, data: dict) -> None:
    """Push status to all SSE subscribers for a video."""
    queues = sse_queues.get(video_id, [])
    for q in queues:
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            pass


FIXED_STEP_PROGRESS = {
    "queued": 0,
    "processing_context": 5,
    "planning_scenes": 12,
    "generating_audio": 20,
    "generating_scenes": 25,
    "stitching": 90,
    "completed": 100,
    "editing_code": 30,
    "failed": 0,
}

SCENE_PHASE_START = 25
SCENE_PHASE_END = 88

_video_scene_tracker: dict[str, dict] = {}


def _update_status(video_id: str, step: str, **extra) -> None:
    """Write status to disk and broadcast via SSE.

    Computes smooth progress for per-scene sub-steps (generating_code_*,
    rendering_*) based on the total number of scenes.
    """
    progress = extra.pop("progress", None)
    label = step

    scenes_total = extra.pop("scenes_total", None)
    if scenes_total is not None and step == "generating_scenes":
        scene_ids = extra.pop("scene_ids", [])
        _video_scene_tracker[video_id] = {
            "total": scenes_total,
            "order": {sid: i for i, sid in enumerate(scene_ids)},
            "done_count": 0,
            "sub_step_count": 0,
        }

    if progress is None:
        if step in FIXED_STEP_PROGRESS:
            progress = FIXED_STEP_PROGRESS[step]
        elif step.startswith("generating_code_") or step.startswith("rendering_") or step.startswith("fixing_"):
            tracker = _video_scene_tracker.get(video_id)
            if tracker:
                total = tracker["total"]
                sub_steps_per_scene = 2  # codegen + render
                total_sub_steps = total * sub_steps_per_scene

                tracker["sub_step_count"] = tracker.get("sub_step_count", 0) + 1
                done = tracker["sub_step_count"]

                frac = min(done / max(total_sub_steps, 1), 1.0)
                progress = int(SCENE_PHASE_START + frac * (SCENE_PHASE_END - SCENE_PHASE_START))
            else:
                progress = SCENE_PHASE_START
        else:
            progress = 0

    if step.startswith("generating_code_") or step.startswith("rendering_") or step.startswith("fixing_"):
        label = "generating_scenes"

    scenes_done = extra.pop("scenes_done", None)
    if scenes_done is not None and video_id in _video_scene_tracker:
        _video_scene_tracker[video_id]["done_count"] = scenes_done

    status_data = {
        "status": "processing" if step not in ("completed", "failed") else step,
        "step": label,
        "progress": progress,
        "timestamp": time.time(),
        **extra,
    }
    if step == "completed":
        status_data["status"] = "completed"
        _video_scene_tracker.pop(video_id, None)
    elif step == "failed":
        status_data["status"] = "error"
        _video_scene_tracker.pop(video_id, None)

    status_path = BASE_OUTPUT_DIR / video_id / "status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(status_data, indent=2), encoding="utf-8")

    _broadcast_sse(video_id, status_data)


def _validate_video_id(video_id: str) -> None:
    if not re.match(r"^[a-zA-Z0-9_-]+$", video_id):
        raise HTTPException(status_code=400, detail="Invalid video ID")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Lucent service starting on port %s", os.environ.get("PORT", "9000"))
    yield
    logger.info("Lucent service shutting down")
    for task in active_jobs.values():
        task.cancel()


app = FastAPI(
    title="Manim Educational Video Generator",
    description="Generate 3Blue1Brown-style educational videos from topics, PDFs, and notes",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if BASE_OUTPUT_DIR.exists():
    app.mount(
        "/manim_output",
        StaticFiles(directory=str(BASE_OUTPUT_DIR)),
        name="manim_output",
    )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class GenerateResponse(BaseModel):
    video_id: str
    status: str
    message: str


class GenerateJsonRequest(BaseModel):
    topic: str
    preset: str = "youtube_explainer"
    quality: str = "l"
    urls: list[str] = []


class EditRequest(BaseModel):
    prompt: str
    quality: str = "l"


# ---------------------------------------------------------------------------
# Pipeline runner (runs in background)
# ---------------------------------------------------------------------------

async def _run_pipeline_bg(
    video_id: str,
    topic: str,
    files: list[Path],
    urls: list[str],
    quality: str = "l",
    preset: str = "youtube_explainer",
) -> None:
    """Run pipeline in background, bridging sync pipeline to async status updates."""
    loop = asyncio.get_event_loop()

    def status_callback(step: str, **kwargs) -> None:
        try:
            loop.call_soon_threadsafe(lambda: _update_status(video_id, step, **kwargs))
        except RuntimeError:
            pass

    try:
        result = await loop.run_in_executor(
            None,
            lambda: run_pipeline(
                video_id=video_id,
                topic=topic,
                files=files if files else None,
                urls=urls if urls else None,
                on_status=status_callback,
                quality=quality,
                preset_name=preset,
            ),
        )

        if result.success:
            _update_status(video_id, "completed", video_path=result.video_path)
            _upload_artifacts_to_gcs(video_id)
        else:
            _update_status(video_id, "failed", error=result.error)

    except Exception as e:
        logger.error("Pipeline %s failed: %s", video_id, e)
        _update_status(video_id, "failed", error=str(e))


async def _run_edit_bg(
    video_id: str,
    scene_id: str,
    edit_prompt: str,
    quality: str = "l",
    preset: str = "youtube_explainer",
) -> None:
    """Run edit pipeline in background."""
    loop = asyncio.get_event_loop()

    def status_callback(step: str, **kwargs) -> None:
        try:
            loop.call_soon_threadsafe(lambda: _update_status(video_id, step, **kwargs))
        except RuntimeError:
            pass

    try:
        result = await loop.run_in_executor(
            None,
            lambda: edit_pipeline(
                video_id=video_id,
                scene_id=scene_id,
                edit_prompt=edit_prompt,
                on_status=status_callback,
                quality=quality,
                preset_name=preset,
            ),
        )

        if result.success:
            _update_status(video_id, "completed", video_path=result.video_path)
            _upload_artifacts_to_gcs(video_id)
        else:
            _update_status(video_id, "failed", error=result.error)

    except Exception as e:
        logger.error("Edit pipeline %s/%s failed: %s", video_id, scene_id, e)
        _update_status(video_id, "failed", error=str(e))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/generate", response_model=GenerateResponse)
async def generate_video(
    topic: str = Form(...),
    subject: str = Form("general"),
    files: list[UploadFile] = File(default=[]),
    urls: str = Form(default=""),
    quality: str = Form(default="l"),
    preset: str = Form(default="youtube_explainer"),
):
    """Start video generation.

    Accepts topic, optional subject, uploaded files (PDFs/images), URLs,
    quality ("l" low 480p, "m" medium 720p, "h" HD 1080p, "k" 4K),
    and preset (e.g. "youtube_deep_dive", "youtube_explainer", "youtube_short",
    "tiktok", "instagram_post").
    """
    if quality not in ("l", "m", "h", "k"):
        quality = "l"
    if preset not in VIDEO_PRESETS:
        preset = "youtube_explainer"

    video_id = f"manim_{uuid.uuid4().hex[:12]}"

    video_upload_dir = UPLOAD_DIR / video_id
    video_upload_dir.mkdir(parents=True, exist_ok=True)

    saved_files: list[Path] = []
    for upload in files:
        if upload.filename:
            dest = video_upload_dir / upload.filename
            content = await upload.read()
            dest.write_bytes(content)
            saved_files.append(dest)

    url_list = [u.strip() for u in urls.split(",") if u.strip()] if urls else []

    _update_status(video_id, "queued", topic=topic, subject=subject, preset=preset)

    task = asyncio.create_task(
        _run_pipeline_bg(video_id, topic, saved_files, url_list, quality=quality, preset=preset)
    )
    active_jobs[video_id] = task
    task.add_done_callback(lambda _t, vid=video_id: active_jobs.pop(vid, None))

    return GenerateResponse(
        video_id=video_id,
        status="processing",
        message=f"Video generation started for: '{topic}'",
    )


@app.post("/generate/json")
async def generate_video_json(body: GenerateJsonRequest):
    """Start video generation from a JSON body (used by Gemini Live frontend).

    Accepts { topic, preset?, quality? } as JSON. Returns { video_id }.
    """
    quality = body.quality if body.quality in ("l", "m", "h", "k") else "l"
    preset = body.preset if body.preset in VIDEO_PRESETS else "youtube_explainer"

    url_list = [u.strip() for u in body.urls if u.strip()] if body.urls else []

    video_id = f"manim_{uuid.uuid4().hex[:12]}"
    _update_status(video_id, "queued", topic=body.topic, preset=preset)

    task = asyncio.create_task(
        _run_pipeline_bg(video_id, body.topic, [], url_list, quality=quality, preset=preset)
    )
    active_jobs[video_id] = task
    task.add_done_callback(lambda _t, vid=video_id: active_jobs.pop(vid, None))

    return {"video_id": video_id}


PRESET_DESCRIPTIONS: dict[str, str] = {
    "youtube_deep_dive": "Long-form, detailed educational video — thorough explanations, "
                         "step-by-step builds, 3-6 minutes.",
    "youtube_explainer": "Focused explainer — clear narrative arc with visual intuition, 2-4 minutes.",
    "youtube_short": "YouTube Short — fast-paced vertical video, punchy hook + one insight, 30-50 seconds.",
    "tiktok": "TikTok / Instagram Reel — maximum energy, instant hook, rapid visuals, 15-45 seconds.",
    "instagram_post": "Instagram / LinkedIn post — clean, polished square video, 30-90 seconds.",
    "doubt_clearer": "Quick doubt clearer — single scene, one specific concept, 10-15 seconds.",
}


@app.get("/presets")
async def list_presets():
    """List available video presets with descriptions for the frontend."""
    return {
        key: {
            "name": p.name,
            "width": p.width,
            "height": p.height,
            "aspect_ratio": p.aspect_ratio,
            "description": PRESET_DESCRIPTIONS.get(key, ""),
        }
        for key, p in VIDEO_PRESETS.items()
    }


@app.get("/generate/stream/{video_id}")
async def stream_progress(video_id: str, request: Request):
    """SSE endpoint for real-time generation progress."""
    _validate_video_id(video_id)
    status_path = BASE_OUTPUT_DIR / video_id / "status.json"
    if not status_path.exists():
        raise HTTPException(status_code=404, detail="Video ID not found")

    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    if video_id not in sse_queues:
        sse_queues[video_id] = []
    sse_queues[video_id].append(queue)

    async def event_generator():
        try:
            if status_path.exists():
                current = json.loads(status_path.read_text(encoding="utf-8"))
                yield {"event": "status", "data": json.dumps(current)}

            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield {"event": "status", "data": json.dumps(data)}
                    if data.get("status") in ("completed", "error"):
                        yield {"event": "done", "data": json.dumps(data)}
                        break
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            if video_id in sse_queues:
                try:
                    sse_queues[video_id].remove(queue)
                except ValueError:
                    pass
                if not sse_queues.get(video_id):
                    sse_queues.pop(video_id, None)

    return EventSourceResponse(event_generator())


@app.get("/status/{video_id}")
async def get_status(video_id: str):
    """Poll status of a video generation job."""
    _validate_video_id(video_id)
    status_path = BASE_OUTPUT_DIR / video_id / "status.json"
    if not status_path.exists():
        if _restore_artifacts_from_gcs(video_id) and status_path.exists():
            pass
        else:
            raise HTTPException(status_code=404, detail="Video ID not found")
    return json.loads(status_path.read_text(encoding="utf-8"))


@app.get("/videos/{video_id}/composition")
async def get_composition(video_id: str):
    """Get scene metadata and reference index for a video."""
    _validate_video_id(video_id)
    comp_path = BASE_OUTPUT_DIR / video_id / "composition.json"
    if not comp_path.exists():
        _restore_artifacts_from_gcs(video_id)
    if not comp_path.exists():
        raise HTTPException(status_code=404, detail="Composition not found")
    return json.loads(comp_path.read_text(encoding="utf-8"))


@app.get("/videos/{video_id}/metadata")
async def get_metadata(video_id: str):
    """Get video metadata in the format expected by the Gemini Live frontend.

    Returns { videoId, title, videoFile, duration, sections[], screenshots[] }.
    """
    _validate_video_id(video_id)
    _restore_artifacts_from_gcs(video_id)
    meta_path = BASE_OUTPUT_DIR / video_id / "metadata.json"
    if meta_path.exists():
        return json.loads(meta_path.read_text(encoding="utf-8"))
    comp_path = BASE_OUTPUT_DIR / video_id / "composition.json"
    if not comp_path.exists():
        raise HTTPException(status_code=404, detail="Metadata not found")
    return json.loads(comp_path.read_text(encoding="utf-8"))


@app.get("/videos/{video_id}/video")
async def get_video(video_id: str):
    """Serve the final MP4 video."""
    _validate_video_id(video_id)
    video_path = BASE_OUTPUT_DIR / video_id / "video.mp4"
    if not video_path.exists():
        _restore_artifacts_from_gcs(video_id)
    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video not found")
    return FileResponse(
        str(video_path),
        media_type="video/mp4",
        headers={"Accept-Ranges": "bytes"},
    )


@app.get("/videos/{video_id}/screenshots/{filename}")
async def get_screenshot(video_id: str, filename: str):
    """Serve a screenshot image from the video's screenshots directory."""
    _validate_video_id(video_id)
    if not re.match(r"^[\d.]+\.jpg$", filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    ss_path = BASE_OUTPUT_DIR / video_id / "screenshots" / filename
    if not ss_path.exists():
        _restore_artifacts_from_gcs(video_id)
    if not ss_path.exists():
        raise HTTPException(status_code=404, detail="Screenshot not found")
    return FileResponse(str(ss_path), media_type="image/jpeg")


@app.get("/videos/{video_id}/scenes/{scene_id}/video")
async def get_scene_video(video_id: str, scene_id: str):
    """Serve an individual scene's video clip."""
    _validate_video_id(video_id)
    temp_dir = BASE_OUTPUT_DIR / video_id / "temp_stitch"
    scene_path = temp_dir / f"{scene_id}_merged.mp4"
    if not scene_path.exists():
        raise HTTPException(status_code=404, detail="Scene video not found")
    return FileResponse(str(scene_path), media_type="video/mp4")


@app.get("/videos/{video_id}/scenes/{scene_id}/code")
async def get_scene_code(video_id: str, scene_id: str):
    """Get the generated Manim code for a scene."""
    _validate_video_id(video_id)
    code_path = BASE_OUTPUT_DIR / video_id / "scene_code" / f"{scene_id}.py"
    if not code_path.exists():
        raise HTTPException(status_code=404, detail="Scene code not found")
    return {"scene_id": scene_id, "code": code_path.read_text(encoding="utf-8")}


@app.post("/videos/{video_id}/scenes/{scene_id}/edit")
async def edit_scene(video_id: str, scene_id: str, body: EditRequest):
    """Edit a specific scene and re-generate."""
    _validate_video_id(video_id)

    output_dir = BASE_OUTPUT_DIR / video_id
    if not output_dir.exists():
        raise HTTPException(status_code=404, detail="Video not found")

    _update_status(video_id, "editing", scene_id=scene_id)

    task = asyncio.create_task(
        _run_edit_bg(video_id, scene_id, body.prompt, quality=body.quality)
    )
    active_jobs[f"{video_id}_edit"] = task
    task.add_done_callback(lambda _t, key=f"{video_id}_edit": active_jobs.pop(key, None))

    return {"status": "editing", "video_id": video_id, "scene_id": scene_id}


@app.get("/videos")
async def list_videos():
    """List all generated videos."""
    videos = []
    if BASE_OUTPUT_DIR.exists():
        for d in sorted(BASE_OUTPUT_DIR.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            entry = {"video_id": d.name, "status": "unknown"}

            status_path = d / "status.json"
            if status_path.exists():
                try:
                    status = json.loads(status_path.read_text(encoding="utf-8"))
                    entry["status"] = status.get("status", "unknown")
                    entry["step"] = status.get("step", "")
                except json.JSONDecodeError:
                    pass

            comp_path = d / "composition.json"
            if comp_path.exists():
                try:
                    comp = json.loads(comp_path.read_text(encoding="utf-8"))
                    entry["title"] = comp.get("title", "")
                    entry["total_duration"] = comp.get("total_duration", 0)
                    entry["total_scenes"] = comp.get("total_scenes", 0)
                except json.JSONDecodeError:
                    pass

            entry["has_video"] = (d / "video.mp4").exists()
            videos.append(entry)

    return {"videos": videos}


@app.get("/videos/{video_id}/plan")
async def get_plan(video_id: str):
    """Get the scene plan for a video."""
    _validate_video_id(video_id)
    plan_path = BASE_OUTPUT_DIR / video_id / "plan.json"
    if not plan_path.exists():
        raise HTTPException(status_code=404, detail="Plan not found")
    return json.loads(plan_path.read_text(encoding="utf-8"))


@app.get("/videos/{video_id}/context")
async def get_context(video_id: str):
    """Get the processed context for a video."""
    _validate_video_id(video_id)
    ctx_path = BASE_OUTPUT_DIR / video_id / "context.json"
    if not ctx_path.exists():
        raise HTTPException(status_code=404, detail="Context not found")
    return json.loads(ctx_path.read_text(encoding="utf-8"))


@app.get("/health")
async def health_check():
    """Check service health including dependencies."""
    checks = {"service": "ok", "manimgl": "unknown", "ffmpeg": "unknown"}

    import subprocess

    try:
        result = subprocess.run(
            ["manimgl", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        checks["manimgl"] = "ok" if result.returncode == 0 else f"error: {result.stderr[:100]}"
    except FileNotFoundError:
        checks["manimgl"] = "not found"
    except Exception as e:
        checks["manimgl"] = f"error: {e}"

    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True, text=True, timeout=5,
        )
        checks["ffmpeg"] = "ok" if result.returncode == 0 else f"error: {result.stderr[:100]}"
    except FileNotFoundError:
        checks["ffmpeg"] = "not found"
    except Exception as e:
        checks["ffmpeg"] = f"error: {e}"

    vertex_ok = bool(os.environ.get("GOOGLE_CLOUD_PROJECT"))
    checks["vertex_ai"] = "ok" if vertex_ok else "GOOGLE_CLOUD_PROJECT not set"

    all_ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content=checks,
    )


# ---------------------------------------------------------------------------
# WebSocket proxy to Gemini Live API (embedded, replaces ws_proxy.py)
# ---------------------------------------------------------------------------

WS_DEBUG = os.environ.get("WS_DEBUG", "1") == "1"
ws_logger = logging.getLogger("ws_proxy")


def _generate_access_token() -> str | None:
    try:
        creds, _ = google.auth.default()
        if not creds.valid:
            creds.refresh(google.auth.transport.requests.Request())
        return creds.token
    except Exception as e:
        ws_logger.error("Failed to generate access token: %s", e)
        return None


async def _ws_relay(source, dest, label: str):
    """Relay messages between two WebSocket connections."""
    try:
        async for message in source:
            try:
                data = json.loads(message)
                if WS_DEBUG:
                    summary = _ws_summarize(data)
                    if summary:
                        ws_logger.info("[%s] %s", label, summary)
                if isinstance(dest, WebSocket):
                    await dest.send_text(json.dumps(data))
                else:
                    await dest.send(json.dumps(data))
            except Exception as e:
                ws_logger.error("Relay %s error: %s", label, e)
    except Exception:
        pass


def _ws_summarize(data: dict) -> str | None:
    if "serverContent" in data:
        sc = data["serverContent"]
        if sc.get("turnComplete"):
            return "TURN_COMPLETE"
        if sc.get("interrupted"):
            return "INTERRUPTED"
        if sc.get("inputTranscription"):
            return f"INPUT: {sc['inputTranscription'].get('text', '')[:80]}"
        if sc.get("outputTranscription"):
            return f"OUTPUT: {sc['outputTranscription'].get('text', '')[:80]}"
        if sc.get("modelTurn"):
            parts = sc["modelTurn"].get("parts", [])
            if parts and parts[0].get("inlineData"):
                return "AUDIO"
            if parts and parts[0].get("text"):
                return f"TEXT: {parts[0]['text'][:80]}"
        return f"serverContent: {list(sc.keys())}"
    if "setupComplete" in data:
        return "SETUP_COMPLETE"
    if "toolCall" in data:
        names = [c.get("name", "?") for c in data["toolCall"].get("functionCalls", [])]
        return f"TOOL_CALL: {names}"
    if "setup" in data:
        return "SETUP"
    if "client_content" in data:
        turns = data["client_content"].get("turns", [])
        if turns:
            parts = turns[0].get("parts", [])
            if parts and parts[0].get("text"):
                return f"CLIENT: {parts[0]['text'][:80]}"
            if len(parts) > 1:
                return f"CLIENT: text+{len(parts)-1} parts"
        return "CLIENT (empty)"
    if "tool_response" in data:
        return "TOOL_RESPONSE"
    if "realtime_input" in data:
        return None
    if "service_url" in data:
        return "SERVICE_URL"
    return str(list(data.keys()))


@app.websocket("/ws")
async def gemini_ws_proxy(client_ws: WebSocket):
    """Proxy WebSocket connections to Gemini Live API."""
    await client_ws.accept()
    ws_logger.info("New Gemini WS client connected")

    try:
        raw = await asyncio.wait_for(client_ws.receive_text(), timeout=10.0)
        init = json.loads(raw)
        bearer_token = init.get("bearer_token")
        service_url = init.get("service_url")

        if not bearer_token:
            ws_logger.info("Generating access token...")
            bearer_token = _generate_access_token()
            if not bearer_token:
                await client_ws.close(1008, "Auth failed")
                return

        if not service_url:
            await client_ws.close(1008, "No service_url")
            return

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {bearer_token}",
        }
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())

        ws_logger.info("Connecting to Gemini API...")
        async with ws_lib.connect(
            service_url, additional_headers=headers, ssl=ssl_ctx
        ) as server_ws:
            ws_logger.info("Connected to Gemini API")

            async def client_to_server():
                try:
                    while True:
                        msg = await client_ws.receive_text()
                        data = json.loads(msg)
                        if WS_DEBUG:
                            s = _ws_summarize(data)
                            if s:
                                ws_logger.info("[C->S] %s", s)
                        await server_ws.send(json.dumps(data))
                except Exception:
                    pass

            s2c = asyncio.create_task(_ws_relay(server_ws, client_ws, "S->C"))
            c2s = asyncio.create_task(client_to_server())

            done, pending = await asyncio.wait(
                [c2s, s2c], return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()

    except asyncio.TimeoutError:
        await client_ws.close(1008, "Timeout")
    except Exception as e:
        ws_logger.error("WS proxy error: %s", e)
        try:
            await client_ws.close(1011, "Internal error")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Static file serving — SPA at /lucent/, about-me at /
# Must be mounted LAST (after all API routes).
# ---------------------------------------------------------------------------

_APP_DIR = Path(__file__).parent
_FRONTEND_DIST = _APP_DIR / "frontend" / "dist"
_STATIC_DIR = _APP_DIR / "static"


@app.get("/lucent")
@app.get("/lucent/{rest_of_path:path}")
async def serve_spa(rest_of_path: str = ""):
    """Serve the React SPA. Returns index.html for any unmatched path (client-side routing)."""
    if rest_of_path:
        file_path = (_FRONTEND_DIST / rest_of_path).resolve()
        if file_path.is_relative_to(_FRONTEND_DIST) and file_path.is_file():
            return FileResponse(str(file_path))
    index = _FRONTEND_DIST / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="Frontend not built")


if _STATIC_DIR.exists():
    @app.get("/")
    async def serve_homepage():
        """Serve the about-me homepage."""
        index = _STATIC_DIR / "index.html"
        if index.exists():
            return HTMLResponse(index.read_text(encoding="utf-8"))
        raise HTTPException(status_code=404, detail="Homepage not found")

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="homepage_static")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "9000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
