"""Cross-platform child-process spawning and tree teardown.

Historically omnigent terminated child agent processes by killing their POSIX
process group (``os.killpg``), which only works because children are spawned
with ``start_new_session=True`` so ``pid == pgid``. Neither process groups nor
``os.killpg`` exist on Windows, so this module centralizes the portable
equivalents:

* :func:`spawn_kwargs` — the ``Popen``/``create_subprocess_exec`` keyword args
  that put a child in its own group/session (so signals don't leak to the
  parent and the whole tree can be torn down).
* :func:`terminate_tree` / :func:`kill_tree` — recursively stop a process and
  all of its descendants, using the process-group fast path on POSIX and
  :mod:`psutil` walking on every platform.
* :func:`install_child_subreaper` — keep detached Linux descendants attached
  to the nearest owning supervisor so tree teardown can still find them.
* :func:`process_alive` — liveness check that doesn't rely on ``os.kill(pid, 0)``.

:mod:`psutil` is already a core dependency, so the descendant walk needs no new
package.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from typing import Protocol

import psutil

from omnigent._platform import IS_POSIX

logger = logging.getLogger(__name__)

# Resolved via getattr so this module type-checks and imports on Windows, where
# process groups and SIGKILL do not exist. None on non-POSIX hosts.
_killpg_fn = getattr(os, "killpg", None)
_getpgid_fn = getattr(os, "getpgid", None)
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def install_child_subreaper() -> bool:
    """Keep orphaned Linux descendants attached to this supervisor process.

    ``prctl(PR_SET_CHILD_SUBREAPER, 1)`` makes this process the reparenting
    target for descendants whose immediate parent exits. This lets a runner
    retain detached tool daemons in its process tree so normal tree teardown
    can terminate them. On unsupported platforms the call is a safe no-op.

    :returns: ``True`` if the subreaper bit was set, otherwise ``False``.
    """
    if sys.platform != "linux":
        return False
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        pr_set_child_subreaper = 36
        return libc.prctl(pr_set_child_subreaper, 1, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


class _ProcessLike(Protocol):
    """The subset of ``subprocess.Popen`` / ``asyncio.subprocess.Process`` used here."""

    @property
    def pid(self) -> int | None:
        pass

    @property
    def returncode(self) -> int | None:
        pass

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


def spawn_kwargs() -> dict[str, object]:
    """
    Keyword args that isolate a child process into its own group/session.

    On POSIX returns ``{"start_new_session": True}`` (new session, so the child
    becomes a process-group leader and ``os.killpg(pid, ...)`` reaps the whole
    tree). On Windows returns ``{"creationflags": CREATE_NEW_PROCESS_GROUP}``
    so the child is in its own Ctrl-C group and can be torn down independently
    of the parent console.

    Pass via ``**spawn_kwargs()`` to :class:`subprocess.Popen` or
    :func:`asyncio.create_subprocess_exec`.
    """
    if IS_POSIX:
        return {"start_new_session": True}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}


def _killpg(pid: int, sig: int) -> bool:
    """
    POSIX fast path: signal the child's whole process group.

    Refuses to signal our OWN process group. A child spawned without
    ``start_new_session`` never becomes a group leader, so ``getpgid(pid)``
    resolves to the group we *share* with our parent — pytest, the
    harness/runner supervisor, the CI job step. ``killpg`` on that group would
    take down this process and everything around it (observed in CI as a
    job-wide "runner received shutdown signal" cancelling e2e at ~96%). The old
    code passed ``pid`` itself as the pgid, which failed safe for a non-leader
    (no group is numbered ``pid`` → ``ProcessLookupError`` → caller falls back);
    resolving the real group removed that accidental safety. Returning False
    here makes :func:`terminate_tree` / :func:`kill_tree` fall back to the
    psutil per-descendant walk, which signals only the real target subtree.

    :returns: True if the group signal was delivered, False if process groups
        are unavailable (Windows), the lookup failed, or the target group is
        our own.
    """
    if not IS_POSIX or _killpg_fn is None or _getpgid_fn is None:
        return False
    try:
        target_pgid = _getpgid_fn(pid)
        if target_pgid == _getpgid_fn(0):
            return False
        _killpg_fn(target_pgid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _walk_descendants(pid: int) -> list[psutil.Process]:
    """Return the process plus all live descendants, innermost-last is not guaranteed."""
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return []
    procs = [root]
    with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
        procs.extend(root.children(recursive=True))
    return procs


def reap_exited_children(*, exclude_pids: set[int] | None = None) -> int:
    """Reap exited direct children except explicitly owned process handles.

    Intended for a Linux child-subreaper supervisor. ``WNOWAIT`` lets the
    caller leave an owned worker's status for its :class:`subprocess.Popen`
    while consuming only adopted orphan statuses.

    :param exclude_pids: Child PIDs whose exit status another owner must reap.
    :returns: Number of adopted child statuses consumed.
    """
    if sys.platform != "linux" or not hasattr(os, "waitid"):
        return 0
    excluded = exclude_pids or set()
    reaped = 0
    while True:
        try:
            info = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except (ChildProcessError, OSError):
            break
        if info is None:
            break
        pid = info.si_pid
        if pid in excluded:
            break
        try:
            os.waitpid(pid, 0)
            reaped += 1
        except ChildProcessError:
            break
    return reaped


def terminate_descendants(pid: int, *, grace: float = 0.0) -> None:
    """Terminate every live descendant of ``pid`` without signalling ``pid``."""
    try:
        descendants = psutil.Process(pid).children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return
    for proc in reversed(descendants):
        with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.terminate()
    if grace:
        survivors = _wait_alive(descendants, grace)
        for proc in survivors:
            with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                proc.kill()
        _wait_alive(survivors, min(grace, 1.0))


def terminate_tree(process: _ProcessLike | None, *, grace: float = 0.0) -> None:
    """
    Gracefully stop ``process`` and all of its descendants.

    Sends ``SIGTERM`` (POSIX) / ``terminate()`` (Windows ``TerminateProcess``)
    to a snapshot of the whole tree. On POSIX the process-group fast path is
    also used, while the explicit snapshot covers descendants that created
    their own sessions. When ``grace`` is positive, survivors are killed after
    the deadline. Already exited processes are no-ops. All "process gone / not
    permitted" errors are swallowed — teardown is best-effort.

    :param process: A ``Popen``/``asyncio`` process handle, or ``None``.
    :param grace: Optional seconds to wait for the tree to exit after signaling.
    """
    if process is None or process.returncode is not None:
        return
    pid = process.pid
    if pid is None:
        with suppress(Exception):
            process.terminate()
        return

    procs = _walk_descendants(pid)
    group_signalled = _killpg(pid, signal.SIGTERM)
    for proc in reversed(procs):
        if group_signalled and proc.pid == pid:
            continue
        with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.terminate()
    if not procs:
        with suppress(Exception):
            process.terminate()
    if grace:
        survivors = _wait_alive(procs, grace)
        for proc in survivors:
            with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                proc.kill()
        _wait_alive(survivors, min(grace, 1.0))


def kill_tree(process: _ProcessLike | None) -> None:
    """
    Forcibly kill ``process`` and all of its descendants.

    Like :func:`terminate_tree` but with ``SIGKILL`` (POSIX) /
    ``TerminateProcess`` (Windows). Use after a grace period when a graceful
    terminate did not take.
    """
    if process is None or process.returncode is not None:
        return
    pid = process.pid
    if pid is None:
        with suppress(Exception):
            process.kill()
        return

    if _killpg(pid, _SIGKILL):
        return

    procs = _walk_descendants(pid)
    for proc in procs:
        with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.kill()
    if not procs:
        with suppress(Exception):
            process.kill()


def _wait_alive(procs: list[psutil.Process], timeout: float) -> list[psutil.Process]:
    """Return tree members still alive after a bounded non-reaping wait."""
    deadline = time.monotonic() + timeout
    alive = _alive_processes(procs)
    while alive and time.monotonic() < deadline:
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        alive = _alive_processes(alive)
    return alive


def _alive_processes(procs: list[psutil.Process]) -> list[psutil.Process]:
    """Filter live non-zombie processes without consuming child exit status."""
    alive: list[psutil.Process] = []
    for proc in procs:
        try:
            if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                alive.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return alive


def process_alive(pid: int) -> bool:
    """
    Whether ``pid`` names a live, non-zombie process.

    Cross-platform replacement for the ``os.kill(pid, 0)`` liveness probe (which
    behaves differently on Windows). A zombie/defunct process counts as not
    alive — it has exited and is only awaiting reaping.

    :param pid: The process id to probe.
    :returns: True if the process exists and has not exited.
    """
    if pid <= 0:
        return False
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        # Includes psutil.ZombieProcess (a NoSuchProcess subclass).
        return False
    except psutil.AccessDenied:
        # Exists but belongs to another user / can't introspect -> alive.
        return True
