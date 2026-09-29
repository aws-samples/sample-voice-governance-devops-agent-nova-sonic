/**
 * Unit tests for typed engineer input alongside voice.
 *
 * Covers the client half of the feature: `VoiceClient.sendText` emits a
 * `text.input` control frame on the open voice socket, refuses blank text
 * and closed sockets, responds to the `portal:session-text` event, and
 * renders nothing locally (the server echoes a `transcript` frame, keeping
 * the transcript a single source of truth). Also covers the enablement
 * rule: the typed-input controls are operable exactly while a session is
 * underway.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

import { VoiceClient } from '../../src/ws/voice-client.js';
import { IN_SESSION_CONTROL_IDS } from '../../src/ui/session-controls.js';

/** Minimal recording WebSocket fake. */
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
   * @param {*} data - Frame payload.
   * @returns {void}
   */
  send(data) {
    this.sent.push(data);
  }

  /**
   * Client-initiated close.
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
 * Starts a session on a detached document and opens its socket.
 * @returns {Promise<{client: VoiceClient, ws: FakeWebSocket, doc: Document,
 *   transcripts: object[]}>} The client under test, its open socket, the
 *   host document, and captured transcript events.
 */
async function startOpenSession() {
  const doc = document.implementation.createHTMLDocument('text-input-test');
  const transcripts = [];
  doc.addEventListener('portal:transcript', (event) =>
    transcripts.push(event.detail),
  );
  const client = new VoiceClient({
    WebSocketCtor: FakeWebSocket,
    documentRef: doc,
    startCaptureFn: vi.fn(async () => ({ stream: 'fake' })),
    stopCaptureFn: vi.fn(),
    createPlaybackQueue: vi.fn(() => ({
      enqueue: vi.fn(async () => {}),
      clear: vi.fn(),
      close: vi.fn(),
    })),
    getFreshAccessTokenFn: vi.fn(async () => 'test-jwt'),
  }).init();
  await client.startSession({
    config: { voiceWsUrl: 'wss://portal.example/ws/voice' },
  });
  const ws = FakeWebSocket.instances.at(-1);
  ws.simulateOpen();
  return { client, ws, doc, transcripts };
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  document.body.innerHTML = '';
});

describe('typed input alongside voice', () => {
  it('sends a text.input frame on the open socket', async () => {
    const { client, ws } = await startOpenSession();

    expect(client.sendText('describe instance i-0abc123')).toBe(true);
    expect(ws.controlFrames().at(-1)).toEqual({
      type: 'text.input',
      text: 'describe instance i-0abc123',
    });
  });

  it('renders nothing locally — the server echoes the transcript', async () => {
    const { client, transcripts } = await startOpenSession();

    client.sendText('list my instances');
    expect(transcripts).toEqual([]);
  });

  it('refuses blank text and non-strings', async () => {
    const { client, ws } = await startOpenSession();
    const before = ws.controlFrames().length;

    expect(client.sendText('   ')).toBe(false);
    expect(client.sendText('')).toBe(false);
    expect(client.sendText(undefined)).toBe(false);
    expect(client.sendText(42)).toBe(false);
    expect(ws.controlFrames()).toHaveLength(before);
  });

  it('refuses to send when no session is open', () => {
    const doc = document.implementation.createHTMLDocument('no-session');
    const client = new VoiceClient({
      WebSocketCtor: FakeWebSocket,
      documentRef: doc,
      getFreshAccessTokenFn: vi.fn(async () => 'test-jwt'),
    }).init();

    expect(client.sendText('anything')).toBe(false);
    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it('sends on a portal:session-text event', async () => {
    const { ws, doc } = await startOpenSession();

    doc.dispatchEvent(
      new CustomEvent('portal:session-text', {
        detail: { text: 'why is latency high' },
      }),
    );

    expect(ws.controlFrames().at(-1)).toEqual({
      type: 'text.input',
      text: 'why is latency high',
    });
  });
});

describe('typed-input control enablement', () => {
  it('enables the field and Send only while a session is underway', async () => {
    await import('../../src/ui/session-controls.js');
    document.body.innerHTML = IN_SESSION_CONTROL_IDS.map((id) =>
      id === 'text-input'
        ? `<input id="${id}" disabled />`
        : `<button id="${id}" disabled></button>`,
    ).join('');

    document.dispatchEvent(
      new CustomEvent('portal:session-state', { detail: { state: 'live' } }),
    );
    for (const id of IN_SESSION_CONTROL_IDS) {
      expect(document.getElementById(id).disabled).toBe(false);
    }

    document.dispatchEvent(
      new CustomEvent('portal:session-state', { detail: { state: 'ended' } }),
    );
    for (const id of IN_SESSION_CONTROL_IDS) {
      expect(document.getElementById(id).disabled).toBe(true);
    }
  });
});
