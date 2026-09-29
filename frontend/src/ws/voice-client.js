/**
 * Voice WebSocket client: the browser side of the voice plane.
 *
 * Connects to the CloudFront `wss://.../ws/voice` endpoint (URL from the
 * runtime configuration — never hardcoded, Req 14.3) carrying the Cognito
 * access token as the `("bearer", <jwt>)` subprotocol pair, streams
 * microphone PCM out and plays Nova Sonic PCM back:
 *
 * - client → server: binary frames of 16 kHz 16-bit mono PCM from the
 *   capture worklet (Req 1.1), plus JSON control frames
 *   `{"type":"session.start", executionId, incidentContext,
 *   resumeSessionId}` and `{"type":"session.end"}`;
 * - server → client: binary frames of 24 kHz 16-bit mono PCM fed to the
 *   playback queue (Req 1.3, 1.7), plus JSON control frames
 *   `session.state`, `transcript`, `error`, and `session.terminating`.
 *
 * Interruption and resume (Req 1.5, 8.3, 8.6): when the socket closes
 * without the engineer having ended the session, the client immediately
 * (well within the 5-second requirement) dispatches a
 * `portal:session-error` with category `connection_interrupted` and
 * unhides the `#btn-session-reconnect` button. Clicking it re-establishes
 * the connection with `resumeSessionId` set to the last session id the
 * server announced, so the Voice_Service restores the persisted session.
 * A `session_not_found` error frame (unknown or expired resume target)
 * clears the stored resume id and keeps the reconnect button hidden — the
 * engineer starts a fresh session with the Start button.
 *
 * Event contract — consumed from `document`:
 * - `portal:session-start` detail `{config, executionId?,
 *   incidentContext?}` — dispatched by `src/main.js` (Start button) and by
 *   the notification modules (incident popup / service worker) which add
 *   the scoping fields; always begins a fresh session.
 * - `portal:session-stop` — graceful end: `session.end` frame, socket
 *   close, capture and playback teardown.
 * - clicks on `#btn-session-reconnect` (delegated document listener).
 *
 * Event contract — dispatched on `document` (rendering belongs to the UI
 * modules of task 9.8, which listen for these):
 * - `portal:session-state` detail `{state, sessionId}` — one per server
 *   `session.state` frame (states connecting | live | segmenting | ended |
 *   error), plus a client-synthesized `connecting` when the socket is
 *   being opened and a client-synthesized `ended` when the engineer stops
 *   a session the server never acknowledged as ended;
 * - `portal:transcript` detail `{role, text, timestamp}` — one per server
 *   `transcript` frame (Req 1.4);
 * - `portal:session-error` detail `{category, message}` — protocol
 *   categories (auth_invalid | auth_expired | bedrock_unavailable |
 *   segmentation_failed | session_not_found | internal) pass through
 *   verbatim; the client adds `connection_interrupted` (unexpected close
 *   of a live session, Req 1.5), `connection_failed` (the socket never
 *   reached open), and `session_terminating` (server drain notice,
 *   Req 10.8).
 *
 * Session start ordering (Req 1.8): the access token is read first
 * (signed-out engineers are the route guard's concern — nothing happens
 * here), then microphone capture starts, and only after capture succeeds
 * is the WebSocket opened. {@link MicrophoneDeniedError} aborts silently
 * before any socket exists — `src/audio/capture.js` already surfaced the
 * error to the engineer.
 *
 * Integration seam: `src/main.js` imports this module for its side
 * effect — a default {@link VoiceClient} wires itself to `document` on
 * import (same pattern as `src/auth/cognito.js`). Tests construct their
 * own client via {@link initVoiceClient} with injectable seams
 * (WebSocket constructor, document, capture/playback factories, token
 * accessor).
 */

import { getFreshAccessToken } from '../auth/cognito.js';
import {
  MicrophoneDeniedError,
  startCapture,
  stopCapture,
} from '../audio/capture.js';
import { PlaybackQueue } from '../audio/playback.js';

