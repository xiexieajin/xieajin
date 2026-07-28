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


def _save_uploaded_attachments(files):
    """
    把前端上传的附件文件保存到 uploads 目录，返回附件信息列表

    小白讲解：邮件附件保存到 uploads 目录。
    返回的是字典列表，每个字典包含：file_path（路径）、original_filename（原文件名）、
    mime_type（MIME类型）、file_size（大小）、is_image（是否图片）。
    调用方发完邮件后用 _cleanup_attachments 删除非图片附件（图片保留以便会话中查看）。

    参数：files - request.files.getlist(...) 拿到的文件列表
    返回：附件信息字典列表（没文件则空列表）
    """
    import time as _time
    upload_dir = app.config["UPLOAD_FOLDER"]
    os.makedirs(upload_dir, exist_ok=True)  # 确保目录存在
    saved_files = []
    for f in files:
        if not f or not f.filename:
            continue  # 跳过空文件
        # 用 werkzeug 的安全文件名，防止 ../ 之类的路径穿越攻击
        from werkzeug.utils import secure_filename
        safe_name = secure_filename(f.filename) or "attachment"
        # 加时间戳前缀避免重名
        unique_name = f"{int(_time.time() * 1000)}_{safe_name}"
        file_path = os.path.join(upload_dir, unique_name)
        f.save(file_path)

        # 小白讲解：获取文件MIME类型和大小，判断是否图片
        # MIME类型从上传文件的mimetype字段取（浏览器根据文件扩展名判断）
        mime_type = f.mimetype or "application/octet-stream"
        file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        is_image = 1 if mime_type.startswith("image/") else 0

        saved_files.append({
            "file_path": file_path,
            "original_filename": f.filename,
            "saved_filename": unique_name,
            "mime_type": mime_type,
            "file_size": file_size,
            "is_image": is_image,
        })
    return saved_files


def _cleanup_attachments(attachments):
    """
    发完邮件后删除非图片附件的临时文件（图片保留以便会话中查看）

    小白讲解：附件参数既支持旧的"路径列表"格式（兼容老代码），
    也支持新的"字典列表"格式（含文件信息）。
    图片附件保留在 uploads 目录，其他附件删除节省空间。
    """
    if not attachments:
        return
    import os as _os
    for item in attachments:
        # 兼容两种格式：字符串路径（旧）或字典（新）
        if isinstance(item, str):
            path = item
            is_image = False
        elif isinstance(item, dict):
            path = item.get("file_path")
            is_image = item.get("is_image") == 1
        else:
            continue
        # 图片附件保留，其他删除
        if is_image:
            continue
        try:
            if path and _os.path.exists(path):
                _os.remove(path)
        except OSError:
            pass  # 删除失败就算了，不影响主流程


def _save_attachments_to_db(cursor, conn, communication_id, attachments):
    """
    把附件信息写入 communication_attachments 表（图片附件可后续查看）

    小白讲解：发送邮件成功后调用这个函数，把附件的文件名、类型等信息存数据库。
    图片附件的文件保留在 uploads 目录，用户在会话中可以查看图片。
    非图片附件的文件已删除，但数据库记录仍保留（显示文件名，无下载链接）。

    参数：
        cursor: 数据库游标
        conn: 数据库连接
        communication_id: 关联的沟通记录ID
        attachments: 附件信息字典列表（来自 _save_uploaded_attachments 的返回值）
    """
    from db import now_str
    for att in attachments:
        if not isinstance(att, dict):
            continue
        cursor.execute("""
            INSERT INTO communication_attachments
            (communication_id, original_filename, saved_filename, file_path,
             mime_type, file_size, is_image, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            communication_id,
            att.get("original_filename", ""),
            att.get("saved_filename", ""),
            att.get("file_path", ""),
            att.get("mime_type", ""),
            att.get("file_size", 0),
            att.get("is_image", 0),
            now_str(),
        ))
    conn.commit()


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
        # 小白讲解：用try/except包裹close()，避免连接已被SSE后台线程关闭时
        # 抛出"Already closed"异常，这个异常会中断SSE流式响应导致前端报network error
        try:
            db_conn.close()
        except Exception:
            pass
        g.db = None


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
        # 小白讲解：先保存刚插入的供应商ID，因为后面的 recalc_requirement_status
        # 会执行其他SQL（SELECT/UPDATE），会覆盖 cursor.lastrowid，导致取不到ID
        new_supplier_id = cursor.lastrowid
        # 手动新增了供应商（默认"已寻源待初筛"），需求状态应推进到"寻源中"
        recalc_requirement_status(cursor, data["requirement_id"])
        g.db.commit()

        return redirect(url_for("supplier_detail", id=new_supplier_id))

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
            "customs_export_count": int(request.form.get("customs_export_count", 0) or 0),
            "customs_total_qty": float(request.form.get("customs_total_qty", 0) or 0),
            "customs_total_amount": float(request.form.get("customs_total_amount", 0) or 0),
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
                has_cross_border_exp=%s, dev_stage=%s,
                customs_export_count=%s, customs_total_qty=%s, customs_total_amount=%s,
                updated_at=%s
            WHERE id=%s {uid_sql}
        """, (data["name"], data["intro"], data["factory_address"], data["email"],
              data["phone"], data["main_product"], establish_years, data["establish_date"],
              data["operating_status"], data["has_cross_border_exp"], data["dev_stage"],
              data["customs_export_count"], data["customs_total_qty"], data["customs_total_amount"],
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
    # 产品名称优先取用户在补充表单里输入的 product_name，为空时再回退到上一轮的 prev_product_name
    product_name_input = request.form.get("product_name", "").strip()
    previous_data = {
        "product_name": product_name_input if product_name_input else request.form.get("prev_product_name", "").strip(),
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
        "hs_code": request.form.get("hs_code", "").strip(),
    }

    # 如果前端没传HS编码，用DeepSeek自动归类
    if not data["hs_code"]:
        try:
            from ai_helper import classify_hs_code
            data["hs_code"] = classify_hs_code(data["product_name"])
            if data["hs_code"]:
                print(f"HS编码自动归类：{data['product_name']} → {data['hs_code']}")
        except Exception as e:
            print(f"HS编码自动归异常：{e}")
            data["hs_code"] = ""

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
                requirement_summary=%s, keywords=%s, customization_req=%s,
                hs_code=%s, status='寻源中', updated_at=%s
            WHERE id=%s {uid_sql}
        """, (data["product_aliases"], data["core_functions"], data["material"],
              data["spec_size"], data["first_purchase_qty"], data["acceptable_moq"],
              data["min_ship_qty"], data["acceptable_lead_time"], data["target_market"],
              data["required_certs"], data["requirement_summary"], keywords_json,
              data["customization_req"], data["hs_code"], now_str(), req_id, *uid_params))
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
             hs_code, status, created_at, updated_at, user_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '寻源中', %s, %s, %s)
        """, (data["product_name"], data["product_aliases"], data["core_functions"],
              data["material"], data["spec_size"], data["first_purchase_qty"],
              data["acceptable_moq"], data["min_ship_qty"], data["acceptable_lead_time"],
              data["target_market"], data["required_certs"], data["requirement_summary"],
              keywords_json, data["customization_req"], data["hs_code"],
              now_str(), now_str(), g.user_id))
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
            hs_code = requirement.get("hs_code", "") or ""
            if hs_code:
                print(f"海关搜索使用HS编码：{hs_code}")
            suppliers = search_suppliers(keywords, requirement["product_name"], progress_callback, hs_code)

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
                     product_title, product_link, price, moq,
                     customs_export_count, customs_total_qty, customs_total_amount,
                     created_at, updated_at, user_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '已寻源待初筛',
                            %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s,
                        %s, %s)
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
                      s.get("product_title", ""), s.get("product_link", ""),
                      s.get("price", ""), s.get("moq", ""),
                      s.get("customs_export_count", 0),
                      s.get("customs_total_qty", 0),
                      s.get("customs_total_amount", 0),
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

    # POST请求：提交初筛任务（返回task_id，由前端轮询进度）
    # 小白讲解：改成"消息列表"轮询模式，和供应商搜索保持一致。
    # 原因：Railway代理会切断长时间保持的SSE连接（约5分钟超时），
    # 初筛100+家供应商要10+分钟，连接被切断后前端就报network error。
    # 轮询模式每次请求都是瞬间的，不受长连接超时限制。
    if pending_count == 0:
        flash("没有需要初筛的供应商（所有供应商已完成初筛）", "info")
        return redirect(url_for("requirement_detail", id=req_id))

    import uuid
    task_id = str(uuid.uuid4())

    # 创建任务记录，放入全局task_store（messages列表保留全部进度，req_id用于刷新恢复）
    task_store[task_id] = {
        "messages": [],          # 所有进度消息列表（不再消费式读取，全部保留）
        "status": "running",     # 任务状态：running/done/error
        "result": None,          # 最终结果（done时存审计报告，error时存错误信息）
        "req_id": req_id,        # 关联的需求ID（用户刷新页面后通过它找到正在运行的任务）
        "task_type": "screening",  # 任务类型标记（区分搜索/初筛，便于后续清理）
    }

    # 小白讲解：把user_id存到局部变量，避免后台线程访问g对象（g是请求级的，请求结束后失效）
    current_user_id = g.user_id

    # 小白讲解：读取前端选择的规则模板名（用户在初筛页下拉框选的）。
    # 空字符串表示用全局默认规则，非空表示用该模板保存的规则参数初筛。
    template_name = (request.form.get("template_name") or "").strip() or None

    # 小白讲解：迁移到MySQL后，不再需要手动关闭g.db释放锁（MySQL支持并发读写）。
    # 只需提交当前事务即可。后台线程用独立连接写入，不会与主请求冲突。
    g.db.commit()

    def run_screening_thread():
        """后台线程：执行初筛引擎，把进度写入task_store的messages列表"""
        try:
            from screening_engine import run_screening
            # 小白讲解：用适配器把 screening_engine 的 queue.put 接口
            # 转成写 task_store["messages"]，这样engine代码完全不用改。
            # 适配器加锁保护，避免并发写入冲突。
            class _QueueToTaskStore:
                """把queue.Queue的put接口适配到task_store消息列表"""
                def put(self, msg):
                    with _task_store_lock:
                        task_store[task_id]["messages"].append(msg)

            progress_queue = _QueueToTaskStore()

            # 小白讲解：后台线程不在Flask请求上下文中，需要用app.app_context()创建应用上下文
            # 否则db.get_db()等依赖Flask上下文的函数会报"Working outside of application context"
            # template_name 透传给初筛引擎，用于加载模板的规则参数和通过线阈值。
            with app.app_context():
                report = run_screening(req_id, current_user_id, progress_queue, template_name=template_name)
            # 初筛完成：标记任务完成，存最终结果
            with _task_store_lock:
                task_store[task_id]["status"] = "done"
                task_store[task_id]["result"] = report
        except Exception as e:
            import traceback
            err_msg = f"初筛引擎失败：{str(e)}"
            with _task_store_lock:
                task_store[task_id]["messages"].append({"type": "error", "message": err_msg})
                task_store[task_id]["status"] = "error"
                task_store[task_id]["result"] = {"error": str(e), "traceback": traceback.format_exc()[:500]}

    # 先放一条"正在启动"消息，让前端第一次轮询就能收到响应
    with _task_store_lock:
        task_store[task_id]["messages"].append(
            {"type": "progress", "step": 0, "total": 3, "desc": "正在启动初筛引擎..."}
        )

    # 启动后台初筛线程
    thread = threading.Thread(target=run_screening_thread)
    thread.start()

    # 瞬间返回task_id，前端拿到后开始每3秒轮询进度
    return jsonify({"task_id": task_id, "status": "started"})


