"""
数据迁移脚本：从 SQLite 导出到 MySQL

小白讲解：
  这个脚本是一次性工具，把原来存在 instance/sourcing.db（SQLite）里的业务数据，
  全部搬到 MySQL 的 sourcing_db 里。搬完会逐表对比行数，确保一条不丢。
  搬运过程中 SQLite 源文件完全不动，只是读出来写到 MySQL，所以就算搬错了也能重跑。

运行方法：
    python migrate_data.py

策略说明：
    MySQL 已经预置了一些初始数据（1个管理员、5个AI服务商、7个模型配置、
    2个搜索平台、17条初筛规则模板）。为了和 SQLite 源库完全对齐，
    这5张"有预置数据"的表采用"先清空再导入"的方式；
    其他纯业务表（需求/供应商/审计日志等）直接插入即可。
"""
import sqlite3
import pymysql
import sys
import os

sys.stdout.reconfigure(line_buffering=True)  # 让 print 立即输出，不缓冲

# 从 config.py 读取 MySQL 连接参数
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE

# SQLite 源数据库路径
SQLITE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "sourcing.db")

# 表名列表（按依赖顺序：先主表后子表，避免外键冲突）
TABLES = [
    "users",
    "ai_providers",
    "ai_model_configs",
    "search_platforms",
    "requirements",
    "suppliers",
    "screenings",
    "communications",
    "screening_rule_templates",
    "screening_rule_instances",
    "screening_audit_logs",
]

# 这5张表在 MySQL 里已经有预置数据，需要先清空再从 SQLite 导入
# （否则会和 SQLite 来的数据主键冲突）
PRESEEDED_TABLES = {
    "users",
    "ai_providers",
    "ai_model_configs",
    "search_platforms",
    "screening_rule_templates",
}

# 外键检查需要暂时关闭，否则 TRUNCATE 和按依赖顺序插入会报外键约束错误
SET_FK_OFF = "SET FOREIGN_KEY_CHECKS = 0"
SET_FK_ON = "SET FOREIGN_KEY_CHECKS = 1"


