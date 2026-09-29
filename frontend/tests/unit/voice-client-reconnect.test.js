/**
 * Unit tests for voice WebSocket interruption and reconnect (task 9.12).
 *
 * Covers Req 1.5 — an interrupted WebSocket connection surfaces a
 * notification synchronously (well within the 5-second contract) and a
 * reconnect option that re-establishes the connection — and Req 8.6 —
 * the reconnect carries `resumeSessionId`, and a `session_not_found`
 * answer clears the resume target without offering another reconnect.
 *
 * A {@link VoiceClient} is constructed per test with injected seams: a
 * recording fake WebSocket, stubbed capture/playback, and a detached
 * document hosting the reconnect button, so nothing leaks to the client
 * self-wired to the global document.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  BEARER_SUBPROTOCOL,
  RECONNECT_BUTTON_ID,
  SESSION_ERROR_EVENT,
  SESSION_STATE_EVENT,
  VoiceClient,
} from '../../src/ws/voice-client.js';

/**
 * Recording WebSocket fake: captures constructor arguments and sent
 * frames, and exposes helpers that simulate server-side events.
 */
class FakeWebSocket {
  /** @type {FakeWebSocket[]} */
  static instances = [];

  /**
   * Records the connection attempt in the CONNECTING state.
   * @param {string} url - WebSocket URL.
   * @param {string[]} protocols - Requested subprotocols.
   */
  constructor(url, protocols) {
    this.url = url;
    this.protocols = protocols;
    this.readyState = 0;
    this.binaryType = '';
    /** @type {*[]} */
    this.sent = [];
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
    FakeWebSocket.instances.push(this);
  }

  /**
   * Records an outbound frame.
   * @param {*} data - Frame payload (JSON string or binary).
   * @returns {void}
   */
  send(data) {
    this.sent.push(data);
  }

  /**
   * Client-initiated close: settles the socket and fires the close
   * handler like a browser socket would.
   * @returns {void}
   */
  close() {
    this.readyState = 3;
    this.onclose?.();
  }

  /**
   * Simulates the socket reaching the open state.
   * @returns {void}
   */
  simulateOpen() {
    this.readyState = 1;
    this.onopen?.();
  }

  /**
   * Simulates an incoming server message.
   * @param {*} data - Message payload.
   * @returns {void}
   */
  simulateMessage(data) {
    this.onmessage?.({ data });
  }

  /**
   * Simulates an unexpected connection drop (server or network side).
   * @returns {void}
   */
  simulateInterruption() {
    this.readyState = 3;
    this.onclose?.();
  }

  /**
   * Returns the JSON control frames sent so far, parsed.
   * @returns {object[]} The parsed frames in send order.
   */
  controlFrames() {
    return this.sent
      .filter((data) => typeof data === 'string')
      .map((data) => JSON.parse(data));
  }
}

/**
 * Creates a detached document hosting the (initially hidden) reconnect
 * button and recording every session-state and session-error event
 * dispatched by the client under test.
 * @returns {{doc: Document, button: Element, errors: object[],
 *   states: object[]}} The isolated document, the reconnect button, and
 *   the captured event details.
 */
function createVoiceHost() {
  const doc = document.implementation.createHTMLDocument('voice-test');
  const button = doc.createElement('button');
  button.id = RECONNECT_BUTTON_ID;
  button.className = 'btn d-none';
  doc.body.appendChild(button);
  const errors = [];
  const states = [];
  doc.addEventListener(SESSION_ERROR_EVENT, (event) =>
    errors.push(event.detail),
  );
  doc.addEventListener(SESSION_STATE_EVENT, (event) =>
    states.push(event.detail),
  );
  return { doc, button, errors, states };
}

/**
 * Constructs an initialized {@link VoiceClient} bound to the given
 * detached document, with fake WebSocket, capture, playback, and token
 * seams.
 * @param {Document} doc - Detached document from {@link createVoiceHost}.
 * @returns {VoiceClient} The initialized client under test.
 */
function createClient(doc) {
  return new VoiceClient({
    WebSocketCtor: FakeWebSocket,
    documentRef: doc,
    startCaptureFn: vi.fn(async () => ({ stream: 'fake-capture' })),
    stopCaptureFn: vi.fn(),
    createPlaybackQueue: vi.fn(() => ({
      enqueue: vi.fn(async () => {}),
      close: vi.fn(),
    })),
    getFreshAccessTokenFn: vi.fn(async () => 'test-jwt'),
  }).init();
}

/**
 * Runtime configuration stub carrying only the voice WebSocket URL.
 * @type {object}
 */
const CONFIG = { voiceWsUrl: 'wss://portal.example/ws/voice' };

beforeEach(() => {
  FakeWebSocket.instances = [];
});

