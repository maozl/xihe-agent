# -*- coding: utf-8 -*-
"""防双发的唯一防线：jobs.json 事务的原子推进（显式模式，无包装类）。

归属锁已按设计移除（锁只管多进程写 jobs）。两个进程的调度循环并行
轮询时，"同一个到期任务恰好派发一次"完全依赖：到期判定 + next_run 推进
+ 落盘在同一个 CrossProcessLock 事务内。先入事务的一方推进到未来时刻，
后入方只能看到"未到期"。本测试用并发 tick 事务把这个不变量钉死——
把 due 判定挪出事务会直接挂在这里。
"""
import threading
from datetime import datetime

import pytest

import core.services.scheduler as sched
from core.support.process_lock import CrossProcessLock


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(sched, "_CRON_DIR", tmp_path)
    monkeypatch.setattr(sched, "_JOBS_FILE", tmp_path / "jobs.json")
    monkeypatch.setattr(sched, "_JOBS_LOCK_FILE", tmp_path / "jobs.lock")
    monkeypatch.setattr(sched, "_running", False)
    monkeypatch.setattr(sched, "_agent", None)
    monkeypatch.setattr(sched, "_agent_factory", None)


def _due_job_on_disk():
    schedule = sched.parse_schedule("every 1m")
    sched._jobs["j1"] = {
        "id": "j1", "name": "t", "enabled": True, "state": "scheduled",
        "schedule": schedule,
        "next_run_at": "2020-01-01T00:00:00",  # 早已到期
    }
    sched._save_jobs()


def _tick_body(dispatched):
    """一个调度循环 tick 的事务体（与 _scheduler_loop 相同的显式形状）。"""
    with sched._lock, CrossProcessLock(sched._JOBS_LOCK_FILE):
        sched._load_jobs()
        due = sched._get_due_jobs()
        for j in due:
            src = sched._jobs.get(j["id"])
            src["next_run_at"] = sched._compute_next_run(
                src.get("schedule", {}), datetime.now().isoformat())
            src["state"] = "running"
        if due:
            _save = [j["id"] for j in due]
            sched._save_jobs()
            dispatched.append(_save)


def test_concurrent_ticks_dispatch_exactly_once():
    _due_job_on_disk()
    dispatched = []
    barrier = threading.Barrier(2)

    def tick():
        barrier.wait()      # 事务外对齐——让两个事务真刀真枪抢锁
        _tick_body(dispatched)

    t1, t2 = threading.Thread(target=tick), threading.Thread(target=tick)
    t1.start(); t2.start()
    t1.join(); t2.join()

    total = [jid for batch in dispatched for jid in batch]
    assert total == ["j1"], f"double-dispatch or miss: {total}"
    # 输家看到的是推进后的未来时刻，不派发
    assert sched._jobs["j1"]["next_run_at"] > "2020-01-01T00:00:00"


def test_start_scheduler_idempotent():
    sched.start_scheduler()
    sched.start_scheduler()   # 第二次调用直接返回，不起第二个循环
    assert sched.scheduler_health()["running"] is True


def test_parse_schedule_normalizes_aware_tz():
    """带 Z/+00:00 的 once 时刻必须落成 naive 本地时间——否则与
    datetime.now() 的 naive 比较抛 TypeError，被 _get_due_jobs 静默吞掉，
    任务永远不调度。"""
    from datetime import datetime as _dt
    s = sched.parse_schedule("2026-09-11T09:00:00Z")
    run_at = _dt.fromisoformat(s["run_at"])
    assert run_at.tzinfo is None
    expect = _dt.fromisoformat("2026-09-11T09:00:00+00:00").astimezone().replace(tzinfo=None)
    assert run_at == expect


def test_due_jobs_survive_legacy_aware_next_run():
    """jobs.json 里的历史 aware 数据同样要能被判定到期（_to_naive_local 防御）：
    无防御时 aware/naive 比较抛 TypeError 被 except 吞掉，任务永不调度。"""
    sched._jobs.clear()
    sched._jobs["legacy"] = {
        "id": "legacy", "name": "t", "enabled": True, "state": "scheduled",
        "schedule": sched.parse_schedule("every 1m"),
        "next_run_at": "2020-01-01T00:00:00+00:00",  # 早已到期的 aware 旧数据
    }
    assert [j["id"] for j in sched._get_due_jobs()] == ["legacy"]


def test_list_job_runs_reads_output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sched, "_OUTPUT_DIR", tmp_path)
    job_dir = tmp_path / "j1"
    job_dir.mkdir()
    for name in ["2026-09-11_09-00-00.md", "2026-09-11_10-30-15.md",
                 "2026-09-10_23-59-59.md"]:
        (job_dir / name).write_text(f"out {name}", encoding="utf-8")
    (job_dir / "scripts").mkdir()
    (job_dir / "scripts" / "2026-09-11_10-30-15.txt").write_text("s", encoding="utf-8")
    (job_dir / "2026-09-09_08-00-00.md").write_text("x" * 5000, encoding="utf-8")

    runs = sched.list_job_runs("j1", limit=2)
    assert [r["run_at"] for r in runs] == ["2026-09-11T10:30:15", "2026-09-11T09:00:00"]
    assert runs[0]["content"] == "out 2026-09-11_10-30-15.md"

    all_runs = sched.list_job_runs("j1")
    assert len(all_runs) == 4
    assert all_runs[-1]["content"].endswith("…(truncated)")
    assert len(all_runs[-1]["content"]) == 4000 + len("…(truncated)")
    assert sched.list_job_runs("no-such") == []
    assert sched.list_job_runs("j1", limit=0) == []
