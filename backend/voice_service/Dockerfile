# syntax=docker/dockerfile:1

# =============================================================================
# Voice_Service image — Nova Sonic Support Portal (Req 10.1, 10.5)
# =============================================================================
#
# BUILD-CONTEXT CONTRACT
#   The build context is backend/ (this file's parent directory), NOT
#   backend/voice_service/. The service imports two sibling top-level
#   packages — `app` (backend/voice_service/app) and `shared`
#   (backend/shared) — so both must be inside the context. The backend
#   pipeline therefore builds this image from backend/ with:
#
#       docker build -f voice_service/Dockerfile .
#
# IMAGE SHAPE
#   Multi-stage: the builder stage resolves third-party dependencies into a
#   self-contained virtualenv; the runtime stage copies that venv plus the
#   first-party sources (/srv/app, /srv/shared) onto a fresh
#   python:3.14-slim base and runs as a non-root user. The image deploys to
#   ECS Fargate behind an ALB (Req 10.1).

# ---------------------------------------------------------------------------
# Stage 1 — builder: resolve third-party dependencies into /opt/venv
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS builder

# Outstanding Debian security updates (see the runtime stage for the
# rationale). Applied here too so any native wheel resolved below is built
# and linked against the patched system libraries rather than the base
# image's older ones.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# All installs land in a self-contained venv that the runtime stage copies
# wholesale, leaving pip caches and build tooling behind in this stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# Hatchling needs the package sources present to build the voice-service
# wheel, so app/ is copied alongside pyproject.toml before the install
# (this caps layer-cache granularity: an app/ change re-runs pip).
COPY voice_service/pyproject.toml voice_service/pyproject.toml
COPY voice_service/app voice_service/app

# Install the package to resolve ALL third-party dependencies (fastapi,
# uvicorn[standard], websockets, aioboto3, httpx, aws-sdk-bedrock-runtime,
# PyJWT[crypto]), then uninstall the first-party `voice-service`
# distribution itself: the runtime imports first-party code exclusively
# from /srv (PYTHONPATH below), so removing the site-packages copy keeps
# /srv the single import source and rules out stale-duplicate shadowing.
#
# No compiler toolchain is installed: the native dependencies (awscrt for
# the Bedrock bidirectional stream, cryptography for PyJWT[crypto]) ship
# manylinux binary wheels. Should pip ever need to compile one of them on
# Python 3.14 (a wheel gap for a new interpreter), that surfaces as a
# pipeline-time build failure to address then — not a concern baked here.
RUN pip install --no-cache-dir ./voice_service \
    && pip uninstall -y voice-service

# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:3.14-slim

