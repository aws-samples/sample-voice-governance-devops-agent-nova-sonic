/**
 * AppSync Events realtime client for incident notifications
 * (Req 5.2, 7.4).
 *
 * Subscribes to the `/incidents/all` broadcast channel of the AppSync
 * Events API over its realtime WebSocket protocol and re-dispatches
 * every received Incident_Notification as a `portal:incident`
 * CustomEvent on `document`, where the notification UI
 * (`src/ui/incident-popup.js`) renders the popup, the feed entry, and
 * the chime (Req 5.2-5.4).
 *
 * Protocol, per the AppSync Events WebSocket documentation
 * (https://docs.aws.amazon.com/appsync/latest/eventapi/event-api-websocket-protocol.html):
 * 1. open a WebSocket to `config.events.realtimeEndpoint` with two
 *    subprotocols — `aws-appsync-event-ws` and the connection
 *    authorization encoded as `header-<base64url(JSON headers)>`;
 * 2. send `{"type":"connection_init"}`, wait for `connection_ack`
 *    (which carries `connectionTimeoutMs`, the keep-alive budget);
 * 3. send a `subscribe` message with a unique id, the channel path, and
 *    an authorization object; wait for `subscribe_success`;
 * 4. consume `ka` keep-alives (each one re-arms a watchdog that closes
 *    the socket when the budget elapses without one) and `data`
 *    messages, whose `event` field is a JSON-encoded string;
 * 5. on close, reconnect with jittered exponential backoff.
 *
 * Authorization: the Events API authorizes connect/subscribe with the
 * Cognito user pool (Req 7.4). The headers object is
 * `{host, Authorization}` where `host` is the HTTP endpoint's hostname
 * and `Authorization` is the Cognito **ID token** — the standard token
 * for AppSync user pool authorization (the documented header content is
 * "a JWT ID token"; access tokens are for OAuth resource servers). The
 * token comes from `getIdToken()` in `src/auth/cognito.js`.
 *
 * Start-up: the module self-wires on import (side-effect import from
 * `src/main.js`) and starts on the `portal:ready` event. Because
 * Cognito initialization is asynchronous, the client may observe no
 * token yet at that point; it then waits and re-checks every
 * {@link AUTH_RETRY_DELAY_MS} up to {@link MAX_AUTH_ATTEMPTS} times
 * (3 s x 40 = 2 minutes — ample for the post-sign-in token restore,
 * bounded so a signed-out tab does not poll forever). Unauthenticated
 * tabs are redirected to the Hosted UI by the auth module anyway, and
 * the page reload after sign-in restarts this client.
 */

import { getIdToken, isAuthenticated } from '../auth/cognito.js';

/**
 * An incident notification produced by the Notifier's normalizer and
 * broadcast on the Events_Channel (design: Incident_Notification
 * Payload Schema).
 * @typedef {object} IncidentNotification
 * @property {string} notificationId - Unique notification identifier.
 * @property {string} source - Originating source: `cloudwatch-alarm`,
 *   `incident-manager`, or `devops-agent-finding`.
 * @property {string} summary - Human-readable incident summary.
 * @property {string} severity - `critical`, `high`, `medium`, or `low`.
 * @property {string} timestamp - ISO-8601 UTC timestamp.
 * @property {string} [executionId] - DevOps Agent execution id, present
 *   iff the source event provided one (Req 5.6).
 * @property {object} [detail] - Source-specific extras.
 */

/**
 * Connection/subscription authorization headers for Cognito user pool
 * auth on the Events API.
 * @typedef {object} EventsAuthorization
 * @property {string} host - Hostname of the Events HTTP endpoint (used
 *   to validate the connection even though the socket targets the
 *   realtime endpoint).
 * @property {string} Authorization - Cognito-issued JWT ID token.
 */

/**
 * Injectable environment seams so tests can substitute fakes; every
 * property defaults to the browser global or the Cognito auth module.
 * @typedef {object} EventsClientRuntime
 * @property {Function} WebSocketCtor - WebSocket constructor, called
 *   with `(url, subprotocols)`.
 * @property {Function} CustomEventCtor - CustomEvent constructor used
 *   for `portal:incident` dispatch.
 * @property {object | undefined} documentRef - Document on which events
 *   are (un)wired and dispatched; undefined outside a DOM.
 * @property {() => string | null} getIdTokenFn - Returns the current
 *   Cognito ID token, or null when unauthenticated.
 * @property {() => boolean} isAuthenticatedFn - Reports whether the
 *   engineer currently holds valid tokens.
 * @property {(callback: Function, delayMs: number) => *} setTimer -
 *   Timer scheduler, `setTimeout`-shaped.
 * @property {(timerId: *) => void} clearTimer - Timer canceller,
 *   `clearTimeout`-shaped.
 * @property {() => number} random - Uniform [0,1) source for backoff
 *   jitter.
 * @property {() => string} randomId - Generates a subscription id
 *   matching the protocol's `/^[a-zA-Z0-9-_+]{1,128}$/` constraint.
 */

