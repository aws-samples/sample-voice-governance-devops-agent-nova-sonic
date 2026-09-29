// Feature: nova-sonic-support-portal, Property 20: Capability gate blocks unsupported browsers

/**
 * Property 20 (design.md): for any subset of required browser capabilities
 * (getUserMedia, AudioWorklet, WebSocket) reported as unavailable, the
 * Frontend SHALL display the unsupported-browser error and refuse to start
 * a Voice_Session; with all capabilities present, the session start path
 * SHALL be allowed.
 *
 * Validates: Requirements 9.7
 *
 * The gate is exercised through `src/capability.js`: {@link checkCapabilities}
 * over injected environment objects covering every capability subset
 * (including environments with no `navigator` at all and with a bare
 * `navigator` carrying no capabilities), and {@link renderUnsupportedError}
 * rendering into a jsdom container. `supported === false` is what refuses
 * session start; push capabilities are detected separately and must never
 * influence the session gate.
 */

import { describe, expect, it } from 'vitest';
import fc from 'fast-check';

import {
  PUSH_CAPABILITIES,
  SESSION_CAPABILITIES,
  checkCapabilities,
  renderUnsupportedError,
} from '../../src/capability.js';

/**
 * One boolean per detectable capability, plus `bareNavigator` which forces
 * an empty `navigator` object even when no navigator-hosted capability is
 * present (otherwise `navigator` is omitted entirely, covering both shapes
 * of "navigator capabilities missing").
 * @type {import('fast-check').Arbitrary<Record<string, boolean>>}
 */
const capabilityFlagsArb = fc.record({
  getUserMedia: fc.boolean(),
  AudioWorklet: fc.boolean(),
  WebSocket: fc.boolean(),
  serviceWorker: fc.boolean(),
  PushManager: fc.boolean(),
  bareNavigator: fc.boolean(),
});

/**
 * Capability flags with at least one Voice_Session capability forced
 * absent, i.e. an unsupported browser.
 * @type {import('fast-check').Arbitrary<Record<string, boolean>>}
 */
const unsupportedFlagsArb = capabilityFlagsArb.chain((flags) =>
  fc
    .constantFrom(...SESSION_CAPABILITIES)
    .map((forcedMissing) => ({ ...flags, [forcedMissing]: false })),
);

/**
 * Minimal stand-in for a capability constructor (AudioWorklet, WebSocket,
 * PushManager): detection only checks that the global is defined.
 */
class CapabilityStub {}

/**
 * Stand-in for `navigator.mediaDevices.getUserMedia`: detection only checks
 * that it is a function.
 * @returns {Promise<undefined>} Resolved promise; never inspected.
 */
const getUserMediaStub = () => Promise.resolve(undefined);

/**
 * Builds an environment object shaped like the browser global scope from
 * capability flags, matching how `capability.js` probes each capability:
 * `navigator.mediaDevices.getUserMedia` as a function, `AudioWorklet` /
 * `WebSocket` / `PushManager` as defined globals, and `serviceWorker` as a
 * `navigator` property.
 * @param {Record<string, boolean>} flags - Which capabilities to include.
 * @returns {object} Environment object exposing exactly the flagged
 *   capabilities.
 */
function buildEnv(flags) {
  const env = {};
  if (flags.getUserMedia || flags.serviceWorker || flags.bareNavigator) {
    env.navigator = {};
    if (flags.getUserMedia) {
      env.navigator.mediaDevices = { getUserMedia: getUserMediaStub };
    }
    if (flags.serviceWorker) {
      env.navigator.serviceWorker = {};
    }
  }
  if (flags.AudioWorklet) {
    env.AudioWorklet = CapabilityStub;
  }
  if (flags.WebSocket) {
    env.WebSocket = CapabilityStub;
  }
  if (flags.PushManager) {
    env.PushManager = CapabilityStub;
  }
  return env;
}

