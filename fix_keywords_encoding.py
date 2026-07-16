"""
一次性修复脚本：把数据库里 keywords 字段的 Unicode 转义（\\uXXXX）还原成中文

小白讲解：
  之前前端用 tojson 过滤器生成关键词隐藏域，默认会把中文转成 \\u5b9e\\u6728 这种编码。
  存到数据库后，编辑需求时就会看到一堆 \\uXXXX 而不是中文。
  这个脚本遍历所有需求，把 keywords 字段重新序列化成保留中文的格式，修复历史数据。

运行方法：
    python fix_keywords_encoding.py
"""
import sys
import os
import json
import pymysql

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE


def fix_one(cur, req):
    """
    修复单条需求的 keywords 字段

    参数：
        cur: 数据库游标
        req: 需求记录字典（包含 id 和 keywords 字段）
    返回：True 表示已修复，False 表示无需修复
    """
    req_id = req["id"]
    old_kw = req["keywords"]

    if not old_kw:
        return False

    # 尝试把字符串解析成Python对象，再用ensure_ascii=False重新序列化
    # 这样 \\u5b9e\\u6728 会被还原成 "实木"
    try:
        kw_obj = json.loads(old_kw)
    except (json.JSONDecodeError, TypeError):
        # 不是JSON格式（比如手动输入的逗号分隔关键词），不需要修复
        return False

    # 重新序列化，保留中文
    new_kw = json.dumps(kw_obj, ensure_ascii=False)

    # 如果新旧值一样，说明本来就是中文，不需要更新
    if new_kw == old_kw:
        return False

    # 写回数据库
    cur.execute(
        "UPDATE requirements SET keywords = %s WHERE id = %s",
        (new_kw, req_id),
    )
    print(f"  ✅ 需求 #{req_id}: 已修复")
    print(f"     修复前: {old_kw[:80]}...")
    print(f"     修复后: {new_kw[:80]}...")
    return True


def main():
    """主函数：连接数据库，遍历所有需求，修复 keywords 编码"""
    conn = pymysql.connect(
        host=MYSQL_HOST, port=MYSQL_PORT,
        user=MYSQL_USER, password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE, charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    cur = conn.cursor()

    # 查所有有 keywords 的需求
    cur.execute("""
        SELECT id, keywords
        FROM requirements
        WHERE keywords IS NOT NULL AND keywords != ''
        ORDER BY id ASC
    """)
    reqs = cur.fetchall()

    print(f"=== 共 {len(reqs)} 条需求待检查 ===\n")

    fixed = 0
    skipped = 0
    for req in reqs:
        try:
            if fix_one(cur, req):
                fixed += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"  ❌ 需求 #{req['id']} 异常: {e}")

    conn.commit()
    conn.close()

    print(f"\n=== 完成 ===")
    print(f"已修复: {fixed} / 无需修复: {skipped} / 总计: {len(reqs)}")


if __name__ == "__main__":
    main()
