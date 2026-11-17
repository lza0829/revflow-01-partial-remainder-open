"""部分冲正级联：正向剩余敞口的物化与事务内扣减。

落库形态：独立敞口表 ``forward_exposures``，按正向流水键关联。
剩余敞口 = 正向原金额 − 已成功配平冲正累计；每次成功冲正在**同一个**
数据库事务内改写该行，归并报告与互证只从该表和流水状态派生，不在内存
累加，刷新/进程重启后仍可查询。

状态：未冲 / 部分冲 / 已闭口（剩余敞口为 0）。
"""

from __future__ import annotations

import sqlite3

STATE_UNTOUCHED = "未冲"
STATE_PARTIAL = "部分冲"
STATE_CLOSED = "已闭口"

#: 超额冲正整笔失败时的隔离原因；与 quarantine_items.excess_cents /
#: exposure_cents 一起写明超额与当时敞口。
EXCESS_REASON = "冲正超额"


def _state(original_cents: int, reversed_cents: int) -> str:
    if original_cents - reversed_cents <= 0:
        return STATE_CLOSED
    if reversed_cents > 0:
        return STATE_PARTIAL
    return STATE_UNTOUCHED


def ensure_rows(conn: sqlite3.Connection, now: str) -> None:
    """为所有已装载正向补齐敞口行（幂等）。

    新正向以原金额开敞口；已存在的行绝不覆盖，避免把已冲累计抹回原值。
    """
    conn.execute(
        """
        INSERT OR IGNORE INTO forward_exposures(
          forward_entry_key, original_cents, reversed_cents, remaining_cents,
          state, updated_at
        )
        SELECT entry_key, amount_cents, 0, amount_cents, ?, ?
        FROM ledger_entries
        WHERE kind = '正向'
        """,
        (STATE_UNTOUCHED, now),
    )


def load(conn: sqlite3.Connection, forward_entry_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM forward_exposures WHERE forward_entry_key = ?",
        (forward_entry_key,),
    ).fetchone()


def apply(
    conn: sqlite3.Connection,
    forward_entry_key: str,
    amount_cents: int,
    reversal_entry_key: str,
    settle_run_id: int,
    now: str,
) -> dict:
    """在调用方事务内扣减敞口。

    超额时不改写任何敞口（整笔失败），只回报当时敞口与超额，由调用方
    写未平隔离。成功时物化新的已冲累计与剩余敞口，并返回扣减后快照。
    """
    row = load(conn, forward_entry_key)
    if row is None:
        return {"ok": False, "missing": True}

    remaining = row["remaining_cents"]
    if amount_cents > remaining:
        return {
            "ok": False,
            "missing": False,
            "remaining_cents": remaining,
            "excess_cents": amount_cents - remaining,
            "reversed_cents": row["reversed_cents"],
        }

    reversed_cents = row["reversed_cents"] + amount_cents
    remaining_cents = row["original_cents"] - reversed_cents
    state = _state(row["original_cents"], reversed_cents)
    conn.execute(
        """
        UPDATE forward_exposures
        SET reversed_cents = ?,
            remaining_cents = ?,
            state = ?,
            last_reversal_key = ?,
            last_settle_run_id = ?,
            updated_at = ?
        WHERE forward_entry_key = ?
        """,
        (
            reversed_cents,
            remaining_cents,
            state,
            reversal_entry_key,
            settle_run_id,
            now,
            forward_entry_key,
        ),
    )
    return {
        "ok": True,
        "missing": False,
        "remaining_cents": remaining_cents,
        "reversed_cents": reversed_cents,
        "excess_cents": 0,
        "state": state,
    }


def snapshot(conn: sqlite3.Connection) -> dict:
    """敞口合计——归并报告的唯一来源。"""
    row = conn.execute(
        """
        SELECT
          COALESCE(SUM(reversed_cents), 0) AS reversed_total_cents,
          COALESCE(SUM(remaining_cents), 0) AS open_cents,
          COALESCE(SUM(CASE WHEN state = ? THEN 1 ELSE 0 END), 0) AS closed_count,
          COALESCE(SUM(CASE WHEN state = ? THEN 1 ELSE 0 END), 0) AS partial_count,
          COUNT(*) AS total_count
        FROM forward_exposures
        """,
        (STATE_CLOSED, STATE_PARTIAL),
    ).fetchone()
    return {
        "reversed_total_cents": row["reversed_total_cents"],
        "open_cents": row["open_cents"],
        "closed_count": row["closed_count"],
        "partial_count": row["partial_count"],
        "total_count": row["total_count"],
    }


def mismatch_count(conn: sqlite3.Connection) -> int:
    """敞口守恒校验：原额 = 已冲 + 剩余，且已冲累计 = 已归并冲正之和。"""
    bad = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM forward_exposures fe
        WHERE fe.reversed_cents + fe.remaining_cents != fe.original_cents
           OR fe.remaining_cents < 0
           OR fe.reversed_cents != COALESCE((
                SELECT SUM(le.amount_cents)
                FROM ledger_entries le
                WHERE le.kind = '冲正'
                  AND le.status = '已归并'
                  AND le.target_entry_key = fe.forward_entry_key
              ), 0)
        """
    ).fetchone()["c"]
    missing = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM ledger_entries e
        WHERE e.kind = '正向'
          AND NOT EXISTS (
            SELECT 1 FROM forward_exposures fe
            WHERE fe.forward_entry_key = e.entry_key
          )
        """
    ).fetchone()["c"]
    return bad + missing
