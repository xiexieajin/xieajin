"""
初筛审计日志模块 - 记录每次初筛的完整执行轨迹

小白讲解：这个文件是初筛过程的"黑匣子记录仪"。
每次初筛都会产生多个步骤（查天眼查、匹配规则、AI判断等），
每个步骤的输入、输出、状态都会被记录下来，方便事后追溯和排查问题。

核心概念：
- run_id：一次初筛运行的全局唯一ID（UUID格式），关联本次所有供应商的所有步骤
- task_code：每个步骤的编码，如 tyc_registration_check / veto_capital / score_capital_scale
- status：步骤执行状态 success(成功) / fail(失败) / skip(跳过) / uncertain(不确定)

使用流程：
1. 初筛开始前调用 create_run_id() 拿到本次运行的ID
2. 每个步骤执行后调用 log_task() 记录结果
3. 初筛结束后调用 generate_audit_report() 生成报告
4. 排查问题时用 get_run_logs() 或 get_supplier_logs() 查历史记录
"""

import json
import uuid
import pymysql
import threading
from datetime import datetime, date
from decimal import Decimal
from db import now_str


# 小白讲解：自定义JSON编码器，处理数据库里Decimal/日期等特殊类型。
# 海关数据的customs_total_qty等字段是DECIMAL类型，默认json.dumps会报错
# "Object of type Decimal is not JSON serializable"，加这个编码器就能自动转成数字。
class _SafeJSONEncoder(json.JSONEncoder):
    """能序列化Decimal/date等数据库特殊类型的JSON编码器"""
    def default(self, o):
        if isinstance(o, Decimal):
            # Decimal转float，避免精度丢失过多；如果是整数就转int
            if o == o.to_integral_value():
                return int(o)
            return float(o)
        if isinstance(o, (date, datetime)):
            return o.isoformat()
        return super().default(o)


def _safe_json_dumps(obj):
    """安全序列化JSON，支持Decimal等数据库特殊类型"""
    if obj is None:
        return "{}"
    return json.dumps(obj, ensure_ascii=False, cls=_SafeJSONEncoder)
from config import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE


# 小白讲解：全局数据库写锁，用于串行化后台初筛线程的所有数据库写操作。
# 迁移到MySQL后并发写不再互相阻塞，但保留这把锁作为额外保险，避免极端情况下的竞争。
_db_write_lock = threading.Lock()


