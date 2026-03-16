const IS_DEV = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
const VIDEO_SERVER = IS_DEV ? 'http://localhost:9000' : '';

/**
 * Start video generation. Supports optional file uploads and URL list.
 * If files are provided, uses multipart form. Otherwise uses JSON.
 */
export async function submitGeneration(topic, {
  preset = 'youtube_explainer',
  quality = 'l',
  files = [],
  urls = [],
} = {}) {
  if (files.length > 0) {
    const form = new FormData();
    form.append('topic', topic);
    form.append('preset', preset);
    form.append('quality', quality);
    form.append('urls', urls.join(','));
    for (const f of files) form.append('files', f);
    const resp = await fetch(`${VIDEO_SERVER}/generate`, { method: 'POST', body: form });
    if (!resp.ok) throw new Error(`Generate failed: HTTP ${resp.status}`);
    return resp.json();
  }

  const resp = await fetch(`${VIDEO_SERVER}/generate/json`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ topic, preset, quality, urls }),
  });
  if (!resp.ok) throw new Error(`Generate failed: HTTP ${resp.status}`);
  return resp.json();
}

export async function pollStatus(videoId) {
  const resp = await fetch(`${VIDEO_SERVER}/status/${videoId}`);
  if (!resp.ok) throw new Error(`Status failed: HTTP ${resp.status}`);
  return resp.json();
}

export async function fetchMetadata(videoId) {
  const resp = await fetch(`${VIDEO_SERVER}/videos/${videoId}/metadata`);
  if (!resp.ok) throw new Error(`Metadata failed: HTTP ${resp.status}`);
  return resp.json();
}

export async function fetchVideoBlob(videoId) {
  const resp = await fetch(`${VIDEO_SERVER}/videos/${videoId}/video`);
  if (!resp.ok) throw new Error(`Video download failed: HTTP ${resp.status}`);
  const blob = await resp.blob();
  return URL.createObjectURL(blob);
}

export async function fetchScreenshotBlob(videoId, filename) {
  const resp = await fetch(`${VIDEO_SERVER}/videos/${videoId}/screenshots/${filename}`);
  if (!resp.ok) throw new Error(`Screenshot failed: HTTP ${resp.status}`);
  const blob = await resp.blob();
  return URL.createObjectURL(blob);
}

export async function fetchScreenshotBase64(videoId, filename) {
  const resp = await fetch(`${VIDEO_SERVER}/videos/${videoId}/screenshots/${filename}`);
  if (!resp.ok) return null;
  const blob = await resp.blob();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onloadend = () => resolve(reader.result.split(',')[1]);
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
}

/**
 * Non-blocking video generation. Kicks off generation and returns immediately
 * with { videoId, pollHandle }. The caller can poll or use the handle to wait.
 */
export function startGeneration(topic, { preset, quality, files, urls, onProgress, signal } = {}) {
  const state = { videoId: null, done: false, error: null, metadata: null, videoBlobUrl: null };

  const promise = (async () => {
    const { video_id } = await submitGeneration(topic, { preset, quality, files, urls });
    state.videoId = video_id;

    let status = 'processing';
    while (status !== 'completed') {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      await new Promise((r) => setTimeout(r, 3000));
      const result = await pollStatus(video_id);
      status = result.status;
      onProgress?.(result);
      if (status === 'error') throw new Error(result.error || 'Generation failed');
    }

    const [metadata, videoBlobUrl] = await Promise.all([
      fetchMetadata(video_id),
      fetchVideoBlob(video_id),
    ]);

    state.metadata = metadata;
    state.videoBlobUrl = videoBlobUrl;
    state.done = true;
    return { videoId: video_id, metadata, videoBlobUrl };
  })();

  return { state, promise };
}

/**
 * Blocking generate (original interface). Waits until video is ready.
 */
export async function generateVideo(topic, opts = {}) {
  const { promise } = startGeneration(topic, opts);
  return promise;
}

export async function fetchPresets() {
  const resp = await fetch(`${VIDEO_SERVER}/presets`);
  if (!resp.ok) throw new Error(`Presets failed: HTTP ${resp.status}`);
  return resp.json();
}
