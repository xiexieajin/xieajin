"""
供应商寻源系统 - Gmail 邮件服务模块

小白讲解：这个文件负责两件事：
1. 发邮件：用 Gmail 的 SMTP 服务器把邮件发给供应商
2. 收邮件：开一个后台线程，每隔几分钟连一次 Gmail 的 IMAP 服务器，
           把供应商回复的未读邮件拉下来，自动存进数据库

为什么用 Gmail？
- 完全免费（500封/天，个人用足够）
- SMTP 发送 + IMAP 接收都是 Gmail 官方支持的协议
- 只需要 Gmail 账号 + 16位应用专用密码（不是登录密码）

发送流程：
  用户点"发邮件" → send_email() → Gmail SMTP → 供应商收到 → 自动写 communications 表

接收流程：
  供应商回复 → 到你的 Gmail 收件箱 → 后台线程 IMAP 拉取 → 匹配供应商邮箱
  → 匹配到：写 communications 表（direction=inbound）
  → 匹配不到：写 pending_emails 表（等用户手动认领）
"""

import smtplib
import imaplib
import email
import email.utils
import threading
import time
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header

import pymysql

# 从项目配置读取数据库连接参数
from config import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE


# ==================== 全局变量 ====================
# 小白讲解：后台轮询线程对象，启动后一直跑，直到程序退出才停。
# 用全局变量是为了避免线程被垃圾回收（Python的线程如果是局部变量，函数结束后可能被回收）
_poll_thread = None
_poll_stop_event = threading.Event()  # 停止信号，设置后线程会退出循环


# ==================== 数据库辅助函数 ====================
def _get_db_connection():
    """
    建立一个新的数据库连接（每个函数用完自己关闭）

    小白讲解：后台线程不能复用 Flask 的 g.db（那个是请求级别的，请求结束就关闭了）。
    后台线程需要自己开连接、自己关闭，避免连接泄漏。
    """
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE,
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def get_email_config():
    """
    从数据库读取邮箱配置（Gmail账号、密码、发件人名称等）

    小白讲解：邮箱配置存在 email_config 表，只有一条记录（id=1）。
    管理员在"邮箱配置"页面填写后存入，这里读出来给发送和接收函数用。
    没配置过则返回 None，调用方据此判断是否启用邮件功能。

    返回：字典格式配置，或 None（未配置）
    """
    conn = _get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM email_config WHERE id = 1")
        row = cursor.fetchone()
        return row
    finally:
        conn.close()


# ==================== 邮件正文格式化 ====================
def _text_to_html(body):
    """
    把用户在 textarea 里输入的纯文本转成美观的 HTML 邮件内容

    小白讲解：用户在输入框里敲回车换行，但 HTML 邮件里 \\n 不会显示成换行，
    所以供应商收到的邮件会堆成一坨。本函数做三件事：
    1. 先把 < > & 等特殊字符转义，防止内容被当成 HTML 标签
    2. 把换行 \\n 转成 <br>，把连续空行转成段落间距
    3. 用一个带样式的 div 包起来，设置字体、行高、留白，让邮件看着舒服

    参数：body - 用户输入的纯文本正文
    返回：可在邮件里直接用的 HTML 字符串
    """
    if not body:
        return ""
    # 第1步：转义特殊字符，避免用户输入的内容被当成 HTML 执行
    import html as _html
    safe = _html.escape(body)
    # 第2步：换行转 <br>，连续两个换行（空行）转成段落分隔
    # 小白讲解：先按空行分段，每段内部的换行转 <br>
    paragraphs = safe.split("\n\n")
    html_parts = []
    for p in paragraphs:
        # 单段内的换行转 <br>
        p = p.replace("\n", "<br>")
        html_parts.append(f'<p style="margin:0 0 12px 0;line-height:1.7;">{p}</p>')
    body_html = "".join(html_parts)
    # 第3步：用带样式的容器包起来，字体、颜色、留白都设好
    return f"""<div style="font-family:'Microsoft YaHei','Segoe UI',Arial,sans-serif;font-size:14px;color:#333;max-width:680px;padding:16px;background:#ffffff;">
{body_html}
</div>"""


