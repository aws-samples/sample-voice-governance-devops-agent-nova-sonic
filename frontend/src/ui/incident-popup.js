/**
 * Incident notification popup, feed, and chime (Req 5.2-5.4, 5.7, 5.8).
 *
 * The AppSync Events client (`src/events/appsync-client.js`) relays
 * every Incident_Notification as a `portal:incident` CustomEvent on
 * `document` whose detail is the payload. This module listens for those
 * events and, synchronously on receipt — trivially within the 2-second
 * display bound of Req 5.2 —
 * 1. renders a Bootstrap toast into `#toast-area` showing the incident
 *    summary, a severity badge, and the timestamp;
 * 2. appends a persistent entry to the `#notifications-list` feed
 *    (replacing its placeholder before the first entry);
 * 3. plays a short audio chime (Req 5.3). The popup is rendered before
 *    the chime is attempted, and every chime failure mode — no Web
 *    Audio, autoplay policy keeping the context suspended, scheduling
 *    errors — is swallowed, so a blocked or failed chime can never
 *    suppress the notification (Req 5.4).
 *
 * Duplicate deliveries are detected by `notificationId` and skipped
 * entirely (no second toast, feed entry, or chime); the seen-id set is
 * scoped to the tab session.
 *
 * Clicking a toast or a feed entry opens a Voice_Session: the click
 * dispatches `portal:session-start` (consumed by
 * `src/ws/voice-client.js`) carrying the runtime config plus either the
 * payload's `executionId` (Req 5.7) or, when the payload has none, an
 * `incidentContext` of `{summary, severity}` (Req 5.8). The toast is
 * dismissed after the click.
 *
 * Chime design: a two-tone beep generated with `OscillatorNode`s on a
 * lazily created shared `AudioContext` — no audio asset to load, cache,
 * or deploy, and the synthesis is a handful of lines. The context is
 * created on first use and reused; when the browser's autoplay policy
 * leaves it `suspended`, `resume()` is attempted and any rejection is
 * ignored (the tones are scheduled regardless and simply stay silent
 * until a user gesture unlocks audio).
 *
 * {@link renderIncidentPopup} is a pure builder over an injected
 * document (no lookups, no globals) so property tests (task 9.9,
 * Property 18) can exercise it against jsdom documents directly. All
 * content is inserted via `textContent`, never markup injection.
 */

import { getConfig } from '../main.js';

/**
 * An incident notification payload, as carried by the `portal:incident`
 * event detail (design: Incident_Notification Payload Schema).
 * @typedef {import('../events/appsync-client.js').IncidentNotification} IncidentNotification
 */

/**
 * Bootstrap contextual variant per normalized severity: critical and
 * high render as `danger`, medium as `warning`, low as `info`. Exported
 * so tests can assert the mapping without duplicating class strings.
 * @type {Readonly<Record<string, string>>}
 */
export const SEVERITY_BADGE_VARIANTS = Object.freeze({
  critical: 'danger',
  high: 'danger',
  medium: 'warning',
  low: 'info',
});

/**
 * Bootstrap variant used for severities outside the documented set, so
 * rendering is total over arbitrary payloads.
 * @type {string}
 */
export const DEFAULT_BADGE_VARIANT = 'secondary';

/**
 * How long a toast stays on screen before auto-dismissing.
 * @type {number}
 */
export const TOAST_AUTOHIDE_MS = 15_000;

/**
 * Notification ids already rendered in this tab session, used to skip
 * duplicate deliveries.
 * @type {Set<string>}
 */
const shownNotificationIds = new Set();

/** @type {AudioContext | null} */
let sharedAudioContext = null;

/**
 * Forgets all seen notification ids (test hook).
 * @returns {void}
 */
export function clearShownNotifications() {
  shownNotificationIds.clear();
}

/**
 * Normalizes a severity value and resolves its Bootstrap badge variant.
 * @param {unknown} severity - Severity value from a payload.
 * @returns {{severity: string, variant: string}} The lower-cased
 *   severity (or `"unknown"` for blank input) and its badge variant.
 */
export function severityPresentation(severity) {
  const normalized = String(severity ?? '')
    .trim()
    .toLowerCase();
  return {
    severity: normalized === '' ? 'unknown' : normalized,
    // Own-key guard (as in status.js/errors.js): payload severities like
    // "constructor" must not resolve Object.prototype members.
    variant: Object.hasOwn(SEVERITY_BADGE_VARIANTS, normalized)
      ? SEVERITY_BADGE_VARIANTS[normalized]
      : DEFAULT_BADGE_VARIANT,
  };
}

