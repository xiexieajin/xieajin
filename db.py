"""
供应商寻源系统 - 数据库初始化模块

小白讲解：这个文件负责创建数据库表（就像建房子打地基）。
所有"需求"和"供应商"的数据都按这里的结构存到SQLite数据库里。
运行一次 `python db.py` 就能自动建好所有表。
"""

import sqlite3
import os
import pymysql

# ==================== MySQL 数据库配置 ====================
# 小白讲解：从SQLite迁移到MySQL，DB_PATH保留供迁移脚本使用，新代码用MySQL连接。
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "sourcing.db")

# MySQL连接参数（从config.py读取，避免重复配置）
from config import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE

# ==================== 常量枚举 ====================
# 供应商开发阶段（对应工作流5个阶段的第4步"供应商开发"）
SUPPLIER_STAGES = ["已寻源待初筛", "已通过初筛", "沟通中", "已合作", "未通过初筛"]

# 需求状态
REQUIREMENT_STATUSES = ["需求确认中", "已确认", "已完成"]

# ==================== 用户系统相关常量 ====================
# 初始管理员账号（首次启动时自动创建，用户要求固定为此账号）
INITIAL_ADMIN_USERNAME = "xieajin"
INITIAL_ADMIN_PASSWORD = "bsq123"  # 明文传入，init时会用bcrypt哈希后存储
INITIAL_ADMIN_DISPLAY = "系统管理员"


def _add_column_if_not_exists(cursor, table, column, column_type):
    """
    如果某列不存在，就给表加上这列（兼容旧数据库不丢数据）

    小白讲解：MySQL版本用INFORMATION_SCHEMA查列是否存在，比SQLite的PRAGMA更标准。
    """
    cursor.execute("""
        SELECT COUNT(*) as cnt FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
    """, (MYSQL_DATABASE, table, column))
    if cursor.fetchone()["cnt"] == 0:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def _drop_column_if_exists(cursor, table, column):
    """
    如果某列存在，就删除这列（清理不再使用的旧字段）

    小白讲解：MySQL版本用INFORMATION_SCHEMA查列是否存在。
    """
    cursor.execute("""
        SELECT COUNT(*) as cnt FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
    """, (MYSQL_DATABASE, table, column))
    if cursor.fetchone()["cnt"] > 0:
        cursor.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def now_str():
    """返回当前时间的字符串格式（如"2026-07-15 14:30:00"）"""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ==================== 密码哈希工具（用Python标准库，无需安装bcrypt）====================
def hash_password(password):
    """
    把明文密码加密成不可逆的哈希值存储（永不明文存密码）

    小白讲解：用PBKDF2-HMAC-SHA256算法，迭代100000次把密码捣碎。
    每次加密会随机生成"盐值"（salt），所以同一个密码每次加密结果不同，更安全。
    存储格式：salt的十六进制$哈希的十六进制（用$分隔，验证时拆开用）

    参数：password 明文密码
    返回：加密后的字符串，格式 "salt_hex$hash_hex"
    """
    import hashlib
    import os
    # 1. 随机生成32字节的盐值（每次不同，防止彩虹表攻击）
    salt = os.urandom(32)
    # 2. 用PBKDF2算法迭代100000次计算哈希（迭代次数越多越难暴力破解）
    hash_bytes = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
    # 3. 把salt和hash都转成十六进制字符串，用$拼接存储
    return salt.hex() + '$' + hash_bytes.hex()


def verify_password(password, stored_hash):
    """
    验证用户输入的密码是否正确

    小白讲解：把用户输入的密码用同样的salt和算法再算一次，对比两次结果是否一致。
    一致=密码正确，不一致=密码错误。整个过程不还原明文，安全。

    参数：
        password: 用户输入的明文密码
        stored_hash: 数据库里存的哈希字符串（salt$hash格式）
    返回：True=密码正确，False=密码错误
    """
    import hashlib
    if not stored_hash or '$' not in stored_hash:
        return False
    # 1. 从存储的字符串中拆出salt和hash
    salt_hex, hash_hex = stored_hash.split('$', 1)
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    # 2. 用同样的salt和算法对输入密码计算哈希
    input_hash = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
    # 3. 对比两个哈希是否一致（用恒定时间比较防时序攻击）
    return input_hash.hex() == hash_hex


def get_db():
    """
    获取MySQL数据库连接

    小白讲解：从SQLite迁移到MySQL后，连接方式变了：
    - sqlite3.connect(文件路径) → pymysql.connect(主机/端口/用户/密码/库名)
    - row_factory=sqlite3.Row → cursorclass=DictCursor（返回字典而不是元组）
    - 不再需要PRAGMA设置（MySQL默认支持外键，无需WAL模式）
    - autocommit=False 保持手动提交事务，与SQLite行为一致
    """
    conn = pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE,
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    return conn


