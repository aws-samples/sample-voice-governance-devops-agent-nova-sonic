/**
 * Unit tests for web push registration edge cases (task 9.12).
 *
 * Covers Req 6.6 — a denied push permission shows the push-disabled
 * indication and no subscription registration is attempted — and
 * Req 6.7 — a subscription persistence failure (POST
 * `/api/push-subscriptions`) shows an error message with a retry
 * option, and retrying registration works.
 *
 * The flow is exercised two ways: {@link registerPush} directly with
 * injected seams for the outcome contract, and {@link wirePushManager}
 * against a detached document for the button-driven UI transitions.
 * Detached documents keep the dispatched events away from the modules
 * self-wired to the global document.
 */

import { describe, expect, it, vi } from 'vitest';

import {
  PUSH_BUTTON_ID,
  SUBSCRIPTIONS_ENDPOINT,
  registerPush,
  wirePushManager,
} from '../../src/push/push-manager.js';

/**
 * A syntactically valid base64url VAPID public key stand-in (decodes
 * without error; content is never interpreted by the code under test).
 * @type {string}
 */
const TEST_VAPID_KEY = 'BPk9ZH8xkvRhFvEfBU7GK2mBNCVXbTAG_LKmQtYqOzu4'; // pragma: allowlist secret -- test-only VAPID key stand-in, not a real secret

/**
 * Builds the injectable seams for one registration run: a Notification
 * stub resolving to the given permission, a service worker container
 * whose registration subscribes successfully, a mutable fetch stub, and
 * an authenticated token accessor.
 * @param {string} permission - Permission the Notification stub resolves
 *   to (`granted`, `denied`, or `default`).
 * @returns {{overrides: object, register: import('vitest').Mock,
 *   subscribe: import('vitest').Mock, fetchMock: import('vitest').Mock}}
 *   The overrides object for {@link registerPush} /
 *   {@link wirePushManager} and the observable stubs.
 */
function buildSeams(permission) {
  const subscription = {
    /**
     * Serializes the fake subscription like a real PushSubscription.
     * @returns {object} The subscription JSON shape.
     */
    toJSON: () => ({
      endpoint: 'https://push.example/endpoint-1',
      keys: { p256dh: 'key', auth: 'auth' },
    }),
  };
  const subscribe = vi.fn(async () => subscription);
  const register = vi.fn(async () => ({ pushManager: { subscribe } }));
  const fetchMock = vi.fn(async () => ({ ok: true, status: 201 }));
  const overrides = {
    notification: { requestPermission: vi.fn(async () => permission) },
    serviceWorkerContainer: { register, addEventListener: vi.fn() },
    fetch: fetchMock,
    getAccessTokenFn: vi.fn(() => 'test-access-token'),
    isAuthenticatedFn: vi.fn(() => true),
    storage: {
      getItem: vi.fn(() => null),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    },
    locationRef: { search: '', pathname: '/' },
    historyRef: { replaceState: vi.fn() },
  };
  return { overrides, register, subscribe, fetchMock };
}

/**
 * Creates a detached document hosting the push button and the alert
 * area, wires the push manager to it with the given seams, and delivers
 * the runtime configuration via `portal:ready`.
 * @param {object} overrides - Seams from {@link buildSeams}.
 * @returns {{doc: Document, button: Element}} The isolated document and
 *   the push opt-in button.
 */
function createWiredHost(overrides) {
  const doc = document.implementation.createHTMLDocument('push-test');
  const button = doc.createElement('button');
  button.id = PUSH_BUTTON_ID;
  button.className = 'btn btn-outline-primary';
  button.textContent = 'Enable push';
  doc.body.appendChild(button);
  const alertArea = doc.createElement('div');
  alertArea.id = 'alert-area';
  doc.body.appendChild(alertArea);

  wirePushManager({ ...overrides, documentRef: doc });
  doc.dispatchEvent(
    new CustomEvent('portal:ready', {
      detail: { config: { vapidPublicKey: TEST_VAPID_KEY } },
    }),
  );
  return { doc, button };
}

