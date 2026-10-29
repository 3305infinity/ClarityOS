// Collects mono microphone audio (AudioContext runs at 16 kHz) and posts Float32 chunks to the page.
class PCM extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch && ch.length) this.port.postMessage(ch.slice(0));
    return true;
  }
}
registerProcessor('pcm', PCM);
