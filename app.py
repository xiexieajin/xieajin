"""
供应商寻源系统 - Flask 主应用

这是整个网页系统的"入口"，所有的网页路由（URL地址）都在这里定义。
比如访问 http://localhost:5000/ 就会打开首页。

运行方法：
    python app.py
然后在浏览器打开 http://localhost:5000
"""

from flask import Flask, render_template, request, redirect, url_for, g, flash, Response, stream_with_context, session, jsonify
import os
import json
import queue
import threading
from functools import wraps
import db
from db import get_db, now_str, SUPPLIER_STAGES, REQUIREMENT_STATUSES, hash_password, verify_password, recalc_requirement_status
from config import is_api_configured, is_vision_configured
# 小白讲解：导入model_config模块用于启动时加载配置和修改后热更新
import model_config

# 小白讲解：SSE流式响应中无法写入session cookie（cookie只在响应结束时发送），
# 所以用这个字典临时存放解析结果，前端通过result_id来取。
_tmp_results = {}

# 创建 Flask 应用实例
app = Flask(__name__)
# 自动加载模板修改（开发时方便，不用每次重启）
app.config["TEMPLATES_AUTO_RELOAD"] = True
# 小白讲解：让Jinja2的tojson过滤器保留中文，不转成\uXXXX编码
# 默认ensure_ascii=True会把中文转成Unicode转义序列，导致存库后编辑页显示乱码
app.config["JSON_AS_ASCII"] = False
# 文件上传相关配置
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 最大上传16MB
app.config["UPLOAD_FOLDER"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
# 小白讲解：secret_key用于加密session cookie，必须改成随机字符串防止伪造
# 用固定随机串保证重启后已登录用户session仍有效（比写死的"sourcing-system-secret-key"安全得多）
app.secret_key = "f3a9c7e2b1d84f6a0e5c3b9d7a2f8e4c1b6a9d3f7e2c8b5a1d4f9e6c3b7a2e8f5"


# ==================== 全局任务存储（轮询模式 + 断线恢复）====================
# 小白讲解：Railway代理会切断30分钟+的长连接，导致搜索中断。改为轮询模式：
# 浏览器提交搜索任务 → 拿到task_id（瞬间返回）→ 每3秒轮询进度 → 搜索完成后拿到结果。
# 断线恢复：所有进度消息保留在messages列表里（不再消费式读取），用户刷新页面后
# 可以通过 status 接口找到正在运行的任务，用全部历史消息重建进度界面。
# 结构：{task_id: {"messages": [...], "status": "running"|"done"|"error", "result": {...}, "req_id": 123}}
task_store = {}
# 小白讲解：线程锁，保护task_store的读写（后台线程写messages，轮询接口读messages，需要加锁避免冲突）
import threading as _threading
_task_store_lock = _threading.Lock()

# 注册自定义Jinja2过滤器：把JSON字符串解析成Python对象（用于展示P0-P3关键词）
@app.template_filter("from_json")
def from_json_filter(value):
    """把JSON字符串转成字典，解析失败返回None"""
    try:
        return json.loads(value) if value else None
    except (json.JSONDecodeError, TypeError):
        return None


# 注册模板全局函数：构建表头排序链接的查询串（保留当前筛选条件，切换升降序）
# 小白讲解：list.html 表头点击排序时调用此函数，生成类似 "order_by=id&sort=asc&requirement_id=5" 的参数串。
# 点击当前正排序的列→切换升降序；点击其他列→默认降序。
# 同时保留当前URL上的所有筛选参数（需求/来源/阶段等），避免点击排序后筛选条件丢失。
# 注意：@app.template_global() 必须带括号调用，不带括号会导致函数无法注册到模板环境。
@app.template_global()
def _build_sort_query(field, current_order_by, current_sort):
    """
    构建排序URL参数串（保留当前筛选条件）

    参数：
        field: 本次点击要排序的字段名（如 'id'、'quality_score'）
        current_order_by: 当前正在排序的字段名
        current_sort: 当前排序方向（'asc' 或 'desc'）
    返回：URL查询串字符串（已URL编码），如 "order_by=id&sort=asc&requirement_id=5"
    """
    from urllib.parse import urlencode
    # 1. 复制当前URL上所有查询参数（保留筛选条件）
    params = {}
    for key, values in request.args.lists():
        # 保留多选参数（如 requirement_id 多次出现）
        params[key] = values
    # 2. 点击的就是当前排序列：升降序切换（asc↔desc）
    if field == current_order_by:
        new_sort = "asc" if current_sort == "desc" else "desc"
    else:
        # 点击新列：默认降序（数字大的排前面，更符合直觉）
        new_sort = "desc"
    # 3. 覆盖排序参数（单值，直接赋字符串）
    params["order_by"] = field
    params["sort"] = new_sort
    # 4. doseq=True 让多值参数正确展开成多个 key=v（如 requirement_id=5&requirement_id=6）
    return urlencode(params, doseq=True)


@app.before_request
def before_request():
    """每次请求前：连接数据库、加载当前登录用户、做路由权限检查（Flask 自动调用）"""
    # 健康检查端点：不需要数据库连接，直接放行
    if request.endpoint == "health":
        return

    try:
        g.db = get_db()
    except Exception as e:
        print(f"[before_request] 数据库连接失败: {e}")
        return "数据库连接失败，请稍后重试", 500

    g.current_user = None
    g.user_id = None
    # 小白讲解：从session中取user_id，查用户信息存入g.current_user供后续使用
    user_id = session.get("user_id")
    if user_id:
        cursor = g.db.cursor()
        cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()
        if user:
            # 检查账号是否被停用，停用则强制登出
            if not user["is_active"]:
                session.clear()
                g.current_user = None
                g.user_id = None
            else:
                g.current_user = user
                g.user_id = user["id"]

    # ==================== 路由权限统一检查（白名单机制）====================
    # 小白讲解：不需要给每个路由单独加装饰器，在这里统一判断。
    # 公开路由白名单：未登录也能访问
    public_endpoints = {"login", "logout", "static", "health"}
    endpoint = request.endpoint
    # 静态文件和公开路由放行
    if endpoint is None or endpoint in public_endpoints:
        return
    # 其他所有路由都要求已登录
    if not g.current_user:
        return redirect(url_for("login", next=request.url))
    # /admin 开头的路由要求 admin 角色
    if request.path.startswith("/admin") and g.current_user["role"] != "admin":
        flash("无权限访问该页面", "danger")
        return redirect(url_for("index"))


# ==================== 登录验证装饰器 ====================
def login_required(f):
    """装饰器：要求用户已登录才能访问该路由，未登录跳转登录页"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not g.current_user:
            # 保存用户原本要去的URL，登录成功后跳回
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """装饰器：要求用户是admin角色才能访问，普通用户被拒"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not g.current_user:
            return redirect(url_for("login", next=request.url))
        if g.current_user["role"] != "admin":
            flash("无权限访问该页面", "danger")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated


# ==================== 数据隔离辅助函数 ====================
def _uid_clause(alias=""):
    """
    生成数据隔离的WHERE子句片段（管理员不过滤，普通用户只看自己的数据）

    小白讲解：普通用户只能看到自己创建的需求/供应商，管理员能看到所有人的。
    调用时传表别名（如"r."或"s."），返回 ("AND r.user_id = %s", [用户id]) 或 ("", [])

    参数：alias 表别名前缀，如 "r." 或 "s."，用于多表JOIN时区分
    返回：元组 (sql_fragment, params_list)
    """
    # 管理员不限数据范围
    if g.current_user and g.current_user["role"] == "admin":
        return ("", [])
    # 普通用户只看自己的数据
    return (f"AND {alias}user_id = %s", [g.user_id])


# ==================== 登录/登出 ====================
@app.route("/login", methods=["GET", "POST"])
def login():
    """登录页 - GET显示表单，POST验证账号密码"""
    # 已登录用户直接回首页
    if g.current_user:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            return render_template("login.html", error="请输入用户名和密码", username=username)

        cursor = g.db.cursor()
        cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
        user = cursor.fetchone()

        # 验证账号存在、密码正确、账号已启用
        if not user or not verify_password(password, user["password_hash"]):
            return render_template("login.html", error="用户名或密码错误", username=username)
        if not user["is_active"]:
            return render_template("login.html", error="该账号已被停用，请联系管理员", username=username)

        # 登录成功：把user_id存入session
        session.clear()
        session["user_id"] = user["id"]
        # 登录后跳转到next参数指定的页面，没有则回首页
        next_url = request.args.get("next")
        if not next_url or not next_url.startswith("/"):
            next_url = url_for("index")
        return redirect(next_url)

    return render_template("login.html")


@app.route("/logout")
def logout():
    """退出登录 - 清除session后跳转登录页"""
    session.clear()
    flash("您已退出登录", "info")
    return redirect(url_for("login"))


@app.teardown_request
def teardown_request(exception):
    """每次请求后关闭数据库连接（Flask 自动调用）"""
    db_conn = getattr(g, "db", None)
    if db_conn is not None:
        db_conn.close()


# ==================== 首页 ====================
@app.route("/")
def index():
    """
    首页/仪表盘 - 显示系统概览信息

    展示：需求数量、供应商数量、各阶段供应商统计等
    """
    cursor = g.db.cursor()

    # 统计需求数量（管理员看全部，普通用户只看自己的）
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"SELECT COUNT(*) as count FROM requirements WHERE 1=1 {uid_sql}", uid_params)
    req_count = cursor.fetchone()["count"]

    # 统计供应商数量（同上，加数据隔离）
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"SELECT COUNT(*) as count FROM suppliers WHERE 1=1 {uid_sql}", uid_params)
    supplier_count = cursor.fetchone()["count"]

    # 统计各开发阶段的供应商数量
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"""
        SELECT dev_stage, COUNT(*) as count
        FROM suppliers
        WHERE 1=1 {uid_sql}
        GROUP BY dev_stage
        ORDER BY count DESC
    """, uid_params)
    stage_stats = cursor.fetchall()

    # 获取最近添加的5个供应商（JOIN requirements，用s.别名过滤当前用户的供应商）
    uid_sql, uid_params = _uid_clause("s.")
    cursor.execute(f"""
        SELECT s.*, r.product_name
        FROM suppliers s
        JOIN requirements r ON s.requirement_id = r.id
        WHERE 1=1 {uid_sql}
        ORDER BY s.created_at DESC
        LIMIT 5
    """, uid_params)
    recent_suppliers = cursor.fetchall()

    return render_template("index.html",
                           req_count=req_count,
                           supplier_count=supplier_count,
                           stage_stats=stage_stats,
                           recent_suppliers=recent_suppliers,
                           stages=SUPPLIER_STAGES)


# ==================== 需求管理 ====================
@app.route("/requirements")
def requirement_list():
    """需求列表页 - 显示所有采购需求，支持按状态(多选)和名称筛选"""
    cursor = g.db.cursor()

    # 接收筛选参数：status 支持多选（getlist），name_search 是文本搜索保持单值
    status_list = request.args.getlist("status")
    name_search = request.args.get("name_search", "").strip()

    # 动态拼接WHERE条件（多选状态用 IN 查询，OR关系；跨字段 AND关系）
    where_clauses = []
    params = []
    if status_list:
        # 多个状态值用 IN (%s, %s, ...) 占位符，防SQL注入
        placeholders = ",".join("%s" for _ in status_list)
        where_clauses.append(f"r.status IN ({placeholders})")
        params.extend(status_list)
    if name_search:
        where_clauses.append("r.product_name LIKE %s")
        params.append(f"%{name_search}%")

    # 数据隔离：普通用户只看自己创建的需求（_uid_clause返回"AND r.user_id = %s"，去掉AND前缀后追加）
    uid_sql, uid_params = _uid_clause("r.")
    if uid_sql:
        where_clauses.append(uid_sql[4:])  # 去掉开头的"AND "，因为下方用" AND "拼接
        params.extend(uid_params)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    sql = f"""
        SELECT r.*,
               (SELECT COUNT(*) FROM suppliers WHERE requirement_id = r.id) as supplier_count
        FROM requirements r
        {where_sql}
        ORDER BY r.created_at DESC
    """
    cursor.execute(sql, params)
    requirements = cursor.fetchall()

    return render_template("requirement/list.html",
                           requirements=requirements,
                           current_status_list=status_list,
                           current_name_search=name_search,
                           requirement_statuses=REQUIREMENT_STATUSES)