def main():
    """
    迁移主函数

    小白讲解：流程是
      1. 连上 SQLite 源库和 MySQL 目标库
      2. 关闭 MySQL 外键检查（避免清表/插入顺序被外键挡住）
      3. 逐表处理：有预置数据的先 TRUNCATE，再从 SQLite SELECT 出来 INSERT 到 MySQL
      4. 开回外键检查
      5. 逐表对比行数，输出对照表
    """
    # ==================== 1. 打开两个数据库连接 ====================
    if not os.path.exists(SQLITE_PATH):
        print(f"❌ 找不到 SQLite 源库: {SQLITE_PATH}")
        sys.exit(1)

    print(f"📂 SQLite 源库: {SQLITE_PATH}")
    print(f"🎯 MySQL 目标库: {MYSQL_USER}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}")
    print()

    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.row_factory = sqlite3.Row  # 让查询结果可以用列名取值
    sqlite_cur = sqlite_conn.cursor()

    mysql_conn = pymysql.connect(
        host=MYSQL_HOST, port=MYSQL_PORT,
        user=MYSQL_USER, password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE, charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    mysql_cur = mysql_conn.cursor()

    # ==================== 2. 关闭 MySQL 外键检查 ====================
    mysql_cur.execute(SET_FK_OFF)

    # ==================== 3. 逐表迁移 ====================
    migration_summary = []  # 记录每张表的迁移结果，最后统一打印对照表
    missing_columns_report = []  # 记录 MySQL 缺失的列，最后统一打印

    for table in TABLES:
        # 从 SQLite 读取全部数据
        try:
            sqlite_cur.execute(f"SELECT * FROM {table}")
            rows = sqlite_cur.fetchall()
        except sqlite3.OperationalError as e:
            # SQLite 里如果某张表不存在（比如 screenings 一直为空可能未建表），跳过
            print(f"⚠️  {table}: SQLite 中不存在或读取失败 ({e})，跳过")
            migration_summary.append((table, 0, "skip", 0))
            continue

        sqlite_count = len(rows)

        # 对有预置数据的表先清空 MySQL（TRUNCATE 比 DELETE 快，且重置自增ID）
        if table in PRESEEDED_TABLES:
            mysql_cur.execute(f"TRUNCATE TABLE {table}")

        if sqlite_count == 0:
            print(f"⏭️  {table}: SQLite 0条，仅清空 MySQL（如属预置表）")
            # 查一下 MySQL 现在多少条
            mysql_cur.execute(f"SELECT COUNT(*) AS cnt FROM {table}")
            mysql_count = mysql_cur.fetchone()["cnt"]
            migration_summary.append((table, sqlite_count, "ok", mysql_count))
            continue

        # 查询 MySQL 表的所有列名，做交集，避免"SQLite有MySQL没有"的列导致插入失败
        sqlite_columns = list(rows[0].keys())
        mysql_cur.execute("""
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        """, (MYSQL_DATABASE, table))
        mysql_column_set = {row["COLUMN_NAME"] for row in mysql_cur.fetchall()}

        # 只保留两边都有的列
        common_columns = [c for c in sqlite_columns if c in mysql_column_set]
        missing_in_mysql = [c for c in sqlite_columns if c not in mysql_column_set]
        if missing_in_mysql:
            print(f"   ⚠️  {table}: MySQL 缺失列 {missing_in_mysql}（已跳过这些列）")
            missing_columns_report.append((table, missing_in_mysql))

        col_names = ", ".join([f"`{c}`" for c in common_columns])
        placeholders = ", ".join(["%s"] * len(common_columns))
        insert_sql = f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})"

        # 逐行插入（数据量不大，单行插入足够，且方便定位错误）
        inserted = 0
        for row in rows:
            values = tuple(row[col] for col in common_columns)
            try:
                mysql_cur.execute(insert_sql, values)
                inserted += 1
            except pymysql.err.IntegrityError as e:
                # 主键冲突或唯一键冲突，打印出来便于排查
                print(f"   ❌ {table} 插入失败（主键/唯一键冲突）: {e}")
                continue

        mysql_conn.commit()
        print(f"✅ {table}: SQLite {sqlite_count}条 → MySQL 插入 {inserted}条")

        # 查 MySQL 实际行数做对照
        mysql_cur.execute(f"SELECT COUNT(*) AS cnt FROM {table}")
        mysql_count = mysql_cur.fetchone()["cnt"]
        migration_summary.append((table, sqlite_count, "ok", mysql_count))

    # ==================== 4. 开回外键检查 ====================
    mysql_cur.execute(SET_FK_ON)
    mysql_conn.commit()

    # ==================== 5. 输出行数对照表 ====================
    print("\n" + "=" * 60)
    print("数据迁移对照表（SQLite → MySQL）")
    print("=" * 60)
    print(f"{'表名':<32} {'SQLite':>8} {'MySQL':>8} {'结果':>6}")
    print("-" * 60)
    all_match = True
    for table, sc, status, mc in migration_summary:
        if status == "skip":
            result = "跳过"
            all_match = False
        elif sc == mc:
            result = "✅"
        else:
            result = "❌不一致"
            all_match = False
        print(f"{table:<32} {sc:>8} {mc:>8} {result:>6}")

    print("-" * 60)
    if all_match:
        print("🎉 全部表行数一致，迁移成功！")
    else:
        print("⚠️  存在不一致，请检查上方对照表")

    # ==================== 5.1 输出 MySQL 缺失列汇总 ====================
    if missing_columns_report:
        print("\n" + "=" * 60)
        print("⚠️  MySQL 缺失列汇总（需后续补 ALTER TABLE）")
        print("=" * 60)
        for table, cols in missing_columns_report:
            print(f"  {table}: {cols}")
        print("\n建议：在 db.py 的 init_db() 中为这些列添加 _add_column_if_not_exists()")
    else:
        print("\n✅ 所有列在 MySQL 中都存在，无缺失")

    # ==================== 6. 关闭连接 ====================
    mysql_cur.close()
    mysql_conn.close()
    sqlite_cur.close()
    sqlite_conn.close()


if __name__ == "__main__":
    main()
