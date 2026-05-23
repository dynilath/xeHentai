import asyncio
import queue
import threading
import inspect
import time
from typing import Callable, TypeVar, ParamSpec

P = ParamSpec("P")
R = TypeVar("R")


class Scheduler:
    """Run sync callables in background threads and await their results in asyncio.

    A fixed number of daemon worker threads consume jobs from an internal queue,
    execute them, and complete asyncio futures on the event loop thread.
    """

    def __init__(self, workers: int = 4, interval: float = None):
        """Create a scheduler and start worker threads.

        Args:
            workers: Number of background worker threads.
            interval: Time interval between job executions.
        """
        self.q = queue.Queue()
        self.interval = max(0.1, interval) if interval is not None else None

        for _ in range(workers):
            threading.Thread(
                target=self._worker,
                daemon=True,
            ).start()

    def _worker(self):
        """Continuously process queued jobs and resolve associated futures."""
        while True:
            fn, args, kwargs, future = self.q.get()

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

        self.q.put((fn, args, kwargs, future))

        return await future
