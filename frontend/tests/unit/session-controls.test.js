/**
 * Unit tests for the session control button state driver (Req 9.6).
 *
 * The app shell ships Start and End disabled; `ui/session-controls.js`
 * drives both from the `portal:session-state` / `portal:session-error`
 * events the voice client dispatches. These tests import the module for
 * its side-effect wiring against the jsdom global document and assert
 * the enable/disable mapping for every session state, including the
 * historic defect where the End button was never enabled in any state.
 */

import { beforeEach, describe, expect, it } from 'vitest';

import {
  ACTIVE_SESSION_STATES,
  TERMINAL_ERROR_CATEGORIES,
  applyControlState,
} from '../../src/ui/session-controls.js';

/**
 * Creates the two session control buttons in the page shell's shipped
 * state (both disabled) and attaches them to the live document the
 * module's listeners query.
 * @returns {{startButton: HTMLButtonElement, stopButton:
 *   HTMLButtonElement}} The attached buttons.
 */
function createButtons() {
  const startButton = document.createElement('button');
  startButton.id = 'btn-session-start';
  startButton.disabled = true;
  const stopButton = document.createElement('button');
  stopButton.id = 'btn-session-stop';
  stopButton.disabled = true;
  document.body.append(startButton, stopButton);
  return { startButton, stopButton };
}

/**
 * Dispatches one `portal:session-state` event on the document.
 * @param {string} state - Session state carried in the event detail.
 * @returns {void}
 */
function dispatchState(state) {
  document.dispatchEvent(
    new CustomEvent('portal:session-state', { detail: { state } }),
  );
}

/**
 * Dispatches one `portal:session-error` event on the document.
 * @param {string} category - Error category carried in the event detail.
 * @returns {void}
 */
function dispatchError(category) {
  document.dispatchEvent(
    new CustomEvent('portal:session-error', { detail: { category } }),
  );
}

beforeEach(() => {
  document.body.innerHTML = '';
});

describe('session control button state (Req 9.6)', () => {
  it.each(ACTIVE_SESSION_STATES.map((state) => [state]))(
    'enables End and disables Start while the session is %s',
    (state) => {
      const { startButton, stopButton } = createButtons();
      dispatchState(state);
      expect(stopButton.disabled).toBe(false);
      expect(startButton.disabled).toBe(true);
    },
  );

  it.each([['ended'], ['error'], ['idle']])(
    'disables End and re-enables Start once the session is %s',
    (state) => {
      const { startButton, stopButton } = createButtons();
      dispatchState('live');
      dispatchState(state);
      expect(stopButton.disabled).toBe(true);
      expect(startButton.disabled).toBe(false);
    },
  );

  it.each(TERMINAL_ERROR_CATEGORIES.map((category) => [category]))(
    'treats a %s error as the end of the session',
    (category) => {
      const { startButton, stopButton } = createButtons();
      dispatchState('live');
      dispatchError(category);
      expect(stopButton.disabled).toBe(true);
      expect(startButton.disabled).toBe(false);
    },
  );

  it('leaves the buttons alone for a mid-session error report', () => {
    const { startButton, stopButton } = createButtons();
    dispatchState('live');
    dispatchError('internal');
    expect(stopButton.disabled).toBe(false);
    expect(startButton.disabled).toBe(true);
  });

  it('ignores state events without a state and missing buttons', () => {
    // No buttons attached: the handlers must not throw.
    dispatchState('live');
    document.dispatchEvent(
      new CustomEvent('portal:session-state', { detail: {} }),
    );
    expect(() => applyControlState(true, null, null)).not.toThrow();
  });
});
