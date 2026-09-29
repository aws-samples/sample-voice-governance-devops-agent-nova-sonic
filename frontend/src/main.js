/**
 * Application bootstrap for the Nova Sonic Support Portal SPA.
 *
 * On load this module:
 * 1. runs the browser capability gate ({@link module:capability}) and, when
 *    a Voice_Session capability is missing, shows the unsupported-browser
 *    error and refuses session start (Req 9.7);
 * 2. loads runtime configuration from `config.json` — Cognito ids, the
 *    voice WebSocket URL, AppSync Events endpoints, and the VAPID public
 *    key are never hardcoded (Req 14.3) — and shows a clear error when the
 *    file is missing or invalid;
 * 3. enables the session controls and dispatches lifecycle DOM events.
 *
 * Later feature modules (auth, audio capture, WebSocket client, events,
 * push) plug in through the events dispatched on `document`:
 * - `portal:ready`          detail `{ config }` — bootstrap finished;
 * - `portal:session-start`  detail `{ config }` — engineer requested start
 *   and the capability gate passed;
 * - `portal:session-stop`   detail `{ config }` — engineer requested stop;
 * - `portal:session-text`   detail `{ text, config }` — engineer submitted
 *   a typed request to send on the open voice socket.
 * They may also import {@link getConfig} for the loaded configuration.
 */

import { checkCapabilities, renderUnsupportedError } from './capability.js';
// Side-effect import: the auth module registers its `portal:ready`
// listener and enforces the Cognito sign-in route guard (Req 7.1, 7.7).
import './auth/cognito.js';
// Side-effect import: the voice WebSocket client registers its
// `portal:session-start`/`portal:session-stop` listeners and drives the
// voice plane — capture, socket, playback, reconnect (Req 1.1, 1.5, 8.6).
import './ws/voice-client.js';
// Side-effect import: the AppSync Events client registers its
// `portal:ready` listener and maintains the Cognito-authorized realtime
// subscription to `/incidents/all`, re-dispatching notifications as
// `portal:incident` events (Req 5.2, 7.4).
import './events/appsync-client.js';
// Side-effect imports: the UI modules register their document listeners
// for transcript, session-state, and error events (Req 1.4, 9.4-9.7).
import './ui/transcript.js';
import './ui/status.js';
import './ui/errors.js';
// Side-effect import: the session-controls module drives the Start/End
// session buttons from the session lifecycle events, so the End button
// is operable exactly while a session is underway (Req 9.6).
import './ui/session-controls.js';
// Side-effect import: the push manager wires the push opt-in button,
// service worker registration and subscription persistence, and the
// notification click-through into a scoped Voice_Session
// (Req 6.1, 6.4, 6.6, 6.7).
import './push/push-manager.js';
// Side-effect import: the incident popup registers its `portal:incident`
// listener — toast, feed entry, chime, and click-to-session scoping
// (Req 5.2-5.4, 5.7, 5.8).
import './ui/incident-popup.js';

/**
 * Runtime configuration loaded from `config.json`, generated at deploy
 * time from Terraform outputs (see `config.example.json` for the shape).
 * @typedef {object} PortalConfig
 * @property {{userPoolId: string, clientId: string, domain: string}} cognito -
 *   Cognito user pool id, SPA client id, and Hosted UI domain.
 * @property {string} voiceWsUrl - CloudFront `wss://` URL of the voice
 *   WebSocket endpoint.
 * @property {{httpEndpoint: string, realtimeEndpoint: string}} events -
 *   AppSync Events HTTP and realtime endpoints.
 * @property {string} vapidPublicKey - VAPID public key for web push.
 * @property {string} [region] - AWS region, when clients need it explicitly.
 */

/**
 * Dot-separated paths of configuration keys that must be present in
 * `config.json` for the Portal to operate.
 * @type {Readonly<string[]>}
 */
export const REQUIRED_CONFIG_KEYS = Object.freeze([
  'cognito.userPoolId',
  'cognito.clientId',
  'cognito.domain',
  'voiceWsUrl',
  'events.httpEndpoint',
  'events.realtimeEndpoint',
  'vapidPublicKey',
]);

/** @type {PortalConfig | null} */
let config = null;

