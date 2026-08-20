"""
沟通管理 - AI 生成邮件模块

小白讲解：这个文件封装了"AI 生成邮件"的核心逻辑。
- 会话回复：读取该供应商的沟通记录，结合用户提示词生成回复
- 群发/单发：读取产品需求数据，结合模板生成询价邮件
- 重新生成：把上次不满意的生成结果作为"错误示例"发给 AI，让 AI 避免同样问题
- 多语言：用户可选择生成中文/英文/其他语言，多选时同时生成多版

无论用户是否填写提示词，系统提示词（角色+目的）都会带上。
用户提示词为空时，用默认提示词。
"""

import json
import re
from db import get_db, now_str


# ==================== 常量 ====================

# 系统提示词基础部分（无论用户提示词是否为空都必带）
# 小白讲解：系统提示词定义了 AI 的角色、任务和输出格式，是每次生成都必带的基础提示。
# 管理员可在"沟通模板管理"页面直接修改提示词内容，修改后立即生效，不用改代码。
def _get_system_prompt_base():
    """
    从数据库读取AI系统提示词

    小白讲解：管理员可在沟通模板管理页面修改提示词内容，
    修改后立即生效，不需要重启服务。
    如果数据库读取失败，回退到默认硬编码值，保证系统不报错。
    """
    try:
        from db import get_db
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT setting_value FROM ai_prompt_settings WHERE setting_key = 'comm_system_prompt'")
        row = cursor.fetchone()
        conn.close()
        if row and row["setting_value"]:
            return row["setting_value"]
    except Exception:
        pass
    # 回退默认值（数据库读取失败时用，保证系统正常运行）
    return """你是一位专业的采购沟通助手。你的任务是根据供应商沟通记录和产品需求，生成一封专业、礼貌、清晰的商务邮件。

要求：
1. 邮件标题简洁明确，包含关键信息（产品名、目的）
2. 正文结构：称呼 → 开场白 → 核心内容 → 期待回复 → 落款
3. 语气专业但友好，避免过度客气
4. 涉及具体参数（价格、MOQ、交期）时保留原数值，不要编造"""

# 各语言对应的输出格式说明
# 小白讲解：根据用户选的语言，告诉AI用什么语言写邮件，以及输出格式
_LANGUAGE_INSTRUCTIONS = {
    "zh": "请用**中文**撰写邮件。",
    "en": "Please write the email in **English**.",
    "other": "请用**通用商务语言**（如西班牙语、法语、德语等，根据供应商所在国家选择）撰写邮件。",
}

# 单语言输出格式
_OUTPUT_FORMAT_SINGLE = """输出格式为JSON：{"subject": "标题", "body": "正文"}
输出必须是纯JSON，不要包含```json标记或其他文字。"""

# 多语言输出格式（同时生成多种语言）
# 小白讲解：用户多选语言时，AI要同时生成多版标题和正文，用语言代码作为key
_OUTPUT_FORMAT_MULTI = """输出格式为JSON，包含所有选定语言版本：
{
  "zh": {"subject": "中文标题", "body": "中文正文"},
  "en": {"subject": "English Subject", "body": "English Body"}
}
输出必须是纯JSON，不要包含```json标记或其他文字。"""

# 语言中文名映射
LANGUAGE_LABELS = {
    "zh": "中文",
    "en": "英文",
    "other": "其他语言",
}

# 默认用户提示词（用户提示词为空时使用）
# 小白讲解：从数据库读取，管理员可在沟通模板管理页面修改
DEFAULT_PROMPTS = {
    "session_reply": "请根据以上沟通记录，生成一封得体的回复邮件。",
    "bulk_send": "请根据该供应商的产品和需求信息，生成一封首次询价邮件。",
    "single_send": "请根据该供应商的产品和需求信息，生成一封首次询价邮件。",
}

def _get_default_prompt(scene):
    """
    从数据库读取场景默认提示词

    小白讲解：管理员可在沟通模板管理页面修改各场景的默认提示词，
    修改后立即生效。数据库读取失败时回退到 DEFAULT_PROMPTS 硬编码值。
    """
    key_map = {
        "session_reply": "prompt_session_reply",
        "bulk_send": "prompt_bulk_send",
        "single_send": "prompt_single_send",
    }
    db_key = key_map.get(scene)
    if not db_key:
        return DEFAULT_PROMPTS.get(scene, "")
    try:
        from db import get_db
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT setting_value FROM ai_prompt_settings WHERE setting_key = %s", (db_key,))
        row = cursor.fetchone()
        conn.close()
        if row and row["setting_value"]:
            return row["setting_value"]
    except Exception:
        pass
    return DEFAULT_PROMPTS.get(scene, "")

