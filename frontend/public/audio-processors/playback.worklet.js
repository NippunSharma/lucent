class PCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.audioQueue = [];
    this.wasPlaying = false;
    this.port.onmessage = (event) => {
      if (event.data === 'interrupt') {
        this.audioQueue = [];
      } else if (event.data === 'query_empty') {
        this.port.postMessage({ type: 'queue_status', empty: this.audioQueue.length === 0 });
      } else if (event.data instanceof Float32Array) {
        this.audioQueue.push(event.data);
      }
    };
  }

  process(inputs, outputs) {
    const output = outputs[0];
    if (output.length === 0) return true;
    const channel = output[0];
    let idx = 0;

    while (idx < channel.length && this.audioQueue.length > 0) {
      const buf = this.audioQueue[0];
      if (!buf || buf.length === 0) {
        this.audioQueue.shift();
        continue;
      }
      const remaining = channel.length - idx;
      const copy = Math.min(remaining, buf.length);
      for (let i = 0; i < copy; i++) {
        channel[idx++] = buf[i];
      }
      if (copy < buf.length) {
        this.audioQueue[0] = buf.slice(copy);
      } else {
        this.audioQueue.shift();
      }
    }

    while (idx < channel.length) {
      channel[idx++] = 0;
    }

    const isPlaying = idx > 0 && this.audioQueue.length > 0;
    if (this.wasPlaying && this.audioQueue.length === 0) {
      this.port.postMessage({ type: 'queue_drained' });
    }
    this.wasPlaying = this.audioQueue.length > 0 || idx > 0;

    return true;
  }
}

registerProcessor('pcm-processor', PCMProcessor);
