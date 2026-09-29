/**
 * Microphone capture pipeline: getUserMedia → AudioContext → AudioWorklet.
 *
 * {@link startCapture} requests the microphone, loads the PCM worklet
 * (`src/audio/pcm-worklet.js`), and wires a
 * MediaStreamSource → AudioWorkletNode graph that delivers 16 kHz 16-bit
 * mono PCM chunks (ArrayBuffer) to a caller-provided callback (Req 1.1).
 * All resampling happens inside the worklet thread — never on the main
 * thread. The WebSocket wiring lives in `src/ws/voice-client.js` /
 * `main.js` (task 9.7); this module never touches a WebSocket.
 *
 * Permission-denial contract (Req 1.8, 9.6): when the engineer denies
 * microphone permission (`NotAllowedError`, or the legacy
 * `PermissionDeniedError` name), {@link startCapture}:
 *
 * 1. dispatches a cancelable `portal:error` CustomEvent on `document` with
 *    `detail: { category: 'mic-denied', message }` so a central error UI
 *    (`src/ui/errors.js`, task 9.8) can render it — that listener calls
 *    `event.preventDefault()` to take ownership;
 * 2. if no listener canceled the event, renders a minimal local fallback
 *    alert into `#alert-area` (same pattern as `capability.js`);
 * 3. throws {@link MicrophoneDeniedError}. Callers MUST catch this typed
 *    error and MUST NOT open the voice WebSocket when it is thrown — no
 *    stream, context, or handle exists at that point.
 *
 * Like `capability.js`, browser APIs are read from an injectable
 * environment object (default `globalThis`) so tests can supply fakes.
 */

import { PROCESSOR_NAME } from './pcm-worklet.js';

/**
 * Explanatory message shown when microphone permission is denied
 * (Req 1.8, 9.6).
 * @type {string}
 */
const MIC_DENIED_MESSAGE =
  'Microphone access is required to start a voice session. Allow ' +
  'microphone permission for this site in your browser settings and try ' +
  'again. No voice connection was opened.';

/**
 * getUserMedia constraints for voice capture: mono input with browser
 * echo cancellation and noise suppression enabled. The browser keeps its
 * native sample rate; downsampling to 16 kHz happens in the worklet.
 * @type {Readonly<{audio: Readonly<{channelCount: number,
 *   echoCancellation: boolean, noiseSuppression: boolean}>}>}
 */
const AUDIO_CONSTRAINTS = Object.freeze({
  audio: Object.freeze({
    channelCount: 1,
    echoCancellation: true,
    noiseSuppression: true,
  }),
});

/**
 * Typed error thrown by {@link startCapture} when the engineer denies
 * microphone permission. Callers check `instanceof MicrophoneDeniedError`
 * (or `error.name === 'MicrophoneDeniedError'`) and skip opening the voice
 * WebSocket (Req 1.8).
 */
export class MicrophoneDeniedError extends Error {
  /**
   * Creates the typed denial error.
   * @param {string} message - Explanatory message for the engineer.
   */
  constructor(message) {
    super(message);
    this.name = 'MicrophoneDeniedError';
  }
}

/**
 * Live capture pipeline returned by {@link startCapture} and released by
 * {@link stopCapture}.
 * @typedef {object} CaptureHandle
 * @property {MediaStream} stream - Microphone stream whose tracks are
 *   stopped on release.
 * @property {AudioContext} context - Audio context hosting the worklet.
 * @property {MediaStreamAudioSourceNode} sourceNode - Microphone source
 *   node feeding the worklet.
 * @property {AudioWorkletNode} workletNode - PCM capture worklet node
 *   whose port delivers encoded chunks.
 */

/**
 * Tells whether a getUserMedia rejection means the engineer denied
 * microphone permission.
 * @param {unknown} error - Rejection reason from getUserMedia.
 * @returns {boolean} True for `NotAllowedError` or the legacy
 *   `PermissionDeniedError` name, false otherwise.
 */
function isPermissionDenial(error) {
  const name = error?.name;
  return name === 'NotAllowedError' || name === 'PermissionDeniedError';
}

/**
 * Renders the minimal mic-denied fallback alert into `#alert-area`,
 * replacing any mic-denied alert already displayed there. Used only when
 * no `portal:error` listener took ownership of the error; the full error
 * UI belongs to `src/ui/errors.js` (task 9.8). Content is inserted via
 * `textContent`, never markup injection.
 * @param {Document} doc - Document that hosts the alert area.
 * @returns {Element|null} The appended alert element, or null when the
 *   alert area does not exist.
 */
function renderMicDeniedFallback(doc) {
  const container = doc.getElementById('alert-area');
  if (!container) {
    return null;
  }
  container.querySelector('[data-error="mic-denied"]')?.remove();

  const alert = doc.createElement('div');
  alert.className = 'alert alert-danger';
  alert.setAttribute('role', 'alert');
  alert.dataset.error = 'mic-denied';

  const heading = doc.createElement('p');
  heading.className = 'fw-bold mb-1';
  heading.textContent = 'Microphone access required';
  alert.appendChild(heading);

  const detail = doc.createElement('p');
  detail.className = 'mb-0';
  detail.textContent = MIC_DENIED_MESSAGE;
  alert.appendChild(detail);

  container.appendChild(alert);
  return alert;
}