# 场景对应的中文说明
SCENE_LABELS = {
    "session_reply": "会话回复",
    "bulk_send": "群发邮件",
    "single_send": "单发邮件",
}


def _build_system_prompt(languages):
    """
    根据用户选择的语言组装完整的系统提示词

    小白讲解：系统提示词 = 基础要求 + 语言要求 + 输出格式。
    单语言时输出 {"subject": "...", "body": "..."}
    多语言时输出 {"zh": {"subject": "...", "body": "..."}, "en": {...}}

    参数：
        languages: 语言代码列表，如 ["zh"] 或 ["zh", "en"]
    返回：完整的系统提示文字符串
    """
    parts = [_get_system_prompt_base(), ""]

    # 语言要求
    if len(languages) == 1:
        parts.append(_LANGUAGE_INSTRUCTIONS.get(languages[0], ""))
    else:
        # 多语言：列出所有选定语言
        lang_names = [LANGUAGE_LABELS.get(l, l) for l in languages]
        parts.append(f"请同时用以下语言各生成一版邮件：{'、'.join(lang_names)}。")

    parts.append("")

    # 输出格式
    if len(languages) == 1:
        parts.append(_OUTPUT_FORMAT_SINGLE)
    else:
        parts.append(_OUTPUT_FORMAT_MULTI)

    return "\n".join(parts)


def generate_session_reply(supplier_id, user_prompt="", prev_log_id=None, languages=None, template_id=None):
    """
    会话回复场景：读取该供应商的沟通记录，生成回复邮件

    小白讲解：用户在邮件管理会话框点"AI生成"时调用。
    读取该供应商最近20条邮件记录（双方都要），结合用户提示词生成回复。
    如果是重新生成，会带上上次不满意的生成结果作为"错误示例"。
    支持多语言：用户可选中文/英文/其他，多选时同时生成多版。
    支持模板：用户可选邮件模板，AI 会参考模板风格生成。

    参数：
        supplier_id: 供应商ID
        user_prompt: 用户填写的提示词（可为空）
        prev_log_id: 上次生成记录的ID（重新生成时传入，首次生成为None）
        languages: 语言代码列表，如 ["zh"] 或 ["zh", "en"]，默认 ["zh"]
        template_id: 邮件模板ID（可选，传入时AI会参考模板风格）

    返回：(success, result_or_message)
        success=True 时 result_or_message = {"subject": "...", "body": "...", "log_id": ..., "languages": [...]}
        多语言时 result_or_message = {"multi": True, "versions": {"zh": {...}, "en": {...}}, "log_id": ..., "languages": [...]}
        success=False 时 result_or_message = 错误信息字符串
    """
    # 默认中文
    if not languages:
        languages = ["zh"]

    conn = get_db()
    try:
        cursor = conn.cursor()

        # 1. 查供应商信息
        cursor.execute("""
            SELECT s.id, s.name, s.email, s.main_product, s.product_title,
                   r.product_name, r.core_functions, r.material, r.target_market
            FROM suppliers s
            LEFT JOIN requirements r ON s.requirement_id = r.id
            WHERE s.id = %s
        """, (supplier_id,))
        supplier = cursor.fetchone()
        if not supplier:
            return False, "供应商不存在"

        # 2. 查最近20条邮件沟通记录（双方都要，按时间正序）
        cursor.execute("""
            SELECT direction, subject, content, comm_time
            FROM communications
            WHERE supplier_id = %s AND channel = '邮件'
            ORDER BY comm_time DESC, id DESC
            LIMIT 20
        """, (supplier_id,))
        records = cursor.fetchall()
        # 反转成时间正序（旧→新），方便 AI 理解对话顺序
        records = list(reversed(records))

        # 3. 组装上下文
        context = _build_session_context(supplier, records)

        # 3.5 如果选了模板，把模板内容加到上下文里
        if template_id:
            cursor.execute("SELECT name, content FROM email_templates WHERE id = %s", (template_id,))
            tpl = cursor.fetchone()
            if tpl:
                context += f"\n\n【参考模板：{tpl['name']}】\n{tpl['content']}\n"

        # 4. 组装提示词
        actual_prompt = user_prompt.strip() if user_prompt and user_prompt.strip() else _get_default_prompt("session_reply")

        # 5. 如果是重新生成，带上上次的错误示例
        prev_example = ""
        if prev_log_id:
            prev_example = _get_prev_example(prev_log_id)

        # 6. 组装系统提示词（根据语言）
        system_prompt = _build_system_prompt(languages)

        # 7. 组装完整消息
        messages = _build_messages(system_prompt, actual_prompt, context, prev_example)

        # 8. 调用 MiniMax
        # 小白讲解：显式指定 max_tokens=32768，覆盖场景配置。
        # 原因：MiniMax-M3 思考过程也消耗输出 token，配置值过小会导致
        # content 被截断或为空，JSON 解析失败报"AI 返回格式异常"。
        # 32768 足以容纳思考过程 + 完整邮件正文，彻底避免截断。
        from ai_helper import call_llm
        result_text = call_llm(
            messages=messages,
            scene_code="comm_reply",
            json_mode=True,
            max_tokens=32768,
        )

        # 9. 解析 JSON 结果
        parsed = _parse_ai_result(result_text, languages)
        if not parsed:
            # 解析失败时把 AI 原始返回前200字带给前端，方便排查
            preview = (result_text or "")[:200]
            return False, f"AI 返回格式异常，请重试。原始返回前200字：{preview}"

        # 10. 记录到 ai_generation_logs
        # 多语言时把所有版本拼成一段文字存日志
        if parsed.get("multi"):
            log_text = json.dumps(parsed["versions"], ensure_ascii=False)
            log_subject = parsed["versions"].get(languages[0], {}).get("subject", "")
        else:
            log_text = parsed.get("body", "")
            log_subject = parsed.get("subject", "")

        log_id = _save_generation_log(
            cursor=cursor, conn=conn,
            scene="session_reply",
            supplier_id=supplier_id,
            user_prompt=user_prompt,
            generated_subject=log_subject,
            generated_body=log_text,
        )

        parsed["log_id"] = log_id
        parsed["languages"] = languages
        return True, parsed

    except Exception as e:
        return False, f"AI 生成失败：{str(e)}"
    finally:
        conn.close()


