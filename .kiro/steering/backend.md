---
inclusion: fileMatch
fileMatchPattern: 'backend/**'
---

# Backend Conventions (backend/)

Standards for all Python code under `backend/`: the Voice_Service (`backend/voice_service`), the Notifier Lambda (`backend/notifier`), and shared primitives (`backend/shared`).

## Language and Runtime

- Python 3.14. Voice_Service: FastAPI + uvicorn with `websockets`. Notifier: Lambda handler driven by `asyncio.run`.
- All I/O is asynchronous (`async`/`await`): WebSocket handling, Bedrock streaming, DevOps Agent calls, DynamoDB access.
- Never invoke blocking synchronous I/O inside an async execution path. Isolate blocking SDK surfaces behind adapters using `aioboto3`, `httpx`, or `asyncio.to_thread`.

## Documentation

- Every module, class, method, and function carries a docstring describing: its purpose, each parameter by name, the return value where a value is returned, and each exception type it raises.
- Docstring coverage is enforced at 100% (`interrogate --fail-under=100`); a missing docstring fails the build.

## Errors and Exceptions

- Raise only specific exception classes from the `PortalError` hierarchy (`backend/shared/exceptions.py`, extended in `backend/voice_service/app/exceptions.py`): ConfigurationError, AuthenticationError (TokenInvalidError / TokenExpiredError), BedrockStreamError (StreamOpenError / SegmentationError), DevOpsAgentError (AgentRequestError / AgentTimeoutError), GuardrailUnavailableError, SessionStoreError, NotificationPublishError, WebPushError (SubscriptionGoneError / PushDeliveryError), TaskProtectionError.
- Never raise bare or generic `Exception`. Never catch bare `Exception` — the only exceptions are top-level boundary handlers (WebSocket connection handler, FastAPI exception middleware, Lambda entrypoint) that convert unhandled errors into error frames or responses.
- Use the shared bounded retry helper (`backend/shared/retry.py`) for retryable operations: at most 3 retries (4 attempts), exponential backoff with jitter (0.2 s / 0.8 s / 2 s), a log entry per failure, and a distinct exhaustion record naming the specific exception class.

## Architecture (SOLID ports and adapters)

- `domain/` holds pure logic only: no I/O, no SDK imports, fully unit- and property-testable.
- `ports/` holds abstract base classes (interfaces) that domain and orchestration code depend on.
- `adapters/` is the only place AWS SDKs (`boto3`, `aioboto3`, `botocore`) may be imported — enforced by an import-linter contract.
- Structure each module around a single responsibility. Reach external dependencies (Bedrock, DevOps Agent, DynamoDB, ECS agent) only through their port interfaces.

## Configuration and Secrets

- Non-sensitive configuration comes from environment variables; sensitive values come from SSM Parameter Store or Secrets Manager.
- Validate the required-key manifest at startup: a missing key raises `ConfigurationError` naming the key, and the process exits non-zero before the server binds.
- Wrap secrets in the `Secret` type whose `str`/`repr` renders `Secret(<key>)`. Never log or render a secret value; reference configuration entries by key name only.
- No hardcoded secrets, credentials, account identifiers, or environment-specific endpoints anywhere in source.

## Logging

- Structured JSON logs only: every entry carries a timestamp and a severity level.
- Every entry produced while handling a Voice_Session carries the Voice_Session identifier (use the session-scoped logger from `logging.py`).

## Quality Gates (build fails on any violation)

- `ruff check` with rule groups `D` (pydocstyle), `ASYNC`, `BLE` (no blind except), and `TRY`.
- `mypy --strict`.
- `interrogate --fail-under=100`.
- `import-linter` (AWS SDK imports confined to `adapters/`).
- `pytest` with `pytest-asyncio` for all unit and property suites.

## Testing

- Property-based tests use `hypothesis` with `settings(max_examples=100)` or more, live under `tests/property/`, and carry the tag comment `# Feature: nova-sonic-support-portal, Property N: <name>`.
- All tests run against the in-memory fakes in `backend/voice_service/tests/fakes.py` (FakeBedrockStream, FakeDevOpsAgent, FakeGuardrail, FakeSessionStore, FakeTaskProtection, FakeClock) — no AWS access, no mocking of SDK internals.
- Unit tests cover the branch-specific edge cases (failure paths, timeouts, boundary timings) alongside the property suites.
