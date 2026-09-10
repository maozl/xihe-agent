# -*- coding: utf-8 -*-
"""回归：工具面（tools 模块）发起的变更必须落盘。

2026-09-08 线上事故：_load_jobs 里 `_jobs = {...}` 重新绑定，而
tools.cronjob_tools 通过 `from scheduler import _jobs` 持有旧 dict——
handler 写旧 dict、guard 保存新 dict，create 成功但文件里永远没有任务，
list 却显示存在。修复 = _load_jobs 原地 clear+update（不再重绑定）+
handler 改模块限定访问。本测试专门跨模块边界验证"文件内容"而非内存。
"""
import json

import pytest

import tools.cronjob_tools as t
import core.services.scheduler as sched


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(sched, "_CRON_DIR", tmp_path)
    monkeypatch.setattr(sched, "_JOBS_FILE", tmp_path / "jobs.json")
    monkeypatch.setattr(sched, "_JOBS_LOCK_FILE", tmp_path / "jobs.lock")


def _ids_on_disk():
    return {j["id"] for j in
            json.loads(sched._JOBS_FILE.read_text(encoding="utf-8"))["jobs"]}


def test_create_via_tools_persists_to_file():
    r = json.loads(t._cronjob({"action": "create", "name": "k", "schedule": "5m",
                               "prompt": "x", "deliver": "local"}))
    jid = r["job_id"]
    # 关键断言：磁盘上有，而不是只有内存里有
    assert jid in _ids_on_disk()
    # list（也走 tools 模块）与磁盘一致
    listed = {j["job_id"] for j in json.loads(t._cronjob({"action": "list"}))["jobs"]}
    assert listed == _ids_on_disk()
    assert "success" in t._cronjob({"action": "delete", "job_id": jid})
    assert jid not in _ids_on_disk()


def test_load_jobs_never_rebinds_dict_identity():
    ident_before = id(sched._jobs)
    sched._save_jobs()
    sched._load_jobs()
    assert id(sched._jobs) == ident_before