def generate_bulk_or_single(supplier_ids, user_prompt="", scene="bulk_send",
                              template_id=None, prev_log_id=None, languages=None):
    """
    群发/单发场景：读取产品需求数据，结合模板生成询价邮件

    小白讲解：用户在群发邮件或单发邮件界面点"AI生成"时调用。
    读取选中供应商对应的产品需求数据，结合用户选择的模板生成邮件。
    群发时取第一个供应商的需求作为代表（所有供应商通常来自同一需求）。
    支持多语言：用户可选中文/英文/其他，多选时同时生成多版。

    参数：
        supplier_ids: 供应商ID列表（群发多个，单发一个）
        user_prompt: 用户提示词
        scene: "bulk_send" 或 "single_send"
        template_id: 选用的模板ID（可为None）
        prev_log_id: 重新生成时传入
        languages: 语言代码列表，如 ["zh"] 或 ["zh", "en"]，默认 ["zh"]

    返回：(success, result_or_message)
    """
    if not supplier_ids:
        return False, "请至少选择一个供应商"

    # 默认中文
    if not languages:
        languages = ["zh"]

    conn = get_db()
    try:
        cursor = conn.cursor()

        # 1. 查供应商和需求信息（取第一个作为代表）
        first_id = supplier_ids[0]
        cursor.execute("""
            SELECT s.id, s.name, s.email, s.main_product, s.product_title,
                   r.id AS requirement_id, r.product_name, r.core_functions,
                   r.material, r.spec_size, r.target_market, r.first_purchase_qty,
                   r.acceptable_moq, r.acceptable_lead_time, r.required_certs
            FROM suppliers s
            LEFT JOIN requirements r ON s.requirement_id = r.id
            WHERE s.id = %s
        """, (first_id,))
        supplier = cursor.fetchone()
        if not supplier:
            return False, "供应商不存在"

        # 2. 查模板（如选了模板）
        template = None
        if template_id:
            cursor.execute("""
                SELECT * FROM communication_templates WHERE id = %s AND is_enabled = 1
            """, (template_id,))
            template = cursor.fetchone()

        # 3. 组装上下文（产品需求数据 + 模板内容）
        context = _build_requirement_context(supplier, template, len(supplier_ids))

        # 4. 组装提示词
        # 小白讲解：用户没填提示词时，从数据库读管理员配置的默认提示词（不是写死的）
        # 这样管理员在"沟通模板管理→AI系统提示词配置"里改完保存就能立即生效
        actual_prompt = user_prompt.strip() if user_prompt and user_prompt.strip() else _get_default_prompt(scene)

        # 5. 重新生成时带上次错误示例
        prev_example = ""
        if prev_log_id:
            prev_example = _get_prev_example(prev_log_id)

        # 6. 组装系统提示词（根据语言）
        system_prompt = _build_system_prompt(languages)

        # 7. 组装完整消息
        messages = _build_messages(system_prompt, actual_prompt, context, prev_example)

        # 8. 调用 MiniMax
        # 小白讲解：显式指定 max_tokens=32768，覆盖场景配置。
        # 原因：MiniMax-M3 思考过程也消耗输出 token，配置值过小会导致
        # content 被截断或为空，JSON 解析失败报"AI 返回格式异常"。
        # 32768 足以容纳思考过程 + 完整邮件正文，彻底避免截断。
        from ai_helper import call_llm
        result_text = call_llm(
            messages=messages,
            scene_code="comm_send",
            json_mode=True,
            max_tokens=32768,
        )

        # 9. 解析结果
        parsed = _parse_ai_result(result_text, languages)
        if not parsed:
            preview = (result_text or "")[:200]
            return False, f"AI 返回格式异常，请重试。原始返回前200字：{preview}"

        # 10. 记录日志（群发时 supplier_id 为 NULL）
        if parsed.get("multi"):
            log_text = json.dumps(parsed["versions"], ensure_ascii=False)
            log_subject = parsed["versions"].get(languages[0], {}).get("subject", "")
        else:
            log_text = parsed.get("body", "")
            log_subject = parsed.get("subject", "")

        log_id = _save_generation_log(
            cursor=cursor, conn=conn,
            scene=scene,
            supplier_id=first_id if scene == "single_send" else None,
            user_prompt=user_prompt,
            generated_subject=log_subject,
            generated_body=log_text,
        )

        parsed["log_id"] = log_id
        parsed["languages"] = languages
        return True, parsed

    except Exception as e:
        return False, f"AI 生成失败：{str(e)}"
    finally:
        conn.close()


