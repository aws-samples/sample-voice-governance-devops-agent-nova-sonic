/**
 * Web push manager: permission flow, subscription registration and
 * persistence, and notification click-through into a Voice_Session.
 *
 * Enable flow (Req 6.1, 6.6, 6.7) — a click on `#btn-push-enable`
 * (delegated document listener; the Cognito route guard keeps the button
 * disabled until the engineer is authenticated) runs:
 * 1. `Notification.requestPermission()`;
 * 2. on `granted`: register the service worker (`./sw.js`, deployed at
 *    the site root from `frontend/public/`, so its scope covers the whole
 *    Portal), subscribe via `registration.pushManager.subscribe` with the
 *    VAPID public key from the runtime configuration, and persist the
 *    subscription through `POST /api/push-subscriptions` with the Cognito
 *    access token — bounded so the grant-to-persisted path completes
 *    within 10 seconds (Req 6.1); success renders the button as
 *    "Push enabled";
 * 3. on `denied`: no registration is attempted and a push-disabled
 *    indication is rendered next to the button (Req 6.6);
 * 4. on persistence (or registration) failure: an error alert with a
 *    retry hint is shown and the button is re-enabled so the engineer can
 *    retry registration (Req 6.7).
 *
 * The `/api/push-subscriptions` URL is deliberately relative: per the
 * design, CloudFront serves the SPA and routes `/api/*` (and `/ws/*`) on
 * the same domain to the ALB, so no endpoint needs to be configured
 * (Req 14.3).
 *
 * Notification click-through (Req 6.4) — the service worker reaches the
 * page two ways, both funneled into a `portal:session-start` dispatch
 * scoped with the incident's `executionId`/summary/severity:
 * - an existing Portal window is focused and receives a
 *   `{type: "portal:incident-click", payload}` message on
 *   `navigator.serviceWorker`; the listener here dispatches the session
 *   start;
 * - with no window open, the worker opens `/?incident=<json>`; on load
 *   this module parses the parameter, removes it from the address bar,
 *   stashes it in sessionStorage, and dispatches the session start once
 *   bootstrap (`portal:ready`) and authentication have completed.
 *
 * Authentication verification for Req 6.4 ("verify the engineer holds a
 * valid authenticated session, prompting for authentication if none
 * exists") is delegated to the existing auth machinery: unauthenticated
 * page loads are redirected to the Cognito sign-in flow by
 * `src/auth/cognito.js` (Req 7.7) and the voice client no-ops without an
 * access token. The sessionStorage stash survives the Hosted UI redirect
 * (same tab), so the scoped session still starts after sign-in; this
 * module simply waits for `isAuthenticated()` before dispatching.
 *
 * Unsubscribe is intentionally not implemented in this task: the design
 * has no delete UI, expired or invalidated subscriptions are pruned
 * server-side on push-service 404/410 responses (Req 6.5), and the
 * backend exposes `DELETE /api/push-subscriptions` should a later task
 * add an opt-out control.
 *
 * Integration seam: `src/main.js` imports this module for its side
 * effect — it wires itself to `document` on import (same pattern as
 * `src/auth/cognito.js` and `src/ws/voice-client.js`) and reads the
 * runtime configuration from the `portal:ready` event detail. Tests call
 * {@link registerPush} and {@link wirePushManager} directly with
 * injectable seams.
 */

import { getAccessToken, isAuthenticated } from '../auth/cognito.js';

/**
 * Element id of the push opt-in button (contract documented in
 * `public/index.html`).
 * @type {string}
 */
export const PUSH_BUTTON_ID = 'btn-push-enable';

/**
 * Relative URL of the subscription persistence endpoint. Relative on
 * purpose: CloudFront routes `/api/*` on the SPA's own domain to the ALB
 * (see the module description), keeping the frontend environment-free.
 * @type {string}
 */
export const SUBSCRIPTIONS_ENDPOINT = '/api/push-subscriptions';

/**
 * Service worker script URL, relative to the site root. `sw.js` lives in
 * `frontend/public/` and deploys at the bucket root, giving the worker
 * root scope.
 * @type {string}
 */
export const SERVICE_WORKER_URL = './sw.js';

/**
 * Budget in milliseconds for the permission-grant-to-persisted path
 * (Req 6.1): the persistence request is aborted once the budget from the
 * moment of the grant is exhausted, surfacing the retry flow instead of
 * hanging.
 * @type {number}
 */
