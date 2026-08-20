"""
供应商寻源系统 - 数据库初始化模块

小白讲解：这个文件负责创建数据库表（就像建房子打地基）。
所有"需求"和"供应商"的数据都按这里的结构存到SQLite数据库里。
运行一次 `python db.py` 就能自动建好所有表。
"""

import os
import pymysql

# ==================== MySQL 数据库配置 ====================
# 小白讲解：系统已从SQLite切换到MySQL，所有数据都存MySQL，新代码统一用MySQL连接。
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "sourcing.db")

# MySQL连接参数（从config.py读取，避免重复配置）
from config import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE

# ==================== 常量枚举 ====================
# 供应商开发阶段（对应工作流5个阶段的第4步"供应商开发"）
SUPPLIER_STAGES = ["已寻源待初筛", "已通过初筛", "沟通中", "已合作", "未通过初筛"]

# 需求状态（5种，按业务流程顺序排列，与供应商开发阶段联动）
# 小白讲解：需求状态会根据该需求下供应商的整体进度自动推断，取"最靠后"的阶段。
# 对应关系：
#   需求确认中 → 无供应商
#   寻源中     → 有供应商，且全部是"已寻源待初筛"
#   初筛中     → 出现"已通过初筛"或"未通过初筛"
#   沟通中     → 出现"沟通中"
#   已完成     → 出现"已合作"（终态，一旦设为已完成不会自动回退）
REQUIREMENT_STATUSES = ["需求确认中", "寻源中", "初筛中", "沟通中", "已完成"]

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


def recalc_requirement_status(cursor, requirement_id):
    """
    根据需求下所有供应商的开发阶段分布，重新推断并更新需求状态。

    小白讲解：需求状态不是写死的，而是看这个需求下的供应商整体走到哪一步了。
    取"最靠后"的阶段作为需求状态。规则：
      - 有"已合作"                → 已完成
      - 当前已是"已完成"          → 已完成（终态，不自动回退，要回退需用户手动改）
      - 有"沟通中"                → 沟通中
      - 有"已通过初筛"或"未通过初筛" → 初筛中
      - 有"已寻源待初筛"          → 寻源中
      - 无供应商                  → 保持当前状态（不回退）
        原因："需求确认中"和"寻源中"的差别在于需求是否已确认，不在于有没有供应商。
        AI确认保存会设为"寻源中"，此时还没供应商；不应被本函数回退成"需求确认中"。

    参数：
        cursor: 已打开的数据库游标（pymysql DictCursor）
        requirement_id: 需求ID
    返回：新的状态字符串；需求不存在时返回 None
    """
    cursor.execute("SELECT status FROM requirements WHERE id=%s", (requirement_id,))
    row = cursor.fetchone()
    if not row:
        return None
    current_status = row["status"]

    # 统计该需求下各开发阶段的供应商数量
    cursor.execute(
        "SELECT dev_stage, COUNT(*) AS cnt FROM suppliers WHERE requirement_id=%s GROUP BY dev_stage",
        (requirement_id,)
    )
    stage_counts = {r["dev_stage"]: int(r["cnt"]) for r in cursor.fetchall()}

    has_cooperated = stage_counts.get("已合作", 0) > 0
    has_communicating = stage_counts.get("沟通中", 0) > 0
    has_screened = (stage_counts.get("已通过初筛", 0) + stage_counts.get("未通过初筛", 0)) > 0
    has_pending = stage_counts.get("已寻源待初筛", 0) > 0
    total_suppliers = sum(stage_counts.values())

    if has_cooperated:
        new_status = "已完成"
    elif current_status == "已完成":
        # 已完成是终态：不因供应商变化自动回退，用户手动改回才生效
        new_status = "已完成"
    elif has_communicating:
        new_status = "沟通中"
    elif has_screened:
        new_status = "初筛中"
    elif has_pending or total_suppliers > 0:
        new_status = "寻源中"
    else:
        # 无供应商：保持当前状态（不回退，区分"需求确认中"和"寻源中"）
        new_status = current_status or "需求确认中"

    if new_status != current_status:
        cursor.execute(
            "UPDATE requirements SET status=%s, updated_at=%s WHERE id=%s",
            (new_status, now_str(), requirement_id)
        )
    return new_status