def _has_html_tag(text):
    """快速判断文本是否已经包含 HTML 标签（避免对已有 HTML 重复转换）"""
    if not text:
        return False
    import re as _re
    return bool(_re.search(r'<(p|div|br|span|table|ul|ol|h[1-6])\b', text, _re.IGNORECASE))


# ==================== 邮件发送功能 ====================
def send_email(to_addr, subject, body, supplier_id=None, user_id=1, attachments=None):
    """
    通过 Gmail SMTP 发送邮件给供应商（支持附件）

    小白讲解：这是发邮件的核心函数。流程：
    1. 从数据库读 Gmail 配置（账号+密码）
    2. 把正文转成美观的 HTML（换行正确显示），如果有附件就附加
    3. 连接 Gmail 的 SMTP 服务器（smtp.gmail.com 端口465，SSL加密）
    4. 登录并发送邮件
    5. 发送成功后，自动在 communications 表写一条记录（direction=outbound）
    6. 失败也写一条记录（status=failed），方便用户知道发送失败

    参数：
        to_addr: 收件人邮箱（供应商邮箱）
        subject: 邮件主题
        body: 邮件正文（纯文本或HTML，会自动格式化）
        supplier_id: 关联的供应商ID（用于写沟通记录，可为空）
        user_id: 操作用户ID（用于数据隔离）
        attachments: 附件信息字典列表（含 file_path/original_filename/mime_type/is_image）
                     也兼容旧的"路径字符串列表"格式

    返回：(success: bool, message: str, communication_id: int or None)
        success=True 表示发送成功，message是成功信息，communication_id是沟通记录ID
        success=False 表示发送失败，message是错误原因，communication_id=None
    """
    from db import now_str

    # 1. 读取邮箱配置
    config = get_email_config()
    if not config or not config.get("is_enabled"):
        return False, "邮件功能未启用，请先在「邮箱配置」页面开启", None
    if not config.get("gmail_address") or not config.get("app_password"):
        return False, "Gmail账号或应用专用密码未配置", None
    if not to_addr or "@" not in to_addr:
        return False, f"收件人邮箱无效：{to_addr}", None

    gmail_addr = config["gmail_address"]
    app_password = config["app_password"]
    sender_name = config.get("sender_name") or ""

    # 2. 构造邮件内容
    # 小白讲解：邮件结构分两种情况：
    #   - 无附件：用 MIMEMultipart("alternative")，挂纯文本+HTML两版，客户端自选
    #   - 有附件：顶层用 MIMEMultipart("mixed")，下面挂 alternative（文本+HTML）+ 各个附件
    # 这样无论有没有附件，邮件正文都能正确显示，附件也能正常发送
    plain_body = re.sub(r'<[^>]+>', '', body)  # 纯文本版本（给老客户端用）
    # 小白讲解：如果用户输入的是纯文本（没有HTML标签），就转成美观的HTML；
    # 如果已经包含HTML标签（比如从模板加载的），就直接用，避免重复转换
    if _has_html_tag(body):
        html_body = body
    else:
        html_body = _text_to_html(body)

    # 构造 alternative 部分（纯文本 + HTML，客户端二选一显示）
    alt_part = MIMEMultipart("alternative")
    alt_part.attach(MIMEText(plain_body, "plain", "utf-8"))
    alt_part.attach(MIMEText(html_body, "html", "utf-8"))

    # 顶层容器：有附件用 mixed，无附件直接用 alternative
    # 小白讲解：attachments 既支持新格式（字典列表），也兼容旧格式（字符串路径列表）
    # 统一转成"文件路径 + 文件名"的列表，方便后面构造附件
    attachment_files = []
    if attachments:
        for item in attachments:
            if isinstance(item, dict):
                file_path = item.get("file_path")
                original_filename = item.get("original_filename") or "attachment"
            else:
                file_path = item
                original_filename = "attachment"
            if file_path:
                attachment_files.append((file_path, original_filename))

    if attachment_files:
        msg = MIMEMultipart("mixed")
        msg.attach(alt_part)
        # 小白讲解：遍历附件列表，每个文件读进来用 base64 编码后挂到邮件上
        from email.mime.base import MIMEBase
        from email import encoders
        import os as _os
        for file_path, original_filename in attachment_files:
            if not file_path or not _os.path.exists(file_path):
                continue  # 跳过不存在的文件
            with open(file_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            # base64 编码，让二进制文件能通过邮件传输
            encoders.encode_base64(part)
            # 设置附件文件名（处理中文文件名，用原始文件名而非带时间戳的）
            from email.header import Header
            part.add_header(
                "Content-Disposition",
                "attachment",
                filename=("utf-8", "", original_filename)
            )
            msg.attach(part)
    else:
        msg = alt_part

    # 发件人格式："显示名称 <邮箱地址>"，让收件人看到发件人名称
    if sender_name:
        msg["From"] = email.utils.formataddr((sender_name, gmail_addr))
    else:
        msg["From"] = gmail_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    # Message-ID：每封邮件唯一标识，收件人回复时会带上，用于邮件线程关联
    msg["Message-ID"] = email.utils.make_msgid(domain=gmail_addr.split("@")[-1])

    # 3. 连接Gmail SMTP并发送（带重试机制）
    external_id = msg["Message-ID"]  # 保存Message-ID用于后续关联回复
    import time as _time

    # 重试参数
    # 小白讲解：smtplib 的 timeout 对连接、SSL握手、登录、发送数据都生效。
    # 带附件的邮件数据量大，发送传输需要更多时间，20秒太短会导致带附件邮件超时。
    # 改回 60 秒超时（覆盖大附件传输），2 次重试（比原来 3 次少，最坏 120 秒）。
    MAX_RETRIES = 2          # 最多重试2次
    RETRY_INTERVAL = 3       # 每次重试间隔3秒
    SMTP_TIMEOUT = 60        # 单次超时60秒（覆盖大附件传输 + 慢网络SSL握手）

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # 小白讲解：SMTP_SSL 是加密连接，比普通的 SMTP 更安全（类似 https vs http）
            # Gmail SMTP 服务器地址 smtp.gmail.com，SSL端口 465
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=SMTP_TIMEOUT) as server:
                server.login(gmail_addr, app_password)
                server.sendmail(gmail_addr, [to_addr], msg.as_string())

            # 发送成功，写沟通记录
            comm_id = _save_communication(
                supplier_id=supplier_id,
                channel="邮件",
                direction="outbound",
                subject=subject,
                content=plain_body[:1000],  # 沟通记录只存前1000字
                conclusion="已发送",
                external_id=external_id,
                status="sent",
                user_id=user_id,
            )
            return True, f"邮件已发送至 {to_addr}", comm_id

        except smtplib.SMTPAuthenticationError:
            # Gmail认证失败：通常是应用专用密码错误，或账号没开两步验证
            # 这种错误重试也没用，直接返回
            error_msg = "Gmail认证失败：请检查应用专用密码是否正确，或账号是否已开启两步验证"
            _save_communication(
                supplier_id=supplier_id,
                channel="邮件",
                direction="outbound",
                subject=subject,
                content=plain_body[:1000],
                conclusion="发送失败",
                external_id=external_id,
                status="failed",
                user_id=user_id,
            )
            return False, error_msg, None

        except (TimeoutError, OSError, Exception) as e:
            # 网络超时、SSL握手失败等可重试的错误
            last_error = e
            error_str = str(e)
            # 如果是认证类错误，不重试
            if "Authentication" in error_str or "auth" in error_str.lower():
                break
            # 还有重试机会，等5秒再试
            if attempt < MAX_RETRIES:
                _time.sleep(RETRY_INTERVAL)
                continue

    # 所有重试都失败，写失败记录
    # 小白讲解：如果是 SSL 握手超时，给用户更友好的提示
    error_str = str(last_error) if last_error else "未知错误"
    if "handshake" in error_str.lower() or "timeout" in error_str.lower():
        error_msg = f"邮件发送失败（网络超时）：连接Gmail服务器超时，请检查网络/VPN后重试"
    else:
        error_msg = f"发送失败：{error_str}"
    _save_communication(
        supplier_id=supplier_id,
        channel="邮件",
        direction="outbound",
        subject=subject,
        content=plain_body[:1000],
        conclusion="发送失败",
        external_id=external_id,
        status="failed",
        user_id=user_id,
    )
    return False, error_msg, None


