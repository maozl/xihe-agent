# -*- coding: utf-8 -*-
"""jobs.json 的跨进程/跨线程读-改-写安全（显式事务模式）。

2026-09-07 事故：A 进程 create 落盘后数毫秒，B 进程（更早 load 的旧快照）
save 整文件覆盖，A 的新任务无声丢失。修复 = 每个变更点显式走
`with _lock, CrossProcessLock(_JOBS_LOCK_FILE): _load_jobs(); ...; _save_jobs()`。
"""
import json
import threading

import pytest

import core.services.scheduler as sched
from core.support.process_lock import CrossProcessLock


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(sched, "_CRON_DIR", tmp_path)
    monkeypatch.setattr(sched, "_JOBS_FILE", tmp_path / "jobs.json")
    monkeypatch.setattr(sched, "_JOBS_LOCK_FILE", tmp_path / "jobs.lock")


def _txn_insert(jid):
    with sched._lock, CrossProcessLock(sched._JOBS_LOCK_FILE):
        sched._load_jobs()
        sched._jobs[jid] = {"id": jid, "name": jid}
        sched._save_jobs()


def test_concurrent_transactions_all_survive():
    """N 线程 × M 次事务并发插入——旧代码下先读后写的交错会互相覆盖，
    事务下最终文件必须包含全部 N×M 个任务。"""
    n_workers, m_iters = 6, 8

    def worker(w):
        for i in range(m_iters):
            _txn_insert(f"w{w}_{i}")

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    data = json.loads(sched._JOBS_FILE.read_text(encoding="utf-8"))
    ids = {j["id"] for j in data["jobs"]}
    assert ids == {f"w{w}_{i}" for w in range(n_workers) for i in range(m_iters)}


def test_transaction_reloads_latest_state():
    # 事务门外改内存不落盘；事务进门重读文件，出门只落盘事务内改动
    with sched._lock, CrossProcessLock(sched._JOBS_LOCK_FILE):
        sched._load_jobs()
        sched._jobs["a"] = {"id": "a", "name": "a"}
        sched._save_jobs()
    sched._jobs["stale"] = {"id": "stale"}  # 门外脏写（不落盘）
    with sched._lock, CrossProcessLock(sched._JOBS_LOCK_FILE):
        sched._load_jobs()
        assert "stale" not in sched._jobs    # 进门被磁盘状态刷新
        sched._jobs["b"] = {"id": "b", "name": "b"}
        sched._save_jobs()
    data = json.loads(sched._JOBS_FILE.read_text(encoding="utf-8"))
    assert {j["id"] for j in data["jobs"]} == {"a", "b"}


def test_save_is_atomic_replace():
    # _save_jobs 走 tmp + os.replace：读者永远看不到半个 JSON
    with sched._lock, CrossProcessLock(sched._JOBS_LOCK_FILE):
        sched._load_jobs()
        sched._jobs["x"] = {"id": "x"}
        sched._save_jobs()
    json.loads(sched._JOBS_FILE.read_text(encoding="utf-8"))  # 完整可解析
    assert not (sched._CRON_DIR / "jobs.json.tmp").exists()
