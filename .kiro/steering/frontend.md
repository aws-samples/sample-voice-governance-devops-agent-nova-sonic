---
inclusion: fileMatch
fileMatchPattern: 'frontend/**'
---

# Frontend Conventions (frontend/)

Standards for the browser SPA under `frontend/`: static assets in `frontend/public/`, ES modules in `frontend/src/`, tests in `frontend/tests/`.

## Stack

- Vanilla JavaScript ES modules: no SPA framework. Bootstrap 5 for layout and components.
- Tooling (eslint, vitest) targets Node.js 24 LTS.
- Served as static assets from S3 through CloudFront; the built artifact is environment-independent.

## Responsive Layout

- Every Portal feature must be operable without horizontal scrolling at viewport widths from 320 px to 1920 px.
- Mobile browsers get the same functionality as desktop: microphone capture, audio playback, transcripts, and notifications.

## Documentation

- JSDoc on every function: purpose, each `@param` by name, `@returns` where a value is returned, and `@throws` for each thrown error type. Enforced by eslint jsdoc rules; violations fail the build.

## Web Audio Conventions

- Capture: `getUserMedia` plus an AudioWorklet processor (`src/audio/pcm-worklet.js`) that downsamples Float32 at the browser's native rate to 16 kHz 16-bit mono PCM (Int16). Audio processing stays inside the worklet; never resample on the main thread.
- Playback: 24 kHz 16-bit PCM queue through an `AudioContext` (`src/audio/playback.js`); playback begins within 1 second of the first chunk.
- Feature-detect before use (`src/capability.js`): `navigator.mediaDevices.getUserMedia`, `AudioWorklet`, `WebSocket`, and `serviceWorker` + `PushManager` for push. Unsupported browsers get a clear unsupported-browser error and session start is refused: never a broken session.
- Microphone permission denial shows an explanatory error and never opens the WebSocket.

## Configuration

- No hardcoded environment values: no Cognito ids, endpoints, wss URLs, or VAPID keys in source. All environment-specific values load at runtime from `config.json`, generated at deploy time from Terraform outputs (never committed).

## Authentication

- Cognito OAuth 2.0 authorization code + PKCE against the Hosted UI (`src/auth/cognito.js`), with token refresh. A route guard denies unauthenticated access to any feature and redirects to sign-in.
- The voice WebSocket carries the JWT as the `("bearer", <jwt>)` subprotocol pair in `Sec-WebSocket-Protocol`: never in URLs or query strings.

## Quality Gates (build fails on any violation)

- `eslint` with jsdoc rules.
- `vitest --run` (always non-interactive) with jsdom.

## Testing

- Property-based tests use `fast-check` with `{ numRuns: 100 }` or more, live under `frontend/tests/property/`, and carry the tag comment `// Feature: nova-sonic-support-portal, Property N: <name>`.
- Unit tests cover notification and push edge cases (chime blocked, popup click scoping, permission denial, persistence retry) and UI state rendering (all five status badge states).