@app.route("/requirements/create", methods=["GET", "POST"])
def requirement_create():
    """创建需求 - GET显示表单，POST处理提交"""
    if request.method == "POST":
        # 从表单获取数据
        data = {
            "product_name": request.form.get("product_name", "").strip(),
            "product_aliases": request.form.get("product_aliases", "").strip(),
            "core_functions": request.form.get("core_functions", "").strip(),
            "material": request.form.get("material", "").strip(),
            "first_purchase_qty": request.form.get("first_purchase_qty", "").strip(),
            "daily_replenish_qty": request.form.get("daily_replenish_qty", "").strip(),
            "max_stock_qty": request.form.get("max_stock_qty", "").strip(),
            "acceptable_moq": request.form.get("acceptable_moq", "").strip(),
            "acceptable_lead_time": request.form.get("acceptable_lead_time", "").strip(),
            "target_market": request.form.get("target_market", "").strip(),
            "required_certs": request.form.get("required_certs", "").strip(),
            "customization_req": request.form.get("customization_req", "").strip(),
            "requirement_summary": request.form.get("requirement_summary", "").strip(),
            "keywords": request.form.get("keywords", "").strip(),
        }

        # 检查必填字段
        if not data["product_name"]:
            return render_template("requirement/form.html", error="产品名称为必填项", data=data)

        # 插入数据库（user_id用于数据隔离，记录是哪个用户创建的）
        cursor = g.db.cursor()
        cursor.execute("""
            INSERT INTO requirements
            (product_name, product_aliases, core_functions, material,
             first_purchase_qty, daily_replenish_qty, max_stock_qty,
             acceptable_moq, acceptable_lead_time, target_market,
             required_certs, customization_req, requirement_summary, keywords,
             status, created_at, updated_at, user_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '需求确认中', %s, %s, %s)
        """, (*data.values(), now_str(), now_str(), g.user_id))
        g.db.commit()

        return redirect(url_for("requirement_list"))

    # GET请求：显示空表单
    return render_template("requirement/form.html", data=None)


@app.route("/requirements/<int:id>")
def requirement_detail(id):
    """需求详情页 - 显示需求信息和关联的供应商"""
    cursor = g.db.cursor()
    # 查询需求信息（加数据隔离，普通用户只能看自己创建的）
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"SELECT * FROM requirements WHERE id = %s {uid_sql}", (id, *uid_params))
    requirement = cursor.fetchone()
    if not requirement:
        return "需求不存在", 404

    # 查询该需求下的所有供应商（同上加uid过滤）
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"""
        SELECT * FROM suppliers
        WHERE requirement_id = %s {uid_sql}
        ORDER BY created_at DESC
    """, (id, *uid_params))
    suppliers = cursor.fetchall()

    # 统计各开发阶段的供应商数量，让详情页一眼看出"该需求当前在哪个阶段"
    stage_stats = {}
    for s in suppliers:
        stage = s["dev_stage"] or "未知"
        stage_stats[stage] = stage_stats.get(stage, 0) + 1

    return render_template("requirement/detail.html",
                           requirement=requirement, suppliers=suppliers,
                           stage_stats=stage_stats, supplier_stages=SUPPLIER_STAGES,
                           requirement_statuses=REQUIREMENT_STATUSES)


@app.route("/requirements/<int:id>/edit", methods=["GET", "POST"])
def requirement_edit(id):
    """编辑需求 - GET显示表单，POST处理提交"""
    cursor = g.db.cursor()

    if request.method == "POST":
        data = {
            "product_name": request.form.get("product_name", "").strip(),
            "product_aliases": request.form.get("product_aliases", "").strip(),
            "core_functions": request.form.get("core_functions", "").strip(),
            "material": request.form.get("material", "").strip(),
            "first_purchase_qty": request.form.get("first_purchase_qty", "").strip(),
            "daily_replenish_qty": request.form.get("daily_replenish_qty", "").strip(),
            "max_stock_qty": request.form.get("max_stock_qty", "").strip(),
            "acceptable_moq": request.form.get("acceptable_moq", "").strip(),
            "acceptable_lead_time": request.form.get("acceptable_lead_time", "").strip(),
            "target_market": request.form.get("target_market", "").strip(),
            "required_certs": request.form.get("required_certs", "").strip(),
            "customization_req": request.form.get("customization_req", "").strip(),
            "requirement_summary": request.form.get("requirement_summary", "").strip(),
            "keywords": request.form.get("keywords", "").strip(),
            "status": request.form.get("status", "需求确认中"),
        }

        # 数据隔离：UPDATE需求时确保只能改自己创建的（普通用户）
        uid_sql, uid_params = _uid_clause()
        cursor.execute(f"""
            UPDATE requirements SET
                product_name=%s, product_aliases=%s, core_functions=%s, material=%s,
                first_purchase_qty=%s, daily_replenish_qty=%s, max_stock_qty=%s,
                acceptable_moq=%s, acceptable_lead_time=%s, target_market=%s,
                required_certs=%s, customization_req=%s, requirement_summary=%s, keywords=%s,
                status=%s, updated_at=%s
            WHERE id=%s {uid_sql}
        """, (*data.values(), now_str(), id, *uid_params))

        # 需求状态变更时联动调整关联供应商的开发阶段（同样加uid过滤，避免改到别人的供应商）
        # 小白讲解：手动改需求状态时，只在"已完成"做收尾（把还没初筛的供应商标记为未通过）。
        # 其他状态不强行改供应商阶段——需求状态主要由供应商进度自动推断（见 recalc_requirement_status）。
        new_status = data["status"]
        if new_status == "已完成":
            # 需求标记完成时：还没初筛的供应商收尾为"未通过初筛"，已通过/沟通中/已合作的保留
            uid_sql, uid_params = _uid_clause()
            cursor.execute(f"""
                UPDATE suppliers SET dev_stage='未通过初筛', updated_at=%s
                WHERE requirement_id=%s AND dev_stage = '已寻源待初筛' {uid_sql}
            """, (now_str(), id, *uid_params))

        g.db.commit()

        return redirect(url_for("requirement_detail", id=id))

    # GET请求：显示当前数据（加uid过滤，防止看到别人的需求）
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"SELECT * FROM requirements WHERE id = %s {uid_sql}", (id, *uid_params))
    requirement = cursor.fetchone()
    if not requirement:
        return "需求不存在", 404

    return render_template("requirement/form.html", data=requirement, edit=True,
                           requirement_statuses=REQUIREMENT_STATUSES)


@app.route("/requirement/delete/<int:id>", methods=["POST"])
def requirement_delete(id):
    """
    删除需求 - 同时级联删除关联的供应商、初筛记录、沟通记录

    删除顺序（因为SQLite默认不开启外键级联，需要手动按依赖顺序删）：
    1. 查出该需求下所有供应商ID
    2. 删除这些供应商的初筛记录(screenings)
    3. 删除这些供应商的沟通记录(communications)
    4. 删除这些供应商(suppliers)
    5. 最后删除需求本身(requirements)
    """
    cursor = g.db.cursor()

    # 先确认需求存在（加uid过滤，普通用户只能删自己的）
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"SELECT id FROM requirements WHERE id = %s {uid_sql}", (id, *uid_params))
    if not cursor.fetchone():
        flash("需求不存在，无法删除", "danger")
        return redirect(url_for("requirement_list"))

    # 1. 查出该需求下所有供应商ID（同样加uid过滤）
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"SELECT id FROM suppliers WHERE requirement_id = %s {uid_sql}", (id, *uid_params))
    supplier_ids = [row["id"] for row in cursor.fetchall()]

    # 2-3. 删除这些供应商的初筛审计日志、初筛记录和沟通记录（如果有供应商才删）
    if supplier_ids:
        # 小白讲解：screening_audit_logs/screenings/communications表没有user_id列，不能加uid过滤。
        # 但supplier_id已经唯一关联到当前用户的供应商，不会误删别人的数据。
        # 删除顺序：先删审计日志(有外键指向suppliers)→再删screenings→再删communications→最后删suppliers
        placeholders = ",".join("%s" for _ in supplier_ids)
        cursor.execute(f"DELETE FROM screening_audit_logs WHERE supplier_id IN ({placeholders})",
                       tuple(supplier_ids))
        cursor.execute(f"DELETE FROM screenings WHERE supplier_id IN ({placeholders})",
                       tuple(supplier_ids))
        cursor.execute(f"DELETE FROM communications WHERE supplier_id IN ({placeholders})",
                       tuple(supplier_ids))
        # 4. 删除该需求下所有供应商（加uid过滤，suppliers表有user_id列）
        uid_sql, uid_params = _uid_clause()
        cursor.execute(f"DELETE FROM suppliers WHERE requirement_id = %s {uid_sql}", (id, *uid_params))

    # 5. 最后删除需求本身（加uid过滤）
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"DELETE FROM requirements WHERE id = %s {uid_sql}", (id, *uid_params))
    g.db.commit()

    flash("需求及其关联数据已删除", "success")
    return redirect(url_for("requirement_list"))


@app.route("/requirements/batch-delete", methods=["POST"])
def requirement_batch_delete():
    """
    批量删除需求 - 同时级联删除关联的供应商、初筛记录、沟通记录

    前端通过checkbox勾选多个需求，提交ID列表到这里统一删除。
    """
    ids = request.form.getlist("requirement_ids")
    if not ids:
        flash("未选择任何需求", "warning")
        return redirect(url_for("requirement_list"))

    req_ids = []
    for rid in ids:
        try:
            req_ids.append(int(rid))
        except (ValueError, TypeError):
            continue

    if not req_ids:
        flash("未选择有效的需求", "warning")
        return redirect(url_for("requirement_list"))

    cursor = g.db.cursor()
    # 查出这些需求下所有供应商ID（加uid过滤，普通用户只能查自己的供应商）
    placeholders = ",".join("%s" for _ in req_ids)
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"SELECT id FROM suppliers WHERE requirement_id IN ({placeholders}) {uid_sql}",
                   (*req_ids, *uid_params))
    supplier_ids = [row["id"] for row in cursor.fetchall()]

    if supplier_ids:
        s_placeholders = ",".join("%s" for _ in supplier_ids)
        # 小白讲解：删除顺序：审计日志→screenings→communications→suppliers（外键约束要求）
        cursor.execute(f"DELETE FROM screening_audit_logs WHERE supplier_id IN ({s_placeholders})",
                       tuple(supplier_ids))
        cursor.execute(f"DELETE FROM screenings WHERE supplier_id IN ({s_placeholders})",
                       tuple(supplier_ids))
        cursor.execute(f"DELETE FROM communications WHERE supplier_id IN ({s_placeholders})",
                       tuple(supplier_ids))
        uid_sql, uid_params = _uid_clause()
        cursor.execute(f"DELETE FROM suppliers WHERE requirement_id IN ({placeholders}) {uid_sql}",
                       (*req_ids, *uid_params))

    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"DELETE FROM requirements WHERE id IN ({placeholders}) {uid_sql}",
                   (*req_ids, *uid_params))
    g.db.commit()

    flash(f"已批量删除 {len(req_ids)} 个需求", "success")
    return redirect(url_for("requirement_list"))


# ==================== 供应商管理（基础框架，后续完善） ====================
@app.route("/suppliers")
def supplier_list():
    """供应商列表页 - 显示所有供应商，支持多条件多选筛选"""
    cursor = g.db.cursor()

    # 接收筛选参数：5个字段全部支持多选（getlist），name_search 是文本搜索保持单值
    req_id_list = request.args.getlist("requirement_id")
    source_list = request.args.getlist("source")
    dev_stage_list = request.args.getlist("dev_stage")
    supplier_type_list = request.args.getlist("supplier_type")
    contact_status_list = request.args.getlist("contact_status")
    name_search = request.args.get("name_search", "").strip()

    # 接收排序参数（点击表头列名切换升降序）
    # 小白讲解：order_by决定按哪列排序，sort决定升序还是降序
    allowed_order = {"id", "name", "quality_score", "dev_stage", "created_at"}
    order_by = request.args.get("order_by", "created_at")
    if order_by not in allowed_order:
        order_by = "created_at"
    sort = request.args.get("sort", "desc")
    if sort not in ("asc", "desc"):
        sort = "desc"

    # 动态拼接WHERE条件（同字段多选用 IN 查询 OR关系；跨字段 AND关系）
    where_clauses = []
    params = []
    if req_id_list:
        # requirement_id 从字符串转int，过滤无效值
        req_ids = []
        for rid in req_id_list:
            try:
                req_ids.append(int(rid))
            except (ValueError, TypeError):
                continue
        if req_ids:
            placeholders = ",".join("%s" for _ in req_ids)
            where_clauses.append(f"s.requirement_id IN ({placeholders})")
            params.extend(req_ids)
    if source_list:
        placeholders = ",".join("%s" for _ in source_list)
        where_clauses.append(f"s.source IN ({placeholders})")
        params.extend(source_list)
    if dev_stage_list:
        placeholders = ",".join("%s" for _ in dev_stage_list)
        where_clauses.append(f"s.dev_stage IN ({placeholders})")
        params.extend(dev_stage_list)
    if supplier_type_list:
        placeholders = ",".join("%s" for _ in supplier_type_list)
        where_clauses.append(f"s.supplier_type IN ({placeholders})")
        params.extend(supplier_type_list)
    if contact_status_list:
        placeholders = ",".join("%s" for _ in contact_status_list)
        where_clauses.append(f"s.contact_status IN ({placeholders})")
        params.extend(contact_status_list)
    if name_search:
        where_clauses.append("s.name LIKE %s")
        params.append(f"%{name_search}%")

    # 数据隔离：普通用户只看自己创建的供应商（alias用"s."）
    uid_sql, uid_params = _uid_clause("s.")
    if uid_sql:
        where_clauses.append(uid_sql[4:])  # 去掉"AND "前缀
        params.extend(uid_params)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    # 小白讲解：LEFT JOIN screenings表获取初筛总分（quality_score字段存的是总分0-100）
    # 用子查询取每个供应商最新一条初筛记录，避免一对多导致重复行
    sql = f"""
        SELECT s.*, r.product_name,
               sc.quality_score as score
        FROM suppliers s
        JOIN requirements r ON s.requirement_id = r.id
        LEFT JOIN (
            SELECT supplier_id, MAX(id) as max_id FROM screenings GROUP BY supplier_id
        ) latest ON latest.supplier_id = s.id
        LEFT JOIN screenings sc ON sc.id = latest.max_id
        {where_sql}
        ORDER BY {order_by} {sort.upper()}
    """
    cursor.execute(sql, params)
    suppliers = cursor.fetchall()

    # 获取所有需求（供筛选按钮组使用，加uid过滤只显示自己的需求）
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"SELECT id, product_name FROM requirements WHERE 1=1 {uid_sql} ORDER BY created_at DESC",
                   uid_params)
    requirements = cursor.fetchall()

    # 获取来源平台的去重列表（供筛选按钮组使用，加uid过滤只统计自己的供应商来源）
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"SELECT DISTINCT source FROM suppliers WHERE source != '' {uid_sql} ORDER BY source",
                   uid_params)
    sources = [row["source"] for row in cursor.fetchall()]

    return render_template("supplier/list.html",
                           suppliers=suppliers, requirements=requirements,
                           current_req_list=[str(r) for r in req_id_list],
                           current_source_list=source_list,
                           current_dev_stage_list=dev_stage_list,
                           current_supplier_type_list=supplier_type_list,
                           current_contact_status_list=contact_status_list,
                           current_name_search=name_search,
                           sources=sources,
                           supplier_stages=SUPPLIER_STAGES,
                           order_by=order_by, sort=sort)


