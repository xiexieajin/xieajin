"""
初筛执行引擎 - 编排整个初筛流程

小白讲解：这个文件是初筛的"总指挥"。
它把前面几个模块串起来，按顺序完成每个供应商的初筛：
1. 从数据库加载规则集（screening_rules.py）
2. 对每个供应商查天眼查数据（screening_data.py）
3. 用规则评估否决项和评分（screening_rules.py）
4. 必要时调用AI做语义判断（ai_helper.py）
5. 把每步结果记到审计日志（screening_audit.py）
6. 把最终结论写回 screenings 表和 suppliers 表

进度反馈：通过 progress_queue 推送实时进度，供前端SSE展示。
"""

import json
import pymysql
import traceback
from db import now_str, recalc_requirement_status
from config import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE
from screening_rules import get_active_rules, evaluate_condition, parse_condition, get_rule_template
from screening_data import (
    query_supplier_full_data, parse_risk_detail, parse_capital_wan,
    parse_established_years, validate_contact_info, search_platform_infringe,
)
# 小白讲解：从 screening_audit 导入全局写锁，迁移到MySQL后并发写不再互斥，
# 但保留这把锁作为额外保险。
from screening_audit import create_run_id, log_task, generate_audit_report, _db_write_lock
from ai_helper import call_deepseek, extract_json_from_text


