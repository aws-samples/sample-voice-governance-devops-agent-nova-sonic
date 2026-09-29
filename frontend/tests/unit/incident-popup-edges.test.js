/**
 * Unit tests for incident popup edge cases (task 9.12).
 *
 * Covers Req 5.3/5.4 — a blocked or failed audio chime never suppresses
 * the incident popup — and Req 5.7/5.8 — clicking a notification opens a
 * Voice_Session scoped to the payload's executionId, or carrying the
 * summary/severity incident context when the payload has none.
 *
 * Chime failure modes are driven through {@link playChime} and the
 * `portal:incident` handler {@link handleIncidentEvent}; click scoping is
 * driven through {@link showIncidentToast} and
 * {@link appendNotificationEntry} rendered into a detached document, so
 * the `portal:session-start` dispatches stay isolated from the modules
 * self-wired to the global document.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  appendNotificationEntry,
  clearShownNotifications,
  handleIncidentEvent,
  playChime,
  showIncidentToast,
} from '../../src/ui/incident-popup.js';

/** @type {number} */
let payloadCounter = 0;

/**
 * Builds an incident payload with a unique notification id so the
 * module's duplicate-delivery suppression never skips a test render.
 * @param {object} [overrides] - Fields overriding the defaults (an
 *   `executionId` is only present when a test provides one).
 * @returns {object} The incident payload.
 */
function buildPayload(overrides = {}) {
  payloadCounter += 1;
  return {
    notificationId: `notif-${payloadCounter}`,
    source: 'cloudwatch-alarm',
    summary: `Incident summary ${payloadCounter}`,
    severity: 'critical',
    timestamp: '2026-02-11T10:00:00Z',
    ...overrides,
  };
}

/**
 * Asserts that the payload's popup toast and persistent feed entry are
 * rendered into the page containers with the payload summary (Req 5.4:
 * the notification is never suppressed).
 * @param {object} payload - Incident payload expected on screen.
 * @returns {void}
 */
function expectNotificationRendered(payload) {
  const toast = document.querySelector(
    `#toast-area [data-incident-popup][data-notification-id="${payload.notificationId}"]`,
  );
  expect(toast).not.toBeNull();
  expect(toast.querySelector('[data-summary]').textContent).toBe(
    payload.summary,
  );
  const entry = document.querySelector(
    `#notifications-list [data-notification-entry]` +
      `[data-notification-id="${payload.notificationId}"]`,
  );
  expect(entry).not.toBeNull();
  expect(entry.querySelector('[data-summary]').textContent).toBe(
    payload.summary,
  );
}

/**
 * Creates a detached document hosting a toast container and a feed list,
 * capturing every `portal:session-start` dispatched on it. Using a
 * detached document keeps the clicks away from the voice client that
 * self-wired to the global document on import.
 * @returns {{doc: Document, container: Element, list: Element,
 *   sessionStarts: object[]}} The isolated document, its containers, and
 *   the captured session-start event details.
 */
function createIsolatedHost() {
  const doc = document.implementation.createHTMLDocument('popup-test');
  const container = doc.createElement('div');
  doc.body.appendChild(container);
  const list = doc.createElement('div');
  doc.body.appendChild(list);
  const sessionStarts = [];
  doc.addEventListener('portal:session-start', (event) => {
    sessionStarts.push(event.detail);
  });
  return { doc, container, list, sessionStarts };
}

/**
 * AudioContext stand-in whose constructor always throws, simulating a
 * browser that blocks Web Audio creation outright (Req 5.4).
 */
class ThrowingAudioContext {
  /**
   * Always throws to simulate blocked audio.
   * @throws {Error} Always.
   */
  constructor() {
    throw new Error('Web Audio blocked by the browser');
  }
}

/**
 * Recording AudioContext fake: oscillators and gains record their
 * scheduling calls. The newest instance is kept on the class because the
 * module caches its shared AudioContext after the first successful use,
 * so later tests mutate this cached instance to simulate failures.
 */
class FakeAudioContext {
  /** @type {FakeAudioContext | null} */
  static lastInstance = null;

  /**
   * Creates the fake in the running state.
   */
  constructor() {
    this.state = 'running';
    this.currentTime = 0;
    this.destination = { name: 'destination' };
    /** @type {object[]} */
    this.oscillators = [];
    FakeAudioContext.lastInstance = this;
  }

  /**
   * Creates a recording oscillator stub whose connect chain returns the
   * connected node (matching the Web Audio API).
   * @returns {object} The oscillator stub.
   */
  createOscillator() {
    const oscillator = {
      type: '',
      frequency: { value: 0 },
      start: vi.fn(),
      stop: vi.fn(),
      connect: vi.fn((node) => node),
    };
    this.oscillators.push(oscillator);
    return oscillator;
  }

  /**
   * Creates a gain stub with a schedulable gain parameter.
   * @returns {object} The gain node stub.
   */
  createGain() {
    return {
      gain: {
        setValueAtTime: vi.fn(),
        linearRampToValueAtTime: vi.fn(),
      },
      connect: vi.fn((node) => node),
    };
  }
}

