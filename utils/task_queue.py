# Standard library imports
import threading
from queue import Queue, Full
from concurrent.futures import Future
import logging


class TaskQueue:
    """Simple threaded task queue with fixed worker count."""

    def __init__(self, size: int, workers: int):
        self.size = size
        self.workers = workers
        self.queue = Queue(maxsize=size)
        self._start_workers()

    def _start_workers(self):
        for _ in range(self.workers):
            thread = threading.Thread(target=self._worker, daemon=True)
            thread.start()

    def _worker(self):
        while True:
            func, args, kwargs, future = self.queue.get()
            try:
                result = func(*args, **kwargs)
                future.set_result(result)
            except Exception as exc:  # pragma: no cover - logging side effect
                logging.error("[x] Task raised exception: %s", exc)
                future.set_exception(exc)
            finally:
                self.queue.task_done()

    def submit(self, func, *args, **kwargs) -> Future:
        """Submit a callable to the queue. Raises queue.Full if the queue is full."""
        future = Future()
        try:
            self.queue.put_nowait((func, args, kwargs, future))
        except Full:
            logging.warning("[!] Task queue full; rejecting new task")
            raise
        return future