def _get_db_connection():
    """
    创建MySQL数据库连接（不依赖Flask的g.db，可在定时任务或后台线程使用）

    小白讲解：从SQLite迁移到MySQL后，用pymysql连接。DictCursor让查询结果
    可以用列名取值。autocommit=False保持手动提交事务。
    MySQL天然支持并发读写，不再需要SQLite的WAL和busy_timeout防锁配置。
    """
    conn = pymysql.connect(
        host=MYSQL_HOST, port=MYSQL_PORT,
        user=MYSQL_USER, password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE, charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    return conn


def create_run_id():
    """
    创建一次初筛运行的唯一批次ID

    小白讲解：每次点"开始初筛"时调用一次，拿到一个不重复的UUID字符串。
    后续这个run_id会贯穿本次初筛的所有供应商、所有步骤，方便事后查"某次初筛都做了什么"。

    返回：UUID字符串，如 "a3b4c5d6-e7f8-9012-abcd-ef0123456789"
    """
    return str(uuid.uuid4())


def log_task(run_id, supplier_id, task_code, task_name,
             input_data=None, result_data=None, evidence="",
             status="success", user_id=1):
    """
    记录单个步骤的执行日志

    小白讲解：初筛引擎每执行完一个步骤就调用这个函数，把"做了什么、输入了什么、
    得到了什么结果、证据从哪来"都记下来。如果某个供应商被否决了，可以反查是哪个步骤触发的。

    参数：
        run_id: 本次初筛运行的批次ID（来自 create_run_id()）
        supplier_id: 供应商ID
        task_code: 任务编码，如 "tyc_registration_check"/"veto_capital"/"score_capital_scale"
        task_name: 任务中文名，如"天眼查主体复核"/"注册资本一票否决"
        input_data: 输入数据快照（字典，会自动转JSON存储），如 {"company_name":"百度在线"}
        result_data: 执行结果（字典，会自动转JSON存储），如 {"passed":False,"capital_wan":50}
        evidence: 证据来源说明，如"天眼查MCP get_company_basic_profile 返回"
        status: 状态 success/fail/skip/uncertain
            - success：步骤执行成功
            - fail：步骤执行失败（如天眼查调用超时）
            - skip：步骤被跳过（如供应商已被前置否决，后续步骤跳过）
            - uncertain：结果不确定（如风险总览文本无法明确判断）
        user_id: 所属用户ID（数据隔离）

    返回：新插入的日志记录ID
    """
    # 小白讲解：用全局写锁串行化写操作，避免多连接同时写报 database is locked
    print(f"[诊断] log_task 准备获取写锁, task_code={task_code}, supplier_id={supplier_id}")
    with _db_write_lock:
        print(f"[诊断] log_task 已获取写锁, task_code={task_code}")
        conn = _get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO screening_audit_logs
                (run_id, supplier_id, task_code, task_name,
                 input_data, result_data, evidence, status, created_at, user_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            run_id,
            supplier_id,
            task_code,
            task_name,
            _safe_json_dumps(input_data) if input_data else "{}",
            _safe_json_dumps(result_data) if result_data else "{}",
            evidence,
            status,
            now_str(),
            user_id,
        ))
        conn.commit()
        log_id = cursor.lastrowid
        conn.close()
    return log_id


def get_run_logs(run_id, user_id=None):
    """
    查询某次初筛运行的所有审计日志

    小白讲解：排查问题时，用这个函数查"某次初筛的完整执行过程"。
    返回的日志按时间排序，可以看到每个供应商每个步骤的执行情况。

    参数：
        run_id: 初筛运行批次ID
        user_id: 可选，按用户过滤（普通用户只看自己的日志，管理员传None看全部）

    返回：日志记录列表（sqlite3.Row对象），按创建时间升序
    """
    conn = _get_db_connection()
    cursor = conn.cursor()
    if user_id is None:
        cursor.execute("""
            SELECT * FROM screening_audit_logs
            WHERE run_id = %s
            ORDER BY created_at ASC, id ASC
        """, (run_id,))
    else:
        cursor.execute("""
            SELECT * FROM screening_audit_logs
            WHERE run_id = %s AND user_id = %s
            ORDER BY created_at ASC, id ASC
        """, (run_id, user_id))
    logs = cursor.fetchall()
    conn.close()
    return logs


def get_supplier_logs(supplier_id, user_id=None, limit=100):
    """
    查询某个供应商的所有审计日志

    小白讲解：查看某家供应商的初筛历史，可能跨多次初筛运行。
    按"最新优先"排序，默认返回最近100条。

    参数：
        supplier_id: 供应商ID
        user_id: 可选，按用户过滤
        limit: 最多返回的记录数，默认100

    返回：日志记录列表，按创建时间降序
    """
    conn = _get_db_connection()
    cursor = conn.cursor()
    if user_id is None:
        cursor.execute("""
            SELECT * FROM screening_audit_logs
            WHERE supplier_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s
        """, (supplier_id, limit))
    else:
        cursor.execute("""
            SELECT * FROM screening_audit_logs
            WHERE supplier_id = %s AND user_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s
        """, (supplier_id, user_id, limit))
    logs = cursor.fetchall()
    conn.close()
    return logs


