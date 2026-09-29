/**
 * Cognito authentication and route guard for the Nova Sonic Support
 * Portal SPA.
 *
 * Implements the OAuth 2.0 authorization code flow with PKCE (S256)
 * against the Cognito Hosted UI, token storage and scheduled refresh, and
 * the route guard that denies unauthenticated access to every Portal
 * feature and redirects the engineer to the Cognito sign-in flow
 * (Req 7.1, 7.7).
 *
 * Integration seam: `src/main.js` imports this module for its side
 * effect — the module listens for the `portal:ready` event dispatched on
 * `document` after bootstrap and initializes itself from
 * `event.detail.config.cognito`. The Hosted UI base URL and SPA client id
 * come from the runtime `config.json`; no endpoint is hardcoded
 * (Req 14.3). {@link initAuth} is also exported directly so tests (or an
 * alternative bootstrapper) can drive initialization with injectable
 * fetch/storage/location/crypto/timer seams.
 *
 * Token storage choice: tokens are kept in `sessionStorage` rather than
 * `localStorage` deliberately — sessionStorage is scoped to the browser
 * tab and cleared when the tab closes, which limits token lifetime to the
 * tab session instead of persisting across browser restarts, and isolates
 * concurrent sign-ins in different tabs.
 *
 * Security notes:
 * - The ID token payload is decoded client-side for display only (the
 *   navbar username); no signature verification happens in the browser —
 *   the Voice_Service validates signatures server-side (Req 7.2).
 * - Sign-in requests carry a random `state` parameter that is compared on
 *   the Hosted UI callback, binding the callback to a sign-in this tab
 *   initiated (CSRF protection). A mismatch discards the callback.
 * - The PKCE verifier and state are one-shot values: stored immediately
 *   before the redirect and removed as soon as the callback is processed.
 */

/**
 * Cognito client configuration — the `cognito` object from `config.json`
 * (see `config.example.json` for the shape).
 * @typedef {object} CognitoClientConfig
 * @property {string} userPoolId - Cognito user pool id (carried in the
 *   config shape; not needed for the Hosted UI flows in this module).
 * @property {string} clientId - Public SPA app client id (PKCE, no secret).
 * @property {string} domain - Hosted UI base URL including scheme, e.g.
 *   `https://prefix.auth.region.amazoncognito.com`.
 */

/**
 * Tokens held for the signed-in engineer.
 * @typedef {object} TokenSet
 * @property {string} accessToken - OAuth 2.0 access token (JWT).
 * @property {string} idToken - OpenID Connect ID token (JWT).
 * @property {string | null} refreshToken - Refresh token when issued,
 *   otherwise null.
 * @property {number} expiresAt - Epoch milliseconds at which the access
 *   and ID tokens expire.
 */

/**
 * One-shot PKCE state persisted for the duration of a Hosted UI redirect.
 * @typedef {object} PkceState
 * @property {string} verifier - PKCE code verifier.
 * @property {string} state - Random CSRF `state` parameter sent to the
 *   Hosted UI.
 * @property {string} redirectUri - Redirect URI used in the authorize
 *   request; the token exchange must repeat it exactly.
 */

/**
 * OAuth callback parameters found in the page URL after the Hosted UI
 * redirects back.
 * @typedef {object} CallbackParams
 * @property {string | null} code - Authorization code, when present.
 * @property {string | null} state - Echoed CSRF state, when present.
 * @property {string | null} error - OAuth error identifier, when the
 *   Hosted UI reported a failure (e.g. `access_denied`).
 * @property {string | null} errorDescription - Human-readable error
 *   detail, when provided.
 */

/**
 * Injectable environment seams used by the stateful auth flows so tests
 * can substitute fakes; every property defaults to the browser global.
 * @typedef {object} AuthRuntime
 * @property {(url: string, options?: object) => Promise<Response>} fetch -
 *   Fetch implementation used for token endpoint requests.
 * @property {object} storage - `sessionStorage`-shaped store with
 *   `getItem`/`setItem`/`removeItem`.
 * @property {object} location - `window.location`-shaped object with
 *   `origin`, `pathname`, `search`, and `assign()`.
 * @property {object} history - `window.history`-shaped object with
 *   `replaceState()`.
 * @property {object | undefined} documentRef - Document used for the
 *   route guard and navbar updates; undefined outside a DOM.
 * @property {object} crypto - Web Crypto implementation providing
 *   `getRandomValues` and `subtle.digest`.
 * @property {() => number} now - Clock returning epoch milliseconds.
 * @property {(callback: Function, delayMs: number) => *} setTimer -
 *   Timer scheduler, `setTimeout`-shaped.
 * @property {(timerId: *) => void} clearTimer - Timer canceller,
 *   `clearTimeout`-shaped.
 */

