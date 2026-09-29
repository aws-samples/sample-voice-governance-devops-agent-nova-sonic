/**
 * Role-distinguished transcript rendering (Req 1.4).
 *
 * The voice WebSocket client (`src/ws/voice-client.js`) relays server
 * `transcript` frames as `portal:transcript` CustomEvents on `document`
 * with `detail: { role, text, timestamp }`. This module listens for those
 * events and appends a rendered entry to the `#transcript` log element
 * synchronously on receipt, so the 2-second display bound of Req 1.4 is
 * met trivially.
 *
 * Layout is a single chat-style thread in arrival order: engineer turns
 * align right, assistant turns align left (alignment comes from the
 * `data-role` attribute via the stylesheet), and both carry distinct
 * Bootstrap colours and the role labels "You" / "Assistant"
 * ({@link ROLE_PRESENTATION}). One thread keeps each answer adjacent to
 * its question — separate per-role panes made the reader correlate turns
 * across columns by timestamp.
 *
 * {@link renderTranscriptEntry} is a pure builder over an injected
 * document (no lookups, no globals) so property tests (task 9.9,
 * Property 18) can exercise it against jsdom documents directly. All
 * content is inserted via `textContent`, never markup injection.
 */

/**
 * A single transcript entry, as carried by the `portal:transcript` event
 * detail and by server `transcript` frames.
 * @typedef {object} TranscriptEntry
 * @property {string} role - Speaker role; `USER`/`user` for the engineer,
 *   `ASSISTANT`/`assistant` for Nova Sonic (matched case-insensitively).
 * @property {string} text - Utterance or response text.
 * @property {string} [timestamp] - ISO-8601 timestamp of the entry.
 */

/**
 * Visual presentation per normalized role: the on-screen label and the
 * Bootstrap classes that keep engineer utterances and assistant responses
 * visually distinct (Req 1.4). Exported so tests can assert the
 * distinction without duplicating class strings.
 * @type {Readonly<Record<'user' | 'assistant', Readonly<{label: string,
 *   entryClass: string, labelClass: string}>>>}
 */
export const ROLE_PRESENTATION = Object.freeze({
  user: Object.freeze({
    label: 'You',
    entryClass: 'border-end border-primary bg-primary-subtle',
    labelClass: 'text-primary-emphasis',
  }),
  assistant: Object.freeze({
    label: 'Assistant',
    entryClass: 'border-start border-success bg-success-subtle',
    labelClass: 'text-success-emphasis',
  }),
});

/**
 * Normalizes a transcript role to the presentation key. `USER` in any
 * casing maps to `user`; everything else (including `ASSISTANT` in any
 * casing) maps to `assistant`, so rendering is total over arbitrary role
 * strings.
 * @param {unknown} role - Role value from a transcript frame.
 * @returns {'user' | 'assistant'} The normalized presentation key.
 */
export function normalizeRole(role) {
  return String(role ?? '').toUpperCase() === 'USER' ? 'user' : 'assistant';
}

/**
 * Formats an ISO-8601 timestamp for display as a local time of day.
 * Total over arbitrary input: nullish values yield an empty string and
 * unparseable values are returned verbatim so information is never lost.
 * @param {unknown} timestamp - Timestamp value from a transcript frame.
 * @returns {string} The formatted time, the verbatim input when it cannot
 *   be parsed as a date, or an empty string for nullish input.
 */
function formatTimestamp(timestamp) {
  if (timestamp === undefined || timestamp === null || timestamp === '') {
    return '';
  }
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    return String(timestamp);
  }
  return date.toLocaleTimeString();
}

/**
 * Builds the DOM element for one transcript entry: a colored, bordered
 * block carrying the role label ("You" / "Assistant"), the display
 * timestamp, and the entry text. Pure with respect to its inputs — the
 * element is created on the injected document and not attached anywhere,
 * so tests can render entries without page markup.
 * @param {TranscriptEntry} entry - Transcript entry to render.
 * @param {Document} [doc] - Document used to create elements; defaults to
 *   the global document when one exists (tests pass a jsdom document).
 * @returns {Element} The detached transcript entry element, marked with
 *   `data-transcript-entry` and `data-role`.
 */
export function renderTranscriptEntry(entry, doc = globalThis.document) {
  const { role, text, timestamp } = entry ?? {};
  const roleKey = normalizeRole(role);
  const presentation = ROLE_PRESENTATION[roleKey];

  const element = doc.createElement('div');
  element.className =
    'transcript-entry mb-2 p-2 rounded border-4 ' + presentation.entryClass;
  element.dataset.transcriptEntry = '';
  element.dataset.role = roleKey;

  const header = doc.createElement('div');
  header.className =
    'd-flex justify-content-between align-items-baseline gap-2 small mb-1';

  const label = doc.createElement('span');
  label.className = `fw-bold ${presentation.labelClass}`;
  label.textContent = presentation.label;
  header.appendChild(label);

  const time = doc.createElement('time');
  time.className = 'text-body-secondary';
  if (typeof timestamp === 'string' && timestamp !== '') {
    time.setAttribute('datetime', timestamp);
  }
  time.textContent = formatTimestamp(timestamp);
  header.appendChild(time);

  element.appendChild(header);

  const body = doc.createElement('p');
  body.className = 'mb-0';
  body.textContent = String(text ?? '');
  element.appendChild(body);

  return element;
}

/**
 * Renders a transcript entry and appends it to the single chronological
 * thread, clearing the static placeholder before the first entry and
 * keeping the thread scrolled to the newest turn.
 *
 * Appending every role to one thread is what makes a reply sit directly
 * under the request it answers; role is conveyed by the entry's alignment
 * and colour, not by its container.
 * @param {TranscriptEntry} entry - Transcript entry to append.
 * @param {Element} [container] - Explicit log element; defaults to the
 *   page's `#transcript` thread.
 * @returns {Element | null} The appended entry element, or null when no
 *   transcript container exists (e.g. under unit test import).
 */
export function appendTranscript(entry, container) {
  const log =
    container ?? globalThis.document?.getElementById('transcript') ?? null;
  if (!log) {
    return null;
  }
  const element = renderTranscriptEntry(entry, log.ownerDocument);
  if (!log.querySelector('[data-transcript-entry]')) {
    log.replaceChildren();
  }
  log.appendChild(element);
  log.scrollTop = log.scrollHeight;
  return element;
}

/**
 * Handles a `portal:transcript` CustomEvent by appending its detail to
 * the transcript log synchronously, well within the 2-second display
 * bound of Req 1.4.
 * @param {CustomEvent<TranscriptEntry>} event - Transcript event
 *   dispatched by the voice WebSocket client.
 * @returns {void}
 */
function handleTranscriptEvent(event) {
  const detail = event?.detail;
  if (!detail) {
    return;
  }
  appendTranscript(detail);
}

// Self-wire on import so main.js only needs a side-effect import; guarded
// for document-less environments (plain Node test runners).
if (typeof document !== 'undefined') {
  document.addEventListener('portal:transcript', handleTranscriptEvent);
}
