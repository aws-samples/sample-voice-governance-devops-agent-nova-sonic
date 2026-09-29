# Container security exceptions

Findings from container vulnerability scanning (Amazon Inspector) on the
Voice_Service image, with the analysis behind each decision.

Everything fixable is fixed by the build itself:
`backend/voice_service/Dockerfile` runs `apt-get upgrade` in both stages, so
every pipeline run pulls the current Debian security archive. That closed 26
advisories across perl, glibc, pcre2, sqlite3, and gzip.

**One finding remains: CVE-2026-85091 in zlib.** It is documented below as an
accepted exception because no fixed package exists in any Debian suite and
`libz` cannot be removed. CVE-2026-82560 in perl is **no longer an
exception: it is fixed** by removing the package; see below.

Re-check before each release: if a tracker now shows a fixed version, no code
change is needed: rebuild the backend image and the finding closes.

---

## FIXED: CVE-2026-82560 (perl / Pod::Text), and four more with it

Previously carried here as an exception on the grounds that Debian publishes
no fix. That reasoning was incomplete: the package did not need to be
patched, it needed to be **gone**.

The runtime stage now purges it:

```dockerfile
&& dpkg --purge --force-remove-essential --force-depends perl-base \
&& rm -rf /usr/share/perl /usr/share/perl5 /usr/lib/*/perl /usr/lib/*/perl-base /usr/lib/*/perl5
```

**Why this is safe.** Verified against the built `amd64` image:

| Check | Result |
|---|---|
| Packages depending on `perl-base` | none (`awk` over `/var/lib/dpkg/status`) |
| `Pod/Text.pm` present | no: `perl -MPod::Text -e1` fails, "Can't locate Pod/Text.pm in @INC" |
| `pod2text` / `perldoc` present | no |
| Service shells out to perl | no: no `subprocess`, `os.system`, `os.popen`, `shutil.which` anywhere |
| `useradd` after the purge | works: it is a C binary from `shadow`, not the perl `adduser` |
| `import app.main` after the purge | identical to the unmodified image (same `ConfigurationError` on the missing `AWS_REGION`) |
| perl files remaining | 0 |

Because the vulnerable module was never shipped in the first place, the
finding was source-package attribution against `perl-base` (built from the
`perl` source package) rather than reachable code. Removing the package
removes the attribution **and** 743 files of unused script-interpreter
surface.

**Measured effect** (grype, `linux/amd64`, image before vs after): total
matches fell from 152 to 147, and `perl-base` left the vulnerable-package
list entirely. Five findings closed, not one:

| CVE | Severity |
|---|---|
| CVE-2026-9538 | **High** |
| CVE-2026-15534 | Medium |
| CVE-2026-19487 | Medium |
| CVE-2026-82560 | Unknown (this one) |
| CVE-2011-4116 | Negligible |

The purge must stay last in that `RUN`, after all `apt` work, so no later
apt invocation or maintainer script can need the interpreter.

---

## ACCEPTED EXCEPTION: CVE-2026-85091 (zlib)

| | |
|---|---|
| Installed | `1:1.3.dfsg+really1.3.1-1+b1` (Debian 13 trixie, upstream **1.3.1**) |
| Debian status | bookworm, trixie, **and** forky/sid all marked vulnerable; unstable `(unfixed)`: re-verified 2026-09-29 on security-tracker.debian.org, Debian bug 1146895 |
| Fix available | **No.** Upstream has a commit (`df84af2`) but no release. grype reports `fix state: not-fixed`, `fix versions: []` |
| Severity | High |
| Assessment | Not reachable; installed version below the advisory's stated range |

**Why it cannot be fixed.** Three independent reasons:

1. **Nothing to upgrade to.** No Debian suite carries a fix: not bookworm,
   not trixie, not sid. Moving base-image suite does not help, because all
   three are flagged. `apt-get upgrade` has nothing to install.
2. **Cannot be removed.** CPython links `libz.so.1` for its `zlib` module
   (`zlib.ZLIB_RUNTIME_VERSION` reports `1.3.1` in the image). Unlike perl,
   this is load-bearing: removing it breaks the interpreter.
3. **Source build is worse than the finding.** Shipping the upstream commit
   would mean compiling zlib and making CPython link a hand-built library in
   the runtime path of every request. That trades a probably-inapplicable
   advisory for a real supply-chain and maintenance risk.

**Why the match is probably not even applicable.** The advisory states
versions **1.3.1.2 through 1.3.2** are affected. The installed upstream
version is **1.3.1**: below the range. Two corroborating signals:

- grype matched with `versionConstraint: "none (unknown)"`, i.e. it flagged
  the package with **no version comparison at all**, because the Debian
  entry carries no affected-range data. Any zlib version would match.
- Debian marks bookworm's `1.2.13` vulnerable too, which predates the range
  by even more. The tracker entry reads as "unresolved, pending analysis"
  rather than a version-verified hit.

**Why it is unreachable regardless.** The overflow is in `gz_vacate()`,
reached only through zlib's `gzFile` stdio-style API: a non-blocking
`gzwrite()` stall followed by `gzprintf()`/`gzvprintf()`. CPython's `zlib`
module does not expose that API: it binds the deflate/inflate interface,
and `gzip` is pure Python on top of it. The service also compresses nothing
of its own: no `import zlib`, no `import gzip`, no `gz*` call anywhere in
`backend/voice_service/app` or `backend/shared`.

**Closes when.** Debian publishes a fixed `zlib` in trixie (watch bug
1146895) and the backend pipeline rebuilds. No code change needed.

---

## Known gap: the pipeline does not scan the image it builds

The backend `SecurityScan` stage runs gitleaks, bandit, and pip-audit, none
of which inspect the built image's OS packages. That is why this class of
finding was discovered post-deploy by Inspector rather than at build time,
and why a High in `perl-base` sat in the image unnoticed.

Adding a container scan to the build stage (grype or trivy, against the
image it just pushed) would surface these before deployment. To be adoptable
it needs an allowlist for the accepted exception above, since the zlib
finding is High and unfixable, otherwise it blocks every build. Suggested
shape: fail on **fixable** Critical/High only, using grype's
`--only-fixed`, which would have caught the perl High while ignoring zlib.
