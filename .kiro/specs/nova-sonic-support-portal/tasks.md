# Implementation Plan: Nova Sonic Support Portal

## Overview

Implementation follows the design's dependency structure: repository scaffolding and quality-gate tooling first, then shared backend primitives, pure domain modules with their property-based tests, port interfaces and AWS adapters, orchestration plus the FastAPI app, the Notifier Lambda, the frontend SPA, and finally the two Terraform layers with pipeline buildspecs, infrastructure tests, and documentation. Languages per design: Python 3.14 (backend), JavaScript ES modules (frontend), Terraform HCL (infrastructure). All 20 design correctness properties are implemented as property-based tests (hypothesis for Python, fast-check for JS, ≥100 iterations each) placed next to the code they validate and executed against in-memory fakes, no AWS access.

## Tasks

- [x] 1. Repository scaffolding and quality gates
  - [x] 1.1 Create repository layout and toolchain configuration
    - Delete the placeholder `main.py`; create top-level `infrastructure/`, `frontend/`, `backend/` folders with the module skeletons from the design (`backend/voice_service`, `backend/notifier`, `backend/shared`)
    - `backend/voice_service/pyproject.toml` and `backend/notifier` tooling: Python 3.14, FastAPI, uvicorn, websockets, aioboto3, httpx, pytest, pytest-asyncio, hypothesis; ruff with `D`, `ASYNC`, `BLE`, `TRY` rule groups; `mypy --strict`; `interrogate --fail-under=100`; import-linter contract confining AWS SDK imports to `adapters/`
    - `frontend/package.json`: vitest, fast-check, jsdom, eslint with jsdoc rules, Bootstrap 5
    - _Requirements: 18.1, 17.7_
  - [x] 1.2 Create Kiro steering files and hooks
    - `.kiro/steering/infrastructure.md`, `.kiro/steering/frontend.md`, `.kiro/steering/backend.md` documenting conventions and coding standards per code area
    - `.kiro/hooks/lint-on-save.json` and `.kiro/hooks/test-on-save.json` triggering lint and test execution on file save
    - _Requirements: 18.3, 18.4_

- [x] 2. Shared backend primitives
  - [x] 2.1 Implement exception hierarchy
    - `backend/shared/exceptions.py` (`PortalError` base) and `backend/voice_service/app/exceptions.py` with the full design hierarchy: ConfigurationError, AuthenticationError → TokenInvalidError / TokenExpiredError, BedrockStreamError → StreamOpenError / SegmentationError, DevOpsAgentError → AgentRequestError / AgentTimeoutError, GuardrailUnavailableError, SessionStoreError, NotificationPublishError, WebPushError → SubscriptionGoneError / PushDeliveryError, TaskProtectionError
    - _Requirements: 17.4, 17.5_
  - [x] 2.2 Implement structured JSON logging
    - `backend/shared/logging.py` + `backend/voice_service/app/logging.py`: every entry carries timestamp and severity; a session-scoped logger binds the Voice_Session identifier into all entries produced while handling a session
    - _Requirements: 19.4_
  - [x]* 2.3 Write property test for structured logging
    - **Property 19: Log entries carry required fields**
    - **Validates: Requirements 19.4**
    - `backend/voice_service/tests/property/test_p19_logging.py`, hypothesis ≥100 examples, tag `# Feature: nova-sonic-support-portal, Property 19: Log entries carry required fields`
  - [x] 2.4 Implement bounded async retry helper
    - `backend/shared/retry.py`: up to 3 retries (4 attempts), exponential backoff with jitter (0.2 s / 0.8 s / 2 s), retries only retryable error classes, logs each failure and a distinct exhaustion record with the specific exception class
    - _Requirements: 2.7, 5.9, 5.10, 6.8, 10.7_
  - [x]* 2.5 Write property test for the retry contract
    - **Property 14: Bounded retry contract**
    - **Validates: Requirements 2.7, 5.9, 5.10, 6.8, 10.7**
    - `backend/voice_service/tests/property/test_p14_retry.py`, hypothesis ≥100 examples, tag `# Feature: nova-sonic-support-portal, Property 14: Bounded retry contract`
  - [x] 2.6 Implement configuration loader and Secret wrapper
    - `backend/voice_service/app/config.py`: required-key manifest validated at startup; environment variables for non-sensitive values, SSM Parameter Store / Secrets Manager for sensitive values; missing key → `ConfigurationError` naming the key, process exits non-zero before the server binds; failed secret retrieval aborts startup naming the secret key only; `Secret` wrapper whose `str`/`repr` renders `Secret(<key>)`
    - _Requirements: 14.1, 14.4, 14.5, 14.6_
  - [x]* 2.7 Write property test for startup configuration validation
    - **Property 15: Startup configuration validation is complete**
    - **Validates: Requirements 14.4**
    - `backend/voice_service/tests/property/test_p15_config_validation.py`, hypothesis ≥100 examples, tag `# Feature: nova-sonic-support-portal, Property 15: Startup configuration validation is complete`
  - [x]* 2.8 Write property test for secret redaction
    - **Property 16: Secret values never appear in output**
    - **Validates: Requirements 14.6**
    - `backend/voice_service/tests/property/test_p16_secret_redaction.py`, hypothesis ≥100 examples, tag `# Feature: nova-sonic-support-portal, Property 16: Secret values never appear in output`