/**
 * Event type dispatched for every voice session state change.
 * @type {string}
 */
export const SESSION_STATE_EVENT = 'portal:session-state';

/**
 * Event type dispatched for every transcript frame received.
 * @type {string}
 */
export const TRANSCRIPT_EVENT = 'portal:transcript';

/**
 * Event type dispatched for protocol error frames and client-detected
 * connection failures.
 * @type {string}
 */
export const SESSION_ERROR_EVENT = 'portal:session-error';

/**
 * Element id of the reconnect button this client shows after an
 * interruption and hides again once a session starts (contract documented
 * in `public/index.html`).
 * @type {string}
 */
export const RECONNECT_BUTTON_ID = 'btn-session-reconnect';

/**
 * WebSocket subprotocol name paired with the JWT; the token travels in
 * `Sec-WebSocket-Protocol`, never in the URL (Req 7.2).
 * @type {string}
 */
export const BEARER_SUBPROTOCOL = 'bearer';

/**
 * WebSocket readyState value for a socket still connecting.
 * @type {number}
 */
const WS_CONNECTING = 0;

/**
 * WebSocket readyState value for an open socket.
 * @type {number}
 */
const WS_OPEN = 1;

/**
 * WebSocket readyState value for a fully closed socket.
 * @type {number}
 */
const WS_CLOSED = 3;

/**
 * Message dispatched with `connection_interrupted` when a live session's
 * socket closes unexpectedly (Req 1.5).
 * @type {string}
 */
const INTERRUPTED_MESSAGE =
  'The voice connection was interrupted. Use the Reconnect button to ' +
  'resume your session.';

/**
 * Message dispatched with `connection_failed` when the socket never
 * reaches the open state (network failure or rejected upgrade).
 * @type {string}
 */
const CONNECT_FAILED_MESSAGE =
  'Could not connect to the voice service. Check your network and start ' +
  'the session again.';

/**
 * Injectable environment seams so tests can substitute fakes; every
 * property defaults to the real browser implementation.
 * @typedef {object} VoiceClientRuntime
 * @property {Function} WebSocketCtor - WebSocket constructor
 *   (`new (url, protocols) => WebSocket`-shaped) used to open the voice
 *   socket.
 * @property {object | undefined} documentRef - Document used for event
 *   listening/dispatching and the reconnect button; undefined outside a
 *   DOM (the client then no-ops all UI interaction).
 * @property {Function} CustomEventCtor - CustomEvent constructor used for
 *   dispatching the portal events.
 * @property {typeof startCapture} startCaptureFn - Microphone capture
 *   starter (`src/audio/capture.js`).
 * @property {typeof stopCapture} stopCaptureFn - Microphone capture
 *   stopper.
 * @property {() => PlaybackQueue} createPlaybackQueue - Factory for the
 *   24 kHz playback queue, one per session (`src/audio/playback.js`).
 * @property {() => Promise<string | null>} getFreshAccessTokenFn -
 *   Resolves an access token with its full lifetime ahead of it,
 *   refreshing first when possible, or null when the engineer is signed
 *   out (`src/auth/cognito.js`). Used instead of a plain read because a
 *   socket's credential is fixed at the handshake, so a session started
 *   on a nearly-expired token is killed early by the server's expiry
 *   watchdog.
 */

/**
 * Detail shape of the `portal:session-start` event and argument shape of
 * {@link VoiceClient#startSession}.
 * @typedef {object} SessionStartOptions
 * @property {object} config - Portal runtime configuration; only
 *   `voiceWsUrl` is read here.
 * @property {string | null} [executionId] - DevOps Agent execution id
 *   when the session is opened from an incident notification (Req 3.5).
 * @property {object | null} [incidentContext] - `{summary, severity}`
 *   incident context for incident-scoped sessions (Req 5.8).
 * @property {string | null} [resumeSessionId] - Voice_Session id to
 *   resume (reconnect path only, Req 8.3).
 */