beforeEach(() => {
  clearShownNotifications();
  document.body.innerHTML =
    '<div id="toast-area"></div>' +
    '<div id="notifications-list"><p>No notifications yet.</p></div>';
});

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

// NOTE: the tests in this block are order-dependent because the module
// caches its shared AudioContext after the first successful playChime
// call and exposes no reset hook. The no-context failure modes run
// first; the working-context baseline then caches FakeAudioContext,
// which the remaining tests mutate to simulate later failures.
describe('chime blocked or failed never suppresses the popup (Req 5.3, 5.4)', () => {
  it('renders the popup and feed entry when Web Audio is entirely unavailable', () => {
    vi.stubGlobal('AudioContext', undefined);
    vi.stubGlobal('webkitAudioContext', undefined);
    expect(playChime()).toBe(false);

    const payload = buildPayload();
    expect(() => handleIncidentEvent({ detail: payload })).not.toThrow();
    expectNotificationRendered(payload);
  });

  it('renders the popup when the AudioContext constructor throws (chime blocked)', () => {
    vi.stubGlobal('AudioContext', ThrowingAudioContext);
    expect(playChime()).toBe(false);

    const payload = buildPayload();
    expect(() => handleIncidentEvent({ detail: payload })).not.toThrow();
    expectNotificationRendered(payload);
  });

  it('schedules the two-tone chime on a working context (baseline)', () => {
    expect(playChime(FakeAudioContext)).toBe(true);

    const context = FakeAudioContext.lastInstance;
    expect(context).not.toBeNull();
    expect(context.oscillators).toHaveLength(2);
    expect(
      context.oscillators.map((oscillator) => oscillator.frequency.value),
    ).toEqual([880, 1318.5]);
    for (const oscillator of context.oscillators) {
      expect(oscillator.start).toHaveBeenCalledTimes(1);
      expect(oscillator.stop).toHaveBeenCalledTimes(1);
    }
  });

  it('swallows a rejected autoplay resume and still shows the popup', async () => {
    const context = FakeAudioContext.lastInstance;
    context.state = 'suspended';
    context.resume = vi.fn(() => Promise.reject(new Error('autoplay blocked')));

    const payload = buildPayload();
    expect(() => handleIncidentEvent({ detail: payload })).not.toThrow();
    // Let the swallowed rejection settle before the test ends.
    await Promise.resolve();

    expect(context.resume).toHaveBeenCalled();
    expectNotificationRendered(payload);
  });

  it('renders the popup when tone scheduling throws (chime failed)', () => {
    const context = FakeAudioContext.lastInstance;
    context.createOscillator = vi.fn(() => {
      throw new Error('scheduling failed');
    });
    expect(playChime()).toBe(false);

    const payload = buildPayload();
    expect(() => handleIncidentEvent({ detail: payload })).not.toThrow();
    expectNotificationRendered(payload);
  });
});

describe('notification click opens a scoped Voice_Session (Req 5.7, 5.8)', () => {
  it('with an executionId the session start is scoped to it and carries no incident context', () => {
    const { container, sessionStarts } = createIsolatedHost();
    const payload = buildPayload({ executionId: 'exec-42' });

    const toast = showIncidentToast(payload, container);
    toast.click();

    expect(sessionStarts).toHaveLength(1);
    expect(sessionStarts[0].executionId).toBe('exec-42');
    expect(sessionStarts[0].incidentContext).toBeNull();
    // The toast is dismissed after the click.
    expect(container.contains(toast)).toBe(false);
  });

  it('without an executionId the session start carries the summary/severity context', () => {
    const { container, sessionStarts } = createIsolatedHost();
    const payload = buildPayload({
      summary: 'Database failover in progress',
      severity: 'high',
    });

    const toast = showIncidentToast(payload, container);
    toast.click();

    expect(sessionStarts).toHaveLength(1);
    expect(sessionStarts[0].executionId).toBeNull();
    expect(sessionStarts[0].incidentContext).toEqual({
      summary: 'Database failover in progress',
      severity: 'high',
    });
  });

  it('the dismiss button closes the toast without starting a session', () => {
    const { container, sessionStarts } = createIsolatedHost();
    const payload = buildPayload({ executionId: 'exec-9' });

    const toast = showIncidentToast(payload, container);
    toast.querySelector('[data-bs-dismiss]').click();

    expect(sessionStarts).toHaveLength(0);
    expect(container.contains(toast)).toBe(false);
  });

  it('a feed entry click also starts the scoped session', () => {
    const { list, sessionStarts } = createIsolatedHost();
    const payload = buildPayload({ executionId: 'exec-7' });

    const entry = appendNotificationEntry(payload, list);
    entry.click();

    expect(sessionStarts).toHaveLength(1);
    expect(sessionStarts[0].executionId).toBe('exec-7');
    expect(sessionStarts[0].incidentContext).toBeNull();
  });
});
