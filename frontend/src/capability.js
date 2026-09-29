/**
 * Browser capability detection for the Nova Sonic Support Portal.
 *
 * A Voice_Session needs microphone capture (getUserMedia), off-main-thread
 * audio processing (AudioWorklet), and realtime streaming (WebSocket).
 * Web push additionally needs a service worker and the Push API.
 * Browsers missing any session capability get a clear unsupported-browser
 * error and session start is refused — never a broken session (Req 9.7).
 *
 * All checks read from an injectable environment object (defaulting to
 * `globalThis`) so tests can pass arbitrary capability subsets.
 */

/**
 * Capability identifiers required to run a Voice_Session.
 * @type {Readonly<string[]>}
 */
export const SESSION_CAPABILITIES = Object.freeze([
  'getUserMedia',
  'AudioWorklet',
  'WebSocket',
]);

/**
 * Capability identifiers required to receive web push notifications.
 * Missing push capabilities disable push features but never block a
 * Voice_Session.
 * @type {Readonly<string[]>}
 */
export const PUSH_CAPABILITIES = Object.freeze([
  'serviceWorker',
  'PushManager',
]);

/**
 * Human-readable labels for capability identifiers, used in error messages.
 * @type {Readonly<Record<string, string>>}
 */
const CAPABILITY_LABELS = Object.freeze({
  getUserMedia: 'microphone capture (getUserMedia)',
  AudioWorklet: 'audio processing (AudioWorklet)',
  WebSocket: 'realtime streaming (WebSocket)',
  serviceWorker: 'background workers (Service Worker)',
  PushManager: 'push notifications (Push API)',
});

/**
 * Tests whether a single capability is available in the given environment.
 * @param {string} capabilityId - One of the identifiers listed in
 *   {@link SESSION_CAPABILITIES} or {@link PUSH_CAPABILITIES}.
 * @param {object} env - Environment object shaped like the browser global
 *   scope (`navigator`, `AudioWorklet`, `WebSocket`, `PushManager`, ...).
 * @returns {boolean} True when the capability is present, false when it is
 *   missing or the identifier is unknown.
 */
export function hasCapability(capabilityId, env) {
  switch (capabilityId) {
    case 'getUserMedia':
      return typeof env?.navigator?.mediaDevices?.getUserMedia === 'function';
    case 'AudioWorklet':
      return typeof env?.AudioWorklet !== 'undefined';
    case 'WebSocket':
      return typeof env?.WebSocket !== 'undefined';
    case 'serviceWorker':
      return (
        typeof env?.navigator === 'object' &&
        env.navigator !== null &&
        'serviceWorker' in env.navigator
      );
    case 'PushManager':
      return typeof env?.PushManager !== 'undefined';
    default:
      return false;
  }
}

/**
 * Detects the browser capabilities the Portal depends on.
 *
 * Pure with respect to the injected environment: the same `env` always
 * yields the same result, and nothing is mutated. `supported` is true
 * exactly when every Voice_Session capability in
 * {@link SESSION_CAPABILITIES} is present; push capabilities are reported
 * separately and never block a session (Req 9.7, Property 20).
 * @param {object} [env] - Environment to inspect, shaped like the browser
 *   global scope. Defaults to `globalThis`. Tests may pass arbitrary
 *   subsets, e.g. `{ WebSocket: class {}, navigator: { mediaDevices:
 *   { getUserMedia: () => {} }, serviceWorker: {} }, AudioWorklet: class {},
 *   PushManager: class {} }`.
 * @returns {{supported: boolean, missing: string[], pushSupported: boolean,
 *   pushMissing: string[]}} Detection result: `supported`/`missing` cover
 *   the Voice_Session capabilities, `pushSupported`/`pushMissing` cover the
 *   web push capabilities. `missing` and `pushMissing` preserve the order
 *   of the capability constants.
 */
export function checkCapabilities(env = globalThis) {
  const missing = SESSION_CAPABILITIES.filter(
    (capabilityId) => !hasCapability(capabilityId, env),
  );
  const pushMissing = PUSH_CAPABILITIES.filter(
    (capabilityId) => !hasCapability(capabilityId, env),
  );
  return {
    supported: missing.length === 0,
    missing,
    pushSupported: pushMissing.length === 0,
    pushMissing,
  };
}

/**
 * Renders the unsupported-browser error into the given container,
 * replacing any unsupported-browser error already displayed there.
 *
 * The message states that the browser is unsupported (Req 9.7), lists the
 * missing capabilities in plain language, and names supported browsers.
 * All content is inserted via `textContent`, never markup injection.
 * @param {string[]} missing - Missing capability identifiers, as reported
 *   by {@link checkCapabilities}.
 * @param {Element} container - DOM element that receives the alert, e.g.
 *   the page-level alert area.
 * @returns {Element} The alert element that was appended to the container.
 */
export function renderUnsupportedError(missing, container) {
  const doc = container.ownerDocument;
  container.querySelector('[data-error="unsupported-browser"]')?.remove();

  const alert = doc.createElement('div');
  alert.className = 'alert alert-danger';
  alert.setAttribute('role', 'alert');
  alert.dataset.error = 'unsupported-browser';

  const heading = doc.createElement('p');
  heading.className = 'fw-bold mb-1';
  heading.textContent = 'Unsupported browser';
  alert.appendChild(heading);

  const labels = missing.map(
    (capabilityId) => CAPABILITY_LABELS[capabilityId] ?? capabilityId,
  );
  const detail = doc.createElement('p');
  detail.className = 'mb-1';
  detail.textContent =
    'This browser is missing capabilities the Portal requires for voice ' +
    `sessions: ${labels.join(', ')}.`;
  alert.appendChild(detail);

  const advice = doc.createElement('p');
  advice.className = 'mb-0';
  advice.textContent =
    'A Voice_Session cannot be started in this browser. Please use a ' +
    'current version of Chrome, Edge, Firefox, or Safari.';
  alert.appendChild(advice);

  container.appendChild(alert);
  return alert;
}