/**
 * Surfaces the mic-denied error to the UI: dispatches the cancelable
 * `portal:error` event on the document and, unless a listener called
 * `preventDefault()`, renders the local fallback alert. Does nothing in a
 * document-less environment.
 * @param {object} env - Environment object shaped like the browser global
 *   scope (`document`, `CustomEvent`).
 */
function reportMicrophoneDenied(env) {
  const doc = env?.document;
  if (!doc) {
    return;
  }
  const CustomEventCtor = env.CustomEvent ?? globalThis.CustomEvent;
  const event = new CustomEventCtor('portal:error', {
    cancelable: true,
    detail: { category: 'mic-denied', message: MIC_DENIED_MESSAGE },
  });
  const notHandled = doc.dispatchEvent(event);
  if (notHandled) {
    renderMicDeniedFallback(doc);
  }
}

/**
 * Starts microphone capture and streams 16 kHz Int16 PCM chunks to the
 * caller.
 *
 * Flow: `getUserMedia` (mono, echo cancellation, noise suppression) →
 * `AudioContext` at the browser's native rate → load the PCM worklet
 * module (resolved relative to this module via `import.meta.url`) →
 * MediaStreamSource → AudioWorkletNode. The worklet node is connected to
 * the context destination so the graph is always pulled; the processor
 * writes no output, so nothing is audible. Each worklet message delivers
 * one ArrayBuffer of 16 kHz 16-bit mono PCM to `onChunk` — the voice
 * WebSocket client (task 9.7) forwards these frames.
 *
 * On permission denial the error is surfaced per the module contract and
 * {@link MicrophoneDeniedError} is thrown before any audio resources are
 * created — the caller must not open the WebSocket in that case
 * (Req 1.8, 9.6). Any failure after the microphone is granted releases the
 * tracks and closes the context before rethrowing.
 * @param {object} options - Capture options.
 * @param {(chunk: ArrayBuffer) => void} options.onChunk - Receives each
 *   encoded PCM chunk as it becomes available.
 * @param {object} [options.env] - Environment object shaped like the
 *   browser global scope (`navigator`, `AudioContext`, `AudioWorkletNode`,
 *   `document`). Defaults to `globalThis`; tests may pass fakes.
 * @returns {Promise<CaptureHandle>} Resolves with the live capture
 *   pipeline once audio is flowing.
 * @throws {TypeError} If `onChunk` is not a function.
 * @throws {MicrophoneDeniedError} If the engineer denies microphone
 *   permission.
 * @throws {Error} If getUserMedia is unavailable (the capability gate in
 *   `capability.js` should refuse session start first), or when microphone
 *   or audio-graph setup fails for any other reason (original error is
 *   rethrown).
 */
export async function startCapture(options) {
  const { onChunk, env = globalThis } = options ?? {};
  if (typeof onChunk !== 'function') {
    throw new TypeError('startCapture requires an onChunk callback function');
  }
  const mediaDevices = env?.navigator?.mediaDevices;
  if (typeof mediaDevices?.getUserMedia !== 'function') {
    throw new Error(
      'getUserMedia is not available in this browser; session start ' +
        'should have been refused by the capability gate',
    );
  }

  let stream;
  try {
    stream = await mediaDevices.getUserMedia(AUDIO_CONSTRAINTS);
  } catch (error) {
    if (isPermissionDenial(error)) {
      reportMicrophoneDenied(env);
      throw new MicrophoneDeniedError(MIC_DENIED_MESSAGE);
    }
    throw error;
  }

  let context;
  try {
    context = new env.AudioContext();
    await context.audioWorklet.addModule(
      new URL('./pcm-worklet.js', import.meta.url),
    );

    const sourceNode = context.createMediaStreamSource(stream);
    const workletNode = new env.AudioWorkletNode(context, PROCESSOR_NAME, {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      channelCount: 1,
      channelCountMode: 'explicit',
    });

    /**
     * Delivers each encoded PCM chunk from the worklet to the caller.
     * @param {MessageEvent} event - Worklet message whose `data` is an
     *   ArrayBuffer of 16 kHz Int16 PCM.
     */
    workletNode.port.onmessage = (event) => {
      onChunk(event.data);
    };

    sourceNode.connect(workletNode);
    workletNode.connect(context.destination);
    if (context.state === 'suspended') {
      await context.resume();
    }
    return { stream, context, sourceNode, workletNode };
  } catch (error) {
    for (const track of stream.getTracks()) {
      track.stop();
    }
    try {
      if (context && context.state !== 'closed') {
        await context.close();
      }
    } catch {
      /* best-effort cleanup; the original setup error is what matters */
    }
    throw error;
  }
}

/**
 * Stops a capture pipeline started by {@link startCapture}: detaches the
 * worklet message handler, disconnects the audio graph, stops every
 * microphone track (releasing the device), and closes the AudioContext.
 * Safe to call with a null/undefined handle and safe to call twice.
 * @param {CaptureHandle|null|undefined} handle - Handle returned by
 *   {@link startCapture}; nullish values are ignored.
 * @returns {Promise<void>} Resolves once the context is closed.
 */
export async function stopCapture(handle) {
  if (!handle) {
    return;
  }
  const { stream, context, sourceNode, workletNode } = handle;
  if (workletNode) {
    workletNode.port.onmessage = null;
    workletNode.disconnect();
  }
  if (sourceNode) {
    sourceNode.disconnect();
  }
  if (stream) {
    for (const track of stream.getTracks()) {
      track.stop();
    }
  }
  if (context && context.state !== 'closed') {
    await context.close();
  }
}
