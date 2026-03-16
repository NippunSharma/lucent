# DEPRECATED: Not used in browser-first architecture. Kept for reference.
"""Manim Cloud Run rendering — sends code to the remote Manim service.

Posts manim code to a Cloud Run HTTP API that renders and uploads to GCS.
Downloads the resulting MP4 to the local output directory.

Uses the Remotion service account key (from env vars) to mint an OIDC
identity token for authenticated Cloud Run invocation.
"""

import json
import os
import time as _time
from pathlib import Path
from urllib.request import Request, urlopen

from .tools import BASE_OUTPUT_DIR, _update_status

_cached_token: dict = {"token": None, "expires_at": 0}


def _get_identity_token(audience: str) -> str:
    """Get an OIDC identity token for authenticating to Cloud Run.

    Uses the service account key from REMOTION_GCP_CLIENT_EMAIL /
    REMOTION_GCP_PRIVATE_KEY env vars to mint a short-lived token
    with the correct audience. Falls back to gcloud CLI.
    """
    if _cached_token["token"] and _time.time() < _cached_token["expires_at"]:
        return _cached_token["token"]

    # Method 1: Service account key from env vars (most reliable on local dev)
    client_email = os.environ.get("REMOTION_GCP_CLIENT_EMAIL", "")
    private_key = os.environ.get("REMOTION_GCP_PRIVATE_KEY", "")
    if client_email and private_key:
        try:
            from google.oauth2 import service_account
            import google.auth.transport.requests

            private_key_clean = private_key.replace("\\n", "\n")

            info = {
                "type": "service_account",
                "project_id": os.environ.get("REMOTION_GCP_PROJECT_ID",
                                             os.environ.get("GOOGLE_CLOUD_PROJECT", "")),
                "private_key": private_key_clean,
                "client_email": client_email,
                "token_uri": "https://oauth2.googleapis.com/token",
            }
            creds = service_account.IDTokenCredentials.from_service_account_info(
                info, target_audience=audience,
            )
            auth_req = google.auth.transport.requests.Request()
            creds.refresh(auth_req)
            token = creds.token
            _cached_token["token"] = token
            _cached_token["expires_at"] = _time.time() + 3000
            return token
        except Exception as e:
            print(f"  [MANIM-CR] SA key token failed: {e}")

    # Method 2: gcloud CLI with impersonation
    if client_email:
        try:
            import subprocess
            result = subprocess.run(
                ["gcloud", "auth", "print-identity-token",
                 f"--impersonate-service-account={client_email}",
                 f"--audiences={audience}", "--include-email"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                token = result.stdout.strip()
                _cached_token["token"] = token
                _cached_token["expires_at"] = _time.time() + 3000
                return token
        except Exception:
            pass

    # Method 3: Default credentials (works on GCP)
    try:
        import google.auth.transport.requests
        from google.oauth2 import id_token
        auth_req = google.auth.transport.requests.Request()
        token = id_token.fetch_id_token(auth_req, audience)
        _cached_token["token"] = token
        _cached_token["expires_at"] = _time.time() + 3000
        return token
    except Exception as e:
        print(f"  [MANIM-CR] Default credentials failed: {e}")

    raise RuntimeError("Could not obtain identity token for Cloud Run")


def render_manim_cloudrun(video_id: str, manim_code: str) -> dict:
    """Render a Manim scene on Cloud Run.

    Posts the manim code to the Manim Cloud Run service, which renders it
    and uploads the MP4 to GCS. Downloads the MP4 locally.

    Returns the same {status, video_path} dict format as render_manim_scene.
    """
    cloudrun_url = os.environ.get("MANIM_CLOUDRUN_URL", "")
    if not cloudrun_url:
        return {
            "status": "error",
            "error_message": "MANIM_CLOUDRUN_URL not set.",
        }

    _update_status(video_id, "processing", "rendering_manim_cloudrun")
    print(f"  [MANIM-CR] Sending render request for {video_id}...")

    try:
        id_token_str = _get_identity_token(cloudrun_url)
    except Exception as e:
        return {
            "status": "error",
            "error_message": f"Failed to get identity token: {e}",
        }

    render_url = f"{cloudrun_url.rstrip('/')}/render"
    payload = json.dumps({
        "manim_code": manim_code,
        "video_id": video_id,
        "quality": "h",
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {id_token_str}",
    }

    req = Request(render_url, data=payload, headers=headers, method="POST")

    try:
        with urlopen(req, timeout=600) as resp:
            response_data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        error_msg = str(e)
        if hasattr(e, "read"):
            try:
                error_body = json.loads(e.read().decode("utf-8"))
                error_msg = error_body.get("error", error_msg)
                stderr = error_body.get("stderr", "")
                return {
                    "status": "error",
                    "error_message": f"Cloud Run render failed: {error_msg}",
                    "stderr": stderr,
                }
            except Exception:
                pass
        return {
            "status": "error",
            "error_message": f"Cloud Run request failed: {error_msg}",
        }

    if response_data.get("status") != "success":
        return {
            "status": "error",
            "error_message": response_data.get("error", "Unknown error"),
            "stderr": response_data.get("stderr", ""),
        }

    gcs_uri = response_data.get("gcs_uri", "")
    public_url = response_data.get("public_url", "")
    bucket_name = response_data.get("bucket", "")
    blob_name = response_data.get("blob_name", "")

    project_dir = BASE_OUTPUT_DIR / video_id
    output_dir = project_dir / "media" / "videos" / "scene" / "1080p60"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "GeneratedScene.mp4"

    # Try downloading via GCS client (works even if blob isn't public)
    if bucket_name and blob_name:
        try:
            from google.cloud import storage
            client = storage.Client()
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            blob.download_to_filename(str(output_path))
            print(f"  [MANIM-CR] Downloaded via GCS client: {output_path}")
            return {
                "status": "success",
                "video_path": str(output_path),
                "message": "Manim Cloud Run render completed.",
            }
        except Exception as e:
            print(f"  [MANIM-CR] GCS client download failed: {e}, trying public URL...")

    # Fallback to public URL download
    if public_url:
        try:
            dl_req = Request(public_url)
            with urlopen(dl_req, timeout=120) as resp:
                with open(output_path, "wb") as f:
                    f.write(resp.read())
            if output_path.exists() and output_path.stat().st_size > 0:
                print(f"  [MANIM-CR] Downloaded via public URL: {output_path}")
                return {
                    "status": "success",
                    "video_path": str(output_path),
                    "message": "Manim Cloud Run render completed.",
                }
        except Exception as e:
            print(f"  [MANIM-CR] Public URL download failed: {e}")

    return {
        "status": "error",
        "error_message": "Could not download rendered video from GCS.",
    }
