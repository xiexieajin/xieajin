"""
初筛规则管理模块 - 规则的增删改查、条件解析、模板管理

小白讲解：这个文件是「规则系统」的大管家。
所有初筛规则（11条一票否决 + 6条评分规则）都通过这里管理：
- 查规则：list_rule_templates() 列出所有默认规则
- 改规则：update_rule_template() 修改规则参数（阈值/分值/开关）
- 存模板：save_as_template() 把当前规则配置保存成模板供下次复用
- 加模板：load_template() 加载之前保存的模板
- 解析条件：parse_condition() 把JSON条件转成可比较的结构
- 评估条件：evaluate_condition() 拿数据去匹配条件，返回是否命中

规则条件JSON格式见方案第7节，支持简单条件和复合条件，12种操作符。
"""

import json
import pymysql
from db import now_str
from config import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE


def _get_db_connection():
    """
    创建MySQL数据库连接（既能在Flask请求中使用，也能在后台线程中使用）

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


# ==================== 规则模板CRUD ====================

def list_rule_templates():
    """
    列出所有规则模板（按sort_order排序）

    小白讲解：从数据库读取全部17条默认规则，按排序顺序返回。
    前端规则配置页用这个函数展示一票否决规则区和评分规则区。

    返回：sqlite3.Row列表，每条包含rule_code/rule_name/rule_type/max_score等字段
    """
    conn = _get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM screening_rule_templates
        ORDER BY sort_order ASC
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_rule_template(rule_code):
    """
    获取单条规则模板

    参数：rule_code 规则编码，如 "veto_capital"
    返回：sqlite3.Row 或 None
    """
    conn = _get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM screening_rule_templates WHERE rule_code = %s", (rule_code,))
    row = cursor.fetchone()
    conn.close()
    return row


def update_rule_template(rule_code, updates):
    """
    更新规则模板的参数（阈值/分值/开关/启用状态）

    小白讲解：用户在规则配置页修改规则参数后，调用这个函数保存到数据库。
    只允许修改 is_enabled / default_condition / max_score / scoring_logic 这几个字段，
    规则编码和类型不能改。

    参数：
        rule_code: 规则编码
        updates: 要更新的字段字典，如 {"is_enabled": 0} 或 {"max_score": 20}
    返回：True=更新成功，False=规则不存在
    """
    # 允许更新的字段白名单（防止用户修改不该改的字段）
    allowed_fields = {"is_enabled", "default_condition", "default_action",
                      "max_score", "scoring_logic", "is_configurable", "sort_order"}

    conn = _get_db_connection()
    cursor = conn.cursor()

    # 先检查规则是否存在
    cursor.execute("SELECT id FROM screening_rule_templates WHERE rule_code = %s", (rule_code,))
    if not cursor.fetchone():
        conn.close()
        return False

    # 构造SET子句（只更新白名单内的字段）
    set_parts = []
    params = []
    for field, value in updates.items():
        if field in allowed_fields:
            set_parts.append(f"{field} = %s")
            params.append(value)

    if not set_parts:
        conn.close()
        return False

    set_parts.append("updated_at = %s")
    params.append(now_str())
    params.append(rule_code)

    cursor.execute(
        f"UPDATE screening_rule_templates SET {', '.join(set_parts)} WHERE rule_code = %s",
        params
    )
    conn.commit()
    conn.close()
    return True


# ==================== 规则实例管理（用于初筛执行）====================

def get_active_rules(user_id=None, template_name=None):
    """
    获取当前启用的规则集（供初筛引擎使用）

    小白讲解：初筛引擎执行时调用这个函数拿规则。
    - 如果传了 template_name，就用用户保存的模板参数（覆盖默认规则）
    - 没传 template_name，就用全局默认规则（screening_rule_templates 表的当前配置）

    参数：
        user_id: 用户ID（暂未使用，预留数据隔离）
        template_name: 模板名称，传了就用该模板的参数覆盖默认规则
    返回：规则字典列表，每个字典包含合并后的条件/分值/启用状态
    """
    conn = _get_db_connection()
    cursor = conn.cursor()
    # 永远从模板表读取所有规则作为基础（包含 is_enabled=0 的，因为模板可能改了启用状态）
    cursor.execute("""
        SELECT * FROM screening_rule_templates
        ORDER BY sort_order ASC
    """)
    rows = cursor.fetchall()
    conn.close()

    # 如果传了模板名，加载模板实例，按 rule_code 建索引，用于覆盖默认参数
    template_instances = {}
    if template_name:
        instances = load_template(user_id, template_name)
        template_instances = {inst["rule_code"]: inst for inst in instances}

    # 把sqlite3.Row转成字典，并解析JSON字段
    rules = []
    for row in rows:
        rule = dict(row)
        rule_code = rule["rule_code"]

        # 如果有模板实例覆盖，用模板的参数
        if rule_code in template_instances:
            inst = template_instances[rule_code]
            # 启用状态用模板的
            rule["is_enabled"] = inst.get("is_enabled", rule.get("is_enabled", 1))
            # 条件用模板的自定义条件（如果有），否则保持默认
            if inst.get("custom_condition"):
                rule["default_condition"] = inst["custom_condition"]
                if isinstance(rule["default_condition"], str):
                    try:
                        rule["default_condition"] = json.loads(rule["default_condition"])
                    except (json.JSONDecodeError, TypeError):
                        pass
            # 满分值/通过线用模板的（如果有）
            if inst.get("custom_score_cap") is not None:
                rule["max_score"] = inst["custom_score_cap"]

        # 解析条件JSON（如果还不是dict）
        if isinstance(rule.get("default_condition"), str):
            try:
                rule["default_condition"] = json.loads(rule["default_condition"])
            except (json.JSONDecodeError, TypeError):
                rule["default_condition"] = {}
        # 解析动作JSON
        try:
            rule["default_action"] = json.loads(row["default_action"]) if isinstance(row.get("default_action"), str) else (row.get("default_action") or {})
        except (json.JSONDecodeError, TypeError):
            rule["default_action"] = {}
        # 解析天眼查工具列表
        try:
            rule["tyc_commands"] = json.loads(row["tyc_commands"]) if isinstance(row.get("tyc_commands"), str) else (row.get("tyc_commands") or [])
        except (json.JSONDecodeError, TypeError):
            rule["tyc_commands"] = []

        # 只返回启用的规则（模板可能禁用了某些规则）
        if rule.get("is_enabled"):
            rules.append(rule)
    return rules


def get_veto_rules():
    """获取所有启用的一票否决规则"""
    return [r for r in get_active_rules() if r["rule_type"] == "veto"]


def get_score_rules():
    """获取所有启用的评分规则"""
    return [r for r in get_active_rules() if r["rule_type"] == "score"]


# ==================== 模板保存与加载 ====================

def save_as_template(user_id, template_name, rule_overrides):
    """
    把用户修改后的规则配置保存为模板（供下次复用）

    小白讲解：用户在规则配置页调好参数后，可以点"保存为模板"，
    系统把这次的所有规则参数存成一份模板。下次初筛时点"从模板加载"就能恢复这套配置。

    参数：
        user_id: 用户ID
        template_name: 模板名称，如"严格模式""宽松模式"
        rule_overrides: 规则覆盖列表，每项含 rule_code + 修改的字段
            如 [{"rule_code":"veto_capital","custom_condition":{...},"custom_score_cap":200,"is_enabled":1}]
    返回：新创建的模板实例数量
    """
    conn = _get_db_connection()
    cursor = conn.cursor()
    now = now_str()
    count = 0

    for override in rule_overrides:
        # 查模板ID
        cursor.execute("SELECT id FROM screening_rule_templates WHERE rule_code = %s",
                       (override.get("rule_code"),))
        row = cursor.fetchone()
        if not row:
            continue

        # 序列化自定义条件为JSON
        custom_condition = override.get("custom_condition")
        custom_condition_json = json.dumps(custom_condition, ensure_ascii=False) if custom_condition else None

        cursor.execute("""
            INSERT INTO screening_rule_instances
            (requirement_id, template_id, template_name, custom_condition,
             custom_score_cap, is_enabled, updated_at, user_id, created_at)
            VALUES (NULL, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (row["id"], template_name, custom_condition_json,
              override.get("custom_score_cap"), override.get("is_enabled", 1),
              now, user_id, now))
        count += 1

    conn.commit()
    conn.close()
    return count


