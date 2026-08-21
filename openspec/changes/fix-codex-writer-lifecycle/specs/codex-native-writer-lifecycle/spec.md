## Purpose

Define fail-closed ownership and cleanup for host-spawned Codex-native app-server processes so runner shutdown, terminal loss, crashes, and failed resume attempts cannot retain a thread writer or accumulate orphan processes.

## ADDED Requirements

### Requirement: Runner-owned Codex app-servers are reclaimed
**ID:** REQ-CWL-001
**Status:** accepted
**Source:** user:USER-001
The runner SHALL close and unregister every Codex-native app-server it owns when the corresponding terminal disappears or the runner shuts down gracefully, and a later runner SHALL reconcile app-server registry entries left by a dead owner.

#### Scenario: Graceful runner shutdown
- **WHEN** a runner that owns one or more Codex-native app-servers shuts down through its lifecycle handler
- **THEN** every owned app-server is closed and removed from the in-process registry before runner shutdown completes

#### Scenario: Terminal pane disappears
- **WHEN** the native terminal is reaped for idleness or exits unexpectedly
- **THEN** the corresponding forwarder and Codex app-server are torn down without waiting for session deletion

#### Scenario: Prior runner died without cleanup
- **WHEN** a runner starts and the crash-safe process registry contains an app-server whose owner is no longer alive
- **THEN** the stale app-server is reconciled and terminated

### Requirement: Failed Codex terminal startup leaves no app-server
**ID:** REQ-CWL-002
**Status:** accepted
**Source:** user:USER-001
After a Codex app-server has started but before its background forwarder owns teardown, the runner MUST close the event client when present, close the app-server, and remove its session registration if resume preload or auxiliary terminal launch fails.

#### Scenario: Resume preload reports an active writer
- **WHEN** `preload_codex_thread_for_resume` rejects a resume with `already has an active writer`
- **THEN** the newly started app-server is closed and the session is absent from the app-server registry before the error is returned

#### Scenario: Terminal registration fails after preload
- **WHEN** auxiliary terminal creation raises after the app-server was registered
- **THEN** the event client and app-server are closed and the registration is removed before the error is returned

#### Scenario: User retries a failed terminal launch
- **WHEN** terminal creation is retried after any failed-start scenario
- **THEN** no app-server process from the prior failed attempt remains to accumulate alongside the retry

### Requirement: Reconciliation preserves live owners and unrelated sessions
**ID:** REQ-CWL-003
**Status:** accepted
**Source:** user:USER-001
Lifecycle cleanup MUST be scoped to the target session or to registry entries whose owner is confirmed dead, and MUST NOT terminate an app-server owned by a live sibling runner or an unrelated session.

#### Scenario: Live sibling runner owns a registry entry
- **WHEN** crash reconciliation inspects an app-server entry whose owner lock is still held by a live runner
- **THEN** that app-server is left running

#### Scenario: Per-session teardown is repeated
- **WHEN** teardown is called again after the target session has already been cleaned up
- **THEN** the operation is a no-op and does not alter other session registrations
