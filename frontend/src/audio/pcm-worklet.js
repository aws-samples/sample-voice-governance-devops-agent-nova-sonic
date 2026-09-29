/**
 * PCM capture worklet: browser-rate Float32 audio → 16 kHz 16-bit mono PCM.
 *
 * Two layers live in this module so the conversion math stays testable
 * outside the AudioWorklet runtime (Property 1, task 9.5):
 *
 * 1. Pure functions ({@link downsampleFloat32}, {@link float32ToInt16})
 *    exported at module top level. They hold no state and touch no Web
 *    Audio API, so vitest/jsdom — and plain Node — can import this file
 *    directly.
 * 2. `PcmCaptureProcessor`, an `AudioWorkletProcessor` registered as
 *    {@link PROCESSOR_NAME}. The class is defined and registered only when
 *    the AudioWorklet globals exist (`globalThis.AudioWorkletProcessor` and
 *    `globalThis.registerProcessor`), so importing this module in a
 *    non-worklet runtime is safe and does nothing beyond exporting the
 *    pure functions.
 *
 * All audio processing stays inside the worklet thread; the main thread
 * (`src/audio/capture.js`) only receives ready-to-send ArrayBuffer chunks
 * of 16 kHz Int16 PCM (Req 1.1).
 */

/**
 * Name under which the capture processor is registered; `capture.js`
 * imports this to construct the matching `AudioWorkletNode`.
 * @type {string}
 */
export const PROCESSOR_NAME = 'pcm-capture-processor';

/**
 * Output sample rate expected by the Voice_Service (Req 1.1).
 * @type {number}
 */
export const TARGET_SAMPLE_RATE = 16000;

/**
 * Input samples accumulated before a block is downsampled and posted.
 * 3072 samples is 24 render quanta (128 frames each) — 64 ms at 48 kHz —
 * small enough for interactive latency, large enough to keep per-message
 * overhead low.
 * @type {number}
 */
const CHUNK_SIZE = 3072;

/**
 * Resamples Float32 audio from one sample rate to another using linear
 * interpolation.
 *
 * Algorithm: output sample `i` is read at the fractional input position
 * `i * (inputRate / outputRate)`; its value is the linear blend of the two
 * nearest input samples, weighted by the fractional part (the final
 * position clamps its right-hand neighbour to the last input sample). The
 * output length is `floor(input.length * outputRate / inputRate)`, i.e. the
 * input length scaled by the rate ratio, within one frame. Silence maps to
 * silence: an all-zero input yields an all-zero output. No anti-aliasing
 * low-pass filter is applied — acceptable for 16 kHz speech capture.
 *
 * Pure: no state, the input array is never mutated, and a new array is
 * returned on every call (a copy even when the rates are equal).
 * @param {Float32Array} input - Source samples at `inputRate`.
 * @param {number} inputRate - Sample rate of `input` in Hz (e.g. the
 *   browser's native context rate, 44100 or 48000).
 * @param {number} outputRate - Desired sample rate in Hz (16000 for the
 *   Voice_Service).
 * @returns {Float32Array} Resampled audio at `outputRate`, length
 *   `floor(input.length * outputRate / inputRate)`.
 * @throws {TypeError} If `input` is not a Float32Array.
 * @throws {RangeError} If either rate is not a positive finite number.
 */
export function downsampleFloat32(input, inputRate, outputRate) {
  if (!(input instanceof Float32Array)) {
    throw new TypeError('downsampleFloat32 expects a Float32Array input');
  }
  if (!Number.isFinite(inputRate) || inputRate <= 0) {
    throw new RangeError(
      `inputRate must be a positive finite number, got ${inputRate}`,
    );
  }
  if (!Number.isFinite(outputRate) || outputRate <= 0) {
    throw new RangeError(
      `outputRate must be a positive finite number, got ${outputRate}`,
    );
  }
  if (inputRate === outputRate) {
    return Float32Array.from(input);
  }

  const ratio = inputRate / outputRate;
  const outputLength = Math.floor((input.length * outputRate) / inputRate);
  const output = new Float32Array(outputLength);
  const lastIndex = input.length - 1;
  for (let i = 0; i < outputLength; i += 1) {
    const position = i * ratio;
    const baseIndex = Math.min(Math.floor(position), lastIndex);
    const nextIndex = Math.min(baseIndex + 1, lastIndex);
    const fraction = position - baseIndex;
    output[i] = input[baseIndex] * (1 - fraction) + input[nextIndex] * fraction;
  }
  return output;
}