def accept_generation(log_id):
    """
    标记某次 AI 生成结果为已采纳

    小白讲解：用户点"确认填入"后调用，把这次生成记录标记为已采纳，
    方便后续统计 AI 生成质量和采纳率。
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE ai_generation_logs SET is_accepted = 1 WHERE id = %s", (log_id,))
        conn.commit()
        return True
    finally:
        conn.close()


# ==================== 内部辅助函数 ====================

def _build_session_context(supplier, records):
    """
    组装会话回复场景的上下文（供应商信息 + 沟通记录）

    小白讲解：把供应商的基本信息和往来邮件记录拼成一段文字发给 AI，
    让 AI 知道之前聊了什么，才能生成合适的回复。
    """
    lines = []
    lines.append("【供应商信息】")
    lines.append(f"供应商名称：{supplier['name']}")
    lines.append(f"主营产品：{supplier.get('main_product') or '未知'}")
    if supplier.get('product_name'):
        lines.append(f"需求产品：{supplier['product_name']}")
    lines.append("")

    if records:
        lines.append("【最近沟通记录】")
        for i, r in enumerate(records, 1):
            direction = "我方发出" if r["direction"] == "outbound" else "供应商回复"
            lines.append(f"{i}. [{direction}] {r.get('comm_time', '')}")
            if r.get("subject"):
                lines.append(f"   主题：{r['subject']}")
            if r.get("content"):
                # 截取前500字，避免上下文过长
                content = r["content"][:500]
                lines.append(f"   内容：{content}")
            lines.append("")
    else:
        lines.append("【最近沟通记录】暂无历史记录，这是首次沟通。")
        lines.append("")

    return "\n".join(lines)


def _build_requirement_context(supplier, template, supplier_count):
    """
    组装群发/单发场景的上下文（产品需求数据 + 模板内容）

    小白讲解：把产品需求的详细参数和选用的模板内容拼成文字发给 AI，
    让 AI 根据需求生成一封专业的询价邮件。
    """
    lines = []
    lines.append("【供应商信息】")
    lines.append(f"供应商名称：{supplier['name']}")
    lines.append(f"主营产品：{supplier.get('main_product') or '未知'}")
    if supplier.get('product_title'):
        lines.append(f"产品标题：{supplier['product_title']}")
    lines.append("")

    lines.append("【产品需求数据】")
    if supplier.get('product_name'):
        lines.append(f"需求产品：{supplier['product_name']}")
    if supplier.get('core_functions'):
        lines.append(f"核心功能：{supplier['core_functions']}")
    if supplier.get('material'):
        lines.append(f"材质：{supplier['material']}")
    if supplier.get('spec_size'):
        lines.append(f"规格尺寸：{supplier['spec_size']}")
    if supplier.get('target_market'):
        lines.append(f"目标市场：{supplier['target_market']}")
    if supplier.get('first_purchase_qty'):
        lines.append(f"首次采购数量：{supplier['first_purchase_qty']}")
    if supplier.get('acceptable_moq'):
        lines.append(f"可接受MOQ：{supplier['acceptable_moq']}")
    if supplier.get('acceptable_lead_time'):
        lines.append(f"可接受交期：{supplier['acceptable_lead_time']}")
    if supplier.get('required_certs'):
        lines.append(f"需要认证：{supplier['required_certs']}")
    lines.append("")

    if supplier_count > 1:
        lines.append(f"【群发说明】本次邮件将群发给 {supplier_count} 家供应商，邮件内容应通用，不要包含特定供应商的名称。")
        lines.append("")
    else:
        lines.append("【发送说明】本次邮件单独发送给该供应商，可以包含供应商名称。")
        lines.append("")

    if template:
        lines.append("【参考模板】")
        lines.append(f"模板名称：{template['name']}")
        if template.get('subject_template'):
            lines.append(f"标题模板：{template['subject_template']}")
        if template.get('body_template'):
            lines.append(f"正文模板：{template['body_template']}")
        lines.append("（请参考模板的风格和结构，但不要完全照搬，根据需求信息灵活调整）")
        lines.append("")

    return "\n".join(lines)


def _get_prev_example(prev_log_id):
    """
    获取上次生成的不满意结果，作为"错误示例"发给 AI

    小白讲解：用户点"重新生成"时，系统把上次生成的标题和正文发给 AI，
    告诉 AI "这版不够好，请避免类似问题，重新生成一版"。
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT generated_subject, generated_body FROM ai_generation_logs WHERE id = %s
        """, (prev_log_id,))
        row = cursor.fetchone()
        if not row:
            return ""

        return f"""【上次生成的不满意示例（请避免类似问题）】