export const PERSIST_TIMEOUT_MS = 10_000;

/**
 * Query parameter carrying an incident payload when the service worker
 * opens the Portal in a fresh window (mirrored in `public/sw.js`).
 * @type {string}
 */
export const INCIDENT_QUERY_PARAM = 'incident';

/**
 * `type` field of the message the service worker posts to an existing
 * Portal window on notification click (mirrored in `public/sw.js`).
 * @type {string}
 */
export const INCIDENT_CLICK_MESSAGE_TYPE = 'portal:incident-click';

/**
 * sessionStorage key under which a pending incident payload is stashed
 * until bootstrap and authentication complete (surviving the Cognito
 * Hosted UI redirect within the tab).
 * @type {string}
 */
export const PENDING_INCIDENT_STORAGE_KEY = 'portal.push.pendingIncident';

/**
 * Interval between authentication checks while a pending incident waits
 * for sign-in to complete.
 * @type {number}
 */
const AUTH_POLL_INTERVAL_MS = 500;

/**
 * Upper bound on waiting for authentication before a pending incident
 * dispatch is abandoned (an unauthenticated tab is redirected to the
 * Hosted UI long before this elapses; the stash survives the redirect).
 * @type {number}
 */
const AUTH_POLL_LIMIT_MS = 120_000;

/**
 * Incident scoping payload extracted from a push notification.
 * @typedef {object} IncidentPayload
 * @property {string | null} executionId - DevOps Agent execution id, when
 *   the incident carries one (Req 6.4).
 * @property {string | null} summary - Incident summary, when present.
 * @property {string | null} severity - Incident severity, when present.
 */

/**
 * Result of a {@link registerPush} run.
 * @typedef {object} RegisterPushResult
 * @property {'registered' | 'denied' | 'dismissed' | 'unsupported' |
 *   'error'} status - Outcome class: `registered` — subscription created
 *   and persisted; `denied` — engineer denied push permission (Req 6.6);
 *   `dismissed` — permission prompt dismissed without a decision;
 *   `unsupported` — the browser lacks Notification/serviceWorker/Push
 *   support; `error` — registration or persistence failed (Req 6.7).
 * @property {object} [subscription] - The PushSubscription, on
 *   `registered`.
 * @property {Error} [error] - The failure, on `error`.
 */

/**
 * Injectable environment seams used by the push flows so tests can
 * substitute fakes; every property defaults to the browser global.
 * @typedef {object} PushRuntime
 * @property {object | undefined} documentRef - Document used for the
 *   button, indications, alerts, and event dispatch; undefined outside a
 *   DOM.
 * @property {object | undefined} locationRef - `window.location`-shaped
 *   object with `search` and `pathname`.
 * @property {object | undefined} historyRef - `window.history`-shaped
 *   object with `replaceState()`.
 * @property {object | undefined} storage - `sessionStorage`-shaped store
 *   for the pending-incident stash.
 * @property {object | undefined} notification - `Notification`-shaped
 *   object exposing `requestPermission()`.
 * @property {object | undefined} serviceWorkerContainer -
 *   `navigator.serviceWorker`-shaped container with `register()` and
 *   `addEventListener()`.
 * @property {(url: string, options?: object) => Promise<Response>} fetch -
 *   Fetch implementation used for the persistence request.
 * @property {() => string | null} getAccessTokenFn - Returns the Cognito
 *   access token, or null when signed out (`src/auth/cognito.js`).
 * @property {() => boolean} isAuthenticatedFn - Reports whether the
 *   engineer holds valid tokens (`src/auth/cognito.js`).
 * @property {Function} CustomEventCtor - CustomEvent constructor used for
 *   dispatching `portal:session-start`.
 * @property {() => number} now - Clock returning epoch milliseconds.
 * @property {(callback: Function, delayMs: number) => *} setTimer - Timer
 *   scheduler, `setTimeout`-shaped.
 * @property {(timerId: *) => void} clearTimer - Timer canceller,
 *   `clearTimeout`-shaped.
 */

/** @type {PushRuntime | null} */
let runtime = null;

/** @type {object | null} */
let pushConfig = null;

/** @type {IncidentPayload | null} */
let pendingIncidentMemory = null;

/** @type {*} */
let authPollTimerId = null;

/** @type {number | null} */
let authPollDeadline = null;