/**
 * sessionStorage key under which the {@link TokenSet} is persisted.
 * @type {string}
 */
export const TOKEN_STORAGE_KEY = 'portal.auth.tokens';

/**
 * sessionStorage key under which the one-shot {@link PkceState} is
 * persisted across the Hosted UI redirect.
 * @type {string}
 */
export const PKCE_STORAGE_KEY = 'portal.auth.pkce';

/**
 * OAuth scopes requested from the Hosted UI.
 * @type {string}
 */
export const DEFAULT_OAUTH_SCOPES = 'openid email profile';

/**
 * Element ids of the Portal feature controls gated by the route guard
 * (contract documented in `public/index.html`).
 * @type {Readonly<string[]>}
 */
export const FEATURE_CONTROL_IDS = Object.freeze([
  'btn-session-start',
  'btn-session-stop',
  'btn-session-reconnect',
  'btn-push-enable',
  'text-input',
  'btn-text-send',
]);

/**
 * CSS selector matching any gated feature control.
 * @type {string}
 */
const FEATURE_CONTROL_SELECTOR = FEATURE_CONTROL_IDS.map(
  (id) => `#${id}`,
).join(', ');

/**
 * How long before token expiry the scheduled refresh runs.
 * @type {number}
 */
const REFRESH_MARGIN_MS = 60_000;

/**
 * Lower bound for the scheduled-refresh delay, preventing a zero-delay
 * hot loop when a token is already inside the refresh margin.
 * @type {number}
 */
const MIN_REFRESH_DELAY_MS = 5_000;

/**
 * ID/access token claims tried in order when picking a display username:
 * the engineer's email first, then human-readable name claims, then the
 * pool username claims, and the opaque Cognito id (`sub`) only as the
 * last resort — pools with email aliasing generate UUID-like usernames,
 * so `cognito:username` must never outrank `email` for display.
 * @type {Readonly<string[]>}
 */
const USERNAME_CLAIM_PRECEDENCE = Object.freeze([
  'email',
  'name',
  'preferred_username',
  'cognito:username',
  'username',
  'sub',
]);

/** @type {CognitoClientConfig | null} */
let cognitoConfig = null;

/** @type {AuthRuntime | null} */
let runtime = null;

/** @type {TokenSet | null} */
let tokens = null;

/** @type {*} */
let refreshTimerId = null;

/** @type {string[]} */
let guardDisabledIds = [];

/** @type {object | null} */
let clickGuardTarget = null;

/* -------------------------------------------------------------------- */
/* Pure helpers (exported for direct testing)                            */
/* -------------------------------------------------------------------- */

/**
 * Encodes bytes as a base64url string without padding (RFC 4648 §5), the
 * encoding PKCE and JWTs use.
 * @param {Uint8Array} bytes - Bytes to encode.
 * @returns {string} base64url-encoded text (characters `A-Za-z0-9-_`).
 */
export function base64UrlEncode(bytes) {
  let binary = '';
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/u, '');
}

/**
 * Decodes a base64url string into UTF-8 text (used for JWT payloads).
 * @param {string} text - base64url-encoded input, with or without padding.
 * @returns {string} The decoded UTF-8 string.
 * @throws {Error} When the input is not valid base64url.
 */