/**
 * Broadcast channel carrying every incident notification.
 * @type {string}
 */
export const INCIDENTS_CHANNEL = '/incidents/all';

/**
 * Mandatory AppSync Events realtime subprotocol name.
 * @type {string}
 */
export const APPSYNC_EVENTS_SUBPROTOCOL = 'aws-appsync-event-ws';

/**
 * Delay between authentication re-checks while waiting for the Cognito
 * token after `portal:ready`.
 * @type {number}
 */
export const AUTH_RETRY_DELAY_MS = 3_000;

/**
 * Upper bound on authentication re-checks (40 x 3 s = 2 minutes) so an
 * unauthenticated tab does not poll indefinitely.
 * @type {number}
 */
export const MAX_AUTH_ATTEMPTS = 40;

/**
 * First-attempt reconnect backoff base.
 * @type {number}
 */
const BASE_BACKOFF_MS = 1_000;

/**
 * Reconnect backoff ceiling.
 * @type {number}
 */
const MAX_BACKOFF_MS = 30_000;

/**
 * Reconnect backoff floor, preventing a hot reconnect loop when the
 * jitter draw lands near zero.
 * @type {number}
 */
const MIN_BACKOFF_MS = 250;

/**
 * Keep-alive watchdog budget used until `connection_ack` reports the
 * authoritative `connectionTimeoutMs` (the documented default is 5
 * minutes).
 * @type {number}
 */
const DEFAULT_KA_TIMEOUT_MS = 300_000;

/* -------------------------------------------------------------------- */
/* Pure helpers (exported for direct testing)                            */
/* -------------------------------------------------------------------- */

/**
 * Builds the Cognito user pool authorization headers for the Events
 * API: the HTTP endpoint's hostname plus the ID token (see the module
 * doc for the ID-token choice).
 * @param {string} httpEndpoint - AppSync Events HTTP endpoint URL, e.g.
 *   `https://xxx.appsync-api.region.amazonaws.com/event`.
 * @param {string} idToken - Cognito-issued JWT ID token.
 * @returns {EventsAuthorization} The authorization headers object.
 * @throws {TypeError} When `httpEndpoint` is not an absolute URL.
 */
export function buildAuthorization(httpEndpoint, idToken) {
  return { host: new URL(httpEndpoint).host, Authorization: idToken };
}

/**
 * Encodes an authorization headers object into the `header-` WebSocket
 * subprotocol value: `header-<base64url(JSON)>` without padding. `btoa`
 * suffices because the payload is ASCII by construction (a hostname and
 * a base64url-encoded JWT).
 * @param {EventsAuthorization} authorization - Authorization headers.
 * @returns {string} The `header-`-prefixed base64url subprotocol value.
 */
export function encodeAuthProtocol(authorization) {
  const encoded = btoa(JSON.stringify(authorization))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/u, '');
  return `header-${encoded}`;
}

/**
 * Builds the WebSocket subprotocol list for the realtime handshake: the
 * mandatory `aws-appsync-event-ws` plus the encoded authorization.
 * @param {EventsAuthorization} authorization - Authorization headers.
 * @returns {string[]} Subprotocols for the WebSocket constructor.
 */
export function buildSubprotocols(authorization) {
  return [APPSYNC_EVENTS_SUBPROTOCOL, encodeAuthProtocol(authorization)];
}

/**
 * Computes the jittered exponential reconnect delay for a given attempt
 * ("full jitter": uniform over (0, min(cap, base * 2^attempt)]), floored
 * at {@link MIN_BACKOFF_MS} so retries never spin hot.
 * @param {number} attempt - Zero-based reconnect attempt counter.
 * @param {() => number} [random] - Uniform [0,1) source; defaults to
 *   `Math.random`.
 * @returns {number} Delay in milliseconds before the next attempt.
 */
export function computeBackoffDelayMs(attempt, random = Math.random) {
  const cap = Math.min(MAX_BACKOFF_MS, BASE_BACKOFF_MS * 2 ** attempt);
  return Math.max(MIN_BACKOFF_MS, Math.round(random() * cap));
}

