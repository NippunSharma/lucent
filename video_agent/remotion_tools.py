# DEPRECATED: Not used in browser-first architecture. Kept for reference.
import json
import os
import shutil
import subprocess
from pathlib import Path

from .tools import _update_status

BASE_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
REMOTION_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "remotion_project"

FPS = 30
WIDTH = 1920
HEIGHT = 1080


def render_remotion_comp(video_id: str, remotion_code: str, duration_in_seconds: float) -> dict:
    """Renders Remotion TypeScript/React code into an MP4 video.

    Writes the generated component to the Remotion template project,
    generates the Root.tsx with the correct durationInFrames, and renders
    via the Remotion CLI.

    Args:
        video_id: Unique identifier for this video project.
        remotion_code: Complete TypeScript/React source for a Remotion component.
            The code MUST export a named component called 'GeneratedComp'.
        duration_in_seconds: Total duration of the video in seconds.
            This is used to set durationInFrames in the Remotion composition.

    Returns:
        dict with 'status' and either 'video_path' on success or
        'error_message' and 'stderr' on failure.
    """
    if "GeneratedComp" not in remotion_code:
        return {
            "status": "error",
            "error_message": "Code must export a named component called 'GeneratedComp'.",
        }

    if not REMOTION_TEMPLATE_DIR.exists():
        return {
            "status": "error",
            "error_message": f"Remotion template project not found at {REMOTION_TEMPLATE_DIR}",
        }

    _update_status(video_id, "processing", "rendering_remotion")

    project_dir = BASE_OUTPUT_DIR / video_id
    work_dir = project_dir / "remotion"

    # Copy template (exclude node_modules for speed)
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    shutil.copytree(
        REMOTION_TEMPLATE_DIR, work_dir,
        ignore=shutil.ignore_patterns("node_modules"),
    )

    # Create a directory junction for node_modules (Windows) or symlink (Unix)
    nm_target = REMOTION_TEMPLATE_DIR / "node_modules"
    nm_junction = work_dir / "node_modules"
    if nm_target.exists() and not nm_junction.exists():
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(nm_junction), str(nm_target)],
                capture_output=True, timeout=10,
            )
        else:
            os.symlink(str(nm_target), str(nm_junction), target_is_directory=True)

    # Copy any generated images to public/ so Remotion can access via staticFile()
    images_dir = project_dir / "images"
    if images_dir.exists():
        public_dir = work_dir / "public"
        public_dir.mkdir(parents=True, exist_ok=True)
        for img_file in images_dir.iterdir():
            if img_file.is_file():
                shutil.copy2(img_file, public_dir / img_file.name)
        print(f"  [REMOTION] Copied {sum(1 for _ in images_dir.iterdir())} images to public/")

    src_dir = work_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    # Write the LLM-generated component
    comp_path = src_dir / "GeneratedComp.tsx"
    with open(comp_path, "w", encoding="utf-8") as f:
        f.write(remotion_code)

    # Generate Root.tsx with the correct duration
    duration_in_frames = int(duration_in_seconds * FPS) + FPS  # +1s safety buffer
    root_code = (
        "import {Composition} from 'remotion';\n"
        "import {GeneratedComp} from './GeneratedComp';\n"
        "\n"
        "export const Root: React.FC = () => {\n"
        "\treturn (\n"
        "\t\t<>\n"
        "\t\t\t<Composition\n"
        '\t\t\t\tid="GeneratedComp"\n'
        "\t\t\t\tcomponent={GeneratedComp}\n"
        f"\t\t\t\tdurationInFrames={{{duration_in_frames}}}\n"
        f"\t\t\t\twidth={{{WIDTH}}}\n"
        f"\t\t\t\theight={{{HEIGHT}}}\n"
        f"\t\t\t\tfps={{{FPS}}}\n"
        "\t\t\t\tdefaultProps={{}}\n"
        "\t\t\t/>\n"
        "\t\t</>\n"
        "\t);\n"
        "};\n"
    )
    root_path = src_dir / "Root.tsx"
    with open(root_path, "w", encoding="utf-8") as f:
        f.write(root_code)

    # Render via Remotion CLI
    output_dir = project_dir / "media" / "videos" / "remotion"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "GeneratedComp.mp4"

    render_cmd = (
        f'npx remotion render src/index.ts GeneratedComp "{output_path}"'
    )
    print(f"  [REMOTION] Running: {render_cmd}")

    try:
        result = subprocess.run(
            render_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(work_dir),
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "error_message": "Remotion rendering timed out after 10 minutes.",
        }

    if result.returncode != 0:
        stderr_tail = result.stderr[-3000:] if result.stderr else ""
        stdout_tail = result.stdout[-1000:] if result.stdout else ""
        return {
            "status": "error",
            "error_message": "Remotion rendering failed.",
            "stderr": stderr_tail,
            "stdout": stdout_tail,
            "suggestion": (
                "Fix the TypeScript/React code and call render_remotion_comp again. "
                "Common issues: syntax errors, missing imports, invalid Remotion API usage. "
                "Check stderr for details."
            ),
        }

    if not output_path.exists():
        return {
            "status": "error",
            "error_message": "Rendering completed but no MP4 file was found.",
            "stdout": result.stdout[-1000:],
        }

    print(f"  [REMOTION] Video rendered: {output_path}")

    return {
        "status": "success",
        "video_path": str(output_path),
        "message": "Remotion component rendered successfully.",
    }


