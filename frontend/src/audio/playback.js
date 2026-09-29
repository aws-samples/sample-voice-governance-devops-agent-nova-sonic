/**
 * 24 kHz PCM playback queue for Nova Sonic voice responses.
 *
 * Binary WebSocket frames carrying 24 kHz 16-bit mono PCM (little-endian,
 * the Nova Sonic output format) are converted to Float32 samples, wrapped
 * in an AudioBuffer created at {@link PLAYBACK_SAMPLE_RATE}, and scheduled
 * back-to-back on a single AudioContext timeline so consecutive chunks
 * play gaplessly. The first chunk is scheduled at the context's current
 * time, so audible playback begins within 1 second of receiving it
 * (Req 1.7). Buffers are always created at the 24 kHz source rate; when
 * the AudioContext runs at a different native rate, the
 * AudioBufferSourceNode resamples automatically.
 *
 * Barge-in: Nova Sonic lets an engineer interrupt a response mid-speech.
 * When the voice client (ws/voice-client.js) detects an interruption it
 * must call {@link PlaybackQueue#clear} so already-scheduled speech stops
 * immediately instead of playing over the new turn.
 *
 * Autoplay policies may leave a fresh AudioContext in the 'suspended'
 * state until a user gesture occurs; enqueue resumes the context before
 * scheduling. The AudioContext is created lazily on first enqueue and its
 * factory is injectable so tests can substitute a fake implementation.
 */

/**
 * Sample rate, in Hz, of the PCM audio Nova Sonic produces. This is a
 * protocol constant of the voice plane, not an environment-specific value.
 * @type {number}
 */
export const PLAYBACK_SAMPLE_RATE = 24000;

/**
 * Bytes per sample of 16-bit PCM audio.
 * @type {number}
 */
const BYTES_PER_SAMPLE = 2;

/**
 * Divisor mapping the signed 16-bit integer range onto Web Audio's
 * [-1.0, 1.0) float range: -32768 maps to -1.0 and 32767 to ~0.99997.
 * @type {number}
 */
const INT16_SCALE = 32768;

/**
 * Creates the browser's default AudioContext at the device's native rate.
 * @returns {AudioContext} A newly constructed AudioContext.
 */
const defaultCreateContext = () => new globalThis.AudioContext();

/**
 * Converts signed 16-bit PCM samples to Web Audio Float32 samples.
 *
 * Each sample is divided by 32768, so -32768 maps to -1.0, 0 maps to 0.0,
 * and 32767 maps to 32767/32768 (~0.99997). The input is not mutated.
 * @param {Int16Array} int16Array - Signed 16-bit mono PCM samples.
 * @returns {Float32Array} A new array of the same length with samples in
 *   the range [-1.0, 1.0).
 */
export function int16ToFloat32(int16Array) {
  const float32 = new Float32Array(int16Array.length);
  for (let i = 0; i < int16Array.length; i += 1) {
    float32[i] = int16Array[i] / INT16_SCALE;
  }
  return float32;
}

/**
 * Gapless playback queue for 24 kHz 16-bit mono PCM chunks.
 *
 * Chunks passed to {@link PlaybackQueue#enqueue} are scheduled strictly in
 * arrival order on one AudioContext timeline. The queue tracks the end
 * time of the last scheduled buffer (nextStartTime) and starts each new
 * buffer at `max(context.currentTime, nextStartTime)`: immediately for the
 * first chunk (Req 1.7), and seamlessly after the previous chunk for the
 * rest. {@link PlaybackQueue#clear} supports barge-in by stopping all
 * scheduled audio, and {@link PlaybackQueue#close} tears the queue down.
 */
export class PlaybackQueue {
  /** @type {() => AudioContext} */
  #createContext;

  /** @type {AudioContext | null} */
  #context = null;

  /** @type {number} */
  #nextStartTime = 0;

  /** @type {Set<AudioBufferSourceNode>} */
  #sources = new Set();

  /** @type {Promise<void>} */
  #chain = Promise.resolve();

  /** @type {boolean} */
  #closed = false;

  /**
   * Creates a playback queue. No AudioContext is created until the first
   * chunk is enqueued, so construction is free of autoplay side effects.
   * @param {object} [options] - Optional queue configuration.
   * @param {() => AudioContext} [options.createContext] - Factory returning
   *   the AudioContext to play through. Defaults to constructing the
   *   browser's AudioContext; tests inject a fake here.
   */
  constructor({ createContext = defaultCreateContext } = {}) {
    this.#createContext = createContext;
  }

