import asyncio
import queue
import threading
import inspect
import math
import time
from typing import Callable, Optional, TypeVar, ParamSpec

P = ParamSpec("P")
R = TypeVar("R")


class Scheduler:
    """Run sync callables in background threads and await their results in asyncio.

    A fixed number of daemon worker threads consume jobs from internal queues,
    execute them, and complete asyncio futures on the event loop thread.
    """

    def __init__(self, workers: int = 4, interval: Optional[float] = None):
        """Create a scheduler and start worker threads.

        Args:
            workers: Number of background worker threads.
            interval: Time interval between job executions.
        """
        self._queue_count = max(1, math.floor(workers * 2 / 3))
        self._queues = [queue.Queue() for _ in range(self._queue_count)]
        self._submit_queue_index = 0
        self._submit_lock = threading.Lock()
        self.interval = max(0.1, interval) if interval is not None else None

        for worker_index in range(workers):
            threading.Thread(
                target=self._worker,
                args=(worker_index,),
                daemon=True,
            ).start()

    def _worker(self, worker_index: int):
        """Continuously process queued jobs and resolve associated futures."""
        queue_index = worker_index % self._queue_count

        while True:
            item = None
            consumed_index = queue_index

            for offset in range(self._queue_count):
                idx = (queue_index + offset) % self._queue_count
                try:
                    item = self._queues[idx].get_nowait()
                    consumed_index = idx
                    break
                except queue.Empty:
                    continue

            if item is None:
                try:
                    item = self._queues[queue_index].get(timeout=0.1)
                    consumed_index = queue_index
                except queue.Empty:
                    queue_index = (queue_index + 1) % self._queue_count
                    continue

            fn, args, kwargs, future = item
            queue_index = (consumed_index + 1) % self._queue_count

            if future.cancelled():
                continue

            try:
                result = fn(*args, **kwargs)

            except BaseException as e:
                future.get_loop().call_soon_threadsafe(
                    self._safe_set_exception,
                    future,
                    e,
                )

            else:
                future.get_loop().call_soon_threadsafe(
                    self._safe_set_result,
                    future,
                    result,
                )
            finally:
                if self.interval is not None:
                    time.sleep(self.interval)

    @staticmethod
    def _safe_set_result(future, result):
        """Set a future result if the future is not already completed."""
        if not future.done():
            future.set_result(result)

    @staticmethod
    def _safe_set_exception(future, exc):
        """Set a future exception if the future is not already completed."""
        if not future.done():
            future.set_exception(exc)

    async def submit(
        self,
        fn: Callable[P, R],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        """Submit a callable to workers and asynchronously wait for its result.

        Args:
            fn: The synchronous callable to execute.
            *args: Positional arguments for ``fn``.
            **kwargs: Keyword arguments for ``fn``.

        Returns:
            The return value produced by ``fn``.

        Raises:
            Exception: Re-raises any exception thrown by ``fn``.
        """
        if inspect.iscoroutinefunction(fn):
            raise TypeError("Scheduler does not support coroutine functions")

        loop = asyncio.get_running_loop()

        future: asyncio.Future[R] = loop.create_future()

        with self._submit_lock:
            queue_index = self._submit_queue_index
            self._submit_queue_index = (self._submit_queue_index + 1) % self._queue_count

        self._queues[queue_index].put((fn, args, kwargs, future))

        return await future