# Apply the base image's outstanding Debian security updates (Req 12.3).
#
# The python:3.14-slim tag moves, but it lags the distro security archive:
# a container scan of the deployed image reported 26 advisories across
# perl, glibc, pcre2, sqlite3, and gzip — 6 Critical and 12 High among
# them — and every fix was an already-published Debian point release
# (for example glibc 2.41-12+deb13u3 -> +deb13u4). Upgrading at build time
# picks those up on every pipeline run instead of waiting for a base-image
# respin, so a rebuild is always a remediation.
#
# `upgrade` (not `dist-upgrade`) never adds or removes packages, and
# --no-install-recommends keeps recommended extras out of the image. The
# apt lists are deleted in the same layer so they never ship.
#
# Then remove perl, which no longer has any reason to be here. The base
# image carries `perl-base` (743 files, a complete script interpreter) and
# it is the flagged package for CVE-2026-82560 (Pod::Text), for which
# Debian publishes no fix. Removal — not suppression — is the remediation:
#
#   * Nothing needs it. No installed package Depends on perl-base
#     (verified with `awk` over /var/lib/dpkg/status in the built image),
#     the service shells out nowhere, and `useradd` below is a C binary
#     from `shadow`, not the perl `adduser`.
#   * The vulnerable module is not even shipped: `perl -MPod::Text -e1`
#     in the base image fails with "Can't locate Pod/Text.pm in @INC", so
#     the finding is source-package attribution against perl-base rather
#     than reachable code. Removing the package removes the attribution
#     and 743 files of unused interpreter surface with it.
#
# perl-base is Essential, so dpkg needs --force-remove-essential; this runs
# in the same layer AFTER all apt work, so no later apt or maintainer
# script can need the interpreter. Keep it last in this RUN.
#
# One finding still survives, CVE-2026-85091 in zlib: libz is linked by
# CPython itself and so cannot be removed, Debian ships no fixed version in
# any suite, and the installed upstream 1.3.1 sits below the advisory's
# stated affected range. The evidence is recorded in SECURITY_EXCEPTIONS.md
# at the repository root — read it before attempting a pin or source build,
# which would put a hand-built libz in the runtime path of every request.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && dpkg --purge --force-remove-essential --force-depends perl-base \
    && rm -rf /usr/share/perl /usr/share/perl5 /usr/lib/*/perl /usr/lib/*/perl-base /usr/lib/*/perl5

# Non-root service account: fixed high UID, no home directory, no login
# shell. Everything under /srv and /opt/venv stays root-owned and
# read-only to this user by convention.
RUN useradd -r -u 10001 --no-create-home -s /usr/sbin/nologin appuser

# Third-party dependencies from the builder venv (same absolute path, same
# base image, so the venv's interpreter symlinks stay valid)...
COPY --from=builder /opt/venv /opt/venv

# ...and the first-party sources: /srv holds exactly the two top-level
# packages the service imports (`shared` and `app`).
COPY shared/ /srv/shared/
COPY voice_service/app/ /srv/app/

# Vendored botocore service models (see voice_service/botocore_data/
# README.md): the aioboto3 pin resolves a botocore older than the
# devops-agent service model, so the model is shipped alongside the app
# and surfaced through AWS_DATA_PATH below. Without it,
# boto3.client("devops-agent") raises UnknownServiceError and every
# ask_devops_agent tool call fails.
COPY voice_service/botocore_data/ /srv/botocore_data/

# PATH: venv binaries (uvicorn) win. PYTHONPATH=/srv makes `app` and
# `shared` importable — the single first-party import source.
# AWS_DATA_PATH: botocore searches these directories BEFORE its bundled
# data, making the vendored devops-agent model loadable.
# PYTHONUNBUFFERED: structured JSON log lines reach the container log
# driver immediately. PYTHONDONTWRITEBYTECODE: no .pyc writes into the
# root-owned /srv tree.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/srv \
    AWS_DATA_PATH=/srv/botocore_data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER appuser

WORKDIR /srv

# The ALB target port (Req 10.1). EXPOSE is documentation; the ECS task
# definition and target group own the actual port mapping.
EXPOSE 8080

# No container HEALTHCHECK on purpose: health is owned by the ALB target
# group probing /healthz (the drain and protection managers flip /healthz
# to 503 per the design). A Docker-level HEALTHCHECK would be a second,
# unconsulted health authority — Fargate only acts on it if the task
# definition opts in, and the design routes on ALB health alone.

# Exec-form CMD: uvicorn IS PID 1 and receives SIGTERM directly from ECS —
# no shell wrapper to swallow or delay the signal. On SIGTERM the app's
# drain manager (app/orchestration/drain_manager.py) rejects new WebSocket
# upgrades, flips /healthz to 503, and drains live sessions for up to
# 120 s, sending session.terminating before any forced close (Req 10.5).
# --timeout-graceful-shutdown 130 gives uvicorn a shutdown budget that
# sits ABOVE that 120 s application drain, so uvicorn never force-closes
# connections the drain manager is still finishing; the hard stop is ECS
# stopTimeout=120 (SIGKILL), per the design's termination ordering.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-graceful-shutdown", "130"]
