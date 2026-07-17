"""
数据迁移脚本：把供应商/初筛/审计日志/沟通记录的 user_id 归位到所属需求的所有者

小白讲解：以前管理员帮用户搜索供应商时，供应商的 user_id 写成了管理员自己的 ID，
导致用户看不到这些数据。这个脚本把所有数据的 user_id 改成"所属需求的所有者 user_id"，
这样数据归属就跟着需求走了，管理员帮用户操作的数据用户也能看到。

执行方法：python migrate_user_id_to_requirement_owner.py
可重复执行（幂等），已经是正确 user_id 的数据不会重复更新。
"""
import pymysql
from config import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE


def migrate():
    """执行数据归位迁移"""
    conn = pymysql.connect(
        host=MYSQL_HOST, port=MYSQL_PORT,
        user=MYSQL_USER, password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE, charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor, autocommit=False,
    )
    cursor = conn.cursor()

    # ==================== 1. suppliers 表归位 ====================
    # 小白讲解：把 suppliers.user_id 改成它关联的 requirements.user_id
    # 用 JOIN 找到每个供应商所属需求的所有者，把 user_id 更新成需求所有者
    print("[迁移1/4] 归位 suppliers.user_id ...")
    cursor.execute("""
        UPDATE suppliers s
        JOIN requirements r ON s.requirement_id = r.id
        SET s.user_id = r.user_id
        WHERE s.user_id != r.user_id
    """)
    suppliers_updated = cursor.rowcount
    conn.commit()
    print(f"  已更新 {suppliers_updated} 条供应商记录的 user_id")

    # ==================== 2. screenings 表归位 ====================
    # 小白讲解：初筛结果没有 requirement_id 字段，需要通过 supplier_id JOIN suppliers 再 JOIN requirements
    print("[迁移2/4] 归位 screenings.user_id ...")
    cursor.execute("""
        UPDATE screenings sc
        JOIN suppliers s ON sc.supplier_id = s.id
        JOIN requirements r ON s.requirement_id = r.id
        SET sc.user_id = r.user_id
        WHERE sc.user_id != r.user_id
    """)
    screenings_updated = cursor.rowcount
    conn.commit()
    print(f"  已更新 {screenings_updated} 条初筛记录的 user_id")

    # ==================== 3. screening_audit_logs 表归位 ====================
    # 小白讲解：审计日志表有 supplier_id 字段，通过它 JOIN 找到需求所有者
    print("[迁移3/4] 归位 screening_audit_logs.user_id ...")
    cursor.execute("""
        UPDATE screening_audit_logs al
        JOIN suppliers s ON al.supplier_id = s.id
        JOIN requirements r ON s.requirement_id = r.id
        SET al.user_id = r.user_id
        WHERE al.user_id != r.user_id
    """)
    audit_updated = cursor.rowcount
    conn.commit()
    print(f"  已更新 {audit_updated} 条审计日志的 user_id")

    # ==================== 4. communications 表归位 ====================
    # 小白讲解：沟通记录也通过 supplier_id JOIN 找到需求所有者
    print("[迁移4/4] 归位 communications.user_id ...")
    cursor.execute("""
        UPDATE communications c
        JOIN suppliers s ON c.supplier_id = s.id
        JOIN requirements r ON s.requirement_id = r.id
        SET c.user_id = r.user_id
        WHERE c.user_id != r.user_id
    """)
    comm_updated = cursor.rowcount
    conn.commit()
    print(f"  已更新 {comm_updated} 条沟通记录的 user_id")

    print()
    print("=" * 60)
    print("迁移完成！数据归属已全部对齐到所属需求的所有者。")
    print(f"  suppliers:           {suppliers_updated} 条")
    print(f"  screenings:          {screenings_updated} 条")
    print(f"  screening_audit_logs: {audit_updated} 条")
    print(f"  communications:      {comm_updated} 条")
    print("=" * 60)
    print()
    print("现在管理员帮用户在用户需求上操作产生的所有数据，用户都能完整看到了。")

    conn.close()


if __name__ == "__main__":
    migrate()
