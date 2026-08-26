"""Event-consumer shutdown.

The consumer is a long-lived background task that spends nearly all its life
parked on `queue.get()`. Stopping it therefore depends entirely on cancellation
propagating out of that await, and the failure mode when it doesn't is one of
the nastiest kinds: nothing raises, nothing logs, and the only symptom is that
the process never exits. `uvicorn --reload` looks wedged, Ctrl-C appears
ignored, and `TestClient(app)` used as a context manager hangs on teardown
rather than failing.

The bug this pins was a `except (asyncio.TimeoutError, asyncio.CancelledError)`
in `_drain_once` that returned 0 for both. Catching a timeout there is correct -
an idle queue is not an error. Catching the cancellation swallowed the only
signal that ever stops the task: `_consumer` looped straight back into another
drain, its own `except CancelledError: break` never ran, and `stop_consumer`'s
`await _consumer_task` waited forever on a task that had quietly declined to
stop.

Both tests run against the real functions rather than a re-implementation,
because the whole point is that those two exception clauses stay
distinguishable.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services import events


def _run(coro):
    """Run `coro` on a throwaway loop that is closed, not drained.

    `asyncio.run` finishes by cancelling whatever is still pending and *awaiting*
    it - which a task that ignores cancellation blocks just as thoroughly as it
    blocks `stop_consumer`. Using it here would turn the exact failure this file
    exists to catch into a hung test session instead of a red one. Closing the
    loop outright abandons the zombie task and lets the assertions below be the
    thing that reports.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


async def _settle(coro, seconds: float = 5.0) -> asyncio.Task:
    """Give `coro` `seconds` to finish, then hand back its task either way.

    Deliberately `asyncio.wait` and not `asyncio.wait_for`. `wait_for` reacts to
    a timeout by cancelling, and `Task.cancel()` on a task that is parked on
    `await other_task` forwards the cancellation to `other_task` rather than
    interrupting the waiter - so when `other_task` is the un-cancellable consumer
    this file is about, the timeout fires and control still never comes back.
    `asyncio.wait` cancels nothing; it just stops waiting, which is all a test
    needs in order to report.
    """
    task = asyncio.ensure_future(coro)
    await asyncio.wait([task], timeout=seconds)
    return task


@pytest.fixture(autouse=True)
def isolated_queue(monkeypatch):
    """The queue and the consumer handle are module globals, and a queue built on
    a closed loop is unusable. Clearing both ends means each test starts on its
    own loop with its own queue."""
    monkeypatch.setattr(events, "_queue", None, raising=False)
    monkeypatch.setattr(events, "_consumer_task", None, raising=False)
    yield
    monkeypatch.setattr(events, "_queue", None, raising=False)
    monkeypatch.setattr(events, "_consumer_task", None, raising=False)


def test_the_consumer_can_actually_be_cancelled():
    """The assertion is simply that `stop_consumer` finishes. There is no clean
    way to observe "declined to stop" other than not finishing, so the deadline
    is the test."""

    async def scenario():
        await events.start_consumer()
        task = events._consumer_task
        assert task is not None
        # Let it reach the `queue.get()` await first; cancelling a task that has
        # not started yet would pass for the wrong reason.
        await asyncio.sleep(0.05)

        stopper = await _settle(events.stop_consumer())
        if not stopper.done():
            pytest.fail(
                "stop_consumer() never returned: the consumer task swallowed "
                "its cancellation, so shutdown can never complete"
            )
        stopper.result()  # surface anything it raised

        assert task.done(), "cancelled task never finished"
        assert events._consumer_task is None

    _run(scenario())


def test_an_idle_queue_is_not_treated_as_a_failure():
    """The other half of the clause that was too broad. `_drain_once` must still
    return 0 on an empty queue rather than propagating a timeout, or an idle
    consumer would log an exception every DRAIN_INTERVAL forever."""

    async def scenario():
        events.get_queue()  # empty
        # DRAIN_INTERVAL is not pinned here on purpose: this should also start
        # failing if that value ever grows to something a shutdown can't wait for.
        drain = await _settle(events._drain_once())
        assert drain.done(), "_drain_once() blocked on an empty queue"
        assert drain.result() == 0

    _run(scenario())
