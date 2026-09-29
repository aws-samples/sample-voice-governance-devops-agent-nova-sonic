// Feature: nova-sonic-support-portal, Property 18: Rendering is complete and role-distinguished

/**
 * Property 18 (design.md): for any transcript frame, the rendered
 * transcript entry SHALL contain the text and carry the visual marker for
 * its role (engineer vs. assistant); and for any Incident_Notification
 * payload, the rendered popup SHALL contain the summary, severity, and
 * timestamp.
 *
 * Validates: Requirements 1.4, 5.2
 *
 * The property is exercised through the pure DOM builders exported by
 * `src/ui/transcript.js` ({@link renderTranscriptEntry}) and
 * `src/ui/incident-popup.js` ({@link renderIncidentPopup}), which are
 * exactly what the event handlers attach to the page. Both builders run
 * against the jsdom `document` provided by the vitest environment.
 * Generated text spans full unicode graphemes and includes markup-shaped
 * strings, so the never-inject-markup contract (all content via
 * `textContent`) is exercised alongside completeness.
 */

import { describe, expect, it } from 'vitest';
import fc from 'fast-check';

import {
  ROLE_PRESENTATION,
  renderTranscriptEntry,
} from '../../src/ui/transcript.js';
import {
  DEFAULT_BADGE_VARIANT,
  SEVERITY_BADGE_VARIANTS,
  renderIncidentPopup,
  severityPresentation,
} from '../../src/ui/incident-popup.js';

/**
 * Arbitrary text spanning full unicode graphemes (emoji, combining marks,
 * non-Latin scripts) so rendering is checked well beyond ASCII.
 * @type {import('fast-check').Arbitrary<string>}
 */
const textArb = fc.string({ unit: 'grapheme', maxLength: 200 });

/**
 * Role values as produced by transcript frames — the documented casings —
 * plus arbitrary strings, since rendering is total over any role value.
 * @type {import('fast-check').Arbitrary<string>}
 */
const roleArb = fc.oneof(
  fc.constantFrom('USER', 'user', 'User', 'ASSISTANT', 'assistant'),
  fc.string(),
);

/**
 * Valid ISO-8601 timestamp strings.
 * @type {import('fast-check').Arbitrary<string>}
 */
const isoTimestampArb = fc
  .date({ noInvalidDate: true })
  .map((date) => date.toISOString());

/**
 * Transcript frame details as relayed by the voice WebSocket client:
 * role, text, and an optional timestamp that may be a valid ISO string,
 * an arbitrary (possibly unparseable) string, or absent.
 * @type {import('fast-check').Arbitrary<{role: string, text: string,
 *   timestamp: string | undefined}>}
 */
const transcriptEntryArb = fc.record({
  role: roleArb,
  text: textArb,
  timestamp: fc.oneof(isoTimestampArb, fc.string(), fc.constant(undefined)),
});

/**
 * Severity values: the documented set in assorted casings/padding, plus
 * arbitrary strings, since popup rendering is total over any severity.
 * @type {import('fast-check').Arbitrary<string>}
 */
const severityArb = fc.oneof(
  fc.constantFrom('critical', 'high', 'medium', 'low', 'CRITICAL', ' High '),
  fc.string(),
);

/**
 * Incident_Notification payloads per the design payload schema.
 * @type {import('fast-check').Arbitrary<object>}
 */
const incidentPayloadArb = fc.record({
  notificationId: fc.string(),
  source: fc.string(),
  summary: textArb,
  severity: severityArb,
  timestamp: fc.oneof(isoTimestampArb, fc.string()),
  executionId: fc.option(fc.string()),
});