@app.route("/suppliers/<int:id>")
def supplier_detail(id):
    """供应商详情页 - 显示供应商信息、初筛结果、沟通记录"""
    cursor = g.db.cursor()
    # 查询供应商信息（加uid过滤，alias用"s."因为有表别名）
    uid_sql, uid_params = _uid_clause("s.")
    cursor.execute(f"""
        SELECT s.*, r.product_name
        FROM suppliers s
        JOIN requirements r ON s.requirement_id = r.id
        WHERE s.id = %s {uid_sql}
    """, (id, *uid_params))
    supplier = cursor.fetchone()
    if not supplier:
        return "供应商不存在", 404

    # 查询初筛结果（screenings表有user_id列，加uid过滤）
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"SELECT * FROM screenings WHERE supplier_id = %s {uid_sql}", (id, *uid_params))
    screening = cursor.fetchone()

    # 查询沟通记录（communications表有user_id列，加uid过滤；按comm_time降序）
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"""
        SELECT * FROM communications
        WHERE supplier_id = %s {uid_sql}
        ORDER BY comm_time DESC, id DESC
    """, (id, *uid_params))
    communications = cursor.fetchall()

    return render_template("supplier/detail.html",
                           supplier=supplier, screening=screening,
                           communications=communications)


@app.route("/suppliers/create", methods=["GET", "POST"])
def supplier_create():
    """手动添加供应商 - GET显示表单，POST处理提交"""
    cursor = g.db.cursor()

    if request.method == "POST":
        data = {
            "requirement_id": request.form.get("requirement_id", type=int),
            "name": request.form.get("name", "").strip(),
            "intro": request.form.get("intro", "").strip(),
            "factory_address": request.form.get("factory_address", "").strip(),
            "email": request.form.get("email", "").strip(),
            "phone": request.form.get("phone", "").strip(),
            "main_product": request.form.get("main_product", "").strip(),
            "establish_date": request.form.get("establish_date", "").strip(),
            "operating_status": request.form.get("operating_status", "存续"),
            "has_cross_border_exp": 1 if request.form.get("has_cross_border_exp") else 0,
            "source": request.form.get("source", "手动添加"),
        }

        if not data["name"] or not data["requirement_id"]:
            # 加uid过滤，普通用户只能选自己的需求
            uid_sql, uid_params = _uid_clause()
            cursor.execute(f"SELECT id, product_name FROM requirements WHERE 1=1 {uid_sql} ORDER BY created_at DESC",
                           uid_params)
            requirements = cursor.fetchall()
            return render_template("supplier/form.html", requirements=requirements,
                                   error="供应商名称和关联需求为必填项", data=data)

        # 小白讲解：如果用户填了成立日期（如2015-03-12），系统自动算出成立年限一起存入数据库
        establish_years = ""
        if data["establish_date"]:
            import re as _re
            year_match = _re.search(r'(\d{4})', data["establish_date"])
            if year_match:
                from datetime import datetime as _dt
                years = _dt.now().year - int(year_match.group(1))
                if years >= 0:
                    establish_years = str(years)

        # 小白讲解：手动添加供应商时，user_id 跟着"所选需求的所有者"走。
        # 管理员在用户的需求下手动加供应商，供应商归属到用户名下，用户能看到。
        cursor.execute("SELECT user_id FROM requirements WHERE id = %s", (data["requirement_id"],))
        _req_row = cursor.fetchone()
        _owner_id = _req_row["user_id"] if _req_row and _req_row.get("user_id") else g.user_id

        # 插入供应商（user_id 用于数据隔离，记录归属用户）
        cursor.execute("""
            INSERT INTO suppliers
            (requirement_id, name, intro, factory_address, email, phone,
             main_product, establish_years, establish_date, operating_status,
             has_cross_border_exp, source, dev_stage,
             created_at, updated_at, user_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '已寻源待初筛', %s, %s, %s)
        """, (data["requirement_id"], data["name"], data["intro"],
              data["factory_address"], data["email"], data["phone"],
              data["main_product"], establish_years, data["establish_date"],
              data["operating_status"], data["has_cross_border_exp"],
              data["source"], now_str(), now_str(), _owner_id))
        # 手动新增了供应商（默认"已寻源待初筛"），需求状态应推进到"寻源中"
        recalc_requirement_status(cursor, data["requirement_id"])
        g.db.commit()

        return redirect(url_for("supplier_detail", id=cursor.lastrowid))

    # GET请求：显示空表单（加uid过滤，普通用户只能选自己的需求）
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"SELECT id, product_name FROM requirements WHERE 1=1 {uid_sql} ORDER BY created_at DESC",
                   uid_params)
    requirements = cursor.fetchall()
    return render_template("supplier/form.html", requirements=requirements, data=None)


@app.route("/suppliers/<int:id>/edit", methods=["GET", "POST"])
def supplier_edit(id):
    """
    编辑供应商信息 - GET显示表单，POST处理提交

    可修改：基本信息、联系方式、开发阶段、资质信息等
    """
    cursor = g.db.cursor()

    if request.method == "POST":
        data = {
            "name": request.form.get("name", "").strip(),
            "intro": request.form.get("intro", "").strip(),
            "factory_address": request.form.get("factory_address", "").strip(),
            "email": request.form.get("email", "").strip(),
            "phone": request.form.get("phone", "").strip(),
            "main_product": request.form.get("main_product", "").strip(),
            "establish_date": request.form.get("establish_date", "").strip(),
            "operating_status": request.form.get("operating_status", "存续"),
            "has_cross_border_exp": 1 if request.form.get("has_cross_border_exp") else 0,
            "dev_stage": request.form.get("dev_stage", "已寻源待初筛"),
        }

        if not data["name"]:
            # 加uid过滤，普通用户只能编辑自己的供应商
            uid_sql, uid_params = _uid_clause()
            cursor.execute(f"SELECT * FROM suppliers WHERE id = %s {uid_sql}", (id, *uid_params))
            supplier = cursor.fetchone()
            return render_template("supplier/form.html", requirements=[],
                                   error="供应商名称为必填项", data=supplier, edit=True)

        # 小白讲解：如果用户填了成立日期，系统自动算出成立年限一起更新
        establish_years = ""
        if data["establish_date"]:
            import re as _re
            year_match = _re.search(r'(\d{4})', data["establish_date"])
            if year_match:
                from datetime import datetime as _dt
                years = _dt.now().year - int(year_match.group(1))
                if years >= 0:
                    establish_years = str(years)

        # 数据隔离：UPDATE供应商时加uid过滤，只能改自己的
        uid_sql, uid_params = _uid_clause()
        cursor.execute(f"""
            UPDATE suppliers SET
                name=%s, intro=%s, factory_address=%s, email=%s, phone=%s,
                main_product=%s, establish_years=%s, establish_date=%s, operating_status=%s,
                has_cross_border_exp=%s, dev_stage=%s, updated_at=%s
            WHERE id=%s {uid_sql}
        """, (data["name"], data["intro"], data["factory_address"], data["email"],
              data["phone"], data["main_product"], establish_years, data["establish_date"],
              data["operating_status"], data["has_cross_border_exp"], data["dev_stage"],
              now_str(), id, *uid_params))
        # 供应商开发阶段可能被手动改了，重新推断所属需求的状态
        cursor.execute("SELECT requirement_id FROM suppliers WHERE id=%s", (id,))
        _row = cursor.fetchone()
        if _row:
            recalc_requirement_status(cursor, _row["requirement_id"])
        g.db.commit()

        flash("供应商信息已更新", "success")
        return redirect(url_for("supplier_detail", id=id))

    # GET请求：显示当前数据（加uid过滤）
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"SELECT * FROM suppliers WHERE id = %s {uid_sql}", (id, *uid_params))
    supplier = cursor.fetchone()
    if not supplier:
        return "供应商不存在", 404

    return render_template("supplier/form.html", requirements=[], data=supplier, edit=True,
                           stages=SUPPLIER_STAGES)


@app.route("/suppliers/<int:id>/delete", methods=["POST"])
def supplier_delete(id):
    """
    删除供应商 - 同时级联删除关联的初筛记录和沟通记录

    删除顺序：
    1. 删除该供应商的初筛记录(screenings)
    2. 删除该供应商的沟通记录(communications)
    3. 删除供应商本身(suppliers)
    """
    cursor = g.db.cursor()

    # 确认供应商存在并取其所属需求ID（加uid过滤，普通用户只能删自己的）
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"SELECT id, requirement_id FROM suppliers WHERE id = %s {uid_sql}", (id, *uid_params))
    sup_row = cursor.fetchone()
    if not sup_row:
        flash("供应商不存在，无法删除", "danger")
        return redirect(url_for("supplier_list"))

    # 小白讲解：screenings/communications表没有user_id列，不加uid过滤。
    # supplier_id已唯一关联当前用户的供应商，不会误删。
    cursor.execute("DELETE FROM screening_audit_logs WHERE supplier_id = %s", (id,))
    cursor.execute("DELETE FROM screenings WHERE supplier_id = %s", (id,))
    cursor.execute("DELETE FROM communications WHERE supplier_id = %s", (id,))
    # 删除供应商（加uid过滤，suppliers表有user_id列）
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"DELETE FROM suppliers WHERE id = %s {uid_sql}", (id, *uid_params))
    # 供应商删了，所属需求的状态可能需要回退（如最后一个供应商被删→需求回到"需求确认中"）
    recalc_requirement_status(cursor, sup_row["requirement_id"])
    g.db.commit()

    flash("供应商及其关联数据已删除", "success")
    return redirect(url_for("supplier_list"))


@app.route("/suppliers/batch-delete", methods=["POST"])
def supplier_batch_delete():
    """
    批量删除供应商 - 同时级联删除关联的初筛记录和沟通记录

    前端通过checkbox勾选多个供应商，提交ID列表到这里统一删除。
    """
    # 从表单获取勾选的供应商ID列表
    ids = request.form.getlist("supplier_ids")
    if not ids:
        flash("未选择任何供应商", "warning")
        return redirect(url_for("supplier_list"))

    # 转成整数列表
    supplier_ids = []
    for sid in ids:
        try:
            supplier_ids.append(int(sid))
        except (ValueError, TypeError):
            continue

    if not supplier_ids:
        flash("未选择有效的供应商", "warning")
        return redirect(url_for("supplier_list"))

    cursor = g.db.cursor()
    # 先查出这些供应商所属的需求ID（去重），删除后要重新推断这些需求的状态
    placeholders = ",".join("%s" for _ in supplier_ids)
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"SELECT DISTINCT requirement_id FROM suppliers WHERE id IN ({placeholders}) {uid_sql}",
                   (*supplier_ids, *uid_params))
    affected_req_ids = [row["requirement_id"] for row in cursor.fetchall()]

    # 小白讲解：screenings/communications/audit_logs表没有user_id列，不加uid过滤。
    # supplier_id已唯一关联当前用户的供应商，不会误删。必须先删子表再删主表（外键约束）。
    cursor.execute(f"DELETE FROM screening_audit_logs WHERE supplier_id IN ({placeholders})",
                   tuple(supplier_ids))
    cursor.execute(f"DELETE FROM screenings WHERE supplier_id IN ({placeholders})",
                   tuple(supplier_ids))
    cursor.execute(f"DELETE FROM communications WHERE supplier_id IN ({placeholders})",
                   tuple(supplier_ids))
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"DELETE FROM suppliers WHERE id IN ({placeholders}) {uid_sql}",
                   (*supplier_ids, *uid_params))
    # 批量删除后，重新推断每个受影响需求的状态
    for rid in affected_req_ids:
        recalc_requirement_status(cursor, rid)
    g.db.commit()

    flash(f"已批量删除 {len(supplier_ids)} 家供应商", "success")
    return redirect(url_for("supplier_list"))


