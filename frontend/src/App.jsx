import React, { useState, useRef, useCallback, useEffect } from 'react';
import { GeminiLiveAPI, EventType } from './utils/gemini-api';
import { AudioStreamer, AudioPlayer } from './utils/media-utils';
import { startGeneration, fetchScreenshotBase64, fetchPresets } from './utils/video-api';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { ScrollArea } from '@/components/ui/scroll-area';
import AudioVisualizer from '@/components/custom/AudioVisualizer';
import SectionProgress from '@/components/custom/SectionProgress';
import VideoGenerating from '@/components/custom/VideoGenerating';
import PresetChooser from '@/components/custom/PresetChooser';
import ResourceUpload from '@/components/custom/ResourceUpload';
import {
  Mic,
  MicOff,
  PhoneOff,
  Sparkles,
  Pause,
  Loader2,
} from 'lucide-react';

const APP_NAME = 'Lucent';

const INITIAL_SYSTEM_INSTRUCTIONS = `You are Lucent — a friendly, enthusiastic tutor who teaches using custom-generated animated videos with built-in voice-over narration.

You can generate a brand-new animated video on ANY topic the student wants to learn about.

THE VIDEO GENERATION FLOW:
When a student asks to learn something, follow this flow:

1. FIRST ask the student what video style they want. Call show_preset_chooser to display a visual style picker. Wait for their choice. If the context makes it obvious (e.g. they said "make a quick TikTok about X"), you can skip this and call play_video directly with the right preset.

2. THEN ask: "Do you have any reference materials you'd like me to include?" If yes, call show_resource_upload. If no, proceed to step 3.

3. Call play_video ONCE with the topic and the chosen preset. The system will handle the rest.

CRITICAL RULES:

1. ONLY CALL play_video ONCE per request. Never call it twice for the same topic. After calling play_video, the system is generating the video. Do NOT call play_video again.

2. AFTER calling play_video: Chat naturally with the student while the video generates in the background (~1-2 minutes). Do NOT say the video is ready. Do NOT say "here's the video". You do NOT know when it will be ready — the system will notify you.

3. SYSTEM MESSAGES: You will receive system messages in square brackets like [VIDEO READY], [SECTION ENDED], [VIDEO ENDED], [PRESET CHOSEN], [RESOURCES UPLOADED], [RESOURCES SKIPPED]. These are sent BY THE SYSTEM, not by you. NEVER output these bracket messages yourself. NEVER say "[VIDEO READY]" or anything similar.

4. WHEN YOU RECEIVE [VIDEO READY]: The video is about to auto-play with its own narration. Stop speaking immediately. Do NOT talk over the video.

5. WHEN YOU RECEIVE [SECTION ENDED] or [VIDEO ENDED]: Briefly acknowledge and wait for the student. Do NOT call any tools automatically.

6. REPLAYING SECTIONS: After receiving [VIDEO READY], you get section IDs. Use jump_to_section to replay specific sections. Only use play_video for a NEW topic.

7. Be warm, encouraging, and conversational. Make the student excited to learn.

AVAILABLE PRESETS:
- youtube_deep_dive: Long detailed explanation (3-6 min)
- youtube_explainer: Focused explainer (2-4 min)
- youtube_short: YouTube Short, punchy vertical (30-50s)
- tiktok: TikTok/Reel, max energy (15-45s)
- instagram_post: Square social post (30-90s)
- doubt_clearer: Single-scene doubt answer (10-15s)`;

const TOOLS = [
  {
    name: 'play_video',
    description: 'Generate and play a brand-new animated educational video. Call this after the student has chosen a preset (or skip if context is clear). The video generates in the background — you can keep chatting.',
    parameters: {
      type: 'object',
      properties: {
        topic: { type: 'string', description: 'The topic to generate a video about.' },
        preset: {
          type: 'string',
          description: 'Video preset. One of: youtube_deep_dive, youtube_explainer, youtube_short, tiktok, instagram_post, doubt_clearer. Defaults to youtube_explainer.',
          enum: ['youtube_deep_dive', 'youtube_explainer', 'youtube_short', 'tiktok', 'instagram_post', 'doubt_clearer'],
        },
      },
      required: ['topic'],
    },
  },
  {
    name: 'show_preset_chooser',
    description: 'Display the video style picker so the student can choose a format (YouTube, TikTok, Short, etc). Use this when the student asks to learn something and hasn\'t specified a format.',
    parameters: { type: 'object', properties: {} },
  },
  {
    name: 'show_resource_upload',
    description: 'Display the resource upload UI so the student can add PDFs, handwritten notes, images, or reference URLs before generating the video.',
    parameters: { type: 'object', properties: {} },
  },
  {
    name: 'resume_video',
    description: 'Resume the video from where it was paused.',
    parameters: { type: 'object', properties: {} },
  },
  {
    name: 'jump_to_section',
    description: 'Jump to a specific section of the video and start playing from there.',
    parameters: {
      type: 'object',
      properties: { section_id: { type: 'string', description: 'Section ID to jump to.' } },
      required: ['section_id'],
    },
  },
];

