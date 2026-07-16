"""
需求状态数据迁移脚本 - 把旧状态值升级到新的5种状态体系

小白讲解：以前需求状态只有3种（需求确认中/已确认/已完成），而且前端实际用的是"寻源中"，
导致数据库里状态值乱七八糟。现在改成5种（需求确认中/寻源中/初筛中/沟通中/已完成），
并和供应商开发阶段联动。这个脚本就是把历史数据迁移到新体系。

迁移规则：
  1. 先把旧状态值做名称映射：
       "已确认"   → "寻源中"（旧代码里"已确认"实际就是开始寻源的意思）
       "寻源中"   → "寻源中"（保持）
       "需求确认中" → "需求确认中"（保持）
       "已完成"   → "已完成"（保持）
       其他/空值  → "需求确认中"（兜底）
  2. 然后对每个需求调用 recalc_requirement_status，根据其供应商分布重算状态，
     这样历史数据也能准确反映"当前在哪个阶段"。

运行方式：
    python migrate_requirement_status.py
"""

import pymysql

from db import get_db, recalc_requirement_status, now_str


# 旧状态 → 新状态 的名称映射表
STATUS_MAP = {
    "已确认": "寻源中",
    "寻源中": "寻源中",
    "需求确认中": "需求确认中",
    "已完成": "已完成",
}


def migrate():
    """执行迁移：先映射旧状态名，再按供应商分布重算每个需求的状态"""
    conn = get_db()
    cursor = conn.cursor()

    # 第一步：查出所有需求及其当前状态
    cursor.execute("SELECT id, status FROM requirements ORDER BY id")
    requirements = cursor.fetchall()

    print(f"共找到 {len(requirements)} 条需求，开始迁移状态...\n")

    mapped_count = 0       # 名称映射发生变化的数量
    recalc_changed = 0     # 重算后状态发生变化的数量

    for req in requirements:
        rid = req["id"]
        old_status = req["status"] or ""

        # 1. 名称映射：把旧状态名统一到新体系
        mapped = STATUS_MAP.get(old_status, "需求确认中")
        if mapped != old_status:
            cursor.execute(
                "UPDATE requirements SET status=%s, updated_at=%s WHERE id=%s",
                (mapped, now_str(), rid)
            )
            mapped_count += 1
            print(f"  需求#{rid}: 名称映射 '{old_status}' -> '{mapped}'")

        # 2. 基于供应商分布重算状态（这一步会更新数据库并返回最新状态）
        final_status = recalc_requirement_status(cursor, rid)
        if final_status != mapped:
            recalc_changed += 1
            print(f"  需求#{rid}: 按供应商分布重算 '{mapped}' -> '{final_status}'")

    conn.commit()
    conn.close()

    print("\n========== 迁移完成 ==========")
    print(f"名称映射变化：{mapped_count} 条")
    print(f"重算后变化：  {recalc_changed} 条")
    print(f"总需求数：    {len(requirements)} 条")


if __name__ == "__main__":
    migrate()