# ==================== 初筛管理 ====================
@app.route("/suppliers/<int:supplier_id>/screening", methods=["GET", "POST"])
def screening_create(supplier_id):
    """创建初筛 - GET显示表单，POST处理提交并自动算分"""
    cursor = g.db.cursor()

    # 获取供应商信息（加uid过滤，alias用"s."因为有表别名）
    uid_sql, uid_params = _uid_clause("s.")
    cursor.execute(f"""
        SELECT s.*, r.product_name, r.required_certs
        FROM suppliers s
        JOIN requirements r ON s.requirement_id = r.id
        WHERE s.id = %s {uid_sql}
    """, (supplier_id, *uid_params))
    supplier = cursor.fetchone()
    if not supplier:
        return "供应商不存在", 404

    if request.method == "POST":
        # 收集表单数据
        form_data = {
            "trademark_result": request.form.get("trademark_result", ""),
            "patent_result": request.form.get("patent_result", ""),
            "lawsuit_result": request.form.get("lawsuit_result", ""),
            "platform_infringe": request.form.get("platform_infringe", ""),
            "own_ip": request.form.get("own_ip", ""),
            "risk_summary": request.form.get("risk_summary", ""),
            "cert_authenticity": request.form.get("cert_authenticity", ""),
            "test_report": request.form.get("test_report", ""),
            "customs_qualification": request.form.get("customs_qualification", ""),
            "export_record": request.form.get("export_record", ""),
            "label_compliance": request.form.get("label_compliance", ""),
            "qual_summary": request.form.get("qual_summary", ""),
            "establish_years": request.form.get("establish_years", ""),
        }

        # 调用评分模块计算得分
        from scoring import calculate_score
        scores = calculate_score(form_data, supplier)

        # 存入数据库（user_id用于数据隔离，记录是哪个用户创建的初筛记录）
        cursor.execute("""
            INSERT INTO screenings
            (supplier_id, trademark_result, patent_result, lawsuit_result,
             platform_infringe, own_ip, risk_summary,
             cert_authenticity, test_report, customs_qualification,
             export_record, label_compliance, qual_summary,
             ip_score, qual_score, basic_score, total_score,
             veto_triggered, passed, veto_reason,
             created_at, updated_at, user_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (supplier_id, form_data["trademark_result"], form_data["patent_result"],
              form_data["lawsuit_result"], form_data["platform_infringe"],
              form_data["own_ip"], form_data["risk_summary"],
              form_data["cert_authenticity"], form_data["test_report"],
              form_data["customs_qualification"], form_data["export_record"],
              form_data["label_compliance"], form_data["qual_summary"],
              scores["ip_score"], scores["qual_score"], scores["basic_score"],
              scores["total_score"], scores["veto_triggered"], scores["passed"],
              scores["veto_reason"], now_str(), now_str(), g.user_id))

        # 更新供应商的开发阶段（加uid过滤，只能改自己的供应商）
        new_stage = "已通过初筛" if scores["passed"] else "未通过初筛"
        uid_sql, uid_params = _uid_clause()
        cursor.execute(f"UPDATE suppliers SET dev_stage=%s, updated_at=%s WHERE id=%s {uid_sql}",
                       (new_stage, now_str(), supplier_id, *uid_params))
        # 供应商阶段变了，重新推断所属需求的状态（初筛完→需求可能进入"初筛中"）
        recalc_requirement_status(cursor, supplier["requirement_id"])
        g.db.commit()

        return redirect(url_for("supplier_detail", id=supplier_id))

    # GET请求：显示初筛表单
    return render_template("screening/form.html", supplier=supplier, screening=None)


@app.route("/suppliers/<int:supplier_id>/screening/edit", methods=["GET", "POST"])
def screening_edit(supplier_id):
    """编辑初筛 - GET显示表单，POST处理提交"""
    cursor = g.db.cursor()

    # 获取供应商信息（加uid过滤，alias用"s."因为有表别名）
    uid_sql, uid_params = _uid_clause("s.")
    cursor.execute(f"""
        SELECT s.*, r.product_name, r.required_certs
        FROM suppliers s
        JOIN requirements r ON s.requirement_id = r.id
        WHERE s.id = %s {uid_sql}
    """, (supplier_id, *uid_params))
    supplier = cursor.fetchone()
    if not supplier:
        return "供应商不存在", 404

    # 查询初筛记录（加uid过滤）
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"SELECT * FROM screenings WHERE supplier_id = %s {uid_sql}", (supplier_id, *uid_params))
    screening = cursor.fetchone()

    if request.method == "POST":
        form_data = {
            "trademark_result": request.form.get("trademark_result", ""),
            "patent_result": request.form.get("patent_result", ""),
            "lawsuit_result": request.form.get("lawsuit_result", ""),
            "platform_infringe": request.form.get("platform_infringe", ""),
            "own_ip": request.form.get("own_ip", ""),
            "risk_summary": request.form.get("risk_summary", ""),
            "cert_authenticity": request.form.get("cert_authenticity", ""),
            "test_report": request.form.get("test_report", ""),
            "customs_qualification": request.form.get("customs_qualification", ""),
            "export_record": request.form.get("export_record", ""),
            "label_compliance": request.form.get("label_compliance", ""),
            "qual_summary": request.form.get("qual_summary", ""),
            "establish_years": request.form.get("establish_years", ""),
        }

        from scoring import calculate_score
        scores = calculate_score(form_data, supplier)

        if screening:
            # 更新已有记录（加uid过滤，只能改自己的初筛记录）
            uid_sql, uid_params = _uid_clause()
            cursor.execute(f"""
                UPDATE screenings SET
                    trademark_result=%s, patent_result=%s, lawsuit_result=%s,
                    platform_infringe=%s, own_ip=%s, risk_summary=%s,
                    cert_authenticity=%s, test_report=%s, customs_qualification=%s,
                    export_record=%s, label_compliance=%s, qual_summary=%s,
                    ip_score=%s, qual_score=%s, basic_score=%s, total_score=%s,
                    veto_triggered=%s, passed=%s, veto_reason=%s, updated_at=%s
                WHERE supplier_id=%s {uid_sql}
            """, (form_data["trademark_result"], form_data["patent_result"],
                  form_data["lawsuit_result"], form_data["platform_infringe"],
                  form_data["own_ip"], form_data["risk_summary"],
                  form_data["cert_authenticity"], form_data["test_report"],
                  form_data["customs_qualification"], form_data["export_record"],
                  form_data["label_compliance"], form_data["qual_summary"],
                  scores["ip_score"], scores["qual_score"], scores["basic_score"],
                  scores["total_score"], scores["veto_triggered"], scores["passed"],
                  scores["veto_reason"], now_str(), supplier_id, *uid_params))
        else:
            # 不存在则新建（user_id用于数据隔离）
            cursor.execute("""
                INSERT INTO screenings
                (supplier_id, trademark_result, patent_result, lawsuit_result,
                 platform_infringe, own_ip, risk_summary,
                 cert_authenticity, test_report, customs_qualification,
                 export_record, label_compliance, qual_summary,
                 ip_score, qual_score, basic_score, total_score,
                 veto_triggered, passed, veto_reason, created_at, updated_at, user_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (supplier_id, form_data["trademark_result"], form_data["patent_result"],
                  form_data["lawsuit_result"], form_data["platform_infringe"],
                  form_data["own_ip"], form_data["risk_summary"],
                  form_data["cert_authenticity"], form_data["test_report"],
                  form_data["customs_qualification"], form_data["export_record"],
                  form_data["label_compliance"], form_data["qual_summary"],
                  scores["ip_score"], scores["qual_score"], scores["basic_score"],
                  scores["total_score"], scores["veto_triggered"], scores["passed"],
                  scores["veto_reason"], now_str(), now_str(), g.user_id))

        new_stage = "已通过初筛" if scores["passed"] else "未通过初筛"
        # 更新供应商开发阶段（加uid过滤）
        uid_sql, uid_params = _uid_clause()
        cursor.execute(f"UPDATE suppliers SET dev_stage=%s, updated_at=%s WHERE id=%s {uid_sql}",
                       (new_stage, now_str(), supplier_id, *uid_params))
        # 供应商阶段变了，重新推断所属需求的状态
        recalc_requirement_status(cursor, supplier["requirement_id"])
        g.db.commit()

        return redirect(url_for("supplier_detail", id=supplier_id))

    return render_template("screening/form.html", supplier=supplier, screening=screening)


# ==================== 沟通记录管理 ====================
@app.route("/suppliers/<int:supplier_id>/communication", methods=["GET", "POST"])
def communication_create(supplier_id):
    """添加沟通记录 - GET显示表单，POST处理提交"""
    cursor = g.db.cursor()

    # 获取供应商信息（加uid过滤，普通用户只能给自己的供应商添加沟通记录）
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"SELECT * FROM suppliers WHERE id = %s {uid_sql}", (supplier_id, *uid_params))
    supplier = cursor.fetchone()
    if not supplier:
        return "供应商不存在", 404

    if request.method == "POST":
        # 小白讲解：communications表的列名是channel/content/conclusion/next_step/comm_time，
        # 不是comm_type/direction/subject/sent_at（旧代码用错了列名导致500错误）
        channel = request.form.get("channel", "微信/企微")
        content = request.form.get("content", "").strip()
        conclusion = request.form.get("conclusion", "").strip()
        next_step = request.form.get("next_step", "").strip()
        comm_time = request.form.get("comm_time", now_str())

        # 插入沟通记录（user_id用于数据隔离，记录是哪个用户创建的）
        cursor.execute("""
            INSERT INTO communications
            (supplier_id, channel, content, conclusion, next_step, comm_time, created_at, updated_at, user_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (supplier_id, channel, content, conclusion, next_step, comm_time, now_str(), now_str(), g.user_id))
        g.db.commit()

        return redirect(url_for("supplier_detail", id=supplier_id))

    return render_template("communication/form.html", supplier=supplier)


# ==================== AI 自动化功能 ====================
@app.route("/ai")
def ai_index():
    """AI自动化首页 - 选择AI功能的入口"""
    cursor = g.db.cursor()
    # 获取所有需求和对应的供应商数量（加uid过滤，alias用"r."因为有表别名）
    uid_sql, uid_params = _uid_clause("r.")
    cursor.execute(f"""
        SELECT r.*,
               (SELECT COUNT(*) FROM suppliers WHERE requirement_id = r.id) as supplier_count
        FROM requirements r
        WHERE 1=1 {uid_sql}
        ORDER BY r.created_at DESC
    """, uid_params)
    requirements = cursor.fetchall()

    # 转成 {需求ID: 供应商数量} 的字典
    suppliers_count = {req["id"]: req["supplier_count"] for req in requirements}

    return render_template("ai/index.html",
                           api_configured=is_api_configured(),
                           requirements=requirements,
                           suppliers_count=suppliers_count)


@app.route("/ai/parse-requirement", methods=["GET", "POST"])
def ai_parse_requirement():
    """
    AI解析需求（第一轮）- 用户输入文本或上传文件，AI提取并判断确认状态

    按需求确认SKILL逻辑：
    - 信息完整(confirmed=True) → 显示确认页可保存
    - 信息缺失(confirmed=False) → 显示追问页让用户补充，不保存
    """
    if not is_api_configured():
        flash("请先在 config.py 中配置 DeepSeek API Key", "danger")
        return redirect(url_for("ai_index"))

    if request.method == "POST":
        input_text = request.form.get("input_text", "").strip()

        # 处理上传的文件（单个输入框，支持多种格式）
        uploaded_file = request.files.get("file")
        image_base64 = None
        file_content = None

        if uploaded_file and uploaded_file.filename:
            file_bytes = uploaded_file.read()
            from file_parser import parse_uploaded_file, TYPE_IMAGE
            try:
                result = parse_uploaded_file(file_bytes, uploaded_file.filename)
                if result["type"] == TYPE_IMAGE:
                    if not is_vision_configured():
                        flash("上传图片需要配置智谱API Key（用于图片识别），请查看config.py", "warning")
                        return render_template("ai/parse_requirement.html",
                                               vision_configured=is_vision_configured())
                    image_base64 = result["content"]
                else:
                    file_content = result["content"]
            except Exception as e:
                flash(f"文件解析失败：{str(e)}", "danger")
                return render_template("ai/parse_requirement.html",
                                       vision_configured=is_vision_configured())

        if not input_text and not file_content and not image_base64:
            flash("请输入需求描述或上传文件", "warning")
            return render_template("ai/parse_requirement.html",
                                   vision_configured=is_vision_configured())

        try:
            from ai_helper import parse_requirement
            parsed = parse_requirement(
                input_text if input_text else None,
                file_content,
                image_base64
            )

            # 把原始输入也传给确认页（追问时需要带上）
            return render_template("ai/requirement_confirm.html",
                                   parsed=parsed,
                                   original_text=input_text,
                                   vision_configured=is_vision_configured())
        except Exception as e:
            flash(f"AI解析失败：{str(e)}", "danger")
            return render_template("ai/parse_requirement.html",
                                   vision_configured=is_vision_configured())

    # GET请求：显示输入页面
    return render_template("ai/parse_requirement.html",
                           vision_configured=is_vision_configured())


@app.route("/ai/parse-requirement-stream", methods=["POST"])
def ai_parse_requirement_stream():
    """
    AI解析需求-流式版：通过SSE实时推送解析进度给前端

    小白讲解：原版是一次性等所有步骤跑完才返回结果，用户只能干等。
    这个版本用SSE（Server-Sent Events）把每一步的进度实时推送给前端，
    用户能看到"正在识别图片→正在抓取网页→AI正在分析"等细节，心里有底。

    前端用JS fetch接收流式数据，最后一步收到done事件后自动跳转确认页。
    """
    if not is_api_configured():
        def _err():
            yield f"data: {json.dumps({'step': 'error', 'message': 'DeepSeek API Key 未配置', 'status': 'error'}, ensure_ascii=False)}\n\n"
        return Response(stream_with_context(_err()), mimetype="text/event-stream")

    input_text = request.form.get("input_text", "").strip()

    # 处理上传文件（和原版逻辑一致）
    uploaded_file = request.files.get("file")
    image_base64 = None
    file_content = None
    if uploaded_file and uploaded_file.filename:
        file_bytes = uploaded_file.read()
        from file_parser import parse_uploaded_file, TYPE_IMAGE
        try:
            result = parse_uploaded_file(file_bytes, uploaded_file.filename)
            if result["type"] == TYPE_IMAGE:
                if not is_vision_configured():
                    def _err():
                        yield f"data: {json.dumps({'step': 'error', 'message': '智谱API Key未配置，无法识别图片', 'status': 'error'}, ensure_ascii=False)}\n\n"
                    return Response(stream_with_context(_err()), mimetype="text/event-stream")
                image_base64 = result["content"]
            else:
                file_content = result["content"]
        except Exception as e:
            def _err():
                msg = f"文件解析失败：{str(e)}"
                yield f"data: {json.dumps({'step': 'error', 'message': msg, 'status': 'error'}, ensure_ascii=False)}\n\n"
            return Response(stream_with_context(_err()), mimetype="text/event-stream")

    if not input_text and not file_content and not image_base64:
        def _err():
            yield f"data: {json.dumps({'step': 'error', 'message': '请输入需求描述或上传文件', 'status': 'error'}, ensure_ascii=False)}\n\n"
        return Response(stream_with_context(_err()), mimetype="text/event-stream")

    def generate():
        from ai_helper import parse_requirement
        import uuid

        # 生成唯一ID，用于跳转后取结果
        result_id = uuid.uuid4().hex

        # 进度推送队列（线程安全）
        progress_queue = queue.Queue()

        def progress_callback(step, message, status="running"):
            progress_queue.put({"step": step, "message": message, "status": status})

        # 在后台线程中执行解析（避免阻塞主线程）
        parse_thread = threading.Thread(
            target=_run_parse_requirement,
            args=(input_text, file_content, image_base64, progress_callback, progress_queue, result_id),
            daemon=True
        )
        parse_thread.start()

        # 主循环：不断从队列取进度消息，推送给前端
        while True:
            try:
                msg = progress_queue.get(timeout=0.5)
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                if msg["status"] in ("done", "error"):
                    break
            except queue.Empty:
                # 超时检查线程是否还活着
                if not parse_thread.is_alive():
                    yield f"data: {json.dumps({'step': 'done', 'message': '✅ 解析完成', 'status': 'done'}, ensure_ascii=False)}\n\n"
                    break

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 禁用nginx缓冲
        }
    )


def _run_parse_requirement(input_text, file_content, image_base64, progress_callback, progress_queue, result_id):
    """
    在后台线程中执行AI解析，结果存入内存字典

    小白讲解：因为SSE响应期间session cookie无法写回浏览器，
    所以用内存字典 _tmp_results 暂存结果，前端跳转时通过result_id来取。
    """
    try:
        from ai_helper import parse_requirement
        parsed = parse_requirement(
            input_text if input_text else None,
            file_content,
            image_base64,
            progress_callback=progress_callback
        )
        # 结果存入内存字典
        _tmp_results[result_id] = {
            "parsed": parsed,
            "original_text": input_text,
        }
        progress_queue.put({
            "step": "done",
            "message": "✅ 解析完成，正在跳转...",
            "status": "done",
            "redirect": f"/ai/parse-requirement-result?rid={result_id}"
        })
    except Exception as e:
        progress_queue.put({
            "step": "error",
            "message": f"❌ 解析失败：{str(e)[:200]}",
            "status": "error"
        })


@app.route("/ai/parse-requirement-result")
def ai_parse_requirement_result():
    """
    流式解析完成后跳转到此路由，通过result_id从内存字典取结果渲染确认页
    """
    result_id = request.args.get("rid", "")
    data = _tmp_results.pop(result_id, None)
    if not data:
        flash("解析结果已过期，请重新提交需求", "warning")
        return redirect(url_for("ai_parse_requirement"))
    return render_template("ai/requirement_confirm.html",
                           parsed=data["parsed"],
                           original_text=data["original_text"],
                           vision_configured=is_vision_configured())


@app.route("/ai/supplement-requirement", methods=["POST"])
def ai_supplement_requirement():
    """
    AI补充需求（追问轮次）- 用户补充缺失项后，带之前的识别结果再次调用AI判断

    把用户补充的信息 + 之前AI已识别的信息合并，重新调用 parse_requirement
    previous_data 作为上下文让AI知道之前已经识别和确认了什么
    """
    if not is_api_configured():
        flash("请先在 config.py 中配置 DeepSeek API Key", "danger")
        return redirect(url_for("ai_index"))

    # 收集用户补充的信息 + 之前已识别的信息（作为previous_data传给AI）
    previous_data = {
        "product_name": request.form.get("prev_product_name", "").strip(),
        "core_functions": request.form.get("core_functions", "").strip(),
        "material": request.form.get("material", "").strip(),
        "spec_size": request.form.get("spec_size", "").strip(),
        "first_purchase_qty": request.form.get("first_purchase_qty", "").strip(),
        "acceptable_moq": request.form.get("acceptable_moq", "").strip(),
        "min_ship_qty": request.form.get("min_ship_qty", "").strip(),
        "target_market": request.form.get("target_market", "").strip(),
        "required_certs": request.form.get("required_certs", "").strip(),
        "acceptable_lead_time": request.form.get("acceptable_lead_time", "").strip(),
        "product_aliases": request.form.get("product_aliases", "").strip(),
        "other_requirements": request.form.get("other_requirements", "").strip(),
    }

    # 原始输入文本（第一轮的文字描述，保留下来继续给AI）
    original_text = request.form.get("original_text", "").strip()

    try:
        from ai_helper import parse_requirement
        # 带previous_data重新解析（AI会结合之前的信息判断是否确认完成）
        parsed = parse_requirement(
            original_text if original_text else None,
            None,
            None,
            previous_data
        )

        return render_template("ai/requirement_confirm.html",
                               parsed=parsed,
                               original_text=original_text,
                               vision_configured=is_vision_configured())
    except Exception as e:
        flash(f"AI解析失败：{str(e)}", "danger")
        return redirect(url_for("ai_parse_requirement"))


@app.route("/ai/save-requirement", methods=["POST"])
def ai_save_requirement():
    """
    保存AI确认完成的需求到数据库

    按需求确认SKILL逻辑：
    - 查重：按产品名称查，已存在则更新，不存在则新增
    - 状态写为"寻源中"（需求已确认，准备开始寻源）
    - keywords存为P0-P3的JSON字符串
    """
    import json

    data = {
        "product_name": request.form.get("product_name", "").strip(),
        "product_aliases": request.form.get("product_aliases", "").strip(),
        "core_functions": request.form.get("core_functions", "").strip(),
        "material": request.form.get("material", "").strip(),
        "spec_size": request.form.get("spec_size", "").strip(),
        "first_purchase_qty": request.form.get("first_purchase_qty", "").strip(),
        "acceptable_moq": request.form.get("acceptable_moq", "").strip(),
        "min_ship_qty": request.form.get("min_ship_qty", "").strip(),
        "acceptable_lead_time": request.form.get("acceptable_lead_time", "").strip(),
        "target_market": request.form.get("target_market", "").strip(),
        "required_certs": request.form.get("required_certs", "").strip(),
        "requirement_summary": request.form.get("requirement_summary", "").strip(),
        "customization_req": request.form.get("other_requirements", "").strip(),
    }

    # keywords 是JSON字符串（从前端隐藏域传过来）
    # 小白讲解：前端tojson可能把中文转成了\uXXXX编码，这里做兜底转换还原成中文
    # 先json.loads把字符串解析成字典，再用ensure_ascii=False重新序列化，确保存库的是中文
    keywords_raw = request.form.get("keywords", "").strip()
    try:
        keywords_obj = json.loads(keywords_raw)
        keywords_json = json.dumps(keywords_obj, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        # 解析失败说明不是JSON格式（如手动输入的逗号分隔关键词），保持原样
        keywords_json = keywords_raw

    if not data["product_name"]:
        flash("产品名称不能为空", "danger")
        return redirect(url_for("ai_parse_requirement"))

    cursor = g.db.cursor()

    # 查重：按产品名称查是否已存在（加uid过滤，普通用户只在自己的需求里查重）
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"SELECT id FROM requirements WHERE product_name = %s {uid_sql}",
                   (data["product_name"], *uid_params))
    existing = cursor.fetchone()

    if existing:
        # 已存在：更新记录（加uid过滤，只能改自己的需求）
        req_id = existing["id"]
        uid_sql, uid_params = _uid_clause()
        cursor.execute(f"""
            UPDATE requirements SET
                product_aliases=%s, core_functions=%s, material=%s, spec_size=%s,
                first_purchase_qty=%s, acceptable_moq=%s, min_ship_qty=%s,
                acceptable_lead_time=%s, target_market=%s, required_certs=%s,
                requirement_summary=%s, keywords=%s, customization_req=%s, status='寻源中', updated_at=%s
            WHERE id=%s {uid_sql}
        """, (data["product_aliases"], data["core_functions"], data["material"],
              data["spec_size"], data["first_purchase_qty"], data["acceptable_moq"],
              data["min_ship_qty"], data["acceptable_lead_time"], data["target_market"],
              data["required_certs"], data["requirement_summary"], keywords_json,
              data["customization_req"], now_str(), req_id, *uid_params))
        g.db.commit()
        flash("需求已更新（产品名称已存在，已覆盖更新）！", "success")
        return redirect(url_for("requirement_detail", id=req_id))
    else:
        # 不存在：新增记录（user_id用于数据隔离，记录是哪个用户创建的）
        cursor.execute("""
            INSERT INTO requirements
            (product_name, product_aliases, core_functions, material, spec_size,
             first_purchase_qty, acceptable_moq, min_ship_qty, acceptable_lead_time,
             target_market, required_certs, requirement_summary, keywords, customization_req,
             status, created_at, updated_at, user_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '寻源中', %s, %s, %s)
        """, (data["product_name"], data["product_aliases"], data["core_functions"],
              data["material"], data["spec_size"], data["first_purchase_qty"],
              data["acceptable_moq"], data["min_ship_qty"], data["acceptable_lead_time"],
              data["target_market"], data["required_certs"], data["requirement_summary"],
              keywords_json, data["customization_req"], now_str(), now_str(), g.user_id))
        g.db.commit()
        flash("需求已确认并保存！可以开始AI搜索供应商了", "success")
        return redirect(url_for("requirement_detail", id=cursor.lastrowid))


@app.route("/ai/search-suppliers/<int:req_id>", methods=["GET", "POST"])
def ai_search_suppliers(req_id):
    """
    AI搜索供应商 - 根据需求P0-P3关键词自动搜索供应商

    流程（参考供应商寻源SKILL文档）：
    1. 用智谱web_search按P0-P3关键词搜索（7组中英文关键词，共14次搜索）
    2. 用DeepSeek从搜索结果中提取+过滤供应商（剔除配件/材料/贸易商）
    3. 用天眼查MCP补全工商信息（注册资本、地址、电话等）
    4. 批量写入数据库

    GET: 显示搜索配置页面
    POST: 执行搜索，用SSE流式推送进度给前端
    """
    cursor = g.db.cursor()
    # 加uid过滤，普通用户只能搜索自己需求的供应商
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"SELECT * FROM requirements WHERE id = %s {uid_sql}", (req_id, *uid_params))
    requirement = cursor.fetchone()
    if not requirement:
        return "需求不存在", 404

    # 小白讲解：数据归属跟着"需求所有者"走，而不是跟着"操作人"走。
    # 这样管理员帮用户在用户的需求上搜索供应商时，搜出来的供应商 user_id 是用户自己的，
    # 用户能完整看到（不会被"只看自己数据"的过滤挡掉）。
    # 管理员本来就能看所有数据，所以不受影响。
    user_id = requirement["user_id"] if requirement.get("user_id") else g.user_id

    # GET请求：显示搜索配置页面
    if request.method == "GET":
        if not is_api_configured():
            flash("请先在 config.py 中配置 DeepSeek API Key", "danger")
            return redirect(url_for("ai_index"))
        return render_template("ai/search_suppliers.html", requirement=requirement)

    # POST请求：提交搜索任务（返回task_id，由前端轮询进度）
    # 小白讲解：改为"消息列表"模式——所有进度消息都保留在messages列表里，
    # 用户刷新页面后可以通过status接口拿到全部历史消息，恢复进度界面。
    import uuid
    task_id = str(uuid.uuid4())

    # 创建任务记录，放入全局task_store（messages列表保留全部进度，req_id用于刷新恢复）
    task_store[task_id] = {
        "messages": [],          # 所有进度消息列表（不再消费式读取，全部保留）
        "status": "running",     # 任务状态：running/done/error
        "result": None,          # 最终结果（done时存搜索结果，error时存错误信息）
        "req_id": req_id,        # 关联的需求ID（用户刷新页面后通过它找到正在运行的任务）
    }

    def progress_callback(step, total, desc):
        """进度回调：把进度消息追加到messages列表（加锁保护，避免并发写入冲突）"""
        msg = {"type": "progress", "step": step, "total": total, "desc": desc}
        with _task_store_lock:
            task_store[task_id]["messages"].append(msg)

    def run_search_thread():
        conn = db.get_db()
        cur = conn.cursor()
        try:
            from supplier_search import search_suppliers
            keywords = requirement["keywords"] or requirement["product_name"]
            suppliers = search_suppliers(keywords, requirement["product_name"], progress_callback)

            saved_count = 0
            for s in suppliers:
                if not s.get("name"):
                    continue
                cur.execute("""
                    INSERT INTO suppliers
                    (requirement_id, name, intro, factory_address, email, phone,
                     main_product, source, dev_stage,
                     hit_keyword,
                     supplier_type, contact_status, registered_capital, legal_person,
                     operating_status, establish_years, establish_date, has_cross_border_exp,
                     created_at, updated_at, user_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '已寻源待初筛',
                            %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (req_id, s.get("name", ""), s.get("intro", ""),
                      s.get("factory_address", ""), s.get("email", ""),
                      s.get("phone", ""), s.get("main_product", ""),
                      s.get("source", "AI搜索"),
                      s.get("hit_keyword", ""),
                      s.get("supplier_type", ""), s.get("contact_status", "未获取"),
                      s.get("registered_capital", ""), s.get("legal_person", ""),
                      s.get("operating_status", "存续"),
                      s.get("establish_years", ""), s.get("establish_date", ""),
                      s.get("has_cross_border_exp", 0),
                      now_str(), now_str(), user_id))
                saved_count += 1
            # 新增了供应商（都是"已寻源待初筛"），需求状态应从"需求确认中"推进到"寻源中"
            recalc_requirement_status(cur, req_id)
            conn.commit()

            result = {
                "type": "done",
                "saved_count": saved_count,
                "total_found": len(suppliers),
                "req_id": req_id,
            }
            with _task_store_lock:
                task_store[task_id]["messages"].append(result)
                task_store[task_id]["status"] = "done"
                task_store[task_id]["result"] = result
        except Exception as e:
            error_msg = {"type": "error", "message": f"AI搜索失败：{str(e)}"}
            with _task_store_lock:
                task_store[task_id]["messages"].append(error_msg)
                task_store[task_id]["status"] = "error"
                task_store[task_id]["result"] = error_msg
        finally:
            conn.close()

    # 先放启动消息
    with _task_store_lock:
        task_store[task_id]["messages"].append(
            {"type": "progress", "step": 0, "total": 3, "desc": "正在启动搜索..."}
        )

    # 启动后台搜索线程
    thread = threading.Thread(target=run_search_thread)
    thread.start()

    # 返回task_id（瞬间完成，前端拿到后开始轮询）
    return jsonify({"task_id": task_id, "status": "started"})


@app.route("/ai/search-suppliers/<int:req_id>/poll/<task_id>", methods=["GET"])
def poll_ai_search(req_id, task_id):
    """
    轮询搜索任务进度（每3秒调用一次）

    小白讲解：游标模式——前端传 cursor 参数表示"上次读到第N条消息"，
    后端返回第N条之后的所有新消息 + 新的cursor位置。
    这样即使前端断线重连，也不会丢失中间的进度消息。
    """
    task = task_store.get(task_id)
    if not task:
        return jsonify({"status": "not_found", "error": "任务不存在或已过期"}), 404

    # 获取前端传来的游标位置（默认0，表示从头读）
    cursor = request.args.get("cursor", 0, type=int)

    # 加锁读取增量消息（从cursor位置开始的所有新消息）
    with _task_store_lock:
        all_messages = task["messages"]
        new_messages = all_messages[cursor:]
        new_cursor = len(all_messages)
        current_status = task["status"]
        result = task.get("result")

    return jsonify({
        "status": current_status,
        "messages": new_messages,
        "cursor": new_cursor,
        "result": result
    })


@app.route("/ai/search-suppliers/<int:req_id>/status", methods=["GET"])
def ai_search_status(req_id):
    """
    查询需求是否有正在运行或已完成的搜索任务（用于页面刷新后恢复进度）

    小白讲解：用户刷新页面后，前端的task_id丢了，不知道之前搜索到哪了。
    这个接口根据需求ID(req_id)在task_store里查找对应的任务：
    - 找到running状态 → 返回task_id和全部历史消息，前端恢复进度界面继续轮询
    - 找到done/error状态 → 返回task_id和结果，前端直接显示完成/错误
    - 没找到 → 返回not_found，前端正常显示搜索表单
    """
    with _task_store_lock:
        for task_id, task in task_store.items():
            if task.get("req_id") == req_id:
                return jsonify({
                    "task_id": task_id,
                    "status": task["status"],
                    "messages": task["messages"],
                    "cursor": len(task["messages"]),
                    "result": task.get("result")
                })

    return jsonify({"status": "not_found"})


@app.route("/ai/auto-screening/<int:req_id>", methods=["GET", "POST"])
def ai_auto_screening(req_id):
    """
    AI批量自动初筛 - 规则驱动 + 天眼查MCP + AI语义辅助

    小白讲解：这是新版初筛入口，采用"规则驱动 + AI语义辅助"的标准化流程：
    - GET：显示确认页面（含规则预览）
    - POST：用SSE流式返回初筛进度，调用 screening_engine.run_screening()

    流程：
    1. 查询该需求下所有"需求获取"阶段的供应商
    2. 逐个执行：天眼查数据采集 → 一票否决规则 → 评分规则 → 写回结果
    3. 通过SSE实时推送进度给前端
    """
    if not is_api_configured():
        flash("请先在管理中心配置 DeepSeek API Key", "danger")
        return redirect(url_for("ai_index"))

    cursor = g.db.cursor()
    # 加uid过滤，普通用户只能对自己的需求做自动初筛
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"SELECT * FROM requirements WHERE id = %s {uid_sql}", (req_id, *uid_params))
    requirement = cursor.fetchone()
    if not requirement:
        return "需求不存在", 404

    # GET请求：显示确认页面（加uid过滤，只统计自己需求下待初筛的供应商数量）
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"""
        SELECT COUNT(*) as count FROM suppliers
        WHERE requirement_id = %s AND dev_stage = '已寻源待初筛' {uid_sql}
    """, (req_id, *uid_params))
    pending_count = cursor.fetchone()["count"]

    # 加载规则预览（展示本次初筛将使用的规则）
    from screening_rules import get_veto_rules, get_score_rules, list_user_templates
    veto_rules = get_veto_rules()
    score_rules = get_score_rules()
    templates = list_user_templates(g.user_id)

    # 小白讲解：初筛前兜底校验"启用的评分规则满分总和=100"。
    # 单条规则保存允许临时≠100，但初筛执行时必须=100，否则初筛总分会不对。
    # 校验只针对"用默认规则"的情况；如果用户选了模板，模板保存时已经校验过总分=100，这里跳过。
    template_name_submit = (request.form.get("template_name") or "").strip() if request.method == "POST" else ""
    if not template_name_submit:
        enabled_score_rules = [r for r in score_rules if r.get("is_enabled")]
        score_total = sum(r["max_score"] for r in enabled_score_rules if r.get("max_score"))
        if score_total != 100:
            if request.method == "GET":
                flash(f"当前启用的评分规则满分总和为{score_total}，不等于100，无法开始初筛。"
                      f"请先到规则配置页调整满分（单条保存不会被拦截），总分=100后再来初筛。", "danger")
            else:
                flash(f"开始初筛失败！启用的评分规则满分总和必须等于100（当前{score_total}）。"
                      f"请先到规则配置页调整满分。", "danger")
            return redirect(url_for("screening_rules_config", req_id=req_id))

    if request.method == "GET":
        return render_template("ai/auto_screening.html",
                               requirement=requirement, pending_count=pending_count,
                               veto_rules=veto_rules, score_rules=score_rules,
                               templates=templates)

    # POST请求：执行初筛（用SSE流式返回进度）
    if pending_count == 0:
        flash("没有需要初筛的供应商（所有供应商已完成初筛）", "info")
        return redirect(url_for("requirement_detail", id=req_id))

    # 用队列在线程间传递进度消息（初筛线程 -> 主响应generator）
    progress_queue = queue.Queue()
    # 小白讲解：把user_id存到局部变量，避免后台线程访问g对象（g是请求级的，请求结束后失效）
    current_user_id = g.user_id

    # 小白讲解：读取前端选择的规则模板名（用户在初筛页下拉框选的）。
    # 空字符串表示用全局默认规则，非空表示用该模板保存的规则参数初筛。
    template_name = (request.form.get("template_name") or "").strip() or None

    # 小白讲解：迁移到MySQL后，不再需要手动关闭g.db释放锁（MySQL支持并发读写）。
    # 只需提交当前事务即可。后台线程用独立连接写入，不会与主请求冲突。
    g.db.commit()

    def run_screening_thread():
        """后台线程：执行初筛引擎并通过队列推送进度"""
        try:
            from screening_engine import run_screening
            # 小白讲解：后台线程不在Flask请求上下文中，需要用app.app_context()创建应用上下文
            # 否则db.get_db()等依赖Flask上下文的函数会报"Working outside of application context"
            # template_name 透传给初筛引擎，用于加载模板的规则参数和通过线阈值。
            with app.app_context():
                run_screening(req_id, current_user_id, progress_queue, template_name=template_name)
        except Exception as e:
            progress_queue.put({"type": "error", "message": f"初筛引擎失败：{str(e)}"})

    # 先放一条"正在启动"消息，让前端立即收到响应
    progress_queue.put({"type": "progress", "step": 0, "total": 3, "desc": "正在启动初筛引擎..."})

    # 启动初筛线程
    thread = threading.Thread(target=run_screening_thread)
    thread.start()

    # generator：从队列读消息，以SSE格式推送给浏览器
    def generate():
        """SSE事件生成器：把队列里的消息实时推送给前端"""
        yield b": connected\n\n"

        while True:
            try:
                item = progress_queue.get(timeout=1)
            except queue.Empty:
                yield b": heartbeat\n\n"
                continue

            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n".encode("utf-8")

            if item.get("type") in ("done", "error"):
                break

    return Response(
        generate(),
        mimetype="text/event-stream",
        direct_passthrough=True,
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ==================== 初筛规则管理路由 ====================
@app.route("/screening/rules")
def screening_rules_config():
    """规则配置页 - 展示所有一票否决规则和评分规则，支持启用/禁用和参数修改"""
    from screening_rules import list_rule_templates, list_user_templates, get_rule_template
    # 接收req_id参数（从AI初筛页面跳转过来时带上），用于返回按钮跳回AI初筛页
    req_id = request.args.get("req_id", type=int)
    rules = list_rule_templates()
    templates = list_user_templates(g.user_id)
    # 按类型分组
    veto_rules = [r for r in rules if r["rule_type"] == "veto"]
    score_rules = [r for r in rules if r["rule_type"] == "score"]
    check_rules = [r for r in rules if r["rule_type"] == "check"]

    # 小白讲解：从数据库读取通过标准阈值（threshold_pass/threshold_manual_review），
    # 传给前端"初筛通过标准"卡片用于填充输入框默认值。
    # 如果数据库没有这两条记录，使用默认值 75 / 60。
    pass_rule = get_rule_template("threshold_pass")
    review_rule = get_rule_template("threshold_manual_review")
    threshold_pass = pass_rule["max_score"] if pass_rule and pass_rule.get("max_score") is not None else 75
    threshold_manual_review = review_rule["max_score"] if review_rule and review_rule.get("max_score") is not None else 60

    # 计算评分规则当前总分（必须=100，否则初筛总分不正确）
    score_total = sum(r["max_score"] for r in score_rules if r.get("max_score"))

    return render_template("screening/rule_config.html",
                           veto_rules=veto_rules, score_rules=score_rules,
                           check_rules=check_rules, templates=templates,
                           threshold_pass=threshold_pass,
                           threshold_manual_review=threshold_manual_review,
                           score_total=score_total,
                           req_id=req_id)


@app.route("/screening/rules/update", methods=["POST"])
def screening_rules_update():
    """
    更新规则配置（启用/禁用、修改条件参数、评分参数）

    小白讲解：这个路由处理两种来源的提交：
    1. 规则列表页的"启用/禁用"按钮：只传 rule_code + is_enabled
    2. 规则编辑模态框的"保存修改"按钮：传可视化字段（capital_threshold/allowed_status/veto_reason/max_score/scoring_logic）
       后端根据规则编码自动拼装条件JSON和动作JSON，业务人员不需要接触JSON。
    """
    from screening_rules import update_rule_template, get_rule_template
    import json as _json

    # 获取req_id（用于保存后返回AI初筛页）
    req_id = request.form.get("req_id", "") or request.args.get("req_id", "")

    rule_code = request.form.get("rule_code", "")
    is_enabled = 1 if request.form.get("is_enabled") == "on" else 0

    # 基础更新：启用/禁用状态
    updates = {"is_enabled": is_enabled}

    # 查询当前规则，判断类型（veto/score）
    rule = get_rule_template(rule_code)
    if not rule:
        flash(f"规则 {rule_code} 不存在", "danger")
        return redirect(url_for("screening_rules_config", req_id=req_id) if req_id else url_for("screening_rules_config"))

    rule_type = rule["rule_type"]

    # 小白讲解：区分两种提交来源
    # 列表页"启用/禁用"按钮：只传 rule_code + is_enabled，不传 veto_reason
    # 编辑模态框"保存修改"按钮：传 rule_code + is_enabled + veto_reason（否决规则）或 max_score（评分规则）
    is_from_edit_modal = "veto_reason" in request.form or "max_score" in request.form

    # 如果来自列表页的启用/禁用按钮，只更新状态即可
    if not is_from_edit_modal:
        if update_rule_template(rule_code, updates):
            action_text = "已启用" if is_enabled else "已禁用"
            flash(f"规则「{rule['rule_name']}」{action_text}", "success")
        else:
            flash(f"规则「{rule['rule_name']}」更新失败", "danger")
        return redirect(url_for("screening_rules_config", req_id=req_id) if req_id else url_for("screening_rules_config"))

    # ==================== 一票否决规则：接收可视化字段，自动拼装JSON ====================
    if rule_type == "veto":
        veto_reason = request.form.get("veto_reason", "").strip()
        if not veto_reason:
            flash("请填写否决原因提示语", "danger")
            return redirect(url_for("screening_rules_config", req_id=req_id) if req_id else url_for("screening_rules_config"))

        # 根据规则编码拼装条件JSON
        if rule_code == "veto_capital":
            # 注册资本阈值：从数字输入框取值，拼成条件JSON
            threshold = request.form.get("capital_threshold", type=int)
            if threshold is None or threshold < 0:
                flash("请填写有效的注册资本阈值（正整数）", "danger")
                return redirect(url_for("screening_rules_config", req_id=req_id) if req_id else url_for("screening_rules_config"))
            condition = {
                "type": "single", "field": "reg_capital_wan",
                "operator": "lt", "value": threshold, "unit": "万元"
            }
            updates["default_condition"] = _json.dumps(condition, ensure_ascii=False)
            action_result = "veto"

        elif rule_code == "veto_operating_status":
            # 经营状态：从多选框取值，拼成条件JSON
            allowed_statuses = request.form.getlist("allowed_status")
            if not allowed_statuses:
                flash("请至少勾选一个允许的经营状态", "danger")
                return redirect(url_for("screening_rules_config", req_id=req_id) if req_id else url_for("screening_rules_config"))
            condition = {
                "type": "single", "field": "operating_status",
                "operator": "not_in", "value": allowed_statuses
            }
            updates["default_condition"] = _json.dumps(condition, ensure_ascii=False)
            action_result = "veto"

        elif rule_code == "veto_capital_unknown":
            # 注册资本未披露：特殊规则，result是manual_review而非veto
            condition = {
                "type": "single", "field": "reg_capital_wan",
                "operator": "is_null", "value": None
            }
            updates["default_condition"] = _json.dumps(condition, ensure_ascii=False)
            action_result = "manual_review"

        else:
            # 其他布尔型规则：保留原有条件，只更新否决原因
            # 从数据库取当前条件，不修改
            action_result = "veto"
            # 特殊规则保留原result
            try:
                old_action = _json.loads(rule["default_action"])
                if old_action.get("result") == "manual_review":
                    action_result = "manual_review"
            except (_json.JSONDecodeError, TypeError):
                pass

        # 拼装动作JSON（所有否决规则统一处理）
        action = {"result": action_result, "reason": veto_reason}
        # 保留原有的 need_manual_review 标记（如平台侵权规则）
        try:
            old_action = _json.loads(rule["default_action"])
            if old_action.get("need_manual_review"):
                action["need_manual_review"] = True
        except (_json.JSONDecodeError, TypeError):
            pass
        updates["default_action"] = _json.dumps(action, ensure_ascii=False)

    # ==================== 评分规则：接收满分值和评分逻辑 ====================
    elif rule_type == "score":
        max_score = request.form.get("max_score", type=int)
        scoring_logic = request.form.get("scoring_logic", "").strip()

        if max_score is not None:
            if max_score < 0 or max_score > 100:
                flash("满分分值必须在0-100之间", "danger")
                return redirect(url_for("screening_rules_config", req_id=req_id) if req_id else url_for("screening_rules_config"))
            # 小白讲解：单条规则保存时不校验总分=100，只校验本规则范围。
            # 原因：用户调整评分通常需要改多条规则才能平衡总分（如A加5分、B减5分），
            # 如果每改一条就强制总分=100，用户永远改不动。
            # 总分校验放在"保存为模板"和"开始初筛"两个时机，那里才需要完整配置自洽。
            # 页面顶部会实时显示当前总分，提醒用户是否还需调整。
            updates["max_score"] = max_score
        if scoring_logic:
            updates["scoring_logic"] = scoring_logic

    if update_rule_template(rule_code, updates):
        flash(f"规则「{rule['rule_name']}」已更新", "success")
    else:
        flash(f"规则「{rule['rule_name']}」更新失败", "danger")
    return redirect(url_for("screening_rules_config", req_id=req_id) if req_id else url_for("screening_rules_config"))


@app.route("/screening/rules/threshold", methods=["POST"])
def screening_rules_threshold():
    """
    更新初筛通过标准（通过线、人工确认线）

    小白讲解：业务人员在规则配置页修改"通过线"和"人工确认线"两个数字后，
    通过这个路由保存到数据库的 threshold_pass 和 threshold_manual_review 两条记录。
    下次初筛时 screening_engine.py 会读取新的阈值做判定。

    规则：
    - 通过线必须 >= 人工确认线
    - 若两者相等，相当于取消人工确认环节（低于此分一律未通过）
    - 两者都必须在 0-100 之间
    """
    from screening_rules import update_rule_template

    # 获取req_id（用于保存后返回AI初筛页）
    req_id = request.form.get("req_id", "") or request.args.get("req_id", "")

    pass_val = request.form.get("threshold_pass", type=int)
    review_val = request.form.get("threshold_manual_review", type=int)

    # 参数校验
    if pass_val is None or pass_val < 0 or pass_val > 100:
        flash("通过线必须是0-100之间的数字", "danger")
        return redirect(url_for("screening_rules_config", req_id=req_id) if req_id else url_for("screening_rules_config"))
    if review_val is None or review_val < 0 or review_val > 100:
        flash("人工确认线必须是0-100之间的数字", "danger")
        return redirect(url_for("screening_rules_config", req_id=req_id) if req_id else url_for("screening_rules_config"))
    if pass_val < review_val:
        flash(f"通过线({pass_val})不能低于人工确认线({review_val})", "danger")
        return redirect(url_for("screening_rules_config", req_id=req_id) if req_id else url_for("screening_rules_config"))

    # 更新两条配置记录的 max_score 字段
    # 小白讲解：通过线/人工确认线借用 max_score 列存储数值，screening_engine.py 读取时也用 max_score
    ok1 = update_rule_template("threshold_pass", {"max_score": pass_val})
    ok2 = update_rule_template("threshold_manual_review", {"max_score": review_val})

    if ok1 and ok2:
        flash(f"初筛通过标准已更新：≥{pass_val}分通过，{review_val}-{pass_val - 1}分人工确认，<{review_val}分未通过", "success")
    else:
        flash("通过标准更新失败，请联系管理员检查数据库配置", "danger")
    return redirect(url_for("screening_rules_config", req_id=req_id) if req_id else url_for("screening_rules_config"))


@app.route("/screening/rules/template/save", methods=["POST"])
def screening_rules_template_save():
    """保存当前规则配置为模板（完整快照：含阈值/满分/通过线/启用状态）"""
    from screening_rules import save_as_template, list_rule_templates, get_score_rules
    req_id = request.form.get("req_id", "") or request.args.get("req_id", "")
    template_name = request.form.get("template_name", "").strip()
    if not template_name:
        flash("请输入模板名称", "danger")
        return redirect(url_for("screening_rules_config", req_id=req_id) if req_id else url_for("screening_rules_config"))

    # 小白讲解：保存模板前强制校验"所有启用的评分规则满分总和=100"。
    # 原因：单条规则保存时允许临时≠100（方便逐步调整），但模板是完整配置快照，
    # 必须自洽。如果总分别100，初筛总分会不对（如满分105但通过线75，语义错乱）。
    # 启用的评分规则才参与校验，禁用的不计算。
    score_rules = get_score_rules()
    enabled_score_rules = [r for r in score_rules if r.get("is_enabled")]
    score_total = sum(r["max_score"] for r in enabled_score_rules)
    if score_total != 100:
        flash(f"保存模板失败！启用的评分规则满分总和必须等于100。"
              f"当前{len(enabled_score_rules)}条启用规则满分合计{score_total}，"
              f"请先到规则配置页调整满分（单条保存不会被拦截），总分=100后再保存模板。", "danger")
        return redirect(url_for("screening_rules_config", req_id=req_id) if req_id else url_for("screening_rules_config"))

    # 小白讲解：读取当前全局表所有规则的完整参数（不只是 is_enabled），
    # 包括条件表达式 condition、满分值 max_score、通过线阈值，全部快照存为模板。
    # 这样保存的模板是一份完整独立配置，加载时能完整还原初筛规则。
    rules = list_rule_templates()
    rule_overrides = []
    for r in rules:
        # 解析条件JSON（list_rule_templates返回的default_condition是字符串，需解析为dict再存）
        import json as _json
        try:
            cond = _json.loads(r["default_condition"]) if r.get("default_condition") else None
        except Exception:
            cond = None
        rule_overrides.append({
            "rule_code": r["rule_code"],
            "is_enabled": r["is_enabled"],
            "custom_condition": cond,                  # 条件（如注册资本阈值100万）
            "custom_score_cap": r.get("max_score"),    # 满分值（通过线/评分上限都存在这个字段）
        })

    count = save_as_template(g.user_id, template_name, rule_overrides)
    flash(f"模板「{template_name}」已保存（{count}条规则，含完整参数，总分{score_total}）", "success")
    return redirect(url_for("screening_rules_config", req_id=req_id) if req_id else url_for("screening_rules_config"))


@app.route("/screening/rules/template/delete", methods=["POST"])
def screening_rules_template_delete():
    """
    删除已保存的规则模板

    小白讲解：管理员在规则配置页点模板后面的"删除"按钮，调用这个路由。
    按 template_name 删除该模板的所有规则实例记录（一个模板由17条记录组成）。
    删除后该模板不再出现在初筛页的模板选择下拉框里。
    """
    from screening_rules import delete_template
    req_id = request.form.get("req_id", "") or request.args.get("req_id", "")
    template_name = request.form.get("template_name", "").strip()
    if not template_name:
        flash("未指定要删除的模板", "danger")
        return redirect(url_for("screening_rules_config", req_id=req_id) if req_id else url_for("screening_rules_config"))

    deleted = delete_template(template_name)
    if deleted > 0:
        flash(f"模板「{template_name}」已删除（共{deleted}条规则记录）", "success")
    else:
        flash(f"模板「{template_name}」不存在或已被删除", "warning")
    return redirect(url_for("screening_rules_config", req_id=req_id) if req_id else url_for("screening_rules_config"))


@app.route("/screening/rules/audit/<run_id>")
def screening_audit_detail(run_id):
    """审计日志详情页 - 查看某次初筛运行的完整执行过程"""
    from screening_audit import generate_audit_report
    report = generate_audit_report(run_id, user_id=g.user_id if g.current_user["role"] != "admin" else None)
    return render_template("screening/audit_detail.html", report=report)


# ==================== 管理中心 ====================
@app.route("/admin")
def admin_index():
    """管理中心首页 - 展示管理功能入口"""
    cursor = g.db.cursor()
    cursor.execute("SELECT COUNT(*) as cnt FROM users")
    user_count = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM ai_providers")
    provider_count = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM ai_model_configs")
    config_count = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM search_platforms WHERE is_enabled=1")
    platform_count = cursor.fetchone()["cnt"]
    return render_template("admin/index.html",
                           user_count=user_count, provider_count=provider_count,
                           config_count=config_count, platform_count=platform_count)


# ---------- 用户管理 ----------
@app.route("/admin/users")
def admin_user_list():
    """用户列表 - 支持按角色/状态筛选和用户名搜索"""
    cursor = g.db.cursor()
    role = request.args.get("role", "").strip()
    is_active = request.args.get("is_active", "").strip()
    name_search = request.args.get("name_search", "").strip()

    where_clauses = []
    params = []
    if role:
        where_clauses.append("role = %s")
        params.append(role)
    if is_active:
        where_clauses.append("is_active = %s")
        params.append(int(is_active))
    if name_search:
        where_clauses.append("username LIKE %s")
        params.append(f"%{name_search}%")
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    cursor.execute(f"SELECT * FROM users {where_sql} ORDER BY created_at DESC", params)
    users = cursor.fetchall()
    return render_template("admin/users/list.html",
                           users=users, current_role=role,
                           current_is_active=is_active, current_name_search=name_search)


@app.route("/admin/users/create", methods=["GET", "POST"])
def admin_user_create():
    """新增用户 - 管理员填写用户名/显示名/密码/角色"""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        display_name = request.form.get("display_name", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "user").strip()

        # 表单校验
        if not username or not display_name or not password:
            flash("用户名、显示名、密码不能为空", "danger")
            return redirect(url_for("admin_user_create"))
        if role not in ("admin", "user"):
            role = "user"
        if len(password) < 4:
            flash("密码长度至少4位", "danger")
            return redirect(url_for("admin_user_create"))

        cursor = g.db.cursor()
        # 检查用户名是否已存在
        cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
        if cursor.fetchone():
            flash(f"用户名'{username}'已存在", "danger")
            return redirect(url_for("admin_user_create"))

        # 创建用户（密码用PBKDF2哈希存储）
        password_hash = hash_password(password)
        now = now_str()
        cursor.execute("""
            INSERT INTO users (username, password_hash, display_name, role, is_active, created_at, updated_at)
            VALUES (%s, %s, %s, %s, 1, %s, %s)
        """, (username, password_hash, display_name, role, now, now))
        g.db.commit()
        flash(f"用户'{display_name}'创建成功", "success")
        return redirect(url_for("admin_user_list"))

    return render_template("admin/users/form.html", user=None)


@app.route("/admin/users/<int:id>/edit", methods=["GET", "POST"])
def admin_user_edit(id):
    """编辑用户 - 修改显示名和角色（不在此处改密码）"""
    cursor = g.db.cursor()
    cursor.execute("SELECT * FROM users WHERE id = %s", (id,))
    user = cursor.fetchone()
    if not user:
        flash("用户不存在", "danger")
        return redirect(url_for("admin_user_list"))

    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()
        role = request.form.get("role", "user").strip()
        if not display_name:
            flash("显示名不能为空", "danger")
            return redirect(url_for("admin_user_edit", id=id))
        if role not in ("admin", "user"):
            role = "user"

        cursor.execute("UPDATE users SET display_name=%s, role=%s, updated_at=%s WHERE id=%s",
                       (display_name, role, now_str(), id))
        g.db.commit()
        flash("用户信息已更新", "success")
        return redirect(url_for("admin_user_list"))

    return render_template("admin/users/form.html", user=user)


@app.route("/admin/users/<int:id>/reset-password", methods=["GET", "POST"])
def admin_user_reset_password(id):
    """重置用户密码 - 管理员设置新密码"""
    cursor = g.db.cursor()
    cursor.execute("SELECT * FROM users WHERE id = %s", (id,))
    user = cursor.fetchone()
    if not user:
        flash("用户不存在", "danger")
        return redirect(url_for("admin_user_list"))

    if request.method == "POST":
        new_password = request.form.get("password", "")
        if len(new_password) < 4:
            flash("密码长度至少4位", "danger")
            return redirect(url_for("admin_user_reset_password", id=id))
        password_hash = hash_password(new_password)
        cursor.execute("UPDATE users SET password_hash=%s, updated_at=%s WHERE id=%s",
                       (password_hash, now_str(), id))
        g.db.commit()
        flash(f"用户'{user['display_name']}'的密码已重置", "success")
        return redirect(url_for("admin_user_list"))

    return render_template("admin/users/reset_password.html", user=user)


@app.route("/admin/users/<int:id>/toggle-status", methods=["POST"])
def admin_user_toggle_status(id):
    """启用/停用用户 - 停用后该用户无法登录"""
    cursor = g.db.cursor()
    cursor.execute("SELECT * FROM users WHERE id = %s", (id,))
    user = cursor.fetchone()
    if not user:
        flash("用户不存在", "danger")
        return redirect(url_for("admin_user_list"))
    # 不允许停用自己
    if id == g.user_id:
        flash("不能停用自己的账号", "danger")
        return redirect(url_for("admin_user_list"))

    new_status = 0 if user["is_active"] else 1
    cursor.execute("UPDATE users SET is_active=%s, updated_at=%s WHERE id=%s",
                   (new_status, now_str(), id))
    g.db.commit()
    action = "停用" if new_status == 0 else "启用"
    flash(f"用户'{user['display_name']}'已{action}", "success")
    return redirect(url_for("admin_user_list"))


# ---------- 模型与平台管理（阶段6）----------
@app.route("/admin/models")
def admin_models_index():
    """模型与平台管理首页 - 展示三个板块入口：服务商/场景配置/搜索平台"""
    cursor = g.db.cursor()
    # 统计各类配置数量
    cursor.execute("SELECT COUNT(*) as cnt FROM ai_providers")
    provider_count = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM ai_model_configs")
    config_count = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM search_platforms")
    platform_count = cursor.fetchone()["cnt"]
    # 检查核心服务商配置状态（密钥是否已填）
    cursor.execute("SELECT provider_code, api_key, is_enabled FROM ai_providers")
    providers_status = {row["provider_code"]: {"has_key": bool(row["api_key"]), "enabled": bool(row["is_enabled"])} for row in cursor.fetchall()}
    return render_template("admin/models/index.html",
                           provider_count=provider_count, config_count=config_count,
                           platform_count=platform_count, providers_status=providers_status)


# ---------- 服务商管理 ----------
@app.route("/admin/models/providers")
def admin_provider_list():
    """服务商列表 - 显示所有AI服务商/搜索平台/数据API的配置状态"""
    cursor = g.db.cursor()
    # 按类型分组显示
    cursor.execute("SELECT * FROM ai_providers ORDER BY provider_type, id")
    providers = cursor.fetchall()
    return render_template("admin/models/providers/list.html", providers=providers)


@app.route("/admin/models/providers/<int:id>/edit", methods=["GET", "POST"])
def admin_provider_edit(id):
    """编辑服务商 - 修改显示名、API地址、密钥、启用状态"""
    cursor = g.db.cursor()
    if request.method == "POST":
        provider_name = request.form.get("provider_name", "").strip()
        base_url = request.form.get("base_url", "").strip()
        api_key = request.form.get("api_key", "").strip()
        is_enabled = 1 if request.form.get("is_enabled") else 0

        # 表单校验
        if not provider_name:
            flash("服务商名称不能为空", "danger")
            return redirect(url_for("admin_provider_edit", id=id))

        # 检查是否要清空密钥（管理员提交空密钥时保留原值，避免误清空）
        if not api_key:
            cursor.execute("SELECT api_key FROM ai_providers WHERE id = %s", (id,))
            api_key = cursor.fetchone()["api_key"]

        # 更新数据库
        cursor.execute("""
            UPDATE ai_providers
            SET provider_name = %s, base_url = %s, api_key = %s, is_enabled = %s, updated_at = %s
            WHERE id = %s
        """, (provider_name, base_url, api_key, is_enabled, now_str(), id))
        g.db.commit()

        # 热更新内存配置
        model_config.refresh_configs()
        flash(f"服务商'{provider_name}'配置已更新", "success")
        return redirect(url_for("admin_provider_list"))

    cursor.execute("SELECT * FROM ai_providers WHERE id = %s", (id,))
    provider = cursor.fetchone()
    if not provider:
        flash("服务商不存在", "danger")
        return redirect(url_for("admin_provider_list"))
    return render_template("admin/models/providers/form.html", provider=provider)


# ---------- 场景配置管理 ----------
@app.route("/admin/models/configs")
def admin_config_list():
    """场景配置列表 - 显示7个AI调用场景的模型参数"""
    cursor = g.db.cursor()
    # 关联服务商表，显示服务商名称
    cursor.execute("""
        SELECT c.*, p.provider_name
        FROM ai_model_configs c
        LEFT JOIN ai_providers p ON c.provider_id = p.id
        ORDER BY c.sort_order, c.id
    """)
    configs = cursor.fetchall()
    return render_template("admin/models/configs/list.html", configs=configs)


@app.route("/admin/models/configs/<int:id>/edit", methods=["GET", "POST"])
def admin_config_edit(id):
    """编辑场景配置 - 修改模型名、思考强度、温度、max_tokens等参数"""
    cursor = g.db.cursor()
    if request.method == "POST":
        model_name = request.form.get("model_name", "").strip()
        thinking_enabled = 1 if request.form.get("thinking_enabled") else 0
        thinking_effort = request.form.get("thinking_effort", "high").strip()
        max_tokens = int(request.form.get("max_tokens", 4096))
        temperature = float(request.form.get("temperature", 0.3))
        timeout_seconds = int(request.form.get("timeout_seconds", 120))
        is_enabled = 1 if request.form.get("is_enabled") else 0

        # 表单校验
        if not model_name:
            flash("模型名称不能为空", "danger")
            return redirect(url_for("admin_config_edit", id=id))
        if thinking_effort not in ("low", "medium", "high", "max", ""):
            thinking_effort = "high"

        # 更新数据库
        cursor.execute("""
            UPDATE ai_model_configs
            SET model_name = %s, thinking_enabled = %s, thinking_effort = %s,
                max_tokens = %s, temperature = %s, timeout_seconds = %s, is_enabled = %s, updated_at = %s
            WHERE id = %s
        """, (model_name, thinking_enabled, thinking_effort, max_tokens, temperature,
              timeout_seconds, is_enabled, now_str(), id))
        g.db.commit()

        # 热更新内存配置
        model_config.refresh_configs()
        flash("场景配置已更新", "success")
        return redirect(url_for("admin_config_list"))

    cursor.execute("SELECT * FROM ai_model_configs WHERE id = %s", (id,))
    config = cursor.fetchone()
    if not config:
        flash("场景配置不存在", "danger")
        return redirect(url_for("admin_config_list"))
    return render_template("admin/models/configs/form.html", config=config)


# ---------- 搜索平台管理 ----------
@app.route("/admin/platforms")
def admin_platform_list():
    """搜索平台列表 - 显示参与供应商搜索的平台及优先级"""
    cursor = g.db.cursor()
    # 关联服务商表，显示平台名称和密钥状态
    cursor.execute("""
        SELECT sp.*, ap.provider_name, ap.provider_code, ap.api_key,
               CASE WHEN ap.api_key IS NULL OR ap.api_key = '' THEN 0 ELSE 1 END as has_key
        FROM search_platforms sp
        JOIN ai_providers ap ON sp.provider_id = ap.id
        ORDER BY sp.priority, sp.id
    """)
    platforms = cursor.fetchall()
    return render_template("admin/platforms/list.html", platforms=platforms)


@app.route("/admin/platforms/<int:id>/edit", methods=["GET", "POST"])
def admin_platform_edit(id):
    """编辑搜索平台 - 修改优先级、最大结果数、启用状态"""
    cursor = g.db.cursor()
    if request.method == "POST":
        priority = int(request.form.get("priority", 0))
        max_results = int(request.form.get("max_results", 50))
        is_enabled = 1 if request.form.get("is_enabled") else 0

        # 更新数据库
        cursor.execute("""
            UPDATE search_platforms
            SET priority = %s, max_results = %s, is_enabled = %s, updated_at = %s
            WHERE id = %s
        """, (priority, max_results, is_enabled, now_str(), id))
        g.db.commit()

        # 热更新内存配置
        model_config.refresh_configs()
        flash("搜索平台配置已更新", "success")
        return redirect(url_for("admin_platform_list"))

    cursor.execute("""
        SELECT sp.*, ap.provider_name, ap.provider_code
        FROM search_platforms sp
        JOIN ai_providers ap ON sp.provider_id = ap.id
        WHERE sp.id = %s
    """, (id,))
    platform = cursor.fetchone()
    if not platform:
        flash("搜索平台不存在", "danger")
        return redirect(url_for("admin_platform_list"))
    return render_template("admin/platforms/form.html", platform=platform)


# ==================== 健康检查接口（Railway等云平台需要200 OK响应）====================
@app.route("/health")
def health():
    """返回200 OK让Railway知道应用正常运行，不做任何登录验证"""
    return "OK", 200

# ==================== 数据库初始化 ====================
# 必须放在模块顶层而不是 if __name__ == "__main__" 里
# 原因：Railway等云平台用 gunicorn app:app 启动，__name__ 不是 "__main__"，
# 导致 init_db() 不被执行，数据库全是空表。移到顶层后无论什么方式启动都会建表。
# try包裹防止数据库还没配好时启动就崩溃（Railway添加MySQL后才生效）。
try:
    db.init_db()
    # 从数据库加载AI配置到内存，让ai_helper和supplier_search能快速读取
    model_config.load_model_configs_from_db()
except Exception as e:
    print(f"[启动] 数据库初始化失败（可能还未配置MySQL）: {e}")

if __name__ == "__main__":
    # 本地开发直接运行 python app.py 时，数据库已在上面初始化过了
    # 这里只负责启动 Web 服务器
    print("=" * 50)
    print("供应商寻源系统启动中...")
    # 云端部署检测：有PORT环境变量（Railway自动注入）→ 监听0.0.0.0供外部访问
    import os as _os
    port = int(_os.environ.get("PORT", "5000"))
    host = "0.0.0.0" if _os.environ.get("PORT") else "127.0.0.1"
    print(f"[启动] 监听地址: {host}:{port}")
    print("=" * 50)
    # 云端用 Flask 内置开发服务器（Railway 兼容性最好）
    # 本地建议手动切到 waitress：python -c "from waitress import serve; from app import app; serve(app, host='127.0.0.1', port=5000)"
    if _os.environ.get("PORT"):
        app.run(host=host, port=port, debug=False)
    else:
        from waitress import serve
        serve(app, host=host, port=port, threads=8, send_bytes=1)