/* -------------------------------------------------------------------- */
/* Pure helpers (exported for direct testing)                            */
/* -------------------------------------------------------------------- */

/**
 * Decodes a base64url-encoded string (the VAPID public key format) into
 * the `Uint8Array` shape `pushManager.subscribe` expects for
 * `applicationServerKey`.
 *
 * Pure: same input, same output; no environment access beyond `atob`.
 * @param {string} base64String - base64url text, with or without padding
 *   (characters `A-Za-z0-9-_`).
 * @returns {Uint8Array} The decoded bytes (a P-256 VAPID public key
 *   decodes to 65 bytes beginning with 0x04).
 * @throws {Error} When the input is not valid base64/base64url.
 */
export function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding)
    .replace(/-/g, '+')
    .replace(/_/g, '/');
  const rawData = atob(base64);
  return Uint8Array.from(rawData, (char) => char.charCodeAt(0));
}

/**
 * Normalizes an arbitrary incident-shaped value into an
 * {@link IncidentPayload}: string fields pass through, everything else
 * becomes null, so the payload is safe to stash as JSON and to dispatch.
 * @param {object | null | undefined} raw - Candidate payload, e.g. the
 *   service worker message payload or the parsed `?incident=` parameter.
 * @returns {IncidentPayload} The normalized payload.
 */
export function normalizeIncident(raw) {
  /**
   * Picks a non-empty string property from the raw payload.
   * @param {string} key - Property name to read.
   * @returns {string | null} The string value, or null.
   */
  const pick = (key) => {
    const value = raw?.[key];
    return typeof value === 'string' && value !== '' ? value : null;
  };
  return {
    executionId: pick('executionId'),
    summary: pick('summary'),
    severity: pick('severity'),
  };
}

/* -------------------------------------------------------------------- */
/* Environment defaults                                                  */
/* -------------------------------------------------------------------- */

/**
 * Default fetch seam delegating to the global fetch.
 * @param {string} url - Request URL.
 * @param {object} [options] - Fetch options.
 * @returns {Promise<Response>} The fetch response.
 */
function defaultFetch(url, options) {
  return globalThis.fetch(url, options);
}

/**
 * Default clock seam returning the current epoch milliseconds.
 * @returns {number} Current time in epoch milliseconds.
 */
function defaultNow() {
  return Date.now();
}

/**
 * Default timer-scheduling seam delegating to the global setTimeout.
 * @param {Function} callback - Function to run after the delay.
 * @param {number} delayMs - Delay in milliseconds.
 * @returns {*} Opaque timer id for {@link defaultClearTimer}.
 */
function defaultSetTimer(callback, delayMs) {
  return globalThis.setTimeout(callback, delayMs);
}

/**
 * Default timer-cancelling seam delegating to the global clearTimeout.
 * @param {*} timerId - Timer id returned by {@link defaultSetTimer}.
 * @returns {void}
 */
function defaultClearTimer(timerId) {
  globalThis.clearTimeout(timerId);
}

/**
 * Builds the default runtime bound to the browser globals; every seam is
 * individually overridable through the `overrides` argument of
 * {@link registerPush} and {@link wirePushManager}.
 * @returns {PushRuntime} Runtime seams backed by the browser environment.
 */
function createDefaultRuntime() {
  return {
    documentRef: globalThis.document,
    locationRef: globalThis.location,
    historyRef: globalThis.history,
    storage: globalThis.sessionStorage,
    notification: globalThis.Notification,
    serviceWorkerContainer: globalThis.navigator?.serviceWorker,
    fetch: defaultFetch,
    getAccessTokenFn: getAccessToken,
    isAuthenticatedFn: isAuthenticated,
    CustomEventCtor: globalThis.CustomEvent,
    now: defaultNow,
    setTimer: defaultSetTimer,
    clearTimer: defaultClearTimer,
  };
}

/* -------------------------------------------------------------------- */
/* Registration flow (Req 6.1, 6.6, 6.7)                                 */
/* -------------------------------------------------------------------- */