def list_user_templates(user_id=None):
    """
    列出所有已保存的规则模板名称（所有用户共享，可共用）

    小白讲解：模板是"可共用配置"——管理员保存的模板，所有用户在初筛前都能选。
    所以这里不按 user_id 过滤，返回所有模板名。user_id 参数保留只是为了向后兼容。

    参数：user_id 已废弃（模板共享），保留参数仅为兼容旧调用
    返回：模板名称列表（去重），如 ["严格模式", "宽松模式"]
    """
    conn = _get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT template_name FROM screening_rule_instances
        WHERE requirement_id IS NULL AND template_name != ''
        ORDER BY template_name
    """)
    rows = cursor.fetchall()
    conn.close()
    return [row["template_name"] for row in rows]


def load_template(user_id, template_name):
    """
    加载模板，返回该模板下所有规则实例（模板所有用户共享）

    小白讲解：用户在初筛页选了某个模板后，调用这个函数拿到模板里的规则配置，
    然后用这些配置覆盖当前默认规则。
    模板是共享的（管理员保存后所有用户可选用），所以不按 user_id 过滤。
    user_id 参数保留只是为了向后兼容旧调用。

    参数：
        user_id: 已废弃（模板共享），保留参数仅为兼容旧调用
        template_name: 模板名称
    返回：规则实例字典列表
    """
    conn = _get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ri.*, rt.rule_code, rt.rule_name, rt.rule_type, rt.rule_category,
               rt.default_condition, rt.default_action, rt.max_score as default_max_score,
               rt.scoring_logic, rt.tyc_commands, rt.description
        FROM screening_rule_instances ri
        JOIN screening_rule_templates rt ON ri.template_id = rt.id
        WHERE ri.template_name = %s AND ri.requirement_id IS NULL
        ORDER BY rt.sort_order
    """, (template_name,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def delete_template(template_name):
    """
    删除模板（按模板名删除所有实例记录）

    小白讲解：一个模板由多条 screening_rule_instances 记录组成（每条规则一行），
    删除时按 template_name 删除所有相关记录。模板是共享的，不按 user_id 过滤。

    参数：template_name 模板名称
    返回：删除的记录数
    """
    conn = _get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        DELETE FROM screening_rule_instances
        WHERE template_name = %s AND requirement_id IS NULL
    """, (template_name,))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


# ==================== 条件JSON解析与评估 ====================

def parse_condition(condition_json):
    """
    解析条件JSON，返回标准化的条件结构

    小白讲解：规则条件用JSON表示，有两种格式：
    1. 简单条件：{"type":"single","field":"reg_capital_wan","operator":"lt","value":100}
    2. 复合条件：{"type":"composite","logic":"and","conditions":[简单条件1, 简单条件2]}

    这个函数把JSON字符串或字典统一转成字典格式，方便后续评估。

    参数：condition_json 可以是JSON字符串、字典、或None
    返回：标准化的条件字典，None表示无条件
    """
    if not condition_json:
        return None

    # 如果传入的是字符串，先解析成字典
    if isinstance(condition_json, str):
        try:
            condition_json = json.loads(condition_json)
        except json.JSONDecodeError:
            return None

    # 确保是字典
    if not isinstance(condition_json, dict):
        return None

    return condition_json


def evaluate_condition(condition, data):
    """
    评估条件是否满足（用data数据去匹配condition条件）

    小白讲解：这是规则引擎的核心。比如规则是"注册资本<100万"，
    这个函数拿供应商的实际注册资本数据去比较，返回True（命中规则）或False（未命中）。

    支持的12种操作符（方案7.3节）：
    - lt/lte/gt/gte：小于/小于等于/大于/大于等于
    - eq/neq：等于/不等于
    - in/not_in：值在/不在列表中
    - contains/not_contains：包含/不包含子串
    - is_null/is_not_null：字段为空/有值

    参数：
        condition: 条件字典（由parse_condition返回）
        data: 供应商数据字典，如 {"reg_capital_wan": 50, "operating_status": "存续"}
    返回：True=条件满足（命中规则），False=条件不满足
    """
    if not condition:
        return False

    cond_type = condition.get("type", "single")

    # 复合条件：递归评估每个子条件，按logic(and/or)组合
    if cond_type == "composite":
        logic = condition.get("logic", "and")
        sub_conditions = condition.get("conditions", [])
        if not sub_conditions:
            return False
        # and：所有子条件都满足才返回True
        if logic == "and":
            return all(evaluate_condition(sub, data) for sub in sub_conditions)
        # or：任一子条件满足就返回True
        elif logic == "or":
            return any(evaluate_condition(sub, data) for sub in sub_conditions)
        return False

    # 简单条件：取字段值，按操作符比较
    field = condition.get("field", "")
    operator = condition.get("operator", "eq")
    target_value = condition.get("value")

    # 从data中取字段值（不存在则为None）
    actual_value = data.get(field) if data else None

    return _apply_operator(actual_value, operator, target_value)


def _apply_operator(actual, operator, target):
    """
    按操作符比较两个值

    小白讲解：这是evaluate_condition的内部辅助函数，
    根据12种操作符分别做比较。

    参数：
        actual: 供应商的实际字段值
        operator: 操作符，如 "lt"/"eq"/"in"
        target: 规则中定义的目标值
    返回：True=满足条件，False=不满足
    """
    # is_null：字段为空或未返回
    if operator == "is_null":
        return actual is None or actual == "" or actual == []

    # is_not_null：字段有值
    if operator == "is_not_null":
        return actual is not None and actual != "" and actual != []

    # 以下操作符需要actual有值才能比较
    if actual is None:
        return False

    # 数值比较类（lt/lte/gt/gte）
    if operator in ("lt", "lte", "gt", "gte"):
        try:
            actual_num = float(actual)
            target_num = float(target)
            if operator == "lt":
                return actual_num < target_num
            elif operator == "lte":
                return actual_num <= target_num
            elif operator == "gt":
                return actual_num > target_num
            elif operator == "gte":
                return actual_num >= target_num
        except (ValueError, TypeError):
            return False

    # 等于
    if operator == "eq":
        # 布尔值特殊处理
        if isinstance(target, bool):
            return str(actual).lower() in ("true", "1", "yes") if target else str(actual).lower() in ("false", "0", "no", "")
        return str(actual) == str(target)

    # 不等于
    if operator == "neq":
        if isinstance(target, bool):
            return not (str(actual).lower() in ("true", "1", "yes") if target else str(actual).lower() in ("false", "0", "no", ""))
        return str(actual) != str(target)

    # 值在列表中
    if operator == "in":
        target_list = target if isinstance(target, list) else [target]
        return str(actual) in [str(t) for t in target_list]

    # 值不在列表中
    if operator == "not_in":
        target_list = target if isinstance(target, list) else [target]
        return str(actual) not in [str(t) for t in target_list]

    # 包含子串
    if operator == "contains":
        return str(target) in str(actual)

    # 不包含子串
    if operator == "not_contains":
        return str(target) not in str(actual)

    # 未知操作符，默认不满足
    return False


def validate_condition(condition_json):
    """
    验证条件JSON格式是否合法

    小白讲解：用户在规则编辑弹窗的JSON模式中手动编辑条件时，
    调用这个函数检查格式是否正确，防止存入无效数据。

    参数：condition_json 条件JSON字符串或字典
    返回：(是否合法, 错误信息) 元组，如 (True, "") 或 (False, "field字段不能为空")
    """
    condition = parse_condition(condition_json)
    if condition is None:
        return False, "条件为空或格式无效"

    cond_type = condition.get("type", "single")

    # 验证简单条件
    if cond_type == "single":
        if not condition.get("field"):
            return False, "简单条件缺少 field 字段"
        operator = condition.get("operator")
        valid_operators = {"lt", "lte", "gt", "gte", "eq", "neq",
                          "in", "not_in", "contains", "not_contains",
                          "is_null", "is_not_null"}
        if operator not in valid_operators:
            return False, f"无效的操作符：{operator}，支持的操作符：{valid_operators}"
        # is_null/is_not_null 不需要value，其他需要
        if operator not in ("is_null", "is_not_null") and "value" not in condition:
            return False, f"操作符 {operator} 需要 value 字段"
        return True, ""

    # 验证复合条件
    if cond_type == "composite":
        logic = condition.get("logic", "and")
        if logic not in ("and", "or"):
            return False, f"复合条件的 logic 只能是 and 或 or，当前为：{logic}"
        sub_conditions = condition.get("conditions", [])
        if not sub_conditions or not isinstance(sub_conditions, list):
            return False, "复合条件缺少 conditions 列表"
        # 递归验证每个子条件
        for i, sub in enumerate(sub_conditions):
            valid, msg = validate_condition(sub)
            if not valid:
                return False, f"第{i+1}个子条件无效：{msg}"
        return True, ""

    return False, f"未知的条件类型：{cond_type}，仅支持 single 或 composite"