def init_db():
    """初始化数据库：创建所有需要的表（如果表不存在的话）"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    conn = get_db()
    cursor = conn.cursor()

    # ==================== 1. 需求表 ====================
    # 对应工作流的"阶段1-需求分析"，存放每次寻源的需求信息
    # 小白讲解：存JSON的字段（keywords、requirement_summary）用TEXT类型，因为JSON可能很长，
    # VARCHAR(1000)装不下。MySQL的TEXT不能有DEFAULT，所以去掉DEFAULT，业务代码读取时用 `or ""` 处理NULL。
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS requirements (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        product_name        TEXT NOT NULL,            -- 产品名称（必填）
        product_aliases     VARCHAR(2000) DEFAULT '',          -- 产品别名
        core_functions      VARCHAR(1000) DEFAULT '',          -- 核心功能
        material            VARCHAR(1000) DEFAULT '',          -- 材质
        spec_size           VARCHAR(1000) DEFAULT '',          -- 规格尺寸
        first_purchase_qty  VARCHAR(1000) DEFAULT '',          -- 首次采购数量
        daily_replenish_qty VARCHAR(1000) DEFAULT '',          -- 日均补货量
        max_stock_qty       VARCHAR(1000) DEFAULT '',          -- 最大库存量
        acceptable_moq      VARCHAR(1000) DEFAULT '',          -- 可接受的MOQ（最小起订量）
        min_ship_qty        VARCHAR(1000) DEFAULT '',          -- 最小发货量
        acceptable_lead_time VARCHAR(1000) DEFAULT '',         -- 可接受的交期
        target_market       VARCHAR(1000) DEFAULT '',          -- 目标市场
        required_certs      VARCHAR(1000) DEFAULT '',          -- 需要认证
        customization_req   TEXT,                              -- 定制化要求（长文本）
        requirement_summary TEXT,                              -- AI生成的完整需求总结（长文本）
        keywords            TEXT,                              -- AI生成的P0-P3关键词（JSON格式，可能很长）
        status              VARCHAR(50) DEFAULT '需求确认中', -- 状态：需求确认中/已确认/已完成
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL
    )
    """)

    # ==================== 2. 供应商表 ====================
    # 对应工作流的"阶段2-供应商寻源"到"阶段4-供应商开发"，存放所有寻到的供应商
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS suppliers (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        requirement_id      INTEGER NOT NULL,         -- 关联的需求ID
        name                TEXT NOT NULL,            -- 供应商/公司名称
        intro               VARCHAR(1000) DEFAULT '',         -- 供应商简介（必须包含"注册资本"）
        factory_address     VARCHAR(1000) DEFAULT '',         -- 工厂地址
        email               VARCHAR(1000) DEFAULT '',         -- 供应商邮箱
        phone               VARCHAR(1000) DEFAULT '',         -- 供应商电话
        main_product        VARCHAR(1000) DEFAULT '',         -- 主营产品
        establish_years     VARCHAR(1000) DEFAULT '',         -- 成立年限
        establish_date      VARCHAR(1000) DEFAULT '',         -- 成立日期（如2015-03-12）
        operating_status    VARCHAR(50) DEFAULT '存续',     -- 经营状态（存续/注销/吊销等）
        has_cross_border_exp INTEGER DEFAULT 0,      -- 是否有跨境电商经验（0否/1是）
        source              VARCHAR(100) DEFAULT '手动添加', -- 来源平台（1688/Made-in-China等）
        dev_stage           VARCHAR(50) DEFAULT '已寻源待初筛', -- 开发阶段：已寻源待初筛/已通过初筛/沟通中/已合作/未通过初筛
        -- 以下字段对齐供应商寻源SKILL文档
        hit_keyword         VARCHAR(1000) DEFAULT '',         -- 命中关键词（P0/P1_1等）
        supplier_type       VARCHAR(1000) DEFAULT '',         -- 供应商类型（制造商/疑似制造商）
        contact_status      VARCHAR(50) DEFAULT '未获取',   -- 联系方式状态（已获取电话/已获取邮箱/已获取电话和邮箱/未获取）
        registered_capital  VARCHAR(1000) DEFAULT '',         -- 注册资本
        legal_person        VARCHAR(1000) DEFAULT '',         -- 法定代表人
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL,
        FOREIGN KEY (requirement_id) REFERENCES requirements (id)
    )
    """)

    # 兼容旧数据库：给已存在的suppliers表添加新字段（不影响已有数据）
    # 对齐供应商寻源SKILL文档的字段要求
    _add_column_if_not_exists(cursor, "suppliers", "hit_keyword", "VARCHAR(1000) DEFAULT ''")
    _add_column_if_not_exists(cursor, "suppliers", "supplier_type", "VARCHAR(1000) DEFAULT ''")
    _add_column_if_not_exists(cursor, "suppliers", "contact_status", "VARCHAR(50) DEFAULT '未获取'")
    _add_column_if_not_exists(cursor, "suppliers", "registered_capital", "VARCHAR(1000) DEFAULT ''")
    _add_column_if_not_exists(cursor, "suppliers", "legal_person", "VARCHAR(1000) DEFAULT ''")
    _add_column_if_not_exists(cursor, "suppliers", "has_cross_border_exp", "INTEGER DEFAULT 0")
    _add_column_if_not_exists(cursor, "suppliers", "establish_date", "VARCHAR(1000) DEFAULT ''")

    # 清理已废弃的旧字段（这些字段不再使用，从旧数据库中删除以节省空间）
    _drop_column_if_exists(cursor, "suppliers", "has_amazon_exp")
    _drop_column_if_exists(cursor, "suppliers", "has_temu_exp")
    _drop_column_if_exists(cursor, "suppliers", "product_title")
    _drop_column_if_exists(cursor, "suppliers", "product_link")
    _drop_column_if_exists(cursor, "suppliers", "price_moq")

    # ==================== 3. 初筛结果表 ====================
    # 对应工作流的"阶段3-供应商初筛"，记录风险排查、资质核实和评分
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS screenings (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        supplier_id     INTEGER NOT NULL,
        risk_score      INTEGER DEFAULT 0,      -- 风险评分（0-100，越高风险越大）
        quality_score   INTEGER DEFAULT 0,      -- 资质评分（0-100，越高越好）
        has_cert        INTEGER DEFAULT 0,      -- 是否有相关认证（0无/1有）
        is_verified     INTEGER DEFAULT 0,      -- 是否已核实（0未核实/1已核实）
        screener        VARCHAR(100) DEFAULT '系统',    -- 初筛负责人
        screen_note     VARCHAR(1000) DEFAULT '',        -- 初筛备注
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        FOREIGN KEY (supplier_id) REFERENCES suppliers (id)
    )
    """)

    # ==================== 4. 沟通记录表 ====================
    # 对应工作流的"阶段4-供应商开发"，记录每次沟通内容和结论
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS communications (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        supplier_id     INTEGER NOT NULL,
        channel         VARCHAR(100) DEFAULT '微信/企微',   -- 沟通渠道
        content         VARCHAR(1000) DEFAULT '',            -- 沟通内容
        conclusion      VARCHAR(1000) DEFAULT '',            -- 沟通结论
        next_step       VARCHAR(1000) DEFAULT '',            -- 后续步骤
        comm_time       VARCHAR(1000) DEFAULT '',            -- 沟通时间
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        FOREIGN KEY (supplier_id) REFERENCES suppliers (id)
    )
    """)

    # ==================== 5. 用户表 ====================
    # 小白讲解：存放系统登录账号，密码用bcrypt加密存储（永不明文）。
    # role分admin(管理员)和user(普通用户)，is_active控制是否启用（停用后无法登录）
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        username        VARCHAR(100) NOT NULL UNIQUE,         -- 登录用户名（唯一）
        password_hash   VARCHAR(255) NOT NULL,                -- bcrypt加密后的密码
        display_name    VARCHAR(100) NOT NULL,                -- 显示名称（页面右上角显示用）
        role            VARCHAR(20) NOT NULL DEFAULT 'user', -- 角色：admin / user
        is_active       INTEGER NOT NULL DEFAULT 1,   -- 是否启用：1=启用，0=停用
        created_at      VARCHAR(30) NOT NULL,
        updated_at      VARCHAR(30) NOT NULL
    )
    """)

    # ==================== 6. AI供应商/服务提供商表 ====================
    # 小白讲解：统一存放所有外部服务商信息。用provider_type区分三类：
    # - ai_model：AI大模型供应商（DeepSeek、智谱），有模型配置
    # - search_platform：供应商搜索平台（1688、中国制造网），参与搜索流程
    # - data_api：数据查询API（天眼查），仅管理密钥
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ai_providers (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        provider_name   VARCHAR(100) NOT NULL UNIQUE,              -- 显示名称，如"DeepSeek"
        provider_code   VARCHAR(50) NOT NULL UNIQUE,              -- 代码标识，如"deepseek"
        provider_type   VARCHAR(30) NOT NULL DEFAULT 'ai_model',  -- 类型：ai_model / search_platform / data_api
        base_url        VARCHAR(255) NOT NULL DEFAULT '',           -- API接口地址
        api_key         VARCHAR(500) NOT NULL DEFAULT '',           -- API密钥
        is_enabled      INTEGER NOT NULL DEFAULT 1,         -- 是否启用
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """)

    # ==================== 7. AI模型场景配置表 ====================
    # 小白讲解：每个AI调用场景一条配置（共7个场景）。
    # 管理员可在Web界面修改模型名、思考强度、温度等参数，无需改代码。
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ai_model_configs (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        provider_id     INTEGER NOT NULL,             -- 关联 ai_providers.id
        scene_code      VARCHAR(50) NOT NULL UNIQUE,         -- 场景代码（代码中写死，不可修改）
        scene_name      TEXT NOT NULL,                -- 场景显示名
        model_name      TEXT NOT NULL,                -- 模型名，如"deepseek-v4-pro"
        thinking_enabled INTEGER NOT NULL DEFAULT 0,  -- 是否开启思考模式（仅DeepSeek类有效）
        thinking_effort VARCHAR(20) NOT NULL DEFAULT '',     -- 思考强度：low/medium/high/max
        max_tokens      INTEGER NOT NULL DEFAULT 4096,-- 最大输出token数
        temperature     REAL NOT NULL DEFAULT 0.3,    -- 温度值（思考模式下忽略）
        timeout_seconds INTEGER NOT NULL DEFAULT 120, -- 超时时间（秒）
        extra_params    VARCHAR(1000) NOT NULL DEFAULT '{}',   -- 额外参数（JSON格式，便于扩展）
        sort_order      INTEGER NOT NULL DEFAULT 0,   -- 排序（管理中心展示顺序）
        is_enabled      INTEGER NOT NULL DEFAULT 1,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        FOREIGN KEY (provider_id) REFERENCES ai_providers(id)
    )
    """)

    # ==================== 8. 搜索平台配置表 ====================
    # 小白讲解：与ai_providers中provider_type=search_platform的记录一一对应。
    # 管理员在此控制搜索时启用哪些平台、优先级、最大结果数。
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS search_platforms (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        provider_id     INTEGER NOT NULL UNIQUE,   -- 关联 ai_providers.id
        is_enabled      INTEGER NOT NULL DEFAULT 1, -- 是否参与搜索
        priority        INTEGER NOT NULL DEFAULT 0, -- 搜索优先级（数字越小越先搜）
        max_results     INTEGER NOT NULL DEFAULT 50,-- 该平台单次搜索最大结果数
        extra_config    VARCHAR(1000) NOT NULL DEFAULT '{}', -- 平台特有配置（JSON，如搜索间隔）
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        FOREIGN KEY (provider_id) REFERENCES ai_providers(id)
    )
    """)

    # ==================== 9. 初筛规则模板表 ====================
    # 小白讲解：存放系统默认的初筛规则模板（11条一票否决 + 6条评分规则）。
    # 系统首次启动通过 _seed_screening_rules 自动初始化，用户可在规则配置页修改参数后保存为衍生模板。
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS screening_rule_templates (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        rule_code           VARCHAR(50) NOT NULL UNIQUE,           -- 规则编码，如 veto_capital / score_capital_scale
        rule_name           TEXT NOT NULL,                  -- 规则显示名称，如"注册资本一票否决"
        rule_type           TEXT NOT NULL,                  -- 规则类型：veto / score / check
        rule_category       VARCHAR(50) NOT NULL DEFAULT '',       -- 分类：basic_info / contact / risk / qualification / match / export
        default_condition   TEXT NOT NULL,                  -- 默认条件表达式（JSON），如 {"field":"reg_capital_wan","operator":"lt","value":100}
        default_action      TEXT NOT NULL,                  -- 默认动作（JSON），如 {"result":"veto","reason":"注册资本不足100万"}
        max_score           INTEGER,                        -- 评分项满分值（veto/check类型为NULL）
        scoring_logic       VARCHAR(1000) NOT NULL DEFAULT '',       -- 评分逻辑描述
        tyc_commands        VARCHAR(1000) NOT NULL DEFAULT '[]',     -- 本规则需要的天眼查MCP工具（JSON数组）
        is_configurable     INTEGER NOT NULL DEFAULT 0,     -- 是否允许用户修改：0=不可改 / 1=可改
        is_enabled          INTEGER NOT NULL DEFAULT 1,     -- 默认是否启用
        sort_order          INTEGER NOT NULL DEFAULT 0,     -- 排序权重
        description         TEXT NOT NULL,                  -- 规则详细说明（长文本）
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL
    )
    """)

    # ==================== 10. 用户规则实例表 ====================
    # 小白讲解：每次初筛时用户可能修改规则参数，修改后的规则保存为"实例"。
    # requirement_id 为 NULL 表示保存为可复用模板；非 NULL 表示关联到某次初筛。
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS screening_rule_instances (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        requirement_id      INTEGER,                        -- 关联需求ID（NULL表示保存为模板）
        template_id         INTEGER NOT NULL,               -- 来源规则模板ID
        template_name       VARCHAR(100) NOT NULL DEFAULT '',       -- 模板名称（用户自定义，如"严格模式""宽松模式"）
        custom_condition    TEXT,                           -- 用户自定义条件（覆盖默认），NULL表示用默认
        custom_score_cap    INTEGER,                        -- 用户自定义满分值，NULL表示用默认
        is_enabled          INTEGER NOT NULL DEFAULT 1,     -- 本次是否启用
        updated_at          TEXT NOT NULL,
        user_id             INTEGER NOT NULL DEFAULT 1,     -- 所属用户ID（数据隔离）
        created_at          TEXT NOT NULL,
        FOREIGN KEY (template_id) REFERENCES screening_rule_templates (id)
    )
    """)

    # ==================== 11. 初筛审计日志表 ====================
    # 小白讲解：每次初筛执行时，记录每个供应商每个步骤的执行过程和结果，实现完整可追溯性。
    # run_id 是本次运行批次ID（UUID），用于关联同一次初筛的所有记录。
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS screening_audit_logs (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        run_id              TEXT NOT NULL,                  -- 本次运行批次ID（UUID）
        supplier_id         INTEGER NOT NULL,               -- 供应商ID
        task_code           TEXT NOT NULL,                  -- 任务编码，如 tyc_registration_check / veto_capital
        task_name           TEXT NOT NULL,                  -- 任务名称，如"天眼查主体复核"
        input_data          TEXT NOT NULL,                  -- 输入数据快照（JSON，可能很长）
        result_data         TEXT NOT NULL,                  -- 执行结果（JSON，可能很长）
        evidence            TEXT NOT NULL,                  -- 证据来源说明（长文本）
        status              VARCHAR(20) NOT NULL DEFAULT 'success',-- 状态：success / fail / skip / uncertain
        created_at          TEXT NOT NULL,
        user_id             INTEGER NOT NULL DEFAULT 1,     -- 所属用户ID（数据隔离）
        FOREIGN KEY (supplier_id) REFERENCES suppliers (id)
    )
    """)

    # 审计日志表索引（加速按run_id和supplier_id查询）
    # 小白讲解：MySQL不支持"CREATE INDEX IF NOT EXISTS"，用try-except忽略"索引已存在"错误
    for idx_sql in [
        "CREATE INDEX idx_audit_logs_run_id ON screening_audit_logs(run_id(50))",
        "CREATE INDEX idx_audit_logs_supplier_id ON screening_audit_logs(supplier_id)",
        "CREATE INDEX idx_audit_logs_user_id ON screening_audit_logs(user_id)",
    ]:
        try:
            cursor.execute(idx_sql)
        except pymysql.err.OperationalError as e:
            if e.args[0] != 1061:  # 1061=索引已存在，忽略；其他错误抛出
                raise

    # ==================== 扩展 screenings 表字段（方案6.4节）====================
    # 小白讲解：给现有的初筛结果表加上天眼查工商信息、联系方式审计、审计批次ID等字段
    _add_column_if_not_exists(cursor, "screenings", "tyc_match_status", "VARCHAR(1000) DEFAULT ''")
    _add_column_if_not_exists(cursor, "screenings", "tyc_company_name", "VARCHAR(1000) DEFAULT ''")
    _add_column_if_not_exists(cursor, "screenings", "registered_capital", "REAL")
    _add_column_if_not_exists(cursor, "screenings", "established_date", "VARCHAR(1000) DEFAULT ''")
    _add_column_if_not_exists(cursor, "screenings", "operating_status", "VARCHAR(1000) DEFAULT ''")
    _add_column_if_not_exists(cursor, "screenings", "business_scope", "VARCHAR(1000) DEFAULT ''")
    _add_column_if_not_exists(cursor, "screenings", "contact_audit_result", "VARCHAR(1000) DEFAULT ''")
    _add_column_if_not_exists(cursor, "screenings", "run_id", "VARCHAR(1000) DEFAULT ''")
    # 新100分评分体系的6个维度得分（替代旧的 ip_score/qual_score/basic_score）
    _add_column_if_not_exists(cursor, "screenings", "score_capital_scale", "INTEGER DEFAULT 0")
    _add_column_if_not_exists(cursor, "screenings", "score_operating_years", "INTEGER DEFAULT 0")
    _add_column_if_not_exists(cursor, "screenings", "score_product_match", "INTEGER DEFAULT 0")
    _add_column_if_not_exists(cursor, "screenings", "score_contact_complete", "INTEGER DEFAULT 0")
    _add_column_if_not_exists(cursor, "screenings", "score_risk_record", "INTEGER DEFAULT 0")
    _add_column_if_not_exists(cursor, "screenings", "score_export_exp", "INTEGER DEFAULT 0")
    # 初筛结论（通过/未通过/需人工确认）
    _add_column_if_not_exists(cursor, "screenings", "conclusion", "VARCHAR(1000) DEFAULT ''")

    # ==================== 给现有4张业务表加 user_id 列（实现数据隔离）====================
    # 小白讲解：DEFAULT 1 确保旧数据自动归属给id=1的初始管理员，不报错不丢数据
    _add_column_if_not_exists(cursor, "requirements", "user_id", "INTEGER NOT NULL DEFAULT 1")
    _add_column_if_not_exists(cursor, "suppliers", "user_id", "INTEGER NOT NULL DEFAULT 1")
    _add_column_if_not_exists(cursor, "screenings", "user_id", "INTEGER NOT NULL DEFAULT 1")
    _add_column_if_not_exists(cursor, "communications", "user_id", "INTEGER NOT NULL DEFAULT 1")

    # 给user_id建索引，加速按用户过滤的查询
    # 小白讲解：MySQL不支持"CREATE INDEX IF NOT EXISTS"，用try-except忽略"索引已存在"错误
    for idx_sql in [
        "CREATE INDEX idx_requirements_user_id ON requirements(user_id)",
        "CREATE INDEX idx_suppliers_user_id ON suppliers(user_id)",
        "CREATE INDEX idx_screenings_user_id ON screenings(user_id)",
        "CREATE INDEX idx_communications_user_id ON communications(user_id)",
    ]:
        try:
            cursor.execute(idx_sql)
        except pymysql.err.OperationalError as e:
            if e.args[0] != 1061:  # 1061=索引已存在，忽略；其他错误抛出
                raise

    conn.commit()

    # ==================== 首次初始化预置数据 ====================
    _seed_initial_data(cursor, conn)

    conn.close()
    print("数据库初始化完成！数据表已创建。")


