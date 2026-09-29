/**
 * Voice_Session status badge (Req 9.4, 9.5).
 *
 * The voice WebSocket client (`src/ws/voice-client.js`) relays server
 * `session.state` frames as `portal:session-state` CustomEvents on
 * `document` with `detail: { state, sessionId }`. This module listens for
 * those events and updates the `#session-status` badge synchronously on
 * receipt, so the 1-second update bound of Req 9.5 is met trivially. The
 * five session states — connecting, live, segmenting, ended, error — each
 * get a distinct Bootstrap badge color (Req 9.4), plus the pre-session
 * `idle` state the shell starts in.
 *
 * {@link renderStatus} is a pure renderer over an injected element (no
 * lookups, no globals) so tests (task 9.12) can assert every state
 * against jsdom elements directly.
 */

/**
 * Bootstrap `text-bg-*` badge class per session state: the five
 * Voice_Session states of Req 9.4 plus the initial `idle` state.
 * Exported so tests can enumerate all states without duplicating class
 * strings.
 * @type {Readonly<Record<string, string>>}
 */
export const STATUS_BADGE_CLASSES = Object.freeze({
  idle: 'text-bg-secondary',
  connecting: 'text-bg-warning',
  live: 'text-bg-success',
  segmenting: 'text-bg-info',
  ended: 'text-bg-secondary',
  error: 'text-bg-danger',
});

/**
 * Normalizes a state value to a known {@link STATUS_BADGE_CLASSES} key so
 * rendering is total: unknown or nullish states render as `error`, which
 * is the honest fallback for a state the UI cannot interpret.
 * @param {unknown} state - State value from a `session.state` frame.
 * @returns {string} A key of {@link STATUS_BADGE_CLASSES}.
 */
export function normalizeState(state) {
  const key = String(state ?? '').toLowerCase();
  return Object.hasOwn(STATUS_BADGE_CLASSES, key) ? key : 'error';
}

/**
 * Renders a session state onto the status badge element: sets the state
 * name as the badge text and swaps the Bootstrap `text-bg-*` color class
 * to the one mapped for the state, leaving unrelated classes (e.g.
 * `badge`) intact. Pure with respect to its inputs apart from mutating
 * the given element.
 * @param {string} state - Session state to display; one of connecting,
 *   live, segmenting, ended, error (Req 9.4) or idle. Unknown values
 *   render as error.
 * @param {Element} element - Badge element to update (the page's
 *   `#session-status`, or any element under test).
 * @returns {Element} The updated element, for chaining in tests.
 */
export function renderStatus(state, element) {
  const key = normalizeState(state);
  for (const cls of Object.values(STATUS_BADGE_CLASSES)) {
    element.classList.remove(cls);
  }
  element.classList.add(STATUS_BADGE_CLASSES[key]);
  element.textContent = key;
  element.dataset.state = key;
  return element;
}

/**
 * Handles a `portal:session-state` CustomEvent by rendering its state
 * onto the page's `#session-status` badge synchronously, well within the
 * 1-second update bound of Req 9.5. No-ops when the badge element is
 * absent (e.g. under unit test import).
 * @param {CustomEvent<{state: string, sessionId?: string}>} event -
 *   Session-state event dispatched by the voice WebSocket client.
 * @returns {void}
 */
function handleSessionStateEvent(event) {
  const state = event?.detail?.state;
  if (state === undefined) {
    return;
  }
  const badge = document.getElementById('session-status');
  if (!badge) {
    return;
  }
  renderStatus(state, badge);
}

// Self-wire on import so main.js only needs a side-effect import; guarded
// for document-less environments (plain Node test runners).
if (typeof document !== 'undefined') {
  document.addEventListener('portal:session-state', handleSessionStateEvent);
}
