# -*- coding: utf-8 -*-
"""core/support/process_lock：跨进程互斥的通用文件锁。

从 cron 的 jobs_write_lock 抽取的通用件——只管互斥（OS 字节范围锁 +
超时），不带任何业务语义。崩溃属主的锁随句柄关闭而释放，不死锁。
"""
import threading
import time

import pytest

from core.support.process_lock import CrossProcessLock


def test_mutual_exclusion_across_handles(tmp_path):
    """两个句柄（模拟两个进程）不能同时持有——后到者等待。"""
    lock = tmp_path / "a.lock"
    with CrossProcessLock(lock):
        got = []
        try:
            with CrossProcessLock(lock, timeout=0.3):
                got.append("entered")
        except TimeoutError:
            got.append("timeout")
        assert got == ["timeout"]


def test_exclusion_serializes_threads(tmp_path):
    """并发线程逐个进入临界区，计数无竞态。"""
    lock = tmp_path / "b.lock"
    counter = {"n": 0}
    peak = {"max": 0}

    def worker():
        for _ in range(20):
            with CrossProcessLock(lock):
                counter["n"] += 1
                peak["max"] = max(peak["max"], counter["n"])
                counter["n"] -= 1

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak["max"] == 1  # 任何时刻至多 1 人在临界区


def test_release_on_exception(tmp_path):
    lock = tmp_path / "c.lock"
    with pytest.raises(RuntimeError):
        with CrossProcessLock(lock):
            raise RuntimeError("boom")
    # 异常路径释放：下一个立刻能进
    with CrossProcessLock(lock, timeout=1):
        pass


def test_timeout_raises_not_blocks_forever(tmp_path):
    lock = tmp_path / "d.lock"
    with CrossProcessLock(lock):
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            with CrossProcessLock(lock, timeout=0.2):
                pass
        assert time.monotonic() - t0 < 2


def test_reacquire_after_release(tmp_path):
    lock = tmp_path / "e.lock"
    with CrossProcessLock(lock):
        pass
    with CrossProcessLock(lock, timeout=1):
        pass