/**
 * Parses the `event` field of a `data` message — a JSON-encoded string
 * per the protocol — into an incident payload. Total over arbitrary
 * input: anything unparseable or non-object yields null.
 * @param {unknown} eventText - The raw `event` field value.
 * @returns {IncidentNotification | null} The parsed payload, or null
 *   when the field is not a JSON object string.
 */
export function parseIncidentEvent(eventText) {
  if (typeof eventText !== 'string') {
    return null;
  }
  let parsed;
  try {
    parsed = JSON.parse(eventText);
  } catch {
    return null;
  }
  return parsed !== null && typeof parsed === 'object' ? parsed : null;
}

/* -------------------------------------------------------------------- */
/* Client                                                                */
/* -------------------------------------------------------------------- */

/**
 * Default subscription-id generator: a UUID when Web Crypto provides
 * one, otherwise a time-and-random token; both match the protocol's id
 * character set.
 * @returns {string} A unique subscription id.
 */
function defaultRandomId() {
  if (typeof globalThis.crypto?.randomUUID === 'function') {
    return globalThis.crypto.randomUUID();
  }
  const time = Date.now().toString(36);
  const noise = Math.random().toString(36).slice(2, 10);
  return `sub-${time}-${noise}`;
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
 * Builds the default runtime bound to the browser globals and the
 * Cognito auth module; every seam is individually overridable through
 * the {@link IncidentEventsClient} constructor.
 * @returns {EventsClientRuntime} Runtime seams backed by the browser.
 */
function createDefaultRuntime() {
  return {
    WebSocketCtor: globalThis.WebSocket,
    CustomEventCtor: globalThis.CustomEvent,
    documentRef: globalThis.document,
    getIdTokenFn: getIdToken,
    isAuthenticatedFn: isAuthenticated,
    setTimer: defaultSetTimer,
    clearTimer: defaultClearTimer,
    random: Math.random,
    randomId: defaultRandomId,
  };
}

/**
 * Realtime subscriber for the `/incidents/all` channel: maintains one
 * authorized WebSocket to the Events API, keeps the subscription alive
 * across drops with jittered backoff, and dispatches every received
 * notification as a `portal:incident` CustomEvent (Req 5.2, 7.4).
 */
export class IncidentEventsClient {
  /** @type {EventsClientRuntime} */
  #runtime;

  /** @type {object | null} */
  #config = null;

  /** @type {object | null} */
  #socket = null;

  /** @type {string | null} */
  #subscriptionId = null;

  /** @type {boolean} */
  #stopped = true;

  /** @type {boolean} */
  #wired = false;

  /** @type {number} */
  #authAttempts = 0;

  /** @type {number} */
  #reconnectAttempts = 0;

  /** @type {*} */
  #retryTimerId = null;

  /** @type {*} */
  #kaTimerId = null;

  /** @type {number} */
  #kaTimeoutMs = DEFAULT_KA_TIMEOUT_MS;

  /**
   * Creates a client. No listeners are attached until
   * {@link IncidentEventsClient#init}.
   * @param {Partial<EventsClientRuntime>} [overrides] - Test seams; each
   *   property replaces the corresponding default.
   */
  constructor(overrides = {}) {
    this.#runtime = { ...createDefaultRuntime(), ...overrides };
  }

  /**
   * Reports whether the client has been started and not stopped
   * (introspection for tests).
   * @returns {boolean} True while the client is running.
   */
  get running() {
    return !this.#stopped;
  }

  /**
   * `portal:ready` listener: starts the client with the bootstrap
   * configuration carried in the event detail.
   * @param {CustomEvent} event - Event whose `detail.config` is the
   *   loaded Portal configuration.
   * @returns {void}
   */
  #onPortalReady = (event) => {
    try {
      this.start(event?.detail?.config);
    } catch (error) {
      console.error('appsync-client: could not start.', error);
    }
  };

  /**
   * Socket `open` handler: initiates the connection session per the
   * protocol.
   * @returns {void}
   */
  #onSocketOpen = () => {
    this.#send({ type: 'connection_init' });
  };

  /**
   * Socket `message` handler: re-arms the keep-alive watchdog (any
   * traffic proves liveness) and routes the message by type.
   * @param {MessageEvent} event - Raw WebSocket message event.
   * @returns {void}
   */
  #onSocketMessage = (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    this.#resetKaWatchdog();
    switch (message?.type) {
      case 'connection_ack':
        if (Number.isFinite(message.connectionTimeoutMs)) {
          this.#kaTimeoutMs = message.connectionTimeoutMs;
          this.#resetKaWatchdog();
        }
        this.#subscribe();
        break;
      case 'ka':
        break;
      case 'subscribe_success':
        if (message.id === this.#subscriptionId) {
          this.#reconnectAttempts = 0;
        }
        break;
      case 'data':
        this.#handleData(message);
        break;
      case 'subscribe_error':
      case 'connection_error':
      case 'error':
        console.error('appsync-client: server reported an error.', message);
        this.#socket?.close();
        break;
      default:
        break;
    }
  };

  /**
   * Socket `close` handler: schedules a jittered-backoff reconnect
   * unless the client was stopped deliberately.
   * @returns {void}
   */
  #onSocketClose = () => {
    this.#clearKaWatchdog();
    this.#socket = null;
    this.#subscriptionId = null;
    if (this.#stopped) {
      return;
    }
    const delay = computeBackoffDelayMs(
      this.#reconnectAttempts,
      this.#runtime.random,
    );
    this.#reconnectAttempts += 1;
    this.#retryTimerId = this.#runtime.setTimer(() => {
      this.#retryTimerId = null;
      // Re-checks authentication first: the token may have expired (or
      // refreshed) while the socket was down.
      this.#authAttempts = 0;
      this.#connectWhenAuthenticated();
    }, delay);
  };

  /**
   * Socket `error` handler: logs only — the browser always follows an
   * error with a `close` event, which drives the reconnect.
   * @returns {void}
   */
  #onSocketError = () => {
    console.error('appsync-client: WebSocket error; will reconnect.');
  };

  /**
   * Attaches the `portal:ready` document listener. Safe to call more
   * than once; no-ops without a document.
   * @returns {IncidentEventsClient} This client, for chaining.
   */
  init() {
    const doc = this.#runtime.documentRef;
    if (!doc || this.#wired) {
      return this;
    }
    doc.addEventListener('portal:ready', this.#onPortalReady);
    this.#wired = true;
    return this;
  }

  /**
   * Detaches the document listener and stops the client (test and
   * teardown hook).
   * @returns {void}
   */
  dispose() {
    const doc = this.#runtime.documentRef;
    if (doc && this.#wired) {
      doc.removeEventListener('portal:ready', this.#onPortalReady);
    }
    this.#wired = false;
    this.stop();
  }

  /**
   * Starts (or restarts) the realtime subscription for the given Portal
   * configuration. Connection is attempted only once the engineer is
   * authenticated; until then the client re-checks on a bounded
   * schedule (see the module doc).
   * @param {object} config - Portal runtime configuration; reads
   *   `events.httpEndpoint` and `events.realtimeEndpoint`.
   * @returns {void}
   * @throws {Error} When the configuration lacks the Events endpoints.
   */
  start(config) {
    const events = config?.events;
    if (
      typeof events?.httpEndpoint !== 'string' ||
      events.httpEndpoint === '' ||
      typeof events.realtimeEndpoint !== 'string' ||
      events.realtimeEndpoint === ''
    ) {
      throw new Error(
        'appsync-client requires config.events.httpEndpoint and ' +
          'config.events.realtimeEndpoint (from config.json)',
      );
    }
    this.stop();
    this.#config = config;
    this.#stopped = false;
    this.#authAttempts = 0;
    this.#reconnectAttempts = 0;
    this.#connectWhenAuthenticated();
  }

  /**
   * Stops the client: cancels pending timers and closes the socket. The
   * close handler sees the stopped flag and does not reconnect.
   * @returns {void}
   */
  stop() {
    this.#stopped = true;
    if (this.#retryTimerId !== null) {
      this.#runtime.clearTimer(this.#retryTimerId);
      this.#retryTimerId = null;
    }
    this.#clearKaWatchdog();
    this.#socket?.close();
    this.#socket = null;
    this.#subscriptionId = null;
  }

  /**
   * Connects immediately when a Cognito token is available; otherwise
   * waits {@link AUTH_RETRY_DELAY_MS} and re-checks, up to
   * {@link MAX_AUTH_ATTEMPTS} times (Req 7.4: connect only with a valid
   * Cognito authorization).
   * @returns {void}
   */
  #connectWhenAuthenticated() {
    if (this.#stopped) {
      return;
    }
    if (this.#runtime.isAuthenticatedFn() && this.#runtime.getIdTokenFn()) {
      this.#connect();
      return;
    }
    if (this.#authAttempts >= MAX_AUTH_ATTEMPTS) {
      console.error(
        'appsync-client: no Cognito token after ' +
          `${MAX_AUTH_ATTEMPTS} checks; giving up until the next start.`,
      );
      return;
    }
    this.#authAttempts += 1;
    this.#retryTimerId = this.#runtime.setTimer(() => {
      this.#retryTimerId = null;
      this.#connectWhenAuthenticated();
    }, AUTH_RETRY_DELAY_MS);
  }

  /**
   * Opens the WebSocket to the realtime endpoint with the mandatory and
   * authorization subprotocols. A construction failure (malformed
   * endpoint) is terminal for this start — it is a configuration error
   * a retry cannot fix.
   * @returns {void}
   */
  #connect() {
    const token = this.#runtime.getIdTokenFn();
    if (!token) {
      this.#connectWhenAuthenticated();
      return;
    }
    let socket;
    try {
      const authorization = buildAuthorization(
        this.#config.events.httpEndpoint,
        token,
      );
      socket = new this.#runtime.WebSocketCtor(
        this.#config.events.realtimeEndpoint,
        buildSubprotocols(authorization),
      );
    } catch (error) {
      console.error(
        'appsync-client: could not open the realtime connection; check ' +
          'the events endpoints in config.json.',
        error,
      );
      this.#stopped = true;
      return;
    }
    this.#socket = socket;
    socket.onopen = this.#onSocketOpen;
    socket.onmessage = this.#onSocketMessage;
    socket.onclose = this.#onSocketClose;
    socket.onerror = this.#onSocketError;
  }

  /**
   * Sends a protocol message over the current socket as JSON, ignoring
   * sockets that are gone or no longer open.
   * @param {object} message - Protocol message to serialize.
   * @returns {void}
   */
  #send(message) {
    try {
      this.#socket?.send(JSON.stringify(message));
    } catch (error) {
      console.error('appsync-client: send failed.', error);
    }
  }

  /**
   * Registers the `/incidents/all` subscription. The authorization is
   * rebuilt with the current ID token — it may have been refreshed
   * since the connection opened. Without a token the socket is closed,
   * which routes back through the authentication wait.
   * @returns {void}
   */
  #subscribe() {
    const token = this.#runtime.getIdTokenFn();
    if (!token) {
      this.#socket?.close();
      return;
    }
    this.#subscriptionId = this.#runtime.randomId();
    this.#send({
      type: 'subscribe',
      id: this.#subscriptionId,
      channel: INCIDENTS_CHANNEL,
      authorization: buildAuthorization(
        this.#config.events.httpEndpoint,
        token,
      ),
    });
  }

  /**
   * Handles a `data` message: parses the JSON-encoded `event` field and
   * dispatches the payload as a `portal:incident` CustomEvent for the
   * notification UI (Req 5.2).
   * @param {object} message - Parsed `data` protocol message.
   * @returns {void}
   */
  #handleData(message) {
    const payload = parseIncidentEvent(message?.event);
    if (!payload) {
      console.error(
        'appsync-client: discarded a data message whose event field was ' +
          'not a JSON object.',
      );
      return;
    }
    const doc = this.#runtime.documentRef;
    if (!doc) {
      return;
    }
    doc.dispatchEvent(
      new this.#runtime.CustomEventCtor('portal:incident', {
        detail: payload,
      }),
    );
  }

  /**
   * Re-arms the keep-alive watchdog: when no message (`ka` included)
   * arrives within the `connectionTimeoutMs` budget, the connection is
   * considered dead and closed, which triggers the reconnect path.
   * @returns {void}
   */
  #resetKaWatchdog() {
    this.#clearKaWatchdog();
    this.#kaTimerId = this.#runtime.setTimer(() => {
      this.#kaTimerId = null;
      console.error(
        'appsync-client: keep-alive timeout; closing for reconnect.',
      );
      this.#socket?.close();
    }, this.#kaTimeoutMs);
  }

  /**
   * Cancels the keep-alive watchdog, when armed.
   * @returns {void}
   */
  #clearKaWatchdog() {
    if (this.#kaTimerId !== null) {
      this.#runtime.clearTimer(this.#kaTimerId);
      this.#kaTimerId = null;
    }
  }
}

/**
 * Module-level default client, self-wired on import so `src/main.js`
 * only needs a side-effect import; guarded for document-less
 * environments (plain Node test runners).
 * @type {IncidentEventsClient | null}
 */
export const defaultIncidentEventsClient =
  typeof document !== 'undefined' ? new IncidentEventsClient().init() : null;
