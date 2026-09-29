/**
 * Central error UI: user-facing messaging for every Portal error category
 * (Req 1.8, 9.6, 9.7 plus the server error frame categories).
 *
 * Two document CustomEvents feed this module:
 *
 * - `portal:error` — dispatched cancelably by local modules (e.g.
 *   `src/audio/capture.js` with `category: 'mic-denied'`). The listener
 *   here calls `event.preventDefault()` to take ownership, so the
 *   dispatching module skips its minimal fallback alert and this module
 *   renders the full-styled error instead.
 * - `portal:session-error` — dispatched by the voice WebSocket client
 *   (`src/ws/voice-client.js`) for server `error` frames with
 *   `detail: { category, message }`.
 *
 * Rendering follows the established alert pattern (`capability.js`,
 * `main.js`): a Bootstrap alert in `#alert-area`, de-duplicated via the
 * `data-error` attribute, all content inserted via `textContent` — never
 * markup injection. {@link renderError} takes an explicit container so
 * tests can render into jsdom elements directly.
 */

/**
 * User-facing presentation of one error category.
 * @typedef {object} ErrorPresentation
 * @property {string} title - Short alert heading.
 * @property {string} message - Curated user-facing explanation; this text
 *   carries the requirement-mandated messaging for the category.
 * @property {string} variant - Bootstrap contextual variant of the alert
 *   (`danger` for terminal errors, `warning` for recoverable ones).
 */

/**
 * Curated user-facing messaging per error category.
 *
 * Local categories: `mic-denied` (Req 1.8, 9.6) and
 * `unsupported-browser` (Req 9.7). Server error frame categories:
 * `auth_invalid`, `auth_expired` (Req 7.6), `bedrock_unavailable`
 * (Req 1.6), `segmentation_failed` (Req 2.6), `session_not_found`
 * (Req 8.6), `idle_warning`, `idle_timeout`, `internal`. Client
 * connection category: `connection_interrupted` with the reconnect hint
 * (Req 1.5).
 *
 * `idle_warning` is the one advisory entry: the session is still live
 * when it renders, so it is styled as a warning rather than a danger and
 * clears itself from the engineer's point of view as soon as they speak.
 * @type {Readonly<Record<string, Readonly<ErrorPresentation>>>}
 */
export const ERROR_MESSAGES = Object.freeze({
  'mic-denied': Object.freeze({
    title: 'Microphone access required',
    message:
      'Microphone access is required to start a voice session. Allow ' +
      'microphone permission for this site in your browser settings and ' +
      'try again. No voice connection was opened.',
    variant: 'danger',
  }),
  'unsupported-browser': Object.freeze({
    title: 'Unsupported browser',
    message:
      'This browser does not support the capabilities the Portal ' +
      'requires for voice sessions. Please use a current version of ' +
      'Chrome, Edge, Firefox, or Safari.',
    variant: 'danger',
  }),
  auth_invalid: Object.freeze({
    title: 'Authentication failed',
    message:
      'Your sign-in could not be verified, so the voice session was not ' +
      'started. Sign in again and retry.',
    variant: 'danger',
  }),
  auth_expired: Object.freeze({
    title: 'Session expired',
    message:
      'Your sign-in expired during the voice session and the connection ' +
      'was closed. Sign in again to start a new session; your transcript ' +
      'up to this point was saved.',
    variant: 'danger',
  }),
  idle_warning: Object.freeze({
    title: 'Still there?',
    message:
      'No request has been received for a while. Speak or type to carry ' +
      'on — otherwise this session will end in 5 minutes.',
    variant: 'warning',
  }),
  idle_timeout: Object.freeze({
    title: 'Session ended after inactivity',
    message:
      'The session ended because no request was received after the idle ' +
      'warning. Your transcript was saved. Start a new session to continue.',
    variant: 'danger',
  }),
  connection_interrupted: Object.freeze({
    title: 'Connection interrupted',
    message:
      'The voice connection was interrupted. Use the Reconnect button to ' +
      're-establish your session and continue where you left off.',
    variant: 'warning',
  }),
  bedrock_unavailable: Object.freeze({
    title: 'Voice service unavailable',
    message:
      'The voice assistant could not be reached, so the session was ' +
      'closed. Please try starting a new session shortly.',
    variant: 'danger',
  }),
  segmentation_failed: Object.freeze({
    title: 'Voice session interrupted',
    message:
      'The voice session could not be continued past a stream rollover. ' +
      'Your transcript up to this point was saved. Start a new session ' +
      'to continue.',
    variant: 'danger',
  }),
  session_not_found: Object.freeze({
    title: 'Session not available',
    message:
      'The session you tried to reconnect to no longer exists or has ' +
      'expired. Start a new session instead.',
    variant: 'danger',
  }),
  internal: Object.freeze({
    title: 'Something went wrong',
    message:
      'An unexpected error occurred in the voice service. Please try ' +
      'again; if the problem persists, start a new session.',
    variant: 'danger',
  }),
});

