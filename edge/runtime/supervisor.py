"""
edge/runtime/supervisor.py — Managed cart runtime lifecycle supervisor.

NOTE: get_session_meta() returns the 6-column billing dict (without
checkout_started_at).  We call billing.get_session_meta() for session
existence/end_time checks, and a separate targeted SELECT for
checkout_started_at when we need it.

Responsibilities
----------------
- Poll SQLite for an eligible active session for the configured cart_id.
- Create and attach a fresh CartRuntime instance when a session is discovered.
- Monitor the runtime for session transitions:
    - Checkout freeze  → stop/drain runtime; wait for cancellation or payment.
    - Checkout cancel  → discard stale runtime; create fresh one.
    - Payment complete → release runtime; wait for next customer.
    - Runtime failure  → stop; wait before retrying (does not restart in a tight loop).
- When a completed session is detected, wait for a NEW session (next customer).
- Never create two simultaneous runtimes for the same cart.

Non-responsibilities
--------------------
- Does NOT perform payment verification.
- Does NOT bypass checkout/generation fencing.
- Does NOT directly mutate billing tables.
- Does NOT expose HTTP endpoints.
- Does NOT auto-restart the process after ShutdownError.

State machine
-------------
IDLE       → no active session found; supervisor waits and polls.
RUNNING    → runtime is active; events are flowing.
DRAINING   → checkout frozen; runtime stopped, waiting for cancel/complete.
COMPLETED  → session end_time is set; waiting for next customer.

Safety
------
- Uses the existing cart_lock from run_cart.py for exclusive runtime ownership.
- Uses the authoritative get_active_session_for_cart() read for every poll.
- Stale-generation protection is guaranteed by CartSession's hydrated generation
  passed into add_runtime_item / remove_runtime_item through billing.
- Supervisor shutdown is deterministic: it sets a stop event and joins threads.
- ShutdownError (incomplete cleanup) quarantines the lock and stops the supervisor.

WARNING: This supervisor is for DEVELOPMENT/DEMO use in this milestone.
Physical camera, motion, and weight factories must be supplied by the caller.
"""
from __future__ import annotations

import logging
import time
from threading import Event, Thread
from typing import Callable

from edge.pairing.session_store import get_active_session_for_cart
from edge.database.database import get_connection
from edge.billing.billing import get_session_meta
from edge.runtime.diagnostics import log_exception

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifecycle state names (for logging only — not an enum to avoid coupling)
# ---------------------------------------------------------------------------
_IDLE      = 'IDLE'
_RUNNING   = 'RUNNING'
_DRAINING  = 'DRAINING'
_COMPLETED = 'COMPLETED'


class SupervisorError(RuntimeError):
    """Non-recoverable supervisor error; exit this process."""


