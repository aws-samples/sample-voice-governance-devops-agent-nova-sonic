#!/usr/bin/env bash
# scripts/push-source.sh — pipeline trigger helper (Req 16.1, 16.2).
#
# Packages the repository into source.zip and uploads it to the given
# pipeline's source bucket. The bootstrap layer wires an EventBridge rule
# per source bucket, so the upload IS the "commit push" event: a new
# version of source.zip starts exactly the matching pipeline
# (infrastructure/bootstrap/modules/source_buckets).
#
# Usage:
#   scripts/push-source.sh <frontend|backend|iac> <source-bucket-name>
#
#   <frontend|backend|iac>  which pipeline's source is being pushed; used
#                           for validation and messaging (the buckets are
#                           per-pipeline — pass the matching bucket name,
#                           from `terraform output -json source_bucket_names`
#                           in infrastructure/bootstrap)
#   <source-bucket-name>    the S3 source bucket created by the bootstrap
#                           layer for that pipeline
#
# The WHOLE repository is archived for every pipeline: buildspecs resolve
# repo-root-relative paths (backend/, frontend/, infrastructure/, ci/), and
# the object key is source.zip — the bootstrap pipeline module's
# source_object_key default. Override the key via SOURCE_OBJECT_KEY only if
# the bootstrap layer was applied with a non-default key.
#
# Archive strategy:
#   - Inside a git work tree: `git archive HEAD` — exactly the committed
#     tree, so .gitignore'd caches and local junk never ship. Uncommitted
#     changes are NOT included; the script warns when the tree is dirty.
#   - Otherwise: `zip -r` of the working tree with an exclusion list
#     mirroring what a .gitignore would keep out (tool caches, venvs,
#     node_modules, build output, prior archives).

set -euo pipefail

usage() {
  echo "usage: $0 <frontend|backend|iac> <source-bucket-name>" >&2
  echo "  packages the repository as source.zip and uploads it to" >&2
  echo "  s3://<source-bucket-name>/\${SOURCE_OBJECT_KEY:-source.zip}," >&2
  echo "  which starts the matching CodePipeline (Req 16.2)." >&2
}

if [ "$#" -ne 2 ]; then
  usage
  exit 2
fi

pipeline="$1"
bucket="$2"
object_key="${SOURCE_OBJECT_KEY:-source.zip}"

case "$pipeline" in
  frontend|backend|iac) ;;
  *)
    echo "ERROR: unknown pipeline '$pipeline' (expected frontend, backend, or iac)" >&2
    usage
    exit 2
    ;;
esac

if [ -z "$bucket" ]; then
  echo "ERROR: source bucket name must not be empty" >&2
  usage
  exit 2
fi

# Run from the repository root so both archive strategies capture the
# repo-root-relative layout the buildspecs expect.
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT
archive="$workdir/source.zip"

if git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  # Committed tree only — warn when local changes would be left behind.
  if [ -n "$(git status --porcelain)" ]; then
    echo "WARNING: working tree has uncommitted changes; the archive contains HEAD only." >&2
  fi
  git archive --format=zip -o "$archive" HEAD
  echo "Archived committed tree (git archive HEAD)."
else
  # No git metadata: zip the working tree, excluding what a .gitignore
  # would keep out of the committed tree.
  zip -qr "$archive" . \
    -x '.git/*' \
    -x '*/node_modules/*' -x 'node_modules/*' \
    -x '*/.venv/*' -x '.venv/*' \
    -x '*/__pycache__/*' \
    -x '*/.mypy_cache/*' \
    -x '*/.ruff_cache/*' \
    -x '*/.pytest_cache/*' \
    -x '*/.hypothesis/*' \
    -x '*/.terraform/*' \
    -x 'dist/*' \
    -x '*.zip'
  echo "Archived working tree (zip fallback; not a git repository)."
fi

echo "Uploading to s3://$bucket/$object_key (pipeline: $pipeline)..."
aws s3 cp "$archive" "s3://$bucket/$object_key"
echo "Done. The $pipeline pipeline starts automatically on the new source version (Req 16.2)."
