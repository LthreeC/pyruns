# Test scope

Use the smallest test that protects a distinct behavior:

- Python tests cover backend and CLI contracts. Keep process tests for behavior
  that depends on real processes, locking, recovery, or platform differences.
- Frontend unit tests in `frontend/src` cover parsing, API calls, and store logic.
- Browser tests in `frontend/e2e` cover rendered controls, keyboard interaction,
  focus, scrolling, and responsive layout.

Extend an existing regression when it exercises the same contract. Parameterize
shared setup with distinct inputs; keep different failure boundaries explicit.
For a bug fix, check that the regression fails against the unfixed behavior.

Avoid assertions that merely repeat source code, variable names, exact colors,
decorative classes, or toolbar markup. Do not add tests for routine cosmetic edits.
Retire existing source assertions as behavior coverage becomes available; retain
independent integration or performance constraints until they have a replacement.

Keep temporary diagnostics and backup copies outside pytest's collection paths,
for example in `.tmp/`. Windows automation must keep spawned processes hidden.
Editor/sync copies named `test_*-TKB[0-9]*.py` are also excluded from automatic
collection as a safeguard; archive any such copies in `.tmp/`.