/**
 * Persists a push subscription through `POST /api/push-subscriptions`
 * with the Cognito access token, aborting when the remaining Req 6.1
 * budget is exhausted so a hung request surfaces as a retryable failure.
 * @param {object} subscription - PushSubscription to persist; its
 *   `toJSON()` form is sent as `{subscription: ...}`.
 * @param {PushRuntime} activeRuntime - Resolved runtime seams.
 * @param {number} budgetMs - Remaining milliseconds of the 10-second
 *   grant-to-persisted budget.
 * @returns {Promise<void>} Resolves when the backend confirmed the write.
 * @throws {Error} When the engineer is not authenticated, the budget is
 *   already exhausted, the request times out, fails, or the backend
 *   responds with a non-2xx status.
 */
async function persistSubscription(subscription, activeRuntime, budgetMs) {
  const accessToken = activeRuntime.getAccessTokenFn();
  if (!accessToken) {
    throw new Error('not signed in; push subscriptions require a session');
  }
  if (budgetMs <= 0) {
    throw new Error(
      `push subscription was not persisted within ${PERSIST_TIMEOUT_MS} ms`,
    );
  }
  const body =
    typeof subscription?.toJSON === 'function'
      ? subscription.toJSON()
      : subscription;
  const controller = new AbortController();
  const timerId = activeRuntime.setTimer(() => {
    controller.abort();
  }, budgetMs);
  let response;
  try {
    response = await activeRuntime.fetch(SUBSCRIPTIONS_ENDPOINT, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${accessToken}`,
      },
      body: JSON.stringify({ subscription: body }),
      signal: controller.signal,
    });
  } catch (cause) {
    if (controller.signal.aborted) {
      throw new Error(
        `push subscription was not persisted within ${PERSIST_TIMEOUT_MS} ms`,
        { cause },
      );
    }
    throw new Error('push subscription request failed', { cause });
  } finally {
    activeRuntime.clearTimer(timerId);
  }
  if (!response.ok) {
    throw new Error(
      `push subscription persistence responded HTTP ${response.status}`,
    );
  }
}

/**
 * Runs the full push registration flow: permission prompt, service
 * worker registration, push subscription with the configured VAPID key,
 * and persistence to the Session_Store within the 10-second budget
 * (Req 6.1). Denial performs no registration (Req 6.6); failures are
 * returned — never thrown — so the caller drives the retry UI (Req 6.7).
 *
 * DOM-free by design: all UI transitions live in the click handler, so
 * tests can drive this flow with fakes only.
 * @param {object} config - Portal runtime configuration; only
 *   `vapidPublicKey` is read.
 * @param {Partial<PushRuntime>} [overrides] - Test seams; each property
 *   replaces the corresponding browser default.
 * @returns {Promise<RegisterPushResult>} The outcome; see
 *   {@link RegisterPushResult} for the status classes.
 */
export async function registerPush(config, overrides = {}) {
  const activeRuntime = { ...createDefaultRuntime(), ...overrides };
  if (
    typeof activeRuntime.notification?.requestPermission !== 'function' ||
    typeof activeRuntime.serviceWorkerContainer?.register !== 'function'
  ) {
    return { status: 'unsupported' };
  }

  let permission;
  try {
    permission = await activeRuntime.notification.requestPermission();
  } catch (error) {
    return { status: 'error', error };
  }
  if (permission === 'denied') {
    return { status: 'denied' };
  }
  if (permission !== 'granted') {
    return { status: 'dismissed' };
  }

  const grantedAt = activeRuntime.now();
  try {
    const registration =
      await activeRuntime.serviceWorkerContainer.register(SERVICE_WORKER_URL);
    const subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(config.vapidPublicKey),
    });
    const budgetMs =
      PERSIST_TIMEOUT_MS - (activeRuntime.now() - grantedAt);
    await persistSubscription(subscription, activeRuntime, budgetMs);
    return { status: 'registered', subscription };
  } catch (error) {
    return {
      status: 'error',
      error: error instanceof Error ? error : new Error(String(error)),
    };
  }
}

/* -------------------------------------------------------------------- */
/* UI: button states, indications, and the error alert                   */
/* -------------------------------------------------------------------- */

/**
 * Looks up the push opt-in button in the wired document.
 * @returns {object | null} The button element, or null without a
 *   document or button markup.
 */
function getPushButton() {
  return runtime?.documentRef?.getElementById(PUSH_BUTTON_ID) ?? null;
}

/**
 * Renders (or replaces) the small muted indication next to the push
 * button, used for the push-disabled state (Req 6.6) and for unsupported
 * browsers. Content is inserted via `textContent`, never markup
 * injection.
 * @param {string} message - Plain-text indication to display.
 * @returns {void}
 */
function renderPushIndication(message) {
  const doc = runtime?.documentRef;
  const button = getPushButton();
  if (!doc || !button) {
    return;
  }
  doc.querySelector('[data-push-indicator]')?.remove();
  const indication = doc.createElement('small');
  indication.className = 'text-body-secondary';
  indication.dataset.pushIndicator = 'true';
  indication.setAttribute('role', 'status');
  indication.textContent = message;
  button.insertAdjacentElement('afterend', indication);
}

/**
 * Removes the push indication, when one is displayed.
 * @returns {void}
 */
function clearPushIndication() {
  runtime?.documentRef?.querySelector('[data-push-indicator]')?.remove();
}

/**
 * Shows the push registration error alert with the retry hint in the
 * page-level alert area, replacing any previous push alert (Req 6.7).
 * Follows the established alert pattern (`src/ui/errors.js`): content via
 * `textContent`, de-duplicated through the `data-error` attribute.
 * @param {string} detail - Plain-text failure detail appended to the
 *   curated message.
 * @returns {void}
 */
function renderPushError(detail) {
  const doc = runtime?.documentRef;
  const alertArea = doc?.getElementById('alert-area');
  if (!alertArea) {
    return;
  }
  alertArea.querySelector('[data-error="push-registration"]')?.remove();
  const alert = doc.createElement('div');
  alert.className = 'alert alert-danger';
  alert.setAttribute('role', 'alert');
  alert.dataset.error = 'push-registration';

  const heading = doc.createElement('p');
  heading.className = 'fw-bold mb-1';
  heading.textContent = 'Push notification registration failed';
  alert.appendChild(heading);

  const message = doc.createElement('p');
  message.className = 'mb-0';
  message.textContent =
    'Your push subscription could not be saved. Use the Enable push ' +
    'button to retry registration.';
  alert.appendChild(message);

  if (typeof detail === 'string' && detail !== '') {
    const supplement = doc.createElement('p');
    supplement.className = 'small text-body-secondary mb-0 mt-1';
    supplement.textContent = detail;
    alert.appendChild(supplement);
  }

  alertArea.appendChild(alert);
}

/**
 * Removes the push registration error alert, when one is displayed.
 * @returns {void}
 */
function clearPushError() {
  runtime?.documentRef
    ?.getElementById('alert-area')
    ?.querySelector('[data-error="push-registration"]')
    ?.remove();
}

/**
 * Applies the UI transition for a {@link registerPush} outcome:
 * `registered` renders the button as a disabled "Push enabled" success
 * indicator (Req 6.1); `denied` renders the push-disabled indication and
 * keeps the button disabled — the browser would not re-prompt (Req 6.6);
 * `dismissed` re-enables the button for another attempt; `unsupported`
 * renders an indication; `error` re-enables the button and shows the
 * retry alert (Req 6.7).
 * @param {RegisterPushResult} result - Outcome of the registration flow.
 * @param {object} button - The push opt-in button element.
 * @returns {void}
 */
function applyRegistrationOutcome(result, button) {
  switch (result.status) {
    case 'registered':
      clearPushError();
      clearPushIndication();
      button.textContent = 'Push enabled';
      button.classList.remove('btn-outline-primary');
      button.classList.add('btn-success');
      button.disabled = true;
      break;
    case 'denied':
      button.disabled = true;
      renderPushIndication(
        'Push notifications are disabled (permission denied).',
      );
      break;
    case 'unsupported':
      button.disabled = true;
      renderPushIndication(
        'Push notifications are not supported in this browser.',
      );
      break;
    case 'dismissed':
      button.disabled = false;
      break;
    default:
      button.disabled = false;
      renderPushError(result.error?.message ?? '');
      break;
  }
}

/**
 * Handles a click on the push opt-in button: runs {@link registerPush}
 * with the bootstrap configuration and applies the outcome to the UI.
 * The button is disabled for the duration of the flow to prevent
 * concurrent registrations.
 * @returns {Promise<void>} Resolves when the flow and UI update finished.
 */
async function handleEnablePushClick() {
  const button = getPushButton();
  if (!button || !pushConfig) {
    return;
  }
  button.disabled = true;
  const result = await registerPush(pushConfig, runtime ?? {});
  applyRegistrationOutcome(result, button);
}

/**
 * Delegated document click listener: reacts to clicks on (or inside) the
 * push opt-in button. Delegation keeps the wiring independent of when the
 * button markup appears and lets the route guard's capture-phase listener
 * intercept unauthenticated clicks first (Req 7.7).
 * @param {object} event - DOM click event.
 * @returns {void}
 */
function handleDocumentClick(event) {
  const target = event.target;
  if (
    typeof target?.closest !== 'function' ||
    !target.closest(`#${PUSH_BUTTON_ID}`)
  ) {
    return;
  }
  void handleEnablePushClick();
}

/* -------------------------------------------------------------------- */
/* Notification click-through (Req 6.4)                                  */
/* -------------------------------------------------------------------- */

/**
 * Reads the pending incident from module memory or the sessionStorage
 * stash (memory wins), tolerating unavailable storage and corrupted
 * content.
 * @returns {IncidentPayload | null} The pending incident, or null when
 *   none is stashed.
 */
function peekPendingIncident() {
  if (pendingIncidentMemory) {
    return pendingIncidentMemory;
  }
  try {
    const raw = runtime?.storage?.getItem(PENDING_INCIDENT_STORAGE_KEY);
    return raw ? normalizeIncident(JSON.parse(raw)) : null;
  } catch {
    return null;
  }
}

/**
 * Stashes a pending incident in module memory and sessionStorage; the
 * storage copy survives the Cognito Hosted UI redirect so the scoped
 * session still starts after sign-in (Req 6.4).
 * @param {IncidentPayload} incident - Normalized incident payload.
 * @returns {void}
 */
function stashPendingIncident(incident) {
  pendingIncidentMemory = incident;
  try {
    runtime?.storage?.setItem(
      PENDING_INCIDENT_STORAGE_KEY,
      JSON.stringify(incident),
    );
  } catch {
    // Storage unavailable — the in-memory copy still covers this load.
  }
}

/**
 * Clears the pending incident from module memory and sessionStorage.
 * @returns {void}
 */
function clearPendingIncident() {
  pendingIncidentMemory = null;
  try {
    runtime?.storage?.removeItem(PENDING_INCIDENT_STORAGE_KEY);
  } catch {
    // Storage unavailable — nothing persisted to remove.
  }
}

/**
 * Dispatches `portal:session-start` on the wired document with the
 * incident scoping fields, handing off to the voice WebSocket client
 * (which no-ops when unauthenticated): `executionId` scopes the session
 * to the incident (Req 6.4); summary/severity travel as
 * `incidentContext` when present.
 * @param {IncidentPayload} incident - Normalized incident payload.
 * @returns {void}
 */
function dispatchIncidentSession(incident) {
  const doc = runtime?.documentRef;
  if (!doc || !pushConfig) {
    return;
  }
  const hasContext = incident.summary !== null || incident.severity !== null;
  const CustomEventCtor = runtime.CustomEventCtor ?? globalThis.CustomEvent;
  doc.dispatchEvent(
    new CustomEventCtor('portal:session-start', {
      detail: {
        config: pushConfig,
        executionId: incident.executionId,
        incidentContext: hasContext
          ? { summary: incident.summary, severity: incident.severity }
          : null,
      },
    }),
  );
}

/**
 * Timer body for the authentication wait: re-runs the pending-incident
 * dispatch attempt.
 * @returns {void}
 */
function retryPendingDispatch() {
  authPollTimerId = null;
  schedulePendingIncidentDispatch();
}

/**
 * Dispatches the pending incident as soon as bootstrap and
 * authentication allow: with a loaded configuration and a valid session
 * the dispatch happens immediately; while authentication is still being
 * established (e.g. the Hosted UI token exchange after a redirect) the
 * check is retried every {@link AUTH_POLL_INTERVAL_MS} until
 * {@link AUTH_POLL_LIMIT_MS} elapses. This wait is how the module
 * "verifies an authenticated session" per Req 6.4 — the prompt itself is
 * the route guard's Cognito redirect.
 * @returns {void}
 */
function schedulePendingIncidentDispatch() {
  if (!runtime || !pushConfig) {
    return;
  }
  if (authPollTimerId !== null) {
    runtime.clearTimer(authPollTimerId);
    authPollTimerId = null;
  }
  const incident = peekPendingIncident();
  if (!incident) {
    authPollDeadline = null;
    return;
  }
  if (runtime.isAuthenticatedFn()) {
    clearPendingIncident();
    authPollDeadline = null;
    dispatchIncidentSession(incident);
    return;
  }
  authPollDeadline = authPollDeadline ?? runtime.now() + AUTH_POLL_LIMIT_MS;
  if (runtime.now() >= authPollDeadline) {
    authPollDeadline = null;
    return;
  }
  authPollTimerId = runtime.setTimer(
    retryPendingDispatch,
    AUTH_POLL_INTERVAL_MS,
  );
}

/**
 * `navigator.serviceWorker` message listener: a
 * `{type: "portal:incident-click", payload}` message means the engineer
 * clicked a system notification while this Portal window was open — the
 * service worker focused the window and forwarded the incident payload
 * (Req 6.4). The payload is queued and dispatched through the same
 * authenticated path as the URL-parameter flow.
 * @param {object} event - Message event from the service worker.
 * @returns {void}
 */
function handleServiceWorkerMessage(event) {
  const data = event?.data;
  if (!data || data.type !== INCIDENT_CLICK_MESSAGE_TYPE) {
    return;
  }
  stashPendingIncident(normalizeIncident(data.payload));
  schedulePendingIncidentDispatch();
}

/**
 * Extracts the `?incident=` parameter the service worker's `openWindow`
 * path appends (Req 6.4), removes it from the address bar (preserving
 * any other query parameters, e.g. an OAuth callback), and returns the
 * parsed payload. Malformed JSON yields null — the parameter is still
 * removed.
 * @param {PushRuntime} activeRuntime - Resolved runtime seams.
 * @returns {IncidentPayload | null} The normalized incident, or null when
 *   the parameter is absent or unparseable.
 */
function captureIncidentFromUrl(activeRuntime) {
  const loc = activeRuntime.locationRef;
  if (!loc) {
    return null;
  }
  const params = new URLSearchParams(loc.search ?? '');
  const raw = params.get(INCIDENT_QUERY_PARAM);
  if (raw === null) {
    return null;
  }
  params.delete(INCIDENT_QUERY_PARAM);
  const query = params.toString();
  try {
    activeRuntime.historyRef?.replaceState(
      null,
      '',
      `${loc.pathname}${query ? `?${query}` : ''}`,
    );
  } catch {
    // History unavailable — leaving the parameter in place is cosmetic.
  }
  try {
    return normalizeIncident(JSON.parse(raw));
  } catch {
    return null;
  }
}

/* -------------------------------------------------------------------- */
/* Wiring                                                                */
/* -------------------------------------------------------------------- */

/**
 * `portal:ready` listener: stores the bootstrap configuration (source of
 * the VAPID public key, Req 14.3) and attempts the pending-incident
 * dispatch now that the Portal is operational.
 * @param {object} event - `portal:ready` CustomEvent whose
 *   `detail.config` is the loaded Portal configuration.
 * @returns {void}
 */
function handlePortalReady(event) {
  pushConfig = event?.detail?.config ?? null;
  schedulePendingIncidentDispatch();
}

/**
 * Wires the push manager to its environment: the delegated click
 * listener for `#btn-push-enable`, the `portal:ready` configuration
 * listener, the service worker message listener, and the `?incident=`
 * URL-parameter capture. Listener registration is idempotent (stable
 * function references), so calling this more than once is safe.
 * @param {Partial<PushRuntime>} [overrides] - Test seams; each property
 *   replaces the corresponding browser default.
 * @returns {void}
 */
export function wirePushManager(overrides = {}) {
  runtime = { ...createDefaultRuntime(), ...overrides };
  pushConfig = null;
  pendingIncidentMemory = null;
  if (authPollTimerId !== null) {
    runtime.clearTimer(authPollTimerId);
    authPollTimerId = null;
  }
  authPollDeadline = null;

  const doc = runtime.documentRef;
  if (!doc) {
    return;
  }
  doc.addEventListener('portal:ready', handlePortalReady);
  doc.addEventListener('click', handleDocumentClick);
  runtime.serviceWorkerContainer?.addEventListener?.(
    'message',
    handleServiceWorkerMessage,
  );

  const incident = captureIncidentFromUrl(runtime);
  if (incident) {
    stashPendingIncident(incident);
  }
}

// Self-wire on import so main.js only needs a side-effect import; guarded
// for document-less environments (plain Node test runners).
if (typeof document !== 'undefined') {
  wirePushManager();
}
