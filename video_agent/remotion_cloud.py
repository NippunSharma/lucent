# DEPRECATED: Not used in browser-first architecture. Kept for reference.
"""Remotion Cloud Run rendering — deploys a site and renders remotely.

Uses the @remotion/cloudrun CLI to deploy the generated Remotion project
to GCS, render it on Cloud Run, and download the resulting MP4.

Each render gets an isolated workspace copy (symlinked node_modules)
so multiple Remotion renders can execute truly in parallel.
"""

import os
import re
import shutil
import subprocess
import threading
import time as _time
from pathlib import Path

from .tools import BASE_OUTPUT_DIR, _update_status

REMOTION_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "remotion_project"
FPS = 30
WIDTH = 1920
HEIGHT = 1080

_render_counter = 0
_counter_lock = threading.Lock()


def _next_render_id() -> int:
    global _render_counter
    with _counter_lock:
        _render_counter += 1
        return _render_counter


def _create_isolated_workspace(render_num: int, template_dir: Path) -> Path:
    """Create a lightweight workspace copy for this render.

    Symlinks node_modules (heavy, read-only) and copies only the
    small files needed for building and deploying.
    """
    workspaces_root = template_dir.parent / "remotion_workspaces"
    workspaces_root.mkdir(parents=True, exist_ok=True)
    work_dir = workspaces_root / f"render_{render_num}"

    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True)

    # Symlink node_modules (Windows uses junction for dirs)
    nm_src = template_dir / "node_modules"
    nm_dst = work_dir / "node_modules"
    if nm_src.exists():
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(nm_dst), str(nm_src)],
                capture_output=True, timeout=10,
            )
        else:
            nm_dst.symlink_to(nm_src)

    # Copy config files
    for fname in ["package.json", "tsconfig.json", ".env",
                   "remotion.config.ts", "remotion.config.mjs"]:
        src = template_dir / fname
        if src.exists():
            shutil.copy2(src, work_dir / fname)

    # Copy src/ directory
    src_template = template_dir / "src"
    if src_template.exists():
        shutil.copytree(src_template, work_dir / "src")
    else:
        (work_dir / "src").mkdir()

    # Copy public/ directory (images etc.)
    pub_template = template_dir / "public"
    if pub_template.exists():
        shutil.copytree(pub_template, work_dir / "public")
    else:
        (work_dir / "public").mkdir()

    return work_dir


def _cleanup_workspace(work_dir: Path):
    """Remove the isolated workspace after render completes."""
    try:
        nm_dst = work_dir / "node_modules"
        if nm_dst.is_junction() if hasattr(nm_dst, "is_junction") else nm_dst.is_symlink():
            if os.name == "nt":
                subprocess.run(["cmd", "/c", "rmdir", str(nm_dst)],
                               capture_output=True, timeout=10)
            else:
                nm_dst.unlink()
        shutil.rmtree(work_dir, ignore_errors=True)
    except Exception:
        pass


def render_remotion_cloudrun(
    video_id: str,
    remotion_code: str,
    duration_in_seconds: float,
) -> dict:
    """Render a Remotion component on Cloud Run using an isolated workspace.

    Each render gets its own copy of the Remotion project so multiple
    renders can execute truly in parallel without filesystem races.
    """
    if "GeneratedComp" not in remotion_code:
        return {
            "status": "error",
            "error_message": "Code must export a named component called 'GeneratedComp'.",
        }

    region = os.environ.get("REMOTION_GCP_REGION", "us-east1")
    service_name = os.environ.get("REMOTION_SERVICE_NAME", "")

    if not service_name:
        return {
            "status": "error",
            "error_message": "REMOTION_SERVICE_NAME not set. Deploy a service first.",
        }

    render_num = _next_render_id()
    print(f"  [REMOTION-CR] #{render_num} starting render for '{video_id}'...")

    work_dir = _create_isolated_workspace(render_num, REMOTION_TEMPLATE_DIR)
    try:
        return _do_render(video_id, remotion_code, duration_in_seconds,
                          region, service_name, render_num, work_dir)
    finally:
        _cleanup_workspace(work_dir)