describe('WebSocket interruption and reconnect (Req 1.5, 8.6)', () => {
  it('an unexpected close notifies immediately and reveals the reconnect button', async () => {
    const { doc, button, errors } = createVoiceHost();
    createClient(doc);

    doc.dispatchEvent(
      new CustomEvent('portal:session-start', { detail: { config: CONFIG } }),
    );
    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const ws = FakeWebSocket.instances[0];
    ws.simulateOpen();
    ws.simulateMessage(
      JSON.stringify({
        type: 'session.state',
        state: 'live',
        sessionId: 'sess-1',
      }),
    );

    expect(button.classList.contains('d-none')).toBe(true);
    ws.simulateInterruption();

    // The notification is dispatched synchronously from the close event,
    // trivially within the 5-second contract of Req 1.5.
    const interruption = errors.find(
      (detail) => detail.category === 'connection_interrupted',
    );
    expect(interruption).toBeDefined();
    expect(interruption.message).toContain('Reconnect');
    expect(button.classList.contains('d-none')).toBe(false);
  });

  it('clicking reconnect re-establishes the connection with resumeSessionId', async () => {
    const { doc, button, states } = createVoiceHost();
    const client = createClient(doc);

    await client.startSession({ config: CONFIG });
    const first = FakeWebSocket.instances[0];
    first.simulateOpen();
    first.simulateMessage(
      JSON.stringify({
        type: 'session.state',
        state: 'live',
        sessionId: 'sess-42',
      }),
    );
    first.simulateInterruption();
    expect(client.resumeSessionId).toBe('sess-42');
    expect(button.classList.contains('d-none')).toBe(false);

    button.click();
    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2));
    const second = FakeWebSocket.instances[1];

    // The button hides again while the reconnect attempt runs, and the
    // client re-announces the connecting state.
    expect(button.classList.contains('d-none')).toBe(true);
    expect(states.at(-1)).toEqual({
      state: 'connecting',
      sessionId: 'sess-42',
    });

    // The new socket carries the bearer subprotocol pair, and its
    // session.start frame resumes the interrupted session (Req 8.3).
    expect(second.protocols).toEqual([BEARER_SUBPROTOCOL, 'test-jwt']);
    second.simulateOpen();
    expect(second.controlFrames()[0]).toEqual({
      type: 'session.start',
      executionId: null,
      incidentContext: null,
      resumeSessionId: 'sess-42',
    });
  });

  it('a fresh session start sends no resumeSessionId', async () => {
    const { doc } = createVoiceHost();
    const client = createClient(doc);

    await client.startSession({ config: CONFIG });
    const ws = FakeWebSocket.instances[0];
    ws.simulateOpen();

    expect(ws.controlFrames()[0]).toEqual({
      type: 'session.start',
      executionId: null,
      incidentContext: null,
      resumeSessionId: null,
    });
    expect(client.resumeSessionId).toBeNull();
  });

  it('session_not_found clears the resume target and does not offer another reconnect', async () => {
    const { doc, button, errors } = createVoiceHost();
    const client = createClient(doc);

    // Establish a session and lose it, so a resume target exists.
    await client.startSession({ config: CONFIG });
    const first = FakeWebSocket.instances[0];
    first.simulateOpen();
    first.simulateMessage(
      JSON.stringify({
        type: 'session.state',
        state: 'live',
        sessionId: 'sess-9',
      }),
    );
    first.simulateInterruption();
    expect(client.resumeSessionId).toBe('sess-9');

    // Reconnect: the server answers that the session expired (Req 8.6).
    button.click();
    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2));
    const second = FakeWebSocket.instances[1];
    second.simulateOpen();
    second.simulateMessage(
      JSON.stringify({
        type: 'error',
        category: 'session_not_found',
        message: 'The voice session is not available.',
      }),
    );

    const notFound = errors.find(
      (detail) => detail.category === 'session_not_found',
    );
    expect(notFound).toBeDefined();
    expect(client.resumeSessionId).toBeNull();

    // The close that follows must not re-offer a reconnect: the session
    // is terminal and the engineer starts fresh.
    const errorCountBeforeClose = errors.length;
    second.simulateInterruption();
    expect(button.classList.contains('d-none')).toBe(true);
    expect(
      errors
        .slice(errorCountBeforeClose)
        .some((detail) => detail.category === 'connection_interrupted'),
    ).toBe(false);
  });

  it('an unrecoverable server frame is terminal and offers no reconnect', async () => {
    const { doc, button, errors } = createVoiceHost();
    const client = createClient(doc);

    await client.startSession({ config: CONFIG });
    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const ws = FakeWebSocket.instances[0];
    ws.simulateOpen();
    ws.simulateMessage(
      JSON.stringify({
        type: 'session.state',
        state: 'live',
        sessionId: 'sess-idle',
      }),
    );

    // The service ends a quiet session: recoverable false says nothing can
    // be retried here, so the close that follows must not be dressed up as
    // an interruption with a Reconnect button the credential cannot serve.
    ws.simulateMessage(
      JSON.stringify({
        type: 'error',
        category: 'idle_timeout',
        message: 'The session ended because no request was received.',
        recoverable: false,
      }),
    );

    const idle = errors.find((detail) => detail.category === 'idle_timeout');
    expect(idle).toBeDefined();
    expect(idle.recoverable).toBe(false);

    const errorCountBeforeClose = errors.length;
    ws.simulateInterruption();
    expect(button.classList.contains('d-none')).toBe(true);
    expect(
      errors
        .slice(errorCountBeforeClose)
        .some((detail) => detail.category === 'connection_interrupted'),
    ).toBe(false);
  });

  it('a recoverable server frame leaves the session and reconnect intact', async () => {
    const { doc, button, errors } = createVoiceHost();
    const client = createClient(doc);

    await client.startSession({ config: CONFIG });
    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const ws = FakeWebSocket.instances[0];
    ws.simulateOpen();
    ws.simulateMessage(
      JSON.stringify({
        type: 'session.state',
        state: 'live',
        sessionId: 'sess-warn',
      }),
    );

    // The idle warning is advisory: the session is still live, so it must
    // not be treated as terminal.
    ws.simulateMessage(
      JSON.stringify({
        type: 'error',
        category: 'idle_warning',
        message: 'You have been quiet for a while.',
        recoverable: true,
      }),
    );

    const warning = errors.find((detail) => detail.category === 'idle_warning');
    expect(warning).toBeDefined();
    expect(warning.recoverable).toBe(true);

    // An interruption after an advisory notice still offers a reconnect.
    ws.simulateInterruption();
    expect(button.classList.contains('d-none')).toBe(false);
    expect(
      errors.some((detail) => detail.category === 'connection_interrupted'),
    ).toBe(true);
  });
});