/**
 * Converts Float32 samples in the nominal [-1, 1] range to 16-bit signed
 * integer PCM.
 *
 * Each sample is clamped to [-1, 1] and scaled asymmetrically — negative
 * values by 32768, positive values by 32767 — so -1 maps to -32768, +1 maps
 * to +32767, and 0 (silence) maps exactly to 0. Every output sample is
 * therefore within the Int16 range, the output length equals the input
 * length, and silence maps to silence.
 * @param {Float32Array} samples - Float32 audio samples; values outside
 *   [-1, 1] are clamped.
 * @returns {Int16Array} PCM samples in [-32768, 32767], same length as
 *   `samples`, native (little-endian) byte order.
 * @throws {TypeError} If `samples` is not a Float32Array.
 */
export function float32ToInt16(samples) {
  if (!(samples instanceof Float32Array)) {
    throw new TypeError('float32ToInt16 expects a Float32Array input');
  }
  const output = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i += 1) {
    const clamped = Math.min(1, Math.max(-1, samples[i]));
    output[i] = Math.round(clamped < 0 ? clamped * 32768 : clamped * 32767);
  }
  return output;
}

/**
 * AudioWorklet base class, present only inside an AudioWorkletGlobalScope.
 * Read via `globalThis` so importing this module elsewhere never throws.
 * @type {Function|undefined}
 */
const AudioWorkletProcessorBase = globalThis.AudioWorkletProcessor;

if (
  typeof AudioWorkletProcessorBase === 'function' &&
  typeof globalThis.registerProcessor === 'function'
) {
  /**
   * Capture processor running on the audio rendering thread.
   *
   * Buffers incoming 128-frame render quanta (channel 0 only — capture is
   * mono, Req 1.1) until at least {@link CHUNK_SIZE} samples accumulate,
   * downsamples the block from the context rate (`globalThis.sampleRate`)
   * to {@link TARGET_SAMPLE_RATE} via {@link downsampleFloat32}, encodes it
   * with {@link float32ToInt16}, and posts the underlying ArrayBuffer to
   * the main thread as a transferable (zero copy). Block-wise resampling
   * clamps interpolation at each block seam (one sample per ~64 ms block),
   * which is inaudible for speech and keeps the conversion math pure and
   * stateless.
   */
  class PcmCaptureProcessor extends AudioWorkletProcessorBase {
    /**
     * Initializes the empty input accumulation buffer.
     */
    constructor() {
      super();
      /**
       * Copied input quanta awaiting downsampling.
       * @type {Float32Array[]}
       */
      this.pendingFrames = [];
      /**
       * Total samples currently held in `pendingFrames`.
       * @type {number}
       */
      this.pendingLength = 0;
    }

    /**
     * Accumulates one render quantum and flushes a PCM block once enough
     * input is buffered. The engine may reuse the input buffers between
     * calls, so each quantum is copied before being retained.
     * @param {Float32Array[][]} inputs - Per-input, per-channel sample
     *   arrays for this render quantum; only `inputs[0][0]` (mono) is used.
     * @returns {boolean} Always true so the processor stays alive for the
     *   lifetime of the node.
     */
    process(inputs) {
      const channel = inputs[0]?.[0];
      if (channel && channel.length > 0) {
        this.pendingFrames.push(Float32Array.from(channel));
        this.pendingLength += channel.length;
        if (this.pendingLength >= CHUNK_SIZE) {
          this.flushPending();
        }
      }
      return true;
    }

    /**
     * Downsamples and encodes all buffered input, then posts the resulting
     * Int16 PCM chunk to the main thread, transferring the ArrayBuffer.
     */
    flushPending() {
      const block = new Float32Array(this.pendingLength);
      let offset = 0;
      for (const frame of this.pendingFrames) {
        block.set(frame, offset);
        offset += frame.length;
      }
      this.pendingFrames = [];
      this.pendingLength = 0;

      const downsampled = downsampleFloat32(
        block,
        globalThis.sampleRate,
        TARGET_SAMPLE_RATE,
      );
      const pcm = float32ToInt16(downsampled);
      if (pcm.length > 0) {
        this.port.postMessage(pcm.buffer, [pcm.buffer]);
      }
    }
  }

  globalThis.registerProcessor(PROCESSOR_NAME, PcmCaptureProcessor);
}
