# Contributing Guidelines

Thank you for your interest in contributing to the Nova Sonic Support Portal. Whether it's a bug report, a new feature, or a documentation improvement, contributions are welcome.

## Reporting Bugs and Requesting Features

Open an issue on the repository's issue tracker. Check existing open and recently closed issues first to avoid duplicates.

For bug reports, include:

- Steps to reproduce
- Expected vs actual behaviour
- Which plane is affected: voice session, notifications, or deployment
- Relevant versions: Python, Node.js, Terraform, AWS provider
- Structured log entries with the Voice_Session id where applicable (never paste tokens, secrets, or account ids)

## Before You Start

Read [Writing the Change](#writing-the-change) below before your first contribution. The conventions there are enforced by the build, not merely advisory: a change that ignores them fails the pipeline rather than reaching review.

For anything beyond a small fix, open an issue describing your intended change first. This avoids duplicate work and gives maintainers a chance to comment on the approach early.

## Development Environment

Prerequisites:

| Requirement | Notes |
|---|---|
| **Python 3.14+** | Both backend packages declare `requires-python = ">=3.14"` |
| **[uv](https://docs.astral.sh/uv/) 0.9+** | Provisions the interpreter and all Python environments |
| **Node.js 24+** | `frontend/package.json` sets `engines.node = ">=24"` |
| **Terraform 1.9+** | Pipelines pin 1.9.8; both layers require `>= 1.9` |
| **Docker** | Only to build the voice-service image locally |
| **AWS CLI v2, jq, zip, git** | Used by `scripts/deploy.sh`, `scripts/push-source.sh`, and the smoke checks |

No AWS credentials are needed to run the test suites. Backend tests run entirely against in-memory fakes, and the Terraform suites use `terraform init -backend=false`.

### Setup

```bash
git clone <repository-url> nova-sonic-support-portal
cd nova-sonic-support-portal

# Backend: one environment per package
cd backend/voice_service && uv venv --python 3.14 .venv && uv pip install --python .venv/bin/python ".[dev]" && cd -
cd backend/notifier      && uv venv --python 3.14 .venv && uv pip install --python .venv/bin/python ".[dev]" && cd -

# Frontend
cd frontend && npm ci && cd -
```

## Running the Quality Gates Locally

Run these before opening a review. They are exactly what the pipelines run, so a local pass means a green pipeline.

### Backend

`backend/shared` carries no `pyproject.toml` of its own and is checked with the voice service's configuration.

```bash
cd backend/voice_service
.venv/bin/ruff check app tests ../shared
MYPYPATH="$PWD/.." .venv/bin/mypy
.venv/bin/interrogate app tests
.venv/bin/interrogate --fail-under=100 ../shared
PYTHONPATH="$PWD/.." .venv/bin/lint-imports
PYTHONPATH="$PWD/.." .venv/bin/python -m pytest -q

cd ../notifier
.venv/bin/ruff check src tests
MYPYPATH="$PWD/.." .venv/bin/mypy
.venv/bin/interrogate src tests
PYTHONPATH="$PWD/.." .venv/bin/lint-imports
PYTHONPATH="$PWD/.." .venv/bin/python -m pytest -q
```

What each gate enforces:

- **ruff** with rule groups `D` (pydocstyle, Google convention), `ASYNC`, `BLE` (no blind `except`), `TRY` (exception hygiene).
- **mypy `--strict`**. Only untyped third-party SDK surfaces carry targeted `ignore_missing_imports` overrides; application code stays fully strict.
- **interrogate `--fail-under=100`**: every module, class, method, and function needs a docstring covering purpose, each parameter by name, the return value, and each exception raised.
- **import-linter**: AWS SDK imports are confined to `adapters/`. Two contracts apply to the voice service: `domain`, `ports`, `orchestration`, `auth`, `config`, `protocol`, and `exceptions` may not import any SDK, and `app.main` (the composition root) may not import one *directly* even though it wires the adapters that do. For the notifier, `src.normalizer` stays SDK-free.
- **pytest** with `pytest-asyncio` in auto mode.

### Frontend

```bash
cd frontend
npx eslint .          # or: npm run lint
npx vitest --run      # or: npm test, always non-interactive
```

### Infrastructure

```bash
terraform fmt -check -recursive infrastructure

cd infrastructure/bootstrap && terraform init -backend=false -input=false && terraform validate && cd -
cd infrastructure/app       && terraform init -backend=false -input=false && terraform validate && cd -

uv venv --python 3.14 /tmp/iac-tests
uv pip install --python /tmp/iac-tests/bin/python -r infrastructure/tests/requirements.txt
/tmp/iac-tests/bin/python -m pytest infrastructure/tests -q
```

### Security gates

The pipelines also run blocking security scans. Reproduce them locally when your change touches dependencies, Terraform, or anything that could read as a secret:

```bash
gitleaks detect --no-git --redact --source .
bandit -ll -r backend                                    # backend changes
npm audit --audit-level=high                             # frontend changes (from frontend/)
checkov --directory infrastructure --quiet \
  --hard-fail-on HIGH --hard-fail-on CRITICAL            # infrastructure changes
```

Two additional IaC gates are plain greps that fail the build (see `ci/iac/scan.yml`): no 12-digit AWS-account-id-like values anywhere in source, and no hardcoded `us-east-1` in Terraform outside `variables.tf` or comments. See [SECURITY.md](SECURITY.md) for the full scanning picture.

## Writing the Change

### Conventions that will fail the build if ignored

- **Ports and adapters.** `domain/` is pure logic with no I/O and no SDK imports. `ports/` holds the abstract interfaces. `adapters/` is the only place `boto3`, `aioboto3`, or `botocore` may be imported.
- **All backend I/O is async.** Never call blocking synchronous I/O on an async path; isolate blocking libraries behind an adapter using `aioboto3`, `httpx`, or `asyncio.to_thread`.
- **Specific exceptions only**, from the `PortalError` hierarchy in `backend/shared/exceptions.py` and `backend/voice_service/app/exceptions.py`. Never raise or catch bare `Exception` outside a top-level boundary handler (WebSocket connection handler, FastAPI middleware, Lambda entrypoint).
- **Retries go through `backend/shared/retry.py`**: bounded at 3 retries with exponential backoff and jitter, a log line per failure, and a distinct exhaustion record.
- **Structured JSON logging only**, and every entry produced while handling a voice session carries the session id. Use the session-scoped logger.
- **JSDoc on every frontend function**: purpose, each `@param` by name, `@returns`, and `@throws` per error type.
- **No hardcoded environment values.** No account ids, endpoints, `wss://` URLs, Cognito ids, or VAPID keys in source. Frontend values load at runtime from `config.json`, generated at deploy time from Terraform outputs and never committed. Backend secrets come from SSM and are wrapped in the `Secret` type.
- **Terraform region is a variable**, never a literal.

### Tests

Add tests for behaviour you change. This repo leans on property-based testing alongside unit tests:

- **Backend**: `hypothesis` with `settings(max_examples=100)` or more, under `tests/property/`, tagged `# Feature: nova-sonic-support-portal, Property N: <name>`. Tests run against the in-memory fakes in `backend/voice_service/tests/fakes.py`: no AWS access and no mocking of SDK internals.
- **Frontend**: `fast-check` with `{ numRuns: 100 }` or more, under `frontend/tests/property/`, tagged `// Feature: nova-sonic-support-portal, Property N: <name>`.
- **Infrastructure**: plan assertions under `infrastructure/tests/`, which check security defaults against `terraform show -json`.

If you change the guardrail's DENY topic wording, re-run the probe matrix described in `infrastructure/app/modules/bedrock_guardrail/main.tf` before submitting. That wording is empirically calibrated, and plausible-looking edits have previously caused read-only questions to be refused while privilege-escalation requests passed.

## Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org):

```
fix(voice_service): Surface agent throttling and stop retry storms

The DevOps Agent adapter treated throttling as a generic failure, so the
bounded retry helper kept re-issuing a request the service had already
rejected. Map the throttling response to AgentRequestError and let the
caller back off instead.

Closes #42
```

Rules:

- Format: `type(scope): Subject`: capitalized subject, imperative mood ("Add", not "Added" or "Adds"), no trailing period, 50 characters or fewer.
- Types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `chore`, `ci`.
- Scopes used in this repo: `voice_service`, `notifier`, `frontend`, `infrastructure`, `security`, `agent`, `ci`, `docs`.
- Body: explain what and why, not how. Wrap at 72 characters.
- Build the affected package before committing.

## Submitting Your Change

1. Branch off `main`.
2. Make the change and run the relevant gates above.
3. Open a code review against `main`. Keep the title and description accurate: they become the squash commit message.
4. Iterate on feedback by pushing new commits to the same branch rather than rewriting pushed history.

One logical change per review. A feature plus its documentation is fine together; unrelated changes belong in separate reviews.

### Checklist

- [ ] Relevant lint, type-check, docstring, import-linter, and test gates pass locally
- [ ] Tests added or updated for the changed behaviour
- [ ] `terraform fmt -check -recursive infrastructure` passes (for infrastructure changes)
- [ ] No secrets, credentials, account ids, or environment-specific endpoints added
- [ ] Documentation updated (README, steering, or inline) where behaviour changed
- [ ] Commit messages follow Conventional Commits

### Changes that need extra care

- **Deployment flow or buildspecs** (`ci/**`, `scripts/deploy.sh`): the deploy path is idempotent by design. Preserve that: re-running any stage must converge rather than create duplicate or orphaned resources.
- **`container_image` in `infrastructure/app/envs/<env>.tfvars`**: after a backend deploy this must be re-pinned to the deployed image URI, or the next IaC apply rolls ECS back. See the README's Step 2 catch-up contract.
- **Guardrail and mutation-guard logic**: the fail-closed gate is the portal's core safety property. Any change here should come with tests proving that ambiguity, errors, and `GUARDRAIL_INTERVENED` all still resolve to BLOCK.
- **Dependency bumps**: run `pip-audit` or `npm audit --audit-level=high` locally; the pipeline will block on advisories.

## Repository Structure

```
backend/
  voice_service/     FastAPI + websockets on ECS Fargate (ports/adapters)
    app/             config, exceptions, logging, auth, domain, ports,
                     orchestration, protocol, adapters, main
    tests/           unit + hypothesis property suites, in-memory fakes
    Dockerfile       two-stage build; apt-get upgrade in both stages
  notifier/          Notifier Lambda (EventBridge -> AppSync + Web Push + SNS)
    src/             normalizer (pure) + channel/repository adapters
  shared/            exceptions, structured logging, bounded retry
frontend/
  public/            static shell (index.html, sw.js)
  src/               audio, auth, events, push, ui, ws, capability, main
  tests/             vitest unit + fast-check property suites
infrastructure/
  bootstrap/         Layer 1: pipelines, source buckets, ECR, state backend, VAPID
  app/               Layer 2: network, alb, ecs_service, cognito, dynamodb,
                     appsync_events, bedrock_guardrail, waf, cloudfront_s3,
                     notifications, observability, iam, s3_policies,
                     devops_agent_access
  tests/             Terraform plan-assertion and negative-validation suites
ci/
  frontend|backend|iac/   Per-pipeline buildspecs: scan, test, build, deploy
scripts/
  deploy.sh          Idempotent end-to-end orchestrator
  push-source.sh     Pipeline trigger helper
  smoke/             Post-deploy smoke checks (run-all.sh + checks 01-05)
```

Be mindful of which layer you're in: **backend** is Python 3.14 async, **frontend** is vanilla ES modules with Bootstrap 5 (no framework), and **infrastructure** is Terraform across two layers.

## Code of Conduct

This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct). For questions, contact opensource-codeofconduct@amazon.com.

## Security Issue Notifications

If you discover a potential security issue, notify AWS/Amazon Security via the [vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/). Do **not** open a public issue. See [SECURITY.md](SECURITY.md).

## Licensing

See [LICENSE](LICENSE) for distribution terms. We may ask you to confirm the licensing of your contribution.

---
&copy; Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