- [x] 3. Voice service pure domain modules
  - [x] 3.1 Implement Voice_Session state machine
    - `backend/voice_service/app/domain/session.py`: states CONNECTING / LIVE / SEGMENTING / ENDED / ERROR with legal transitions per the design state machine; transition events consumed by persistence and status frames
    - _Requirements: 1.6, 2.2, 2.5, 2.6, 9.4_
  - [x] 3.2 Implement segmentation scheduler and replay builder
    - `backend/voice_service/app/domain/segmentation.py`: rollover trigger at 7 m 30 s of stream age; replay-sequence builder emitting `sessionStart` → `promptStart` (same toolConfiguration including `ask_devops_agent`) → system-prompt block → history as USER/ASSISTANT text blocks in original chronological order, before any audio; 10-second watchdog outcome model
    - _Requirements: 2.2, 2.3, 2.6_
  - [x]* 3.3 Write property test for the replay sequence
    - **Property 2: Segmentation replay reconstructs context in order**
    - **Validates: Requirements 2.3, 3.1**
    - `backend/voice_service/tests/property/test_p02_replay.py`, hypothesis ≥100 examples, tag `# Feature: nova-sonic-support-portal, Property 2: Segmentation replay reconstructs context in order`
  - [x] 3.4 Implement bounded FIFO audio buffer
    - `backend/voice_service/app/domain/audio_buffer.py`: 30-second cap (960 KB at 16 kHz × 16-bit mono), FIFO flush byte-identical in arrival order, drop-newest on overflow with drop counter (never reorder)
    - _Requirements: 2.4_
  - [x]* 3.5 Write property test for the audio buffer
    - **Property 3: Segmentation audio buffer is a bounded FIFO**
    - **Validates: Requirements 2.4**
    - `backend/voice_service/tests/property/test_p03_audio_buffer.py`, hypothesis ≥100 examples, tag `# Feature: nova-sonic-support-portal, Property 3: Segmentation audio buffer is a bounded FIFO`
  - [x] 3.6 Implement transcript accumulator
    - `backend/voice_service/app/domain/transcript.py`: ordered role/text entries with monotonic `seq`; streamed-chunk concatenation in arrival order for tool results
    - _Requirements: 1.4, 2.5, 3.3_
  - [x]* 3.7 Write property test for chunk accumulation
    - **Property 4: Streamed chunk accumulation equals concatenation**
    - **Validates: Requirements 3.3**
    - `backend/voice_service/tests/property/test_p04_chunks.py`, hypothesis ≥100 examples, tag `# Feature: nova-sonic-support-portal, Property 4: Streamed chunk accumulation equals concatenation`
  - [x] 3.8 Implement guardrail fail-closed decision function
    - `backend/voice_service/app/domain/guardrail_policy.py`: pure `decide(response | error) -> Decision`: PASS only on `action == "NONE"` with all Automated Reasoning findings VALID; BLOCK on GUARDRAIL_INTERVENED, any non-VALID finding (INVALID / SATISFIABLE / IMPOSSIBLE / TRANSLATION_AMBIGUOUS / NO_TRANSLATION), SDK exception, timeout, or malformed response
    - _Requirements: 4.4, 4.6, 4.7_
  - [x] 3.9 Implement TTL computation
    - `backend/voice_service/app/domain/ttl.py`: last update time + configurable retention period (default 30 days), expressed in epoch seconds
    - _Requirements: 8.4_
  - [x]* 3.10 Write property test for TTL computation
    - **Property 13: TTL equals last update plus retention**
    - **Validates: Requirements 8.4**
    - `backend/voice_service/tests/property/test_p13_ttl.py`, hypothesis ≥100 examples, tag `# Feature: nova-sonic-support-portal, Property 13: TTL equals last update plus retention`