/**
 * Returns the runtime configuration loaded during bootstrap.
 * @returns {PortalConfig | null} The loaded configuration, or null when
 *   bootstrap has not completed successfully yet.
 */
export function getConfig() {
  return config;
}

/**
 * Reads a nested value from an object by dot-separated path.
 * @param {object} source - Object to read from.
 * @param {string} path - Dot-separated key path, e.g. `"cognito.clientId"`.
 * @returns {unknown} The value at the path, or undefined when any segment
 *   is absent.
 */
function getByPath(source, path) {
  return path
    .split('.')
    .reduce(
      (value, segment) =>
        value === null || value === undefined ? undefined : value[segment],
      source,
    );
}

/**
 * Validates a parsed configuration object against the required-key
 * manifest. A key counts as missing when it is absent, null, or an empty
 * string.
 * @param {object} candidate - Parsed `config.json` content.
 * @returns {string[]} Dot-separated paths of the missing required keys, in
 *   manifest order; empty when the configuration is complete.
 */
export function validateConfig(candidate) {
  return REQUIRED_CONFIG_KEYS.filter((path) => {
    const value = getByPath(candidate, path);
    return value === undefined || value === null || value === '';
  });
}

/**
 * Fetches `config.json` relative to the document base URL, bypassing the
 * HTTP cache so a redeployed configuration takes effect on reload.
 * @param {string} url - URL to fetch.
 * @returns {Promise<Response>} The fetch response.
 */
function defaultFetch(url) {
  return globalThis.fetch(url, { cache: 'no-store' });
}

/**
 * Loads and validates the runtime configuration from `config.json`.
 * @param {(url: string) => Promise<Response>} [fetchFn] - Fetch
 *   implementation, injectable for tests. Defaults to the global fetch.
 * @returns {Promise<PortalConfig>} The validated configuration object.
 * @throws {Error} When the request fails, the response status is not OK,
 *   the body is not valid JSON, or required keys are missing — always with
 *   a message naming the failure and, for missing keys, each key by name.
 */
export async function loadConfig(fetchFn = defaultFetch) {
  let response;
  try {
    response = await fetchFn('./config.json');
  } catch (cause) {
    throw new Error(
      'config.json could not be fetched. The deployment pipeline generates ' +
        'it from Terraform outputs; see config.example.json for the shape.',
      { cause },
    );
  }
  if (!response.ok) {
    throw new Error(
      `config.json request failed with HTTP ${response.status}. The ` +
        'deployment pipeline generates it from Terraform outputs; see ' +
        'config.example.json for the shape.',
    );
  }

  let parsed;
  try {
    parsed = await response.json();
  } catch (cause) {
    throw new Error('config.json is not valid JSON.', { cause });
  }

  const missing = validateConfig(parsed);
  if (missing.length > 0) {
    throw new Error(
      `config.json is missing required keys: ${missing.join(', ')}.`,
    );
  }
  return parsed;
}

/**
 * Looks up the app shell elements by id.
 * @returns {{alertArea: Element | null, startButton: HTMLButtonElement |
 *   null, stopButton: HTMLButtonElement | null, pushButton:
 *   HTMLButtonElement | null, textForm: HTMLFormElement | null}}
 *   References to the shell elements; entries are null when the markup is
 *   absent (e.g. under unit test import).
 */
function getShellElements() {
  return {
    alertArea: document.getElementById('alert-area'),
    startButton: document.getElementById('btn-session-start'),
    stopButton: document.getElementById('btn-session-stop'),
    pushButton: document.getElementById('btn-push-enable'),
    textForm: document.getElementById('text-input-form'),
  };
}

/**
 * Shows a dismissible Bootstrap alert in the page-level alert area,
 * replacing any previous alert with the same identifier.
 * @param {Element} alertArea - Container element for page-level alerts.
 * @param {string} variant - Bootstrap contextual variant, e.g. `"danger"`.
 * @param {string} message - Plain-text message to display (inserted via
 *   `textContent`, never markup injection).
 * @param {string} alertId - Stable identifier used to de-duplicate alerts.
 * @returns {Element} The alert element that was appended.
 */
function showAlert(alertArea, variant, message, alertId) {
  alertArea.querySelector(`[data-alert-id="${alertId}"]`)?.remove();
  const alert = document.createElement('div');
  alert.className = `alert alert-${variant}`;
  alert.setAttribute('role', 'alert');
  alert.dataset.alertId = alertId;
  alert.textContent = message;
  alertArea.appendChild(alert);
  return alert;
}

