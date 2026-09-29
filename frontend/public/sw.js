/**
 * Service worker for the Nova Sonic Support Portal: web push display and
 * notification click-through (Req 6.3, 6.4).
 *
 * Classic service worker script — NOT an ES module (registered without
 * `{type: "module"}` by `src/push/push-manager.js`), so it uses no
 * imports; the shared message-type and query-parameter names are
 * mirrored from `src/push/push-manager.js` as literals below. Deployed
 * at the site root from `frontend/public/`, giving the worker root
 * scope over the whole Portal.
 *
 * Behavior:
 * - `push` (Req 6.3): while the browser is closed (or the page is not
 *   visible), an incoming Web Push message is shown as a system
 *   notification titled with the incident summary, carrying severity and
 *   timestamp in the body and the full payload in `data` for the click
 *   handler.
 * - `notificationclick` (Req 6.4): the notification is closed and the
 *   Portal is opened — an existing Portal window is focused and receives
 *   a `{type: "portal:incident-click", payload}` message; otherwise a
 *   new window is opened at `/?incident=<json>`. The page side
 *   (`src/push/push-manager.js`) turns either signal into a
 *   `portal:session-start` scoped to the incident's executionId once the
 *   engineer's authenticated session is verified (the existing route
 *   guard prompts for sign-in when none exists).
 */

'use strict';

/**
 * Title used when a push payload carries no summary.
 * @type {string}
 */
const DEFAULT_NOTIFICATION_TITLE = 'Incident';

/**
 * `type` field of the message posted to an existing Portal window on
 * notification click (mirrored in `src/push/push-manager.js`).
 * @type {string}
 */
const INCIDENT_CLICK_MESSAGE_TYPE = 'portal:incident-click';

/**
 * Query parameter carrying the incident payload when a new Portal window
 * is opened (mirrored in `src/push/push-manager.js`).
 * @type {string}
 */
const INCIDENT_QUERY_PARAM = 'incident';

/**
 * Parses the push message data into the Incident_Notification payload
 * published by the Notifier (`{notificationId, source, summary,
 * severity, timestamp, executionId?, detail}`). Malformed or missing
 * data degrades to an object with whatever is recoverable — a push
 * always yields a visible notification (Req 6.3).
 * @param {object | null} pushData - `event.data` of the push event
 *   (PushMessageData), or null when the push carried no payload.
 * @returns {object} The parsed payload, possibly empty.
 */
function parsePushPayload(pushData) {
  if (!pushData) {
    return {};
  }
  try {
    const parsed = pushData.json();
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    // Not JSON — fall through to plain text below.
  }
  try {
    const text = pushData.text();
    return text ? { summary: text } : {};
  } catch {
    return {};
  }
}

/**
 * Builds the notification body line from the payload's severity and
 * timestamp, skipping absent fields.
 * @param {object} payload - Parsed Incident_Notification payload.
 * @returns {string} e.g. `"HIGH — 2024-05-01T12:00:00Z"`, or an empty
 *   string when neither field is present.
 */
function buildNotificationBody(payload) {
  return [payload.severity, payload.timestamp]
    .filter((part) => typeof part === 'string' && part !== '')
    .join(' — ');
}

/**
 * `push` listener (Req 6.3): shows a system notification with the
 * incident summary as its title. The payload rides along in
 * `notification.data` for the click handler; `tag` de-duplicates
 * re-deliveries of the same notificationId.
 * @param {object} event - Push event carrying the Web Push message.
 * @returns {void}
 */
function handlePush(event) {
  const payload = parsePushPayload(event.data);
  const title =
    typeof payload.summary === 'string' && payload.summary !== ''
      ? payload.summary
      : DEFAULT_NOTIFICATION_TITLE;
  const options = {
    body: buildNotificationBody(payload),
    data: payload,
  };
  if (typeof payload.notificationId === 'string' && payload.notificationId !== '') {
    options.tag = payload.notificationId;
  }
  event.waitUntil(self.registration.showNotification(title, options));
}

/**
 * Opens the Portal for a clicked notification (Req 6.4): focuses an
 * existing Portal window and forwards the incident payload as a
 * `portal:incident-click` message, or — when no window is open — opens a
 * new one with the incident in the `?incident=` query parameter. Only
 * same-origin clients are returned by `matchAll`, so the first window
 * client is always a Portal window.
 * @param {object} payload - Incident_Notification payload from
 *   `notification.data`.
 * @returns {Promise<void>} Resolves when the Portal has been focused or
 *   opened.
 */
async function openPortalForIncident(payload) {
  const windowClients = await self.clients.matchAll({
    type: 'window',
    includeUncontrolled: true,
  });
  const portalClient = windowClients[0];
  if (portalClient) {
    try {
      await portalClient.focus();
    } catch {
      // Focus can be refused by the platform; the message still arrives.
    }
    portalClient.postMessage({
      type: INCIDENT_CLICK_MESSAGE_TYPE,
      payload,
    });
    return;
  }
  const incident = JSON.stringify({
    executionId: payload.executionId,
    summary: payload.summary,
    severity: payload.severity,
  });
  await self.clients.openWindow(
    `/?${INCIDENT_QUERY_PARAM}=${encodeURIComponent(incident)}`,
  );
}

/**
 * `notificationclick` listener (Req 6.4): closes the notification and
 * hands off to {@link openPortalForIncident}.
 * @param {object} event - Notification click event.
 * @returns {void}
 */
function handleNotificationClick(event) {
  event.notification.close();
  const payload = event.notification.data ?? {};
  event.waitUntil(openPortalForIncident(payload));
}

/**
 * `install` listener: activates an updated worker immediately instead of
 * waiting for every Portal tab to close.
 * @returns {void}
 */
function handleInstall() {
  self.skipWaiting();
}

/**
 * `activate` listener: takes control of already-open Portal windows so
 * the focused-window message path works right after first registration.
 * @param {object} event - Activate event.
 * @returns {void}
 */
function handleActivate(event) {
  event.waitUntil(self.clients.claim());
}

self.addEventListener('push', handlePush);
self.addEventListener('notificationclick', handleNotificationClick);
self.addEventListener('install', handleInstall);
self.addEventListener('activate', handleActivate);