/**
 * Formats an ISO-8601 timestamp for display as a local date and time.
 * Total over arbitrary input: nullish/blank values yield an empty
 * string and unparseable values are returned verbatim so information is
 * never lost.
 * @param {unknown} timestamp - Timestamp value from a payload.
 * @returns {string} The formatted local date-time, the verbatim input
 *   when it cannot be parsed as a date, or an empty string for nullish
 *   input.
 */
function formatTimestamp(timestamp) {
  if (timestamp === undefined || timestamp === null || timestamp === '') {
    return '';
  }
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    return String(timestamp);
  }
  return date.toLocaleString();
}

/**
 * Builds the severity badge element shared by the toast and the feed
 * entry, marked `data-severity-badge` for tests.
 * @param {unknown} severity - Severity value from a payload.
 * @param {Document} doc - Document used to create the element.
 * @returns {Element} The badge span carrying the severity text.
 */
function createSeverityBadge(severity, doc) {
  const presentation = severityPresentation(severity);
  const badge = doc.createElement('span');
  badge.className = `badge text-bg-${presentation.variant} me-2`;
  badge.dataset.severityBadge = presentation.severity;
  badge.textContent = presentation.severity;
  return badge;
}

/**
 * Builds the timestamp element shared by the toast and the feed entry,
 * marked `data-timestamp` (raw payload value) for tests.
 * @param {unknown} timestamp - Timestamp value from a payload.
 * @param {Document} doc - Document used to create the element.
 * @returns {Element} The `<time>` element with the display text.
 */
function createTimestamp(timestamp, doc) {
  const time = doc.createElement('time');
  time.className = 'small text-body-secondary';
  const raw = typeof timestamp === 'string' ? timestamp : '';
  time.dataset.timestamp = raw;
  if (raw !== '') {
    time.setAttribute('datetime', raw);
  }
  time.textContent = formatTimestamp(timestamp);
  return time;
}

/**
 * Builds the DOM element for one incident popup: a Bootstrap toast
 * carrying the severity badge, the source, the timestamp, a dismiss
 * button, and the summary. Pure with respect to its inputs — the
 * element is created on the injected document and not attached
 * anywhere, so tests can render popups without page markup. Test
 * contract: the root carries `data-incident-popup`,
 * `data-notification-id`, and `data-severity`; the summary, severity
 * badge, and timestamp are marked `data-summary`,
 * `data-severity-badge`, and `data-timestamp` (Req 5.2).
 * @param {IncidentNotification} payload - Incident notification to
 *   render.
 * @param {Document} [doc] - Document used to create elements; defaults
 *   to the global document when one exists (tests pass a jsdom
 *   document).
 * @returns {Element} The detached toast element.
 */
export function renderIncidentPopup(payload, doc = globalThis.document) {
  const { notificationId, source, summary, severity, timestamp } =
    payload ?? {};
  const presentation = severityPresentation(severity);

  const toast = doc.createElement('div');
  toast.className = 'toast show';
  toast.setAttribute('role', 'alert');
  toast.setAttribute('aria-live', 'assertive');
  toast.setAttribute('aria-atomic', 'true');
  toast.dataset.incidentPopup = '';
  toast.dataset.notificationId = String(notificationId ?? '');
  toast.dataset.severity = presentation.severity;

  const header = doc.createElement('div');
  header.className = 'toast-header';
  header.appendChild(createSeverityBadge(severity, doc));

  const sourceLabel = doc.createElement('strong');
  sourceLabel.className = 'me-auto text-truncate';
  sourceLabel.dataset.source = '';
  sourceLabel.textContent = String(source ?? 'incident');
  header.appendChild(sourceLabel);

  header.appendChild(createTimestamp(timestamp, doc));

  const close = doc.createElement('button');
  close.type = 'button';
  close.className = 'btn-close ms-2';
  close.setAttribute('data-bs-dismiss', 'toast');
  close.setAttribute('aria-label', 'Dismiss notification');
  header.appendChild(close);

  toast.appendChild(header);

  const body = doc.createElement('div');
  body.className = 'toast-body';
  body.dataset.summary = '';
  body.textContent = String(summary ?? '');
  toast.appendChild(body);

  return toast;
}

/**
 * Builds the persistent feed entry for the `#notifications-list` panel:
 * a real button (keyboard accessible) carrying the same badge, summary,
 * and timestamp marks as the popup.
 * @param {IncidentNotification} payload - Incident notification to
 *   render.
 * @param {Document} doc - Document used to create elements.
 * @returns {Element} The detached feed entry element, marked with
 *   `data-notification-entry`.
 */