def generate_audit_report(run_id, user_id=None):
    """
    生成某次初筛运行的审计报告

    小白讲解：初筛结束后调用这个函数，生成一份汇总报告。
    报告包含：
    - 总体统计：处理了多少家供应商、成功/失败/跳过各多少
    - 按供应商分组的执行结果：每家供应商走了哪些步骤、最终结论
    - 异常步骤清单：哪些步骤失败了或结果不确定，需要人工复核

    参数：
        run_id: 初筛运行批次ID
        user_id: 可选，按用户过滤

    返回：审计报告字典，包含：
        - run_id: 运行批次ID
        - total_logs: 总日志条数
        - statistics: 状态统计 {success:N, fail:N, skip:N, uncertain:N}
        - suppliers: 按供应商分组的结果列表
        - anomalies: 异常步骤清单（status为fail或uncertain的记录）
    """
    logs = get_run_logs(run_id, user_id=user_id)

    # 状态统计
    statistics = {"success": 0, "fail": 0, "skip": 0, "uncertain": 0}
    for log in logs:
        status = log["status"]
        if status in statistics:
            statistics[status] += 1

    # 按供应商分组
    suppliers_map = {}
    for log in logs:
        sid = log["supplier_id"]
        if sid not in suppliers_map:
            suppliers_map[sid] = {
                "supplier_id": sid,
                "tasks": [],
                "has_fail": False,
                "has_uncertain": False,
                "has_veto": False,
            }
        entry = {
            "task_code": log["task_code"],
            "task_name": log["task_name"],
            "status": log["status"],
            "evidence": log["evidence"],
            "created_at": log["created_at"],
        }
        # 解析result_data里的关键字段
        try:
            result = json.loads(log["result_data"])
            if "passed" in result:
                entry["passed"] = result["passed"]
            if "reason" in result:
                entry["reason"] = result["reason"]
            if "score" in result:
                entry["score"] = result["score"]
            # 一票否决规则触发标记
            if log["task_code"].startswith("veto_") and result.get("passed") is False:
                suppliers_map[sid]["has_veto"] = True
        except (json.JSONDecodeError, TypeError):
            pass

        suppliers_map[sid]["tasks"].append(entry)
        if log["status"] == "fail":
            suppliers_map[sid]["has_fail"] = True
        if log["status"] == "uncertain":
            suppliers_map[sid]["has_uncertain"] = True

    # 异常步骤清单
    anomalies = []
    for log in logs:
        if log["status"] in ("fail", "uncertain"):
            anomalies.append({
                "supplier_id": log["supplier_id"],
                "task_code": log["task_code"],
                "task_name": log["task_name"],
                "status": log["status"],
                "evidence": log["evidence"],
                "created_at": log["created_at"],
            })

    return {
        "run_id": run_id,
        "total_logs": len(logs),
        "statistics": statistics,
        "suppliers": list(suppliers_map.values()),
        "anomalies": anomalies,
    }


def get_recent_runs(user_id=None, limit=20):
    """
    获取最近的初筛运行列表

    小白讲解：用于在页面上展示"最近初筛历史"。
    每条返回 run_id、开始时间、涉及供应商数、日志总数。

    参数：
        user_id: 可选，按用户过滤
        limit: 最多返回多少条运行记录，默认20

    返回：运行列表，每条包含 run_id / first_created / last_created / supplier_count / log_count
    """
    conn = _get_db_connection()
    cursor = conn.cursor()
    if user_id is None:
        cursor.execute("""
            SELECT
                run_id,
                MIN(created_at) as first_created,
                MAX(created_at) as last_created,
                COUNT(DISTINCT supplier_id) as supplier_count,
                COUNT(*) as log_count
            FROM screening_audit_logs
            GROUP BY run_id
            ORDER BY first_created DESC
            LIMIT %s
        """, (limit,))
    else:
        cursor.execute("""
            SELECT
                run_id,
                MIN(created_at) as first_created,
                MAX(created_at) as last_created,
                COUNT(DISTINCT supplier_id) as supplier_count,
                COUNT(*) as log_count
            FROM screening_audit_logs
            WHERE user_id = %s
            GROUP BY run_id
            ORDER BY first_created DESC
            LIMIT %s
        """, (user_id, limit))
    runs = cursor.fetchall()
    conn.close()
    return runs
