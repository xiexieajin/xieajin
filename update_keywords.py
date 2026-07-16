"""
批量更新数据库里已有需求的 P0-P3 关键词

小白讲解：
  之前 AI 生成的关键词太长（比如"1800mm茶色玻璃三抽屉三下翻门带插座灯带电视柜"），
  导致 1688 搜索把关键词拆散、搜回来一堆不相关的商品。
  现在改了 AI 的关键词生成规则（改成递减式短词），
  这个脚本遍历数据库里所有已确认的需求，重新调用 AI 生成新关键词并写回数据库。
"""
import sys
import os
import json
import pymysql

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ai_helper import _generate_summary_and_keywords
from config import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE


def rebuild_keywords_for_requirement(cur, req):
    """
    给单个需求重新生成关键词并写回数据库

    参数：
        cur: 数据库游标
        req: 需求记录字典（包含 product_name, core_functions, material 等字段）
    """
    req_id = req["id"]
    product_name = req["product_name"] or ""

    # 从原 keywords JSON 里把 confirmed 信息复原成 parsed dict
    # （_generate_summary_and_keywords 需要的是已确认的需求字段）
    old_kw_str = req["keywords"] or "{}"
    try:
        old_kw = json.loads(old_kw_str) if isinstance(old_kw_str, str) else old_kw_str
    except Exception:
        old_kw = {}

    # 把数据库里的字段拼成 parsed（_generate_summary_and_keywords 只用到这些字段）
    parsed = {
        "product_name": product_name,
        "core_functions": req["core_functions"] or "",
        "material": req["material"] or "",
        "spec_size": req["spec_size"] or "",
        "target_market": req["target_market"] or "",
        "required_certs": req["required_certs"] or "",
        "first_purchase_qty": req["first_purchase_qty"] or "",
        "acceptable_moq": req["acceptable_moq"] or "",
        "min_ship_qty": req["min_ship_qty"] or "",
    }

    print(f"\n>>> 需求 #{req_id}: {product_name[:40]}...")
    print(f"    旧 P0.cn: {old_kw.get('P0', {}).get('cn', '')[:50]}")

    # 调 AI 重新生成
    result = _generate_summary_and_keywords(parsed)
    if not result or "keywords" not in result:
        print(f"    ❌ AI 生成失败，跳过")
        return False

    new_kw = result["keywords"]
    new_p0 = new_kw.get("P0", {}).get("cn", "")
    print(f"    新 P0.cn: {new_p0}")

    # 校验：新关键词应该比旧的短（至少 P0 要短）
    old_p0 = old_kw.get("P0", {}).get("cn", "")
    if old_p0 and len(new_p0) >= len(old_p0):
        print(f"    ⚠️ 警告：新 P0 ({len(new_p0)}字) 没比旧 P0 ({len(old_p0)}字) 短")

    # 写回数据库（updated_at 用当前时间，不能传 NULL，否则 NOT NULL 约束会报错）
    from datetime import datetime
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "UPDATE requirements SET keywords = %s, requirement_summary = %s, updated_at = %s WHERE id = %s",
        (json.dumps(new_kw, ensure_ascii=False), result.get("requirement_summary", ""), now_str, req_id),
    )
    print(f"    ✅ 已更新")
    return True


def main():
    conn = pymysql.connect(
        host=MYSQL_HOST, port=MYSQL_PORT,
        user=MYSQL_USER, password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE, charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    cur = conn.cursor()

    # 查所有已确认的需求（keywords 不为空的就是已确认的）
    cur.execute("""
        SELECT id, product_name, keywords, core_functions, material, spec_size,
               target_market, required_certs, first_purchase_qty,
               acceptable_moq, min_ship_qty
        FROM requirements
        WHERE keywords IS NOT NULL AND keywords != '' AND keywords != '{}'
        ORDER BY id ASC
    """)
    reqs = cur.fetchall()

    print(f"=== 共 {len(reqs)} 个需求需要更新关键词 ===")

    success = 0
    failed = 0
    for req in reqs:
        try:
            if rebuild_keywords_for_requirement(cur, req):
                success += 1
            else:
                failed += 1
        except Exception as e:
            print(f"    ❌ 异常: {e}")
            failed += 1
        conn.commit()

    conn.close()
    print(f"\n=== 完成 ===")
    print(f"成功: {success} / 失败: {failed} / 总计: {len(reqs)}")


if __name__ == "__main__":
    main()
