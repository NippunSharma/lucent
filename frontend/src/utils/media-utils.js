export class AudioStreamer {
  constructor(onData) {
    this.onData = onData;
    this.context = null;
    this.stream = null;
    this.processor = null;
  }

  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        sampleRate: 16000,
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });

    this.context = new AudioContext({ sampleRate: 16000 });
    await this.context.audioWorklet.addModule('/audio-processors/capture.worklet.js');
    const source = this.context.createMediaStreamSource(this.stream);
    this.processor = new AudioWorkletNode(this.context, 'audio-capture-processor');

    this.processor.port.onmessage = (event) => {
      if (event.data.type === 'audio') {
        const pcm = event.data.data;
        const int16 = new Int16Array(pcm.length);
        for (let i = 0; i < pcm.length; i++) {
          int16[i] = Math.max(-32768, Math.min(32767, Math.round(pcm[i] * 32767)));
        }
        const bytes = new Uint8Array(int16.buffer);
        let binary = '';
        for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
        this.onData(btoa(binary));
      }
    };

    source.connect(this.processor);
    this.processor.connect(this.context.destination);
  }

  stop() {
    this.processor?.disconnect();
    this.stream?.getTracks().forEach((t) => t.stop());
    this.context?.close();
    this.processor = null;
    this.stream = null;
    this.context = null;
  }
}

export class AudioPlayer {
  constructor() {
    this.context = null;
    this.workletNode = null;
    this.analyser = null;
    this._ready = false;
    this.onQueueDrained = null;
  }

  async init() {
    this.context = new AudioContext({ sampleRate: 24000 });
    await this.context.audioWorklet.addModule('/audio-processors/playback.worklet.js');
    this.workletNode = new AudioWorkletNode(this.context, 'pcm-processor');

    this.analyser = this.context.createAnalyser();
    this.analyser.fftSize = 256;
    this.analyser.smoothingTimeConstant = 0.8;
    this.workletNode.connect(this.analyser);
    this.analyser.connect(this.context.destination);

    this.workletNode.port.onmessage = (event) => {
      if (event.data?.type === 'queue_drained') {
        this.onQueueDrained?.();
      }
      if (event.data?.type === 'queue_status') {
        this._lastQueueEmpty = event.data.empty;
      }
    };

    this._ready = true;
  }

  play(arrayBuffer) {
    if (!this._ready) return;
    const int16 = new Int16Array(arrayBuffer);
    const float32 = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) float32[i] = int16[i] / 32768;
    this.workletNode.port.postMessage(float32);
  }

  interrupt() {
    if (this.workletNode) {
      this.workletNode.port.postMessage('interrupt');
    }
  }

  destroy() {
    this.onQueueDrained = null;
    this.analyser?.disconnect();
    this.workletNode?.disconnect();
    this.context?.close();
    this.analyser = null;
    this._ready = false;
  }
}