def _get_db_connection():
    """
    创建MySQL数据库连接（后台长任务使用，不依赖Flask请求上下文）

    小白讲解：从SQLite迁移到MySQL后，用pymysql连接。MySQL天然支持并发读写，
    不再需要SQLite的WAL和busy_timeout防锁配置。
    """
    conn = pymysql.connect(
        host=MYSQL_HOST, port=MYSQL_PORT,
        user=MYSQL_USER, password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE, charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    return conn


def _get_thresholds():
    """
    从数据库读取初筛通过标准配置（通过线、人工确认线）

    小白讲解：这两个阈值以前是写死在代码里的（75和60），现在改成从数据库读取，
    业务部门可以在规则配置页直接修改数字，不需要改代码。

    返回：(pass_threshold, manual_review_threshold)
        pass_threshold: 总分≥此值则"已通过初筛"，默认75
        manual_review_threshold: 总分≥此值但<pass_threshold则"需人工确认"，默认60
        低于manual_review_threshold则"未通过初筛"
    """
    try:
        pass_rule = get_rule_template("threshold_pass")
        review_rule = get_rule_template("threshold_manual_review")
        pass_threshold = pass_rule.get("max_score", 75) if pass_rule else 75
        manual_review_threshold = review_rule.get("max_score", 60) if review_rule else 60
        # 防止配置错误：用户把人工确认线设成0表示"不要人工确认"
        # 0会导致 total_score>=0 永远成立，所有供应商变成"需人工确认"，必须截断为正常值
        if manual_review_threshold <= 0:
            manual_review_threshold = 1
        # 防止配置错误（通过线必须≥人工确认线）
        # 小白讲解：允许两线相等——相等时相当于取消人工确认环节，低于此分一律未通过
        if pass_threshold < manual_review_threshold:
            print(f"[警告] 通过线({pass_threshold})<人工确认线({manual_review_threshold})，使用默认值75/60")
            return 75, 60
        return pass_threshold, manual_review_threshold
    except Exception as e:
        print(f"[警告] 读取通过标准配置失败({e})，使用默认值75/60")
        return 75, 60


def _push_progress(queue, **kwargs):
    """
    推送进度消息到队列（前端SSE消费）

    小白讲解：前端通过SSE长连接实时展示初筛进度，
    这个函数把当前进度（处理到第几家、在做什么）推到队列里，前端就能看到。

    参数：queue 队列对象（None则不推送），kwargs 进度字段
    """
    if queue is None:
        return
    queue.put(kwargs)


def run_screening(requirement_id, user_id, progress_queue=None):
    """
    初筛主入口 - 对某需求下所有待初筛供应商执行完整初筛流程

    小白讲解：用户点"开始初筛"后，路由调用这个函数。
    它会按顺序处理每家供应商，并通过progress_queue推送实时进度。

    参数：
        requirement_id: 需求ID
        user_id: 执行初筛的用户ID（数据隔离）
        progress_queue: 进度队列（供前端SSE展示），None则不推送

    返回：审计报告字典，包含 run_id / statistics / suppliers / anomalies
    """
    # ==================== 任务1：加载初筛规则 ====================
    _push_progress(progress_queue,
                   type="progress", step=0, total=3,
                   desc=f"正在加载初筛规则...")

    rules = get_active_rules(user_id=user_id)
    veto_rules = [r for r in rules if r["rule_type"] == "veto"]
    score_rules = [r for r in rules if r["rule_type"] == "score"]

    _push_progress(progress_queue,
                   type="progress", step=1, total=3,
                   desc=f"已加载{len(veto_rules)}条否决规则 + {len(score_rules)}条评分规则")

    # ==================== 任务2：读取待初筛供应商 ====================
    conn = _get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT s.*, r.product_name as req_product_name, r.target_market,
               r.required_certs, r.requirement_summary
        FROM suppliers s
        JOIN requirements r ON s.requirement_id = r.id
        WHERE s.requirement_id = %s AND s.dev_stage = '已寻源待初筛' AND s.user_id = %s
        ORDER BY s.id ASC
    """, (requirement_id, user_id))
    suppliers = cursor.fetchall()
    conn.close()

    if not suppliers:
        _push_progress(progress_queue,
                       type="progress", step=3, total=3,
                       desc="没有待初筛的供应商")
        _push_progress(progress_queue, type="done",
                       message="没有待初筛的供应商",
                       report={"run_id": "", "total_logs": 0,
                               "statistics": {"success": 0, "fail": 0, "skip": 0, "uncertain": 0},
                               "suppliers": [], "anomalies": []})
        return {"run_id": "", "total_logs": 0,
                "statistics": {"success": 0, "fail": 0, "skip": 0, "uncertain": 0},
                "suppliers": [], "anomalies": []}

    total_suppliers = len(suppliers)
    _push_progress(progress_queue,
                   type="progress", step=2, total=3,
                   desc=f"共{total_suppliers}家供应商待初筛，开始处理...")

    # ==================== 任务3：建立审计账本 ====================
    run_id = create_run_id()

    # ==================== 逐供应商执行初筛 ====================
    for idx, supplier in enumerate(suppliers, start=1):
        supplier_dict = dict(supplier)
        _push_progress(progress_queue,
                       type="supplier_progress",
                       current=idx, total=total_suppliers,
                       supplier_name=supplier_dict.get("name", ""),
                       desc=f"正在初筛第{idx}/{total_suppliers}家：{supplier_dict.get('name', '')}")

        try:
            _screen_one_supplier(
                supplier_dict, veto_rules, score_rules,
                run_id, user_id, progress_queue, idx
            )
        except Exception as e:
            # 单个供应商失败不影响其他供应商
            err_msg = f"{supplier_dict.get('name', '')}初筛异常: {str(e)}"
            log_task(run_id, supplier_dict["id"], "engine_exception",
                     "初筛引擎异常", {"supplier": supplier_dict.get("name", "")},
                     {"error": str(e), "traceback": traceback.format_exc()[:500]},
                     "引擎异常", "fail", user_id)
            _push_progress(progress_queue,
                           type="supplier_error",
                           current=idx,
                           supplier_name=supplier_dict.get("name", ""),
                           message=err_msg)

    # ==================== 任务20：输出执行报告 ====================
    report = generate_audit_report(run_id, user_id=user_id)
    passed = sum(1 for s in report["suppliers"] if not s["has_veto"] and not s["has_fail"] and not s["has_uncertain"])

    _push_progress(progress_queue,
                   type="done",
                   message=f"初筛完成！共处理{total_suppliers}家，"
                           f"通过{passed}家，异常{len(report['anomalies'])}处",
                   report=report)

    # 初筛完成后，供应商阶段已变化（已通过/未通过初筛），重新推断所属需求的状态
    # 小白讲解：初筛会让需求从"寻源中"自动推进到"初筛中"
    try:
        _conn = _get_db_connection()
        _cur = _conn.cursor()
        recalc_requirement_status(_cur, requirement_id)
        _conn.commit()
        _conn.close()
    except Exception:
        pass

    return report


def _screen_one_supplier(supplier, veto_rules, score_rules, run_id, user_id, progress_queue, idx=0):
    """
    处理单个供应商的完整初筛流程

    小白讲解：这是初筛的核心函数，按顺序执行以下步骤：
    1. 天眼查主体复核 + 数据采集
    2. 联系方式审计
    3. 执行一票否决规则
    4. 如果未否决，执行评分规则
    5. 生成结论并写回数据库

    参数：
        supplier: 供应商字典（已JOIN需求信息）
        veto_rules: 一票否决规则列表
        score_rules: 评分规则列表
        run_id: 审计批次ID
        user_id: 用户ID
        progress_queue: 进度队列
        idx: 供应商序号（用于前端进度展示）
    """
    supplier_id = supplier["id"]
    company_name = supplier.get("name", "")

    # ==================== 任务4：天眼查同名主体复核 + 数据采集 ====================
    log_task(run_id, supplier_id, "tyc_registration_check", "天眼查主体复核",
             {"company_name": company_name}, {}, "开始查询", "success", user_id)

    tyc_data = query_supplier_full_data(company_name)
    basic_info = tyc_data.get("basic_info", {})
    match_status = tyc_data.get("tyc_match_status", "not_found")

    log_task(run_id, supplier_id, "tyc_registration_check", "天眼查主体复核",
             {"company_name": company_name},
             {"match_status": match_status,
              "company_id": tyc_data.get("company_id", ""),
              "matched_name": basic_info.get("name", company_name)},
             f"天眼查MCP search_companies + get_company_basic_profile，匹配状态：{match_status}",
             "success" if match_status != "not_found" else "uncertain", user_id)

    # 天眼查匹配失败：直接进入人工确认
    if match_status == "not_found":
        _write_screening_result(supplier, run_id, user_id,
                                conclusion="需人工确认",
                                reason="天眼查未找到同名企业，无法核实工商信息",
                                scores={}, tyc_data=tyc_data)
        log_task(run_id, supplier_id, "conclusion", "初筛结论",
                 {"match_status": "not_found"},
                 {"conclusion": "需人工确认", "reason": "天眼查未找到同名企业"},
                 "天眼查无匹配，进入人工确认", "uncertain", user_id)
        _push_progress(progress_queue,
                       type="supplier_done",
                       current=idx,
                       supplier_name=company_name,
                       conclusion="需人工确认",
                       total_score=0,
                       veto_triggered=False)
        return

    # ==================== 任务4b：提取规则评估所需的标准化字段 ====================
    # 小白讲解：把天眼查返回的原始数据，转成规则条件能直接比较的标准化字段
    capital_wan = parse_capital_wan(basic_info.get("registered_capital", ""))
    operating_status = basic_info.get("status", "") or supplier.get("operating_status", "")
    establish_date = basic_info.get("establish_date", "") or supplier.get("establish_date", "")
    operating_years = parse_established_years(establish_date)
    business_scope = basic_info.get("business_scope", "")
    phone = basic_info.get("phone", "") or supplier.get("phone", "")
    email = basic_info.get("email", "") or supplier.get("email", "")

    # 解析风险总览
    risk_detail = parse_risk_detail(tyc_data.get("risk_overview", ""))

    # 联系方式审计
    contact_audit = validate_contact_info(phone, email)

    # 构造规则评估的数据上下文
    eval_data = {
        "reg_capital_wan": capital_wan,
        "operating_status": operating_status,
        "establish_date": establish_date,
        "operating_years": operating_years,
        "business_scope": business_scope,
        "has_business_exception": risk_detail["has_business_exception"],
        "has_serious_violation": risk_detail["has_serious_violation"],
        "is_faithless_person": risk_detail["is_faithless_person"],
        "has_judicial_case": risk_detail["has_judicial_case"],
        "risk_count": risk_detail["risk_count"],
        "has_valid_phone": contact_audit["has_valid_phone"],
        "has_valid_email": contact_audit["has_valid_email"],
        "has_valid_contact": contact_audit["has_valid_contact"],
        "contact_completeness": contact_audit["completeness"],
        "tyc_match_status": match_status,
        "qualifications_text": tyc_data.get("qualifications", ""),
        "trademarks_text": tyc_data.get("trademarks", ""),
        "patents_text": tyc_data.get("patents", ""),
    }

    # 记录数据采集结果到审计日志
    log_task(run_id, supplier_id, "data_collection", "数据采集结果",
             {"company_name": company_name},
             {"capital_wan": capital_wan, "operating_status": operating_status,
              "operating_years": operating_years, "contact_audit": contact_audit,
              "risk_detail": risk_detail},
             "天眼查MCP返回数据已解析", "success", user_id)

    # ==================== 任务5-7：联系方式审计 ====================
    log_task(run_id, supplier_id, "contact_audit", "联系方式审计",
             {"phone": phone, "email": email, "source": "天眼查+供应商表"},
             contact_audit,
             f"联系方式完整度：{contact_audit['completeness']}",
             "success" if contact_audit["has_valid_contact"] else "uncertain", user_id)

    # ==================== 任务14：执行一票否决规则 ====================
    _push_progress(progress_queue,
                   type="supplier_step",
                   supplier_name=company_name,
                   step="正在执行一票否决规则检查...")

    veto_triggered = False
    veto_reason = ""
    veto_rule_code = ""

    for rule in veto_rules:
        if not rule.get("is_enabled", 1):
            continue

        condition = parse_condition(rule.get("default_condition", {}))
        if not condition:
            continue

        hit = evaluate_condition(condition, eval_data)

        # 特殊处理：平台侵权和知识产权败诉需要AI/互联网搜索辅助判断
        if rule["rule_code"] == "veto_platform_infringe":
            hit = _check_platform_infringe(run_id, supplier_id, company_name, user_id)
        elif rule["rule_code"] == "veto_ip_lawsuit":
            hit = _check_ip_lawsuit(run_id, supplier_id, company_name,
                                     tyc_data.get("judicial_case", ""), user_id)
        elif rule["rule_code"] == "veto_non_manufacturer":
            hit = _check_non_manufacturer(run_id, supplier_id, company_name,
                                           business_scope, supplier, user_id)
        elif rule["rule_code"] == "veto_product_mismatch":
            hit = _check_product_mismatch(run_id, supplier_id, supplier, user_id)

        log_task(run_id, supplier_id, rule["rule_code"], rule["rule_name"],
                 {"condition": condition, "actual_data": _safe_subset(eval_data)},
                 {"passed": not hit, "hit": hit},
                 rule.get("description", ""),
                 "success", user_id)

        if hit:
            veto_triggered = True
            veto_reason = rule.get("default_action", {}).get("reason", rule["rule_name"])
            veto_rule_code = rule["rule_code"]
            _push_progress(progress_queue,
                           type="supplier_step",
                           supplier_name=company_name,
                           step=f"触发一票否决：{rule['rule_name']}")
            break  # 触发一个否决就停止继续检查

    # ==================== 任务15：执行评分规则（仅未否决时）====================
    scores = {}
    total_score = 0

    if not veto_triggered:
        _push_progress(progress_queue,
                       type="supplier_step",
                       supplier_name=company_name,
                       step="正在执行评分规则...")

        for rule in score_rules:
            if not rule.get("is_enabled", 1):
                continue

            rule_score = _evaluate_score_rule(
                rule, eval_data, supplier, tyc_data,
                run_id, supplier_id, user_id
            )
            scores[rule["rule_code"]] = rule_score
            total_score += rule_score

            log_task(run_id, supplier_id, rule["rule_code"], rule["rule_name"],
                     {"max_score": rule.get("max_score", 0)},
                     {"score": rule_score, "total_so_far": total_score},
                     rule.get("scoring_logic", ""),
                     "success", user_id)

    # ==================== 任务16：生成初筛结论 ====================
    # 小白讲解：通过线/人工确认线从数据库读取，业务部门可在规则配置页修改
    pass_threshold, manual_review_threshold = _get_thresholds()
    if veto_triggered:
        conclusion = "未通过初筛"
        reason = f"触发一票否决[{veto_rule_code}]：{veto_reason}"
    elif total_score >= pass_threshold:
        conclusion = "已通过初筛"
        reason = f"总分{total_score}≥{pass_threshold}，通过初筛"
    elif manual_review_threshold > 0 and total_score >= manual_review_threshold:
        conclusion = "需人工确认"
        reason = f"总分{total_score}在{manual_review_threshold}-{pass_threshold-1}区间，需人工确认"
    else:
        conclusion = "未通过初筛"
        reason = f"总分{total_score}<{manual_review_threshold}，未通过初筛"

    log_task(run_id, supplier_id, "conclusion", "初筛结论",
             {"veto_triggered": veto_triggered, "total_score": total_score},
             {"conclusion": conclusion, "reason": reason,
              "veto_rule": veto_rule_code, "scores": scores,
              "total_score": total_score},
             "规则引擎汇总", "success", user_id)

    # ==================== 任务18：写回数据库 ====================
    _write_screening_result(
        supplier, run_id, user_id,
        conclusion=conclusion, reason=reason,
        scores=scores, total_score=total_score,
        veto_triggered=veto_triggered, veto_reason=veto_reason,
        tyc_data=tyc_data, basic_info=basic_info,
        contact_audit=contact_audit, capital_wan=capital_wan,
        establish_date=establish_date, operating_status=operating_status,
        business_scope=business_scope, match_status=match_status
    )

    _push_progress(progress_queue,
                   type="supplier_done",
                   current=idx,
                   supplier_name=company_name,
                   conclusion=conclusion,
                   total_score=total_score,
                   veto_triggered=veto_triggered)


# ==================== 一票否决规则的特殊判断 ====================

def _check_platform_infringe(run_id, supplier_id, company_name, user_id):
    """
    检查平台侵权下架记录（互联网搜索 + AI分析）

    小白讲解：天眼查无法提供平台侵权数据，需要用DuckDuckGo搜索，
    然后用AI分析搜索结果中是否有明确的侵权下架记录。
    由于互联网来源可信度较低，默认不作为自动否决的充分条件。
    """
    search_result = search_platform_infringe(company_name)
    # 用AI分析搜索结果
    try:
        prompt = f"""请分析以下搜索结果，判断是否有明确的平台侵权下架记录。
只返回JSON：{{"has_infringe": true/false, "evidence": "证据摘要"}}

企业：{company_name}
搜索结果：
{search_result[:1000]}"""
        result_text = call_deepseek(
            [{"role": "user", "content": prompt}],
            scene_code="auto_screening", temperature=0.1, json_mode=True
        )
        result = extract_json_from_text(result_text)
        has_infringe = bool(result.get("has_infringe", False))
        evidence = result.get("evidence", "")
    except Exception:
        has_infringe = False
        evidence = "AI分析失败"

    log_task(run_id, supplier_id, "veto_platform_infringe_check", "平台侵权检查",
             {"company_name": company_name, "search_result": search_result[:500]},
             {"has_infringe": has_infringe, "evidence": evidence},
             "DuckDuckGo搜索 + AI分析（互联网来源，可信度需人工复核）",
             "uncertain" if has_infringe else "success", user_id)

    # 互联网来源不作为自动否决的充分条件，返回False（标注uncertain供人工复核）
    return False


def _check_ip_lawsuit(run_id, supplier_id, company_name, judicial_case_text, user_id):
    """
    检查近3年是否有知识产权侵权败诉记录

    小白讲解：从天眼查的司法案件文本中，检查是否有知识产权侵权败诉。
    如果司法案件数据未获取，返回False（不触发否决）。
    """
    if not judicial_case_text:
        log_task(run_id, supplier_id, "veto_ip_lawsuit_check", "知识产权败诉检查",
                 {"company_name": company_name},
                 {"has_ip_lawsuit": False, "reason": "司法案件数据未获取"},
                 "天眼查judicial_case未返回", "skip", user_id)
        return False

    # 简单关键字匹配：包含"知识产权"+"败诉"/"判决"
    has_ip = "知识产权" in judicial_case_text or "专利" in judicial_case_text or "商标" in judicial_case_text
    has_lose = "败诉" in judicial_case_text or "判决" in judicial_case_text
    hit = has_ip and has_lose

    log_task(run_id, supplier_id, "veto_ip_lawsuit_check", "知识产权败诉检查",
             {"company_name": company_name, "case_text_len": len(judicial_case_text)},
             {"has_ip_lawsuit": hit, "has_ip_keyword": has_ip, "has_lose_keyword": has_lose},
             "天眼查get_judicial_case返回文本分析",
             "success" if not hit else "success", user_id)
    return hit


def _check_non_manufacturer(run_id, supplier_id, company_name, business_scope, supplier, user_id):
    """
    判断是否为非制造商（AI辅助判断）

    小白讲解：综合分析经营范围、供应商类型、资质等，判断是否为制造商。
    如果经营范围含"生产""制造""加工"等关键字，认为是制造商。
    否则用AI做进一步判断。
    """
    # 快速关键字判断
    manufacture_keywords = ["生产", "制造", "加工", "研发"]
    if any(kw in business_scope for kw in manufacture_keywords):
        log_task(run_id, supplier_id, "veto_non_manufacturer_check", "制造商判断",
                 {"company_name": company_name, "business_scope": business_scope[:200]},
                 {"is_manufacturer": True, "reason": "经营范围含生产/制造/加工关键字"},
                 "经营范围关键字匹配", "success", user_id)
        return False  # 是制造商，不触发否决

    # 供应商类型判断
    if supplier.get("supplier_type") == "制造商":
        log_task(run_id, supplier_id, "veto_non_manufacturer_check", "制造商判断",
                 {"company_name": company_name, "supplier_type": supplier.get("supplier_type")},
                 {"is_manufacturer": True, "reason": "供应商类型标记为制造商"},
                 "数据库supplier_type字段", "success", user_id)
        return False

    # AI进一步判断
    try:
        prompt = f"""请判断以下企业是否为制造商（有实际生产能力）。
只返回JSON：{{"is_manufacturer": true/false, "reason": "判断依据"}}

企业：{company_name}
经营范围：{business_scope}
供应商类型：{supplier.get('supplier_type', '未知')}
主营产品：{supplier.get('main_product', '')}"""
        result_text = call_deepseek(
            [{"role": "user", "content": prompt}],
            scene_code="auto_screening", temperature=0.1, json_mode=True
        )
        result = extract_json_from_text(result_text)
        is_manufacturer = bool(result.get("is_manufacturer", True))  # 默认给True避免误杀
        reason = result.get("reason", "")
    except Exception:
        is_manufacturer = True  # AI失败默认给True
        reason = "AI判断失败，默认保留"

    log_task(run_id, supplier_id, "veto_non_manufacturer_check", "制造商判断",
             {"company_name": company_name},
             {"is_manufacturer": is_manufacturer, "reason": reason},
             "AI语义分析", "success" if is_manufacturer else "success", user_id)
    return not is_manufacturer


def _check_product_mismatch(run_id, supplier_id, supplier, user_id):
    """
    判断产品与经营范围是否明显不匹配（AI语义判断）

    小白讲解：用AI对比采购需求的产品名称与供应商的经营范围/主营产品，
    如果明显不匹配则触发否决。
    """
    req_product = supplier.get("req_product_name", "")
    business_scope = supplier.get("business_scope", "") or supplier.get("intro", "")
    main_product = supplier.get("main_product", "")

    if not req_product or not (business_scope or main_product):
        return False  # 信息不足，不触发否决

    try:
        prompt = f"""请判断供应商的经营范围/主营产品与采购需求是否匹配。
只返回JSON：{{"is_match": true/false, "reason": "判断依据"}}

采购需求产品：{req_product}
供应商经营范围：{business_scope[:300]}
供应商主营产品：{main_product[:200]}"""
        result_text = call_deepseek(
            [{"role": "user", "content": prompt}],
            scene_code="auto_screening", temperature=0.1, json_mode=True
        )
        result = extract_json_from_text(result_text)
        is_match = bool(result.get("is_match", True))  # 默认匹配避免误杀
        reason = result.get("reason", "")
    except Exception:
        is_match = True
        reason = "AI判断失败，默认保留"

    log_task(run_id, supplier_id, "veto_product_mismatch_check", "产品匹配度判断",
             {"req_product": req_product, "business_scope": business_scope[:200]},
             {"is_match": is_match, "reason": reason},
             "AI语义判断", "success", user_id)
    return not is_match


# ==================== 评分规则执行 ====================

def _evaluate_score_rule(rule, eval_data, supplier, tyc_data, run_id, supplier_id, user_id):
    """
    评估单条评分规则，返回得分

    小白讲解：根据规则编码，用对应的评分逻辑计算得分。
    6条评分规则各有不同的计算方式：
    - score_capital_scale：注册资本25分
    - score_operating_years：经营年限15分
    - score_product_match：产品匹配度30分（AI判断）
    - score_contact_complete：联系方式完整度10分
    - score_risk_record：风险记录15分
    - score_export_exp：出口经验5分
    """
    rule_code = rule["rule_code"]
    max_score = rule.get("max_score", 0) or 0

    if rule_code == "score_capital_scale":
        return _score_capital(eval_data.get("reg_capital_wan"), max_score)
    elif rule_code == "score_operating_years":
        return _score_years(eval_data.get("operating_years", 0), max_score)
    elif rule_code == "score_product_match":
        return _score_product_match(rule, eval_data, supplier, max_score,
                                     run_id, supplier_id, user_id)
    elif rule_code == "score_contact_complete":
        return _score_contact(eval_data.get("contact_completeness", "none"), max_score)
    elif rule_code == "score_risk_record":
        return _score_risk(eval_data, max_score)
    elif rule_code == "score_export_exp":
        return _score_export(eval_data, supplier, tyc_data, max_score,
                             run_id, supplier_id, user_id)
    else:
        # 未知评分规则，尝试用条件评估
        condition = parse_condition(rule.get("default_condition", {}))
        if condition and evaluate_condition(condition, eval_data):
            return max_score
        return 0


def _score_capital(capital_wan, max_score=25):
    """注册资本评分：1000万以上满分，100-1000万按比例，100万以下0分"""
    if not capital_wan or capital_wan < 100:
        return 0
    if capital_wan >= 1000:
        return max_score
    # 100-1000万按比例
    return int(max_score * (capital_wan - 100) / 900)


def _score_years(years, max_score=15):
    """经营年限评分：≥5年满分，2-5年按比例，<2年0分"""
    if not years or years < 2:
        return 0
    if years >= 5:
        return max_score
    # 2-5年按比例
    return int(max_score * (years - 2) / 3)


def _score_product_match(rule, eval_data, supplier, max_score, run_id, supplier_id, user_id):
    """产品匹配度评分（AI判断）：完全匹配满分，部分匹配15分，不匹配0分"""
    req_product = supplier.get("req_product_name", "")
    business_scope = eval_data.get("business_scope", "")
    main_product = supplier.get("main_product", "")

    if not req_product:
        return max_score // 2  # 需求信息缺失，给一半分

    try:
        prompt = f"""请评估供应商与采购需求的产品匹配度。
只返回JSON：{{"match_level": "full/partial/none", "reason": "判断依据"}}

采购需求：{req_product}
供应商经营范围：{business_scope[:300]}
供应商主营产品：{main_product[:200]}"""
        result_text = call_deepseek(
            [{"role": "user", "content": prompt}],
            scene_code="auto_screening", temperature=0.1, json_mode=True
        )
        result = extract_json_from_text(result_text)
        level = result.get("match_level", "partial")
        reason = result.get("reason", "")
    except Exception:
        level = "partial"
        reason = "AI判断失败"

    log_task(run_id, supplier_id, "score_product_match_check", "产品匹配度评分",
             {"req_product": req_product},
             {"match_level": level, "reason": reason},
             "AI语义判断", "success", user_id)

    if level == "full":
        return max_score
    elif level == "partial":
        return max_score // 2
    else:
        return 0


def _score_contact(completeness, max_score=10):
    """联系方式完整度评分：both满分，phone_only或email_only给一半，none为0"""
    if completeness == "both":
        return max_score
    elif completeness in ("phone_only", "email_only"):
        return max_score // 2
    return 0


def _score_risk(eval_data, max_score=15):
    """风险记录评分：无风险满分，有少量风险给一半，多项风险0分"""
    risk_indicators = [
        eval_data.get("has_business_exception", False),
        eval_data.get("has_serious_violation", False),
        eval_data.get("is_faithless_person", False),
        eval_data.get("has_judicial_case", False),
    ]
    risk_count = sum(1 for r in risk_indicators if r)

    if risk_count == 0:
        return max_score
    elif risk_count == 1:
        return max_score // 2
    else:
        return 0


def _score_export(eval_data, supplier, tyc_data, max_score, run_id, supplier_id, user_id):
    """出口经验评分：有进出口资质满分，有跨境电商经验满分，否则0分"""
    # 检查资质证书文本中是否含进出口/外贸相关
    qual_text = eval_data.get("qualifications_text", "")
    export_keywords = ["进出口", "外贸", "出口", "海关", "CE", "FCC", "RoHS", "UL", "ETL"]
    has_export_qual = any(kw in qual_text for kw in export_keywords)

    # 检查供应商是否有跨境电商经验
    has_cross_border = bool(supplier.get("has_cross_border_exp", 0))

    # 检查经营范围是否含进出口
    business_scope = eval_data.get("business_scope", "")
    has_export_scope = "进出口" in business_scope or "外贸" in business_scope

    if has_export_qual or has_cross_border or has_export_scope:
        log_task(run_id, supplier_id, "score_export_exp_check", "出口经验评分",
                 {"has_export_qual": has_export_qual,
                  "has_cross_border": has_cross_border,
                  "has_export_scope": has_export_scope},
                 {"has_export_exp": True, "score": max_score},
                 "天眼查资质证书 + 供应商表 + 经营范围综合判断",
                 "success", user_id)
        return max_score

    log_task(run_id, supplier_id, "score_export_exp_check", "出口经验评分",
             {"has_export_qual": False, "has_cross_border": False},
             {"has_export_exp": False, "score": 0},
             "未发现出口经验证据", "success", user_id)
    return 0


# ==================== 结果写回数据库 ====================

def _write_screening_result(supplier, run_id, user_id, conclusion, reason,
                             scores=None, total_score=0,
                             veto_triggered=False, veto_reason="",
                             tyc_data=None, basic_info=None,
                             contact_audit=None, capital_wan=None,
                             establish_date="", operating_status="",
                             business_scope="", match_status=""):
    """
    把初筛结论写回 screenings 表和 suppliers 表

    小白讲解：初筛完成后，把结果存到数据库。
    screenings表记录详细的初筛数据（得分/否决原因/天眼查数据等），
    suppliers表的dev_stage字段更新为"已通过初筛"/"未通过初筛"/"已寻源待初筛"（人工确认的保持原阶段）。
    """
    scores = scores or {}
    tyc_data = tyc_data or {}
    basic_info = basic_info or {}
    contact_audit = contact_audit or {}

    # 小白讲解：用全局写锁串行化写操作，避免与 log_task 的写操作互相阻塞报 database is locked
    print(f"[诊断] _write_screening_result 准备获取写锁, supplier_id={supplier['id']}, conclusion={conclusion}")
    with _db_write_lock:
        print(f"[诊断] _write_screening_result 已获取写锁, 开始写入")
        conn = _get_db_connection()
        cursor = conn.cursor()

        # 先删除该供应商的旧初筛记录（如果有）
        cursor.execute("DELETE FROM screenings WHERE supplier_id = %s", (supplier["id"],))

        # 插入新的初筛记录
        cursor.execute("""
            INSERT INTO screenings
                (supplier_id, risk_score, quality_score, has_cert, is_verified,
                 screener, screen_note,
                 tyc_match_status, tyc_company_name, registered_capital,
                 established_date, operating_status, business_scope,
                 contact_audit_result, run_id,
                 score_capital_scale, score_operating_years, score_product_match,
                 score_contact_complete, score_risk_record, score_export_exp,
                 conclusion,
                 created_at, updated_at, user_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            supplier["id"],
            0,  # risk_score（旧字段，保留兼容）
            total_score,  # quality_score存总分（旧字段复用）
            1 if scores.get("score_export_exp", 0) > 0 else 0,  # has_cert
            1,  # is_verified
            "系统AI初筛",  # screener
            reason,  # screen_note
            match_status,  # tyc_match_status
            basic_info.get("name", supplier.get("name", "")),  # tyc_company_name
            capital_wan,  # registered_capital (REAL, 万元)
            establish_date,  # established_date
            operating_status,  # operating_status
            business_scope[:500],  # business_scope
            contact_audit.get("completeness", "none"),  # contact_audit_result
            run_id,  # run_id
            scores.get("score_capital_scale", 0),
            scores.get("score_operating_years", 0),
            scores.get("score_product_match", 0),
            scores.get("score_contact_complete", 0),
            scores.get("score_risk_record", 0),
            scores.get("score_export_exp", 0),
            conclusion,  # conclusion
            now_str(), now_str(), user_id,
        ))

        # 更新供应商开发阶段
        # 小白讲解：初筛结论决定供应商进入哪个阶段
        if conclusion == "已通过初筛":
            new_stage = "已通过初筛"
        elif conclusion == "未通过初筛":
            new_stage = "未通过初筛"
        else:
            # 需人工确认：保持原阶段"已寻源待初筛"，让用户手动决定
            new_stage = "已寻源待初筛"

        # 同步天眼查工商数据到suppliers表（仅当天眼查有数据且供应商表对应字段为空时才补充）
        # 小白讲解：AI搜索阶段如果天眼查没补全成功，初筛阶段获取到了数据，
        # 就把注册资本/经营状态/成立日期/法人/电话/邮箱回填到供应商基础信息，方便业务人员查看
        tyc_basic = tyc_data.get("basic_info", {}) if tyc_data else {}
        update_fields = ["dev_stage = %s", "updated_at = %s"]
        update_params = [new_stage, now_str()]

        # 注册资本（天眼查返回的原始字符串，如"100万人民币"）
        tyc_capital = tyc_basic.get("registered_capital", "") if tyc_basic else ""
        if tyc_capital and not supplier.get("registered_capital"):
            update_fields.append("registered_capital = %s")
            update_params.append(tyc_capital)

        # 经营状态
        tyc_status = tyc_basic.get("status", "") if tyc_basic else ""
        if tyc_status and (not supplier.get("operating_status") or supplier.get("operating_status") == "工商数据未匹配"):
            update_fields.append("operating_status = %s")
            update_params.append(tyc_status)

        # 成立日期
        tyc_establish = tyc_basic.get("establish_date", "") if tyc_basic else ""
        if tyc_establish and not supplier.get("establish_date"):
            update_fields.append("establish_date = %s")
            update_params.append(tyc_establish)

        # 法定代表人
        tyc_legal = tyc_basic.get("legal_person", "") if tyc_basic else ""
        if tyc_legal and not supplier.get("legal_person"):
            update_fields.append("legal_person = %s")
            update_params.append(tyc_legal)

        # 联系电话
        tyc_phone = tyc_basic.get("phone", "") if tyc_basic else ""
        if tyc_phone and not supplier.get("phone"):
            update_fields.append("phone = %s")
            update_params.append(tyc_phone)

        # 联系邮箱
        tyc_email = tyc_basic.get("email", "") if tyc_basic else ""
        if tyc_email and not supplier.get("email"):
            update_fields.append("email = %s")
            update_params.append(tyc_email)

        # 主营产品（天眼查的经营范围可作为主营产品）
        # 小白讲解：AI搜索阶段DeepSeek推断的main_product可能为空或"待确认"，
        # 初筛时用天眼查的经营范围补充，让业务人员能看到企业实际经营什么
        tyc_scope = tyc_basic.get("business_scope", "") if tyc_basic else ""
        if tyc_scope and (not supplier.get("main_product") or supplier.get("main_product") in ("", "待确认")):
            update_fields.append("main_product = %s")
            update_params.append(tyc_scope[:950])  # VARCHAR(1000)，留余量

        # 供应商简介（天眼查的企业简介+注册资本信息组合）
        # 小白讲解：供应商简介要求包含"注册资本"信息，用天眼查的企业简介+注册资本拼接
        tyc_intro = tyc_basic.get("intro", "") if tyc_basic else ""
        if tyc_intro and (not supplier.get("intro") or supplier.get("intro") in ("", "工商数据未匹配")):
            # 组合简介：企业简介 + 注册资本
            intro_parts = []
            if tyc_intro:
                intro_parts.append(tyc_intro[:500])
            if tyc_capital:
                intro_parts.append(f"注册资本：{tyc_capital}")
            combined_intro = "；".join(intro_parts) if intro_parts else ""
            if combined_intro:
                update_fields.append("intro = %s")
                update_params.append(combined_intro[:950])

        update_params.append(supplier["id"])
        cursor.execute(f"""
            UPDATE suppliers SET {', '.join(update_fields)} WHERE id = %s
        """, tuple(update_params))

        conn.commit()
        conn.close()


# ==================== 辅助函数 ====================

def _safe_subset(data, max_len=200):
    """
    截取数据子集用于审计日志（避免日志过长）

    小白讲解：审计日志要记录输入数据，但完整数据可能很长，
    这个函数把字符串字段截断到200字，避免日志表膨胀。
    """
    subset = {}
    for k, v in data.items():
        if isinstance(v, str) and len(v) > max_len:
            subset[k] = v[:max_len] + "..."
        else:
            subset[k] = v
    return subset
