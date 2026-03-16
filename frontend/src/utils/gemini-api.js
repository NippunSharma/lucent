const PROJECT_ID = 'gemini-devpost-hackathon';
const LOCATION = 'us-central1';
const MODEL = 'gemini-live-2.5-flash-native-audio';

const _IS_DEV = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
const PROXY_URL = _IS_DEV
  ? 'ws://localhost:8082'
  : `wss://${window.location.host}/ws`;

export const EventType = {
  SETUP_COMPLETE: 'SETUP_COMPLETE',
  AUDIO: 'AUDIO',
  TEXT: 'TEXT',
  INTERRUPTED: 'INTERRUPTED',
  TURN_COMPLETE: 'TURN_COMPLETE',
  GENERATION_COMPLETE: 'GENERATION_COMPLETE',
  TOOL_CALL: 'TOOL_CALL',
  TOOL_CALL_CANCELLATION: 'TOOL_CALL_CANCELLATION',
  INPUT_TRANSCRIPTION: 'INPUT_TRANSCRIPTION',
  OUTPUT_TRANSCRIPTION: 'OUTPUT_TRANSCRIPTION',
};

/**
 * Parse a single Gemini websocket message into potentially MULTIPLE events.
 * Per the docs, a serverContent message can carry turnComplete, interrupted,
 * generationComplete, modelTurn, inputTranscription, and outputTranscription
 * simultaneously. We must emit each as a separate event.
 */
function parseMessage(data) {
  const events = [];

  if (data?.setupComplete) {
    events.push({ type: EventType.SETUP_COMPLETE });
    return events;
  }

  if (data?.toolCall) {
    events.push({ type: EventType.TOOL_CALL, data: data.toolCall });
    return events;
  }

  if (data?.toolCallCancellation) {
    events.push({ type: EventType.TOOL_CALL_CANCELLATION, data: data.toolCallCancellation });
    return events;
  }

  const sc = data?.serverContent;
  if (!sc) return events;

  const parts = sc.modelTurn?.parts;
  if (parts?.length) {
    for (const part of parts) {
      if (part.inlineData) {
        events.push({ type: EventType.AUDIO, data: part.inlineData.data });
      } else if (part.text) {
        events.push({ type: EventType.TEXT, data: part.text });
      }
    }
  }

  if (sc.inputTranscription) {
    events.push({ type: EventType.INPUT_TRANSCRIPTION, data: sc.inputTranscription });
  }

  if (sc.outputTranscription) {
    events.push({ type: EventType.OUTPUT_TRANSCRIPTION, data: sc.outputTranscription });
  }

  if (sc.interrupted) {
    events.push({ type: EventType.INTERRUPTED });
  }

  if (sc.generationComplete) {
    events.push({ type: EventType.GENERATION_COMPLETE });
  }

  if (sc.turnComplete) {
    events.push({ type: EventType.TURN_COMPLETE });
  }

  return events;
}

export class GeminiLiveAPI {
  constructor() {
    this.ws = null;
    this.connected = false;
    this.systemInstructions = '';
    this.tools = [];
    this.voiceName = 'Puck';

    this._intentionalClose = false;
    this._reconnectAttempt = 0;
    this._reconnectTimer = null;
    this._maxReconnectAttempts = 5;
    this._baseDelay = 1000;

    this.onEvent = () => {};
    this.onConnectionStarted = () => {};
    this.onReconnecting = () => {};
    this.onClose = () => {};
    this.onError = () => {};
  }

  connect() {
    this._intentionalClose = false;
    this._doConnect();
  }

  _doConnect() {
    if (this.ws) {
      try { this.ws.close(); } catch (_) {}
      this.ws = null;
    }

    const serviceUrl =
      `wss://${LOCATION}-aiplatform.googleapis.com/ws/google.cloud.aiplatform.v1beta1.LlmBidiService/BidiGenerateContent`;

    this.ws = new WebSocket(PROXY_URL);

    this.ws.onopen = () => {
      console.log('[GEMINI] WebSocket opened, sending setup...');
      this.connected = true;
      this._reconnectAttempt = 0;

      this._send({ service_url: serviceUrl });

      const setup = {
        setup: {
          model: `projects/${PROJECT_ID}/locations/${LOCATION}/publishers/google/models/${MODEL}`,
          generation_config: {
            response_modalities: ['AUDIO'],
            speech_config: {
              voice_config: {
                prebuilt_voice_config: { voice_name: this.voiceName },
              },
            },
            enable_affective_dialog: true,
          },
          system_instruction: { parts: [{ text: this.systemInstructions }] },
          tools: { function_declarations: this.tools },
          proactivity: { proactiveAudio: true },
          realtime_input_config: {
            automatic_activity_detection: {
              disabled: false,
              silence_duration_ms: 1000,
              prefix_padding_ms: 500,
              start_of_speech_sensitivity: 'START_SENSITIVITY_LOW',
              end_of_speech_sensitivity: 'END_SENSITIVITY_LOW',
            },
          },
          input_audio_transcription: {},
          output_audio_transcription: {},
        },
      };
      this._send(setup);

      this.onConnectionStarted();
    };

    this.ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      const events = parseMessage(data);
      for (const evt of events) {
        this.onEvent(evt);
      }
    };

    this.ws.onclose = (event) => {
      console.log('[GEMINI] WebSocket closed, intentional:', this._intentionalClose);
      this.connected = false;

      if (this._intentionalClose) {
        this.onClose(event);
        return;
      }

      this._tryReconnect();
    };

    this.ws.onerror = (event) => {
      console.error('[GEMINI] WebSocket error:', event);
      this.connected = false;
      this.onError(event);
    };
  }

  _tryReconnect() {
    if (this._intentionalClose) return;
    if (this._reconnectAttempt >= this._maxReconnectAttempts) {
      console.log('[GEMINI] Max reconnect attempts reached, giving up');
      this.onClose({ reason: 'max_reconnect_attempts' });
      return;
    }

    this._reconnectAttempt++;
    const delay = Math.min(this._baseDelay * Math.pow(2, this._reconnectAttempt - 1), 15000);
    console.log(`[GEMINI] Reconnecting in ${delay}ms (attempt ${this._reconnectAttempt}/${this._maxReconnectAttempts})`);
    this.onReconnecting(this._reconnectAttempt, this._maxReconnectAttempts);

    this._reconnectTimer = setTimeout(() => {
      if (!this._intentionalClose) {
        this._doConnect();
      }
    }, delay);
  }

  disconnect() {
    this._intentionalClose = true;
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    if (this.ws) {
      this.ws.close();
      this.ws = null;
      this.connected = false;
    }
  }

  sendAudio(base64) {
    this._send({
      realtime_input: {
        media_chunks: [{ mime_type: 'audio/pcm', data: base64 }],
      },
    });
  }

  sendClientContent(text) {
    this._send({
      client_content: {
        turns: [{ role: 'user', parts: [{ text }] }],
        turn_complete: true,
      },
    });
  }

  sendClientContentWithParts(parts) {
    this._send({
      client_content: {
        turns: [{ role: 'user', parts }],
        turn_complete: true,
      },
    });
  }

  sendToolResponse(functionCalls, responses) {
    this._send({
      tool_response: {
        function_responses: functionCalls.map((call, i) => ({
          id: call.id,
          name: call.name,
          response: responses[i] || {},
        })),
      },
    });
  }

  _send(data) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data));
    }
  }
}