function renderNotificationEntry(payload, doc) {
  const { notificationId, summary, severity, timestamp } = payload ?? {};
  const entry = doc.createElement('button');
  entry.type = 'button';
  entry.className =
    'list-group-item list-group-item-action rounded border w-100 ' +
    'text-start mb-2';
  entry.dataset.notificationEntry = '';
  entry.dataset.notificationId = String(notificationId ?? '');

  const header = doc.createElement('div');
  header.className =
    'd-flex justify-content-between align-items-baseline gap-2 mb-1';
  header.appendChild(createSeverityBadge(severity, doc));
  header.appendChild(createTimestamp(timestamp, doc));
  entry.appendChild(header);

  const body = doc.createElement('div');
  body.dataset.summary = '';
  body.textContent = String(summary ?? '');
  entry.appendChild(body);

  return entry;
}

/**
 * Opens a Voice_Session for a clicked notification by dispatching
 * `portal:session-start` (consumed by the voice WebSocket client): the
 * detail carries the runtime config plus the payload's `executionId`
 * when present (Req 5.7), or an `incidentContext` of the summary and
 * severity when absent (Req 5.8).
 * @param {IncidentNotification} payload - Payload of the clicked
 *   notification.
 * @param {Document} doc - Document on which to dispatch the event.
 * @returns {void}
 */
export function startIncidentSession(payload, doc = globalThis.document) {
  if (!doc) {
    return;
  }
  const executionId = payload?.executionId ?? null;
  doc.dispatchEvent(
    new CustomEvent('portal:session-start', {
      detail: {
        config: getConfig(),
        executionId,
        incidentContext: executionId
          ? null
          : {
              summary: String(payload?.summary ?? ''),
              severity: String(payload?.severity ?? ''),
            },
      },
    }),
  );
}

/**
 * Plays the notification chime: two short sine tones (A5 then E6)
 * scheduled on the shared `AudioContext` with a linear gain envelope to
 * avoid clicks. Never throws — any failure (no Web Audio support,
 * autoplay policy, scheduling error) yields `false` so the caller's
 * popup path is never disturbed (Req 5.3, 5.4).
 * @param {typeof AudioContext} [AudioContextCtor] - AudioContext
 *   constructor, injectable for tests; defaults to the browser's
 *   (webkit-prefixed as a fallback).
 * @returns {boolean} True when the tones were scheduled, false when the
 *   chime was unavailable or scheduling failed.
 */
export function playChime(AudioContextCtor) {
  try {
    if (!sharedAudioContext) {
      const Ctor =
        AudioContextCtor ??
        globalThis.AudioContext ??
        globalThis.webkitAudioContext;
      if (typeof Ctor !== 'function') {
        return false;
      }
      sharedAudioContext = new Ctor();
    }
    const context = sharedAudioContext;
    if (context.state === 'suspended') {
      // Autoplay policy may reject the resume; the rejection is
      // swallowed and the popup is unaffected (Req 5.4).
      context.resume?.()?.catch?.(() => {});
    }
    const startAt = context.currentTime + 0.01;
    const tones = [
      { frequency: 880, offset: 0, duration: 0.15 },
      { frequency: 1318.5, offset: 0.17, duration: 0.22 },
    ];
    for (const tone of tones) {
      const oscillator = context.createOscillator();
      const gain = context.createGain();
      oscillator.type = 'sine';
      oscillator.frequency.value = tone.frequency;
      const from = startAt + tone.offset;
      const until = from + tone.duration;
      gain.gain.setValueAtTime(0, from);
      gain.gain.linearRampToValueAtTime(0.25, from + 0.02);
      gain.gain.linearRampToValueAtTime(0, until);
      oscillator.connect(gain).connect(context.destination);
      oscillator.start(from);
      oscillator.stop(until + 0.05);
    }
    return true;
  } catch {
    return false;
  }
}

/**
 * Dismisses a toast element, delegating to Bootstrap's Toast plugin
 * when it is loaded (CDN bundle in `index.html`) and removing the
 * element directly otherwise (jsdom, plugin blocked).
 * @param {Element} toast - Toast element to dismiss.
 * @returns {void}
 */
function dismissToast(toast) {
  try {
    const Toast = globalThis.bootstrap?.Toast;
    if (Toast) {
      Toast.getOrCreateInstance(toast).hide();
      return;
    }
  } catch {
    // Fall through to direct removal.
  }
  toast.remove();
}

