/**
 * PCM Audio Processor Worklet
 * 
 * Captures microphone audio and converts to 16-bit PCM for WebSocket transmission.
 * Previously embedded in app.js, extracted for better maintainability.
 */

class PCMProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this.bufferSize = 128;
        this.buffer = new Int16Array(this.bufferSize);
        this.index = 0;
    }

    process(inputs) {
        const input = inputs[0];
        if (!input?.length) return true;

        const channel = input[0];
        for (let i = 0; i < channel.length; i++) {
            const sample = Math.max(-1, Math.min(1, channel[i]));
            this.buffer[this.index++] = sample < 0 ? sample * 0x8000 : sample * 0x7FFF;

            if (this.index >= this.bufferSize) {
                this.port.postMessage(this.buffer);
                this.index = 0;
            }
        }
        return true;
    }
}

registerProcessor('pcm-processor', PCMProcessor);
