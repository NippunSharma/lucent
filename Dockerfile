# Stage 1: Build React frontend
FROM node:20-slim AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --prefer-offline 2>/dev/null || npm install
COPY frontend/ ./
RUN npm run build

# Stage 2: Python runtime
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source
COPY manim_new_service.py .
COPY ws_proxy.py .
COPY manim_agent/ manim_agent/
COPY video_agent/ video_agent/
COPY static/ static/

# Copy golden example source files referenced by the catalog
COPY videos/ videos/
COPY manim/ manim/

# Copy built frontend from stage 1
COPY --from=frontend-build /build/dist frontend/dist/

# Create runtime directories
RUN mkdir -p manim_output manim_uploads manim_workspace manim_cache

ENV PORT=8080
EXPOSE 8080

CMD ["python", "manim_new_service.py"]