/**
 * Recursively freezes an object graph so any attempted mutation inside the
 * code under test throws a TypeError (strict mode), proving the input
 * environment is never modified.
 * @param {*} value - Value to freeze; non-objects pass through untouched.
 * @returns {*} The same value, deep-frozen when it is an object.
 */
function deepFreeze(value) {
  if (value !== null && typeof value === 'object') {
    Object.values(value).forEach(deepFreeze);
    Object.freeze(value);
  }
  return value;
}

describe('Property 20: Capability gate blocks unsupported browsers', () => {
  it('supported is true iff every session capability is present, and missing lists exactly the absent ones in order', () => {
    fc.assert(
      fc.property(capabilityFlagsArb, (flags) => {
        const result = checkCapabilities(buildEnv(flags));
        const allSessionPresent = SESSION_CAPABILITIES.every(
          (capabilityId) => flags[capabilityId],
        );
        expect(result.supported).toBe(allSessionPresent);
        expect(result.missing).toEqual(
          SESSION_CAPABILITIES.filter((capabilityId) => !flags[capabilityId]),
        );
      }),
      { numRuns: 100 },
    );
  });

  it('push detection is analogous and push capabilities never affect the session gate', () => {
    fc.assert(
      fc.property(capabilityFlagsArb, (flags) => {
        const result = checkCapabilities(buildEnv(flags));
        expect(result.pushSupported).toBe(
          PUSH_CAPABILITIES.every((capabilityId) => flags[capabilityId]),
        );
        expect(result.pushMissing).toEqual(
          PUSH_CAPABILITIES.filter((capabilityId) => !flags[capabilityId]),
        );

        // Same session flags with both push flags flipped: the session
        // verdict must be identical.
        const flipped = checkCapabilities(
          buildEnv({
            ...flags,
            serviceWorker: !flags.serviceWorker,
            PushManager: !flags.PushManager,
          }),
        );
        expect(flipped.supported).toBe(result.supported);
        expect(flipped.missing).toEqual(result.missing);
      }),
      { numRuns: 100 },
    );
  });

  it('any unsupported subset renders exactly one unsupported-browser error, and re-rendering replaces it', () => {
    fc.assert(
      fc.property(
        unsupportedFlagsArb,
        unsupportedFlagsArb,
        (flags, laterFlags) => {
          const { supported, missing } = checkCapabilities(buildEnv(flags));
          expect(supported).toBe(false);
          expect(missing.length).toBeGreaterThan(0);

          const container = document.createElement('div');
          const alert = renderUnsupportedError(missing, container);
          const rendered = container.querySelectorAll(
            '[data-error="unsupported-browser"]',
          );
          expect(rendered).toHaveLength(1);
          expect(rendered[0]).toBe(alert);
          expect(alert.textContent).toContain('Unsupported browser');
          // Labels embed the capability identifier, so every missing
          // capability is named in the message.
          for (const capabilityId of missing) {
            expect(alert.textContent).toContain(capabilityId);
          }

          // Re-render with another unsupported subset: the previous alert
          // is replaced, never duplicated.
          const laterMissing = checkCapabilities(buildEnv(laterFlags)).missing;
          const laterAlert = renderUnsupportedError(laterMissing, container);
          const afterRerender = container.querySelectorAll(
            '[data-error="unsupported-browser"]',
          );
          expect(afterRerender).toHaveLength(1);
          expect(afterRerender[0]).toBe(laterAlert);
          expect(container.contains(alert)).toBe(false);
        },
      ),
      { numRuns: 100 },
    );
  });

  it('detection is deterministic and never mutates the environment', () => {
    fc.assert(
      fc.property(capabilityFlagsArb, (flags) => {
        // Deep-frozen env: any mutation inside checkCapabilities would
        // throw a TypeError under strict mode.
        const env = deepFreeze(buildEnv(flags));
        const first = checkCapabilities(env);
        const second = checkCapabilities(env);
        expect(second).toEqual(first);
      }),
      { numRuns: 100 },
    );
  });
});