const VS = { IDLE: 0, PLAYING: 1, PAUSED_FOR_DOUBT: 2, ENDED: 3 };

const VIEW = {
  LANDING: 'landing',
  CONNECTED: 'connected',
  CHOOSING_PRESET: 'choosing_preset',
  UPLOADING_RESOURCES: 'uploading_resources',
  GENERATING: 'generating',
  VIDEO: 'video',
};

export default function App() {
  const [connected, setConnected] = useState(false);
  const [micOn, setMicOn] = useState(false);
  const [videoVisible, setVideoVisible] = useState(false);
  const [logs, setLogs] = useState([]);
  const [metadata, setMetadata] = useState(null);
  const [currentView, setCurrentView] = useState(VIEW.LANDING);
  const [videoTime, setVideoTime] = useState(0);
  const [analyserNode, setAnalyserNode] = useState(null);
  const [agentState, setAgentState] = useState('idle');
  const [videoBlobUrl, setVideoBlobUrl] = useState(null);
  const [genStep, setGenStep] = useState('queued');
  const [genProgress, setGenProgress] = useState(0);
  const [genError, setGenError] = useState(null);
  const [presets, setPresets] = useState(null);
  const [isGenerating, setIsGenerating] = useState(false);
  const isGeneratingRef = useRef(false);

  const videoRef = useRef(null);
  const geminiRef = useRef(null);
  const audioPlayerRef = useRef(null);
  const streamerRef = useRef(null);
  const metadataRef = useRef(null);
  const videoVisibleRef = useRef(false);
  const currentVideoIdRef = useRef(null);

  const videoState = useRef(VS.IDLE);
  const pendingAction = useRef(null);
  const contextSent = useRef(false);
  const toolResponsePending = useRef(false);
  const autoResumeTimer = useRef(null);
  const timeUpdateInterval = useRef(null);
  const sectionEndTime = useRef(null);
  const genAbortRef = useRef(null);

  const pendingTopicRef = useRef(null);
  const chosenPresetRef = useRef('youtube_explainer');
  const uploadedResourcesRef = useRef({ files: [], urls: [] });
  const pendingVideoResultRef = useRef(null);

  useEffect(() => { isGeneratingRef.current = isGenerating; }, [isGenerating]);

  const addLog = useCallback((msg) => {
    setLogs((prev) => [...prev.slice(-100), { ts: new Date().toLocaleTimeString(), msg }]);
  }, []);

  const showVideo = useCallback((visible) => {
    videoVisibleRef.current = visible;
    setVideoVisible(visible);
  }, []);

  useEffect(() => {
    fetchPresets().then(setPresets).catch(() => {});
  }, []);

  useEffect(() => {
    if (currentView === VIEW.VIDEO) {
      timeUpdateInterval.current = setInterval(() => {
        const v = videoRef.current;
        if (v) setVideoTime(v.currentTime);
      }, 250);
    }
    return () => {
      if (timeUpdateInterval.current) clearInterval(timeUpdateInterval.current);
    };
  }, [currentView]);

  function getCurrentSection(time) {
    const m = metadataRef.current;
    if (!m) return null;
    for (let i = m.sections.length - 1; i >= 0; i--) {
      if (time >= m.sections[i].startTime) return m.sections[i];
    }
    return m.sections[0];
  }

  function getCoveredSections(time) {
    const m = metadataRef.current;
    if (!m) return [];
    return m.sections.filter((s) => s.startTime <= time);
  }

  function getSectionScreenshots(section) {
    const m = metadataRef.current;
    if (!m?.screenshots || !section) return [];
    return m.screenshots.filter(
      (ss) => ss.time >= section.startTime && ss.time <= section.endTime,
    );
  }

  async function sendContextToGemini(currentTime) {
    if (contextSent.current) return;
    contextSent.current = true;

    const section = getCurrentSection(currentTime);
    const covered = getCoveredSections(currentTime);
    const screenshots = getSectionScreenshots(section);
    const videoId = currentVideoIdRef.current;

    console.log('[APP] sendContextToGemini:', {
      currentTime,
      section: section?.id,
      screenshotCount: screenshots.length,
      videoId,
    });

    const textPart = {
      text: `[VIDEO PAUSED at ${currentTime.toFixed(1)}s]
Current section: "${section?.title}" (${section?.id})
Section time range: ${section?.startTime}s – ${section?.endTime}s
Narration for this section: "${section?.narration || ''}"
Sections covered so far: ${covered.map((s) => `"${s.title}" (${s.id})`).join(', ')}

The student paused the video to ask a question. I am providing you ${screenshots.length} screenshots from this section for visual context. Use them along with the section narration to answer the student's question accurately. After answering, ask if they want to continue watching.`,
    };

    const parts = [textPart];
    let loadedCount = 0;
    if (videoId) {
      for (const ss of screenshots) {
        try {
          const filename = ss.file.replace('screenshots/', '');
          console.log('[APP] Fetching screenshot:', filename, 'for video:', videoId);
          const base64 = await fetchScreenshotBase64(videoId, filename);
          if (base64) {
            parts.push({ inline_data: { mime_type: 'image/jpeg', data: base64 } });
            loadedCount++;
          } else {
            console.warn('[APP] Screenshot returned null:', filename);
          }
        } catch (e) {
          console.warn('[APP] Failed to load screenshot:', ss.file, e);
        }
      }
    }

    console.log(`[APP] Sending context to Gemini: ${parts.length} parts (1 text + ${loadedCount} screenshots)`);
    addLog(`Sent ${loadedCount} screenshot(s) for context`);
    geminiRef.current?.sendClientContentWithParts(parts);
  }

  function pauseVideoElement() {
    const v = videoRef.current;
    if (v && !v.paused) v.pause();
  }

  function clearAutoResume() {
    if (autoResumeTimer.current) {
      clearTimeout(autoResumeTimer.current);
      autoResumeTimer.current = null;
    }
  }

  function resetAllVideoState() {
    clearAutoResume();
    pauseVideoElement();
    videoState.current = VS.IDLE;
    contextSent.current = false;
    pendingAction.current = null;
    toolResponsePending.current = false;
    sectionEndTime.current = null;
    showVideo(false);
    setVideoTime(0);

    if (genAbortRef.current) {
      genAbortRef.current.abort();
      genAbortRef.current = null;
    }

    if (videoBlobUrl) {
      URL.revokeObjectURL(videoBlobUrl);
    }
    setVideoBlobUrl(null);
    setMetadata(null);
    metadataRef.current = null;
    currentVideoIdRef.current = null;
    setGenStep('queued');
    setGenProgress(0);
    setGenError(null);
    setIsGenerating(false);
    pendingVideoResultRef.current = null;
  }

  function presentVideoResult(result) {
    currentVideoIdRef.current = result.videoId;
    metadataRef.current = result.metadata;
    setMetadata(result.metadata);
    setVideoBlobUrl(result.videoBlobUrl);

    const gemini = geminiRef.current;
    if (gemini) {
      const meta = result.metadata;
      const sectionList = meta.sections
        .map((s) => `  - ${s.id} (${s.startTime}s–${s.endTime}s): ${s.title}\n    "${s.narration}"`)
        .join('\n');
      gemini.sendClientContent(
        `[VIDEO READY] The lesson "${meta.title}" has been generated.
Duration: ${meta.duration}s

SECTIONS:
${sectionList}

IMPORTANT: The video is about to start playing with its own narration. Do NOT speak. Stay completely silent until the student pauses or the video ends.
You now have access to resume_video and jump_to_section tools.`,
      );
    }

    pendingAction.current = { type: 'play' };
    showVideo(true);
    setCurrentView(VIEW.VIDEO);
    setIsGenerating(false);
    addLog('Lesson ready — playing now');
  }

  function startVideoAction(action) {
    if (!geminiRef.current) return;
    console.log('[APP] startVideoAction:', action.type);
    audioPlayerRef.current?.interrupt();
    clearAutoResume();
    contextSent.current = false;
    setAgentState('idle');

    if (!videoVisibleRef.current) {
      pendingAction.current = action;
      showVideo(true);
      setCurrentView(VIEW.VIDEO);
      return;
    }

    const v = videoRef.current;
    if (!v) {
      pendingAction.current = action;
      setTimeout(() => startVideoAction(action), 50);
      return;
    }

    switch (action.type) {
      case 'play':
        sectionEndTime.current = null;
        v.currentTime = 0;
        v.play().catch(console.error);
        videoState.current = VS.PLAYING;
        addLog('Lesson started');
        break;
      case 'jump':
        sectionEndTime.current = action.endTime ?? null;
        v.currentTime = action.time;
        v.play().catch(console.error);
        videoState.current = VS.PLAYING;
        addLog(`Jumped to ${action.sectionTitle}`);
        break;
      case 'resume':
        sectionEndTime.current = null;
        v.play().catch(console.error);
        videoState.current = VS.PLAYING;
        addLog('Lesson resumed');
        break;
    }
  }

  useEffect(() => {
    if (!videoVisible) return;
    const action = pendingAction.current;
    if (!action) return;

    const tryExecute = () => {
      const v = videoRef.current;
      if (!v) { setTimeout(tryExecute, 50); return; }
      pendingAction.current = null;
      contextSent.current = false;

      switch (action.type) {
        case 'play':
          sectionEndTime.current = null;
          v.currentTime = 0;
          v.play().catch(console.error);
          videoState.current = VS.PLAYING;
          addLog('Lesson started');
          break;
        case 'jump':
          sectionEndTime.current = action.endTime ?? null;
          v.currentTime = action.time;
          v.play().catch(console.error);
          videoState.current = VS.PLAYING;
          addLog(`Jumped to ${action.sectionTitle}`);
          break;
        case 'resume':
          sectionEndTime.current = null;
          v.play().catch(console.error);
          videoState.current = VS.PLAYING;
          addLog('Lesson resumed');
          break;
      }
    };

    setTimeout(tryExecute, 50);
  }, [videoVisible, addLog]);

  async function handlePlayVideo(topic, preset) {
    if (isGenerating) {
      console.log('[APP] handlePlayVideo: already generating, ignoring duplicate call');
      return;
    }

    if (genAbortRef.current) {
      genAbortRef.current.abort();
      genAbortRef.current = null;
    }

    clearAutoResume();
    pauseVideoElement();
    videoState.current = VS.IDLE;
    contextSent.current = false;
    pendingAction.current = null;
    sectionEndTime.current = null;
    showVideo(false);
    setVideoTime(0);

    if (videoBlobUrl) {
      URL.revokeObjectURL(videoBlobUrl);
      setVideoBlobUrl(null);
    }
    setMetadata(null);
    metadataRef.current = null;
    currentVideoIdRef.current = null;

    setGenStep('queued');
    setGenProgress(0);
    setGenError(null);
    setIsGenerating(true);

    const abortController = new AbortController();
    genAbortRef.current = abortController;

    const resources = uploadedResourcesRef.current;
    uploadedResourcesRef.current = { files: [], urls: [] };

    try {
      addLog(`Generating lesson: ${topic}`);

      const { promise } = startGeneration(topic, {
        preset: preset || chosenPresetRef.current,
        signal: abortController.signal,
        files: resources.files,
        urls: resources.urls,
        onProgress: (status) => {
          setGenStep(status.step || 'queued');
          if (status.progress != null) setGenProgress(status.progress);
        },
      });

      const result = await promise;
      if (abortController.signal.aborted) return;

      if (agentState === 'speaking') {
        pendingVideoResultRef.current = result;
        addLog('Video ready — waiting for tutor to finish speaking...');
      } else {
        presentVideoResult(result);
      }
    } catch (e) {
      if (e.name === 'AbortError') return;
      console.error('[APP] Generation failed:', e);
      setGenError(e.message);
      setIsGenerating(false);
      addLog(`Generation failed: ${e.message}`);
      geminiRef.current?.sendClientContent(
        `[VIDEO GENERATION FAILED] Error: ${e.message}. Let the student know and offer to try again.`,
      );
    } finally {
      genAbortRef.current = null;
    }
  }

  function handlePresetSelected(presetKey) {
    chosenPresetRef.current = presetKey;
    setCurrentView(VIEW.CONNECTED);
    addLog(`Style chosen: ${presetKey}`);

    geminiRef.current?.sendClientContent(
      `[PRESET CHOSEN] The student selected the "${presetKey}" video style. Now ask if they have any reference materials (PDFs, notes, links) they'd like to include, or proceed directly to generating the video.`,
    );
  }

  function handleResourcesSubmitted({ files, urls }) {
    uploadedResourcesRef.current = { files, urls };
    setCurrentView(VIEW.CONNECTED);
    const count = files.length + urls.length;
    addLog(`${count} resource(s) added`);

    geminiRef.current?.sendClientContent(
      `[RESOURCES UPLOADED] The student uploaded ${files.length} file(s) and ${urls.length} URL(s). These will be included when generating the video. Now call play_video to start generating.`,
    );
  }

  function handleResourcesSkipped() {
    uploadedResourcesRef.current = { files: [], urls: [] };
    setCurrentView(VIEW.CONNECTED);

    geminiRef.current?.sendClientContent(
      `[RESOURCES SKIPPED] The student chose not to upload any resources. Proceed to call play_video to generate the video.`,
    );
  }

  const handleConnect = useCallback(async () => {
    try {
      const ap = new AudioPlayer();
      await ap.init();
      audioPlayerRef.current = ap;
      setAnalyserNode(ap.analyser);

      const gemini = new GeminiLiveAPI();
      gemini.systemInstructions = INITIAL_SYSTEM_INSTRUCTIONS;
      gemini.tools = TOOLS;
      gemini.voiceName = 'Puck';
      geminiRef.current = gemini;

      gemini.onConnectionStarted = async () => {
        if (!audioPlayerRef.current) {
          const newAp = new AudioPlayer();
          await newAp.init();
          audioPlayerRef.current = newAp;
          setAnalyserNode(newAp.analyser);
        }
        setConnected(true);
        if (currentView === VIEW.LANDING) setCurrentView(VIEW.CONNECTED);
        addLog('Connected');
      };

      gemini.onClose = (event) => {
        const wasReconnectFailure = event?.reason === 'max_reconnect_attempts';
        const generating = isGeneratingRef.current;
        streamerRef.current?.stop();
        streamerRef.current = null;
        audioPlayerRef.current?.destroy();
        audioPlayerRef.current = null;
        geminiRef.current = null;
        if (!generating) resetAllVideoState();
        setConnected(false);
        setMicOn(false);
        if (!generating) setCurrentView(VIEW.LANDING);
        setAnalyserNode(null);
        setAgentState('idle');
        addLog(wasReconnectFailure ? 'Connection lost — please reconnect' : 'Disconnected');
      };

      gemini.onReconnecting = (attempt, max) => {
        setConnected(false);
        setAgentState('idle');
        addLog(`Reconnecting (${attempt}/${max})...`);
      };

      gemini.onError = (e) => {
        addLog('Connection error');
        console.error('[APP] WS error:', e);
      };

      gemini.onEvent = (evt) => {
        switch (evt.type) {
          case EventType.SETUP_COMPLETE:
            addLog('Ready');
            if (!streamerRef.current) {
              const s = new AudioStreamer((base64) => geminiRef.current?.sendAudio(base64));
              s.start().then(() => {
                streamerRef.current = s;
                setMicOn(true);
              });
            }
            break;

          case EventType.AUDIO: {
            setAgentState('speaking');
            const raw = atob(evt.data);
            const buf = new ArrayBuffer(raw.length);
            const view = new Uint8Array(buf);
            for (let i = 0; i < raw.length; i++) view[i] = raw.charCodeAt(i);
            audioPlayerRef.current?.play(buf);
            break;
          }

          case EventType.INTERRUPTED: {
            audioPlayerRef.current?.interrupt();
            setAgentState('listening');
            pendingAction.current = null;
            toolResponsePending.current = false;

            if (videoState.current === VS.PLAYING) {
              pauseVideoElement();
              addLog('Paused — listening');
              clearAutoResume();
              autoResumeTimer.current = setTimeout(() => {
                autoResumeTimer.current = null;
                const v = videoRef.current;
                if (v && v.paused && videoState.current !== VS.PAUSED_FOR_DOUBT && videoState.current !== VS.IDLE && videoState.current !== VS.ENDED) {
                  v.play().catch(console.error);
                  videoState.current = VS.PLAYING;
                  addLog('Resumed');
                }
              }, 3000);
            }
            break;
          }

          case EventType.INPUT_TRANSCRIPTION: {
            const text = evt.data.text || '';
            addLog(`You: ${text}`);
            setAgentState('listening');

            if (videoState.current === VS.PLAYING || (videoRef.current?.paused && videoState.current !== VS.PAUSED_FOR_DOUBT && videoState.current !== VS.IDLE && videoState.current !== VS.ENDED)) {
              clearAutoResume();
              pauseVideoElement();
              videoState.current = VS.PAUSED_FOR_DOUBT;
              addLog('Paused for your question');
              const currentTime = videoRef.current?.currentTime || 0;
              sendContextToGemini(currentTime);
            }
            break;
          }

          case EventType.OUTPUT_TRANSCRIPTION:
            addLog(`Tutor: ${evt.data.text || ''}`);
            break;

          case EventType.GENERATION_COMPLETE:
            break;

          case EventType.TURN_COMPLETE: {
            setAgentState('idle');

            if (pendingVideoResultRef.current) {
              const result = pendingVideoResultRef.current;
              pendingVideoResultRef.current = null;
              presentVideoResult(result);
              return;
            }

            if (toolResponsePending.current) {
              toolResponsePending.current = false;
              return;
            }

            const action = pendingAction.current;
            if (action) {
              pendingAction.current = null;
              const ap = audioPlayerRef.current;
              if (ap) {
                const prevHandler = ap.onQueueDrained;
                const timeout = setTimeout(() => {
                  ap.onQueueDrained = prevHandler;
                  startVideoAction(action);
                }, 2000);
                ap.onQueueDrained = () => {
                  clearTimeout(timeout);
                  ap.onQueueDrained = prevHandler;
                  startVideoAction(action);
                };
              } else {
                startVideoAction(action);
              }
            }
            break;
          }

          case EventType.TOOL_CALL: {
            const calls = evt.data.functionCalls || [];
            const responses = [];
            const meta = metadataRef.current;

            for (const call of calls) {
              console.log(`[APP] Tool: ${call.name}`, call.args);
              switch (call.name) {
                case 'play_video': {
                  if (isGenerating) {
                    responses.push({
                      result: {
                        status: 'A video is ALREADY being generated. Do NOT call play_video again. Wait for the system to send you a notification when it is ready.',
                      },
                    });
                    break;
                  }
                  const topic = call.args?.topic || 'general topic';
                  const preset = call.args?.preset || chosenPresetRef.current;
                  chosenPresetRef.current = preset;
                  handlePlayVideo(topic, preset);
                  responses.push({
                    result: {
                      status: 'Generation started. The video is being created in the background (1-2 minutes). Continue chatting naturally with the student. Do NOT say the video is ready — the system will send you a notification when it is done. Do NOT call play_video again.',
                    },
                  });
                  break;
                }
                case 'show_preset_chooser': {
                  setCurrentView(VIEW.CHOOSING_PRESET);
                  responses.push({
                    result: {
                      status: 'The preset chooser is now visible. Wait for the student to pick a style. You will receive a [PRESET CHOSEN] message with their selection.',
                    },
                  });
                  break;
                }
                case 'show_resource_upload': {
                  setCurrentView(VIEW.UPLOADING_RESOURCES);
                  responses.push({
                    result: {
                      status: 'The resource upload UI is now visible. Wait for the student to upload files or skip. You will receive a [RESOURCES UPLOADED] or [RESOURCES SKIPPED] message.',
                    },
                  });
                  break;
                }
                case 'resume_video':
                  pendingAction.current = { type: 'resume' };
                  responses.push({
                    result: {
                      status: 'Video is resuming now. Do NOT speak — the video has its own narration. Stay completely silent.',
                    },
                  });
                  break;
                case 'jump_to_section': {
                  const section = meta?.sections?.find((s) => s.id === call.args?.section_id);
                  if (section) {
                    pendingAction.current = {
                      type: 'jump',
                      time: section.startTime,
                      endTime: section.endTime,
                      sectionTitle: section.title,
                    };
                    responses.push({
                      result: {
                        status: `Jumping to "${section.title}" at ${section.startTime}s. Do NOT speak — the video has its own narration. Stay completely silent.`,
                      },
                    });
                  } else {
                    responses.push({ error: `Section "${call.args?.section_id}" not found.` });
                  }
                  break;
                }
                default:
                  responses.push({ error: 'Unknown tool.' });
              }
            }

            toolResponsePending.current = true;
            gemini.sendToolResponse(calls, responses);
            break;
          }

          case EventType.TOOL_CALL_CANCELLATION: {
            pendingAction.current = null;
            toolResponsePending.current = false;
            break;
          }
        }
      };

      gemini.connect();
    } catch (e) {
      addLog(`Connection failed: ${e.message}`);
      console.error(e);
    }
  }, [addLog, showVideo]);

  const handleDisconnect = useCallback(() => {
    streamerRef.current?.stop();
    streamerRef.current = null;
    geminiRef.current?.disconnect();
    geminiRef.current = null;
    audioPlayerRef.current?.destroy();
    audioPlayerRef.current = null;
    resetAllVideoState();
    setConnected(false);
    setMicOn(false);
    setCurrentView(VIEW.LANDING);
    setAnalyserNode(null);
    setAgentState('idle');
    addLog('Disconnected');
  }, [addLog, showVideo]);

  const handleToggleMic = useCallback(async () => {
    if (micOn) {
      streamerRef.current?.stop();
      streamerRef.current = null;
      setMicOn(false);
      addLog('Mic off');
    } else {
      const s = new AudioStreamer((base64) => geminiRef.current?.sendAudio(base64));
      await s.start();
      streamerRef.current = s;
      setMicOn(true);
      addLog('Mic on');
    }
  }, [micOn, addLog]);

  const handleVideoTimeUpdate = useCallback(() => {
    const v = videoRef.current;
    const endAt = sectionEndTime.current;
    if (!v || endAt == null) return;

    if (v.currentTime >= endAt && videoState.current === VS.PLAYING) {
      v.pause();
      sectionEndTime.current = null;
      videoState.current = VS.ENDED;
      clearAutoResume();
      addLog('Section complete');
      geminiRef.current?.sendClientContent(
        '[SECTION ENDED] That section has finished playing. Do NOT call any tools. Simply let the student know the section is done and WAIT for them to tell you what they want to do next.',
      );
    }
  }, [addLog]);

  const handleVideoEnded = useCallback(() => {
    sectionEndTime.current = null;
    videoState.current = VS.ENDED;
    clearAutoResume();
    addLog('Lesson complete');
    geminiRef.current?.sendClientContent(
      '[VIDEO ENDED] The entire lesson has finished. Do NOT call any tools. Simply let the student know the lesson is complete and WAIT for their instructions.',
    );
  }, [addLog]);

  const isVideoView = currentView === VIEW.VIDEO;
  const showVisualizerViews = [VIEW.CONNECTED, VIEW.GENERATING];

  const statusText = (() => {
    if (isGenerating && currentView !== VIEW.VIDEO) return 'Generating lesson in background...';
    if (currentView === VIEW.CHOOSING_PRESET) return 'Choose a video style...';
    if (currentView === VIEW.UPLOADING_RESOURCES) return 'Add learning resources...';
    if (currentView === VIEW.GENERATING) return 'Generating lesson...';
    if (videoState.current === VS.PLAYING) return 'Teaching...';
    if (videoState.current === VS.PAUSED_FOR_DOUBT) return 'Answering your question...';
    if (videoState.current === VS.ENDED) return 'Lesson complete';
    return 'Listening...';
  })();

  return (
    <div className="h-dvh flex flex-col bg-background overflow-hidden">
      {/* Landing */}
      {currentView === VIEW.LANDING && (
        <div className="flex-1 flex flex-col items-center justify-center p-6 gap-6 relative">
          <div className="absolute inset-0 overflow-hidden pointer-events-none">
            <div className="absolute top-1/4 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[600px] h-[600px] rounded-full bg-primary/[0.04] blur-[120px]" />
          </div>

          <div className="relative z-10 flex flex-col items-center gap-6">
            <div className="flex items-center gap-2.5">
              <div className="w-9 h-9 sm:w-10 sm:h-10 rounded-xl bg-primary/15 flex items-center justify-center border border-primary/10">
                <Sparkles className="w-4 h-4 sm:w-5 sm:h-5 text-primary" />
              </div>
              <h1 className="text-2xl sm:text-3xl font-bold tracking-tight">{APP_NAME}</h1>
            </div>
            <p className="text-sm sm:text-base text-muted-foreground text-center max-w-sm leading-relaxed">
              Ask anything. Watch it come alive.
            </p>

            <div className="w-[min(50vw,260px)] sm:w-[min(55vw,300px)] aspect-square">
              <AudioVisualizer analyser={null} state="idle" />
            </div>

            <button
              onClick={handleConnect}
              className="group relative px-10 py-4 sm:px-12 sm:py-5 rounded-2xl font-medium text-sm sm:text-base text-white transition-all duration-300 hover:scale-[1.03] active:scale-[0.98] cursor-pointer"
            >
              <span className="absolute inset-0 rounded-2xl bg-gradient-to-r from-violet-600 via-primary to-indigo-500 opacity-90 group-hover:opacity-100 transition-opacity" />
              <span className="absolute inset-0 rounded-2xl bg-gradient-to-r from-violet-600 via-primary to-indigo-500 opacity-0 group-hover:opacity-60 blur-xl transition-opacity" />
              <span className="absolute inset-[1px] rounded-[15px] bg-gradient-to-b from-white/[0.12] to-transparent pointer-events-none" />
              <span className="relative flex items-center gap-2.5">
                <Sparkles className="w-4 h-4 sm:w-5 sm:h-5" />
                Start Learning
              </span>
            </button>

            <p className="text-[11px] text-muted-foreground/60">
              Powered by Gemini — voice, video, and visuals in real time
            </p>
          </div>
        </div>
      )}

      {/* Active session */}
      {currentView !== VIEW.LANDING && (
        <div className="flex-1 flex flex-col min-h-0">
          {/* Top bar */}
          <div className="flex items-center justify-between px-4 sm:px-5 py-2.5 sm:py-3 border-b border-border/50 shrink-0">
            <div className="flex items-center gap-2.5 sm:gap-3 min-w-0">
              <div className="w-7 h-7 sm:w-8 sm:h-8 rounded-lg bg-primary/10 flex items-center justify-center shrink-0">
                <Sparkles className="w-3.5 h-3.5 sm:w-4 sm:h-4 text-primary" />
              </div>
              <div className="min-w-0">
                <h1 className="text-xs sm:text-sm font-semibold leading-none truncate">{APP_NAME}</h1>
                <p className="text-[10px] sm:text-xs text-muted-foreground mt-0.5 truncate">
                  {statusText}
                </p>
              </div>
            </div>

            <div className="flex items-center gap-2 shrink-0">
              {isGenerating && currentView !== VIEW.VIDEO && (
                <Badge variant="outline" className="text-[10px] sm:text-xs gap-1 border-amber-500/30 text-amber-400 animate-pulse">
                  <Loader2 className="w-3 h-3 animate-spin" />
                  <span className="hidden sm:inline">Generating...</span>
                </Badge>
              )}
              <Badge
                variant="outline"
                className={cn(
                  'text-[10px] sm:text-xs gap-1',
                  connected ? 'border-emerald-500/30 text-emerald-400' : 'border-destructive/30 text-destructive',
                )}
              >
                <span className={cn('w-1.5 h-1.5 rounded-full', connected ? 'bg-emerald-400' : 'bg-destructive')} />
                <span className="hidden sm:inline">{connected ? 'Connected' : 'Disconnected'}</span>
              </Badge>
            </div>
          </div>

          {/* Main content */}
          <div className={cn('flex-1 flex min-h-0', isVideoView ? 'flex-col lg:flex-row' : '')}>
            <div className="flex-1 flex flex-col min-h-0">
              {/* Connected view — visualizer + optional generating indicator */}
              {currentView === VIEW.CONNECTED && (
                <div className="flex-1 flex flex-col items-center justify-center gap-3 p-4 sm:p-8">
                  <div className="w-[min(55vw,300px)] sm:w-[min(50vw,360px)] aspect-square">
                    <AudioVisualizer analyser={analyserNode} state={agentState} />
                  </div>
                  <p className="text-xs sm:text-sm text-muted-foreground text-center max-w-xs sm:max-w-sm">
                    {isGenerating
                      ? 'Your video is generating — keep chatting!'
                      : 'Ask me about anything — math, physics, computer science, and more'}
                  </p>
                  {isGenerating && (
                    <div className="mt-3 flex items-center gap-3 px-4 py-2.5 rounded-xl bg-amber-500/5 border border-amber-500/15 backdrop-blur-sm">
                      <div className="relative w-8 h-8 shrink-0">
                        <svg className="w-full h-full -rotate-90" viewBox="0 0 36 36">
                          <circle cx="18" cy="18" r="14" fill="none" stroke="currentColor" strokeWidth="2.5" className="text-amber-500/15" />
                          <circle cx="18" cy="18" r="14" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"
                            className="text-amber-400 transition-all duration-1000 ease-out"
                            strokeDasharray={`${2 * Math.PI * 14}`}
                            strokeDashoffset={`${2 * Math.PI * 14 * (1 - Math.max(0.02, genProgress / 100))}`}
                          />
                        </svg>
                        <span className="absolute inset-0 flex items-center justify-center text-[9px] font-semibold text-amber-300 tabular-nums">
                          {Math.round(genProgress)}
                        </span>
                      </div>
                      <span className="text-xs text-amber-300/80">
                        {genStep === 'queued' ? 'Starting...'
                          : genStep === 'generating_scenes' ? 'Crafting video...'
                          : genStep === 'stitching' ? 'Stitching...'
                          : genStep === 'generating_audio' ? 'Narration...'
                          : genStep === 'planning_scenes' ? 'Planning...'
                          : genStep === 'processing_context' ? 'Researching...'
                          : genStep.replace(/_/g, ' ')}
                      </span>
                    </div>
                  )}
                </div>
              )}

              {/* Preset chooser */}
              {currentView === VIEW.CHOOSING_PRESET && (
                <div className="flex-1 flex items-center justify-center p-4 overflow-y-auto">
                  <PresetChooser presets={presets} onSelect={handlePresetSelected} />
                </div>
              )}

              {/* Resource upload */}
              {currentView === VIEW.UPLOADING_RESOURCES && (
                <div className="flex-1 flex items-center justify-center p-4 overflow-y-auto">
                  <ResourceUpload
                    onSubmit={handleResourcesSubmitted}
                    onSkip={handleResourcesSkipped}
                  />
                </div>
              )}

              {/* Generating (full-screen, when not in connected chat mode) */}
              {currentView === VIEW.GENERATING && (
                <div className="flex-1 flex items-center justify-center p-4">
                  <VideoGenerating serverStep={genStep} serverProgress={genProgress} error={genError} />
                </div>
              )}

              {/* Video player */}
              {currentView === VIEW.VIDEO && (
                <div className="flex-1 flex flex-col min-h-0">
                  <div className="flex-1 relative bg-black/20 flex items-center justify-center min-h-0">
                    {videoVisible && videoBlobUrl && (
                      <video
                        ref={videoRef}
                        src={videoBlobUrl}
                        className="w-full h-full object-contain"
                        preload="auto"
                        onEnded={handleVideoEnded}
                        onTimeUpdate={handleVideoTimeUpdate}
                      />
                    )}

                    <div className="absolute top-3 left-3 w-16 h-16 sm:w-20 sm:h-20 z-10 opacity-80">
                      <AudioVisualizer analyser={analyserNode} state={agentState} />
                    </div>

                    {videoState.current === VS.PAUSED_FOR_DOUBT && (
                      <div className="absolute inset-0 bg-black/30 flex items-center justify-center backdrop-blur-[2px]">
                        <div className="flex flex-col items-center gap-2 sm:gap-3">
                          <div className="w-12 h-12 sm:w-16 sm:h-16 rounded-full bg-primary/20 flex items-center justify-center backdrop-blur-md">
                            <Pause className="w-5 h-5 sm:w-7 sm:h-7 text-primary" />
                          </div>
                          <p className="text-xs sm:text-sm text-foreground/80 font-medium">
                            Listening to your question...
                          </p>
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>

            {/* Section progress sidebar */}
            {isVideoView && metadata?.sections && (
              <div className="w-full lg:w-[260px] xl:w-[280px] shrink-0 border-t lg:border-t-0 lg:border-l border-border/30 max-h-[30vh] lg:max-h-none overflow-hidden">
                <ScrollArea className="h-full">
                  <SectionProgress
                    sections={metadata.sections}
                    currentTime={videoTime}
                    videoState={videoState.current}
                  />
                </ScrollArea>
              </div>
            )}
          </div>

          {/* Bottom controls */}
          <div className="flex items-center justify-center gap-3 px-4 sm:px-6 py-3 sm:py-4 border-t border-border/50 bg-card/30 shrink-0">
            <Button
              variant={micOn ? 'default' : 'secondary'}
              size="icon"
              className={cn(
                'w-10 h-10 sm:w-12 sm:h-12 rounded-full transition-all',
                micOn && 'bg-emerald-600 hover:bg-emerald-700 shadow-lg shadow-emerald-500/20',
                !connected && 'opacity-40 pointer-events-none',
              )}
              onClick={handleToggleMic}
              disabled={!connected}
            >
              {micOn ? <Mic className="w-4 h-4 sm:w-5 sm:h-5" /> : <MicOff className="w-4 h-4 sm:w-5 sm:h-5" />}
            </Button>

            <Button
              variant="destructive"
              size="icon"
              className={cn(
                'w-10 h-10 sm:w-12 sm:h-12 rounded-full',
                !connected && 'opacity-40 pointer-events-none',
              )}
              onClick={handleDisconnect}
              disabled={!connected}
            >
              <PhoneOff className="w-4 h-4 sm:w-5 sm:h-5" />
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
