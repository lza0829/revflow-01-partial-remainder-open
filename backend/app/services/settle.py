from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect, rows_to_dicts
from app.modules import partial_cascade


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dashboard() -> dict:
    with connect() as conn:
        accounts = rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY id").fetchall())
        fwd = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '正向'"
        ).fetchone()["c"]
        rev = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '冲正'"
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE status = '待归并'"
        ).fetchone()["c"]
        q = conn.execute("SELECT COUNT(*) AS c FROM quarantine_items").fetchone()["c"]
        bal = conn.execute("SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts").fetchone()["s"]
        last = conn.execute("SELECT * FROM settle_runs ORDER BY id DESC LIMIT 1").fetchone()
        snap = partial_cascade.snapshot(conn)
        return {
            "账户数": len(accounts),
            "正向笔数": fwd,
            "冲正笔数": rev,
            "待归并": pending,
            "未平隔离": q,
            "余额合计分": bal,
            "已冲累计分": snap["reversed_total_cents"],
            "剩余敞口分": snap["open_cents"],
            "闭口正向数": snap["closed_count"],
            "部分冲正向数": snap["partial_count"],
            "敞口不符数": partial_cascade.mismatch_count(conn),
            "最近归并": dict(last) if last else None,
            "账户": accounts,
        }


def list_accounts() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY id").fetchall())