@app.route("/ai/auto-screening/<int:req_id>/poll/<task_id>", methods=["GET"])
def poll_ai_screening(req_id, task_id):
    """
    轮询初筛任务进度（每3秒调用一次）

    小白讲解：游标模式——前端传cursor参数表示"上次读到第N条消息"，
    后端返回第N条之后的所有新消息 + 新的cursor位置。
    这样即使前端断线重连，也不会丢失中间的进度消息。
    和供应商搜索的poll接口完全一致。
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


@app.route("/ai/auto-screening/<int:req_id>/status", methods=["GET"])
def ai_screening_status(req_id):
    """
    查询需求是否有正在运行或已完成的初筛任务（用于页面刷新后恢复进度）

    小白讲解：用户刷新页面后，前端的task_id丢了，不知道之前初筛到哪了。
    这个接口根据需求ID(req_id)在task_store里查找初筛类型的任务：
    - 找到running状态 → 返回task_id和全部历史消息，前端恢复进度界面继续轮询
    - 找到done/error状态 → 返回task_id和结果，前端直接显示完成/错误
    - 没找到 → 返回not_found，前端正常显示初筛表单
    """
    with _task_store_lock:
        for task_id, task in task_store.items():
            # 只匹配初筛类型且req_id对应的任务（避免和搜索任务混淆）
            if task.get("task_type") == "screening" and task.get("req_id") == req_id:
                return jsonify({
                    "task_id": task_id,
                    "status": task["status"],
                    "messages": task["messages"],
                    "cursor": len(task["messages"]),
                    "result": task.get("result")
                })

    return jsonify({"status": "not_found"})


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
    from screening_rules import save_as_template, list_rule_templates, get_score_rules, update_rule_template
    req_id = request.form.get("req_id", "") or request.args.get("req_id", "")
    template_name = request.form.get("template_name", "").strip()
    if not template_name:
        flash("请输入模板名称", "danger")
        return redirect(url_for("screening_rules_config", req_id=req_id) if req_id else url_for("screening_rules_config"))

    # 小白讲解：如果表单带了通过线值（从保存模板表单的隐藏字段来的），
    # 先更新数据库的通过线，这样保存模板时读到的是最新通过线。
    # 解决"用户改了通过线但没单独点保存通过标准"的问题。
    tpl_pass = request.form.get("threshold_pass", type=int)
    tpl_review = request.form.get("threshold_manual_review", type=int)
    if tpl_pass is not None and tpl_review is not None:
        if tpl_pass < 0 or tpl_pass > 100 or tpl_review < 0 or tpl_review > 100:
            flash("通过线/人工确认线必须是0-100之间的数字", "danger")
            return redirect(url_for("screening_rules_config", req_id=req_id) if req_id else url_for("screening_rules_config"))
        if tpl_pass < tpl_review:
            flash(f"通过线({tpl_pass})不能低于人工确认线({tpl_review})", "danger")
            return redirect(url_for("screening_rules_config", req_id=req_id) if req_id else url_for("screening_rules_config"))
        update_rule_template("threshold_pass", {"max_score": tpl_pass})
        update_rule_template("threshold_manual_review", {"max_score": tpl_review})

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
    # 注意：通过线（threshold_pass/threshold_manual_review）也在 list_rule_templates() 返回的规则里，
    # 它们的 max_score 就是通过线数值，会被存到 custom_score_cap，加载时 _get_thresholds(template_name) 读取。
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
            "custom_score_cap": r.get("max_score"),    # 满分值（评分规则满分 / 通过线数值都存在这个字段）
        })

    count = save_as_template(g.user_id, template_name, rule_overrides)
    flash(f"模板「{template_name}」已保存（{count}条规则，含完整参数+通过线阈值，总分{score_total}）", "success")
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


@app.route("/screening/rules/template/apply", methods=["POST"])
def screening_rules_template_apply():
    """
    使用模板：把已保存模板的规则参数应用为当前默认配置

    小白讲解：管理员在规则配置页点模板后面的"使用"按钮时调用这个路由。
    做法：把模板里的规则参数（启用状态/条件/满分值/通过线）写回默认规则表，
    覆盖当前默认配置。应用后规则配置页表格会立即显示该模板的参数，
    下次初筛即使用户不在AI初筛页选模板，也会用这套配置。
    注意：应用会覆盖现有默认配置，请在确认提示里告知用户。
    """
    from screening_rules import apply_template_to_default
    req_id = request.form.get("req_id", "") or request.args.get("req_id", "")
    template_name = request.form.get("template_name", "").strip()
    if not template_name:
        flash("未指定要使用的模板", "danger")
        return redirect(url_for("screening_rules_config", req_id=req_id) if req_id else url_for("screening_rules_config"))

    applied = apply_template_to_default(template_name)
    if applied > 0:
        flash(f"模板「{template_name}」已应用为当前默认配置（共{applied}条规则，含通过线阈值）。"
              f"规则配置页已刷新为该模板参数，下次初筛将默认使用此配置。", "success")
    else:
        flash(f"模板「{template_name}」不存在或没有可应用的规则", "warning")
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

    # 小白讲解：查邮箱配置状态，用于管理中心首页显示卡片状态徽章
    email_config = get_email_config()
    email_configured = bool(email_config and email_config.get("gmail_address"))
    email_enabled = bool(email_config and email_config.get("is_enabled"))
    email_address = email_config.get("gmail_address", "") if email_config else ""

    return render_template("admin/index.html",
                           user_count=user_count, provider_count=provider_count,
                           config_count=config_count, platform_count=platform_count,
                           email_configured=email_configured, email_enabled=email_enabled,
                           email_address=email_address)


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


# ==================== 邮件收发功能 ====================
# 小白讲解：以下路由处理邮件发送、邮箱配置、待认领邮件三个功能。
# 邮件核心逻辑在 email_service.py，这里只负责接收网页请求、调用服务、返回页面。
import email_service
from email_service import send_email, get_email_config, poll_inbox_once, claim_pending_email, get_inquiry_template


@app.route("/suppliers/<int:supplier_id>/send-email", methods=["GET", "POST"])
@login_required
def supplier_send_email(supplier_id):
    """
    给供应商发邮件 - GET显示发邮件表单，POST调用Gmail SMTP发送

    小白讲解：用户在供应商详情页点"发邮件"按钮，来到这个页面。
    表单里收件人自动填供应商邮箱，可一键加载询价模板。
    点发送后，系统连Gmail把邮件发出去，成功后自动写一条沟通记录。
    """
    cursor = g.db.cursor()
    # 查供应商信息（加uid过滤）
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"SELECT * FROM suppliers WHERE id = %s {uid_sql}", (supplier_id, *uid_params))
    supplier = cursor.fetchone()
    if not supplier:
        return "供应商不存在", 404

    # 检查邮箱配置是否启用
    config = get_email_config()

    if request.method == "POST":
        # POST：发送邮件
        to_addr = request.form.get("to_addr", "").strip()
        subject = request.form.get("subject", "").strip()
        body = request.form.get("body", "").strip()

        if not to_addr or not subject or not body:
            flash("收件人、主题、正文都不能为空", "danger")
            return redirect(url_for("supplier_send_email", supplier_id=supplier_id))

        # 小白讲解：保存用户上传的附件到临时目录，发完邮件后删除
        attachments = _save_uploaded_attachments(request.files.getlist("attachments"))

        # 调用email_service发送（带附件）
        success, message, _ = send_email(
            to_addr=to_addr,
            subject=subject,
            body=body,
            supplier_id=supplier_id,
            user_id=g.user_id,
            attachments=attachments,
        )

        # 无论成功失败，都清理临时附件文件
        _cleanup_attachments(attachments)

        if success:
            flash(message, "success")
        else:
            flash(message, "danger")
        return redirect(url_for("supplier_detail", id=supplier_id))

    # GET：显示发邮件表单
    # 小白讲解：如果URL带?template=1参数，自动加载询价模板到正文
    load_template = request.args.get("template", "0") == "1"
    initial_body = get_inquiry_template() if load_template else ""

    return render_template("supplier/send_email.html",
                           supplier=supplier, config=config,
                           initial_body=initial_body)


@app.route("/suppliers/<int:supplier_id>/send-email/ajax", methods=["POST"])
@login_required
def supplier_send_email_ajax(supplier_id):
    """
    AJAX方式发送邮件（不刷新页面，前端用fetch调用）

    小白讲解：前端页面用JavaScript调这个接口发送邮件，不用刷新页面。
    返回JSON格式 {success: true/false, message: "..."}。
    """
    cursor = g.db.cursor()
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"SELECT id, email FROM suppliers WHERE id = %s {uid_sql}", (supplier_id, *uid_params))
    supplier = cursor.fetchone()
    if not supplier:
        return jsonify({"success": False, "message": "供应商不存在"}), 404

    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "请求数据为空"}), 400

    to_addr = data.get("to_addr", "").strip()
    subject = data.get("subject", "").strip()
    body = data.get("body", "").strip()

    if not to_addr or not subject or not body:
        return jsonify({"success": False, "message": "收件人、主题、正文都不能为空"})

    success, message, _ = send_email(
        to_addr=to_addr, subject=subject, body=body,
        supplier_id=supplier_id, user_id=g.user_id,
    )
    return jsonify({"success": success, "message": message})


# ==================== 邮件管理（邮箱配置 + 剔除规则，合并到模型与平台管理）====================
@app.route("/admin/models/email", methods=["GET", "POST"])
@admin_required
def admin_models_email():
    """
    邮件管理统一页 - 用Tab切换"邮箱配置"和"邮件剔除规则"两个板块

    小白讲解：原来邮箱配置和剔除规则是两个独立页面，分散在管理中心首页。
    现在合并到「模型与平台管理」下，一个页面用Tab切换，方便集中管理。
    GET 显示页面，POST 保存邮箱配置（剔除规则的增删改走独立路由）。
    """
    cursor = g.db.cursor()

    if request.method == "POST":
        # POST：保存邮箱配置
        gmail_address = request.form.get("gmail_address", "").strip()
        app_password = request.form.get("app_password", "").strip()
        sender_name = request.form.get("sender_name", "").strip()
        poll_interval = int(request.form.get("poll_interval", 300))
        is_enabled = 1 if request.form.get("is_enabled") else 0

        # 小白讲解：email_config表只有一条记录(id=1)，用INSERT ON DUPLICATE KEY UPDATE
        # 实现存在就更新、不存在就插入（upsert）
        cursor.execute("""
            INSERT INTO email_config (id, gmail_address, app_password, sender_name,
                                       poll_interval, is_enabled, created_at, updated_at)
            VALUES (1, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                gmail_address = VALUES(gmail_address),
                app_password = VALUES(app_password),
                sender_name = VALUES(sender_name),
                poll_interval = VALUES(poll_interval),
                is_enabled = VALUES(is_enabled),
                updated_at = VALUES(updated_at)
        """, (gmail_address, app_password, sender_name, poll_interval,
              is_enabled, now_str(), now_str()))
        g.db.commit()

        flash("邮箱配置已保存", "success")
        return redirect(url_for("admin_models_email"))

    # GET：显示统一页面（包含邮箱配置表单 + 剔除规则列表）
    config = get_email_config()
    # 查所有剔除规则，按优先级升序排列
    cursor.execute("""
        SELECT * FROM email_filter_rules
        ORDER BY priority ASC, id ASC
    """)
    rules = cursor.fetchall()
    return render_template("admin/models/email.html", config=config, rules=rules)


# 旧路由保留重定向，避免收藏夹或旧链接失效
@app.route("/admin/email-config", methods=["GET", "POST"])
@admin_required
def admin_email_config():
    """旧邮箱配置路由 - 重定向到新的合并页"""
    return redirect(url_for("admin_models_email"), code=301 if request.method == "GET" else 307)


@app.route("/admin/email-config/test", methods=["POST"])
@admin_required
def admin_email_config_test():
    """
    测试发送一封邮件，验证Gmail配置是否正确

    小白讲解：管理员配置完Gmail后，点"测试发送"给自己发一封测试邮件，
    如果收到说明配置正确，如果失败说明账号或密码有问题。
    """
    config = get_email_config()
    if not config or not config.get("is_enabled"):
        return jsonify({"success": False, "message": "请先保存并启用邮箱配置"})

    test_addr = request.form.get("test_addr", "").strip() or config.get("gmail_address", "")
    if not test_addr:
        return jsonify({"success": False, "message": "请填写测试收件邮箱"})

    # 发一封简单的测试邮件
    test_subject = "供应商寻源系统 - 邮件测试"
    test_body = """\
<div style="font-family: Arial, sans-serif; line-height: 1.8;">
    <p>这是一封测试邮件。</p>
    <p>如果您收到此邮件，说明 Gmail 邮箱配置正确，供应商寻源系统可以正常发送邮件。</p>
    <p style="color: #888; font-size: 12px;">此邮件由系统自动发送，请勿回复。</p>
</div>
"""
    success, message, _ = send_email(
        to_addr=test_addr, subject=test_subject, body=test_body,
        supplier_id=None, user_id=g.user_id,
    )
    return jsonify({"success": success, "message": message})


@app.route("/admin/email-config/poll-now", methods=["POST"])
@admin_required
def admin_email_config_poll_now():
    """
    手动触发一次IMAP收件（不等轮询间隔）

    小白讲解：管理员想立刻看看有没有新邮件，不用等5分钟轮询，点这个按钮立即收一次。
    """
    try:
        count = poll_inbox_once()
        return jsonify({"success": True, "message": f"收件完成，本次新增 {count} 封邮件"})
    except Exception as e:
        return jsonify({"success": False, "message": f"收件失败：{str(e)}"})


# ==================== 待认领邮件 ====================
@app.route("/pending-emails")
@login_required
def pending_email_list():
    """
    待认领邮件列表 - 显示匹配不到供应商的邮件，用户手动认领

    小白讲解：供应商换了邮箱回复，或新供应商主动发邮件来，系统按邮箱匹配不到供应商，
    邮件就出现在这个列表里。用户看到后点"认领"，选择是哪个供应商的回复。
    """
    cursor = g.db.cursor()
    # 查未认领的邮件（is_claimed=0），按收件时间倒序
    cursor.execute("""
        SELECT * FROM pending_emails
        WHERE is_claimed = 0
        ORDER BY created_at DESC
    """)
    pending_emails = cursor.fetchall()

    # 查所有供应商（用于认领时下拉选择）
    uid_sql, uid_params = _uid_clause()
    cursor.execute(f"""
        SELECT id, name, email, phone, main_product
        FROM suppliers
        WHERE 1=1 {uid_sql}
        ORDER BY name
    """, uid_params)
    suppliers = cursor.fetchall()

    return render_template("pending_emails.html",
                           pending_emails=pending_emails, suppliers=suppliers)


@app.route("/pending-emails/<int:pending_id>/claim", methods=["POST"])
@login_required
def pending_email_claim(pending_id):
    """
    认领一封待认领邮件，关联到指定供应商

    小白讲解：用户在待认领邮件列表点"认领"，选择供应商后提交。
    系统把这封邮件从pending_emails转到communications表，供应商详情页就能看到了。
    """
    supplier_id = request.form.get("supplier_id", type=int)
    if not supplier_id:
        flash("请选择供应商", "danger")
        return redirect(url_for("pending_email_list"))

    success, message = claim_pending_email(pending_id, supplier_id, g.user_id)
    if success:
        flash(message, "success")
    else:
        flash(message, "danger")
    return redirect(url_for("pending_email_list"))


# ==================== 邮件剔除规则管理（管理员）====================
# 旧路由保留重定向到合并页
@app.route("/admin/email-filter-rules")
@admin_required
def admin_email_filter_rules():
    """旧剔除规则列表路由 - 重定向到新的合并页"""
    return redirect(url_for("admin_models_email"))


@app.route("/admin/email-filter-rules/cleanup", methods=["POST"])
@admin_required
def admin_email_filter_rule_cleanup():
    """
    一键清理已入库的应剔除邮件

    小白讲解：剔除规则只对"新拉取的邮件"生效，已经躺在 pending_emails 表里的旧邮件不会自动消失。
    管理员新增或调整规则后，点这个按钮，系统会扫描 pending_emails 表，把命中剔除规则的旧邮件一次性删掉。
    """
    from email_service import _should_filter_email
    cursor = g.db.cursor()
    # 查所有未认领的待认领邮件
    cursor.execute("SELECT id, from_addr, from_name, subject, body_preview FROM pending_emails WHERE is_claimed = 0")
    rows = cursor.fetchall()

    deleted_ids = []
    for r in rows:
        should_filter, _ = _should_filter_email(
            r["from_addr"], r["from_name"], r["subject"], r.get("body_preview") or ""
        )
        if should_filter:
            deleted_ids.append(r["id"])

    if deleted_ids:
        placeholders = ",".join(["%s"] * len(deleted_ids))
        cursor.execute(f"DELETE FROM pending_emails WHERE id IN ({placeholders})", deleted_ids)
        g.db.commit()
        flash(f"已清理 {len(deleted_ids)} 封应被剔除的旧邮件", "success")
    else:
        flash("没有需要清理的邮件（待认领列表里没有命中剔除规则的邮件）", "info")
    return redirect(url_for("admin_models_email"))


@app.route("/admin/email-filter-rules/add", methods=["POST"])
@admin_required
def admin_email_filter_rule_add():
    """
    新增一条自定义剔除规则

    小白讲解：管理员填写规则名、检查字段、匹配方式、匹配值后提交。
    新增的规则默认是自定义规则（is_builtin=0），可以随时删除。
    """
    rule_name = request.form.get("rule_name", "").strip()
    field = request.form.get("field", "").strip()
    match_type = request.form.get("match_type", "").strip()
    match_value = request.form.get("match_value", "").strip()
    priority = request.form.get("priority", type=int, default=100)
    description = request.form.get("description", "").strip()

    # 参数校验
    if not rule_name or not field or not match_type or not match_value:
        flash("规则名称、检查字段、匹配方式、匹配值都不能为空", "danger")
        return redirect(url_for("admin_email_filter_rules"))

    # 字段白名单校验（防止SQL注入或写错字段名）
    if field not in ("from_addr", "from_name", "subject", "body"):
        flash("检查字段非法", "danger")
        return redirect(url_for("admin_email_filter_rules"))
    if match_type not in ("contains", "equals", "startswith", "regex"):
        flash("匹配方式非法", "danger")
        return redirect(url_for("admin_email_filter_rules"))

    # 优先级范围限制
    if priority < 1 or priority > 999:
        priority = 100

    from db import now_str
    cursor = g.db.cursor()
    cursor.execute("""
        INSERT INTO email_filter_rules
        (rule_name, field, match_type, match_value, action, is_enabled, priority, is_builtin, description, created_at, updated_at)
        VALUES (%s, %s, %s, %s, 'skip', 1, %s, 0, %s, %s, %s)
    """, (rule_name, field, match_type, match_value, priority, description, now_str(), now_str()))
    g.db.commit()
    flash(f"规则「{rule_name}」已添加", "success")
    return redirect(url_for("admin_email_filter_rules"))


@app.route("/admin/email-filter-rules/<int:rule_id>/toggle", methods=["POST"])
@admin_required
def admin_email_filter_rule_toggle(rule_id):
    """
    启用/禁用一条剔除规则（点一下切换状态）

    小白讲解：规则不删只禁用，方便以后再启用。内置规则被禁用后也不会丢，可以随时开回来。
    """
    from db import now_str
    cursor = g.db.cursor()
    # 先查当前状态，然后切换
    cursor.execute("SELECT is_enabled FROM email_filter_rules WHERE id = %s", (rule_id,))
    row = cursor.fetchone()
    if not row:
        flash("规则不存在", "danger")
        return redirect(url_for("admin_email_filter_rules"))

    new_status = 0 if row["is_enabled"] else 1
    cursor.execute(
        "UPDATE email_filter_rules SET is_enabled = %s, updated_at = %s WHERE id = %s",
        (new_status, now_str(), rule_id)
    )
    g.db.commit()
    action_text = "已启用" if new_status else "已禁用"
    flash(f"规则{action_text}", "success")
    return redirect(url_for("admin_email_filter_rules"))


@app.route("/admin/email-filter-rules/<int:rule_id>/delete", methods=["POST"])
@admin_required
def admin_email_filter_rule_delete(rule_id):
    """
    删除一条自定义剔除规则

    小白讲解：内置规则（is_builtin=1）不能删除，只能禁用，避免误删后无法恢复。
    自定义规则（is_builtin=0）可以删除。
    """
    cursor = g.db.cursor()
    # 先查是不是内置规则
    cursor.execute("SELECT is_builtin, rule_name FROM email_filter_rules WHERE id = %s", (rule_id,))
    row = cursor.fetchone()
    if not row:
        flash("规则不存在", "danger")
        return redirect(url_for("admin_email_filter_rules"))

    if row["is_builtin"]:
        flash("内置规则不能删除，请使用「禁用」功能", "warning")
        return redirect(url_for("admin_email_filter_rules"))

    cursor.execute("DELETE FROM email_filter_rules WHERE id = %s", (rule_id,))
    g.db.commit()
    flash(f"规则「{row['rule_name']}」已删除", "success")
    return redirect(url_for("admin_email_filter_rules"))


# ==================== 沟通管理模块（邮件管理 + 短信管理 + 待认领邮件）====================
# 小白讲解：原来"待认领邮件"是独立模块，现在整合到"沟通管理"下，与邮件管理、短信管理并列。
# 邮件管理是类微信的会话界面，左边联系人列表，右边会话框，可以查看往来邮件并直接回复。

@app.route("/communications")
@login_required
def communications_index():
    """沟通管理首页 - 重定向到邮件管理"""
    return redirect(url_for("communications_email"))


@app.route("/communications/email")
@login_required
def communications_email():
    """
    邮件管理页 - 类微信会话界面

    小白讲解：左边显示所有有过邮件沟通的供应商（按需求分组，可展开收起），
    右边显示选中供应商的邮件往来记录。可以在这里直接回复邮件。
    顶部有搜索框和新建联系人按钮。
    """
    cursor = g.db.cursor()
    uid_sql, uid_params = _uid_clause()

    # 查所有有过邮件沟通的供应商，按最后沟通时间降序
    # 小白讲解：关联 communications 表找有邮件记录的供应商，
    # 同时统计每个供应商的未读邮件数（is_read=0 且 direction=inbound）
    cursor.execute(f"""
        SELECT s.id, s.name, s.email, s.main_product, s.requirement_id,
               s.product_title,
               (SELECT MAX(comm_time) FROM communications
                WHERE supplier_id = s.id AND channel = '邮件') AS last_comm_time,
               (SELECT COUNT(*) FROM communications
                WHERE supplier_id = s.id AND channel = '邮件'
                  AND direction = 'inbound' AND is_read = 0) AS unread_count,
               (SELECT COUNT(*) FROM communications
                WHERE supplier_id = s.id AND channel = '邮件') AS total_count
        FROM suppliers s
        WHERE 1=1 {uid_sql}
          AND EXISTS (
              SELECT 1 FROM communications c
              WHERE c.supplier_id = s.id AND c.channel = '邮件'
          )
        ORDER BY last_comm_time DESC
    """, uid_params)
    contacts = cursor.fetchall()

    # 查每个联系人对应的需求名称和状态（用于左侧分组和归档）
    req_ids = [c["requirement_id"] for c in contacts if c["requirement_id"]]
    requirements_map = {}
    if req_ids:
        placeholders = ",".join(["%s"] * len(set(req_ids)))
        cursor.execute(f"""
            SELECT id, product_name, status FROM requirements WHERE id IN ({placeholders})
        """, list(set(req_ids)))
        requirements_map = {r["id"]: {"product_name": r["product_name"], "status": r["status"]} for r in cursor.fetchall()}

    # 给每个联系人加上需求名称、分组名，并区分活跃/归档
    active_contacts = []
    archived_contacts = []
    for c in contacts:
        req_info = requirements_map.get(c["requirement_id"], {})
        c["requirement_name"] = req_info.get("product_name", "")
        c["requirement_status"] = req_info.get("status", "")
        c["group_name"] = c["requirement_name"] or c["main_product"] or "未分类"
        # 需求状态为"已完成"的联系人进入归档区
        if c["requirement_status"] == "已完成":
            archived_contacts.append(c)
        else:
            active_contacts.append(c)

    return render_template("communications/email.html",
                           contacts=active_contacts,
                           archived_contacts=archived_contacts,
                           email_config=get_email_config())


@app.route("/communications/email/session/<int:supplier_id>")
@login_required
def communications_email_session(supplier_id):
    """
    获取某供应商的邮件会话数据（JSON API）

    小白讲解：前端用户点击左侧联系人后，用 JavaScript 调这个接口，
    拿到该供应商所有邮件往来记录，渲染到右侧会话框。
    同时把这个供应商的未读邮件标记为已读。
    """
    cursor = g.db.cursor()
    uid_sql, uid_params = _uid_clause()

    # 校验供应商归属
    cursor.execute(f"SELECT id, name, email FROM suppliers WHERE id = %s {uid_sql}",
                   (supplier_id, *uid_params))
    supplier = cursor.fetchone()
    if not supplier:
        return jsonify({"success": False, "message": "供应商不存在"}), 404

    # 查该供应商所有邮件记录，按时间正序（旧→新，方便会话展示）
    cursor.execute("""
        SELECT id, channel, content, subject, direction, status, is_read,
               comm_time, created_at, external_id
        FROM communications
        WHERE supplier_id = %s AND channel = '邮件'
        ORDER BY comm_time ASC, id ASC
    """, (supplier_id,))
    messages = cursor.fetchall()

    # 小白讲解：查出每条邮件的附件信息，组装成字典方便前端展示图片附件。
    # 前端渲染时，图片附件显示缩略图（点击放大），其他附件显示文件名。
    if messages:
        msg_ids = [m["id"] for m in messages]
        placeholders = ",".join(["%s"] * len(msg_ids))
        cursor.execute(f"""
            SELECT id, communication_id, original_filename, mime_type, file_size, is_image
            FROM communication_attachments
            WHERE communication_id IN ({placeholders})
        """, msg_ids)
        all_attachments = cursor.fetchall()
        # 按通信ID分组
        att_map = {}
        for att in all_attachments:
            att_map.setdefault(att["communication_id"], []).append(att)
        # 挂到每条邮件上
        for m in messages:
            m["attachments"] = att_map.get(m["id"], [])

    # 标记该供应商的未读邮件为已读
    cursor.execute("""
        UPDATE communications
        SET is_read = 1
        WHERE supplier_id = %s AND channel = '邮件' AND direction = 'inbound' AND is_read = 0
    """, (supplier_id,))
    g.db.commit()

    return jsonify({
        "success": True,
        "supplier": supplier,
        "messages": messages
    })


@app.route("/communications/email/send", methods=["POST"])
@login_required
def communications_email_send():
    """
    在邮件管理会话界面直接发送邮件（AJAX）

    小白讲解：用户在右侧会话框底部输入主题和正文，点发送，
    系统调用 Gmail SMTP 发送邮件，成功后写入 communications 表，
    前端不刷新页面，直接把新邮件追加到会话框。
    """
    cursor = g.db.cursor()
    uid_sql, uid_params = _uid_clause()

    # 小白讲解：会话框发送支持附件，所以改用 FormData（multipart/form-data）提交。
    # 前端用 FormData 把主题、正文、供应商ID、附件一起发过来。
    # 同时兼容旧的 JSON 提交方式（无附件时仍可用JSON）。
    if request.content_type and "application/json" in request.content_type:
        data = request.get_json() or {}
        supplier_id = data.get("supplier_id")
        subject = (data.get("subject") or "").strip()
        body = (data.get("body") or "").strip()
        attachments = []
    else:
        supplier_id = request.form.get("supplier_id")
        subject = (request.form.get("subject") or "").strip()
        body = (request.form.get("body") or "").strip()
        # 保存上传的附件
        attachments = _save_uploaded_attachments(request.files.getlist("attachments"))

    if not supplier_id or not subject or not body:
        _cleanup_attachments(attachments)
        return jsonify({"success": False, "message": "供应商、主题、正文都不能为空"})

    # 校验供应商
    cursor.execute(f"SELECT id, email, name FROM suppliers WHERE id = %s {uid_sql}",
                   (supplier_id, *uid_params))
    supplier = cursor.fetchone()
    if not supplier:
        _cleanup_attachments(attachments)
        return jsonify({"success": False, "message": "供应商不存在"})

    if not supplier["email"]:
        _cleanup_attachments(attachments)
        return jsonify({"success": False, "message": "该供应商没有邮箱地址"})

    # 调用 email_service 发送（带附件）
    # 小白讲解：send_email 返回3个值：成功标志、消息、沟通记录ID
    success, message, comm_id = send_email(
        to_addr=supplier["email"],
        subject=subject,
        body=body,
        supplier_id=supplier_id,
        user_id=g.user_id,
        attachments=attachments,
    )

    # 发送成功且有附件：把附件信息存数据库（图片附件可在会话中查看）
    if success and comm_id and attachments:
        _save_attachments_to_db(cursor, g.db, comm_id, attachments)

    # 发送完清理临时附件（图片附件保留，其他删除）
    _cleanup_attachments(attachments)

    return jsonify({"success": success, "message": message})


@app.route("/communications/attachment/<int:attachment_id>")
@login_required
def communications_attachment_view(attachment_id):
    """
    查看邮件附件（图片直接显示，其他文件下载）

    小白讲解：会话中图片附件要能查看，需要这个接口把 uploads 目录的文件返回给浏览器。
    前端 <img src="/communications/attachment/123"> 就能直接显示图片。
    非图片文件会触发下载。
    """
    cursor = g.db.cursor()
    cursor.execute("""
        SELECT id, original_filename, file_path, mime_type, is_image
        FROM communication_attachments WHERE id = %s
    """, (attachment_id,))
    att = cursor.fetchone()
    if not att:
        return "附件不存在", 404

    import os as _os
    if not att["file_path"] or not _os.path.exists(att["file_path"]):
        return "附件文件已删除", 404

    # 小白讲解：send_file 会读取文件并返回给浏览器，inline 表示在线显示（图片直接打开）
    from flask import send_file
    return send_file(
        att["file_path"],
        mimetype=att["mime_type"] or "application/octet-stream",
        as_attachment=not bool(att["is_image"]),  # 图片在线显示，其他文件下载
        download_name=att["original_filename"] or "attachment",
    )


@app.route("/communications/email/search")
@login_required
def communications_email_search():
    """
    搜索邮件（模糊匹配标题、供应商名称、正文内容）

    小白讲解：前端搜索框输入关键字后，实时调这个接口，
    返回匹配的邮件列表，前端高亮显示匹配的联系人。
    """
    cursor = g.db.cursor()
    uid_sql, uid_params = _uid_clause()
    q = request.args.get("q", "").strip()

    if not q:
        return jsonify({"success": True, "results": []})

    like_q = f"%{q}%"
    # 搜索 communications 的 subject/content，关联 suppliers 的 name
    cursor.execute(f"""
        SELECT DISTINCT c.supplier_id, s.name AS supplier_name, s.email,
               s.main_product, s.requirement_id, s.product_title
        FROM communications c
        JOIN suppliers s ON c.supplier_id = s.id
        WHERE 1=1 {uid_sql}
          AND c.channel = '邮件'
          AND (c.subject LIKE %s OR c.content LIKE %s OR s.name LIKE %s OR s.main_product LIKE %s)
        ORDER BY c.comm_time DESC
        LIMIT 50
    """, (*uid_params, like_q, like_q, like_q, like_q))
    results = cursor.fetchall()

    return jsonify({
        "success": True,
        "results": results
    })


@app.route("/communications/email/new", methods=["GET", "POST"])
@login_required
def communications_email_new():
    """
    新建联系人 + 群发邮件

    小白讲解：
    GET 不带参数：显示供应商选择弹窗页（未进行邮件沟通且不是"未通过初筛"的供应商）
    GET 带 ?ids=1,2,3：显示群发邮件表单（给选中的多个供应商发邮件）
    POST：发送群发邮件，给每个供应商都发一封，各自创建一条 communications 记录
    """
    cursor = g.db.cursor()
    uid_sql, uid_params = _uid_clause()

    if request.method == "POST":
        # POST：群发邮件
        supplier_ids = request.form.get("supplier_ids", "")
        subject = request.form.get("subject", "").strip()
        body = request.form.get("body", "").strip()

        if not supplier_ids or not subject or not body:
            flash("收件人、主题、正文都不能为空", "danger")
            return redirect(url_for("communications_email_new"))

        # 解析供应商ID列表
        try:
            id_list = [int(x) for x in supplier_ids.split(",") if x.strip()]
        except ValueError:
            flash("供应商ID格式错误", "danger")
            return redirect(url_for("communications_email_new"))

        if not id_list:
            flash("请至少选择一个供应商", "danger")
            return redirect(url_for("communications_email_new"))

        # 查这些供应商
        placeholders = ",".join(["%s"] * len(id_list))
        cursor.execute(f"""
            SELECT id, name, email FROM suppliers
            WHERE id IN ({placeholders}) {uid_sql}
        """, (*id_list, *uid_params))
        suppliers = cursor.fetchall()

        if not suppliers:
            flash("未找到有效的供应商", "danger")
            return redirect(url_for("communications_email_new"))

        # 小白讲解：保存群发邮件的附件，所有供应商共用同一份附件
        attachments = _save_uploaded_attachments(request.files.getlist("attachments"))

        # 逐个发送邮件（每个供应商都带同样的附件）
        success_count = 0
        fail_count = 0
        fail_list = []
        for s in suppliers:
            if not s["email"]:
                fail_count += 1
                fail_list.append(f"{s['name']}（无邮箱）")
                continue
            success, msg, _ = send_email(
                to_addr=s["email"],
                subject=subject,
                body=body,
                supplier_id=s["id"],
                user_id=g.user_id,
                attachments=attachments,
            )
            if success:
                success_count += 1
            else:
                fail_count += 1
                fail_list.append(f"{s['name']}（{msg}）")

        # 群发完成，清理临时附件文件
        _cleanup_attachments(attachments)

        if fail_count == 0:
            flash(f"群发成功，共发送 {success_count} 封邮件", "success")
        else:
            flash(f"成功 {success_count} 封，失败 {fail_count} 封：{'；'.join(fail_list)}", "warning")
        return redirect(url_for("communications_email"))

    # GET：判断是显示选择页还是群发表单
    ids_param = request.args.get("ids", "").strip()
    if ids_param:
        # 已选择供应商，显示群发邮件表单
        try:
            id_list = [int(x) for x in ids_param.split(",") if x.strip()]
        except ValueError:
            flash("供应商ID格式错误", "danger")
            return redirect(url_for("communications_email_new"))

        placeholders = ",".join(["%s"] * len(id_list))
        cursor.execute(f"""
            SELECT s.id, s.name, s.email, s.main_product, s.product_title,
                   (SELECT quality_score FROM screenings WHERE supplier_id = s.id ORDER BY id DESC LIMIT 1) AS score
            FROM suppliers s
            WHERE s.id IN ({placeholders}) {uid_sql}
            ORDER BY s.name
        """, (*id_list, *uid_params))
        selected_suppliers = cursor.fetchall()

        return render_template("communications/email_new.html",
                               selected_suppliers=selected_suppliers,
                               email_config=get_email_config(),
                               initial_body=get_inquiry_template())
    else:
        # 显示供应商选择页
        # 查未进行邮件沟通且不是"未通过初筛"的供应商
        # 小白讲解：LEFT JOIN requirements 表带出需求产品名称，用于前端按需求分组展示
        cursor.execute(f"""
            SELECT s.id, s.name, s.email, s.main_product, s.product_title,
                   s.requirement_id, s.dev_stage,
                   r.product_name AS requirement_name,
                   (SELECT quality_score FROM screenings WHERE supplier_id = s.id ORDER BY id DESC LIMIT 1) AS score
            FROM suppliers s
            LEFT JOIN requirements r ON s.requirement_id = r.id
            WHERE 1=1 {uid_sql}
              AND s.dev_stage != '未通过初筛'
              AND s.email != ''
              AND NOT EXISTS (
                  SELECT 1 FROM communications c
                  WHERE c.supplier_id = s.id AND c.channel = '邮件'
              )
            ORDER BY r.product_name ASC, s.name ASC
        """, uid_params)
        available_suppliers = cursor.fetchall()

        return render_template("communications/email_new.html",
                               available_suppliers=available_suppliers,
                               email_config=get_email_config())


@app.route("/communications/sms")
@login_required
def communications_sms():
    """短信管理（占位页，暂未开发）"""
    return render_template("communications/sms.html")


@app.route("/communications/pending")
@login_required
def communications_pending():
    """待认领邮件 - 重定向到原 /pending-emails 路由"""
    return redirect(url_for("pending_email_list"))


# ==================== 沟通模板管理（管理员）====================
# 小白讲解：管理员可以在这里新建、编辑、删除邮件模板，AI 生成邮件时可选用模板。

@app.route("/admin/models/templates")
@admin_required
def admin_templates_list():
    """沟通模板列表页"""
    cursor = g.db.cursor()
    cursor.execute("SELECT * FROM communication_templates ORDER BY id DESC")
    templates = cursor.fetchall()
    return render_template("admin/models/templates.html", templates=templates)


@app.route("/admin/models/templates/new", methods=["GET", "POST"])
@app.route("/admin/models/templates/<int:template_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_template_edit(template_id=None):
    """新建/编辑模板"""
    cursor = g.db.cursor()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        subject_template = request.form.get("subject_template", "").strip()
        body_template = request.form.get("body_template", "").strip()
        description = request.form.get("description", "").strip()
        scene = request.form.get("scene", "general").strip()
        is_enabled = 1 if request.form.get("is_enabled") else 0

        if not name:
            flash("模板名称不能为空", "danger")
            return redirect(request.url)

        if template_id:
            cursor.execute("""
                UPDATE communication_templates
                SET name=%s, subject_template=%s, body_template=%s,
                    description=%s, scene=%s, is_enabled=%s, updated_at=%s
                WHERE id=%s
            """, (name, subject_template, body_template, description, scene,
                  is_enabled, now_str(), template_id))
            flash("模板已更新", "success")
        else:
            cursor.execute("""
                INSERT INTO communication_templates
                (name, subject_template, body_template, description, scene,
                 is_enabled, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (name, subject_template, body_template, description, scene,
                  is_enabled, now_str(), now_str()))
            flash("模板已创建", "success")
        g.db.commit()
        return redirect(url_for("admin_templates_list"))

    # GET：显示表单
    template = None
    if template_id:
        cursor.execute("SELECT * FROM communication_templates WHERE id=%s", (template_id,))
        template = cursor.fetchone()
        if not template:
            flash("模板不存在", "danger")
            return redirect(url_for("admin_templates_list"))

    return render_template("admin/models/template_form.html", template=template)


@app.route("/admin/models/templates/<int:template_id>/delete", methods=["POST"])
@admin_required
def admin_template_delete(template_id):
    """删除模板"""
    cursor = g.db.cursor()
    cursor.execute("SELECT name FROM communication_templates WHERE id=%s", (template_id,))
    row = cursor.fetchone()
    if not row:
        flash("模板不存在", "danger")
        return redirect(url_for("admin_templates_list"))

    cursor.execute("DELETE FROM communication_templates WHERE id=%s", (template_id,))
    g.db.commit()
    flash(f"模板「{row['name']}」已删除", "success")
    return redirect(url_for("admin_templates_list"))


@app.route("/admin/models/templates/api/list")
@login_required
def admin_templates_api_list():
    """获取启用的模板列表（前端 AI 生成时下拉选择用）"""
    cursor = g.db.cursor()
    cursor.execute("""
        SELECT id, name, subject_template, body_template, scene, description
        FROM communication_templates
        WHERE is_enabled = 1
        ORDER BY scene, name
    """)
    return jsonify({"success": True, "templates": cursor.fetchall()})


# ==================== AI系统提示词配置 ====================
# 小白讲解：以下2个路由专门给"沟通模板管理页面"里的 AI 提示词编辑卡片用。
# GET 取出来填到文本框里，POST 把改完的内容保存回数据库。

@app.route("/admin/models/ai-prompt", methods=["GET"])
@login_required
def admin_ai_prompt_get():
    """获取所有AI提示词（前端打开页面时调用，把内容填到编辑框）"""
    cursor = g.db.cursor()
    cursor.execute("SELECT setting_key, setting_value, description, updated_at FROM ai_prompt_settings ORDER BY id")
    rows = cursor.fetchall()
    if not rows:
        return jsonify({"success": False, "message": "提示词配置不存在，请重启服务以初始化"})
    prompts = {r["setting_key"]: {"value": r["setting_value"], "description": r["description"], "updated_at": r["updated_at"]} for r in rows}
    return jsonify({"success": True, "prompts": prompts})


@app.route("/admin/models/ai-prompt", methods=["POST"])
@login_required
def admin_ai_prompt_save():
    """保存AI提示词（前端点"保存"按钮时调用，写入数据库立即生效）"""
    from datetime import datetime
    data = request.get_json()
    if not data or "key" not in data or "value" not in data:
        return jsonify({"success": False, "message": "缺少参数"}), 400
    key = data["key"]
    value = data["value"].strip()
    if not value:
        return jsonify({"success": False, "message": "提示词不能为空"}), 400
    cursor = g.db.cursor()
    cursor.execute("""
        UPDATE ai_prompt_settings SET setting_value = %s, updated_at = %s
        WHERE setting_key = %s
    """, (value, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), key))
    g.db.commit()
    return jsonify({"success": True})