class CartSupervisor:
    """
    Managed lifecycle supervisor for one physical cart.

    Parameters
    ----------
    cart_id:
        Physical cart identifier (e.g. 'CART-001').
    runtime_factory:
        Callable ``(session_id: int, checkout_generation: int) -> CartRuntime``.
        Must create a fully configured CartRuntime ready to call ``.run()``.
        The factory is responsible for constructing the camera, motion,
        weight source, detector, and CartSession appropriate for the session.
        It is called once per lifecycle cycle (per customer).
    poll_interval:
        Seconds between session-discovery polls when idle. Default 2.0.
    drain_poll_interval:
        Seconds between polls while draining (waiting for cancel/complete).
        Default 1.0.
    stop_event:
        Optional external Event to signal supervisor shutdown. When set, the
        supervisor stops cleanly after the current polling iteration.
    """

    def __init__(
        self,
        cart_id: str,
        runtime_factory: Callable,
        *,
        poll_interval: float = 2.0,
        drain_poll_interval: float = 1.0,
        stop_event: Event | None = None,
        use_cart_lock: bool = False,
    ) -> None:
        if not isinstance(cart_id, str) or not cart_id.strip():
            raise ValueError('cart_id must be a non-empty string')
        self.cart_id = cart_id.strip()
        self.runtime_factory = runtime_factory
        self.poll_interval = poll_interval
        self.drain_poll_interval = drain_poll_interval
        self._stop = stop_event if stop_event is not None else Event()
        self.use_cart_lock = use_cart_lock
        self._state = _IDLE
        self._active_session_id: int | None = None

    # ------------------------------------------------------------------
    # Public control
    # ------------------------------------------------------------------

    def request_stop(self) -> None:
        """Signal the supervisor to stop after the current poll cycle."""
        logger.info('Supervisor.request_stop: stop requested for cart_id=%r', self.cart_id)
        self._stop.set()

    @property
    def state(self) -> str:
        """Current supervisor state (for tests and diagnostics)."""
        return self._state

    def tick(self) -> None:
        """Execute one lifecycle poll cycle (for tests and step-by-step driving)."""
        self._tick()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run_forever(self) -> None:
        """
        Block until stop is requested or a non-recoverable error occurs.

        This method is intended to be run in a dedicated thread or process.
        It does NOT catch SupervisorError; callers should treat that as fatal.
        """
        if self.use_cart_lock:
            from edge.runtime.run_cart import cart_lock
            with cart_lock(self.cart_id):
                self._run_loop()
        else:
            self._run_loop()

    def _run_loop(self) -> None:
        logger.info('Supervisor started for cart_id=%r', self.cart_id)
        try:
            while not self._stop.is_set():
                try:
                    self._tick()
                except SupervisorError:
                    raise
                except Exception as error:
                    log_exception(logger,
                                  'Supervisor tick failed; backing off before retry.', error)
                    # Back off to avoid a tight error loop; respect stop signal.
                    self._stop.wait(self.poll_interval * 3)
        finally:
            self._state = _IDLE
            logger.info('Supervisor stopped for cart_id=%r', self.cart_id)

    # ------------------------------------------------------------------
    # Internal tick
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """One supervisor iteration: discover session → run → drain → release."""
        meta = self._discover_session()

        if meta is None:
            # No eligible session — stay IDLE, wait for next poll.
            if self._state != _IDLE:
                logger.info('Supervisor: cart_id=%r transitioned to IDLE (no active session)',
                            self.cart_id)
            self._state = _IDLE
            self._active_session_id = None
            self._stop.wait(self.poll_interval)
            return

        session_id = meta['session_id']

        # If this session is already completed (end_time set) → next customer.
        if meta['end_time'] is not None:
            if self._state != _COMPLETED:
                logger.info(
                    'Supervisor: cart_id=%r session_id=%d is completed (end_time=%s); '
                    'waiting for next customer.',
                    self.cart_id, session_id, meta['end_time'],
                )
            self._state = _COMPLETED
            self._active_session_id = None
            self._stop.wait(self.poll_interval)
            return

        # If this session is currently frozen in checkout, wait in drain mode.
        csa = self._get_checkout_started_at(session_id)
        if csa is not None:
            if self._state != _DRAINING:
                logger.info(
                    'Supervisor: cart_id=%r session_id=%d is frozen in checkout (checkout_started_at=%s); '
                    'waiting for payment completion or cancellation.',
                    self.cart_id, session_id, csa,
                )
            self._drain_until_next(session_id)
            return

        # Active session found — attach a runtime and drive it.
        self._state = _RUNNING
        self._active_session_id = session_id
        logger.info(
            'Supervisor: cart_id=%r discovered active session_id=%d; attaching runtime.',
            self.cart_id, session_id,
        )

        try:
            self._run_session_lifecycle(session_id)
        except SupervisorError:
            raise
        except Exception as error:
            log_exception(logger, 'Supervisor: runtime lifecycle failed.', error)
            # Back off before next attempt.
            self._stop.wait(self.poll_interval * 2)
        finally:
            self._active_session_id = None

    # ------------------------------------------------------------------
    # Session lifecycle driver
    # ------------------------------------------------------------------

    def _run_session_lifecycle(self, session_id: int) -> None:
        """
        Attach a runtime to session_id, run it, then drain/release cleanly.

        This method blocks until the session is completed or the stop signal
        is received.  It does NOT return until the runtime thread has finished.
        """
        from edge.runtime.cart_runtime import CartRuntime, ShutdownError

        runtime = self.runtime_factory(session_id)
        if not hasattr(runtime, 'run') or not hasattr(runtime, 'stop') or not hasattr(runtime, 'stopping'):
            raise SupervisorError('runtime_factory must return a CartRuntime or compatible instance')

        errors: list[BaseException] = []
        run_thread = Thread(
            target=self._run_runtime_thread,
            args=(runtime, errors),
            name=f'supervisor-runtime-{self.cart_id}',
            daemon=True,
        )
        run_thread.start()

        logger.info('Supervisor: runtime thread started for session_id=%d', session_id)

        shutdown_timeout = getattr(runtime, 'shutdown_timeout', 5.0)
        try:
            self._monitor_until_stopped(runtime, session_id, run_thread)
        finally:
            # Ensure the runtime is stopped even if monitoring raises.
            if not runtime.stopping.is_set():
                runtime.stop()
            run_thread.join(timeout=max(shutdown_timeout + 2.0, 10.0))
            if run_thread.is_alive():
                logger.error(
                    'Supervisor: runtime thread for session_id=%d did not stop within timeout; '
                    'supervisor will not restart this session. Operator action required.',
                    session_id,
                )
                raise SupervisorError(
                    f'Runtime thread for session_id={session_id} did not join; '
                    'exit process and reconcile.'
                )

        # Propagate ShutdownError as SupervisorError (quarantine this supervisor).
        for error in errors:
            if isinstance(error, ShutdownError):
                raise SupervisorError(
                    f'Runtime shutdown incomplete for session_id={session_id}: {error}'
                ) from error
            if not isinstance(error, (KeyboardInterrupt, SystemExit)):
                log_exception(logger, 'Supervisor: runtime thread raised an error.', error)

        logger.info('Supervisor: runtime released for session_id=%d', session_id)

    @staticmethod
    def _run_runtime_thread(runtime, errors: list) -> None:
        """Target function for the runtime thread."""
        from edge.runtime.cart_runtime import ShutdownError
        try:
            runtime.run()
        except ShutdownError as error:
            errors.append(error)
        except Exception as error:
            errors.append(error)

    def _monitor_until_stopped(self, runtime, session_id: int, run_thread: Thread) -> None:
        """
        Poll session state and signal the runtime to stop at the right time.

        Transitions handled:
        - checkout_started_at becomes NOT NULL → runtime should stop (frozen);
          after stopping, we enter drain mode.
        - end_time becomes NOT NULL → runtime should stop (completed).
        - runtime.stopping is set (runtime stopped itself) → log and return.
        - stop signal received → request runtime stop and return.
        """
        self._state = _RUNNING
        logger.info('Supervisor: monitoring session_id=%d for state transitions', session_id)

        while not self._stop.is_set():
            # Check if the runtime stopped itself.
            if not run_thread.is_alive() or runtime.stopping.is_set():
                logger.info(
                    'Supervisor: runtime for session_id=%d stopped (failed=%s).',
                    session_id, runtime.failed.is_set(),
                )
                # Enter drain mode to wait for payment or cancellation.
                self._drain_until_next(session_id)
                return

            # Read the authoritative session state.
            meta = get_session_meta(session_id)
            if meta is None:
                logger.error(
                    'Supervisor: session_id=%d disappeared from SQLite; stopping runtime.',
                    session_id,
                )
                runtime.stop()
                return

            if meta['end_time'] is not None:
                # Payment completed or session was administratively closed.
                logger.info(
                    'Supervisor: session_id=%d end_time=%s detected; stopping runtime.',
                    session_id, meta['end_time'],
                )
                self._state = _COMPLETED
                runtime.stop()
                return

            # Check for checkout freeze (checkout_started_at IS NOT NULL).
            # The runtime's own billing transactions will reject mutations, but we
            # also stop the runtime cleanly here to release camera/resources.
            # Check for checkout freeze — read the extra column separately.
            csa = self._get_checkout_started_at(session_id)
            if csa is not None and not runtime.stopping.is_set():
                logger.info(
                    'Supervisor: session_id=%d checkout frozen (checkout_started_at=%s); '
                    'stopping runtime.',
                    session_id, csa,
                )
                self._state = _DRAINING
                runtime.stop()
                # Wait for runtime thread to actually stop before drain polling.
                run_thread.join(timeout=runtime.shutdown_timeout + 2.0)
                self._drain_until_next(session_id)
                return

            self._stop.wait(self.drain_poll_interval)

        # Stop requested externally.
        if not runtime.stopping.is_set():
            logger.info(
                'Supervisor: stop requested; stopping runtime for session_id=%d.', session_id,
            )
            runtime.stop()

    def _drain_until_next(self, session_id: int) -> None:
        """
        Wait after a freeze/runtime-stop for payment completion or cancellation.

        - If payment completes (end_time set) → log and return (_tick will see COMPLETED).
        - If checkout is cancelled (checkout_started_at becomes NULL again) →
          log 'cancelled'; return so _tick creates a fresh runtime.
        - If stop signal is received → return.

        This method does NOT create a new runtime — that happens in _tick.
        """
        self._state = _DRAINING
        logger.info(
            'Supervisor: DRAINING session_id=%d; waiting for payment completion or cancellation.',
            session_id,
        )

        while not self._stop.is_set():
            meta = get_session_meta(session_id)
            if meta is None:
                logger.error(
                    'Supervisor: session_id=%d disappeared during drain; returning to poll.',
                    session_id,
                )
                self._state = _IDLE
                return

            if meta['end_time'] is not None:
                logger.info(
                    'Supervisor: session_id=%d payment completed (end_time=%s).',
                    session_id, meta['end_time'],
                )
                self._state = _COMPLETED
                return

            if self._get_checkout_started_at(session_id) is None:
                logger.info(
                    'Supervisor: session_id=%d checkout cancelled; ready for fresh runtime.',
                    session_id,
                )
                self._state = _IDLE
                return

            self._stop.wait(self.drain_poll_interval)

    # ------------------------------------------------------------------
    # Session discovery
    # ------------------------------------------------------------------

    def _discover_session(self):
        """
        Return the single active (end_time IS NULL) session meta for this cart,
        or None if there is none.

        Uses the authoritative SQLite helper. Multiple active sessions raise
        IntegrityConflict; this propagates to the caller's error handler.
        """
        return get_active_session_for_cart(self.cart_id)

    @staticmethod
    def _get_checkout_started_at(session_id: int):
        """Targeted read of sessions.checkout_started_at for one session_id."""
        conn = get_connection()
        try:
            row = conn.execute(
                'SELECT checkout_started_at FROM sessions WHERE session_id=?',
                (session_id,),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
