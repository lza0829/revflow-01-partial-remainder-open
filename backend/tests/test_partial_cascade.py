"""部分冲正配平后剩余敞口的物化、超额整笔闭入隔离、报告同源派生。

所有断言只经 HTTP 读回，并在重新建连后复查「刷新后仍在」。
"""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture(scope="module")
def _db(db_file):
    """独立连接 = 刷新/重启后视角，确认不是内存态。"""

    def _open() -> sqlite3.Connection:
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        return conn

    return _open


def test_partial_settle_materializes_remaining_exposure(client):
    run = client.post("/api/settle-runs")
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["status"] == "完成"
    assert body["exposure_mismatch_count"] == 0
    # 夹具：FWD-002 原 25000 被部分冲 5000；FWD-005 原 8000 被部分冲 6000；
    # FWD-001 / FWD-003 被全额冲闭口。REV-005 对已闭口再冲 500 → 超额隔离。
    assert body["reversed_total_cents"] == 39000
    assert body["open_cents"] == 72000
    assert body["closed_count"] == 2
    assert body["excess_quarantine_count"] == 1


def test_exposure_visible_after_reconnect(client, _db):
    exp = {e["forward_entry_key"]: e for e in client.get("/api/exposures").json()}
    fwd2 = exp["FWD-20261116-002"]
    assert fwd2["original_cents"] == 25000
    assert fwd2["reversed_cents"] == 5000
    assert fwd2["remaining_cents"] == 20000
    assert fwd2["state"] == "部分冲"

    fwd1 = exp["FWD-20261116-001"]
    assert fwd1["remaining_cents"] == 0
    assert fwd1["state"] == "已闭口"

    # 另开连接直接读库，证明持久化而非请求内变量
    conn = _db()
    try:
        row = conn.execute(
            "SELECT remaining_cents, state FROM forward_exposures WHERE forward_entry_key = ?",
            ("FWD-20261116-002",),
        ).fetchone()
        assert row["remaining_cents"] == 20000
        assert row["state"] == "部分冲"
    finally:
        conn.close()


