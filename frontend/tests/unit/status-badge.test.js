/**
 * Unit tests for the Voice_Session status badge (task 9.12).
 *
 * Covers Req 9.4 — the badge renders every one of the five session
 * states (connecting, live, segmenting, ended, error) — and the
 * synchronous `portal:session-state` event path that satisfies the
 * 1-second update bound of Req 9.5.
 *
 * Rendering is exercised through the pure {@link renderStatus} over
 * jsdom elements; the event path goes through the module's self-wired
 * `document` listener against the page's `#session-status` badge.
 */

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  STATUS_BADGE_CLASSES,
  normalizeState,
  renderStatus,
} from '../../src/ui/status.js';

/**
 * The five Voice_Session states required by Req 9.4.
 * @type {string[]}
 */
const REQUIRED_STATES = ['connecting', 'live', 'segmenting', 'ended', 'error'];

/**
 * Creates a fresh badge element like the page's `#session-status`.
 * @returns {Element} A badge span carrying only the `badge` base class.
 */
function createBadge() {
  const badge = document.createElement('span');
  badge.className = 'badge';
  return badge;
}

beforeEach(() => {
  document.body.innerHTML = '<span id="session-status" class="badge"></span>';
});

afterEach(() => {
  document.body.innerHTML = '';
});

describe('status badge renders all five session states (Req 9.4)', () => {
  for (const state of REQUIRED_STATES) {
    it(`renders the ${state} state with its text and mapped badge class`, () => {
      const badge = renderStatus(state, createBadge());
      expect(badge.textContent).toBe(state);
      expect(badge.dataset.state).toBe(state);
      expect(badge.classList.contains(STATUS_BADGE_CLASSES[state])).toBe(true);
      // The base class survives the color swap.
      expect(badge.classList.contains('badge')).toBe(true);
    });
  }

  it('gives each of the five states a distinct badge color', () => {
    const classes = REQUIRED_STATES.map((state) => STATUS_BADGE_CLASSES[state]);
    expect(new Set(classes).size).toBe(REQUIRED_STATES.length);
  });

  it('swaps the previous color class when the state changes', () => {
    const badge = createBadge();
    renderStatus('connecting', badge);
    renderStatus('live', badge);
    expect(badge.classList.contains(STATUS_BADGE_CLASSES.live)).toBe(true);
    expect(badge.classList.contains(STATUS_BADGE_CLASSES.connecting)).toBe(
      false,
    );
    expect(badge.classList.contains('badge')).toBe(true);
  });

  it('renders unknown or nullish states as error (total rendering)', () => {
    expect(normalizeState('rebooting')).toBe('error');
    expect(normalizeState(undefined)).toBe('error');
    const badge = renderStatus('rebooting', createBadge());
    expect(badge.textContent).toBe('error');
    expect(badge.classList.contains(STATUS_BADGE_CLASSES.error)).toBe(true);
  });
});

describe('portal:session-state events update the page badge (Req 9.4, 9.5)', () => {
  it('updates #session-status synchronously for every session state', () => {
    const badge = document.getElementById('session-status');
    for (const state of REQUIRED_STATES) {
      document.dispatchEvent(
        new CustomEvent('portal:session-state', {
          detail: { state, sessionId: 'sess-1' },
        }),
      );
      // Synchronous update: asserted immediately after dispatch, so the
      // 1-second bound of Req 9.5 is met trivially.
      expect(badge.textContent).toBe(state);
      expect(badge.classList.contains(STATUS_BADGE_CLASSES[state])).toBe(true);
    }
  });

  it('ignores events without a state, keeping the last rendered state', () => {
    const badge = document.getElementById('session-status');
    document.dispatchEvent(
      new CustomEvent('portal:session-state', {
        detail: { state: 'live', sessionId: 'sess-1' },
      }),
    );
    document.dispatchEvent(
      new CustomEvent('portal:session-state', { detail: {} }),
    );
    expect(badge.textContent).toBe('live');
  });
});