def list_entries(kind: str | None = None) -> list[dict]:
    with connect() as conn:
        if kind:
            rows = conn.execute(
                """
                SELECT e.*, a.code AS account_code, a.name AS account_name,
                       fe.original_cents AS exposure_original_cents,
                       fe.reversed_cents AS exposure_reversed_cents,
                       fe.remaining_cents AS exposure_remaining_cents,
                       fe.state AS exposure_state,
                       fe.last_reversal_key AS exposure_last_reversal_key
                FROM ledger_entries e
                JOIN accounts a ON a.id = e.account_id
                LEFT JOIN forward_exposures fe
                  ON e.kind = '正向' AND fe.forward_entry_key = e.entry_key
                WHERE e.kind = ?
                ORDER BY e.id
                """,
                (kind,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT e.*, a.code AS account_code, a.name AS account_name,
                       fe.original_cents AS exposure_original_cents,
                       fe.reversed_cents AS exposure_reversed_cents,
                       fe.remaining_cents AS exposure_remaining_cents,
                       fe.state AS exposure_state,
                       fe.last_reversal_key AS exposure_last_reversal_key
                FROM ledger_entries e
                JOIN accounts a ON a.id = e.account_id
                LEFT JOIN forward_exposures fe
                  ON e.kind = '正向' AND fe.forward_entry_key = e.entry_key
                ORDER BY e.id
                """
            ).fetchall()
        return rows_to_dicts(rows)


def list_exposures() -> list[dict]:
    """正向剩余敞口明细（持久物化，刷新后仍在）。"""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
              fe.forward_entry_key,
              e.account_id,
              a.code AS account_code,
              e.amount_cents AS original_amount_cents,
              fe.original_cents,
              fe.reversed_cents,
              fe.remaining_cents,
              fe.state,
              fe.last_reversal_key,
              fe.last_settle_run_id,
              fe.updated_at
            FROM forward_exposures fe
            JOIN ledger_entries e
              ON e.kind = '正向' AND e.entry_key = fe.forward_entry_key
            JOIN accounts a ON a.id = e.account_id
            ORDER BY e.id
            """
        ).fetchall()
        return rows_to_dicts(rows)


def list_quarantine() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT q.*, a.code AS account_code
            FROM quarantine_items q
            JOIN accounts a ON a.id = q.account_id
            ORDER BY q.id DESC
            """
        ).fetchall()
        return rows_to_dicts(rows)


def list_settle_runs() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(
            conn.execute("SELECT * FROM settle_runs ORDER BY id DESC").fetchall()
        )


def list_verify_reports() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(
            conn.execute("SELECT * FROM verify_reports ORDER BY id DESC").fetchall()
        )


def run_settle() -> dict:
    """一轮净额归并：敞口扣减 / 隔离 / 流水状态 / 余额同一事务成败。

    冲正额度只取自持久化的 ``forward_exposures.remaining_cents``，不依赖本轮
    内存变量，因此跨轮次（含上一轮已归并的正向）也按真实剩余敞口扣减。
    超额整笔失败进未平隔离并写明超额与当时敞口，绝不先扣余额再改状态。
    余额由「正式已归并流水」全量派生后覆盖写，保证重复跑不重复扣。
    """
    conn = connect()
    try:
        now = _now()
        # 单事务提交：余额 / 敞口 / 隔离 / 流水状态同成败
        conn.execute("BEGIN IMMEDIATE")
        partial_cascade.ensure_rows(conn, now)

        cur = conn.execute(
            "INSERT INTO settle_runs(status, started_at) VALUES ('进行中', ?)",
            (now,),
        )
        run_id = cur.lastrowid

        forwards = rows_to_dicts(
            conn.execute(
                "SELECT * FROM ledger_entries WHERE kind = '正向' AND status = '待归并' ORDER BY id"
            ).fetchall()
        )
        # 本轮正向并入候选：允许冲正命中早前轮次已归并的正向，额度仍以敞口表为准
        all_forwards = rows_to_dicts(
            conn.execute(
                "SELECT * FROM ledger_entries WHERE kind = '正向' ORDER BY id"
            ).fetchall()
        )
        reversals = rows_to_dicts(
            conn.execute(
                "SELECT * FROM ledger_entries WHERE kind = '冲正' AND status = '待归并' ORDER BY id"
            ).fetchall()
        )
        fwd_by_key = {f["entry_key"]: f for f in all_forwards}

        touched_accounts: set[int] = {f["account_id"] for f in forwards}

        def quarantine(r: dict, reason: str, excess: int = 0, exposure: int = 0) -> None:
            conn.execute(
                """
                INSERT INTO quarantine_items(
                  entry_key, account_id, reason, amount_cents, settle_run_id,
                  created_at, target_entry_key, excess_cents, exposure_cents
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["entry_key"],
                    r["account_id"],
                    reason,
                    r["amount_cents"],
                    run_id,
                    now,
                    r.get("target_entry_key"),
                    excess,
                    exposure,
                ),
            )
            conn.execute(
                "UPDATE ledger_entries SET status = '已隔离', settle_run_id = ? WHERE id = ?",
                (run_id, r["id"]),
            )

        for r in reversals:
            target = r.get("target_entry_key")
            if not target or target not in fwd_by_key:
                quarantine(r, "冲正无原单")
                continue

            fwd = fwd_by_key[target]
            if fwd["account_id"] != r["account_id"]:
                quarantine(r, "冲正账户与原单不一致")
                continue

            # 额度来源：持久化剩余敞口，而非本轮内存 used 字典
            result = partial_cascade.apply(
                conn,
                forward_entry_key=target,
                amount_cents=r["amount_cents"],
                reversal_entry_key=r["entry_key"],
                settle_run_id=run_id,
                now=now,
            )
            if result.get("missing"):
                # 敞口行缺失：保守闭口，不猜额度，整笔隔离
                quarantine(r, "正向敞口缺失")
                continue
            if not result["ok"]:
                quarantine(
                    r,
                    partial_cascade.EXCESS_REASON,
                    excess=result["excess_cents"],
                    exposure=result["remaining_cents"],
                )
                continue

            # 敞口已物化成功后才改冲正状态——同一事务内，异常整体回滚
            touched_accounts.add(r["account_id"])
            conn.execute(
                "UPDATE ledger_entries SET status = '已归并', settle_run_id = ? WHERE id = ?",
                (run_id, r["id"]),
            )

        for f in forwards:
            conn.execute(
                "UPDATE ledger_entries SET status = '已归并', settle_run_id = ? WHERE id = ?",
                (run_id, f["id"]),
            )

        # 余额全量派生：正式已归并正向 − 正式已归并冲正，按账户覆盖写
        bal_rows = conn.execute(
            """
            SELECT account_id,
                   COALESCE(SUM(CASE WHEN kind = '正向' THEN amount_cents
                                     WHEN kind = '冲正' THEN -amount_cents
                                     ELSE 0 END), 0) AS net
            FROM ledger_entries
            WHERE status = '已归并'
            GROUP BY account_id
            """
        ).fetchall()
        account_net = {row["account_id"]: row["net"] for row in bal_rows}
        for acc_id in sorted(touched_accounts | set(account_net)):
            conn.execute(
                "UPDATE accounts SET balance_cents = ?, last_settle_run_id = ? WHERE id = ?",
                (account_net.get(acc_id, 0), run_id, acc_id),
            )

        # 报告口径全部从敞口表 / 流水状态 / 隔离表 / 账户余额派生，禁止内存计数
        snap = partial_cascade.snapshot(conn)
        mismatch = partial_cascade.mismatch_count(conn)
        counts = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM ledger_entries
                WHERE status = '已归并' AND settle_run_id = ?) AS settled_count,
              (SELECT COUNT(*) FROM quarantine_items
                WHERE settle_run_id = ?) AS quarantine_count,
              (SELECT COUNT(*) FROM quarantine_items
                WHERE settle_run_id = ? AND reason = ?) AS excess_count,
              (SELECT COALESCE(SUM(balance_cents), 0) FROM accounts
                WHERE last_settle_run_id = ?) AS net_cents
            """,
            (
                run_id,
                run_id,
                run_id,
                partial_cascade.EXCESS_REASON,
                run_id,
            ),
        ).fetchone()
        finished = _now()
        conn.execute(
            """
            UPDATE settle_runs
            SET status = '完成',
                forward_count = ?,
                reversal_count = ?,
                settled_count = ?,
                quarantine_count = ?,
                net_cents = ?,
                finished_at = ?,
                reversed_total_cents = ?,
                open_cents = ?,
                closed_count = ?,
                excess_quarantine_count = ?,
                exposure_mismatch_count = ?,
                commit_mode = '单事务',
                note = ?
            WHERE id = ?
            """,
            (
                len(forwards),
                len(reversals),
                counts["settled_count"],
                counts["quarantine_count"],
                counts["net_cents"],
                finished,
                snap["reversed_total_cents"],
                snap["open_cents"],
                snap["closed_count"],
                counts["excess_count"],
                mismatch,
                "敞口/余额/隔离/流水状态同事务提交",
                run_id,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_settle_run(run_id)


def get_settle_run(run_id: int) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "归并运行不存在")
        return dict(row)


def run_verify(settle_run_id: int | None = None) -> dict:
    with connect() as conn:
        if settle_run_id is None:
            last = conn.execute("SELECT id FROM settle_runs ORDER BY id DESC LIMIT 1").fetchone()
            if not last:
                raise HTTPException(400, "尚无归并运行，无法互证")
            settle_run_id = last["id"]
        run = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (settle_run_id,)).fetchone()
        if not run:
            raise HTTPException(404, "归并运行不存在")

        # 已归并正向 − 已归并冲正
        fwd_sum = conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries
            WHERE kind = '正向' AND status = '已归并' AND settle_run_id = ?
            """,
            (settle_run_id,),
        ).fetchone()["s"]
        rev_sum = conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries
            WHERE kind = '冲正' AND status = '已归并' AND settle_run_id = ?
            """,
            (settle_run_id,),
        ).fetchone()["s"]
        ledger_net = fwd_sum - rev_sum
        balance_sum = conn.execute(
            "SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts WHERE last_settle_run_id = ?",
            (settle_run_id,),
        ).fetchone()["s"]
        # 账户余额合计可能含历史；底座约定：互证看「本轮触及账户余额合计」vs 本轮流水净额
        # 简化：用全部账户余额 vs 全部已归并净额（多轮后仍应一致若每轮覆盖写）
        all_bal = conn.execute("SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts").fetchone()["s"]
        q_count = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine_items WHERE settle_run_id = ?",
            (settle_run_id,),
        ).fetchone()["c"]

        # 全量已归并净额
        all_fwd = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries WHERE kind = '正向' AND status = '已归并'"
        ).fetchone()["s"]
        all_rev = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries WHERE kind = '冲正' AND status = '已归并'"
        ).fetchone()["s"]
        all_ledger_net = all_fwd - all_rev

        # 敞口口径：全部从敞口表 / 隔离表 / 流水状态派生，禁止内存计数
        snap = partial_cascade.snapshot(conn)
        exposure_mismatch = partial_cascade.mismatch_count(conn)
        excess_q = conn.execute(
            """
            SELECT COUNT(*) AS c FROM quarantine_items
            WHERE settle_run_id = ? AND reason = ?
            """,
            (settle_run_id, partial_cascade.EXCESS_REASON),
        ).fetchone()["c"]
        latest = conn.execute("SELECT MAX(id) AS m FROM settle_runs").fetchone()["m"]
        # 该轮落库的敞口派生值；仅对最新轮与当前敞口表逐分核对（旧轮为历史快照）
        run_snapshot_matches = (
            run["reversed_total_cents"] == snap["reversed_total_cents"]
            and run["open_cents"] == snap["open_cents"]
            and run["excess_quarantine_count"] == excess_q
            if settle_run_id == latest
            else True
        )
        # 敞口守恒：原额 − 已冲累计 = 剩余敞口，且已冲累计与已归并冲正同额
        consistent = (
            1
            if all_ledger_net == all_bal
            and exposure_mismatch == 0
            and snap["reversed_total_cents"] == all_rev
            and run_snapshot_matches
            else 0
        )
        detail = {
            "本轮正向分": fwd_sum,
            "本轮冲正分": rev_sum,
            "本轮流水净额分": ledger_net,
            "本轮触及余额合计分": balance_sum,
            "全量流水净额分": all_ledger_net,
            "全量账户余额分": all_bal,
            "本轮隔离数": q_count,
            "已冲累计分": run["reversed_total_cents"],
            "剩余敞口合计分": run["open_cents"],
            "当前剩余敞口合计分": snap["open_cents"],
            "闭口正向数": snap["closed_count"],
            "部分冲正向数": snap["partial_count"],
            "敞口不符数": exposure_mismatch,
            "本轮超额隔离笔数": excess_q,
            "最新轮敞口一致": 1 if run_snapshot_matches else 0,
        }
        now = _now()
        cur = conn.execute(
            """
            INSERT INTO verify_reports(
              settle_run_id, ledger_net_cents, balance_sum_cents,
              quarantine_count, consistent, detail_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settle_run_id,
                all_ledger_net,
                all_bal,
                q_count,
                consistent,
                json.dumps(detail, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
        rid = cur.lastrowid
        row = conn.execute("SELECT * FROM verify_reports WHERE id = ?", (rid,)).fetchone()
        out = dict(row)
        out["detail"] = detail
        return out