def test_second_round_uses_materialized_remainder(client, _db):
    """再装一笔 18000 的冲正打同一正向：只能吃 20000 剩余中的额度。"""
    conn = _db()
    try:
        conn.execute(
            """
            INSERT INTO ledger_entries(
              id, entry_key, account_id, kind, amount_cents, biz_ref,
              target_entry_key, booked_at, status, note
            ) VALUES (?, ?, ?, '冲正', ?, ?, ?, ?, '待归并', ?)
            """,
            (
                900,
                "REV-ROUND2-001",
                1,
                20000,
                "ORD-9002",
                "FWD-20261116-002",
                "2026-11-16T18:00:00Z",
                "二轮补冲至闭口",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    run = client.post("/api/settle-runs")
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["exposure_mismatch_count"] == 0

    exp = {e["forward_entry_key"]: e for e in client.get("/api/exposures").json()}
    fwd2 = exp["FWD-20261116-002"]
    assert fwd2["reversed_cents"] == 25000
    assert fwd2["remaining_cents"] == 0
    assert fwd2["state"] == "已闭口"

    # 重复归并不会按原金额再扣：余额 = 已归并正向 − 已归并冲正
    accs = {a["id"]: a for a in client.get("/api/accounts").json()}
    # 账户 1：正向 10000+25000+8000 − 冲正 10000+5000+20000（REV-006 的 6000 已在首轮）
    assert accs[1]["balance_cents"] == 10000 + 25000 + 8000 - 10000 - 5000 - 20000 - 6000


def test_excess_reversal_whole_fails_into_quarantine(client, _db):
    """对已闭口正向再冲 1 分：整笔失败，写明超额与当时敞口，余额不动。"""
    before = {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}
    conn = _db()
    try:
        conn.execute(
            """
            INSERT INTO ledger_entries(
              id, entry_key, account_id, kind, amount_cents, biz_ref,
              target_entry_key, booked_at, status, note
            ) VALUES (?, ?, ?, '冲正', ?, ?, ?, ?, '待归并', ?)
            """,
            (
                901,
                "REV-EXCESS-001",
                1,
                1,
                "ORD-9002",
                "FWD-20261116-002",
                "2026-11-16T19:00:00Z",
                "超额——应整笔隔离",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    run = client.post("/api/settle-runs").json()
    assert run["excess_quarantine_count"] == 1

    q = [x for x in client.get("/api/quarantine").json() if x["entry_key"] == "REV-EXCESS-001"]
    assert len(q) == 1
    item = q[0]
    assert item["reason"] == "冲正超额"
    assert item["amount_cents"] == 1
    assert item["excess_cents"] == 1
    assert item["exposure_cents"] == 0
    assert item["target_entry_key"] == "FWD-20261116-002"

    # 整笔失败：余额未被扣，敞口仍闭口
    after = {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}
    assert after == before
    exp = {e["forward_entry_key"]: e for e in client.get("/api/exposures").json()}
    assert exp["FWD-20261116-002"]["remaining_cents"] == 0


def test_reports_derived_from_persisted_sources(client, _db):
    run = client.get("/api/settle-runs").json()[0]
    exp = client.get("/api/exposures").json()
    reversed_total = sum(e["reversed_cents"] for e in exp)
    open_total = sum(e["remaining_cents"] for e in exp)
    closed = sum(1 for e in exp if e["state"] == "已闭口")

    assert run["reversed_total_cents"] == reversed_total
    assert run["open_cents"] == open_total
    assert run["closed_count"] == closed
    assert run["exposure_mismatch_count"] == 0

    report = client.post("/api/verify-reports").json()
    assert report["consistent"] == 1
    assert report["detail"]["最新轮敞口一致"] == 1
    # 最新轮（超额那轮）落库快照 = 当前敞口表派生
    assert report["detail"]["已冲累计分"] == reversed_total
    assert report["detail"]["剩余敞口合计分"] == open_total
    assert report["detail"]["当前剩余敞口合计分"] == open_total
    assert report["detail"]["本轮超额隔离笔数"] == 1
    assert report["detail"]["敞口不符数"] == 0

    dash = client.get("/api/dashboard").json()
    assert dash["已冲累计分"] == reversed_total
    assert dash["剩余敞口分"] == open_total

    # 刷新场景：直接读库比对本轮报告列
    conn = _db()
    try:
        row = conn.execute(
            "SELECT reversed_total_cents, open_cents FROM settle_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["reversed_total_cents"] == reversed_total
        assert row["open_cents"] == open_total
    finally:
        conn.close()


def test_forward_entries_carry_exposure(client):
    rows = {e["entry_key"]: e for e in client.get("/api/entries?kind=正向").json()}
    assert rows["FWD-20261116-002"]["exposure_remaining_cents"] == 0
    assert rows["FWD-20261116-002"]["exposure_reversed_cents"] == 25000
    assert rows["FWD-20261116-002"]["exposure_state"] == "已闭口"
    assert rows["FWD-20261116-004"]["exposure_remaining_cents"] == 50000
    assert rows["FWD-20261116-004"]["exposure_state"] == "未冲"


def test_rerun_without_pending_does_not_double_deduct(client, _db):
    before = {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}
    exp_before = {e["forward_entry_key"]: e["reversed_cents"] for e in client.get("/api/exposures").json()}

    body = client.post("/api/settle-runs").json()
    assert body["forward_count"] == 0
    assert body["reversal_count"] == 0
    assert body["reversed_total_cents"] == sum(exp_before.values())

    after = {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}
    exp_after = {e["forward_entry_key"]: e["reversed_cents"] for e in client.get("/api/exposures").json()}
    assert after == before
    assert exp_after == exp_before


def test_failed_round_rolls_back_exposure_and_balance(client, _db):
    """事务内后期失败：敞口、流水状态、余额、隔离全部回退，不留半截。"""
    conn = _db()
    try:
        conn.execute(
            """
            INSERT INTO ledger_entries(
              id, entry_key, account_id, kind, amount_cents, biz_ref,
              target_entry_key, booked_at, status, note
            ) VALUES (?, ?, ?, '冲正', ?, ?, ?, ?, '待归并', ?)
            """,
            (
                902,
                "REV-ROLLBACK-001",
                3,
                1000,
                "ORD-9201",
                "FWD-20261116-004",
                "2026-11-16T20:00:00Z",
                "用于触发回滚",
            ),
        )
        # 在余额落库后强制失败，验证同事务内的敞口/隔离/状态一并回退
        conn.execute(
            """
            CREATE TRIGGER fail_balance_update
            BEFORE UPDATE OF balance_cents ON accounts
            BEGIN
              SELECT RAISE(ABORT, '模拟过账失败');
            END
            """
        )
        conn.commit()
    finally:
        conn.close()

    before_exp = {e["forward_entry_key"]: e["remaining_cents"] for e in client.get("/api/exposures").json()}
    before_bal = {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}
    before_q = len(client.get("/api/quarantine").json())

    with pytest.raises(Exception):
        client.post("/api/settle-runs")

    after_exp = {e["forward_entry_key"]: e["remaining_cents"] for e in client.get("/api/exposures").json()}
    after_bal = {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}
    status = {
        e["entry_key"]: e["status"] for e in client.get("/api/entries?kind=冲正").json()
    }

    assert after_exp == before_exp
    assert after_bal == before_bal
    assert len(client.get("/api/quarantine").json()) == before_q
    assert status["REV-ROLLBACK-001"] == "待归并"
    # 失败轮不留下「进行中」的完成态：余额/敞口/流水保持一致即满足闭合
    assert all(e["status"] != "已归并" or e["entry_key"] != "REV-ROLLBACK-001" for e in client.get("/api/entries?kind=冲正").json())

    conn = _db()
    try:
        conn.execute("DROP TRIGGER IF EXISTS fail_balance_update")
        conn.commit()
    finally:
        conn.close()
