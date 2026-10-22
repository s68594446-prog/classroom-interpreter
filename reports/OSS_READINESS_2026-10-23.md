# Open-source readiness maintenance — 2026-10-23

## Phase and scope

Maintenance of onboarding, licensing and offline verification. This is not a new
Phase 10 acceptance or independent Reviewer/Validator/Oracle sign-off.

## Changes and decisions

- `LICENSE`, `CONTRIBUTING.md`, `SECURITY.md`: MIT selected by the owner;
  contributor workflow, synthetic-data rules and private vulnerability reporting.
- `README.md`, `pyproject.toml`: replace obsolete Phase 1 onboarding with current
  installation steps and explicit historical/live verification limitations.
- `.env.example`: align the silence auto-pause example with the existing 600-second
  application default. The runtime implementation is unchanged.
- `.github/workflows/ci.yml`: macOS/Python 3.13/Node 24 offline tests and frontend
  type checking/build, with read-only repository permissions and no secrets.
- `tests/integration/test_end_class_robustness.py`: assert persisted first-error
  fields after drain. The prior assertion raced with background summary errors
  updating last-error fields. This retains the capture-error persistence check.

## Verification

Local macOS, Python 3.13, isolated virtual environment; all inputs synthetic:

- `npm run typecheck` in frontend: PASS.
- `npm run build` in frontend: PASS.
- `.venv/bin/python -m pytest -q tests/integration/test_end_class_robustness.py`:
  2 passed after correcting the assertion.
- `.venv/bin/python -m pytest -q` from repository root: all 376 passed, exit 0.
- Initial full run before the assertion correction: 375 passed, 1 failed due to
  last-error overwrite; retained here rather than presenting the first run as green.
- Changed/new text reviewed for credentials, personal paths and real classroom
  content; credential-pattern scan and `git diff --check`: PASS.

Evidence classification: local deterministic / simulated provider. Running on
macOS does not establish real microphone, device, permissions or provider acceptance.
GitHub CI is separately observable in Actions; this report does not claim a remote
run passed before its result exists.

## Invariants, unknowns and next phase

PERSIST FIRST, PROCESS SECOND and NO VALID SUMMARY = NO PURGE are unchanged.
No new classroom content, paid model call, microphone capture, production database
operation, or purge was used. Historical data was not rewritten.

Target-hardware three-hour operation, sleep/device recovery, current real-provider
quality, real-data cleanup and independent final acceptance remain unverified here.
Next: separately authorized hardware/provider acceptance with synthetic or explicitly
consented data, followed by independent final review. No phase promotion is claimed.

## Dependency security follow-up

GitHub reported eight open dependency alerts after private reporting and dependency
alerts were enabled. Updated Vite 8.2.2 → 8.2.4, source-map-js 1.2.1 → 1.2.2,
and pytest to >=9.0.3,<10 (uv resolves 9.1.1). Updated frontend/package.json,
frontend/package-lock.json, pyproject.toml and uv.lock. Regenerating uv.lock also
restored the already-declared websockets dependency missing from the old lock.
No new runtime dependency was added to pyproject.toml.

After these changes:
- `npm audit --json`: zero reported vulnerabilities.
- `npm run typecheck` and `npm run build`: PASS with Vite 8.2.4.
- `.venv/bin/python -m pytest -o addopts='' -q`: 376 passed in 145.32 seconds.
- `.venv/bin/python -m pip check`: no broken requirements.
- `git diff --check`: PASS; dependency diffs contain package metadata only.

These are local dependency and synthetic regression checks, not a guarantee that
all vulnerabilities are known or that production/hardware behavior is verified.