describe('Property 18: Rendering is complete and role-distinguished', () => {
  describe('transcript entries', () => {
    it('rendered entry contains the frame text exactly', () => {
      fc.assert(
        fc.property(transcriptEntryArb, (entry) => {
          const element = renderTranscriptEntry(entry, document);
          expect(element.matches('[data-transcript-entry]')).toBe(true);
          expect(element.querySelector('p').textContent).toBe(entry.text);
        }),
        { numRuns: 100 },
      );
    });

    it('data-role marks USER (any casing) as user and everything else as assistant', () => {
      fc.assert(
        fc.property(transcriptEntryArb, (entry) => {
          const element = renderTranscriptEntry(entry, document);
          const expectedRole =
            String(entry.role).toUpperCase() === 'USER' ? 'user' : 'assistant';
          expect(element.dataset.role).toBe(expectedRole);
        }),
        { numRuns: 100 },
      );
    });

    it('engineer and assistant entries carry distinct visual markers and labels', () => {
      fc.assert(
        fc.property(
          textArb,
          fc.oneof(isoTimestampArb, fc.constant(undefined)),
          (text, timestamp) => {
            const userEl = renderTranscriptEntry(
              { role: 'USER', text, timestamp },
              document,
            );
            const assistantEl = renderTranscriptEntry(
              { role: 'ASSISTANT', text, timestamp },
              document,
            );

            for (const [element, key] of [
              [userEl, 'user'],
              [assistantEl, 'assistant'],
            ]) {
              for (const token of ROLE_PRESENTATION[key].entryClass.split(
                ' ',
              )) {
                expect(element.classList.contains(token)).toBe(true);
              }
              expect(element.querySelector('.fw-bold').textContent).toBe(
                ROLE_PRESENTATION[key].label,
              );
            }

            // The two presentations are actually distinguishable (Req 1.4).
            expect(userEl.className).not.toBe(assistantEl.className);
            expect(userEl.querySelector('.fw-bold').textContent).not.toBe(
              assistantEl.querySelector('.fw-bold').textContent,
            );
          },
        ),
        { numRuns: 100 },
      );
    });

    it('markup-shaped text stays text and never becomes elements', () => {
      fc.assert(
        fc.property(textArb, roleArb, (text, role) => {
          const hostile = `<script>${text}</script><img src=x onerror="x">`;
          const element = renderTranscriptEntry(
            { role, text: hostile },
            document,
          );
          expect(element.querySelector('script')).toBeNull();
          expect(element.querySelector('img')).toBeNull();
          expect(element.querySelector('p').textContent).toBe(hostile);
        }),
        { numRuns: 100 },
      );
    });
  });

  describe('incident popups', () => {
    it('rendered popup contains the summary exactly', () => {
      fc.assert(
        fc.property(incidentPayloadArb, (payload) => {
          const popup = renderIncidentPopup(payload, document);
          expect(popup.matches('[data-incident-popup]')).toBe(true);
          expect(popup.querySelector('[data-summary]').textContent).toBe(
            payload.summary,
          );
        }),
        { numRuns: 100 },
      );
    });

    it('rendered popup carries the normalized severity on badge and root', () => {
      fc.assert(
        fc.property(incidentPayloadArb, (payload) => {
          const popup = renderIncidentPopup(payload, document);
          const normalized = String(payload.severity).trim().toLowerCase();
          const expected = normalized === '' ? 'unknown' : normalized;
          const badge = popup.querySelector('[data-severity-badge]');
          expect(badge).not.toBeNull();
          expect(badge.textContent).toBe(expected);
          expect(severityPresentation(payload.severity).severity).toBe(
            expected,
          );
          expect(popup.dataset.severity).toBe(expected);
        }),
        { numRuns: 100 },
      );
    });

    it('badge variant follows the severity mapping with the default fallback', () => {
      fc.assert(
        fc.property(incidentPayloadArb, (payload) => {
          const popup = renderIncidentPopup(payload, document);
          const badge = popup.querySelector('[data-severity-badge]');
          const normalized = String(payload.severity).trim().toLowerCase();
          // Own-key oracle: prototype-member severities (e.g.
          // "constructor") map to the default variant, never to
          // inherited Object.prototype values.
          const variant = Object.hasOwn(SEVERITY_BADGE_VARIANTS, normalized)
            ? SEVERITY_BADGE_VARIANTS[normalized]
            : DEFAULT_BADGE_VARIANT;
          expect(badge.classList.contains(`text-bg-${variant}`)).toBe(true);
        }),
        { numRuns: 100 },
      );
    });

    it('rendered popup carries the timestamp without information loss', () => {
      fc.assert(
        fc.property(incidentPayloadArb, (payload) => {
          const popup = renderIncidentPopup(payload, document);
          const time = popup.querySelector('[data-timestamp]');
          expect(time).not.toBeNull();
          // Raw payload timestamp is always preserved verbatim.
          expect(time.dataset.timestamp).toBe(payload.timestamp);
          // Unparseable non-empty timestamps display verbatim (fallback).
          const parsed = new Date(payload.timestamp);
          if (payload.timestamp !== '' && Number.isNaN(parsed.getTime())) {
            expect(time.textContent).toBe(payload.timestamp);
          }
        }),
        { numRuns: 100 },
      );
    });

    it('markup-shaped summaries stay text and never become elements', () => {
      fc.assert(
        fc.property(incidentPayloadArb, (payload) => {
          const hostile = `<script>${payload.summary}</script><img src=x onerror="x">`;
          const popup = renderIncidentPopup(
            { ...payload, summary: hostile },
            document,
          );
          expect(popup.querySelector('script')).toBeNull();
          expect(popup.querySelector('img')).toBeNull();
          expect(popup.querySelector('[data-summary]').textContent).toBe(
            hostile,
          );
        }),
        { numRuns: 100 },
      );
    });
  });
});