/**
 * Normalizes an error category to a key of {@link ERROR_MESSAGES} so
 * rendering is total: unknown or nullish categories present as
 * `internal`.
 * @param {unknown} category - Category value from an error event.
 * @returns {string} A key of {@link ERROR_MESSAGES}.
 */
function normalizeCategory(category) {
  const key = String(category ?? '');
  return Object.hasOwn(ERROR_MESSAGES, key) ? key : 'internal';
}

/**
 * Renders the full-styled error alert for a category into the given
 * container, replacing any alert for the same category (de-duplicated via
 * the `data-error` attribute, per the established pattern).
 *
 * The alert always carries the curated title and message from
 * {@link ERROR_MESSAGES}, guaranteeing the requirement-mandated
 * messaging; a supplemental `message` that differs from the curated text
 * is appended as a secondary detail line. All content is inserted via
 * `textContent`.
 * @param {string} category - Error category; unknown values render as
 *   `internal`.
 * @param {string} [message] - Optional supplemental detail, e.g. the
 *   server-provided error message.
 * @param {Element} [container] - Element that receives the alert;
 *   defaults to the page's `#alert-area`.
 * @returns {Element | null} The appended alert element, or null when no
 *   container exists (e.g. under unit test import without page markup).
 */
export function renderError(category, message, container) {
  const target =
    container ?? globalThis.document?.getElementById('alert-area') ?? null;
  if (!target) {
    return null;
  }
  const key = normalizeCategory(category);
  const presentation = ERROR_MESSAGES[key];
  const doc = target.ownerDocument;

  target.querySelector(`[data-error="${key}"]`)?.remove();

  const alert = doc.createElement('div');
  alert.className = `alert alert-${presentation.variant}`;
  alert.setAttribute('role', 'alert');
  alert.dataset.error = key;

  const heading = doc.createElement('p');
  heading.className = 'fw-bold mb-1';
  heading.textContent = presentation.title;
  alert.appendChild(heading);

  const detail = doc.createElement('p');
  detail.className = 'mb-0';
  detail.textContent = presentation.message;
  alert.appendChild(detail);

  if (
    typeof message === 'string' &&
    message !== '' &&
    message !== presentation.message
  ) {
    const supplement = doc.createElement('p');
    supplement.className = 'small text-body-secondary mb-0 mt-1';
    supplement.textContent = message;
    alert.appendChild(supplement);
  }

  target.appendChild(alert);
  return alert;
}

/**
 * Handles a cancelable `portal:error` CustomEvent from a local module:
 * calls `preventDefault()` to take ownership (so the dispatcher skips its
 * minimal fallback, e.g. capture.js for `mic-denied`, Req 1.8, 9.6) and
 * renders the full-styled error.
 * @param {CustomEvent<{category: string, message?: string}>} event -
 *   Local error event.
 * @returns {void}
 */
function handlePortalError(event) {
  const detail = event?.detail;
  if (!detail?.category) {
    return;
  }
  event.preventDefault();
  renderError(detail.category, detail.message);
}

/**
 * Handles a `portal:session-error` CustomEvent from the voice WebSocket
 * client by rendering the server error frame's category and message.
 * @param {CustomEvent<{category: string, message?: string}>} event -
 *   Session error event carrying a server `error` frame's content.
 * @returns {void}
 */
function handleSessionError(event) {
  const detail = event?.detail;
  if (!detail) {
    return;
  }
  renderError(detail.category, detail.message);
}

// Self-wire on import so main.js only needs a side-effect import; guarded
// for document-less environments (plain Node test runners).
if (typeof document !== 'undefined') {
  document.addEventListener('portal:error', handlePortalError);
  document.addEventListener('portal:session-error', handleSessionError);
}