/**
 * Dispatches a Portal lifecycle event on `document` so feature modules
 * can react without being imported here.
 * @param {string} type - Event type, e.g. `"portal:session-start"`.
 * @returns {void}
 */
function dispatchPortalEvent(type) {
  document.dispatchEvent(new CustomEvent(type, { detail: { config } }));
}

/**
 * Handles a click on the session start button: re-runs the capability
 * gate and refuses to start when a Voice_Session capability is missing
 * (Req 9.7); otherwise announces the start request to the session modules
 * via the `portal:session-start` event.
 * @returns {void}
 */
function handleSessionStart() {
  const shell = getShellElements();
  const capabilities = checkCapabilities();
  if (!capabilities.supported) {
    if (shell.alertArea) {
      renderUnsupportedError(capabilities.missing, shell.alertArea);
    }
    return;
  }
  dispatchPortalEvent('portal:session-start');
}

/**
 * Handles a click on the session stop button by announcing the stop
 * request to the session modules via the `portal:session-stop` event.
 * @returns {void}
 */
function handleSessionStop() {
  dispatchPortalEvent('portal:session-stop');
}

/**
 * Handles submission of the typed-request form: announces the text to the
 * voice client via `portal:session-text` and clears the field. Prevents
 * the default form submit so the page never navigates.
 * @param {Event} event - Form submit event.
 * @returns {void}
 */
function handleTextSubmit(event) {
  event.preventDefault();
  const field = document.getElementById('text-input');
  const text = field?.value ?? '';
  if (text.trim() === '') {
    return;
  }
  document.dispatchEvent(
    new CustomEvent('portal:session-text', { detail: { text, config } }),
  );
  field.value = '';
}

/**
 * Wires the session controls once bootstrap succeeded: enables the start
 * button and attaches the start/stop and typed-request handlers. The
 * typed-input controls stay disabled until a session is underway
 * (`ui/session-controls.js` owns that state).
 * @param {ReturnType<typeof getShellElements>} shell - App shell element
 *   references.
 * @returns {void}
 */
function enableSessionControls(shell) {
  shell.startButton.disabled = false;
  shell.startButton.addEventListener('click', handleSessionStart);
  shell.stopButton?.addEventListener('click', handleSessionStop);
  shell.textForm?.addEventListener('submit', handleTextSubmit);
}

/**
 * Bootstraps the application: capability gate, runtime configuration,
 * then session controls. No-ops when the app shell markup is absent.
 *
 * Failure modes surface in the alert area instead of throwing: a missing
 * Voice_Session capability shows the unsupported-browser error and leaves
 * session start disabled (Req 9.7); a missing or invalid `config.json`
 * shows a configuration error and leaves session start disabled.
 * @returns {Promise<void>} Resolves when bootstrap finished, whether or
 *   not the Portal ended up operational.
 */
export async function bootstrapApp() {
  const shell = getShellElements();
  if (!shell.alertArea || !shell.startButton) {
    return;
  }

  const capabilities = checkCapabilities();
  if (!capabilities.supported) {
    renderUnsupportedError(capabilities.missing, shell.alertArea);
    return;
  }
  if (!capabilities.pushSupported && shell.pushButton) {
    shell.pushButton.title =
      'Push notifications are not supported in this browser.';
  }

  try {
    config = await loadConfig();
  } catch (error) {
    showAlert(
      shell.alertArea,
      'danger',
      `The Portal could not start: ${error.message}`,
      'config-error',
    );
    return;
  }

  enableSessionControls(shell);
  dispatchPortalEvent('portal:ready');
}

/**
 * Schedules {@link bootstrapApp} to run as soon as the DOM is ready, or
 * immediately when the document has already been parsed.
 * @returns {void}
 */
function initOnLoad() {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', runBootstrap, {
      once: true,
    });
  } else {
    runBootstrap();
  }
}

/**
 * Runs the async bootstrap from a synchronous event context, explicitly
 * discarding the promise ({@link bootstrapApp} never rejects).
 * @returns {void}
 */
function runBootstrap() {
  void bootstrapApp();
}

initOnLoad();
