# Lucent — AI Tutor with Real-Time Voice & Generated Animated Videos

**Lucent** is an AI-powered tutor that generates 3Blue1Brown-style animated educational videos on any topic, in real time, through a voice-first conversational interface powered by Gemini Live.

Ask anything. Watch it come alive.

---

## What It Does

1. **Talk naturally** — Lucent uses Gemini Live API for real-time voice conversation. Ask it to teach you anything.
2. **Generates animated videos** — The system plans scenes, writes ManimGL code, renders on Cloud Run, generates narration via Gemini TTS, and stitches everything into a polished video — all automatically.
3. **Interactive learning** — Pause the video at any point to ask a question. Lucent sees the current frame (via screenshots) and answers in context, then resumes.
4. **Multiple formats** — YouTube landscape, YouTube Shorts, TikTok/Reels, Instagram posts, or quick doubt-clearers.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Google Cloud Run: lucent-app                    │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────┐ │
│  │  React SPA   │  │   FastAPI    │  │  WS Proxy         │ │
│  │  /lucent/    │  │  /generate   │  │  /ws → Gemini     │ │
│  │              │  │  /status     │  │  Live API         │ │
│  └──────────────┘  │  /videos     │  └───────────────────┘ │
│                    └──────┬───────┘                         │
│                           │                                 │
└───────────────────────────┼─────────────────────────────────┘
                            │
              ┌─────────────┼─────────────────┐
              │             │                 │
              ▼             ▼                 ▼
     ┌────────────┐  ┌────────────┐  ┌──────────────┐
     │ Gemini API │  │ Cloud Run: │  │  Gemini TTS  │
     │ (Planner,  │  │ manimgl-  │  │  (Narration)  │
     │  Codegen)  │  │ renderer   │  │              │
     └────────────┘  └────────────┘  └──────────────┘
```

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Voice interface | **Gemini Live API** (`gemini-live-2.5-flash-native-audio`) |
| Scene planning & code generation | **Gemini API** via Vertex AI (`gemini-3-flash-preview`) |
| Animation rendering | **ManimGL** (3b1b version) on **Cloud Run** |
| Narration | **Gemini TTS** |
| Video stitching | **FFmpeg** |
| Backend | **FastAPI** (Python) |
| Frontend | **React 19** + Vite + Tailwind CSS |
| Hosting | **Google Cloud Run** |

## Google Cloud Services Used

- **Cloud Run** — Hosts the main app (`lucent-app`) and the ManimGL renderer (`manimgl-renderer`)
- **Vertex AI** — Gemini API for planning, code generation, TTS, and live conversation
- **Cloud Build** — Container image builds (via `deploy.sh`)
- **Container Registry** — Docker image storage

---

## Getting Started (From Scratch)

### Prerequisites

- **Python 3.11+**
- **Node.js 20+** and npm
- **FFmpeg** installed and on PATH ([download](https://ffmpeg.org/download.html))
- **Google Cloud SDK** (`gcloud`) installed and authenticated ([install](https://cloud.google.com/sdk/docs/install))
- A **Google Cloud project** with the following APIs enabled:
  - Vertex AI API
  - Cloud Run API
  - Cloud Build API

### 1. Clone the Repository

```bash
git clone https://github.com/NippunSharma/lucent.git
cd lucent
```

### 2. Set Up the ManimGL Renderer on Cloud Run

The ManimGL renderer runs as a separate Cloud Run service. You need to deploy it first.

```bash
# Build and deploy the ManimGL renderer
cd manimgl_service
gcloud builds submit --tag gcr.io/YOUR_PROJECT_ID/manimgl-renderer
gcloud run deploy manimgl-renderer \
  --image gcr.io/YOUR_PROJECT_ID/manimgl-renderer \
  --region us-east1 \
  --memory 8Gi --cpu 4 \
  --timeout 540 \
  --no-allow-unauthenticated \
  --min-instances 0 --max-instances 8
cd ..
```

Note the service URL (e.g., `https://manimgl-renderer-XXXX.us-east1.run.app`).

> **Note:** The `manimgl_service/` directory is excluded from the main repo's `.gitignore` for cleanliness but is available in the full project. See [Deploying the ManimGL Renderer](#deploying-the-manimgl-renderer) for its Dockerfile.

### 3. Configure Environment Variables

```bash
cp video_agent/.env.example video_agent/.env
```

Edit `video_agent/.env`:

```env
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
GOOGLE_CLOUD_LOCATION=global
MANIM_RENDER_BACKEND=cloudrun
MANIMGL_CLOUDRUN_URL=https://your-manimgl-renderer-url.run.app
```

### 4. Install Python Dependencies

```bash
pip install -r requirements.txt
```

### 5. Install Frontend Dependencies

```bash
cd frontend
npm install
npm run build
cd ..
```