def _save_communication(supplier_id, channel, direction, subject, content,
                         conclusion, external_id, status, user_id):
    """
    把一条邮件记录写入 communications 表

    小白讲解：不管是发出的邮件还是收到的邮件，都统一存 communications 表。
    用 direction 字段区分：outbound=发出的，inbound=收到的。
    external_id 存 Message-ID，用于去重（防止同一封回复邮件被重复入库）。

    返回：插入的 communication_id，去重跳过时返回已存在记录的ID
    """
    from db import now_str, mark_supplier_communicating
    conn = _get_db_connection()
    try:
        cursor = conn.cursor()
        # 检查是否已存在相同 external_id 的记录（去重）
        if external_id:
            cursor.execute(
                "SELECT id FROM communications WHERE external_id = %s LIMIT 1",
                (external_id,)
            )
            existing = cursor.fetchone()
            if existing:
                return existing["id"]  # 已存在，跳过（去重）

        cursor.execute("""
            INSERT INTO communications
            (supplier_id, channel, content, conclusion, next_step, comm_time,
             created_at, updated_at, user_id, direction, subject, external_id, status, is_read)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            supplier_id, channel, content, conclusion, "", now_str(),
            now_str(), now_str(), user_id, direction, subject, external_id, status,
            # 小白讲解：发出的邮件默认已读（is_read=1），收到的邮件默认未读（is_read=0）
            1 if direction == "outbound" else 0
        ))
        if supplier_id:
            mark_supplier_communicating(cursor, supplier_id)
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


# ==================== 邮件接收功能（IMAP轮询）====================
def _should_filter_email(from_addr, from_name, subject, body):
    """
    检查一封邮件是否应该被剔除（不进系统）

    小白讲解：Gmail 收件箱里会混进 Google 安全提醒、验证码、noreply 系统通知等"非供应商回复"邮件。
    本函数从 email_filter_rules 表读取所有启用的剔除规则，按优先级排序后逐条匹配。
    命中任意一条规则就返回 (True, 命中的规则名)，调用方据此跳过入库。
    都不命中返回 (False, None)，邮件正常进入待认领或沟通记录。

    支持的检查字段：
        from_addr  - 发件人邮箱地址
        from_name  - 发件人显示名称
        subject    - 邮件主题
        body       - 邮件正文（前500字）

    支持的匹配方式：
        contains   - 包含（大小写不敏感，最常用）
        equals     - 完全等于（大小写不敏感）
        startswith - 以...开头（大小写不敏感）
        regex      - 正则表达式（大小写敏感，高级用法）

    参数：
        from_addr: 发件人邮箱
        from_name: 发件人名称
        subject: 邮件主题
        body: 邮件正文

    返回：(should_filter: bool, matched_rule_name: str or None)
    """
    # 把各字段拼成字典方便查
    fields = {
        "from_addr": (from_addr or "").lower(),
        "from_name": (from_name or "").lower(),
        "subject":   (subject or "").lower(),
        "body":      (body or "")[:500].lower(),  # 正文只查前500字，提高性能
    }

    conn = _get_db_connection()
    try:
        cursor = conn.cursor()
        # 查所有启用的规则，按 priority 升序（数字小先匹配）
        cursor.execute("""
            SELECT rule_name, field, match_type, match_value
            FROM email_filter_rules
            WHERE is_enabled = 1
            ORDER BY priority ASC, id ASC
        """)
        rules = cursor.fetchall()
    finally:
        conn.close()

    # 逐条规则匹配，命中即返回
    for rule in rules:
        field_key = rule["field"]
        match_type = rule["match_type"]
        match_value = rule["match_value"] or ""

        # 取出要检查的字段值（已经在上面转成小写了，方便 contains/equals/startswith 比较）
        target = fields.get(field_key, "")
        if not target and match_type != "equals":
            # 字段为空时跳过（除非是 equals 匹配空值，那种场景极少，这里也跳过）
            continue

        try:
            if match_type == "contains":
                # 包含匹配（大小写不敏感，target 和 match_value 都已小写）
                if match_value.lower() in target:
                    return True, rule["rule_name"]
            elif match_type == "equals":
                # 完全等于匹配（大小写不敏感）
                if target == match_value.lower():
                    return True, rule["rule_name"]
            elif match_type == "startswith":
                # 以...开头匹配（大小写不敏感）
                if target.startswith(match_value.lower()):
                    return True, rule["rule_name"]
            elif match_type == "regex":
                # 正则匹配（大小写敏感，因为正则可能需要精确控制大小写）
                # 小白讲解：正则比 contains 更强大，比如可以用 ^noreply@.*\\.com$ 精确匹配
                import re as _re
                if _re.search(match_value, fields.get(field_key, "")):
                    return True, rule["rule_name"]
        except Exception as e:
            # 单条规则匹配出错不影响其他规则（比如正则写错了）
            print(f"[email_service] 规则匹配异常（规则：{rule['rule_name']}）：{e}")
            continue

    return False, None


def poll_inbox_once():
    """
    执行一次IMAP收件：连接Gmail，拉取未读邮件，解析并入库

    小白讲解：这个函数每次被后台线程调用一次，流程：
    1. 连接 Gmail IMAP 服务器（imap.gmail.com 端口993）
    2. 搜索所有未读邮件（UNSEEN）
    3. 逐封解析：发件人、主题、正文、Message-ID
    4. 按发件人邮箱匹配 suppliers 表的 email 字段
       - 匹配到：写 communications 表（direction=inbound）
       - 匹配不到：写 pending_emails 表（待用户认领）
    5. 把邮件标记为已读（避免下次重复拉取）
    6. 更新 email_config.last_poll_time（显示最近一次轮询时间）

    返回：本次拉取的新邮件数量
    """
    from db import now_str

    config = get_email_config()
    if not config or not config.get("is_enabled"):
        return 0
    if not config.get("gmail_address") or not config.get("app_password"):
        return 0

    gmail_addr = config["gmail_address"]
    app_password = config["app_password"]
    new_count = 0

    try:
        # 小白讲解：IMAP4_SSL 是加密的IMAP连接，Gmail要求加密。
        # imap.gmail.com 端口 993 是Gmail官方IMAP地址。
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(gmail_addr, app_password)
        mail.select("inbox")  # 选择收件箱

        # 搜索未读邮件
        # 小白讲解：UNSEEN 是IMAP的关键字，表示"未读"。返回的是邮件ID列表。
        status, data = mail.search(None, "UNSEEN")
        if status != "OK":
            mail.logout()
            return 0

        mail_ids = data[0].split()
        if not mail_ids:
            mail.logout()
            # 更新轮询时间
            _update_last_poll_time()
            return 0

        # 逐封处理未读邮件
        for mail_id in mail_ids:
            try:
                # 获取邮件完整内容
                status, msg_data = mail.fetch(mail_id, "(RFC822)")
                if status != "OK":
                    continue

                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                # 解析邮件各字段
                from_addr, from_name = _parse_email_address(msg.get("From", ""))
                subject = _decode_email_header(msg.get("Subject", ""))
                message_id = msg.get("Message-ID", "")
                received_time = msg.get("Date", "")
                body = _extract_email_body(msg)

                # 去重：检查这封邮件是否已经入库（按Message-ID）
                if _is_email_already_saved(message_id):
                    continue

                # 剔除检查：命中剔除规则的邮件直接跳过（标记已读，不进系统）
                # 小白讲解：Google 安全提醒、验证码、noreply 通知等"非供应商回复"邮件在这里被过滤掉。
                # 命中规则后直接标记已读并 continue，邮件不会进 communications 或 pending_emails 表。
                should_filter, matched_rule = _should_filter_email(from_addr, from_name, subject, body)
                if should_filter:
                    print(f"[email_service] 邮件被剔除（命中规则：{matched_rule}）- 发件人：{from_addr}，主题：{subject[:50]}")
                    # 标记为已读，避免下次重复拉取
                    mail.store(mail_id, "+FLAGS", "\\Seen")
                    continue

                # 按发件人邮箱匹配供应商
                supplier = _match_supplier_by_email(from_addr)

                if supplier:
                    # 匹配到供应商：写 communications 表
                    _save_communication(
                        supplier_id=supplier["id"],
                        channel="邮件",
                        direction="inbound",
                        subject=subject,
                        content=body[:1000],
                        conclusion="收到回复",
                        external_id=message_id,
                        status="received",
                        user_id=supplier.get("user_id", 1),
                    )
                else:
                    # 匹配不到供应商：写 pending_emails 表，等用户认领
                    _save_pending_email(
                        from_addr=from_addr,
                        from_name=from_name,
                        subject=subject,
                        body_preview=body[:500],
                        external_id=message_id,
                        received_time=received_time,
                    )

                new_count += 1

                # 把邮件标记为已读（避免下次重复处理）
                # 小白讲解：\Seen 是IMAP标记，表示"已读"。设置后这封邮件不再是UNSEEN。
                mail.store(mail_id, "+FLAGS", "\\Seen")

            except Exception as e:
                # 单封邮件解析失败不影响其他邮件，记录错误继续处理下一封
                print(f"[email_service] 邮件解析失败：{e}")
                continue

        mail.logout()

    except Exception as e:
        print(f"[email_service] IMAP轮询失败：{e}")
        return new_count

    # 更新轮询时间
    _update_last_poll_time()
    return new_count


def _parse_email_address(from_header):
    """
    解析邮件的 From 头，拆出邮箱地址和显示名称

    小白讲解：邮件的 From 头有两种格式：
    1. "张三 <zhangsan@example.com>" → 名称=张三，邮箱=zhangsan@example.com
    2. "zhangsan@example.com" → 名称=空，邮箱=zhangsan@example.com
    本函数统一拆成 (邮箱地址, 显示名称) 返回。
    """
    if not from_header:
        return "", ""
    try:
        name, addr = email.utils.parseaddr(from_header)
        # 解码名称（可能带编码，如 =?utf-8?B?xxx?=）
        if name:
            name = _decode_email_header(name)
        return addr.lower().strip(), name
    except Exception:
        return from_header.lower().strip(), ""


def _decode_email_header(header_value):
    """
    解码邮件头（主题、发件人名等可能带编码）

    小白讲解：中文邮件的主题和发件人名通常编码过，比如：
    =?utf-8?B?5p2l5b6X6aqM?= 表示"测试邮件"
    这个函数把它还原成正常中文。
    """
    if not header_value:
        return ""
    try:
        parts = decode_header(header_value)
        result = []
        for data, charset in parts:
            if isinstance(data, bytes):
                result.append(data.decode(charset or "utf-8", errors="replace"))
            else:
                result.append(data)
        return "".join(result)
    except Exception:
        return header_value


def _extract_email_body(msg):
    """
    从邮件对象中提取正文（优先取纯文本，没有再取HTML）

    小白讲解：一封邮件可能包含多个部分（multipart）：
    - text/plain：纯文本正文
    - text/html：HTML格式正文
    我们优先用纯文本（更干净），没有再用HTML（要去掉标签）。
    """
    body = ""
    if msg.is_multipart():
        # 多部分邮件：遍历各部分，找第一个text/plain
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            # 跳过附件
            if "attachment" in content_disposition:
                continue
            if content_type == "text/plain":
                try:
                    charset = part.get_content_charset() or "utf-8"
                    body = part.get_payload(decode=True).decode(charset, errors="replace")
                    break
                except Exception:
                    continue
        # 如果没找到纯文本，用HTML
        if not body:
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    try:
                        charset = part.get_content_charset() or "utf-8"
                        body = part.get_payload(decode=True).decode(charset, errors="replace")
                        # 简单去掉HTML标签
                        body = re.sub(r'<[^>]+>', '', body)
                        break
                    except Exception:
                        continue
    else:
        # 单部分邮件：直接取
        try:
            charset = msg.get_content_charset() or "utf-8"
            body = msg.get_payload(decode=True).decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                body = re.sub(r'<[^>]+>', '', body)
        except Exception:
            body = ""
    return body.strip()


def _match_supplier_by_email(from_addr):
    """
    按发件人邮箱匹配 suppliers 表的供应商

    小白讲解：收到供应商回复邮件后，要根据发件人邮箱找到是哪个供应商发的。
    匹配规则：邮箱地址忽略大小写精确匹配 suppliers.email 字段。
    匹配到返回供应商记录（字典），匹配不到返回 None。

    参数：from_addr 发件人邮箱地址
    返回：供应商记录字典，或 None
    """
    if not from_addr:
        return None
    conn = _get_db_connection()
    try:
        cursor = conn.cursor()
        # 小白讲解：LOWER() 把邮箱转成小写再比较，避免大小写不一致导致匹配失败
        cursor.execute(
            "SELECT * FROM suppliers WHERE LOWER(email) = LOWER(%s) LIMIT 1",
            (from_addr,)
        )
        return cursor.fetchone()
    finally:
        conn.close()


def _is_email_already_saved(external_id):
    """
    检查某封邮件是否已经入库（按Message-ID去重）

    小白讲解：IMAP有时会重复返回同一封邮件（比如标记已读失败），用Message-ID
    去重可以避免同一封回复被写入数据库多次。
    """
    if not external_id:
        return False
    conn = _get_db_connection()
    try:
        cursor = conn.cursor()
        # 同时检查 communications 和 pending_emails 两张表
        cursor.execute(
            "SELECT id FROM communications WHERE external_id = %s LIMIT 1",
            (external_id,)
        )
        if cursor.fetchone():
            return True
        cursor.execute(
            "SELECT id FROM pending_emails WHERE external_id = %s LIMIT 1",
            (external_id,)
        )
        if cursor.fetchone():
            return True
        return False
    finally:
        conn.close()


def _save_pending_email(from_addr, from_name, subject, body_preview,
                         external_id, received_time):
    """
    把匹配不到供应商的邮件存入 pending_emails 表

    小白讲解：供应商换了邮箱回复，或新供应商主动发邮件来，系统匹配不到，
    先存这张表。用户在"待认领邮件"页面看到后，手动选择关联到哪个供应商。
    """
    from db import now_str
    conn = _get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO pending_emails
            (from_addr, from_name, subject, body_preview, external_id,
             received_time, user_id, is_claimed, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, 1, 0, %s, %s)
        """, (from_addr, from_name, subject, body_preview, external_id,
              received_time, now_str(), now_str()))
        conn.commit()
    finally:
        conn.close()