- [x] 4. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Ports and adapters
  - [x] 5.1 Define port interfaces
    - `backend/voice_service/app/ports/`: `BedrockStreamPort` (open/send_event/receive/close), `DevOpsAgentPort` (create_chat, send_message async iterator), `GuardrailPort` (evaluate → GuardrailResult), `SessionStorePort` (sessions, chats, transcripts, subscriptions), `TaskProtectionPort` (acquire/release) as abstract base classes
    - _Requirements: 17.6_
  - [x] 5.2 Implement in-memory fakes for all ports
    - `backend/voice_service/tests/fakes.py`: FakeBedrockStream, FakeDevOpsAgent, FakeGuardrail, FakeSessionStore, FakeTaskProtection, FakeClock. They are deterministic, need no AWS access, and are used by all property and unit tests
    - _Requirements: 17.6_
  - [x] 5.3 Implement Bedrock bidirectional stream adapter
    - `backend/voice_service/app/adapters/bedrock_stream_client.py`: `InvokeModelWithBidirectionalStream` over HTTP/2 (TLS) in us-east-1; event grammar encode/decode: sessionStart, promptStart with `ask_devops_agent` toolSpec, content blocks, base64 audioInput/audioOutput, toolUse/toolResult, promptEnd/sessionEnd; `StreamOpenError` on open failure
    - _Requirements: 1.6, 2.1, 3.1, 3.4, 12.7, 12.8_
  - [x] 5.4 Implement DevOps Agent adapter
    - `backend/voice_service/app/adapters/devops_agent_client.py`: boto3 `devops-agent` client (`aidevops:CreateChat` / `aidevops:SendMessage`) isolated via aioboto3/`asyncio.to_thread`; streamed chunks as async iterator; optional executionId scoping on chat creation; `AgentRequestError` on failure
    - _Requirements: 3.2, 3.3, 3.5, 3.7, 12.7_
  - [x] 5.5 Implement Guardrail adapter
    - `backend/voice_service/app/adapters/guardrail_client.py`: bedrock-runtime `ApplyGuardrail` (guardrail id/version from config, `source=INPUT`) → GuardrailResult; SDK errors, timeouts, throttles surface as `GuardrailUnavailableError`
    - _Requirements: 4.1, 12.7_
  - [x] 5.6 Implement DynamoDB session store adapter
    - `backend/voice_service/app/adapters/dynamodb_store.py`: aioboto3 access to the four tables (voice-sessions with GSI by-engineer, agent-chats, push-subscriptions, transcripts) per the data model; conditional single-item writes / `TransactWriteItems` so failures leave no partial record; TTL attribute via `domain/ttl`; `SessionStoreError` with session id logging
    - _Requirements: 8.1, 8.4, 8.5, 12.7_
  - [x] 5.7 Implement ECS task protection adapter
    - `backend/voice_service/app/adapters/ecs_task_protection.py`: `PUT $ECS_AGENT_URI/task-protection/v1/state` with expiry parameter; `TaskProtectionError` on failure
    - _Requirements: 10.3_