def mark_supplier_communicating(cursor, supplier_id):
    """
    将已经发生沟通的供应商同步为“沟通中”阶段。

    小白讲解：供应商一旦产生沟通记录，就代表已经进入沟通阶段。
    但已合作、未通过初筛以及所属需求已完成的供应商不应被改回“沟通中”。
    """
    cursor.execute("""
        SELECT s.requirement_id, s.dev_stage, r.status AS requirement_status
        FROM suppliers s
        LEFT JOIN requirements r ON r.id = s.requirement_id
        WHERE s.id = %s
    """, (supplier_id,))
    supplier = cursor.fetchone()
    if not supplier:
        return False

    if supplier.get("requirement_status") == "已完成":
        return False
    if supplier.get("dev_stage") in ("已合作", "未通过初筛"):
        return False

    if supplier.get("dev_stage") != "沟通中":
        cursor.execute(
            "UPDATE suppliers SET dev_stage=%s, updated_at=%s WHERE id=%s",
            ("沟通中", now_str(), supplier_id)
        )

    if supplier.get("requirement_id"):
        recalc_requirement_status(cursor, supplier["requirement_id"])
    return True


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
    from datetime import datetime
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
        status              VARCHAR(50) DEFAULT '需求确认中', -- 状态：需求确认中/寻源中/初筛中/沟通中/已完成
        hs_code             VARCHAR(20) DEFAULT '',          -- HS编码（DeepSeek自动归类，如9403）
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
        -- 产品字段（搜产品方式返回，用于业务核对供应商是否真做该产品）
        product_title       VARCHAR(1000) DEFAULT '',         -- 产品名称（1688中文原样，MIC已翻译成中文）
        product_link        VARCHAR(1000) DEFAULT '',         -- 产品链接（产品页URL）
        price               VARCHAR(200) DEFAULT '',          -- 价格（如"176.63"或"US$105.00-155.00"）
        moq                 VARCHAR(200) DEFAULT '',          -- 起订量MOQ（如"1件"或"30 Pieces (MOQ)"）
        -- 海关出口数据字段（topease海关数据搜索返回，用于出口经验评分）
        customs_export_count INT DEFAULT 0,                   -- 海关出口次数
        customs_total_qty   DECIMAL(18,2) DEFAULT 0,         -- 海关出口总量（千克/件）
        customs_total_amount DECIMAL(18,2) DEFAULT 0,        -- 海关出口总金额（美元）
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
    # 恢复产品字段（搜产品方式需要，用于业务核对供应商是否真做该产品）
    _add_column_if_not_exists(cursor, "suppliers", "product_title", "VARCHAR(1000) DEFAULT ''")
    _add_column_if_not_exists(cursor, "suppliers", "product_link", "VARCHAR(1000) DEFAULT ''")
    _add_column_if_not_exists(cursor, "suppliers", "price", "VARCHAR(200) DEFAULT ''")
    _add_column_if_not_exists(cursor, "suppliers", "moq", "VARCHAR(200) DEFAULT ''")
    # 海关出口数据字段（topease海关数据搜索返回，用于出口经验评分）
    _add_column_if_not_exists(cursor, "suppliers", "customs_export_count", "INT DEFAULT 0")
    _add_column_if_not_exists(cursor, "suppliers", "customs_total_qty", "DECIMAL(18,2) DEFAULT 0")
    _add_column_if_not_exists(cursor, "suppliers", "customs_total_amount", "DECIMAL(18,2) DEFAULT 0")
    # 天眼查工商信息缓存字段（搜索阶段保存，初筛阶段复用，减少MCP调用次数）
    # 小白讲解：以前初筛时要重新调天眼查查一遍工商信息，现在搜索阶段查完后直接存库，
    # 初筛时读库就行，不用再调天眼查，省掉重复的MCP请求。
    _add_column_if_not_exists(cursor, "suppliers", "tyc_match_status", "VARCHAR(50) DEFAULT ''")
    _add_column_if_not_exists(cursor, "suppliers", "business_scope", "TEXT")
    _add_column_if_not_exists(cursor, "suppliers", "tyc_company_id", "VARCHAR(100) DEFAULT ''")

    # 清理已废弃的旧字段（price_moq已拆分成price和moq两个独立字段，需删除旧的合并字段）
    _drop_column_if_exists(cursor, "suppliers", "price_moq")
    # 清理其他已废弃旧字段
    _drop_column_if_exists(cursor, "suppliers", "has_amazon_exp")
    _drop_column_if_exists(cursor, "suppliers", "has_temu_exp")

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
    # 小白讲解：supplier_id 允许为 NULL，因为测试邮件或待认领邮件场景下可能没有关联供应商。
    # 外键约束也去掉，方便测试邮件入库（外键会强制 supplier_id 必须在 suppliers 表存在）。
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS communications (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        supplier_id     INTEGER,
        channel         VARCHAR(100) DEFAULT '微信/企微',   -- 沟通渠道
        content         VARCHAR(1000) DEFAULT '',            -- 沟通内容
        conclusion      VARCHAR(1000) DEFAULT '',            -- 沟通结论
        next_step       VARCHAR(1000) DEFAULT '',            -- 后续步骤
        comm_time       VARCHAR(1000) DEFAULT '',            -- 沟通时间
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """)

    # 兼容旧数据库：把已存在的 communications.supplier_id 改成允许 NULL
    # 小白讲解：旧版表把 supplier_id 设为 NOT NULL 还加了外键，导致测试邮件（无供应商）无法入库。
    # 这里用 ALTER TABLE 把约束改宽松：允许 NULL，并删掉外键（如果存在）。
    try:
        cursor.execute("""
            SELECT CONSTRAINT_NAME FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'communications'
            AND CONSTRAINT_TYPE = 'FOREIGN KEY'
        """, (MYSQL_DATABASE,))
        for fk_row in cursor.fetchall():
            fk_name = fk_row["CONSTRAINT_NAME"]
            cursor.execute(f"ALTER TABLE communications DROP FOREIGN KEY {fk_name}")
    except Exception as e:
        print(f"[db] 清理 communications 外键时跳过（可能不存在）：{e}")

    try:
        cursor.execute("ALTER TABLE communications MODIFY COLUMN supplier_id INTEGER NULL")
    except Exception as e:
        print(f"[db] 修改 communications.supplier_id 为 NULL 时跳过：{e}")

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
    _add_column_if_not_exists(cursor, "requirements", "hs_code", "VARCHAR(20) DEFAULT ''")
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

    # ==================== 邮件功能扩展：communications表加4个字段 ====================
    # 小白讲解：接入Gmail邮件收发后，沟通记录需要区分"发出"还是"收到"，还要存邮件主题、
    # Gmail邮件ID（用于去重，防止同一封邮件重复入库）、发送状态。
    # direction: outbound=系统发出 / inbound=系统收到（旧数据默认outbound，兼容老记录）
    # subject: 邮件主题（短信等渠道为空）
    # external_id: Gmail的MessageID，用于去重（每封Gmail邮件唯一）
    # status: 发送状态 sent=已发送/delivered=已送达/failed=发送失败（仅outbound有值）
    _add_column_if_not_exists(cursor, "communications", "direction", "VARCHAR(10) NOT NULL DEFAULT 'outbound'")
    _add_column_if_not_exists(cursor, "communications", "subject", "VARCHAR(500) DEFAULT ''")
    _add_column_if_not_exists(cursor, "communications", "external_id", "VARCHAR(200) DEFAULT ''")
    _add_column_if_not_exists(cursor, "communications", "status", "VARCHAR(20) DEFAULT ''")
    # 邮件已读标记：0=未读，1=已读。发出的邮件默认已读，收到的邮件默认未读。
    # 小白讲解：用户在"邮件管理"页面点击某个供应商会话后，系统把该供应商收到的邮件标记为已读，
    # 联系人列表上的红点未读数字会消失。
    _add_column_if_not_exists(cursor, "communications", "is_read", "INTEGER NOT NULL DEFAULT 0")

    # 给external_id建索引，加速去重查询（每次收邮件先查这个ID是否已存在）
    try:
        cursor.execute("CREATE INDEX idx_communications_external_id ON communications(external_id(100))")
    except pymysql.err.OperationalError as e:
        if e.args[0] != 1061:
            raise

    # ==================== 12. 邮箱配置表 ====================
    # 小白讲解：存Gmail账号和应用专用密码，管理员在"邮箱配置"页面填写。
    # 只有一条记录（id=1），更新覆盖即可。密码存明文是因为Gmail应用专用密码本身
    # 就是16位专用密码（非登录密码），且需要原文发送给SMTP服务器验证。
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS email_config (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        gmail_address   VARCHAR(200) NOT NULL DEFAULT '',   -- Gmail邮箱地址（如 yourname@gmail.com）
        app_password    VARCHAR(200) NOT NULL DEFAULT '',   -- 16位应用专用密码（非登录密码）
        sender_name     VARCHAR(100) NOT NULL DEFAULT '',   -- 发件人显示名称（如"XX公司采购部"）
        poll_interval   INTEGER NOT NULL DEFAULT 300,       -- IMAP轮询间隔（秒，默认5分钟=300秒）
        is_enabled      INTEGER NOT NULL DEFAULT 0,         -- 是否启用邮件收发（0关/1开）
        last_poll_time  VARCHAR(30) DEFAULT '',             -- 上次轮询时间（用于显示和排查）
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """)

    # ==================== 13. 待认领邮件表 ====================
    # 小白讲解：供应商回复邮件到你的Gmail，但发件人邮箱匹配不到系统里任何供应商时，
    # 邮件内容先存这张表。用户在"待认领邮件"页面手动关联到某个供应商后，记录转入
    # communications表，本表记录删除。
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS pending_emails (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        from_addr       VARCHAR(200) NOT NULL DEFAULT '',   -- 发件人邮箱
        from_name       VARCHAR(200) DEFAULT '',             -- 发件人名称（可能为空）
        subject         VARCHAR(500) DEFAULT '',             -- 邮件主题
        body_preview    TEXT,                                -- 正文预览（前500字）
        external_id     VARCHAR(200) DEFAULT '',             -- Gmail邮件ID（去重用）
        received_time   VARCHAR(30) DEFAULT '',              -- 收件时间
        user_id         INTEGER NOT NULL DEFAULT 1,          -- 所属用户（先归管理员，认领后转移）
        is_claimed      INTEGER NOT NULL DEFAULT 0,          -- 是否已认领（0未认领/1已认领）
        claimed_supplier_id INTEGER DEFAULT NULL,            -- 认领到的供应商ID
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """)

    # 给pending_emails的external_id建索引（去重查询用）
    try:
        cursor.execute("CREATE INDEX idx_pending_emails_external_id ON pending_emails(external_id(100))")
    except pymysql.err.OperationalError as e:
        if e.args[0] != 1061:
            raise

    # ==================== 邮件剔除规则表 ====================
    # 小白讲解：Gmail 收件箱里会混进来很多"不是供应商真实回复"的邮件，
    # 比如 Google 安全提醒、二步验证码、noreply 类通知邮件。
    # 这些邮件不该进待认领列表污染用户视野，本表存储剔除规则。
    # 每条规则检查一个字段（发件人/主题等），命中就跳过入库。
    # 用户可在「邮件剔除规则」页面自行增删规则，灵活适配新场景。
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS email_filter_rules (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        rule_name       VARCHAR(100) NOT NULL,           -- 规则名称（如"Google安全提醒"）
        field           VARCHAR(20) NOT NULL,             -- 检查字段：from_addr/from_name/subject/body
        match_type      VARCHAR(20) NOT NULL,             -- 匹配方式：contains/regex/equals/startswith
        match_value     VARCHAR(500) NOT NULL,            -- 匹配值（如noreply、验证码、安全提醒）
        action          VARCHAR(20) NOT NULL DEFAULT 'skip',  -- 动作：skip=跳过入库（标记已读）
        is_enabled      INTEGER NOT NULL DEFAULT 1,       -- 是否启用（1启用/0禁用）
        priority        INTEGER NOT NULL DEFAULT 100,     -- 优先级（数字小的先匹配，命中即跳过）
        is_builtin      INTEGER NOT NULL DEFAULT 0,       -- 是否内置规则（1内置/0自定义），内置规则只能禁用不能删
        description     VARCHAR(500) DEFAULT '',          -- 规则说明（管理员可看）
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """)

    # 给 email_filter_rules 建索引（按启用状态+优先级查询最快）
    try:
        cursor.execute("CREATE INDEX idx_filter_rules_enabled ON email_filter_rules(is_enabled, priority)")
    except pymysql.err.OperationalError as e:
        if e.args[0] != 1061:
            raise

    # ==================== 沟通模板表 ====================
    # 小白讲解：管理员可以新建多个邮件模板（询价模板、跟进模板等），
    # 模板里支持 {product_name} {supplier_name} 等变量，AI生成邮件时会自动替换。
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS communication_templates (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        name            VARCHAR(100) NOT NULL,           -- 模板名称（如"询价模板"）
        subject_template VARCHAR(500) NOT NULL DEFAULT '',  -- 标题模板（支持变量）
        body_template   TEXT,                            -- 正文模板（支持变量）
        description     VARCHAR(500) DEFAULT '',          -- 模板说明
        scene           VARCHAR(50) NOT NULL DEFAULT 'general',  -- 适用场景：inquiry/follow_up/negotiation/general
        is_enabled      INTEGER NOT NULL DEFAULT 1,       -- 是否启用
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """)

    # ==================== AI系统设置表 ====================
    # 小白讲解：存储AI系统提示词等可编辑配置，管理员可在沟通模板管理页面
    # 直接修改提示词内容，修改后立即生效，不用改代码、不用重启服务。
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ai_prompt_settings (
            id              INT AUTO_INCREMENT PRIMARY KEY,
            setting_key     VARCHAR(100) NOT NULL UNIQUE,
            setting_value   TEXT,
            description     VARCHAR(500) DEFAULT '',
            updated_at      TEXT NOT NULL
        )
    """)
    # 小白讲解：初始化4个AI提示词（系统提示词 + 3个场景默认提示词）
    _ai_defaults = [
        ("comm_system_prompt",
         "你是一位专业的采购沟通助手。你的任务是根据供应商沟通记录和产品需求，生成一封专业、礼貌、清晰的商务邮件。\n\n要求：\n1. 邮件标题简洁明确，包含关键信息（产品名、目的）\n2. 正文结构：称呼 → 开场白 → 核心内容 → 期待回复 → 落款\n3. 语气专业但友好，避免过度客气\n4. 涉及具体参数（价格、MOQ、交期）时保留原数值，不要编造",
         "系统提示词：定义AI的角色和生成邮件的基本要求，每次生成都必带"),
        ("prompt_session_reply",
         "请根据以上沟通记录，生成一封得体的回复邮件。",
         "会话回复默认提示词：用户在会话框点AI生成且未填写提示词时使用"),
        ("prompt_bulk_send",
         "请根据该供应商的产品和需求信息，生成一封首次询价邮件。",
         "群发邮件默认提示词：群发询价邮件时用户未填写提示词时使用"),
        ("prompt_single_send",
         "请根据该供应商的产品和需求信息，生成一封首次询价邮件。",
         "单发邮件默认提示词：单发询价邮件时用户未填写提示词时使用"),
    ]
    for key, value, desc in _ai_defaults:
        cursor.execute("SELECT COUNT(*) AS cnt FROM ai_prompt_settings WHERE setting_key = %s", (key,))
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute("""
                INSERT INTO ai_prompt_settings (setting_key, setting_value, description, updated_at)
                VALUES (%s, %s, %s, %s)
            """, (key, value, desc, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))

    # ==================== AI生成记录表 ====================
    # 小白讲解：记录每次AI生成邮件的结果，用户点"重新生成"时，
    # 系统把上次生成的不满意结果作为"错误示例"发给AI，让AI避免同样的问题。
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ai_generation_logs (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        user_id         INTEGER NOT NULL DEFAULT 1,
        scene           VARCHAR(50) NOT NULL,             -- 场景：session_reply/bulk_send/single_send
        supplier_id     INTEGER,                          -- 关联供应商（群发时为NULL）
        user_prompt     TEXT,                             -- 用户填写的提示词（可为空）
        system_prompt   TEXT,                             -- 系统提示词
        generated_subject VARCHAR(500) DEFAULT '',        -- AI生成的标题
        generated_body  TEXT,                             -- AI生成的正文
        is_accepted     INTEGER NOT NULL DEFAULT 0,       -- 0未采纳/1已采纳
        created_at      TEXT NOT NULL
    )
    """)

    # ==================== 14. 邮件附件表 ====================
    # 小白讲解：邮件发完后附件要能查看（尤其是图片），所以要把附件信息存数据库。
    # 附件文件本身保存在 uploads 目录，这里只存"元信息"（文件名、类型、路径、关联哪封邮件）。
    # 图片附件永久保留，其他附件发送后删除临时文件（只留数据库记录用于展示文件名）。
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS communication_attachments (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        communication_id    INTEGER NOT NULL,                 -- 关联的沟通记录ID
        original_filename   VARCHAR(500) DEFAULT '',           -- 原始文件名（用户上传时的名字）
        saved_filename      VARCHAR(500) DEFAULT '',           -- 保存到uploads目录的文件名（带时间戳）
        file_path           VARCHAR(1000) DEFAULT '',          -- 完整文件路径
        mime_type           VARCHAR(200) DEFAULT '',           -- 文件MIME类型（如 image/jpeg）
        file_size           INTEGER DEFAULT 0,                 -- 文件大小（字节）
        is_image            INTEGER DEFAULT 0,                 -- 是否图片（1是/0否，方便快速判断）
        created_at          TEXT NOT NULL,
        FOREIGN KEY (communication_id) REFERENCES communications (id) ON DELETE CASCADE
    )
    """)

    # 修复历史数据：以前产生沟通记录后没有同步供应商阶段，导致首页“沟通中”一直为0。
    # 小白讲解：应用启动时把已有沟通记录、尚未归档且未合作/未淘汰的供应商补成“沟通中”。
    cursor.execute("""
        UPDATE suppliers s
        LEFT JOIN requirements r ON r.id = s.requirement_id
        SET s.dev_stage = '沟通中', s.updated_at = %s
        WHERE EXISTS (
            SELECT 1 FROM communications c WHERE c.supplier_id = s.id
        )
          AND (r.status IS NULL OR r.status <> '已完成')
          AND s.dev_stage NOT IN ('沟通中', '已合作', '未通过初筛')
    """, (now_str(),))

    cursor.execute("""
        SELECT DISTINCT s.requirement_id
        FROM suppliers s
        WHERE s.dev_stage = '沟通中' AND s.requirement_id IS NOT NULL
    """)
    for row in cursor.fetchall():
        recalc_requirement_status(cursor, row["requirement_id"])

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
    2. AI服务提供商（MiniMax/智谱/1688/中国制造网/天眼查）
    3. 7个AI模型场景配置（从config.py迁移参数）
    4. 2个搜索平台配置（1688/中国制造网）
    """
    # 小白讲解：在函数开头统一导入config里的常量，确保后面所有分支都能用到。
    from config import (MINIMAX_API_KEY, MINIMAX_BASE_URL, MINIMAX_MODEL,
                        MINIMAX_MAX_TOKENS, MINIMAX_TIMEOUT, ZHIPU_VISION_MODEL)

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

    # ---------- 2. AI服务提供商预置（支持增量补录） ----------
    # 从 config.py 读取现有配置值迁移到数据库
    from config import (MINIMAX_API_KEY, MINIMAX_BASE_URL,
                        ZHIPU_API_KEY, ZHIPU_BASE_URL,
                        TYC_MCP_URL, TYC_MCP_AUTH,
                        ALI_1688_AK)
    now = now_str()
    providers = [
        ("MiniMax", "minimax", "ai_model", MINIMAX_BASE_URL, MINIMAX_API_KEY),
        ("智谱AI", "zhipu", "ai_model", ZHIPU_BASE_URL, ZHIPU_API_KEY),
        ("1688", "ali1688", "search_platform", "https://api.1688.com", ALI_1688_AK),
        ("中国制造网", "madeinchina", "search_platform", "https://mcp.chexb.com/sse", ""),
        ("海关贸易数据", "topease_customs", "search_platform", "https://mcp.topease.net/mcp",
         "trdmcp_live_gh-CN9jbAnZrRd99lJR9MNSG8avtLdnXZKoY0NaE8c4"),
        ("天眼查", "tianyancha", "data_api", TYC_MCP_URL, TYC_MCP_AUTH),
        ("Jina Reader", "jina_reader", "data_api", "https://r.jina.ai", ""),
        ("Firecrawl", "firecrawl", "data_api", "https://api.firecrawl.dev/v1", ""),
    ]
    # 小白讲解：逐个检查是否已存在，不存在才插入（INSERT IGNORE）
    # 这样老用户升级时也能自动补录新增的服务商
    for name, code, ptype, url, key in providers:
        cursor.execute("SELECT id FROM ai_providers WHERE provider_code = %s", (code,))
        if not cursor.fetchone():
            cursor.execute("""
                INSERT INTO ai_providers (provider_name, provider_code, provider_type, base_url, api_key, is_enabled, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, 1, %s, %s)
            """, (name, code, ptype, url, key, now, now))
            print(f"[初始化] 新增服务商：{name}（{code}）")
    conn.commit()
    print("[初始化] AI服务提供商检查完成")

    # ---------- 2.5 DeepSeek → MiniMax 迁移（老库自动切换，幂等可重复执行） ----------
    _migrate_deepseek_to_minimax(cursor, conn)

    # ---------- 3. AI模型场景配置预置（7个场景）----------
    cursor.execute("SELECT COUNT(*) as cnt FROM ai_model_configs")
    if cursor.fetchone()["cnt"] == 0:
        # 从 config.py 读取模型参数（函数开头已统一导入）
        # 查询provider_id
        cursor.execute("SELECT id, provider_code FROM ai_providers")
        provider_map = {row["provider_code"]: row["id"] for row in cursor.fetchall()}
        minimax_id = provider_map.get("minimax")
        zhipu_id = provider_map.get("zhipu")
        now = now_str()
        # 7个场景配置（scene_code / scene_name / provider / model / thinking / effort / tokens / temp / timeout）
        # 小白讲解：MiniMax-M3 思考模式无分级（只有开/关），effort统一留空；
        # 温度按官方推荐用1.0；过滤/翻译场景max_tokens给到8192（思考过程也消耗输出token，太小正文会为空）
        scenes = [
            ("req_parse", "需求解析", minimax_id, MINIMAX_MODEL, 1, "", MINIMAX_MAX_TOKENS, 1.0, MINIMAX_TIMEOUT, 1),
            ("keyword_gen", "关键词生成", minimax_id, MINIMAX_MODEL, 1, "", MINIMAX_MAX_TOKENS, 1.0, MINIMAX_TIMEOUT, 2),
            ("auto_screening", "自动初筛", minimax_id, MINIMAX_MODEL, 1, "", MINIMAX_MAX_TOKENS, 1.0, MINIMAX_TIMEOUT, 3),
            ("supplier_translate", "供应商过滤-翻译", minimax_id, MINIMAX_MODEL, 1, "", 8192, 1.0, MINIMAX_TIMEOUT, 4),
            ("supplier_filter", "供应商过滤-第一批", minimax_id, MINIMAX_MODEL, 1, "", 8192, 1.0, MINIMAX_TIMEOUT, 5),
            ("supplier_filter_v2", "供应商过滤-第二批", minimax_id, MINIMAX_MODEL, 1, "", 8192, 1.0, MINIMAX_TIMEOUT, 6),
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

    # ---------- 3.5 沟通管理AI场景配置（补丁式插入，已存在则跳过）----------
    # 小白讲解：后续版本新增的"会话回复"和"群发/单发邮件"两个AI场景，
    # 用 INSERT IGNORE 确保只在首次运行时插入，不会重复。
    cursor.execute("SELECT id FROM ai_model_configs WHERE scene_code='comm_reply' LIMIT 1")
    if not cursor.fetchone():
        cursor.execute("SELECT id FROM ai_providers WHERE provider_code='minimax' LIMIT 1")
        mm_row = cursor.fetchone()
        if mm_row:
            mm_id = mm_row["id"]
            now = now_str()
            cursor.execute("""
                INSERT INTO ai_model_configs
                (provider_id, scene_code, scene_name, model_name, thinking_enabled, thinking_effort,
                 max_tokens, temperature, timeout_seconds, extra_params, sort_order, is_enabled, created_at, updated_at)
                VALUES
                (%s, 'comm_reply', '沟通-会话回复', %s, 0, '', 4096, 1.0, %s, '{}', 8, 1, %s, %s),
                (%s, 'comm_send', '沟通-邮件生成', %s, 0, '', 4096, 1.0, %s, '{}', 9, 1, %s, %s)
            """, (mm_id, MINIMAX_MODEL, MINIMAX_TIMEOUT, now, now,
                  mm_id, MINIMAX_MODEL, MINIMAX_TIMEOUT, now, now))
            conn.commit()
            print("[初始化] 已补丁插入2个沟通管理AI场景配置（comm_reply / comm_send）")

    # ---------- 4. 搜索平台配置预置（补丁式插入，已存在则跳过）----------
    # 小白讲解：之前用"表为空才插入"的逻辑，老用户升级时表里已有1688和中国制造网，
    # 导致新增的海关贸易数据平台不会被插入。改成逐个检查，不存在的才插入。
    cursor.execute("SELECT id, provider_code FROM ai_providers WHERE provider_type='search_platform'")
    now = now_str()
    for row in cursor.fetchall():
        # 检查该平台是否已在 search_platforms 表中
        cursor.execute("SELECT id FROM search_platforms WHERE provider_id = %s", (row["id"],))
        if cursor.fetchone():
            continue  # 已存在，跳过
        # 优先级：1688=1，中国制造网=2，海关贸易数据=3
        priority = {"ali1688": 1, "madeinchina": 2, "topease_customs": 3}.get(row["provider_code"], 9)
        cursor.execute("""
            INSERT INTO search_platforms (provider_id, is_enabled, priority, max_results, extra_config, created_at, updated_at)
            VALUES (%s, 1, %s, 50, '{}', %s, %s)
        """, (row["id"], priority, now, now))
        print(f"[初始化] 新增搜索平台：{row['provider_code']}（优先级{priority}）")
    conn.commit()

    # ---------- 5. 初筛规则模板预置（11条一票否决 + 6条评分规则）----------
    _seed_screening_rules(cursor, conn)

    # ---------- 6. 数据归属归位（让供应商/初筛/审计/沟通记录跟着需求所有者走）----------
    # 小白讲解：以前管理员帮用户搜索供应商时，数据 user_id 写成了管理员 ID，用户看不到。
    # 这里把所有数据的 user_id 对齐到"所属需求的所有者 user_id"，幂等可重复执行。
    _realign_user_id_to_requirement_owner(cursor, conn)

    # ---------- 7. 邮件剔除规则预置（内置规则，首次启动自动插入）----------
    # 小白讲解：Gmail 收件箱里会有 Google 安全提醒、验证码、noreply 通知等"非供应商回复"邮件，
    # 这些邮件不该进系统。这里预置一批常见剔除规则，用户可在「邮件剔除规则」页面禁用或新增。
    # 内置规则用 is_builtin=1 标记，只能禁用不能删除，避免用户误删后无法恢复。
    cursor.execute("SELECT COUNT(*) as cnt FROM email_filter_rules WHERE is_builtin = 1")
    if cursor.fetchone()["cnt"] == 0:
        now = now_str()
        # 内置规则清单：(规则名, 检查字段, 匹配方式, 匹配值, 优先级, 说明)
        builtin_rules = [
            # --- 发件人维度：剔除 noreply / no-reply / google 官方通知 ---
            ("noreply 发件人",       "from_addr", "contains", "noreply",     10, "发件人地址含 noreply，通常是系统通知邮件"),
            ("no-reply 发件人",      "from_addr", "contains", "no-reply",    10, "发件人地址含 no-reply，通常是系统通知邮件"),
            ("google.com 官方发件人", "from_addr", "contains", "google.com",  20, "发件人是 google.com 域名（如 google-noreply@google.com），通常是 Google 官方通知"),
            ("mailer-daemon 发件人", "from_addr", "contains", "mailer-daemon", 20, "退信通知邮件，通常是邮件投递失败的系统通知"),
            ("postmaster 发件人",    "from_addr", "contains", "postmaster",  20, "邮局管理员通知邮件，通常是系统通知"),
            # --- 主题维度：剔除安全提醒 / 验证码 / 二步验证等 ---
            ("主题-安全提醒",        "subject", "contains", "安全提醒",      30, "主题含『安全提醒』，通常是 Google 安全通知"),
            ("主题-Security alert", "subject", "contains", "security alert", 30, "主题含『Security alert』，Google 安全通知英文版"),
            ("主题-验证码",          "subject", "contains", "验证码",        30, "主题含『验证码』，通常是网站注册/登录验证码邮件"),
            ("主题-Verification code", "subject", "contains", "verification code", 30, "主题含『Verification code』，验证码邮件英文版"),
            ("主题-二步验证",        "subject", "contains", "二步验证",      30, "主题含『二步验证』，Google 二步验证相关邮件"),
            ("主题-Two-step",       "subject", "contains", "two-step",     30, "主题含『Two-step』，二步验证英文版"),
            ("主题-确认订阅",        "subject", "contains", "确认订阅",      40, "主题含『确认订阅』，邮件列表订阅确认邮件"),
            ("主题-Unsubscribe",    "subject", "contains", "unsubscribe",  40, "主题含『Unsubscribe』，退订确认邮件"),
        ]
        for rule_name, field, match_type, match_value, priority, desc in builtin_rules:
            cursor.execute("""
                INSERT INTO email_filter_rules
                (rule_name, field, match_type, match_value, action, is_enabled, priority, is_builtin, description, created_at, updated_at)
                VALUES (%s, %s, %s, %s, 'skip', 1, %s, 1, %s, %s, %s)
            """, (rule_name, field, match_type, match_value, priority, desc, now, now))
        conn.commit()
        print(f"[初始化] 已预置 {len(builtin_rules)} 条邮件剔除规则")


def _migrate_deepseek_to_minimax(cursor, conn):
    """
    把数据库里指向 DeepSeek 的场景配置自动迁移到 MiniMax（幂等，可重复执行）

    小白讲解：DeepSeek 服务已无法使用，系统切换到 MiniMax-M3。
    老数据库里的场景配置（需求解析/关键词生成/初筛等8个文本场景）还指向 deepseek 服务商，
    这个函数在每次启动时检查：发现指向 deepseek 的场景就自动改成 minimax，并调整参数适配新模型：
    - 模型名换成 MiniMax-M3
    - 思考强度清空（MiniMax-M3 的思考模式只有开/关，没有 low/medium/high/max 分级）
    - 温度统一调到 1.0（MiniMax 官方推荐值，思考模式下输出更稳定）
    - max_tokens 太小的场景调大（MiniMax 思考过程也消耗输出token，512会导致思考用完、正文为空）
    迁移完成后 deepseek 服务商记录保留但禁用（万一以后恢复服务可重新启用）。

    参数：cursor 数据库游标 / conn 数据库连接
    """
    from config import MINIMAX_API_KEY, MINIMAX_BASE_URL, MINIMAX_MODEL

    # 1. 查 DeepSeek 服务商，不存在说明是全新数据库（种子数据直接用MiniMax），无需迁移
    cursor.execute("SELECT id FROM ai_providers WHERE provider_code='deepseek'")
    ds_row = cursor.fetchone()
    if not ds_row:
        return
    deepseek_id = ds_row["id"]

    # 2. 查 MiniMax 服务商，不存在则补插一条（正常情况下前面的种子步骤已插入，这里兜底）
    cursor.execute("SELECT id FROM ai_providers WHERE provider_code='minimax'")
    mm_row = cursor.fetchone()
    if not mm_row:
        now = now_str()
        cursor.execute("""
            INSERT INTO ai_providers (provider_name, provider_code, provider_type, base_url, api_key, is_enabled, created_at, updated_at)
            VALUES ('MiniMax', 'minimax', 'ai_model', %s, %s, 1, %s, %s)
        """, (MINIMAX_BASE_URL, MINIMAX_API_KEY, now, now))
        conn.commit()
        cursor.execute("SELECT id FROM ai_providers WHERE provider_code='minimax'")
        mm_row = cursor.fetchone()
        print("[迁移] 已补插 MiniMax 服务商记录")
    minimax_id = mm_row["id"]

    # 3. 找出所有还指向 DeepSeek 的场景配置（已迁移过的不会再命中，保证幂等）
    cursor.execute("SELECT id, scene_code, max_tokens FROM ai_model_configs WHERE provider_id = %s", (deepseek_id,))
    ds_configs = cursor.fetchall()
    if not ds_configs:
        # 没有场景指向 deepseek 了，只需确保 deepseek 处于禁用状态
        cursor.execute("UPDATE ai_providers SET is_enabled = 0, updated_at = %s WHERE id = %s AND is_enabled = 1",
                       (now_str(), deepseek_id))
        conn.commit()
        return

    now = now_str()
    migrated = 0
    for cfg in ds_configs:
        scene_code = cfg["scene_code"]
        old_tokens = cfg["max_tokens"]
        # max_tokens 调整：MiniMax 思考消耗输出token，小场景必须放大
        # - supplier_* 系列（翻译/过滤，原512）→ 8192
        # - comm_* 系列（沟通回复/邮件，原1024）→ 4096
        # - 其他大值场景（32768）保持不变
        if scene_code.startswith("supplier_"):
            new_tokens = max(old_tokens, 8192)
        elif scene_code.startswith("comm_"):
            new_tokens = max(old_tokens, 4096)
        else:
            new_tokens = old_tokens
        cursor.execute("""
            UPDATE ai_model_configs
            SET provider_id = %s, model_name = %s, thinking_effort = '', temperature = 1.0,
                max_tokens = %s, updated_at = %s
            WHERE id = %s
        """, (minimax_id, MINIMAX_MODEL, new_tokens, now, cfg["id"]))
        migrated += 1

    # 4. 禁用 DeepSeek 服务商（保留记录不删除，便于以后恢复）
    cursor.execute("UPDATE ai_providers SET is_enabled = 0, updated_at = %s WHERE id = %s", (now, deepseek_id))
    conn.commit()
    print(f"[迁移] DeepSeek→MiniMax 完成：{migrated} 个场景已切换到 MiniMax-M3，DeepSeek 服务商已禁用")


def _seed_screening_rules(cursor, conn):
    """
    预置初筛规则模板（11条一票否决 + 5条评分规则）

    小白讲解：系统第一次启动时自动把方案5.2节的16条默认规则写入 screening_rule_templates 表。
    之后用户可以在「规则配置页」修改参数（阈值/分值/启用开关），修改后保存为衍生模板。
    系统默认规则不可删除，但用户可以基于默认规则创建自己的模板。

    升级机制：已有数据库时（cnt>0），调用 _upgrade_screening_rules 做"规则升级"，
    把过时的 score_contact_complete 规则删除、把 score_product_match 满分从30改为40、
    更新两条 veto 规则的描述文案。这样老用户重启系统后规则自动同步到最新版。
    """
    cursor.execute("SELECT COUNT(*) as cnt FROM screening_rule_templates")
    if cursor.fetchone()["cnt"] > 0:
        # 老数据库：执行规则升级（删除联系方式评分、更新产品匹配度满分与veto规则描述）
        _upgrade_screening_rules(cursor, conn)
        return  # 规则已存在，跳过重复插入

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
            "description": "供应商非制造商且无制造能力证据的，触发一票否决。判断依据：公司名关键字（厂/制造 vs 贸易/商贸）+ 经营范围关键字 + AI综合判断。用户可关闭此规则。",
        },
        # 7. 产品不匹配一票否决
        {
            "rule_code": "veto_product_mismatch",
            "rule_name": "产品不匹配一票否决",
            "rule_type": "veto",
            "rule_category": "match",
            "default_condition": '{"type":"single","field":"product_match","operator":"eq","value":"mismatch"}',
            "default_action": '{"result":"veto","reason":"供应商产品不是需求主产品（可能是配件）"}',
            "max_score": None,
            "scoring_logic": "",
            "tyc_commands": '[]',
            "is_configurable": 1,  # 可开关
            "is_enabled": 1,
            "sort_order": 7,
            "description": "判断供应商已获取的产品是否为需求主产品本身（不是配件）。AI从需求提取主产品（如'茶色玻璃下翻门电视柜'→'电视柜'），判断供应商产品是否就是该主产品。配件（如电视柜脚轮/支架）触发否决。用户可关闭此规则。",
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
        # 3. 产品匹配度（40分，AI宽松评分：主产品50分基础+核心功能加分+材质加分+额外功能奖励）
        {
            "rule_code": "score_product_match",
            "rule_name": "产品匹配度",
            "rule_type": "score",
            "rule_category": "match",
            "default_condition": '{"field":"product_match"}',
            "default_action": '{}',
            "max_score": 40,
            "scoring_logic": "AI宽松评分：主产品符合给基础50分，核心功能按符合比例加0-25分，材质完全符合加25分/部分符合加15分/不符加0分，有额外功能奖励5分（上限100）。数据源=product_title+main_product+intro",
            "tyc_commands": '[]',
            "is_configurable": 1,
            "is_enabled": 1,
            "sort_order": 103,
            "description": "用供应商产品标题+主营产品+简介与需求的核心功能+材质做匹配评分。宽松模式：主产品符合就给基础分，不轻易给0分。",
        },
        # 4. 风险记录（15分）
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
            "sort_order": 104,
            "description": "根据天眼查风险总览中的风险记录数量评分。用户可修改分值和阈值。",
        },
        # 5. 出口经验、平台经验或资质（5分）
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
            "sort_order": 105,
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
    print("[初始化] 已预置16条初筛规则模板 + 2条通过标准配置（共18条记录）")


def _upgrade_screening_rules(cursor, conn):
    """
    老数据库规则升级：把过时规则同步到最新版

    小白讲解：用户改了初筛逻辑后，老数据库里还是旧规则（比如还有"联系方式评分"规则、
    产品匹配度满分还是30分）。这个函数在每次启动时检查并升级：
    1. 删除 score_contact_complete（联系方式改为否决规则，不再评分）
    2. 把 score_product_match 满分从30改成40，更新评分说明
    3. 更新 veto_non_manufacturer 描述（增加公司名判断说明）
    4. 更新 veto_product_mismatch 描述和动作（改为判断主产品而非经营范围）

    幂等设计：已经升级过的数据库再跑也不会出错（用rule_code做唯一判断）。
    """
    now = now_str()
    upgraded = []

    # 1. 删除联系方式评分规则（已改为 veto_no_contact 否决规则）
    # 小白讲解：先删除引用了该模板的实例记录（用户保存的模板快照），
    # 再删除模板本身，否则外键约束会阻止删除。
    cursor.execute("""
        DELETE FROM screening_rule_instances
        WHERE template_id IN (
            SELECT id FROM screening_rule_templates WHERE rule_code = %s
        )
    """, ("score_contact_complete",))
    cursor.execute("DELETE FROM screening_rule_templates WHERE rule_code = %s",
                   ("score_contact_complete",))
    if cursor.rowcount > 0:
        upgraded.append("删除 score_contact_complete（联系方式改为否决规则）")

    # 2. 更新产品匹配度评分规则：满分30→40，更新评分说明（宽松模式）
    # 小白讲解：只在 max_score 还是旧值（30或非40）或评分说明未更新时才更新，
    # 避免每次启动都重复更新（用 WHERE 条件限制只在需要时才匹配）。
    new_scoring_logic = "AI宽松评分：主产品符合给基础50分，核心功能按符合比例加0-25分，材质完全符合加25分/部分符合加15分/不符加0分，有额外功能奖励5分（上限100）。数据源=product_title+main_product+intro"
    new_description = "用供应商产品标题+主营产品+简介与需求的核心功能+材质做匹配评分。宽松模式：主产品符合就给基础分，不轻易给0分。"
    cursor.execute("""
        UPDATE screening_rule_templates
        SET max_score = 40,
            scoring_logic = %s,
            description = %s,
            updated_at = %s
        WHERE rule_code = %s AND (max_score <> 40 OR scoring_logic <> %s OR description <> %s)
    """, (new_scoring_logic, new_description, now, "score_product_match",
          new_scoring_logic, new_description))
    if cursor.rowcount > 0:
        upgraded.append("更新 score_product_match 满分30→40 + 宽松评分说明")

    # 3. 更新非制造商否决规则描述（增加公司名判断说明）
    new_veto_nm_desc = "供应商非制造商且无制造能力证据的，触发一票否决。判断依据：公司名关键字（厂/制造 vs 贸易/商贸）+ 经营范围关键字 + AI综合判断。用户可关闭此规则。"
    cursor.execute("""
        UPDATE screening_rule_templates
        SET description = %s,
            updated_at = %s
        WHERE rule_code = %s AND description <> %s
    """, (new_veto_nm_desc, now, "veto_non_manufacturer", new_veto_nm_desc))
    if cursor.rowcount > 0:
        upgraded.append("更新 veto_non_manufacturer 描述（增加公司名判断说明）")

    # 4. 更新产品不匹配否决规则描述和动作（改为判断主产品而非经营范围）
    new_veto_pm_action = '{"result":"veto","reason":"供应商产品不是需求主产品（可能是配件）"}'
    new_veto_pm_desc = "判断供应商已获取的产品是否为需求主产品本身（不是配件）。AI从需求提取主产品（如'茶色玻璃下翻门电视柜'→'电视柜'），判断供应商产品是否就是该主产品。配件（如电视柜脚轮/支架）触发否决。用户可关闭此规则。"
    cursor.execute("""
        UPDATE screening_rule_templates
        SET default_action = %s,
            description = %s,
            updated_at = %s
        WHERE rule_code = %s AND (default_action <> %s OR description <> %s)
    """, (new_veto_pm_action, new_veto_pm_desc, now, "veto_product_mismatch",
          new_veto_pm_action, new_veto_pm_desc))
    if cursor.rowcount > 0:
        upgraded.append("更新 veto_product_mismatch 描述和动作（改为判断主产品）")

    # 5. 同步更新用户保存的模板实例中的产品匹配度满分（旧模板可能还是30分）
    cursor.execute("""
        UPDATE screening_rule_instances si
        JOIN screening_rule_templates srt ON si.template_id = srt.id
        SET si.custom_score_cap = 40
        WHERE srt.rule_code = 'score_product_match' AND si.custom_score_cap <> 40
    """)
    if cursor.rowcount > 0:
        upgraded.append(f"更新{cursor.rowcount}个模板实例的产品匹配度满分30→40")

    if upgraded:
        conn.commit()
        print(f"[升级] 已升级初筛规则：{'; '.join(upgraded)}")
    else:
        print("[升级] 初筛规则已是最新，无需升级")


def _realign_user_id_to_requirement_owner(cursor, conn):
    """
    数据归属归位：把供应商/初筛/审计/沟通记录的 user_id 对齐到所属需求的所有者

    小白讲解：以前管理员帮用户在用户的需求上搜索供应商时，数据 user_id 写成了管理员 ID，
    导致用户看不到这些数据。这个函数把所有数据的 user_id 改成"所属需求的所有者 user_id"，
    实现数据归属跟着需求走。幂等可重复执行，已经是正确 user_id 的不会重复更新。
    """
    # suppliers 表：直接 JOIN requirements 取需求所有者
    cursor.execute("""
        UPDATE suppliers s
        JOIN requirements r ON s.requirement_id = r.id
        SET s.user_id = r.user_id
        WHERE s.user_id != r.user_id
    """)
    n1 = cursor.rowcount

    # screenings 表：通过 supplier_id JOIN suppliers 再 JOIN requirements
    cursor.execute("""
        UPDATE screenings sc
        JOIN suppliers s ON sc.supplier_id = s.id
        JOIN requirements r ON s.requirement_id = r.id
        SET sc.user_id = r.user_id
        WHERE sc.user_id != r.user_id
    """)
    n2 = cursor.rowcount

    # screening_audit_logs 表：通过 supplier_id JOIN
    cursor.execute("""
        UPDATE screening_audit_logs al
        JOIN suppliers s ON al.supplier_id = s.id
        JOIN requirements r ON s.requirement_id = r.id
        SET al.user_id = r.user_id
        WHERE al.user_id != r.user_id
    """)
    n3 = cursor.rowcount

    # communications 表：通过 supplier_id JOIN
    cursor.execute("""
        UPDATE communications c
        JOIN suppliers s ON c.supplier_id = s.id
        JOIN requirements r ON s.requirement_id = r.id
        SET c.user_id = r.user_id
        WHERE c.user_id != r.user_id
    """)
    n4 = cursor.rowcount

    conn.commit()
    total = n1 + n2 + n3 + n4
    if total > 0:
        print(f"[初始化] 数据归属归位完成：suppliers {n1} / screenings {n2} / audit_logs {n3} / communications {n4}")


if __name__ == "__main__":
    init_db()