def stitch_video_segments(video_id: str, segments_json: str) -> dict:
    """Concatenates multiple rendered video segments into a single video file.

    Normalizes all segments to the same resolution and frame rate before
    concatenating. Used for hybrid videos that combine Manim and Remotion clips.

    Args:
        video_id: Unique identifier for this video project.
        segments_json: JSON array string of segment objects IN ORDER.
            Each object must have a 'video_path' field pointing to a rendered MP4.
            Example: [{"video_path": "/path/to/clip1.mp4"}, {"video_path": "/path/to/clip2.mp4"}]

    Returns:
        dict with 'status' and 'video_path' of the combined video on success,
        or 'error_message' on failure.
    """
    try:
        segments = json.loads(segments_json)
    except json.JSONDecodeError as e:
        return {"status": "error", "error_message": f"Invalid JSON: {e}"}

    if not segments:
        return {"status": "error", "error_message": "No segments provided."}

    if len(segments) == 1:
        return {
            "status": "success",
            "video_path": segments[0]["video_path"],
            "message": "Only one segment — no stitching needed.",
        }

    _update_status(video_id, "processing", "stitching_segments")

    project_dir = BASE_OUTPUT_DIR / video_id
    stitch_dir = project_dir / "stitch"
    stitch_dir.mkdir(parents=True, exist_ok=True)

    # Re-encode all segments to a common resolution/fps for clean concatenation
    normalized_paths = []
    for i, seg in enumerate(segments):
        src_path = seg["video_path"]
        if not os.path.exists(src_path):
            return {
                "status": "error",
                "error_message": f"Segment video not found: {src_path}",
            }

        norm_path = str(stitch_dir / f"segment_{i:03d}.mp4")
        cmd = [
            "ffmpeg", "-y",
            "-i", src_path,
            "-vf",
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2",
            "-r", str(FPS),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-an",
            norm_path,
        ]

        print(f"  [STITCH] Normalizing segment {i}: {src_path}")
        norm_result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if norm_result.returncode != 0:
            return {
                "status": "error",
                "error_message": (
                    f"Failed to normalize segment {i}: "
                    f"{norm_result.stderr[-500:]}"
                ),
            }
        normalized_paths.append(norm_path)

    # Write FFmpeg concat list
    concat_list = stitch_dir / "concat.txt"
    with open(concat_list, "w") as f:
        for p in normalized_paths:
            escaped = p.replace("\\", "/").replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    # Concatenate segments
    output_dir = project_dir / "media" / "videos" / "stitched"
    output_dir.mkdir(parents=True, exist_ok=True)
    combined_path = str(output_dir / "combined.mp4")

    concat_cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        combined_path,
    ]

    print(f"  [STITCH] Concatenating {len(segments)} segments...")
    concat_result = subprocess.run(
        concat_cmd, capture_output=True, text=True, timeout=300
    )
    if concat_result.returncode != 0:
        return {
            "status": "error",
            "error_message": (
                f"Failed to concatenate segments: "
                f"{concat_result.stderr[-500:]}"
            ),
        }

    print(f"  [STITCH] Combined {len(segments)} segments: {combined_path}")

    return {
        "status": "success",
        "video_path": combined_path,
        "message": f"Stitched {len(segments)} segments into one video.",
    }


def render_remotion(video_id: str, remotion_code: str, duration_in_seconds: float) -> dict:
    """Render Remotion code, choosing Cloud Run or local based on env var.

    Checks REMOTION_RENDER_BACKEND:
      - "cloudrun": tries Cloud Run first, falls back to local on failure
        (unless DISABLE_LOCAL_RENDERING=true)
      - "local" (default): renders locally via npx remotion render
    """
    backend = os.environ.get("REMOTION_RENDER_BACKEND", "local").lower()
    local_disabled = os.environ.get("DISABLE_LOCAL_RENDERING", "").lower() == "true"

    if backend == "cloudrun":
        print(f"  [REMOTION] Using Cloud Run backend")
        try:
            from .remotion_cloud import render_remotion_cloudrun
            result = render_remotion_cloudrun(video_id, remotion_code, duration_in_seconds)
            if result.get("status") == "success":
                return result
            err = result.get("error_message", "")[:200]
            if local_disabled:
                print(f"  [REMOTION] Cloud Run failed (local fallback disabled): {err}")
                return result
            print(f"  [REMOTION] Cloud Run failed, falling back to local: {err}")
        except Exception as e:
            if local_disabled:
                print(f"  [REMOTION] Cloud Run exception (local fallback disabled): {e}")
                return {"status": "error", "error_message": f"Cloud Run failed: {e}"}
            print(f"  [REMOTION] Cloud Run exception, falling back to local: {e}")

    if local_disabled and backend == "cloudrun":
        return {"status": "error", "error_message": "Cloud Run failed and local rendering is disabled."}

    print(f"  [REMOTION] Using local backend")
    return render_remotion_comp(video_id, remotion_code, duration_in_seconds)
