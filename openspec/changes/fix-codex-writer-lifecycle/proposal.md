## Why

The test host running Omnigent `v0.7.0` left Codex app-server processes alive after runner shutdown and after failed native terminal resume. Those processes retained the Codex thread writer and made later terminal launches fail with `thread ... already has an active writer` until the processes were manually terminated. The requested outcome is a permanent lifecycle fix rather than repeated operational cleanup.

## Evidence

- USER-001: The owner explicitly requested a permanent fix after the bounded writer cleanup restored the terminal and asked whether an existing internet/upstream solution could be reused.
- FACT-001: On the test host, runner `1930871` had logged idle shutdown but remained alive together with Codex app-server process group `1931047`; repeated failed terminal launches left six additional app-server groups for the same bridge session.
- FACT-002: Runner logs showed `preload_codex_thread_for_resume` raising `thread 01a020ea-602d-7770-84ac-fd08978dd17e already has an active writer`; in installed `v0.7.0`, this call occurs after the app-server is registered but before the existing terminal-launch cleanup block.
- FACT-003: Upstream commit `f68abff46392ca19ffea83095e600d1e351ad266` (PR `#3925`) adds per-session teardown, runner-wide graceful teardown, pane-exit cleanup, and boot-time crash reconciliation for Codex app-servers.
- FACT-004: Current upstream still calls `preload_codex_thread_for_resume` before the later terminal-launch `try/except`, so PR `#3925` does not close an app-server created by a failed resume preload.
- OBS-001: The local implementation base is tag `v0.7.0` at `35519fb04743f66b30cac8a40695d5d72fa163ea`, matching the installed host version. The working branch is `fix/codex-writer-lifecycle-v0.7` with no upstream configured.

## What Changes

- Backport upstream PR `#3925` lifecycle cleanup to the `v0.7.0` implementation base.
- Close and unregister a newly started Codex app-server when resume preload fails, before reporting terminal creation failure.
- Add regression coverage for runner shutdown, crash reconciliation, pane teardown, and failed resume preload so retries do not accumulate app-server processes.
- Keep host installation and restart outside this local change until a separate authorized host `GO`.

## Capabilities

### New Capabilities

- `codex-native-writer-lifecycle`: Codex-native app-server ownership, failed-start cleanup, graceful teardown, and crash reconciliation.

### Modified Capabilities


## Impact

- Production code: `omnigent/runner/native/orchestration.py`, `omnigent/runner/_entry.py`, runner terminal teardown wiring, and the existing Codex native process registry integration.
- Tests: focused runner lifecycle and process-registry tests.
- Dependencies and data: no new dependency, schema, public API, or persistent-data format.
- External effects: none during local implementation. Installing the result on the test host, restarting Omnigent, or creating/pushing a fork remains separately gated.

<!-- openspec-architecture-contract:v1 -->
## Architecture Impact

**Architecture impact:** material

The planned production diff is small, but it touches existing production modules over 1000 lines: `omnigent/runner/native/orchestration.py` is approximately 6567 lines and `omnigent/runner/_entry.py` approximately 1490 lines on the `v0.7.0` base. Component ownership and growth are therefore recorded in `design.md` and checked before implementation.

## UI Contract

**Mode:** none

## Decisions

No user-owned product or rollout decision is required for the local patch. The implementation base is the inspected host-compatible `v0.7.0` tag, and external rollout remains separately authorized.

## Open Questions

None for local implementation. Host rollout and remote repository publication are later external effects, not assumptions in this change.
