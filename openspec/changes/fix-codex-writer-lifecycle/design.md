## Context

The host runs the `v0.7.0` codebase. Its Codex-native startup path creates an app-server in a new process group, stores it in `_AUTO_CODEX_APP_SERVERS`, and preloads an existing thread before entering the later auxiliary-terminal cleanup block. A failed preload therefore returns without closing or unregistering the new process. Separately, a runner can stop or lose its tmux pane without executing the per-session delete path, allowing the new-session app-server to outlive its owner.

Upstream commit `f68abff46392ca19ffea83095e600d1e351ad266` already defines the repository's intended ownership for pane teardown, graceful runner shutdown, and dead-owner reconciliation. The local change reuses that implementation on the matching `v0.7.0` base and adds the missing failed-preload cleanup at the same startup boundary.

## Goals / Non-Goals

Goals:

- Reuse upstream lifecycle ownership rather than create a parallel watchdog.
- Make every failure between app-server registration and forwarder ownership close and unregister the process.
- Preserve live sibling runner ownership during crash reconciliation.
- Prove the observed active-writer retry path with focused tests.

Non-goals:

- No generic process killer, cron job, or broad Omnigent service supervisor.
- No Codex writer-lock bypass or deletion of thread history/state.
- No runner/app.py decomposition or unrelated legacy cleanup.
- No host install, restart, fork creation, commit, or push in the local implementation phase.

## Decisions

The implementation follows the existing upstream ownership boundary:

- `omnigent.runner.native.orchestration` owns the in-process app-server and forwarder registries and exposes idempotent per-session and all-session teardown.
- Runner lifecycle and terminal/pane lifecycle code invoke those helpers but do not duplicate process-management rules.
- The existing crash-safe Codex process registry remains the hard-death authority; owner locks distinguish dead owners from live sibling runners.
- Failed resume preload uses the same cleanup invariant already applied to failed event-client connection and failed auxiliary-terminal launch. Cleanup occurs before re-raising the original exception.

No new dependency, daemon, configuration flag, or generic abstraction is introduced.

## Component Ownership

**Architecture impact:** material

**Inspected baseline:** `omnigent/runner/native/orchestration.py` is approximately 6567 lines; `omnigent/runner/_entry.py` approximately 1490 lines; `omnigent/runner/app.py` approximately 9880 lines; `omnigent/runner/native/__init__.py` approximately 219 lines; `omnigent/codex_native_process_registry.py` approximately 417 lines.

**Expected growth:** about 60 production lines from upstream teardown helpers and exports, about 20 lifecycle-hook lines, and fewer than 20 lines for failed-preload cleanup. No production file is expected to grow by 250 lines.

**Existing responsibilities:** `orchestration.py` owns native harness launch, per-session forwarders, Codex app-server registration, and Codex terminal creation; `_entry.py` owns runner startup/shutdown; `app.py` owns terminal API and idle/exit callbacks; `codex_native_process_registry.py` owns crash-safe PID/owner-lock reconciliation.

**New responsibilities:** no new independently testable subsystem. Existing lifecycle owners gain missing calls that close their already-owned Codex app-server resources. The failed-start path gains one cleanup branch before ownership transfers to the forwarder.

**Transaction owner:** no database transaction is involved. The active runner event loop owns in-memory registry mutation; the existing process registry and owner lock own cross-process crash reconciliation.

**Boundary options:** (1) keep process ownership in native orchestration and invoke it from existing lifecycle hooks; (2) introduce a new generic process supervisor; (3) add an external watchdog that scans and kills Codex processes. Option 1 matches upstream PR `#3925`, uses session identity and owner locks, and has the smallest blast radius. Options 2 and 3 add duplicate ownership or risk terminating live sessions.

**Decision:** keep-cohesive

**Known cost:** the large orchestration and app modules remain large, and the backport adds a few lifecycle calls to them. This is accepted as baseline compatibility, not an endorsement of their existing size.

**Ratchet scope:** change only Codex app-server teardown helpers, their existing runner/pane call sites, the failed-preload boundary, exports, and focused tests. Do not refactor adjacent runner APIs or native harnesses.

## Risks / Mitigations

- **Partial backport dependency:** PR `#3925` may rely on post-`v0.7.0` context. Apply it on the `v0.7.0` branch, inspect every conflict, and run its focused test module plus the new failed-preload regression.
- **Double cleanup:** forwarder cancellation and explicit app-server close can converge. Keep teardown idempotent, pop registries defensively, and suppress only cleanup errors while preserving the initiating exception.
- **Live sibling termination:** use the existing owner-lock reconciliation contract and its tests; do not add name-based or broad PID scanning.
- **Failed preload leak:** wrap the resume branch itself because the later auxiliary-terminal `try/except` cannot observe an exception raised before it.
- **Host rollout regression:** local verification cannot prove systemd/host integration. Require a separate host `GO`, preserve the current package as rollback, restart only the Omnigent host runtime, then reproduce one resume/retry path and inspect processes/ports.

## Migration And Rollback

There is no data migration. The local rollback is to revert the lifecycle patch on the `v0.7.0` branch. A later host rollout must retain the currently installed `v0.7.0` environment or package path so the runtime can be restored before restarting the service. Rollback must not delete Codex session history or private `CODEX_HOME` state.

## Open Questions

None for local implementation.