/**
 * Mutable per-connection state tracked from socket creation to settle.
 * @typedef {object} ActiveSession
 * @property {object | null} ws - The WebSocket, once constructed.
 * @property {object | null} captureHandle - Live microphone pipeline from
 *   `startCapture`.
 * @property {PlaybackQueue | null} playback - Playback queue for this
 *   session's response audio.
 * @property {boolean} opened - True once the socket reached open.
 * @property {boolean} intendedClose - True once the engineer requested
 *   the stop (a following close is expected, not an interruption).
 * @property {boolean} finalized - True once the server declared the
 *   session terminal (`session.state` ended, or `session_not_found`), so
 *   the following close must not offer a reconnect.
 * @property {boolean} settled - True once teardown ran; makes settle
 *   idempotent.
 * @property {string | null} lastState - Last state dispatched to the UI,
 *   used to decide whether a synthetic `ended` is needed.
 */

/**
 * No-op rejection handler for best-effort teardown promises (stopping
 * capture, closing playback, late playback enqueues racing a close).
 * @returns {void}
 */
function ignoreTeardownFailure() {}

/**
 * Creates the default playback queue for one session.
 * @returns {PlaybackQueue} A fresh playback queue.
 */
function defaultCreatePlaybackQueue() {
  return new PlaybackQueue();
}

/**
 * Builds the default runtime bound to the browser globals and the real
 * auth/audio modules; every seam is individually overridable through the
 * {@link VoiceClient} constructor.
 * @returns {VoiceClientRuntime} Runtime seams backed by the browser
 *   environment.
 */
function createDefaultRuntime() {
  return {
    WebSocketCtor: globalThis.WebSocket,
    documentRef: globalThis.document,
    CustomEventCtor: globalThis.CustomEvent,
    startCaptureFn: startCapture,
    stopCaptureFn: stopCapture,
    createPlaybackQueue: defaultCreatePlaybackQueue,
    getFreshAccessTokenFn: getFreshAccessToken,
  };
}

/**
 * Voice WebSocket client managing one session at a time: microphone
 * capture, the authenticated socket, control-frame handling, response
 * playback, and the interruption/reconnect flow.
 */
export class VoiceClient {
  /** @type {VoiceClientRuntime} */
  #runtime;

  /** @type {ActiveSession | null} */
  #session = null;

  /**
   * Resume target: the last Voice_Session id announced by the server in a
   * `session.state` frame. Survives across connections so a reconnect can
   * resume; cleared when the server answers `session_not_found` (Req 8.6).
   * @type {string | null}
   */
  #lastSessionId = null;

  /** @type {object | null} */
  #config = null;

  /** @type {string | null} */
  #lastExecutionId = null;

  /** @type {object | null} */
  #lastIncidentContext = null;

  /** @type {boolean} */
  #wired = false;

  /**
   * Creates a client. No listeners are attached until
   * {@link VoiceClient#init}.
   * @param {Partial<VoiceClientRuntime>} [overrides] - Test seams; each
   *   property replaces the corresponding browser default.
   */
  constructor(overrides = {}) {
    this.#runtime = { ...createDefaultRuntime(), ...overrides };
  }

  /**
   * Reports whether a session is currently active (from start request
   * until the connection settles).
   * @returns {boolean} True while a session is active.
   */
  get sessionActive() {
    return this.#session !== null;
  }

  /**
   * Returns the current resume target (introspection for tests).
   * @returns {string | null} The last server-announced session id, or
   *   null when there is nothing to resume.
   */
  get resumeSessionId() {
    return this.#lastSessionId;
  }