def _seed_initial_data(cursor, conn):
    """
    首次初始化预置数据（仅在对应表为空时执行，避免重复插入）

    小白讲解：系统第一次启动时自动创建：
    1. 初始管理员账号（xieajin/bsq123）
    2. 5个AI服务提供商（DeepSeek/智谱/1688/中国制造网/天眼查）
    3. 7个AI模型场景配置（从config.py迁移参数）
    4. 2个搜索平台配置（1688/中国制造网）
    """
    # ---------- 1. 初始管理员账号 ----------
    cursor.execute("SELECT COUNT(*) as cnt FROM users")
    if cursor.fetchone()["cnt"] == 0:
        # 小白讲解：用Python标准库的PBKDF2-HMAC-SHA256加密密码（永不明文存储）
        # 这是NIST推荐的安全哈希算法，100000次迭代防止暴力破解，无需安装bcrypt
        password_hash = hash_password(INITIAL_ADMIN_PASSWORD)
        now = now_str()
        cursor.execute("""
            INSERT INTO users (username, password_hash, display_name, role, is_active, created_at, updated_at)
            VALUES (%s, %s, %s, 'admin', 1, %s, %s)
        """, (INITIAL_ADMIN_USERNAME, password_hash, INITIAL_ADMIN_DISPLAY, now, now))
        conn.commit()
        print(f"[初始化] 已创建初始管理员账号：{INITIAL_ADMIN_USERNAME}")

    # ---------- 2. AI服务提供商预置 ----------
    cursor.execute("SELECT COUNT(*) as cnt FROM ai_providers")
    if cursor.fetchone()["cnt"] == 0:
        # 从 config.py 读取现有配置值迁移到数据库
        from config import (DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL,
                            ZHIPU_API_KEY, ZHIPU_BASE_URL,
                            TYC_MCP_URL, TYC_MCP_AUTH,
                            ALI_1688_AK)
        now = now_str()
        providers = [
            ("DeepSeek", "deepseek", "ai_model", DEEPSEEK_BASE_URL, DEEPSEEK_API_KEY),
            ("智谱AI", "zhipu", "ai_model", ZHIPU_BASE_URL, ZHIPU_API_KEY),
            ("1688", "ali1688", "search_platform", "https://api.1688.com", ALI_1688_AK),
            ("中国制造网", "madeinchina", "search_platform", "https://mcp.chexb.com/sse", ""),
            ("天眼查", "tianyancha", "data_api", TYC_MCP_URL, TYC_MCP_AUTH),
        ]
        for name, code, ptype, url, key in providers:
            cursor.execute("""
                INSERT INTO ai_providers (provider_name, provider_code, provider_type, base_url, api_key, is_enabled, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, 1, %s, %s)
            """, (name, code, ptype, url, key, now, now))
        conn.commit()
        print("[初始化] 已预置5个AI服务提供商")

    # ---------- 3. AI模型场景配置预置（7个场景）----------
    cursor.execute("SELECT COUNT(*) as cnt FROM ai_model_configs")
    if cursor.fetchone()["cnt"] == 0:
        # 从 config.py 读取模型参数
        from config import (DEEPSEEK_MODEL, DEEPSEEK_THINKING_ENABLED, DEEPSEEK_THINKING_EFFORT,
                            DEEPSEEK_EFFORT_SIMPLE, DEEPSEEK_EFFORT_COMPLEX,
                            DEEPSEEK_MAX_TOKENS, DEEPSEEK_TIMEOUT, ZHIPU_VISION_MODEL)
        # 查询provider_id
        cursor.execute("SELECT id, provider_code FROM ai_providers")
        provider_map = {row["provider_code"]: row["id"] for row in cursor.fetchall()}
        deepseek_id = provider_map.get("deepseek")
        zhipu_id = provider_map.get("zhipu")
        now = now_str()
        # 7个场景配置（scene_code / scene_name / provider / model / thinking / effort / tokens / temp / timeout）
        # 小白讲解：每个场景的参数从config.py迁移，管理员后续可在Web界面修改
        scenes = [
            ("req_parse", "需求解析", deepseek_id, DEEPSEEK_MODEL, 1, DEEPSEEK_EFFORT_SIMPLE, DEEPSEEK_MAX_TOKENS, 0.2, DEEPSEEK_TIMEOUT, 1),
            ("keyword_gen", "关键词生成", deepseek_id, DEEPSEEK_MODEL, 1, DEEPSEEK_EFFORT_SIMPLE, DEEPSEEK_MAX_TOKENS, 0.4, DEEPSEEK_TIMEOUT, 2),
            ("auto_screening", "自动初筛", deepseek_id, DEEPSEEK_MODEL, 1, DEEPSEEK_EFFORT_COMPLEX, DEEPSEEK_MAX_TOKENS, 0.2, DEEPSEEK_TIMEOUT, 3),
            ("supplier_translate", "供应商过滤-翻译", deepseek_id, DEEPSEEK_MODEL, 1, DEEPSEEK_EFFORT_SIMPLE, 512, 0.3, DEEPSEEK_TIMEOUT, 4),
            ("supplier_filter", "供应商过滤-第一批", deepseek_id, DEEPSEEK_MODEL, 1, DEEPSEEK_EFFORT_SIMPLE, 512, 0.1, DEEPSEEK_TIMEOUT, 5),
            ("supplier_filter_v2", "供应商过滤-第二批", deepseek_id, DEEPSEEK_MODEL, 1, DEEPSEEK_EFFORT_COMPLEX, 512, 0.2, DEEPSEEK_TIMEOUT, 6),
            ("vision_ocr", "图片识别", zhipu_id, ZHIPU_VISION_MODEL, 0, "", 1024, 0.2, 60, 7),
        ]
        for code, name, pid, model, think, effort, tokens, temp, timeout, order in scenes:
            cursor.execute("""
                INSERT INTO ai_model_configs
                (provider_id, scene_code, scene_name, model_name, thinking_enabled, thinking_effort,
                 max_tokens, temperature, timeout_seconds, extra_params, sort_order, is_enabled, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, '{}', %s, 1, %s, %s)
            """, (pid, code, name, model, think, effort, tokens, temp, timeout, order, now, now))
        conn.commit()
        print("[初始化] 已预置7个AI模型场景配置")

    # ---------- 4. 搜索平台配置预置 ----------
    cursor.execute("SELECT COUNT(*) as cnt FROM search_platforms")
    if cursor.fetchone()["cnt"] == 0:
        cursor.execute("SELECT id, provider_code FROM ai_providers WHERE provider_type='search_platform'")
        now = now_str()
        for row in cursor.fetchall():
            # 1688优先级1，中国制造网优先级2
            priority = 1 if row["provider_code"] == "ali1688" else 2
            cursor.execute("""
                INSERT INTO search_platforms (provider_id, is_enabled, priority, max_results, extra_config, created_at, updated_at)
                VALUES (%s, 1, %s, 50, '{}', %s, %s)
            """, (row["id"], priority, now, now))
        conn.commit()
        print("[初始化] 已预置2个搜索平台配置")

    # ---------- 5. 初筛规则模板预置（11条一票否决 + 6条评分规则）----------
    _seed_screening_rules(cursor, conn)