# ==================== AI 生成邮件 ====================
# 小白讲解：以下 4 个路由处理 AI 生成邮件的请求，对应 3 个场景 + 1 个重新生成。
# 所有路由返回 JSON，前端用 fetch 调用，不刷新页面。

@app.route("/communications/email/ai-generate", methods=["POST"])
@login_required
def communications_ai_generate():
    """
    会话回复场景：AI 生成回复邮件

    小白讲解：用户在邮件管理会话框点"AI生成"时调用。
    读取该供应商的沟通记录，结合用户提示词生成回复。
    """
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "请求数据为空"}), 400

    supplier_id = data.get("supplier_id")
    user_prompt = data.get("user_prompt", "")
    prev_log_id = data.get("prev_log_id")
    languages = data.get("languages") or ["zh"]
    template_id = data.get("template_id")

    if not supplier_id:
        return jsonify({"success": False, "message": "缺少供应商ID"})

    from communication_ai import generate_session_reply
    success, result = generate_session_reply(supplier_id, user_prompt, prev_log_id, languages, template_id)
    return jsonify({"success": success, "message": result if not success else "",
                    "data": result if success else None})


@app.route("/communications/email/ai-generate-send", methods=["POST"])
@login_required
def communications_ai_generate_send():
    """
    群发/单发场景：AI 生成询价邮件

    小白讲解：用户在群发邮件或单发邮件界面点"AI生成"时调用。
    读取产品需求数据，结合选用的模板生成邮件。
    """
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "请求数据为空"}), 400

    supplier_ids = data.get("supplier_ids", [])
    user_prompt = data.get("user_prompt", "")
    scene = data.get("scene", "bulk_send")
    template_id = data.get("template_id")
    prev_log_id = data.get("prev_log_id")
    languages = data.get("languages") or ["zh"]

    if not supplier_ids:
        return jsonify({"success": False, "message": "请至少选择一个供应商"})

    from communication_ai import generate_bulk_or_single
    success, result = generate_bulk_or_single(
        supplier_ids=supplier_ids,
        user_prompt=user_prompt,
        scene=scene,
        template_id=template_id,
        prev_log_id=prev_log_id,
        languages=languages,
    )
    return jsonify({"success": success, "message": result if not success else "",
                    "data": result if success else None})