def _update_last_poll_time():
    """更新 email_config 表的 last_poll_time 字段（显示最近轮询时间）"""
    from db import now_str
    conn = _get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE email_config SET last_poll_time = %s, updated_at = %s WHERE id = 1",
            (now_str(), now_str())
        )
        conn.commit()
    finally:
        conn.close()


# ==================== 后台轮询线程 ====================
def _poll_loop():
    """
    后台轮询主循环（由 start_polling() 启动，在独立线程运行）

    小白讲解：这个函数是个死循环，每隔 N 秒（从数据库读 poll_interval）执行一次
    poll_inbox_once()。用 _poll_stop_event 控制退出：主程序退出时设置这个事件，
    线程下一轮循环检测到就退出。
    """
    print("[email_service] 邮件接收后台线程已启动")
    while not _poll_stop_event.is_set():
        try:
            config = get_email_config()
            if config and config.get("is_enabled"):
                poll_inbox_once()
        except Exception as e:
            print(f"[email_service] 轮询异常：{e}")

        # 从数据库读轮询间隔（管理员可改，默认300秒=5分钟）
        try:
            config = get_email_config()
            interval = config.get("poll_interval", 300) if config else 300
        except Exception:
            interval = 300

        # 小白讲解：用 wait() 而不是 sleep()，这样设置停止信号时能立即响应退出
        # 如果用 time.sleep()，必须等够时间才能检测到停止信号
        _poll_stop_event.wait(timeout=interval)

    print("[email_service] 邮件接收后台线程已停止")