### 6. Authenticate with Google Cloud

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project your-gcp-project-id
```

The service account used must have:
- **Vertex AI User** role (for Gemini API)
- **Cloud Run Invoker** role (to call the manimgl-renderer)

### 7. Run Locally

**Option A — Production-like (single process):**

```bash
python manim_new_service.py
```

Opens on `http://localhost:9000`. The app serves the frontend at `/lucent/` and the about-me page at `/`.

You still need the standalone WS proxy for local dev (the embedded one uses the same port):

```bash
# In a separate terminal
python ws_proxy.py
```

**Option B — Development (with hot reload):**

```bash
# Terminal 1: Backend API
python manim_new_service.py

# Terminal 2: WebSocket proxy
python ws_proxy.py

# Terminal 3: Frontend dev server (hot reload)
cd frontend && npm run dev
```

Frontend dev server runs at `http://localhost:5174` with hot reload. It auto-connects to the backend on port 9000 and WS proxy on port 8082.

---

## Cloud Deployment

### Automated Deployment

```bash
bash deploy.sh
```

This script:
1. Builds a multi-stage Docker image (frontend + backend in one container)
2. Pushes to Google Container Registry via Cloud Build
3. Deploys to Cloud Run with correct environment variables
4. Prints the service URL

### Manual Deployment

```bash
# Build
gcloud builds submit --tag gcr.io/YOUR_PROJECT_ID/lucent-app

# Deploy
gcloud run deploy lucent-app \
  --image gcr.io/YOUR_PROJECT_ID/lucent-app \
  --region us-east1 \
  --memory 4Gi --cpu 2 \
  --timeout 900 \
  --allow-unauthenticated \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID,GOOGLE_GENAI_USE_VERTEXAI=TRUE,MANIM_RENDER_BACKEND=cloudrun"
```

### Custom Domain Mapping

```bash
gcloud run domain-mappings create \
  --service lucent-app \
  --domain your-domain.com \
  --region us-east1
```

Then add the DNS records (CNAME to `ghs.googlehosted.com`) at your domain registrar.

### Environment Variables (Cloud Run)

| Variable | Description | Default |
|----------|-------------|---------|
| `PORT` | Server port (set by Cloud Run) | `9000` (local), `8080` (Cloud Run) |
| `GOOGLE_CLOUD_PROJECT` | GCP project ID | — |
| `GOOGLE_GENAI_USE_VERTEXAI` | Use Vertex AI for Gemini | `TRUE` |
| `GOOGLE_CLOUD_LOCATION` | Vertex AI location | `global` |
| `MANIM_RENDER_BACKEND` | `cloudrun` or `local` | `cloudrun` |
| `MANIMGL_CLOUDRUN_URL` | ManimGL renderer URL | Built-in default |
| `WS_DEBUG` | WebSocket proxy debug logging | `1` |

---

## Project Structure

```
├── manim_new_service.py       # FastAPI backend (API + WS proxy + static serving)
├── ws_proxy.py                # Standalone WS proxy (local dev only)
├── manim_agent/               # Video generation pipeline
│   ├── pipeline.py            # Orchestrator (parallel per-scene pipeline)
│   ├── planner.py             # Scene planning via Gemini
│   ├── code_generator.py      # ManimGL code generation via Gemini
│   ├── renderer.py            # Cloud Run rendering client
│   ├── tts.py                 # Gemini TTS narration generation
│   ├── stitcher.py            # FFmpeg video/audio stitching
│   ├── context_processor.py   # PDF/URL/image context extraction
│   ├── golden_examples.py     # 3b1b example catalog for codegen
│   ├── prompt_templates.py    # LLM system prompts
│   └── example_catalog.json   # Golden example index
├── frontend/                  # React SPA (Vite + Tailwind)
│   ├── src/
│   │   ├── App.jsx            # Main app with Gemini Live integration
│   │   ├── utils/gemini-api.js    # Gemini Live WebSocket client
│   │   ├── utils/video-api.js     # Video generation API client
│   │   └── components/custom/     # UI components
│   ├── package.json
│   └── vite.config.js
├── videos/                    # 3b1b ManimGL scene source (golden examples)
├── manim/                     # ManimGL library source (example scenes)
├── video_agent/
│   └── .env.example           # Environment variable template
├── static/                    # About-me homepage
│   └── index.html
├── Dockerfile                 # Multi-stage build (Node + Python)
├── deploy.sh                  # Automated Cloud Run deployment
├── requirements.txt           # Python dependencies
└── README.md
```

---

## Hackathon Categories

This project targets both hackathon categories:

### Live Agents

Real-time voice interaction via **Gemini Live API**. Students talk naturally, can interrupt mid-sentence, pause videos to ask contextual questions (with screenshot-based visual understanding), and get immediate spoken answers.

### Creative Storyteller

The system generates rich **animated educational videos** from scratch — combining scene planning, ManimGL code generation, mathematical visualizations, narration, and structured storytelling into a cohesive multimodal output. Supports multiple video formats (landscape, portrait, square) tailored for different platforms.

---

## License

MIT