  /**
   * Enqueues one binary PCM chunk for playback.
   *
   * The chunk must contain 24 kHz 16-bit mono PCM, as delivered by the
   * voice WebSocket (Req 1.7). Chunks are scheduled in the order enqueue
   * is called, even when an internal await (context resume) is pending,
   * because scheduling is serialized on an internal promise chain. If the
   * AudioContext is suspended by an autoplay policy, it is resumed before
   * scheduling. Empty chunks resolve without scheduling anything.
   * @param {ArrayBuffer} arrayBuffer - Binary chunk of 24 kHz 16-bit mono
   *   PCM samples (little-endian), e.g. a WebSocket binary message body.
   * @returns {Promise<void>} Resolves once the chunk has been scheduled on
   *   the AudioContext timeline (or dropped because the queue was closed
   *   while the chunk was waiting).
   * @throws {Error} If the queue has been closed with
   *   {@link PlaybackQueue#close}.
   * @throws {TypeError} If `arrayBuffer` is not an ArrayBuffer or its byte
   *   length is not a multiple of 2 (16-bit samples).
   */
  async enqueue(arrayBuffer) {
    if (this.#closed) {
      throw new Error(
        'PlaybackQueue is closed; create a new queue to play audio.',
      );
    }
    if (!(arrayBuffer instanceof ArrayBuffer)) {
      throw new TypeError(
        'enqueue expects an ArrayBuffer of 16-bit PCM data.',
      );
    }
    if (arrayBuffer.byteLength % BYTES_PER_SAMPLE !== 0) {
      throw new TypeError(
        'PCM chunk byte length must be a multiple of ' +
          `${BYTES_PER_SAMPLE} (16-bit samples), got ` +
          `${arrayBuffer.byteLength} bytes.`,
      );
    }
    const scheduled = this.#chain.then(() => this.#schedule(arrayBuffer));
    // Keep the chain alive regardless of this chunk's outcome so one
    // failure never wedges the queue; the caller still sees the rejection
    // through the returned promise.
    this.#chain = scheduled.then(
      () => undefined,
      () => undefined,
    );
    return scheduled;
  }

  /**
   * Stops all scheduled playback and resets the timeline (barge-in hook).
   *
   * The voice client calls this when Nova Sonic reports the engineer
   * interrupted the spoken response: every scheduled or playing source is
   * stopped and disconnected, and nextStartTime is reset so the next
   * enqueued chunk starts immediately. The queue remains usable; this is
   * flush, not teardown.
   */
  clear() {
    for (const source of this.#sources) {
      source.stop();
      source.disconnect();
    }
    this.#sources.clear();
    this.#nextStartTime = 0;
  }

  /**
   * Tears the queue down: stops scheduled audio, closes the AudioContext,
   * and rejects further enqueues. Safe to call more than once; chunks
   * still waiting on the internal chain are dropped silently.
   * @returns {Promise<void>} Resolves once the AudioContext (if one was
   *   ever created) has been closed.
   */
  async close() {
    if (this.#closed) {
      return;
    }
    this.#closed = true;
    this.clear();
    const context = this.#context;
    this.#context = null;
    if (context && context.state !== 'closed') {
      await context.close();
    }
  }

  /**
   * Returns the AudioContext, creating it on first use.
   * @returns {AudioContext} The queue's AudioContext.
   */
  #ensureContext() {
    if (!this.#context) {
      this.#context = this.#createContext();
    }
    return this.#context;
  }

  /**
   * Converts one PCM chunk and schedules it gaplessly after the last
   * scheduled buffer. Runs serialized on the internal promise chain, so
   * arrival order is preserved across the context-resume await. Drops the
   * chunk if the queue closed while it was waiting.
   * @param {ArrayBuffer} arrayBuffer - Validated binary chunk of 24 kHz
   *   16-bit mono PCM samples.
   * @returns {Promise<void>} Resolves once the chunk is scheduled or
   *   dropped.
   */
  async #schedule(arrayBuffer) {
    if (this.#closed || arrayBuffer.byteLength === 0) {
      return;
    }
    const context = this.#ensureContext();
    if (context.state === 'suspended') {
      // Autoplay policies suspend fresh contexts until a user gesture;
      // resume before scheduling so the chunk becomes audible (Req 1.7).
      await context.resume();
    }
    if (this.#closed) {
      return;
    }
    // Int16Array reads the bytes as platform-endian; all supported
    // browsers are little-endian, matching the PCM wire format.
    const samples = int16ToFloat32(new Int16Array(arrayBuffer));
    const buffer = context.createBuffer(
      1,
      samples.length,
      PLAYBACK_SAMPLE_RATE,
    );
    buffer.copyToChannel(samples, 0);
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    this.#sources.add(source);
    source.addEventListener('ended', () => {
      this.#sources.delete(source);
      source.disconnect();
    });
    const startAt = Math.max(context.currentTime, this.#nextStartTime);
    source.start(startAt);
    this.#nextStartTime = startAt + buffer.duration;
  }
}
