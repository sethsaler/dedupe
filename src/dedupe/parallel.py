"""Parallel map helpers for hashing stages.

Defaults are intentionally conservative so a multi-thousand-file scan
does not pin every core or flood RAM/disk with unbounded work.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from typing import Literal, TypeVar

T = TypeVar("T")
R = TypeVar("R")

Backend = Literal["thread", "process"]

# Auto worker budget: leave headroom for UI/OS, never use every logical core.
DEFAULT_WORKERS_CAP = 8
# Concurrent full-file reads (SHA-256 of large videos is disk-bound).
DEFAULT_EXACT_WORKERS_CAP = 4
# Concurrent image decodes (Pillow + pHash are CPU + RAM heavy on big JPEGs/HEIC).
DEFAULT_IMAGE_WORKERS_CAP = 6
# Direct-seek fingerprint jobs are short-lived; four keeps modern SSDs/CPUs busy
# without returning to the contention caused by parallel full-timeline decodes.
DEFAULT_VIDEO_WORKERS_CAP = 4
# Concurrent OpenCV detector instances. Photon remains serial because its local
# model is substantially heavier and does not promise thread-safe inference.
DEFAULT_HUMAN_WORKERS_CAP = 4


def resolve_workers(
    workers: int | None,
    *,
    cap: int | None = None,
) -> int:
    """
    Normalize a workers setting.

    - None / 0 / negative → auto: min(cpu-1, DEFAULT_WORKERS_CAP), at least 1
    - positive → that many workers (at least 1), then optional stage ``cap``
    """
    if workers is None or workers <= 0:
        cpu = os.cpu_count() or 1
        # Leave one logical core free so the machine stays responsive.
        n = max(1, cpu - 1) if cpu > 2 else max(1, cpu)
        n = min(n, DEFAULT_WORKERS_CAP)
    else:
        n = max(1, int(workers))

    if cap is not None:
        n = min(n, max(1, int(cap)))
    return n


def map_parallel(
    fn: Callable[[T], R],
    items: Sequence[T],
    *,
    workers: int = 1,
    backend: Backend = "thread",
    progress: Callable[[int, int], None] | None = None,
    progress_every: int = 1,
    cancelled: Callable[[], bool] | None = None,
) -> list[R]:
    """
    Map ``fn`` over ``items``, preserving order.

    When ``workers <= 1`` or there is only one item, runs sequentially
    (no pool overhead). Progress is ``progress(done, total)``.

    For large item lists, only keeps ~2×workers futures in flight so we
    do not allocate tens of thousands of Future objects or queue unbounded
    disk/CPU work.
    """
    if not items:
        return []

    n = len(items)
    every = max(1, progress_every)

    if workers <= 1 or n == 1:
        results: list[R] = []
        for i, item in enumerate(items):
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            results.append(fn(item))
            done = i + 1
            if progress and (done % every == 0 or done == n):
                progress(done, n)
        return results

    Executor = ThreadPoolExecutor if backend == "thread" else ProcessPoolExecutor
    ordered: list[R | None] = [None] * n
    done = 0
    # Bound in-flight work: enough to keep cores busy, not enough to thrash.
    window = max(workers * 2, workers)
    next_i = 0
    inflight: dict = {}

    with Executor(max_workers=workers) as ex:
        def _submit_more() -> None:
            nonlocal next_i
            while next_i < n and len(inflight) < window:
                fut = ex.submit(fn, items[next_i])
                inflight[fut] = next_i
                next_i += 1

        _submit_more()
        while inflight:
            if cancelled and cancelled():
                for future in inflight:
                    future.cancel()
                raise InterruptedError("scan cancelled")
            finished, _ = wait(inflight.keys(), return_when=FIRST_COMPLETED)
            for fut in finished:
                idx = inflight.pop(fut)
                ordered[idx] = fut.result()
                done += 1
                if progress and (done % every == 0 or done == n):
                    progress(done, n)
            _submit_more()

    return ordered  # type: ignore[return-value]
