"""
初筛评分模块 - 实现供应商初筛的评分和通过判断

这个文件实现了工作流文档中"阶段3-供应商初筛"的评分规则。

评分规则（100分制，通过标准见规则配置页的通过线/人工确认线设置）：
1. 一票否决项（触发任一条直接淘汰，不进入评分）
   - 公司经营状态非"存续"
   - 近3年有知识产权侵权诉讼败诉记录
   - 目标平台（亚马逊/Temu等）有确认侵权下架记录

2. 评分项：
   知识产权风险（40分）：
   - 商标查询：无风险20分 / 潜在风险10分 / 明确侵权0分
   - 专利查询：无风险20分 / 潜在风险10分 / 明确侵权0分

   资质与合规（40分）：
   - 目标市场认证：2项及以上20分 / 1项10分 / 无0分
   - 第三方检测报告：有10分 / 无0分
   - 进出口经营权：有10分 / 无0分

   基础条件（20分）：
   - 成立年限：≥5年20分 / 2~5年10分
"""


def calculate_score(form_data, supplier):
    """
    计算初筛得分 - 根据表单数据和供应商信息算分

    参数：
        form_data: 从网页表单收集的初筛数据（字典）
        supplier: 供应商信息（字典，包含经营状态等）

    返回：
        dict: 包含各项得分、总分、是否否决、是否通过的信息
    """
    # 第一步：检查一票否决项
    veto_reason = check_veto(form_data, supplier)

    if veto_reason:
        # 触发一票否决，直接0分不通过
        return {
            "ip_score": 0,
            "qual_score": 0,
            "basic_score": 0,
            "total_score": 0,
            "veto_triggered": 1,
            "passed": 0,
            "veto_reason": veto_reason,
        }

    # 第二步：没有触发否决，开始逐项评分
    ip_score = score_ip_risk(form_data)        # 知识产权得分（满分40）
    qual_score = score_qualification(form_data) # 资质合规得分（满分40）
    basic_score = score_basic_condition(form_data, supplier)  # 基础条件得分（满分20）
    total_score = ip_score + qual_score + basic_score

    # 第三步：判断是否通过（通过标准见规则配置页，旧版≥60分通过已废弃）
    passed = 1 if total_score >= 60 else 0

    return {
        "ip_score": ip_score,
        "qual_score": qual_score,
        "basic_score": basic_score,
        "total_score": total_score,
        "veto_triggered": 0,
        "passed": passed,
        "veto_reason": "",
    }


def check_veto(form_data, supplier):
    """
    检查一票否决项 - 触发任一条直接淘汰

    返回：否决原因字符串，如果没有否决返回空字符串
    """
    # 否决项1：公司经营状态非"存续"
    operating_status = supplier.get("operating_status", "存续") if supplier else "存续"
    if operating_status != "存续":
        return f"公司经营状态为"{operating_status}"，非"存续"状态"

    # 否决项2：近3年有知识产权侵权诉讼败诉记录
    lawsuit = form_data.get("lawsuit_result", "")
    if "败诉" in lawsuit:
        return "近3年有知识产权侵权诉讼败诉记录"

    # 否决项3：目标平台有确认侵权下架记录
    platform_infringe = form_data.get("platform_infringe", "")
    if "下架" in platform_infringe or "确认侵权" in platform_infringe:
        return "目标平台有确认侵权下架记录"

    return ""


def score_ip_risk(form_data):
    """
    知识产权风险评分（满分40分）

    商标查询：无风险20分 / 潜在风险10分 / 明确侵权0分
    专利查询：无风险20分 / 潜在风险10分 / 明确侵权0分
    """
    score = 0

    # 商标查询得分
    trademark = form_data.get("trademark_result", "")
    score += score_risk_level(trademark, 20)

    # 专利查询得分
    patent = form_data.get("patent_result", "")
    score += score_risk_level(patent, 20)

    return score


def score_risk_level(text, max_score):
    """
    根据风险描述打分（通用方法）

    无风险 = 满分
    潜在风险 = 满分的一半
    明确侵权 = 0分
    """
    if not text:
        return 0
    if "无风险" in text or "未发现" in text or "正常" in text:
        return max_score
    if "潜在" in text or "疑似" in text or "可能" in text:
        return max_score // 2  # 满分的一半
    # 明确侵权迹象
    return 0


def score_qualification(form_data):
    """
    资质与合规评分（满分40分）

    目标市场认证：2项及以上20分 / 1项10分 / 无0分
    第三方检测报告：有10分 / 无0分
    进出口经营权：有10分 / 无0分
    """
    score = 0

    # 目标市场认证得分
    cert_auth = form_data.get("cert_authenticity", "")
    cert_count = count_certs(cert_auth)
    if cert_count >= 2:
        score += 20
    elif cert_count == 1:
        score += 10

    # 第三方检测报告
    test_report = form_data.get("test_report", "")
    if "有" in test_report and "无" not in test_report:
        score += 10

    # 进出口经营权
    customs = form_data.get("customs_qualification", "")
    if "有" in customs and "无" not in customs:
        score += 10

    return score


def count_certs(text):
    """
    统计认证数量 - 从认证描述文本中数有多少个认证

    常见认证：CE、FCC、RoHS、UKCA、FDA
    """
    if not text or "无" in text:
        return 0
    cert_keywords = ["CE", "FCC", "RoHS", "UKCA", "FDA", "PSE", "ETL", "CUL", "SAA"]
    count = 0
    for cert in cert_keywords:
        if cert in text:
            count += 1
    return count


def score_basic_condition(form_data, supplier):
    """
    基础条件评分（满分20分）

    成立年限：≥5年20分 / 2~5年10分
    """
    # 优先从表单获取，没有则从供应商信息获取
    years_str = form_data.get("establish_years", "")
    if not years_str and supplier:
        years_str = supplier.get("establish_years", "")

    # 尝试提取数字
    try:
        years = int("".join(c for c in str(years_str) if c.isdigit()))
    except (ValueError, TypeError):
        years = 0

    if years >= 5:
        return 20
    elif years >= 2:
        return 10
    else:
        return 0