/**
 * Renders an incident popup into the toast area and wires its
 * interactions: clicking the toast (or pressing Enter/Space on it)
 * starts the scoped Voice_Session and dismisses it (Req 5.7, 5.8); the
 * dismiss button and an auto-hide timer close it without starting a
 * session.
 * @param {IncidentNotification} payload - Incident notification to
 *   show.
 * @param {Element} [container] - Toast container; defaults to the
 *   page's `#toast-area` element.
 * @returns {Element | null} The attached toast element, or null when no
 *   toast container exists (e.g. under unit test import).
 */
export function showIncidentToast(payload, container) {
  const area =
    container ?? globalThis.document?.getElementById('toast-area') ?? null;
  if (!area) {
    return null;
  }
  const doc = area.ownerDocument;
  const toast = renderIncidentPopup(payload, doc);
  toast.setAttribute('tabindex', '0');

  /**
   * Shared activation handler for click and keyboard activation of the
   * toast: ignores the dismiss button, starts the scoped Voice_Session,
   * and dismisses the toast.
   * @param {Event} event - DOM click or keydown event.
   * @returns {void}
   */
  const activate = (event) => {
    if (event.target?.closest?.('[data-bs-dismiss]')) {
      return;
    }
    startIncidentSession(payload, doc);
    dismissToast(toast);
  };
  toast.addEventListener('click', activate);
  toast.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      activate(event);
    }
  });

  area.appendChild(toast);

  const Toast = globalThis.bootstrap?.Toast;
  if (Toast) {
    try {
      new Toast(toast, { autohide: true, delay: TOAST_AUTOHIDE_MS }).show();
    } catch {
      // Plugin failure never suppresses the already-attached popup.
    }
  } else {
    // Without the plugin the `show` class set by the renderer keeps the
    // toast visible; the dismiss button and auto-hide are wired here.
    toast.querySelector('[data-bs-dismiss]')?.addEventListener('click', () => {
      toast.remove();
    });
    globalThis.setTimeout(() => toast.remove(), TOAST_AUTOHIDE_MS);
  }
  return toast;
}

/**
 * Appends a persistent entry for the notification to the
 * `#notifications-list` feed, clearing the static placeholder before
 * the first entry and keeping the newest entry on top. Clicking an
 * entry starts the scoped Voice_Session (Req 5.7, 5.8).
 * @param {IncidentNotification} payload - Incident notification to
 *   append.
 * @param {Element} [container] - Feed element; defaults to the page's
 *   `#notifications-list` element.
 * @returns {Element | null} The attached entry element, or null when no
 *   feed container exists.
 */
export function appendNotificationEntry(payload, container) {
  const list =
    container ??
    globalThis.document?.getElementById('notifications-list') ??
    null;
  if (!list) {
    return null;
  }
  const doc = list.ownerDocument;
  const entry = renderNotificationEntry(payload, doc);
  entry.addEventListener('click', () => {
    startIncidentSession(payload, doc);
  });
  if (!list.querySelector('[data-notification-entry]')) {
    list.replaceChildren();
  }
  list.prepend(entry);
  return entry;
}

/**
 * Handles a `portal:incident` CustomEvent: skips duplicate
 * notification ids, renders the popup and the feed entry synchronously
 * (within the 2-second bound of Req 5.2), and only then attempts the
 * chime — wrapped so a blocked or failed chime never suppresses the
 * already-rendered popup (Req 5.3, 5.4).
 * @param {CustomEvent<IncidentNotification>} event - Incident event
 *   dispatched by the AppSync Events client.
 * @returns {void}
 */
export function handleIncidentEvent(event) {
  const payload = event?.detail;
  if (!payload || typeof payload !== 'object') {
    return;
  }
  const id = payload.notificationId;
  if (typeof id === 'string' && id !== '') {
    if (shownNotificationIds.has(id)) {
      return;
    }
    shownNotificationIds.add(id);
  }
  // Popup and feed first (Req 5.2); the chime is attempted last and
  // every failure mode is contained (Req 5.3, 5.4).
  showIncidentToast(payload);
  appendNotificationEntry(payload);
  try {
    playChime();
  } catch {
    // playChime already never throws; this guard is belt-and-braces so
    // no future change can let a chime failure suppress the popup.
  }
}

// Self-wire on import so main.js only needs a side-effect import;
// guarded for document-less environments (plain Node test runners).
if (typeof document !== 'undefined') {
  document.addEventListener('portal:incident', handleIncidentEvent);
}