describe('push permission denied (Req 6.6)', () => {
  it('registerPush reports denied and never attempts a registration', async () => {
    const { overrides, register, fetchMock } = buildSeams('denied');

    const result = await registerPush(
      { vapidPublicKey: TEST_VAPID_KEY },
      overrides,
    );

    expect(result.status).toBe('denied');
    expect(register).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('the button flow shows the push-disabled indication and keeps the button disabled', async () => {
    const { overrides, register } = buildSeams('denied');
    const { doc, button } = createWiredHost(overrides);

    button.click();

    await vi.waitFor(() => {
      expect(doc.querySelector('[data-push-indicator]')).not.toBeNull();
    });
    expect(doc.querySelector('[data-push-indicator]').textContent).toBe(
      'Push notifications are disabled (permission denied).',
    );
    expect(button.disabled).toBe(true);
    expect(register).not.toHaveBeenCalled();
  });
});

describe('subscription persistence failure and retry (Req 6.7)', () => {
  it('registerPush surfaces a failed POST /api/push-subscriptions as a retryable error', async () => {
    const { overrides, fetchMock } = buildSeams('granted');
    fetchMock.mockResolvedValueOnce({ ok: false, status: 500 });

    const result = await registerPush(
      { vapidPublicKey: TEST_VAPID_KEY },
      overrides,
    );

    expect(result.status).toBe('error');
    expect(result.error).toBeInstanceOf(Error);
    expect(result.error.message).toContain('500');
    expect(fetchMock).toHaveBeenCalledWith(
      SUBSCRIPTIONS_ENDPOINT,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          Authorization: 'Bearer test-access-token',
        }),
      }),
    );
  });

  it('registerPush surfaces a network failure of the persistence request as an error', async () => {
    const { overrides, fetchMock } = buildSeams('granted');
    fetchMock.mockRejectedValueOnce(new Error('network down'));

    const result = await registerPush(
      { vapidPublicKey: TEST_VAPID_KEY },
      overrides,
    );

    expect(result.status).toBe('error');
    expect(result.error).toBeInstanceOf(Error);
  });

  it('the button flow shows the retry error message, and retrying succeeds', async () => {
    const { overrides, fetchMock } = buildSeams('granted');
    fetchMock.mockResolvedValueOnce({ ok: false, status: 503 });
    const { doc, button } = createWiredHost(overrides);

    // First attempt: persistence fails, the error alert with the retry
    // hint appears and the button is re-enabled for another try.
    button.click();
    await vi.waitFor(() => {
      expect(
        doc.querySelector('[data-error="push-registration"]'),
      ).not.toBeNull();
    });
    const alert = doc.querySelector('[data-error="push-registration"]');
    expect(alert.textContent).toContain(
      'Push notification registration failed',
    );
    expect(alert.textContent).toContain('retry');
    expect(button.disabled).toBe(false);

    // Retry: the next persistence attempt succeeds, the alert is
    // cleared, and the button renders the enabled state.
    button.click();
    await vi.waitFor(() => {
      expect(button.textContent).toBe('Push enabled');
    });
    expect(doc.querySelector('[data-error="push-registration"]')).toBeNull();
    expect(button.disabled).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('a successful registration persists the subscription payload', async () => {
    const { overrides, fetchMock, subscribe } = buildSeams('granted');

    const result = await registerPush(
      { vapidPublicKey: TEST_VAPID_KEY },
      overrides,
    );

    expect(result.status).toBe('registered');
    expect(subscribe).toHaveBeenCalledWith(
      expect.objectContaining({ userVisibleOnly: true }),
    );
    const [, options] = fetchMock.mock.calls[0];
    expect(JSON.parse(options.body)).toEqual({
      subscription: {
        endpoint: 'https://push.example/endpoint-1',
        keys: { p256dh: 'key', auth: 'auth' },
      },
    });
  });
});