export function base64UrlDecode(text) {
  const base64 = text.replace(/-/g, '+').replace(/_/g, '/');
  const padded = base64 + '='.repeat((4 - (base64.length % 4)) % 4);
  const binary = atob(padded);
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

/**
 * Generates a cryptographically random base64url string, used for the
 * PKCE code verifier and the CSRF `state` parameter.
 * @param {number} byteLength - Number of random bytes to draw; 32 bytes
 *   yield a 43-character string, the RFC 7636 minimum verifier length.
 * @param {object} [cryptoObj] - Web Crypto implementation providing
 *   `getRandomValues`. Defaults to `globalThis.crypto`.
 * @returns {string} Random base64url text.
 */
export function generateRandomBase64Url(byteLength, cryptoObj = globalThis.crypto) {
  const bytes = new Uint8Array(byteLength);
  cryptoObj.getRandomValues(bytes);
  return base64UrlEncode(bytes);
}

/**
 * Computes the PKCE S256 code challenge for a verifier:
 * `base64url(SHA-256(ascii(verifier)))` per RFC 7636 §4.2.
 * @param {string} codeVerifier - PKCE code verifier.
 * @param {object} [cryptoObj] - Web Crypto implementation providing
 *   `subtle.digest`. Defaults to `globalThis.crypto`.
 * @returns {Promise<string>} The base64url-encoded S256 challenge.
 * @throws {Error} Rejects when SubtleCrypto is unavailable (e.g. an
 *   insecure context) or digesting fails.
 */
export async function computeCodeChallenge(codeVerifier, cryptoObj = globalThis.crypto) {
  const data = new TextEncoder().encode(codeVerifier);
  const digest = await cryptoObj.subtle.digest('SHA-256', data);
  return base64UrlEncode(new Uint8Array(digest));
}

/**
 * Decodes a JWT payload segment into its claims object. Display use only:
 * the signature is deliberately NOT verified client-side — server-side
 * validation is the authority (Req 7.2).
 * @param {string} jwt - Compact JWT (`header.payload.signature`).
 * @returns {object} The decoded payload claims.
 * @throws {Error} When the input is not a three-segment JWT or the
 *   payload is not base64url-encoded JSON.
 */
export function decodeJwtPayload(jwt) {
  const segments = typeof jwt === 'string' ? jwt.split('.') : [];
  if (segments.length !== 3) {
    throw new Error('malformed JWT: expected three dot-separated segments');
  }
  try {
    return JSON.parse(base64UrlDecode(segments[1]));
  } catch (cause) {
    throw new Error('malformed JWT: payload is not base64url JSON', {
      cause,
    });
  }
}

/**
 * Picks a display username from decoded token claims, trying `email`,
 * `name`, `preferred_username`, `cognito:username`, `username`, then
 * `sub` (the opaque Cognito id, shown only when nothing better exists).
 * @param {object} claims - Decoded JWT payload claims.
 * @returns {string | null} The first present non-empty string claim, or
 *   null when none is usable.
 */
export function extractUsername(claims) {
  for (const key of USERNAME_CLAIM_PRECEDENCE) {
    const value = claims?.[key];
    if (typeof value === 'string' && value !== '') {
      return value;
    }
  }
  return null;
}

/**
 * Normalizes the configured Hosted UI base URL by stripping any trailing
 * slashes, so endpoint paths can be appended safely.
 * @param {string} domain - Hosted UI base URL from `config.json`.
 * @returns {string} The base URL without trailing slashes.
 */
export function normalizeHostedUiBase(domain) {
  return domain.replace(/\/+$/u, '');
}

/**
 * Builds the Hosted UI `/oauth2/authorize` URL for the authorization code
 * + PKCE (S256) sign-in redirect.
 * @param {CognitoClientConfig} cognito - Cognito client configuration.
 * @param {object} options - Request parameters.
 * @param {string} options.redirectUri - Callback URI registered on the
 *   app client; the token exchange must repeat it exactly.
 * @param {string} options.state - Random CSRF state parameter.
 * @param {string} options.codeChallenge - PKCE S256 code challenge.
 * @param {string} [options.scope] - Space-separated OAuth scopes.
 *   Defaults to {@link DEFAULT_OAUTH_SCOPES}.
 * @returns {string} The absolute authorize URL.
 * @throws {TypeError} When `cognito.domain` is not an absolute URL.
 */
export function buildAuthorizeUrl(cognito, { redirectUri, state, codeChallenge, scope }) {
  const url = new URL(`${normalizeHostedUiBase(cognito.domain)}/oauth2/authorize`);
  url.searchParams.set('response_type', 'code');
  url.searchParams.set('client_id', cognito.clientId);
  url.searchParams.set('redirect_uri', redirectUri);
  url.searchParams.set('scope', scope ?? DEFAULT_OAUTH_SCOPES);
  url.searchParams.set('state', state);
  url.searchParams.set('code_challenge_method', 'S256');
  url.searchParams.set('code_challenge', codeChallenge);
  return url.toString();
}

/**
 * Builds the Hosted UI `/oauth2/token` endpoint URL.
 * @param {CognitoClientConfig} cognito - Cognito client configuration.
 * @returns {string} The absolute token endpoint URL.
 */
export function buildTokenEndpoint(cognito) {
  return `${normalizeHostedUiBase(cognito.domain)}/oauth2/token`;
}

/**
 * Builds the Hosted UI `/logout` URL that clears the Cognito session and
 * returns the browser to the Portal.
 * @param {CognitoClientConfig} cognito - Cognito client configuration.
 * @param {string} logoutUri - Sign-out redirect URI registered on the app
 *   client.
 * @returns {string} The absolute logout URL.
 * @throws {TypeError} When `cognito.domain` is not an absolute URL.
 */
export function buildLogoutUrl(cognito, logoutUri) {
  const url = new URL(`${normalizeHostedUiBase(cognito.domain)}/logout`);
  url.searchParams.set('client_id', cognito.clientId);
  url.searchParams.set('logout_uri', logoutUri);
  return url.toString();
}

/**
 * Computes the epoch-millisecond expiry instant for a token issued now.
 * @param {number} nowMs - Current time in epoch milliseconds.
 * @param {number} expiresInSeconds - Token lifetime as reported by the
 *   token endpoint's `expires_in`.
 * @returns {number} Epoch milliseconds at which the token expires.
 */
export function computeExpiresAt(nowMs, expiresInSeconds) {
  return nowMs + expiresInSeconds * 1000;
}

/**
 * Computes how long to wait before running the scheduled token refresh:
 * the refresh fires {@link REFRESH_MARGIN_MS} before expiry, but never
 * sooner than {@link MIN_REFRESH_DELAY_MS} from now.
 * @param {number} expiresAtMs - Token expiry in epoch milliseconds.
 * @param {number} nowMs - Current time in epoch milliseconds.
 * @returns {number} Delay in milliseconds until the refresh should run.
 */
export function computeRefreshDelayMs(expiresAtMs, nowMs) {
  return Math.max(MIN_REFRESH_DELAY_MS, expiresAtMs - nowMs - REFRESH_MARGIN_MS);
}

/**
 * Tests whether a stored token set is present, well-formed, and unexpired.
 * @param {TokenSet | null | undefined} tokenSet - Candidate token set.
 * @param {number} nowMs - Current time in epoch milliseconds.
 * @returns {boolean} True when the token set can authenticate requests.
 */
export function isTokenSetValid(tokenSet, nowMs) {
  return Boolean(
    tokenSet &&
      typeof tokenSet.accessToken === 'string' &&
      tokenSet.accessToken !== '' &&
      typeof tokenSet.idToken === 'string' &&
      tokenSet.idToken !== '' &&
      typeof tokenSet.expiresAt === 'number' &&
      tokenSet.expiresAt > nowMs,
  );
}

/**
 * Extracts OAuth callback parameters from a location's query string.
 * @param {object} loc - `window.location`-shaped object with `search`.
 * @returns {CallbackParams | null} The callback parameters, or null when
 *   the URL carries neither a `code` nor an `error` (not a callback).
 */
export function readCallbackParams(loc) {
  const params = new URLSearchParams(loc.search ?? '');
  const code = params.get('code');
  const error = params.get('error');
  if (code === null && error === null) {
    return null;
  }
  return {
    code,
    state: params.get('state'),
    error,
    errorDescription: params.get('error_description'),
  };
}

/* -------------------------------------------------------------------- */
/* Environment defaults and storage helpers                              */
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
 * Default clock seam returning the current epoch milliseconds.
 * @returns {number} Current time in epoch milliseconds.
 */
function defaultNow() {
  return Date.now();
}

/**
 * Builds the default runtime bound to the browser globals; every seam is
 * individually overridable through {@link initAuth}'s second argument.
 * @returns {AuthRuntime} Runtime seams backed by the browser environment.
 */
function createDefaultRuntime() {
  return {
    fetch: defaultFetch,
    storage: globalThis.sessionStorage,
    location: globalThis.location,
    history: globalThis.history,
    documentRef: globalThis.document,
    crypto: globalThis.crypto,
    now: defaultNow,
    setTimer: defaultSetTimer,
    clearTimer: defaultClearTimer,
  };
}

/**
 * Reads and parses a JSON value from storage, tolerating missing keys,
 * storage access errors, and corrupted content.
 * @param {object} storage - `sessionStorage`-shaped store.
 * @param {string} key - Storage key to read.
 * @returns {object | null} The parsed value, or null when absent or
 *   unreadable.
 */
function readStoredJson(storage, key) {
  try {
    const raw = storage.getItem(key);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

/**
 * Serializes and writes a JSON value to storage.
 * @param {object} storage - `sessionStorage`-shaped store.
 * @param {string} key - Storage key to write.
 * @param {object} value - JSON-serializable value.
 * @returns {boolean} True when the write succeeded, false when storage is
 *   unavailable (e.g. blocked by a privacy mode).
 */
function writeStoredJson(storage, key, value) {
  try {
    storage.setItem(key, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}

/**
 * Removes a key from storage, tolerating storage access errors.
 * @param {object} storage - `sessionStorage`-shaped store.
 * @param {string} key - Storage key to remove.
 * @returns {void}
 */
function removeStored(storage, key) {
  try {
    storage.removeItem(key);
  } catch {
    // Storage unavailable — nothing to remove.
  }
}

/* -------------------------------------------------------------------- */
/* UI: navbar auth state, error alerts, and the route guard              */
/* -------------------------------------------------------------------- */

/**
 * Updates the `#user-info` navbar element with the signed-in username
 * (from the ID token claims, payload decode only) or "Not signed in".
 * @returns {void}
 */
function updateUserInfo() {
  const element = runtime?.documentRef?.getElementById('user-info');
  if (!element) {
    return;
  }
  if (!isAuthenticated()) {
    element.textContent = 'Not signed in';
    return;
  }
  let username = null;
  try {
    username = extractUsername(decodeJwtPayload(tokens.idToken));
  } catch {
    username = null;
  }
  element.textContent = username ? `Signed in as ${username}` : 'Signed in';
}

/**
 * Shows an authentication error in the page-level alert area, replacing
 * any previous auth alert. Content is inserted via `textContent`, never
 * markup injection.
 * @param {string} message - Plain-text message to display.
 * @returns {void}
 */
function renderAuthError(message) {
  const doc = runtime?.documentRef;
  const alertArea = doc?.getElementById('alert-area');
  if (!alertArea) {
    return;
  }
  alertArea.querySelector('[data-error="auth"]')?.remove();
  const alert = doc.createElement('div');
  alert.className = 'alert alert-danger';
  alert.setAttribute('role', 'alert');
  alert.dataset.error = 'auth';
  alert.textContent = message;
  alertArea.appendChild(alert);
}

/**
 * Route guard "deny" half: disables every currently enabled feature
 * control and remembers which ones this guard disabled, so
 * {@link allowFeatureAccess} re-enables exactly those (Req 7.1, 7.7).
 * @returns {void}
 */
function denyFeatureAccess() {
  const doc = runtime?.documentRef;
  if (!doc) {
    return;
  }
  for (const id of FEATURE_CONTROL_IDS) {
    const control = doc.getElementById(id);
    if (control && !control.disabled) {
      control.disabled = true;
      guardDisabledIds.push(id);
    }
  }
}

/**
 * Route guard "allow" half: re-enables the feature controls that
 * {@link denyFeatureAccess} disabled, leaving controls that other modules
 * keep disabled (e.g. the stop button outside a session) untouched.
 * @returns {void}
 */
function allowFeatureAccess() {
  const doc = runtime?.documentRef;
  if (doc) {
    for (const id of guardDisabledIds) {
      const control = doc.getElementById(id);
      if (control) {
        control.disabled = false;
      }
    }
  }
  guardDisabledIds = [];
}

/**
 * Capture-phase click handler backing the route guard: when the engineer
 * is not authenticated, any interaction with a feature control is blocked
 * before it reaches the feature module and the browser is redirected to
 * the Cognito sign-in flow (Req 7.7).
 * @param {object} event - DOM click event.
 * @returns {void}
 */
function handleGuardedClick(event) {
  if (isAuthenticated()) {
    return;
  }
  const target = event.target;
  if (typeof target?.closest !== 'function' || !target.closest(FEATURE_CONTROL_SELECTOR)) {
    return;
  }
  event.preventDefault();
  event.stopPropagation();
  void beginSignIn();
}

/**
 * Installs the capture-phase click guard on the current document once.
 * @returns {void}
 */
function installClickGuard() {
  const doc = runtime?.documentRef;
  if (!doc || clickGuardTarget === doc) {
    return;
  }
  doc.addEventListener('click', handleGuardedClick, true);
  clickGuardTarget = doc;
}

/* -------------------------------------------------------------------- */
/* Token endpoint operations                                             */
/* -------------------------------------------------------------------- */

/**
 * POSTs a form-encoded request to the Hosted UI token endpoint and parses
 * the JSON response.
 * @param {Record<string, string>} bodyParams - Form fields to send.
 * @returns {Promise<object>} The parsed token endpoint response.
 * @throws {Error} When the request fails, the endpoint responds with a
 *   non-2xx status, or the body is not valid JSON.
 */
async function requestTokens(bodyParams) {
  const response = await runtime.fetch(buildTokenEndpoint(cognitoConfig), {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams(bodyParams).toString(),
  });
  if (!response.ok) {
    let detail = '';
    try {
      detail = (await response.text()).slice(0, 300);
    } catch {
      detail = '';
    }
    throw new Error(
      `token endpoint responded HTTP ${response.status}${detail ? `: ${detail}` : ''}`,
    );
  }
  try {
    return await response.json();
  } catch (cause) {
    throw new Error('token endpoint returned invalid JSON', { cause });
  }
}

/**
 * Converts a token endpoint response into a {@link TokenSet}, stamping
 * the expiry instant from `expires_in`.
 * @param {object} data - Parsed token endpoint response.
 * @param {number} nowMs - Current time in epoch milliseconds.
 * @param {string | null} previousRefreshToken - Refresh token to carry
 *   over when the response omits one (the refresh grant does not rotate
 *   it).
 * @returns {TokenSet} The normalized token set.
 * @throws {Error} When the response is missing `access_token`,
 *   `id_token`, or a positive numeric `expires_in`.
 */
function toTokenSet(data, nowMs, previousRefreshToken) {
  const expiresIn = Number(data?.expires_in);
  if (
    typeof data?.access_token !== 'string' ||
    data.access_token === '' ||
    typeof data.id_token !== 'string' ||
    data.id_token === '' ||
    !Number.isFinite(expiresIn) ||
    expiresIn <= 0
  ) {
    throw new Error('token endpoint returned an unexpected response shape');
  }
  return {
    accessToken: data.access_token,
    idToken: data.id_token,
    refreshToken:
      typeof data.refresh_token === 'string' && data.refresh_token !== ''
        ? data.refresh_token
        : previousRefreshToken,
    expiresAt: computeExpiresAt(nowMs, expiresIn),
  };
}

/**
 * Exchanges an authorization code for tokens using the PKCE verifier
 * stored when the sign-in redirect began (authorization_code grant).
 * @param {string} code - Authorization code from the Hosted UI callback.
 * @param {PkceState} pkce - Stored PKCE verifier and redirect URI.
 * @returns {Promise<TokenSet>} The issued token set.
 * @throws {Error} When the token endpoint rejects the exchange or returns
 *   an unexpected shape.
 */
async function exchangeCodeForTokens(code, pkce) {
  const data = await requestTokens({
    grant_type: 'authorization_code',
    client_id: cognitoConfig.clientId,
    redirect_uri: pkce.redirectUri,
    code,
    code_verifier: pkce.verifier,
  });
  return toTokenSet(data, runtime.now(), null);
}

/**
 * Obtains fresh access and ID tokens with the refresh_token grant.
 * @returns {Promise<TokenSet>} The refreshed token set (refresh token
 *   carried over when Cognito does not rotate it).
 * @throws {Error} When no refresh token is held or the token endpoint
 *   rejects the refresh.
 */
async function refreshTokens() {
  const refreshToken = tokens?.refreshToken;
  if (!refreshToken) {
    throw new Error('no refresh token available');
  }
  const data = await requestTokens({
    grant_type: 'refresh_token',
    client_id: cognitoConfig.clientId,
    refresh_token: refreshToken,
  });
  return toTokenSet(data, runtime.now(), refreshToken);
}

/**
 * Persists the current in-memory token set to sessionStorage.
 * @returns {void}
 */
function persistTokens() {
  writeStoredJson(runtime.storage, TOKEN_STORAGE_KEY, tokens);
}

/**
 * Clears all authentication state: in-memory tokens, persisted tokens,
 * pending PKCE state, and any scheduled refresh.
 * @returns {void}
 */
function clearAuthState() {
  tokens = null;
  cancelScheduledRefresh();
  if (runtime) {
    removeStored(runtime.storage, TOKEN_STORAGE_KEY);
    removeStored(runtime.storage, PKCE_STORAGE_KEY);
  }
}

/**
 * Cancels the scheduled token refresh, when one is pending.
 * @returns {void}
 */
function cancelScheduledRefresh() {
  if (refreshTimerId !== null && runtime) {
    runtime.clearTimer(refreshTimerId);
    refreshTimerId = null;
  }
}

/**
 * Schedules the next token refresh {@link REFRESH_MARGIN_MS} before the
 * current tokens expire (delay derived from the token `expires_in`).
 * @returns {void}
 */
function scheduleRefresh() {
  cancelScheduledRefresh();
  if (!tokens) {
    return;
  }
  const delay = computeRefreshDelayMs(tokens.expiresAt, runtime.now());
  refreshTimerId = runtime.setTimer(runScheduledRefresh, delay);
}

/**
 * Timer body for the scheduled refresh: refreshes tokens and re-schedules
 * on success; on failure clears authentication state and re-enters the
 * sign-in flow, so the Portal never keeps operating with expired
 * credentials (Req 7.1, 7.7).
 * @returns {Promise<void>} Resolves when the refresh attempt completed.
 */
async function runScheduledRefresh() {
  refreshTimerId = null;
  try {
    tokens = await refreshTokens();
    persistTokens();
    updateUserInfo();
    scheduleRefresh();
  } catch {
    clearAuthState();
    denyFeatureAccess();
    updateUserInfo();
    await beginSignIn();
  }
}

/* -------------------------------------------------------------------- */
/* Public authentication API                                             */
/* -------------------------------------------------------------------- */

/**
 * Reports whether the engineer currently holds valid (unexpired) tokens.
 * @returns {boolean} True when authenticated; false when tokens are
 *   absent, expired, or {@link initAuth} has not run.
 */
export function isAuthenticated() {
  return runtime !== null && isTokenSetValid(tokens, runtime.now());
}

/**
 * Returns the current OAuth access token for API calls (e.g. the voice
 * WebSocket bearer subprotocol).
 * @returns {string | null} A valid access token, or null when the
 *   engineer is not authenticated.
 */
export function getAccessToken() {
  return isAuthenticated() ? tokens.accessToken : null;
}

/**
 * Returns an access token with a full lifetime ahead of it, refreshing
 * first whenever a refresh token is held.
 *
 * A voice WebSocket binds its credential once, at the handshake, and the
 * protocol has no frame for presenting a new one — so the session's
 * maximum length is whatever is left on the token it opened with. Reading
 * {@link getAccessToken} was therefore enough to start a session on a
 * nearly-expired token: one opened 54 minutes into a 60-minute token was
 * killed by the server's expiry watchdog after 5 m 44 s. Refreshing here
 * makes every new session (Start, and Reconnect) begin with the token's
 * full validity window.
 *
 * A failed refresh is not fatal and deliberately does not redirect: the
 * currently held token may still be perfectly usable, so this falls back
 * to {@link getAccessToken}. Only when that is also unusable does it
 * return null, leaving the sign-in decision to the caller and the route
 * guard.
 * @returns {Promise<string | null>} A valid access token — freshly
 *   refreshed when possible — or null when the engineer is not
 *   authenticated.
 */
export async function getFreshAccessToken() {
  if (!runtime) {
    return null;
  }
  if (tokens?.refreshToken) {
    try {
      tokens = await refreshTokens();
      persistTokens();
      updateUserInfo();
      // Re-arm the background timer against the NEW expiry; otherwise the
      // pending one fires early and refreshes again for nothing.
      scheduleRefresh();
      return tokens.accessToken;
    } catch {
      // Refresh rejected (revoked or expired refresh token, token
      // endpoint down). Fall through: the held token may still be valid.
    }
  }
  return getAccessToken();
}

/**
 * Returns the current OpenID Connect ID token.
 * @returns {string | null} A valid ID token, or null when the engineer is
 *   not authenticated.
 */
export function getIdToken() {
  return isAuthenticated() ? tokens.idToken : null;
}

/**
 * Returns the page URI (origin + path, no query or fragment) used as the
 * OAuth redirect and sign-out target; it must be registered on the
 * Cognito app client (provisioned by Terraform).
 * @returns {string} The current page URI.
 */
function currentPageUri() {
  return `${runtime.location.origin}${runtime.location.pathname}`;
}

/**
 * Starts the Cognito Hosted UI sign-in: generates the PKCE verifier and
 * CSRF state, persists them for the callback, and redirects the browser
 * to `/oauth2/authorize` (S256 challenge computed via `crypto.subtle`).
 * When sessionStorage is unavailable the redirect is not performed —
 * the callback could never be validated — and an error is shown instead.
 * @returns {Promise<void>} Resolves when the redirect has been issued (or
 *   refused because storage is unavailable).
 * @throws {Error} When called before {@link initAuth} has configured the
 *   module.
 */
export async function beginSignIn() {
  if (!runtime || !cognitoConfig) {
    throw new Error('initAuth must complete before beginSignIn');
  }
  const verifier = generateRandomBase64Url(32, runtime.crypto);
  const state = generateRandomBase64Url(16, runtime.crypto);
  const redirectUri = currentPageUri();
  const persisted = writeStoredJson(runtime.storage, PKCE_STORAGE_KEY, {
    verifier,
    state,
    redirectUri,
  });
  if (!persisted) {
    renderAuthError(
      'Sign-in could not start because browser storage is unavailable. ' +
        'Please allow site data for this page and reload.',
    );
    return;
  }
  const codeChallenge = await computeCodeChallenge(verifier, runtime.crypto);
  runtime.location.assign(
    buildAuthorizeUrl(cognitoConfig, { redirectUri, state, codeChallenge }),
  );
}

/**
 * Signs the engineer out: clears tokens and PKCE state, disables feature
 * access, and redirects to the Hosted UI `/logout` endpoint so the
 * Cognito session cookie is cleared too.
 * @returns {void}
 * @throws {Error} When called before {@link initAuth} has configured the
 *   module.
 */
export function signOut() {
  if (!runtime || !cognitoConfig) {
    throw new Error('initAuth must complete before signOut');
  }
  const logoutUri = currentPageUri();
  clearAuthState();
  denyFeatureAccess();
  updateUserInfo();
  runtime.location.assign(buildLogoutUrl(cognitoConfig, logoutUri));
}

/**
 * Transition into the authenticated state: re-enable the guarded feature
 * controls, show the username, and schedule the token refresh.
 * @returns {void}
 */
function onAuthenticated() {
  allowFeatureAccess();
  updateUserInfo();
  scheduleRefresh();
}

/**
 * Removes the OAuth callback parameters from the address bar so the
 * authorization code is not kept in history or accidentally re-used.
 * @returns {void}
 */
function cleanCallbackUrl() {
  runtime.history.replaceState(null, '', runtime.location.pathname);
}

/**
 * Initializes authentication after bootstrap (Req 7.1): installs the
 * route guard (deny-by-default until a valid token is confirmed), then
 * establishes the session by, in order —
 * 1. processing a Hosted UI callback (`?code=`): the CSRF state is
 *    checked and the code exchanged for tokens with the stored PKCE
 *    verifier, then the URL is cleaned;
 * 2. restoring unexpired tokens persisted in sessionStorage;
 * 3. refreshing with a persisted refresh token when the stored tokens
 *    have expired (e.g. the tab slept past expiry);
 * 4. otherwise redirecting to the Cognito sign-in flow (Req 7.7).
 *
 * Callback failures (state mismatch, exchange rejection, Hosted UI
 * `?error=`) show an alert and leave every feature denied; interacting
 * with a feature then re-triggers sign-in via the click guard, so a
 * failing identity provider cannot cause a redirect loop.
 * @param {object} config - Portal runtime configuration; only the
 *   `cognito` section is read.
 * @param {Partial<AuthRuntime>} [overrides] - Test seams; each property
 *   replaces the corresponding browser default.
 * @returns {Promise<void>} Resolves when the authentication state has
 *   been established (or a sign-in redirect has been issued).
 * @throws {Error} When `config.cognito` lacks `clientId` or `domain`.
 */
export async function initAuth(config, overrides = {}) {
  const cognito = config?.cognito;
  if (!cognito || typeof cognito.clientId !== 'string' || cognito.clientId === '' ||
      typeof cognito.domain !== 'string' || cognito.domain === '') {
    throw new Error(
      'initAuth requires config.cognito with clientId and domain (from config.json)',
    );
  }
  runtime = { ...createDefaultRuntime(), ...overrides };
  cognitoConfig = cognito;
  cancelScheduledRefresh();
  guardDisabledIds = [];
  installClickGuard();
  denyFeatureAccess();

  const callback = readCallbackParams(runtime.location);
  if (callback) {
    cleanCallbackUrl();
    const pkce = readStoredJson(runtime.storage, PKCE_STORAGE_KEY);
    removeStored(runtime.storage, PKCE_STORAGE_KEY);
    if (callback.error) {
      renderAuthError(
        `Sign-in failed: ${callback.errorDescription ?? callback.error}. ` +
          'Use any Portal control to try again.',
      );
      updateUserInfo();
      return;
    }
    if (!pkce || typeof pkce.verifier !== 'string' || pkce.state !== callback.state) {
      renderAuthError(
        'Sign-in could not be completed because the response did not match ' +
          'a sign-in started in this tab. Use any Portal control to try again.',
      );
      updateUserInfo();
      return;
    }
    try {
      tokens = await exchangeCodeForTokens(callback.code, pkce);
    } catch (error) {
      renderAuthError(
        `Sign-in failed while obtaining tokens (${error.message}). ` +
          'Use any Portal control to try again.',
      );
      updateUserInfo();
      return;
    }
    persistTokens();
    onAuthenticated();
    return;
  }

  tokens = readStoredJson(runtime.storage, TOKEN_STORAGE_KEY);
  if (isTokenSetValid(tokens, runtime.now())) {
    onAuthenticated();
    return;
  }
  if (tokens?.refreshToken) {
    try {
      tokens = await refreshTokens();
      persistTokens();
      onAuthenticated();
      return;
    } catch {
      // Refresh token no longer usable — fall through to a fresh sign-in.
    }
  }
  clearAuthState();
  await beginSignIn();
}

/**
 * `portal:ready` listener: initializes authentication from the bootstrap
 * configuration carried in the event detail (seam dispatched by
 * `src/main.js` once the capability gate passed and `config.json`
 * loaded).
 * @param {object} event - `portal:ready` CustomEvent whose
 *   `detail.config` is the loaded Portal configuration.
 * @returns {void}
 */
function handlePortalReady(event) {
  initAuth(event.detail?.config).catch((error) => {
    console.error('Cognito authentication failed to initialize.', error);
  });
}

if (typeof document !== 'undefined') {
  document.addEventListener('portal:ready', handlePortalReady);
}