  /**
   * Attaches the document listeners: `portal:session-start`,
   * `portal:session-stop`, and the delegated reconnect-button click.
   * Safe to call more than once; no-ops without a document.
   * @returns {VoiceClient} This client, for chaining.
   */
  init() {
    const doc = this.#runtime.documentRef;
    if (!doc || this.#wired) {
      return this;
    }
    doc.addEventListener('portal:session-start', this.#onSessionStartEvent);
    doc.addEventListener('portal:session-stop', this.#onSessionStopEvent);
    doc.addEventListener('portal:session-text', this.#onSessionTextEvent);
    doc.addEventListener('click', this.#onDocumentClick);
    this.#wired = true;
    return this;
  }

  /**
   * Detaches the document listeners and stops any active session (test
   * and teardown hook).
   * @returns {void}
   */
  dispose() {
    const doc = this.#runtime.documentRef;
    if (doc && this.#wired) {
      doc.removeEventListener(
        'portal:session-start',
        this.#onSessionStartEvent,
      );
      doc.removeEventListener('portal:session-stop', this.#onSessionStopEvent);
      doc.removeEventListener('click', this.#onDocumentClick);
    }
    this.#wired = false;
    this.stopSession();
  }

  /**
   * Starts a voice session: reads the access token (signed-out engineers
   * are handled by the auth route guard — this returns silently), starts
   * microphone capture, and only then opens the WebSocket with the
   * bearer subprotocol and sends the `session.start` frame (Req 1.1,
   * 1.8). At most one session is active at a time; a start request while
   * one is active is ignored.
   * @param {SessionStartOptions} options - Session parameters; `config`
   *   must carry `voiceWsUrl`.
   * @returns {Promise<void>} Resolves when the start attempt has been
   *   made (or refused); never rejects for expected failures — those
   *   surface as `portal:session-error` events.
   */
  async startSession(options) {
    const {
      config,
      executionId = null,
      incidentContext = null,
      resumeSessionId = null,
    } = options ?? {};
    if (this.#session) {
      return;
    }
    const wsUrl = config?.voiceWsUrl;
    if (typeof wsUrl !== 'string' || wsUrl === '') {
      console.error(
        'voice-client: session start refused — config.voiceWsUrl is ' +
          'missing (bootstrap contract broken).',
      );
      return;
    }
    // Refresh before the handshake, not just read: the socket keeps this
    // exact token for its whole life, so starting on a nearly-expired one
    // means the server's expiry watchdog ends the session early.
    const token = await this.#runtime.getFreshAccessTokenFn();
    if (token === null || token === undefined) {
      // Not authenticated: the cognito.js route guard owns messaging and
      // redirects to sign-in; opening an unauthenticated socket would
      // just be rejected server-side (Req 7.2).
      return;
    }

    /** @type {ActiveSession} */
    const session = {
      ws: null,
      captureHandle: null,
      playback: null,
      opened: false,
      intendedClose: false,
      finalized: false,
      settled: false,
      lastState: null,
    };
    this.#session = session;
    this.#config = config;
    this.#lastExecutionId = executionId;
    this.#lastIncidentContext = incidentContext;
    this.#setReconnectVisible(false);

    // Capture first: microphone permission denial must abort before any
    // socket exists (Req 1.8). capture.js already rendered the error.
    let captureHandle;
    try {
      captureHandle = await this.#runtime.startCaptureFn({
        onChunk: this.#onCaptureChunk,
      });
    } catch (error) {
      if (this.#session === session) {
        this.#session = null;
      }
      if (
        error instanceof MicrophoneDeniedError ||
        error?.name === 'MicrophoneDeniedError'
      ) {
        return;
      }
      this.#dispatch(SESSION_ERROR_EVENT, {
        category: 'internal',
        message: `Microphone setup failed: ${error?.message ?? error}`,
      });
      return;
    }
    if (this.#session !== session) {
      // The engineer stopped while the permission prompt was open —
      // release the microphone and never open the socket.
      this.#safeTeardownStep(() => this.#runtime.stopCaptureFn(captureHandle));
      return;
    }
    session.captureHandle = captureHandle;

    session.lastState = 'connecting';
    this.#dispatch(SESSION_STATE_EVENT, {
      state: 'connecting',
      sessionId: resumeSessionId,
    });

    const startFrame = {
      type: 'session.start',
      executionId,
      incidentContext,
      resumeSessionId,
    };
    try {
      session.playback = this.#runtime.createPlaybackQueue();
      const ws = new this.#runtime.WebSocketCtor(wsUrl, [
        BEARER_SUBPROTOCOL,
        token,
      ]);
      ws.binaryType = 'arraybuffer';
      session.ws = ws;
      /**
       * Open handler: announces the session to the server (Req 1.1).
       * @returns {void}
       */
      ws.onopen = () => this.#handleOpen(session, startFrame);
      /**
       * Message handler: routes binary audio and JSON control frames.
       * @param {MessageEvent} event - Incoming WebSocket message.
       * @returns {void}
       */
      ws.onmessage = (event) => this.#handleMessage(session, event);
      /**
       * Close handler: settles the session (cleanup + outcome events).
       * @returns {void}
       */
      ws.onclose = () => this.#settle(session);
      /**
       * Error handler: intentionally empty — the browser always fires
       * `close` after `error`, and settle handles the outcome there.
       * @returns {void}
       */
      ws.onerror = () => {};
    } catch {
      // Socket or playback construction failed before any connection
      // existed; settle dispatches `connection_failed`.
      this.#settle(session);
    }
  }

  /**
   * Sends one typed engineer request as a `text.input` control frame.
   *
   * Typing complements speaking: identifiers such as instance ids are
   * easier to type than to pronounce, so a typed request enters the same
   * conversation mid-session. The server forwards it to Nova Sonic as a
   * user turn, answers by voice, and echoes it back as a `transcript`
   * frame — so this method deliberately does not render anything itself,
   * keeping the transcript a single server-owned source of truth.
   * @param {string} text - The typed request; blank or non-string values
   *   and calls outside an open session are ignored.
   * @returns {boolean} True when the frame was handed to the socket.
   */
  sendText(text) {
    if (typeof text !== 'string' || text.trim() === '') {
      return false;
    }
    const ws = this.#session?.ws;
    if (!ws || ws.readyState !== WS_OPEN) {
      return false;
    }
    try {
      ws.send(JSON.stringify({ type: 'text.input', text }));
    } catch {
      // Socket raced into closing; the close handler settles the session.
      return false;
    }
    return true;
  }

  /**
   * Gracefully ends the active session: sends `session.end` when the
   * socket is open, closes the socket, and lets settle stop capture and
   * close playback. No-op when no session is active.
   * @returns {void}
   */
  stopSession() {
    const session = this.#session;
    if (!session || session.settled) {
      return;
    }
    session.intendedClose = true;
    const ws = session.ws;
    if (!ws) {
      // Still waiting on the microphone prompt — no socket ever existed.
      this.#settle(session);
      return;
    }
    if (ws.readyState === WS_OPEN) {
      try {
        ws.send(JSON.stringify({ type: 'session.end' }));
      } catch {
        // Socket raced into closing; the close below still applies.
      }
    }
    if (ws.readyState === WS_CONNECTING || ws.readyState === WS_OPEN) {
      ws.close(1000, 'session ended by engineer');
    } else if (ws.readyState === WS_CLOSED) {
      // Defensive: close event already fired or was never delivered.
      this.#settle(session);
    }
  }

  /**
   * Capture callback bound to this client: forwards one 16 kHz 16-bit
   * mono PCM chunk to the active session's socket when it is open; chunks
   * produced while no socket is open (still connecting, closing, or
   * between sessions) are dropped (Req 1.1).
   * @param {ArrayBuffer} chunk - Encoded PCM chunk from the capture
   *   worklet.
   * @returns {void}
   */
  #onCaptureChunk = (chunk) => {
    const ws = this.#session?.ws;
    if (!ws || ws.readyState !== WS_OPEN) {
      return;
    }
    try {
      ws.send(chunk);
    } catch {
      // Socket raced into closing between the check and the send — drop.
    }
  };

  /**
   * `portal:session-start` listener: begins a fresh session with the
   * config and optional incident scoping carried in the event detail.
   * @param {CustomEvent} event - Event whose detail is
   *   {@link SessionStartOptions} without `resumeSessionId`.
   * @returns {void}
   */
  #onSessionStartEvent = (event) => {
    const detail = event?.detail ?? {};
    const config = detail.config ?? this.#config;
    if (!config) {
      console.error(
        'voice-client: portal:session-start carried no config and none ' +
          'was stored; bootstrap contract broken.',
      );
      return;
    }
    this.startSession({
      config,
      executionId: detail.executionId ?? null,
      incidentContext: detail.incidentContext ?? null,
      resumeSessionId: null,
    }).catch((error) => {
      console.error('voice-client: session start failed unexpectedly.', error);
    });
  };

  /**
   * `portal:session-stop` listener: gracefully ends the active session.
   * @returns {void}
   */
  #onSessionStopEvent = () => {
    this.stopSession();
  };

  /**
   * `portal:session-text` listener: sends one typed engineer request on
   * the open voice socket.
   * @param {CustomEvent<{text: string}>} event - Event whose detail
   *   carries the typed `text`.
   * @returns {void}
   */
  #onSessionTextEvent = (event) => {
    this.sendText(event?.detail?.text);
  };

  /**
   * Delegated document click listener for the reconnect button: when the
   * engineer clicks `#btn-session-reconnect`, re-establishes the
   * connection with `resumeSessionId` set to the last announced session
   * id (Req 1.5). Delegation (rather than a direct button listener)
   * keeps the wiring valid even if the shell re-renders the button.
   * @param {MouseEvent} event - Document-level click event.
   * @returns {void}
   */
  #onDocumentClick = (event) => {
    const target = event?.target;
    if (
      typeof target?.closest !== 'function' ||
      !target.closest(`#${RECONNECT_BUTTON_ID}`)
    ) {
      return;
    }
    this.#handleReconnectClick();
  };

  /**
   * Reconnect button action: hides the button and starts a session that
   * resumes the interrupted one. Uses the stored config and incident
   * scoping from the interrupted session; the token is re-read at click
   * time, so a token refreshed since the interruption is picked up.
   * @returns {void}
   */
  #handleReconnectClick() {
    if (this.#session || !this.#config) {
      return;
    }
    this.#setReconnectVisible(false);
    this.startSession({
      config: this.#config,
      executionId: this.#lastExecutionId,
      incidentContext: this.#lastIncidentContext,
      resumeSessionId: this.#lastSessionId,
    }).catch((error) => {
      console.error('voice-client: reconnect failed unexpectedly.', error);
    });
  }

  /**
   * Socket open handler: marks the session live on the wire and sends the
   * `session.start` control frame with the incident scoping and resume
   * target (protocol: first client frame after the upgrade).
   * @param {ActiveSession} session - Session owning the socket.
   * @param {object} startFrame - Prepared `session.start` frame.
   * @returns {void}
   */
  #handleOpen(session, startFrame) {
    if (session.settled || session.intendedClose) {
      return;
    }
    session.opened = true;
    try {
      session.ws.send(JSON.stringify(startFrame));
    } catch {
      // Socket raced into closing; the close handler settles the session.
    }
  }

  /**
   * Socket message handler: binary frames are 24 kHz PCM response audio
   * fed to the playback queue (Req 1.3, 1.7); text frames are JSON
   * control messages. Malformed control frames are dropped — a protocol
   * violation the client cannot act on.
   * @param {ActiveSession} session - Session owning the socket.
   * @param {MessageEvent} event - Incoming WebSocket message.
   * @returns {void}
   */
  #handleMessage(session, event) {
    if (session.settled) {
      return;
    }
    const data = event?.data;
    if (data instanceof ArrayBuffer) {
      try {
        void session.playback
          ?.enqueue(data)
          .catch(ignoreTeardownFailure);
      } catch {
        // Queue already closed — a late frame racing teardown; drop it.
      }
      return;
    }
    if (typeof data !== 'string') {
      return;
    }
    let frame;
    try {
      frame = JSON.parse(data);
    } catch {
      return;
    }
    this.#handleControlFrame(session, frame);
  }

  /**
   * Routes one parsed server control frame per the WebSocket protocol.
   * Unknown frame types are ignored for forward compatibility.
   * @param {ActiveSession} session - Session the frame belongs to.
   * @param {object} frame - Parsed JSON control frame.
   * @returns {void}
   */
  #handleControlFrame(session, frame) {
    switch (frame?.type) {
      case 'session.state': {
        const sessionId =
          typeof frame.sessionId === 'string' && frame.sessionId !== ''
            ? frame.sessionId
            : null;
        if (sessionId) {
          this.#lastSessionId = sessionId;
        }
        if (frame.state === 'ended') {
          session.finalized = true;
        }
        session.lastState = frame.state;
        this.#dispatch(SESSION_STATE_EVENT, {
          state: frame.state,
          sessionId,
        });
        break;
      }
      case 'transcript':
        this.#dispatch(TRANSCRIPT_EVENT, {
          role: frame.role,
          text: frame.text,
          timestamp: frame.timestamp,
        });
        break;
      case 'error':
        this.#handleErrorFrame(session, frame);
        break;
      case 'interrupted':
        // Barge-in (audio/playback.js contract): the engineer spoke over
        // the assistant, so already-scheduled speech for the cancelled
        // response must stop immediately instead of playing over the new
        // turn. clear() flushes the queue but keeps it usable for the
        // next response.
        session.playback?.clear();
        break;
      case 'session.terminating':
        // Server drain notice (Req 10.8): the close that follows takes
        // the interruption path, so the reconnect option appears and the
        // session resumes on a healthy task.
        this.#dispatch(SESSION_ERROR_EVENT, {
          category: 'session_terminating',
          message:
            'The voice service is closing this connection' +
            (frame.reason ? ` (${frame.reason})` : '') +
            '. You can reconnect to continue.',
        });
        break;
      default:
        break;
    }
  }

  /**
   * Handles a server `error` frame: relays it to the UI and, when the
   * server marked it unrecoverable, marks the session terminal so the
   * close that follows offers a fresh start instead of a reconnect.
   *
   * `recoverable: false` is the server's own statement that nothing can
   * be retried on this session, so it — not a hardcoded category list —
   * decides whether the Reconnect affordance appears. Without this,
   * settle treated every server-initiated close as an interruption: an
   * expired sign-in produced a red "Session expired" alert *and* a yellow
   * "Connection interrupted" alert plus a Reconnect button that offered
   * recovery via the very credential the server had just rejected (and
   * which failed silently when clicked).
   *
   * `session_not_found` additionally clears the resume target, because
   * that specific id is what the server could not find (Req 8.6), and is
   * treated as terminal on its category alone rather than trusting the
   * flag to be present.
   * @param {ActiveSession} session - Session the frame belongs to.
   * @param {object} frame - Parsed `error` frame with `category`,
   *   `message`, and `recoverable`.
   * @returns {void}
   */
  #handleErrorFrame(session, frame) {
    const category =
      typeof frame.category === 'string' && frame.category !== ''
        ? frame.category
        : 'internal';
    // `session_not_found` stays terminal on its category alone: that
    // resume target can never work again, so the affordance must go even
    // if the flag is missing from the frame.
    const recoverable =
      frame.recoverable !== false && category !== 'session_not_found';
    this.#dispatch(SESSION_ERROR_EVENT, {
      category,
      message: typeof frame.message === 'string' ? frame.message : '',
      recoverable,
    });
    if (category === 'session_not_found') {
      this.#lastSessionId = null;
    }
    if (!recoverable) {
      session.finalized = true;
      this.#setReconnectVisible(false);
    }
  }

  /**
   * Settles a session exactly once: releases the microphone, closes the
   * playback queue, detaches the session, and dispatches the outcome —
   * a synthetic `ended` state when the engineer stopped a session the
   * server never acknowledged, `connection_failed` when the socket never
   * opened, or `connection_interrupted` plus the reconnect button when a
   * live session dropped (Req 1.5; the notification is dispatched
   * synchronously from the close event, well within 5 seconds).
   * @param {ActiveSession} session - Session to settle.
   * @returns {void}
   */
  #settle(session) {
    if (session.settled) {
      return;
    }
    session.settled = true;
    if (this.#session === session) {
      this.#session = null;
    }
    if (session.captureHandle) {
      const handle = session.captureHandle;
      this.#safeTeardownStep(() => this.#runtime.stopCaptureFn(handle));
    }
    if (session.playback) {
      const playback = session.playback;
      this.#safeTeardownStep(() => playback.close());
    }

    if (session.intendedClose || session.finalized) {
      if (
        session.intendedClose &&
        session.lastState !== null &&
        session.lastState !== 'ended'
      ) {
        // The engineer ended the session but the server's `ended` state
        // never arrived (e.g. stop while still connecting) — settle the
        // status badge anyway.
        this.#dispatch(SESSION_STATE_EVENT, {
          state: 'ended',
          sessionId: this.#lastSessionId,
        });
      }
      return;
    }
    if (!session.opened) {
      this.#dispatch(SESSION_ERROR_EVENT, {
        category: 'connection_failed',
        message: CONNECT_FAILED_MESSAGE,
      });
      return;
    }
    this.#dispatch(SESSION_ERROR_EVENT, {
      category: 'connection_interrupted',
      message: INTERRUPTED_MESSAGE,
    });
    this.#setReconnectVisible(true);
  }

  /**
   * Runs one best-effort teardown step, swallowing synchronous throws and
   * promise rejections — teardown must never mask the session outcome.
   * @param {() => (Promise<void> | void)} step - Teardown action; may
   *   return a promise.
   * @returns {void}
   */
  #safeTeardownStep(step) {
    try {
      void Promise.resolve(step()).catch(ignoreTeardownFailure);
    } catch {
      // Synchronous teardown failure — nothing more to release.
    }
  }

  /**
   * Shows or hides the reconnect button by toggling Bootstrap's `d-none`
   * class (the button ships hidden in `index.html`). No-op without a
   * document or when the button is absent.
   * @param {boolean} visible - True to show the button, false to hide it.
   * @returns {void}
   */
  #setReconnectVisible(visible) {
    const button = this.#runtime.documentRef?.getElementById?.(
      RECONNECT_BUTTON_ID,
    );
    if (!button?.classList) {
      return;
    }
    button.classList.toggle('d-none', !visible);
  }

  /**
   * Dispatches a CustomEvent on the document for the UI modules
   * (task 9.8) to render. No-op without a document.
   * @param {string} type - Event type, e.g. {@link SESSION_STATE_EVENT}.
   * @param {object} detail - Event detail payload per the module
   *   contract.
   * @returns {void}
   */
  #dispatch(type, detail) {
    const doc = this.#runtime.documentRef;
    if (!doc) {
      return;
    }
    const CustomEventCtor =
      this.#runtime.CustomEventCtor ?? globalThis.CustomEvent;
    doc.dispatchEvent(new CustomEventCtor(type, { detail }));
  }
}

/**
 * Constructs a {@link VoiceClient} and attaches its document listeners.
 * @param {Partial<VoiceClientRuntime>} [overrides] - Test seams; each
 *   property replaces the corresponding browser default.
 * @returns {VoiceClient} The initialized client.
 */
export function initVoiceClient(overrides = {}) {
  return new VoiceClient(overrides).init();
}

// Self-wire on import (side-effect pattern shared with auth/cognito.js):
// the default client listens for the portal session lifecycle events
// dispatched by src/main.js.
if (typeof document !== 'undefined') {
  initVoiceClient();
}