def start_polling():
    """
    启动邮件接收后台线程（在 app.py 启动时调用一次）

    小白讲解：Flask 启动时调用这个函数，开启一个后台线程专门收邮件。
    线程会一直跑，每隔几分钟连一次Gmail拉未读邮件。
    即便邮箱配置没开，线程也会跑，但 poll_inbox_once() 会直接返回不做事，
    等管理员开启配置后自动开始收件。
    """
    global _poll_thread
    if _poll_thread and _poll_thread.is_alive():
        return  # 线程已在运行，不重复启动

    _poll_stop_event.clear()
    _poll_thread = threading.Thread(target=_poll_loop, daemon=True, name="email-poller")
    # 小白讲解：daemon=True 表示这是守护线程，主程序退出时自动跟着退出，不阻塞
    _poll_thread.start()


def stop_polling():
    """停止邮件接收后台线程（程序退出时调用）"""
    _poll_stop_event.set()
    if _poll_thread:
        _poll_thread.join(timeout=5)  # 最多等5秒让它退出


# ==================== 待认领邮件相关 ====================
def claim_pending_email(pending_id, supplier_id, user_id):
    """
    把一封待认领邮件关联到指定供应商（用户手动认领）

    小白讲解：用户在"待认领邮件"页面看到一封邮件，认出是某个供应商的回复，
    点击"认领"按钮选择供应商后，系统把这封邮件从 pending_emails 表转移到
    communications 表，并删除 pending_emails 记录。

    参数：
        pending_id: pending_emails 表的记录ID
        supplier_id: 用户选择的供应商ID
        user_id: 操作用户ID

    返回：(success, message)
    """
    from db import now_str, mark_supplier_communicating
    conn = _get_db_connection()
    try:
        cursor = conn.cursor()
        # 1. 查出待认领邮件内容
        cursor.execute("SELECT * FROM pending_emails WHERE id = %s AND is_claimed = 0", (pending_id,))
        pending = cursor.fetchone()
        if not pending:
            return False, "待认领邮件不存在或已被认领"

        # 2. 检查是否已存在相同 external_id 的沟通记录（去重）
        cursor.execute(
            "SELECT id FROM communications WHERE external_id = %s LIMIT 1",
            (pending["external_id"],)
        )
        if cursor.fetchone():
            # 已存在，直接标记认领，不重复插入
            cursor.execute(
                "UPDATE pending_emails SET is_claimed = 1, claimed_supplier_id = %s, updated_at = %s WHERE id = %s",
                (supplier_id, now_str(), pending_id)
            )
            conn.commit()
            return True, "该邮件已存在沟通记录，已标记认领"

        # 3. 写入 communications 表
        cursor.execute("""
            INSERT INTO communications
            (supplier_id, channel, content, conclusion, next_step, comm_time,
             created_at, updated_at, user_id, direction, subject, external_id, status, is_read)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            supplier_id, "邮件", pending["body_preview"] or "", "收到回复（手动认领）",
            "", pending["received_time"] or now_str(),
            now_str(), now_str(), user_id, "inbound",
            pending["subject"] or "", pending["external_id"] or "", "received",
            0  # 小白讲解：手动认领的邮件默认未读，用户在邮件管理页点击后才会标记已读
        ))
        mark_supplier_communicating(cursor, supplier_id)

        # 4. 标记 pending_emails 为已认领
        cursor.execute(
            "UPDATE pending_emails SET is_claimed = 1, claimed_supplier_id = %s, updated_at = %s WHERE id = %s",
            (supplier_id, now_str(), pending_id)
        )

        conn.commit()
        return True, "认领成功，已关联到供应商"

    finally:
        conn.close()


# ==================== 邮件模板功能 ====================
def get_inquiry_template():
    """
    返回默认的询价邮件HTML模板

    小白讲解：用户给供应商发询价邮件时，可以一键加载这个模板，省得每次手写。
    模板里包含产品需求、报价要求等标准内容，用户只需填入具体产品信息。
    """
    return """\
<div style="font-family: 'Microsoft YaHei', Arial, sans-serif; line-height: 1.8; color: #333;">
    <p>您好！</p>
    <p>我司是一家专注于亚马逊、Temu 等跨境电商平台的电商企业，目前正在开发【产品名称】品类的优质供应商，
    在了解贵司后，认为与我们的需求较为匹配，特发此邮件进行初步洽谈。</p>

    <p><strong>产品需求：</strong>（填写产品需求）</p>

    <p>请提供以下报价信息：</p>
    <ol>
        <li>产品单价（请提供阶梯报价）</li>
        <li>最小下单量（MOQ）</li>
        <li>最小发货量</li>
        <li>首批建议下单数量</li>
        <li>生产交期（付款后至出货天数）</li>
    </ol>

    <p>另请附上：产品图片或规格书、现有认证资质（如有）</p>

    <p>如贵司有意向合作，欢迎尽快回复，我们将安排进一步对接。</p>

    <p style="margin-top: 30px; border-top: 1px solid #eee; padding-top: 15px;">
        此致<br>
        [采购方名称] 采购部<br>
        邮箱：[固定邮箱] | 电话：[固定电话]
    </p>
</div>
"""