@app.route("/communications/email/ai-accept", methods=["POST"])
@login_required
def communications_ai_accept():
    """标记 AI 生成结果为已采纳"""
    data = request.get_json()
    if not data or not data.get("log_id"):
        return jsonify({"success": False, "message": "缺少 log_id"}), 400

    from communication_ai import accept_generation
    accept_generation(data["log_id"])
    return jsonify({"success": True})


@app.route("/communications/email/message/<int:msg_id>/delete", methods=["POST"])
@login_required
def communications_email_message_delete(msg_id):
    """
    删除单条邮件沟通记录

    小白讲解：用户在会话框中删除没发出去或无意义的邮件，
    删除后该记录不再显示，AI生成时也不会读取到。
    """
    cursor = g.db.cursor()
    uid_sql, uid_params = _uid_clause()

    # 校验该记录归属当前用户
    cursor.execute(f"""
        SELECT c.id, c.supplier_id FROM communications c
        JOIN suppliers s ON c.supplier_id = s.id
        WHERE c.id = %s {uid_sql}
    """, (msg_id, *uid_params))
    row = cursor.fetchone()
    if not row:
        return jsonify({"success": False, "message": "记录不存在或无权删除"}), 404

    cursor.execute("DELETE FROM communications WHERE id = %s", (msg_id,))
    g.db.commit()
    return jsonify({"success": True})


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

# ==================== 启动邮件接收后台线程 ====================
# 小白讲解：启动一个后台线程，每隔几分钟连一次Gmail拉未读邮件。
# 即使邮箱配置没开，线程也会跑（poll_inbox_once会直接返回），等管理员开启配置后自动开始收件。
try:
    email_service.start_polling()
except Exception as e:
    print(f"[启动] 邮件后台线程启动失败: {e}")

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
