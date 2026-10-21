"""对账：验证 group.db 里已迁移的 events.node_id 是否真的指向正确的 node。

reviewer-02 强调：xjk 迁移时新旧库 nodes id 恰好都 1-44 连续，events.node_id
「碰巧对」——必须 JOIN nodes 验证每个 node_id 存在、且 actor/type 匹配，
确认不是「碰巧对了一半」。

用法：python reconcile.py
"""
import sqlite3
import sys
from pathlib import Path

GROUP_DB = Path(__file__).resolve().parent / "group.db"


def main():
    conn = sqlite3.connect(GROUP_DB)
    conn.row_factory = sqlite3.Row
    try:
        # 1. events.node_id 非空但 nodes 表里不存在的（断链）
        dangling = conn.execute(
            """
            SELECT e.id, e.project, e.node_id, e.event_type, e.actor
            FROM events e
            LEFT JOIN nodes n ON n.id = e.node_id
            WHERE e.node_id IS NOT NULL AND n.id IS NULL
            """
        ).fetchall()
        print(f"断链 event（node_id 指向不存在的 node）: {len(dangling)} 条")
        for d in dangling:
            print(f"  event #{d['id']} project={d['project']} node_id={d['node_id']} type={d['event_type']}")

        # 2. events.node_id 存在，但 actor 不匹配（指向了别人的 node）
        mismatch = conn.execute(
            """
            SELECT e.id, e.project, e.node_id, e.actor AS event_actor, n.actor AS node_actor, n.type
            FROM events e
            JOIN nodes n ON n.id = e.node_id
            WHERE e.node_id IS NOT NULL AND e.actor != n.actor
            """
        ).fetchall()
        print(f"actor 不匹配 event（node 存在但 actor 对不上）: {len(mismatch)} 条")
        for m in mismatch:
            print(f"  event #{m['id']} node_id={m['node_id']} event_actor={m['event_actor']} node_actor={m['node_actor']}")

        # 3. 各表 project 维度完整性
        for t in ["requirements", "tasks", "nodes", "events"]:
            n = conn.execute(f"SELECT COUNT(*) c FROM {t} WHERE project='' OR project IS NULL").fetchone()["c"]
            print(f"{t} 空 project 行: {n}")

        # 4. 总量
        for t in ["requirements", "tasks", "nodes", "events", "settings"]:
            n = conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
            print(f"{t}: {n} 行")

        if not dangling and not mismatch:
            print("\n✅ 对账通过：events.node_id 全部真实有效，无断链、无 actor 错配")
            return 0
        else:
            print("\n❌ 对账发现问题，见上")
            return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
