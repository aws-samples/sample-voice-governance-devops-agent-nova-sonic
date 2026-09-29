/**
 * Unit tests for barge-in playback flushing in the voice client.
 *
 * When Nova Sonic reports that the engineer interrupted the spoken
 * response, the Voice_Service relays an `interrupted` control frame; the
 * client must flush its playback queue (`PlaybackQueue#clear`,
 * `src/audio/playback.js`) so already-scheduled speech stops immediately
 * instead of playing over the engineer's new turn. A {@link VoiceClient}
 * is constructed per test with
 * injected seams: a recording fake WebSocket, stubbed capture, and a
 * spied playback queue on a detached document.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

import { VoiceClient } from '../../src/ws/voice-client.js';

/**
 * Recording WebSocket fake: captures constructor arguments and exposes
 * helpers that simulate server-side events.
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
}

/**
 * Spied playback queue matching the `PlaybackQueue` surface the voice
 * client drives: enqueue, clear (barge-in), and close.
 * @returns {{enqueue: import('vitest').Mock, clear: import('vitest').Mock,
 *   close: import('vitest').Mock}} The recording fake queue.
 */
function createFakePlayback() {
  return {
    enqueue: vi.fn(async () => {}),
    clear: vi.fn(),
    close: vi.fn(),
  };
}

/**
 * Constructs an initialized {@link VoiceClient} on a detached document
 * with fake WebSocket, capture, playback, and token seams, then starts a
 * session and opens its socket.
 * @returns {Promise<{ws: FakeWebSocket, playback: ReturnType<typeof
 *   createFakePlayback>}>} The open fake socket and its playback spy.
 */
async function startOpenSession() {
  const doc = document.implementation.createHTMLDocument('barge-in-test');
  const playback = createFakePlayback();
  const client = new VoiceClient({
    WebSocketCtor: FakeWebSocket,
    documentRef: doc,
    startCaptureFn: vi.fn(async () => ({ stream: 'fake-capture' })),
    stopCaptureFn: vi.fn(),
    createPlaybackQueue: vi.fn(() => playback),
    getFreshAccessTokenFn: vi.fn(async () => 'test-jwt'),
  }).init();
  await client.startSession({
    config: { voiceWsUrl: 'wss://portal.example/ws/voice' },
  });
  const ws = FakeWebSocket.instances.at(-1);
  ws.simulateOpen();
  return { ws, playback };
}

beforeEach(() => {
  FakeWebSocket.instances = [];
});

describe('barge-in playback flush (interrupted frame)', () => {
  it('flushes the playback queue when the server reports an interruption', async () => {
    const { ws, playback } = await startOpenSession();

    // Response audio is playing when the engineer barges in.
    ws.simulateMessage(new ArrayBuffer(4));
    expect(playback.enqueue).toHaveBeenCalledTimes(1);

    ws.simulateMessage(JSON.stringify({ type: 'interrupted' }));
    expect(playback.clear).toHaveBeenCalledTimes(1);
    // Flush, not teardown: the queue stays usable for the next response.
    expect(playback.close).not.toHaveBeenCalled();
  });

  it('does not flush on unrelated control frames', async () => {
    const { ws, playback } = await startOpenSession();

    ws.simulateMessage(
      JSON.stringify({ type: 'session.state', state: 'live', sessionId: 's1' }),
    );
    ws.simulateMessage(
      JSON.stringify({
        type: 'transcript',
        role: 'assistant',
        text: 'hello',
        timestamp: 't',
      }),
    );
    expect(playback.clear).not.toHaveBeenCalled();
  });
});