def _do_render(
    video_id: str,
    remotion_code: str,
    duration_in_seconds: float,
    region: str,
    service_name: str,
    render_num: int,
    work_dir: Path,
) -> dict:
    """Perform the Remotion Cloud Run render in an isolated workspace."""

    _update_status(video_id, "processing", "rendering_remotion_cloudrun")
    project_dir = BASE_OUTPUT_DIR / video_id

    # Write the generated component
    src_dir = work_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    comp_path = src_dir / "GeneratedComp.tsx"
    comp_path.write_text(remotion_code, encoding="utf-8")

    # Generate Root.tsx with correct duration
    duration_in_frames = int(duration_in_seconds * FPS) + FPS
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
    (src_dir / "Root.tsx").write_text(root_code, encoding="utf-8")

    # Copy images from the project's images dir to this workspace's public/
    images_dir = project_dir / "images"
    if images_dir.exists():
        public_dir = work_dir / "public"
        public_dir.mkdir(parents=True, exist_ok=True)
        for img_file in images_dir.iterdir():
            if img_file.is_file():
                shutil.copy2(img_file, public_dir / img_file.name)
        print(f"  [REMOTION-CR] #{render_num} copied images to workspace")

    # Unique site name per render
    sanitized = re.sub(r"[^a-zA-Z0-9-]", "-", video_id)[:40]
    site_name = f"{sanitized}-r{render_num}"[:63]

    # Deploy site to GCS
    print(f"  [REMOTION-CR] #{render_num} deploying site '{site_name}'...")
    deploy_cmd = (
        f"npx remotion cloudrun sites create src/index.ts "
        f"--site-name={site_name} --region={region}"
    )
    deploy_result = subprocess.run(
        deploy_cmd, shell=True, capture_output=True, text=True,
        timeout=300, cwd=str(work_dir), encoding="utf-8", errors="replace",
    )

    if deploy_result.returncode != 0:
        stderr = deploy_result.stderr[-2000:] if deploy_result.stderr else ""
        print(f"  [REMOTION-CR] #{render_num} site deploy failed: {stderr[:200]}")
        return {
            "status": "error",
            "error_message": f"Cloud Run site deploy failed: {stderr}",
        }

    # Extract serve URL from output
    output_text = deploy_result.stdout + deploy_result.stderr
    serve_url = None
    for line in output_text.split("\n"):
        if "storage.googleapis.com" in line or "serveUrl" in line:
            url_match = re.search(r"(https://storage\.googleapis\.com/\S+)", line)
            if url_match:
                serve_url = url_match.group(1)
                break

    if not serve_url:
        serve_url = site_name
    print(f"  [REMOTION-CR] #{render_num} site deployed: {serve_url}")

    # Render on Cloud Run with retry
    output_dir = project_dir / "media" / "videos" / "remotion"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"GeneratedComp_{render_num}.mp4"

    render_cmd = (
        f"npx remotion cloudrun render {site_name} GeneratedComp "
        f"--service-name={service_name} --region={region} "
        f"--codec=h264 --audio-codec=mp3 "
        f'"{output_path}"'
    )

    max_retries = 3
    last_stderr = ""
    last_stdout = ""

    for attempt in range(1, max_retries + 1):
        print(f"  [REMOTION-CR] #{render_num} rendering (attempt {attempt}/{max_retries})...")

        try:
            render_result = subprocess.run(
                render_cmd, shell=True, capture_output=True, text=True,
                timeout=660, cwd=str(work_dir), encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "error",
                "error_message": "Cloud Run rendering timed out after 11 minutes.",
            }

        last_stderr = render_result.stderr[-2000:] if render_result.stderr else ""
        last_stdout = render_result.stdout[-1000:] if render_result.stdout else ""

        if render_result.returncode == 0:
            break

        is_rate_limit = ("Rate exceeded" in last_stderr or "429" in last_stderr
                         or "rate" in last_stderr.lower())
        if is_rate_limit and attempt < max_retries:
            wait = 20 * attempt
            print(f"  [REMOTION-CR] #{render_num} rate limited, waiting {wait}s...")
            _time.sleep(wait)
            continue

        print(f"  [REMOTION-CR] #{render_num} render failed: {last_stderr[:300]}")
        if attempt == max_retries:
            return {
                "status": "error",
                "error_message": "Cloud Run render failed after retries",
                "stderr": last_stderr,
                "stdout": last_stdout,
            }

    if output_path.exists():
        print(f"  [REMOTION-CR] #{render_num} video rendered: {output_path}")
        return {
            "status": "success",
            "video_path": str(output_path),
            "message": "Remotion Cloud Run render completed.",
        }

    # Try to find a GCS URL in the output and download it
    all_output = (last_stdout or "") + (last_stderr or "")
    gcs_match = re.search(r"(https://storage\.googleapis\.com/\S+\.mp4)", all_output)
    if gcs_match:
        gcs_url = gcs_match.group(1)
        print(f"  [REMOTION-CR] #{render_num} downloading from GCS: {gcs_url}")
        try:
            from urllib.request import urlopen as _urlopen
            with _urlopen(gcs_url, timeout=120) as resp:
                with open(output_path, "wb") as f:
                    f.write(resp.read())
        except Exception as dl_err:
            print(f"  [REMOTION-CR] #{render_num} download failed: {dl_err}")

        if output_path.exists() and output_path.stat().st_size > 0:
            return {
                "status": "success",
                "video_path": str(output_path),
                "message": "Remotion Cloud Run render completed (downloaded from GCS).",
            }

    return {
        "status": "error",
        "error_message": "Render finished but output MP4 not found.",
        "stdout": last_stdout,
    }