标题：{row['generated_subject']}
正文：{row['generated_body']}

请重新生成一版更好的邮件，避免上述示例存在的问题。
"""
    finally:
        conn.close()


def _build_messages(system_prompt, user_prompt, context, prev_example=""):
    """
    组装发给 DeepSeek 的完整消息列表

    小白讲解：把系统提示词、上下文数据、用户提示词、错误示例按顺序拼成消息列表。
    消息顺序：system(系统提示词) → user(上下文+用户提示词+错误示例)
    """
    user_content = f"""{context}

{prev_example}

【用户要求】
{user_prompt}
"""
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def _parse_ai_result(result_text, languages):
    """
    解析 AI 返回的 JSON 结果

    小白讲解：
    - 单语言时 AI 返回 {"subject": "...", "body": "..."}，直接取
    - 多语言时 AI 返回 {"zh": {"subject": "...", "body": "..."}, "en": {...}}，
      需要把每个语言版本都解析出来

    解析失败时尝试用正则提取。

    参数：
        result_text: AI 返回的原始文本
        languages: 用户选择的语言列表

    返回：
        单语言：{"subject": "...", "body": "..."}
        多语言：{"multi": True, "versions": {"zh": {...}, "en": {...}}}
        失败：None
    """
    if not result_text:
        print("[AI解析失败] result_text 为空")
        return None

    text = result_text.strip()

    # 小白讲解：思考模式下 AI 可能返回 <think>...</think> 包裹的推理过程，
    # 需要先去掉，只保留 JSON 部分
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    # 去掉可能的 ```json``` 包裹
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    # 小白讲解：有时 AI 在 JSON 前后混入了解释文字，
    # 尝试提取第一个 { 到最后一个 } 之间的内容
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
        text = text[brace_start:brace_end + 1]

    # 辅助函数：安全取字符串值（防止 None 或非字符串导致 .strip() 报错）
    # 小白讲解：AI 有时候会把 subject 或 body 返回成 null，直接 .strip() 会报错，
    # 这个辅助函数先检查是不是字符串，不是就返回空字符串
    def _safe_str(val):
        if val is None:
            return ""
        if isinstance(val, str):
            return val.strip()
        # 数字等其他类型转成字符串
        return str(val).strip()

    # 尝试 JSON 解析
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # JSON 解析失败，尝试正则提取（仅单语言场景）
        subject_match = re.search(r'"subject"\s*:\s*"([^"]+)"', text)
        body_match = re.search(r'"body"\s*:\s*"([^"]+)"', text)
        subject = subject_match.group(1) if subject_match else ""
        body = body_match.group(1) if body_match else ""
        if subject and body:
            return {"subject": subject, "body": body}
        # 打印日志方便排查
        print(f"[AI解析失败] JSON解析失败, 原始返回前500字: {result_text[:500]}")
        return None

    # 单语言场景：直接返回 subject 和 body
    if len(languages) == 1:
        if "subject" in data and "body" in data:
            subject = _safe_str(data["subject"])
            body = _safe_str(data["body"])
            if subject and body:
                return {"subject": subject, "body": body}
            # subject 或 body 为空，继续尝试其他兼容逻辑
            print(f"[AI解析警告] 单语言 subject/body 为空, subject={repr(subject)}, body={repr(body)}")
        # 兼容：多语言格式但只选了一种语言
        lang = languages[0]
        if lang in data and isinstance(data[lang], dict):
            v = data[lang]
            subject = _safe_str(v.get("subject"))
            body = _safe_str(v.get("body"))
            if subject and body:
                return {"subject": subject, "body": body}
        # 兼容：AI 返回的 key 名不同（如 title/content）
        subject = _safe_str(data.get("subject") or data.get("title"))
        body = _safe_str(data.get("body") or data.get("content"))
        if subject and body:
            return {"subject": subject, "body": body}
        print(f"[AI解析失败] 单语言但格式不匹配, keys={list(data.keys())}, 原始前300字: {result_text[:300]}")
        return None

    # 多语言场景：返回所有语言版本
    # 小白讲解：用户选了多种语言时，AI 应该返回每种语言的版本。
    # 只要至少有一种语言解析成功就返回，不要求所有语言都齐全（AI 可能漏掉某种语言）
    versions = {}
    for lang in languages:
        if lang in data and isinstance(data[lang], dict):
            v = data[lang]
            subject = _safe_str(v.get("subject"))
            body = _safe_str(v.get("body"))
            if subject and body:
                versions[lang] = {"subject": subject, "body": body}

    if not versions:
        print(f"[AI解析失败] 多语言但无匹配版本, keys={list(data.keys())}, 原始前300字: {result_text[:300]}")
        return None

    return {"multi": True, "versions": versions}


def _save_generation_log(cursor, conn, scene, supplier_id, user_prompt,
                          generated_subject, generated_body):
    """
    把一次 AI 生成结果保存到 ai_generation_logs 表

    小白讲解：保存生成记录是为了：1.重新生成时能取到上次的"错误示例"
    2.后续可以统计 AI 生成质量和采纳率
    """
    # 系统提示词取当前默认（多语言时无法精确还原，存基础部分即可）
    system_prompt = _get_system_prompt_base()
    cursor.execute("""
        INSERT INTO ai_generation_logs
        (user_id, scene, supplier_id, user_prompt, system_prompt,
         generated_subject, generated_body, is_accepted, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s)
    """, (
        1, scene, supplier_id,
        user_prompt or "", system_prompt,
        generated_subject, generated_body, now_str()
    ))
    conn.commit()
    return cursor.lastrowid
