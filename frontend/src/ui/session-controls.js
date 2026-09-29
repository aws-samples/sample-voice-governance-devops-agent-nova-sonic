/**
 * Session control button state driver (Start session / End session).
 *
 * The app shell ships both buttons disabled; `main.js` enables Start once
 * bootstrap succeeds, and this module drives both buttons from the
 * session lifecycle after that: while a session is underway (connecting,
 * live, or segmenting) the End button is enabled and Start is disabled;
 * once the session is over (idle, ended, error, or a terminal connection
 * failure) Start is enabled again and End is disabled.
 *
 * State arrives on the same `portal:session-state` CustomEvents the
 * status badge renders (dispatched by `ws/voice-client.js`), plus the
 * client-synthesized `portal:session-error` categories that end a session
 * without a `session.state` frame (`connection_failed`,
 * `connection_interrupted`).
 *
 * The Cognito route guard (`auth/cognito.js`) disables whatever controls
 * are enabled while signed out and re-enables exactly those on sign-in;
 * its capture-phase click guard blocks any interaction while
 * unauthenticated, so this module never fights the guard — sessions only
 * exist while signed in.
 */

/**
 * Session states during which a session is underway: the End button must
 * be operable and Start must not spawn a second session.
 * @type {Readonly<string[]>}
 */
export const ACTIVE_SESSION_STATES = Object.freeze([
  'connecting',
  'live',
  'segmenting',
]);

/**
 * Element ids of the controls that are operable exactly while a session
 * is underway: the End button plus the typed-input field and its Send
 * button (typing is only meaningful on an open voice socket).
 * @type {Readonly<string[]>}
 */
export const IN_SESSION_CONTROL_IDS = Object.freeze([
  'btn-session-stop',
  'text-input',
  'btn-text-send',
]);

/**
 * `portal:session-error` categories after which the session is over
 * without any accompanying `session.state` frame — synthesized
 * client-side by the voice client's settle path.
 *
 * Server-sent terminal frames are handled by their `recoverable: false`
 * flag instead of being listed here (see {@link isTerminalSessionError}),
 * so the server stays the single authority on what ends a session.
 * @type {Readonly<string[]>}
 */
export const TERMINAL_ERROR_CATEGORIES = Object.freeze([
  'connection_failed',
  'connection_interrupted',
]);

/**
 * Reports whether a session-error detail means the session is over, so
 * Start must be re-enabled and End disabled.
 * @param {{category?: string, recoverable?: boolean}} detail - Detail of a
 *   `portal:session-error` event.
 * @returns {boolean} True when the session is finished: either a
 *   client-synthesized terminal category, or a server frame the service
 *   explicitly marked unrecoverable (an expired sign-in or an idle
 *   timeout, which otherwise left Start disabled with no session running).
 */
export function isTerminalSessionError(detail) {
  return (
    TERMINAL_ERROR_CATEGORIES.includes(detail?.category) ||
    detail?.recoverable === false
  );
}

/**
 * Applies one session-activity value to the two control buttons: an
 * active session enables End and disables Start; an inactive one does the
 * reverse. Null buttons are skipped, so partial shells (unit test
 * imports) never throw.
 * @param {boolean} sessionActive - Whether a session is underway.
 * @param {HTMLButtonElement | null} startButton - The Start session
 *   button, or null when absent.
 * @param {HTMLButtonElement | null} stopButton - The End session button,
 *   or null when absent.
 * @returns {void}
 */
export function applyControlState(sessionActive, startButton, stopButton) {
  if (startButton) {
    startButton.disabled = sessionActive;
  }
  if (stopButton) {
    stopButton.disabled = !sessionActive;
  }
}

/**
 * Applies one session-activity value to every in-session control on the
 * document: the Start button is disabled while a session runs, and the
 * End button plus the typed-input controls are enabled only then.
 * Absent elements are skipped, so partial shells never throw.
 * @param {boolean} sessionActive - Whether a session is underway.
 * @returns {void}
 */
function applyDocumentControlState(sessionActive) {
  const startButton = document.getElementById('btn-session-start');
  if (startButton) {
    startButton.disabled = sessionActive;
  }
  for (const id of IN_SESSION_CONTROL_IDS) {
    const control = document.getElementById(id);
    if (control) {
      control.disabled = !sessionActive;
    }
  }
}

/**
 * Handles a `portal:session-state` CustomEvent by mapping its state to
 * button enablement: {@link ACTIVE_SESSION_STATES} enable End, every
 * other state (idle, ended, error, unknown) re-enables Start.
 * @param {CustomEvent<{state: string}>} event - Session-state event
 *   dispatched by the voice WebSocket client.
 * @returns {void}
 */
function handleSessionStateEvent(event) {
  const state = event?.detail?.state;
  if (state === undefined) {
    return;
  }
  applyDocumentControlState(
    ACTIVE_SESSION_STATES.includes(String(state).toLowerCase()),
  );
}

/**
 * Handles a `portal:session-error` CustomEvent: a terminal error means
 * the session is gone, so Start is re-enabled and End disabled. Advisory
 * reports the session survives — such as the idle warning — leave the
 * buttons alone, because the engineer can still speak to carry on.
 * @param {CustomEvent<{category: string, recoverable?: boolean}>} event -
 *   Session-error event dispatched by the voice WebSocket client.
 * @returns {void}
 */
function handleSessionErrorEvent(event) {
  if (!isTerminalSessionError(event?.detail)) {
    return;
  }
  applyDocumentControlState(false);
}

// Self-wire on import so main.js only needs a side-effect import; guarded
// for document-less environments (plain Node test runners).
if (typeof document !== 'undefined') {
  document.addEventListener('portal:session-state', handleSessionStateEvent);
  document.addEventListener('portal:session-error', handleSessionErrorEvent);
}