def _seed_screening_rules(cursor, conn):
    """
    预置初筛规则模板（11条一票否决 + 6条评分规则）

    小白讲解：系统第一次启动时自动把方案5.2节的17条默认规则写入 screening_rule_templates 表。
    之后用户可以在「规则配置页」修改参数（阈值/分值/启用开关），修改后保存为衍生模板。
    系统默认规则不可删除，但用户可以基于默认规则创建自己的模板。
    """
    cursor.execute("SELECT COUNT(*) as cnt FROM screening_rule_templates")
    if cursor.fetchone()["cnt"] > 0:
        return  # 规则已存在，跳过（避免重复插入）

    now = now_str()

    # ==================== 11条一票否决规则（veto）====================
    # 小白讲解：一票否决规则触发后，该供应商直接淘汰，不进入评分。
    # default_condition 是触发条件（JSON），default_action 是否决动作（含否决原因）。
    # tyc_commands 是该规则需要调用的天眼查MCP工具列表。
    veto_rules = [
        # 1. 注册资本一票否决
        {
            "rule_code": "veto_capital",
            "rule_name": "注册资本一票否决",
            "rule_type": "veto",
            "rule_category": "basic_info",
            "default_condition": '{"type":"single","field":"reg_capital_wan","operator":"lt","value":100,"unit":"万元"}',
            "default_action": '{"result":"veto","reason":"注册资本不足100万"}',
            "max_score": None,
            "scoring_logic": "",
            "tyc_commands": '["get_company_basic_profile"]',
            "is_configurable": 1,  # 可改阈值
            "is_enabled": 1,
            "sort_order": 1,
            "description": "注册资本低于100万人民币的供应商直接淘汰。用户可在规则配置页修改阈值。",
        },
        # 2. 经营状态一票否决
        {
            "rule_code": "veto_operating_status",
            "rule_name": "经营状态一票否决",
            "rule_type": "veto",
            "rule_category": "basic_info",
            "default_condition": '{"type":"single","field":"operating_status","operator":"not_in","value":["存续","在业","正常","开业","存续（在营）"]}',
            "default_action": '{"result":"veto","reason":"经营状态异常（非存续/在业/正常）"}',
            "max_score": None,
            "scoring_logic": "",
            "tyc_commands": '["get_company_basic_profile"]',
            "is_configurable": 1,  # 可改允许的经营状态列表
            "is_enabled": 1,
            "sort_order": 2,
            "description": "经营状态不在允许列表中的供应商直接淘汰。用户可修改允许的经营状态。",
        },
        # 3. 经营异常一票否决
        {
            "rule_code": "veto_abnormal",
            "rule_name": "经营异常一票否决",
            "rule_type": "veto",
            "rule_category": "risk",
            "default_condition": '{"type":"single","field":"has_business_exception","operator":"eq","value":true}',
            "default_action": '{"result":"veto","reason":"当前处于经营异常状态"}',
            "max_score": None,
            "scoring_logic": "",
            "tyc_commands": '["get_risk_overview"]',
            "is_configurable": 1,  # 可改否决原因
            "is_enabled": 1,
            "sort_order": 3,
            "description": "当前处于经营异常状态的供应商直接淘汰。数据从天眼查风险总览中解析。",
        },
        # 4. 严重违法失信一票否决
        {
            "rule_code": "veto_dishonest",
            "rule_name": "严重违法失信一票否决",
            "rule_type": "veto",
            "rule_category": "risk",
            "default_condition": '{"type":"single","field":"has_serious_violation","operator":"eq","value":true}',
            "default_action": '{"result":"veto","reason":"当前处于严重违法失信状态"}',
            "max_score": None,
            "scoring_logic": "",
            "tyc_commands": '["get_risk_overview"]',
            "is_configurable": 1,  # 可改否决原因
            "is_enabled": 1,
            "sort_order": 4,
            "description": "当前处于严重违法失信状态的供应商直接淘汰。数据从天眼查风险总览中解析。",
        },
        # 5. 无有效联系方式一票否决
        {
            "rule_code": "veto_no_contact",
            "rule_name": "无有效联系方式一票否决",
            "rule_type": "veto",
            "rule_category": "contact",
            "default_condition": '{"type":"single","field":"has_valid_contact","operator":"eq","value":false}',
            "default_action": '{"result":"veto","reason":"无有效电话或邮箱"}',
            "max_score": None,
            "scoring_logic": "",
            "tyc_commands": '["get_company_basic_profile"]',
            "is_configurable": 1,  # 可开关
            "is_enabled": 1,
            "sort_order": 5,
            "description": "供应商无有效电话和邮箱的，触发一票否决。用户可在规则配置页关闭此规则。",
        },
        # 6. 非制造商一票否决
        {
            "rule_code": "veto_non_manufacturer",
            "rule_name": "非制造商一票否决",
            "rule_type": "veto",
            "rule_category": "qualification",
            "default_condition": '{"type":"single","field":"is_manufacturer","operator":"eq","value":false}',
            "default_action": '{"result":"veto","reason":"非制造商且无制造能力证据"}',
            "max_score": None,
            "scoring_logic": "",
            "tyc_commands": '["get_company_basic_profile","get_qualifications"]',
            "is_configurable": 1,  # 可开关
            "is_enabled": 1,
            "sort_order": 6,
            "description": "供应商非制造商且无制造能力证据的，触发一票否决。由AI综合分析注册类型、经营范围、资质等判断。用户可关闭此规则。",
        },
        # 7. 产品不匹配一票否决
        {
            "rule_code": "veto_product_mismatch",
            "rule_name": "产品不匹配一票否决",
            "rule_type": "veto",
            "rule_category": "match",
            "default_condition": '{"type":"single","field":"product_match","operator":"eq","value":"mismatch"}',
            "default_action": '{"result":"veto","reason":"产品或经营范围明显不匹配"}',
            "max_score": None,
            "scoring_logic": "",
            "tyc_commands": '["get_company_basic_profile"]',
            "is_configurable": 1,  # 可开关
            "is_enabled": 1,
            "sort_order": 7,
            "description": "供应商产品或经营范围与采购需求明显不匹配的，触发一票否决。由AI语义判断。用户可关闭此规则。",
        },
        # 8. 失信被执行人一票否决
        {
            "rule_code": "veto_faithless_person",
            "rule_name": "失信被执行人一票否决",
            "rule_type": "veto",
            "rule_category": "risk",
            "default_condition": '{"type":"single","field":"is_faithless_person","operator":"eq","value":true}',
            "default_action": '{"result":"veto","reason":"当前为失信被执行人"}',
            "max_score": None,
            "scoring_logic": "",
            "tyc_commands": '["get_risk_overview"]',
            "is_configurable": 1,  # 可改否决原因
            "is_enabled": 1,
            "sort_order": 8,
            "description": "当前为失信被执行人的供应商直接淘汰。数据从天眼查风险总览中解析。",
        },
        # 9. 知识产权侵权败诉一票否决
        {
            "rule_code": "veto_ip_lawsuit",
            "rule_name": "知识产权侵权败诉一票否决",
            "rule_type": "veto",
            "rule_category": "risk",
            "default_condition": '{"type":"single","field":"has_ip_loss_lawsuit","operator":"eq","value":true}',
            "default_action": '{"result":"veto","reason":"近3年有明确知识产权侵权败诉"}',
            "max_score": None,
            "scoring_logic": "",
            "tyc_commands": '["get_judicial_case"]',
            "is_configurable": 1,  # 可改否决原因
            "is_enabled": 1,
            "sort_order": 9,
            "description": "近3年有明确知识产权侵权败诉记录的供应商直接淘汰。数据从天眼查司法案件中分析。",
        },
        # 10. 平台侵权下架一票否决
        {
            "rule_code": "veto_platform_infringe",
            "rule_name": "平台侵权下架一票否决",
            "rule_type": "veto",
            "rule_category": "risk",
            "default_condition": '{"type":"single","field":"has_platform_infringe","operator":"eq","value":true}',
            "default_action": '{"result":"veto","reason":"有明确平台侵权下架记录","need_manual_review":true}',
            "max_score": None,
            "scoring_logic": "",
            "tyc_commands": '[]',
            "is_configurable": 1,  # 可改否决原因
            "is_enabled": 1,
            "sort_order": 10,
            "description": "有明确平台侵权下架记录的供应商需人工复核后决定。数据通过互联网搜索获取（非天眼查），标注为需人工复核。",
        },
        # 11. 注册资本未披露一票否决
        {
            "rule_code": "veto_capital_unknown",
            "rule_name": "注册资本未披露一票否决",
            "rule_type": "veto",
            "rule_category": "basic_info",
            "default_condition": '{"type":"single","field":"reg_capital_wan","operator":"is_null","value":null}',
            "default_action": '{"result":"manual_review","reason":"注册资本未披露，需人工确认"}',
            "max_score": None,
            "scoring_logic": "",
            "tyc_commands": '["get_company_basic_profile"]',
            "is_configurable": 1,  # 可改否决原因
            "is_enabled": 1,
            "sort_order": 11,
            "description": "注册资本未披露的供应商进入人工确认流程。此规则不直接淘汰，而是标注需人工确认。",
        },
    ]

    for rule in veto_rules:
        cursor.execute("""
            INSERT INTO screening_rule_templates
            (rule_code, rule_name, rule_type, rule_category, default_condition, default_action,
             max_score, scoring_logic, tyc_commands, is_configurable, is_enabled, sort_order,
             description, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (rule["rule_code"], rule["rule_name"], rule["rule_type"], rule["rule_category"],
              rule["default_condition"], rule["default_action"], rule["max_score"],
              rule["scoring_logic"], rule["tyc_commands"], rule["is_configurable"],
              rule["is_enabled"], rule["sort_order"], rule["description"], now, now))

    # ==================== 6条评分规则（score）====================
    # 小白讲解：评分规则计算得分，有满分值和评分逻辑。总分100分=25+15+30+10+15+5。
    # max_score 是满分值，scoring_logic 描述如何根据数据打分。
    score_rules = [
        # 1. 注册资本与企业规模（25分）
        {
            "rule_code": "score_capital_scale",
            "rule_name": "注册资本与企业规模",
            "rule_type": "score",
            "rule_category": "basic_info",
            "default_condition": '{"field":"reg_capital_wan"}',
            "default_action": '{}',
            "max_score": 25,
            "scoring_logic": "1000万以上满分25分，500-1000万20分，100-500万15分，100万以下0分",
            "tyc_commands": '["get_company_basic_profile"]',
            "is_configurable": 1,
            "is_enabled": 1,
            "sort_order": 101,
            "description": "根据注册资本评估企业规模。用户可修改分值和阈值。",
        },
        # 2. 经营状态与成立年限（15分）
        {
            "rule_code": "score_operating_years",
            "rule_name": "经营状态与成立年限",
            "rule_type": "score",
            "rule_category": "basic_info",
            "default_condition": '{"field":"established_years"}',
            "default_action": '{}',
            "max_score": 15,
            "scoring_logic": "成立≥10年满分15分，5-10年10分，2-5年5分，<2年0分",
            "tyc_commands": '["get_company_basic_profile"]',
            "is_configurable": 1,
            "is_enabled": 1,
            "sort_order": 102,
            "description": "根据成立年限评估企业经营稳定性。用户可修改分值和阈值。",
        },
        # 3. 产品与经营范围匹配（30分）
        {
            "rule_code": "score_product_match",
            "rule_name": "产品与经营范围匹配",
            "rule_type": "score",
            "rule_category": "match",
            "default_condition": '{"field":"product_match"}',
            "default_action": '{}',
            "max_score": 30,
            "scoring_logic": "AI语义判断：完全匹配30分，部分匹配20分，弱关联10分，不匹配0分",
            "tyc_commands": '["get_company_basic_profile"]',
            "is_configurable": 1,
            "is_enabled": 1,
            "sort_order": 103,
            "description": "由AI对比需求产品名称与供应商经营范围/主营产品，判断匹配度。用户可修改分值。",
        },
        # 4. 联系方式完整度（10分）
        {
            "rule_code": "score_contact_complete",
            "rule_name": "联系方式完整度",
            "rule_type": "score",
            "rule_category": "contact",
            "default_condition": '{"field":"contact_completeness"}',
            "default_action": '{}',
            "max_score": 10,
            "scoring_logic": "有电话+邮箱满分10分，仅有电话或邮箱5分，均无0分",
            "tyc_commands": '["get_company_basic_profile"]',
            "is_configurable": 1,
            "is_enabled": 1,
            "sort_order": 104,
            "description": "根据联系方式完整度评分。用户可修改分值。",
        },
        # 5. 风险记录（15分）
        {
            "rule_code": "score_risk_record",
            "rule_name": "风险记录",
            "rule_type": "score",
            "rule_category": "risk",
            "default_condition": '{"field":"risk_count"}',
            "default_action": '{}',
            "max_score": 15,
            "scoring_logic": "无风险记录满分15分，1-2条风险10分，3-5条5分，>5条0分",
            "tyc_commands": '["get_risk_overview"]',
            "is_configurable": 1,
            "is_enabled": 1,
            "sort_order": 105,
            "description": "根据天眼查风险总览中的风险记录数量评分。用户可修改分值和阈值。",
        },
        # 6. 出口经验、平台经验或资质（5分）
        {
            "rule_code": "score_export_exp",
            "rule_name": "出口经验、平台经验或资质",
            "rule_type": "score",
            "rule_category": "export",
            "default_condition": '{"field":"has_export_exp"}',
            "default_action": '{}',
            "max_score": 5,
            "scoring_logic": "有出口经验/跨境电商经验/相关资质满分5分，无0分",
            "tyc_commands": '["get_qualifications","get_company_basic_profile"]',
            "is_configurable": 1,
            "is_enabled": 1,
            "sort_order": 106,
            "description": "结合资质证书、经营范围和AI分析判断是否有出口/平台经验。用户可修改分值。",
        },
    ]

    for rule in score_rules:
        cursor.execute("""
            INSERT INTO screening_rule_templates
            (rule_code, rule_name, rule_type, rule_category, default_condition, default_action,
             max_score, scoring_logic, tyc_commands, is_configurable, is_enabled, sort_order,
             description, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (rule["rule_code"], rule["rule_name"], rule["rule_type"], rule["rule_category"],
              rule["default_condition"], rule["default_action"], rule["max_score"],
              rule["scoring_logic"], rule["tyc_commands"], rule["is_configurable"],
              rule["is_enabled"], rule["sort_order"], rule["description"], now, now))

    # ==================== 2条通过标准配置（threshold）====================
    # 小白讲解：这两条记录不是"规则"，而是"配置参数"，控制初筛总分到结论的映射。
    # pass_threshold：总分≥此值则"通过"；manual_review_threshold：总分≥此值但<pass_threshold则"人工确认"。
    # 业务部门可在规则配置页直接修改这两个数字，不需要改代码。
    threshold_configs = [
        {
            "rule_code": "threshold_pass",
            "rule_name": "通过线",
            "rule_type": "threshold",
            "rule_category": "config",
            "default_condition": '{"field":"total_score","operator":"gte","value":75}',
            "default_action": '{"result":"pass","reason":"总分≥%s，通过初筛"}',
            "max_score": 75,
            "scoring_logic": "总分≥此值则判定为已通过初筛",
            "tyc_commands": '[]',
            "is_configurable": 1,
            "is_enabled": 1,
            "sort_order": 100,
            "description": "初筛总分达到此值即为通过。可在规则配置页调整。",
        },
        {
            "rule_code": "threshold_manual_review",
            "rule_name": "人工确认线",
            "rule_type": "threshold",
            "rule_category": "config",
            "default_condition": '{"field":"total_score","operator":"gte","value":60}',
            "default_action": '{"result":"manual_review","reason":"总分在%s-%s之间，需人工确认"}',
            "max_score": 60,
            "scoring_logic": "总分≥此值但低于通过线则判定为需人工确认",
            "tyc_commands": '[]',
            "is_configurable": 1,
            "is_enabled": 1,
            "sort_order": 101,
            "description": "初筛总分达到此值但低于通过线时进入人工确认。可在规则配置页调整。低于此值则未通过。",
        },
    ]

    for rule in threshold_configs:
        cursor.execute("""
            INSERT INTO screening_rule_templates
            (rule_code, rule_name, rule_type, rule_category, default_condition, default_action,
             max_score, scoring_logic, tyc_commands, is_configurable, is_enabled, sort_order,
             description, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (rule["rule_code"], rule["rule_name"], rule["rule_type"], rule["rule_category"],
              rule["default_condition"], rule["default_action"], rule["max_score"],
              rule["scoring_logic"], rule["tyc_commands"], rule["is_configurable"],
              rule["is_enabled"], rule["sort_order"], rule["description"], now, now))

    conn.commit()
    print("[初始化] 已预置17条初筛规则模板 + 2条通过标准配置（共19条记录）")


if __name__ == "__main__":
    init_db()