- [x] 6. Voice service orchestration, auth, and app wiring
  - [x] 6.1 Implement WebSocket protocol schemas
    - `backend/voice_service/app/protocol/ws_messages.py`: client frames `session.start` (executionId / incidentContext / resumeSessionId) and `session.end`; server frames `session.state`, `transcript`, `error` (categories auth_invalid | auth_expired | bedrock_unavailable | segmentation_failed | session_not_found | internal), `session.terminating`; (de)serialization and validation
    - _Requirements: 1.4, 1.6, 8.6, 9.4, 10.8_
  - [x] 6.2 Implement Cognito JWT validator
    - `backend/voice_service/app/auth/jwt_validator.py`: JWKS fetch/cache; validate signature, expiry, issuer, audience from the `Sec-WebSocket-Protocol` bearer subprotocol before accept; `TokenInvalidError` / `TokenExpiredError`; exposes token expiry deadline for the mid-session watchdog
    - _Requirements: 7.2, 7.3, 7.6, 12.5, 12.6_
  - [x]* 6.3 Write property test for JWT validation
    - **Property 10: WebSocket authentication accepts only fully valid tokens**
    - **Validates: Requirements 7.2, 7.3, 12.5, 12.6**
    - `backend/voice_service/tests/property/test_p10_jwt.py`, hypothesis ≥100 examples (valid token + mutations: altered signature, expired, wrong issuer, wrong audience, malformed, absent), tag `# Feature: nova-sonic-support-portal, Property 10: WebSocket authentication accepts only fully valid tokens`
  - [x] 6.4 Implement tool router with guardrail gate
    - `backend/voice_service/app/orchestration/tool_router.py`: on `toolUse(ask_devops_agent)` → guardrail gate (`GuardrailPort` + `guardrail_policy.decide`); on PASS: chat lookup in Session_Store, `create_chat` once per session (scoped to the session's executionId when present, unscoped otherwise; recreate + persist if mapping missing), `send_message` under `asyncio.timeout(60)`, accumulate chunks in order, return answer as `toolResult`; on BLOCK: refusal toolResult (read/diagnostic-only message) without any agent call + audit log entry (blocked content, session id, engineer identity, timestamp); agent failure/timeout → error toolResult + log with exception class and session id
    - _Requirements: 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 4.1, 4.3, 4.4, 4.5_
  - [x]* 6.5 Write property test for chat reuse
    - **Property 5: Chat identifier is created once and reused**
    - **Validates: Requirements 3.2, 3.9**
    - `backend/voice_service/tests/property/test_p05_chat_reuse.py`, hypothesis ≥100 examples, tag `# Feature: nova-sonic-support-portal, Property 5: Chat identifier is created once and reused`
  - [x]* 6.6 Write property test for execution scoping
    - **Property 6: Execution scoping follows the session's origin**
    - **Validates: Requirements 3.5, 3.6**
    - `backend/voice_service/tests/property/test_p06_scoping.py`, hypothesis ≥100 examples, tag `# Feature: nova-sonic-support-portal, Property 6: Execution scoping follows the session's origin`
  - [x]* 6.7 Write property test for the fail-closed guardrail gate
    - **Property 7: Guardrail gate is fail-closed**
    - **Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7**
    - `backend/voice_service/tests/property/test_p07_fail_closed.py`, hypothesis ≥100 examples across all outcome classes (pass, intervention, every AR finding type, SDK exception, timeout, malformed), tag `# Feature: nova-sonic-support-portal, Property 7: Guardrail gate is fail-closed`
  - [x]* 6.8 Write unit tests for tool router edge cases
    - First-call chat creation and missing-mapping recovery; agent API failure returns spoken error indication; 60-second timeout stops stream consumption
    - _Requirements: 3.2, 3.7, 3.8, 3.10_
  - [x] 6.9 Implement voice session manager
    - `backend/voice_service/app/orchestration/voice_session_manager.py`: per-connection asyncio task group, inbound audio pump (forward ≤500 ms after receipt), outbound pump (audioOutput → binary frames, textOutput → transcript frames + accumulator), segmentation timer with rollover (drain, replay via 3.2, flush buffered audio FIFO), token-expiry watchdog (auth_expired frame, close, drop post-expiry audio), transcript persister with bounded retry; every Session_Store write completes before the state change is reported (state frames after store write); session end → promptEnd/sessionEnd, persist final transcript, status ENDED; reconnect via resumeSessionId → restore persisted state + transcripts and replay into a fresh stream, or `session_not_found` error; stream-open failure → `bedrock_unavailable` error frame, close, end session; segmentation failure/watchdog → persist partial transcript, `segmentation_failed` frame, log with session id; incident-scoped sessions inject summary/severity into the system prompt
    - _Requirements: 1.2, 1.3, 1.6, 2.2, 2.4, 2.5, 2.6, 2.7, 5.8, 7.6, 8.2, 8.3, 8.6, 8.7, 9.5_
  - [x]* 6.10 Write property test for persist-before-confirm ordering
    - **Property 11: State changes persist before they are confirmed**
    - **Validates: Requirements 8.2, 8.7**
    - `backend/voice_service/tests/property/test_p11_persist_first.py`, hypothesis ≥100 examples, tag `# Feature: nova-sonic-support-portal, Property 11: State changes persist before they are confirmed`
  - [x]* 6.11 Write property test for reconnect restoration
    - **Property 12: Reconnect restores exactly what was persisted**
    - **Validates: Requirements 8.3**
    - `backend/voice_service/tests/property/test_p12_reconnect.py`, hypothesis ≥100 examples, tag `# Feature: nova-sonic-support-portal, Property 12: Reconnect restores exactly what was persisted`
  - [x]* 6.12 Write unit tests for session lifecycle edge cases
    - Segmentation boundary fires at 7 m 30 s; watchdog failure path (>10 s) persists partial transcript and emits `segmentation_failed`; mid-session token expiry closes and drops post-expiry audio; Bedrock open failure emits `bedrock_unavailable` and ends session; unknown/expired resumeSessionId rejected with `session_not_found`
    - _Requirements: 1.6, 2.2, 2.6, 7.6, 8.6_
  - [x] 6.13 Implement task protection manager
    - `backend/voice_service/app/orchestration/protection_manager.py` + live-session registry: acquire protection on session count 0→1, release within 60 s of count reaching 0, rolling expiry refresh for long sessions; on failure retry ≤3 via shared retry, then flip `/healthz` readiness flag to 503 until protection is confirmed
    - _Requirements: 10.3, 10.4, 10.7, 19.6, 19.7_
  - [x]* 6.14 Write property test for task protection tracking
    - **Property 17: Task protection tracks live sessions**
    - **Validates: Requirements 10.3, 10.4, 19.6, 19.7**
    - `backend/voice_service/tests/property/test_p17_protection.py`, hypothesis ≥100 examples with FakeClock interleavings, tag `# Feature: nova-sonic-support-portal, Property 17: Task protection tracks live sessions`
  - [x] 6.15 Implement drain manager
    - `backend/voice_service/app/orchestration/drain_manager.py`: SIGTERM → reject new WebSocket upgrades, `/healthz` 503; existing sessions continue up to 120 s (`stopTimeout`); sessions still active at expiry receive `{"type":"session.terminating"}` before close
    - _Requirements: 10.5, 10.8_
  - [x] 6.16 Implement FastAPI app factory and wire everything together
    - `backend/voice_service/app/main.py`: app factory running startup config validation; `/ws/voice` endpoint validating JWT before accept (close 4401 on failure, no session created) and handing off to the session manager; `/healthz` combining drain + protection readiness; `POST/DELETE /api/push-subscriptions` (JWT-authenticated) persisting Web_Push_Subscriptions via `SessionStorePort` before confirming to the caller; SIGTERM handler → drain manager; top-level boundary handlers converting unhandled errors into error frames (only place broad catches are permitted)
    - _Requirements: 1.1, 6.1, 7.3, 7.5, 8.7, 12.6, 14.4, 17.2, 17.3, 17.5_
  - [x] 6.17 Write voice service Dockerfile
    - `backend/voice_service/Dockerfile`: Python 3.14 slim, non-root user, uvicorn entrypoint honoring SIGTERM
    - _Requirements: 10.1, 10.5_

- [x] 7. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Notifier Lambda
  - [x] 8.1 Implement event normalizer
    - `backend/notifier/src/normalizer.py`: pure mapping from the three EventBridge source shapes (CloudWatch Alarm, Incident Manager, DevOps Agent finding) → Incident_Notification `{notificationId, source, summary, severity, timestamp, executionId?, detail}` with the design's field mappings; executionId included iff the source event provides one
    - _Requirements: 5.5, 5.6_
  - [x]* 8.2 Write property test for normalization
    - **Property 8: Notification normalization is total and preserving**
    - **Validates: Requirements 5.5, 5.6**
    - `backend/notifier/tests/property/test_p08_normalizer.py`, hypothesis ≥100 examples over arbitrary source-field content, tag `# Feature: nova-sonic-support-portal, Property 8: Notification normalization is total and preserving`
  - [x] 8.3 Implement AppSync Events publisher
    - `backend/notifier/src/channels/appsync_publisher.py`: SigV4 `POST /event` to channel `/incidents/all`; shared retry ≤3 with distinct exhaustion logging
    - _Requirements: 5.1, 5.9, 5.10_
  - [x] 8.4 Implement subscription repository and web push sender
    - `backend/notifier/src/subscription_repo.py` (DynamoDB push-subscriptions access) + `backend/notifier/src/channels/webpush_sender.py`: pywebpush with VAPID (private key from Secrets Manager); payload carries summary + executionId; 404/410 → delete subscription and stop deliveries; other failures retry ≤3 then discard for that subscription; per-subscription isolation so one failure never blocks the rest
    - _Requirements: 6.2, 6.5, 6.8_
  - [x]* 8.5 Write property test for push fan-out
    - **Property 9: Push fan-out is complete and self-cleaning**
    - **Validates: Requirements 6.2, 6.5**
    - `backend/notifier/tests/property/test_p09_fanout.py`, hypothesis ≥100 examples over mixed delivery outcomes, tag `# Feature: nova-sonic-support-portal, Property 9: Push fan-out is complete and self-cleaning`
  - [x]* 8.6 Implement SNS escalation publisher
    - `backend/notifier/src/channels/sns_publisher.py`: publish the Incident_Notification to the configured SNS topic when escalation is configured (no-op otherwise)
    - _Requirements: 5.11_
  - [x] 8.7 Implement Lambda handler wiring channels concurrently
    - `backend/notifier/src/handler.py`: `asyncio.run` entrypoint; normalize → run AppSync, Web Push, and (when configured) SNS channels concurrently and independently so one channel's failure never suppresses the others; async I/O throughout
    - _Requirements: 5.1, 17.2, 17.3_

- [x] 9. Frontend SPA
  - [x] 9.1 Implement app shell, runtime config, and capability gate
    - `frontend/public/index.html` (Bootstrap 5 responsive layout, all features operable without horizontal scrolling from 320 px to 1920 px, mobile parity), `frontend/src/main.js` bootstrapping, runtime `config.json` loading (Cognito ids, wss URL, Events endpoints, VAPID public key), `frontend/src/capability.js` detecting getUserMedia / AudioWorklet / WebSocket / serviceWorker+PushManager and refusing session start with an unsupported-browser error when any is missing
    - _Requirements: 9.1, 9.2, 9.3, 9.7, 14.3_
  - [x]* 9.2 Write property test for the capability gate
    - **Property 20: Capability gate blocks unsupported browsers**
    - **Validates: Requirements 9.7**
    - `frontend/tests/property/p20_capability.test.js`, fast-check `numRuns: 100` over capability subsets, tag `// Feature: nova-sonic-support-portal, Property 20: Capability gate blocks unsupported browsers`
  - [x] 9.3 Implement Cognito authentication and route guard
    - `frontend/src/auth/cognito.js`: OAuth 2.0 authorization code + PKCE against the Cognito Hosted UI, token storage and refresh; route guard denying unauthenticated access to any feature and redirecting to sign-in
    - _Requirements: 7.1, 7.7_
  - [x] 9.4 Implement microphone capture worklet
    - `frontend/src/audio/capture.js` + `frontend/src/audio/pcm-worklet.js`: getUserMedia; AudioWorklet downsampling Float32 at browser rate → 16 kHz 16-bit mono PCM Int16; permission denial → explanatory error and no WebSocket opened
    - _Requirements: 1.1, 1.8, 9.6_
  - [x]* 9.5 Write property test for PCM conversion
    - **Property 1: PCM conversion preserves audio structure**
    - **Validates: Requirements 1.1**
    - `frontend/tests/property/p01_pcm.test.js`, fast-check `numRuns: 100` over Float32 buffers and sample rates, tag `// Feature: nova-sonic-support-portal, Property 1: PCM conversion preserves audio structure`
  - [x] 9.6 Implement audio playback queue
    - `frontend/src/audio/playback.js`: 24 kHz 16-bit PCM playback queue via AudioContext; playback begins within 1 s of the first chunk
    - _Requirements: 1.7_
  - [x] 9.7 Implement voice WebSocket client
    - `frontend/src/ws/voice-client.js`: wss:// to CloudFront `/ws/voice` with the JWT as bearer subprotocol; binary PCM out (16 kHz) / in (24 kHz); JSON control frames per protocol; interruption notification within 5 s + reconnect button re-establishing with `resumeSessionId`; `session_not_found` and error-frame handling
    - _Requirements: 1.1, 1.5, 8.6_
  - [x] 9.8 Implement transcript, status, and error UI
    - `frontend/src/ui/transcript.js` (role-distinguished rendering within 2 s of receipt), `frontend/src/ui/status.js` (connecting | live | segmenting | ended | error badge, updated within 1 s of change), `frontend/src/ui/errors.js` (mic-denied, unsupported-browser, auth-expired messaging)
    - _Requirements: 1.4, 1.8, 9.4, 9.5, 9.6, 9.7_
  - [x]* 9.9 Write property test for rendering
    - **Property 18: Rendering is complete and role-distinguished**
    - **Validates: Requirements 1.4, 5.2**
    - `frontend/tests/property/p18_rendering.test.js`, fast-check `numRuns: 100` over transcript frames and notification payloads (jsdom), tag `// Feature: nova-sonic-support-portal, Property 18: Rendering is complete and role-distinguished`
  - [x] 9.10 Implement AppSync events client, incident popup, and chime
    - `frontend/src/events/appsync-client.js`: Cognito-authorized realtime subscription to `/incidents/all`; `frontend/src/ui/incident-popup.js`: popup with summary, severity, timestamp within 2 s + audio chime; blocked/failed chime never suppresses the popup; popup click opens a Voice_Session scoped to executionId, or carrying summary/severity context when absent
    - _Requirements: 5.2, 5.3, 5.4, 5.7, 5.8, 7.4_
  - [x] 9.11 Implement push manager and service worker
    - `frontend/src/push/push-manager.js`: permission flow; on grant, register Web_Push_Subscription via the service worker and persist through `/api/push-subscriptions` within 10 s; denial → push-disabled indication, no registration; persistence failure → error message with retry option. `frontend/public/sw.js`: push event → system notification with the incident summary; notificationclick → open Portal, verify authenticated session (prompt if none), start Voice_Session scoped to the executionId
    - _Requirements: 6.1, 6.3, 6.4, 6.6, 6.7_
  - [x]* 9.12 Write frontend unit tests for notification and push edge cases
    - Chime blocked → popup still shown; popup click with and without executionId; push permission denied indication; subscription persistence failure + retry; reconnect banner on interruption; status badge renders all five states
    - _Requirements: 1.5, 5.3, 5.4, 5.7, 5.8, 6.6, 6.7, 9.4_

- [x] 10. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 11. Terraform bootstrap layer
  - [x] 11.1 Implement source buckets module
    - `infrastructure/bootstrap/modules/source_buckets/`: three versioned S3 source buckets (frontend / backend / iac) with SSE and TLS-deny policies; EventBridge rules triggering the matching pipeline on object upload
    - _Requirements: 15.2, 16.1, 16.2_
  - [x] 11.2 Implement pipeline and CodeBuild modules
    - `infrastructure/bootstrap/modules/pipeline/` + `modules/codebuild/`: reusable CodePipeline V2 definition Source(S3) → SecurityScan → UnitTest → Build+Plan → ManualApproval (7-day timeout) → Deploy; each stage starts only after the previous succeeds and a failing stage stops the execution; no CodePipeline S3 deploy action anywhere; per-stage CodeBuild projects reading buildspecs from the source artifact
    - _Requirements: 15.2, 16.1, 16.3, 16.5, 16.6, 16.7, 16.8, 16.10_
  - [x] 11.3 Implement ECR, artifact store, state backend, and bootstrap root
    - `infrastructure/bootstrap/modules/ecr/` (scan-on-push, SSE), artifact bucket + CI/CD IAM roles for CodePipeline/CodeBuild, `modules/state_backend/` (app-layer state bucket + DynamoDB lock table); root `main.tf`/`variables.tf`/`outputs.tf` instantiating the three pipelines; bootstrap keeps its own separate state
    - _Requirements: 12.3, 15.2, 15.6_

- [x] 12. Terraform application layer
  - [x] 12.1 Implement network, ALB, ECS service, and IAM modules
    - `infrastructure/app/modules/network/` (VPC, public/private subnets across ≥2 AZs, NAT), `modules/alb/` (HTTP listener, deletion protection enabled, access logs to the provided existing bucket), `modules/ecs_service/` (cluster, task definition, min 2 tasks across 2 AZs, `stopTimeout=120`, autoscaling step policies on ALB ActiveConnectionCount for scale-out within 5 minutes and scale-in respecting protection and the minimum), `modules/iam/` (task role: `bedrock:InvokeModelWithBidirectionalStream`, `bedrock:ApplyGuardrail`, `aidevops:CreateChat`/`aidevops:SendMessage`, DynamoDB, `ecs:UpdateTaskProtection`; execution role)
    - _Requirements: 10.1, 10.2, 10.6, 12.4, 13.1, 13.5, 15.3, 19.5_
  - [x] 12.2 Implement Cognito, DynamoDB, AppSync Events, and Guardrail modules
    - `infrastructure/app/modules/cognito/` (user pool, PKCE SPA client, hosted UI domain), `modules/dynamodb/` (four tables per data model with keys, GSI by-engineer, TTL attribute, SSE, PITR), `modules/appsync_events/` (Events API with Cognito connect/subscribe auth and IAM publish auth), `modules/bedrock_guardrail/` (guardrail + Automated Reasoning read-only-operations policy attachment blocking create/modify/delete/terminate requests)
    - _Requirements: 4.1, 4.2, 7.1, 7.4, 7.8, 8.1, 8.4, 12.3, 15.3_
  - [x] 12.3 Implement CloudFront+S3, WAF, and S3 policy modules
    - `infrastructure/app/modules/cloudfront_s3/` (frontend bucket with OAC-only read policy denying all other principals, dual origin S3 + ALB for `/ws/*` and `/api/*`, redirect-http-to-https, origin-verify header injection), `modules/waf/` (CLOUDFRONT + REGIONAL web ACLs, AWSManagedRulesCommonRuleSet + AWSManagedRulesKnownBadInputsRuleSet in block mode, WAF logging with timestamp/source IP/URI/rule id to a persistent destination), `modules/s3_policies/` (deny `aws:SecureTransport=false` and TLS <1.2 on every created bucket)
    - _Requirements: 9.1, 11.1, 11.2, 11.3, 11.5, 11.6, 12.1, 12.2, 13.2, 13.3, 13.7_
  - [x] 12.4 Implement notifications and observability modules
    - `infrastructure/app/modules/notifications/` (EventBridge rules for CloudWatch Alarms, Incident Manager, and DevOps Agent findings; notifier Lambda packaging, environment, and IAM; optional SNS escalation topic), `modules/observability/` (CloudWatch alarms for running task count, ALB unhealthy targets, Voice_Service error rate, and Notifier delivery failures, each with metric, threshold, evaluation period, and alarm actions publishing to the operations SNS topic)
    - _Requirements: 5.1, 5.11, 15.4, 19.2, 19.3_
  - [x] 12.5 Implement app-layer root with validated variables
    - `infrastructure/app/main.tf` / `variables.tf` / `outputs.tf`: input variables for environment name, account inputs, `access_logging_bucket_name` (validation block rejecting empty values with a clear message), scaling thresholds, retention days: no hardcoded environment values; outputs feeding frontend `config.json` generation; separate app-layer state backend pointing at the bootstrap-created bucket/lock table
    - _Requirements: 13.4, 13.6, 14.3, 15.5, 15.6, 15.7_

- [x] 13. CI/CD buildspecs and infrastructure tests
  - [x] 13.1 Write pipeline buildspecs and source push helper
    - Backend buildspecs: security scan (`bandit -ll`, `pip-audit`, `gitleaks`), unit test (ruff, mypy --strict, interrogate, import-linter, pytest), build (docker build + push to ECR), deploy (ECS service update); frontend buildspecs: scan (`npm audit --audit-level=high`, eslint, gitleaks), test (vitest --run), build, deploy (`aws s3 sync` in CodeBuild + CloudFront invalidation + `config.json` generation from Terraform outputs); iac buildspecs: scan (`checkov --check-severity HIGH`, gitleaks, hardcoded-value grep gate), test (fmt-check, validate, plan-assertion suite), plan, apply; `scripts/push-source.sh` (`git archive` → `aws s3 cp`) as the pipeline trigger helper
    - _Requirements: 14.2, 14.3, 16.3, 16.4, 16.9, 16.10_
  - [x]* 13.2 Write Terraform plan-assertion test suite
    - pytest against `terraform show -json`: ALB deletion protection; SecureTransport + TLS≥1.2 deny statements on every created bucket; no logging-bucket creation and correct external reference; OAC-only frontend bucket policy; WAF ACLs with both managed rule sets in block mode and logging attached; DynamoDB tables/keys/TTL/SSE; ECS min 2 tasks across 2 AZs with stopTimeout 120; four CloudWatch alarms wired to the ops SNS topic; three pipelines with the required stage order, manual approval, and no S3 deploy action
    - _Requirements: 8.1, 8.4, 10.1, 11.1, 11.2, 11.3, 11.6, 12.3, 13.1, 13.2, 13.3, 13.5, 13.7, 16.1, 16.3, 16.6, 16.10, 19.2, 19.3_
  - [x]* 13.3 Write negative Terraform validation tests
    - `terraform plan` with empty `access_logging_bucket_name` and with missing required variables must fail with the expected error messages, without creating or modifying resources
    - _Requirements: 13.6, 15.7_
  - [x]* 13.4 Write post-deploy smoke-test scripts
    - Scripted checks runnable against a deployed environment: canonical destructive utterances blocked by the live guardrail; AppSync Events connect accepted with and rejected without a Cognito token; direct-ALB unauthenticated connection rejected; HTTP→HTTPS redirect and direct-S3 403; WAF blocks a known-bad-input probe
    - _Requirements: 4.2, 7.4, 7.5, 7.8, 11.4, 12.2, 13.7_

- [x] 14. Documentation
  - [x] 14.1 Write README with architecture, prerequisites, and deployment steps
    - `README.md` at the repository root: architecture overview; prerequisites list (AWS account with Bedrock Nova 2 Sonic + Guardrails Automated Reasoning access, Terraform ≥1.9, AWS CLI, Docker, Node 24, Python 3.14); step-by-step deployment in order: bootstrap `terraform init/apply`, push iac source (IaC_Pipeline applies the app layer), push backend source (image → ECR → ECS), push frontend source (build → s3 sync → invalidation). Each step comes with the exact command and the observable outcome indicating success
    - _Requirements: 18.2, 18.5_

- [x] 15. Final checkpoint - Full local quality gate
  - Run backend gates: ruff, mypy --strict, interrogate --fail-under=100, import-linter, pytest (unit + property suites)
  - Run frontend gates: eslint, vitest --run
  - Run terraform fmt -check and terraform validate on both layers
  - Ensure all tests pass, ask the user if questions arise.
  - _Requirements: 17.7_

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP: all test-only sub-tasks, the SNS escalation channel (8.6), the Terraform plan-assertion and negative-validation suites (13.2, 13.3), and the post-deploy smoke scripts (13.4)
- Each of the design's 20 correctness properties has exactly one property-based test sub-task, placed next to the code it validates; backend tests use hypothesis, frontend tests use fast-check, all with ≥100 iterations and the tag comment format `# Feature: nova-sonic-support-portal, Property N: <name>` (`//` in JS)
- All property and unit tests run against the in-memory fakes from task 5.2 (FakeBedrockStream, FakeDevOpsAgent, FakeSessionStore, FakeTaskProtection, FakeClock): no AWS access required
- Each task references the granular acceptance criteria it implements for traceability; Requirement 19.1 (Well-Architected documentation) is already satisfied by the design document
- Checkpoints (tasks 4, 7, 10, 15) ensure incremental validation at phase boundaries
- This plan covers writing code, tests, Terraform, buildspecs, and documentation only; actual AWS deployment (terraform apply against an account, pipeline runs) is performed by the operator per the README

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["2.1", "2.2", "9.1"] },
    { "id": 2, "tasks": ["2.4", "2.6", "3.1", "3.4", "3.6", "3.9", "9.2", "9.3"] },
    { "id": 3, "tasks": ["2.3", "2.5", "2.7", "2.8", "3.2", "3.5", "3.7", "3.8", "3.10", "9.4", "9.6"] },
    { "id": 4, "tasks": ["3.3", "5.1", "9.5", "9.7", "9.8"] },
    { "id": 5, "tasks": ["5.2", "5.3", "5.4", "5.5", "5.6", "5.7", "9.9", "9.10", "9.11"] },
    { "id": 6, "tasks": ["6.1", "6.2", "6.4", "8.1", "9.12"] },
    { "id": 7, "tasks": ["6.3", "6.5", "6.6", "6.7", "6.8", "6.9", "6.13", "8.2", "8.3", "8.4"] },
    { "id": 8, "tasks": ["6.10", "6.11", "6.12", "6.14", "6.15", "8.5", "8.6"] },
    { "id": 9, "tasks": ["6.16", "6.17", "8.7"] },
    { "id": 10, "tasks": ["11.1", "11.2", "12.1", "12.2", "12.3", "12.4"] },
    { "id": 11, "tasks": ["11.3", "12.5"] },
    { "id": 12, "tasks": ["13.1", "13.2", "13.3", "13.4"] },
    { "id": 13, "tasks": ["14.1"] }
  ]
}
```
