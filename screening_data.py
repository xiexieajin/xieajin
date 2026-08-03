"""
初筛数据查询模块 - 封装天眼查MCP调用、联系方式验证、互联网搜索

小白讲解：这个文件是初筛引擎的「数据采集员」。
初筛需要的所有外部数据都通过这里获取：
- 天眼查工商信息（注册资本/经营状态/成立日期/经营范围/联系方式）
- 天眼查风险总览（经营异常/失信/违法/司法案件）
- 天眼查资质证书（ISO/CE/FCC等）
- 天眼查商标和专利
- 互联网搜索（平台侵权下架记录）

天眼查数据通过HTTP MCP协议获取（三步走流程）：
1. search_companies 搜索公司，拿company_id
2. get_company_capabilities 查询该公司可用的内部工具
3. call_tool 用真实工具名调用具体维度

这个模块继承自 supplier_search.py 的 TianyanchaClient 类，复用已有的基础查询能力。
"""

import re
import time
import difflib
import requests
import pymysql
from supplier_search import TianyanchaClient, _extract_core_name
from config import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE


class ScreeningDataClient(TianyanchaClient):
    """
    初筛数据查询客户端 - 扩展自 TianyanchaClient

    小白讲解：这个类在天眼查客户端基础上，新增了初筛专用的数据查询方法。
    核心是实现了MCP三步走流程中的第2步和第3步：
    - get_company_capabilities：查询公司可用的内部工具
    - call_tool：调用具体维度的工具
    - call_tools_batch：批量调用（最多3个）

    使用流程：
    1. client = ScreeningDataClient()
    2. client.initialize()  # 初始化MCP连接
    3. companies = client.search_companies("百度")  # 搜索公司
    4. caps = client.get_company_capabilities(company_id)  # 查可用工具
    5. risk = client.call_tool("百度", "get_risk_overview")  # 调用风险总览
    """

    def get_company_capabilities(self, company_id, company_name=""):
        """
        查询某家公司可用的内部业务工具清单（三步走第2步）

        小白讲解：天眼查MCP的一个关键设计是——每家公司可用的工具不同！
        所以不能硬编码工具名，必须先调用这个方法拿到真实的工具名清单。
        返回的是Markdown文本，包含"可用工具"和"按需可调用工具"两个表格。

        参数：
            company_id: 企业ID（从search_companies结果中获取）
            company_name: 企业全称（可选，用于展示）
        返回：Markdown文本字符串，包含可用工具清单
        """
        if not self.session_id:
            self.initialize()

        result = self._call("tools/call", {
            "name": "get_company_capabilities",
            "arguments": {
                "company_id": company_id,
                "company_name": company_name,
            },
        }, msg_id=200)

        if not result or "result" not in result:
            return ""

        content = result["result"].get("content", [])
        if content:
            return content[0].get("text", "")
        return ""

    def call_tool(self, company_name, tool_name, arguments=None):
        """
        调用某家公司的指定内部业务工具（三步走第3步）

        小白讲解：拿到capabilities返回的真实工具名后，用这个方法调用具体维度。
        比如 tool_name="get_risk_overview" 查风险总览，
        tool_name="get_qualifications" 查资质证书。

        重要：tool_name 必须从 get_company_capabilities 返回中逐字复制，不能猜测！

        参数：
            company_name: 企业全称（优先使用）
            tool_name: 真实工具名（来自capabilities返回）
            arguments: 工具参数字典（列表工具需传page/page_size）
        返回：工具返回的Markdown文本
        """
        if not self.session_id:
            self.initialize()

        # 小白讲解：天眼查MCP协议规定——
        # company_name 放在顶层参数，arguments 里只保留真正的工具参数（如page/page_size）。
        # 之前把 company_name 塞进 arguments 里，天眼查报错：
        #   "arguments 不需要也不允许包含主体定位参数 company_name"
        # 导致风险总览/资质证书/司法案件三个维度全部查询失败，
        # 风险评分和出口经验评分都基于错误数据给满分。
        call_args = dict(arguments) if arguments else {}

        result = self._call("tools/call", {
            "name": "call_tool",
            "arguments": {
                "company_name": company_name,
                "tool_name": tool_name,
                "arguments": call_args,
            },
        }, msg_id=300)

        if not result or "result" not in result:
            return ""

        content = result["result"].get("content", [])
        if content:
            return content[0].get("text", "")
        return ""

    def call_tools_batch(self, company_name, calls):
        """
        批量调用某家公司的多个内部业务工具（每批最多3个）

        小白讲解：当需要同时查风险总览+资质+司法案件时，用批量调用更高效。
        但注意：每批最多3个工具，且必须是互相独立的（不是探索式追踪）。

        参数：
            company_name: 企业全称
            calls: 调用列表，每项含 tool_name 和 arguments
                如 [{"tool_name":"get_risk_overview","arguments":{}},
                    {"tool_name":"get_qualifications","arguments":{"page":1,"page_size":20}}]
        返回：批量调用的结果文本
        """
        if not self.session_id:
            self.initialize()

        # 限制每批最多3个
        calls = calls[:3]

        result = self._call("tools/call", {
            "name": "call_tools_batch",
            "arguments": {
                "company_name": company_name,
                "calls": calls,
            },
        }, msg_id=400)

        if not result or "result" not in result:
            return ""

        content = result["result"].get("content", [])
        if content:
            return content[0].get("text", "")
        return ""

    def search_trademarks(self, query):
        """
        跨公司搜索商标（顶层工具，直接调用）

        小白讲解：天眼查MCP提供了 search_trademarks 工具，可以直接用企业名搜索商标。
        返回Markdown格式的商标列表。

        参数：query 搜索关键词（通常用企业名）
        返回：Markdown文本
        """
        if not self.session_id:
            self.initialize()

        result = self._call("tools/call", {
            "name": "search_trademarks",
            "arguments": {"query": query, "page": 1, "page_size": 10},
        }, msg_id=500)

        if not result or "result" not in result:
            return ""
        content = result["result"].get("content", [])
        return content[0].get("text", "") if content else ""

    def search_patents(self, query):
        """
        跨公司搜索专利（顶层工具，直接调用）

        参数：query 搜索关键词（通常用企业名）
        返回：Markdown文本
        """
        if not self.session_id:
            self.initialize()

        result = self._call("tools/call", {
            "name": "search_patents",
            "arguments": {"query": query, "page": 1, "page_size": 10},
        }, msg_id=600)

        if not result or "result" not in result:
            return ""
        content = result["result"].get("content", [])
        return content[0].get("text", "") if content else ""


# ==================== 初筛专用高层查询函数 ====================

def _verify_brand_name(orig_name, cand_name):
    """
    字号校验：确认两家公司是否是同一家（防止SequenceMatcher误匹配不同公司）

    小白讲解：中国公司名结构是[地域][字号][行业][组织形式]。
    字号才是公司的唯一标识。"深圳市鼎盛科技有限公司"和"深圳市鼎盛电子科技有限公司"
    相似度很高，但字号"鼎盛科技"≠"鼎盛电子科技"，完全是两家公司！

    校验逻辑：
    1. 剥离地域前缀和组织形式后缀，提取核心字号
    2. 如果一方的字号包含另一方 → 同一家公司（如"浩盈"⊆"浩盈家具"）
    3. 否则 → 不同公司，拒绝匹配

    参数：
        orig_name: 原始搜索名
        cand_name: 天眼查返回的候选公司名
    返回：True=同一家公司，False=不同公司
    """
    orig_core = _extract_core_name(orig_name)
    cand_core = _extract_core_name(cand_name)
    if not orig_core or not cand_core:
        # 有一方提取不到字号（如纯英文名），无法校验，保守返回True
        return True
    # 一方包含另一方即视为同一字号
    return orig_core in cand_core or cand_core in orig_core


def _load_cached_tyc_data(supplier_id):
    """
    从数据库读取搜索阶段缓存的天眼查数据

    小白讲解：搜索阶段调天眼查时，已经把匹配状态/经营范围/注册资本等存到了suppliers表。
    这个函数把这些字段读出来，初筛时直接复用，不用再调天眼查。

    参数：supplier_id 供应商ID
    返回：包含缓存数据的字典，没有数据返回None
    """
    try:
        conn = pymysql.connect(
            host=MYSQL_HOST, port=MYSQL_PORT,
            user=MYSQL_USER, password=MYSQL_PASSWORD,
            database=MYSQL_DATABASE, charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor,
        )
        cursor = conn.cursor()
        cursor.execute("""
            SELECT tyc_match_status, business_scope, tyc_company_id,
                   registered_capital, operating_status, establish_date,
                   phone, email, legal_person, name
            FROM suppliers WHERE id = %s
        """, (supplier_id,))
        row = cursor.fetchone()
        conn.close()
        return row
    except Exception as e:
        print(f"[初筛] 读取缓存天眼查数据失败(supplier_id={supplier_id}): {e}")
        return None


def _rebuild_basic_info_from_db(cached, company_name):
    """
    从数据库字段重建basic_info字典（和天眼查返回格式对齐）

    小白讲解：初筛规则引擎期望basic_info里有注册资本/经营状态/经营范围等字段。
    搜索阶段把这些存到了suppliers表的各字段里，这个函数把它们拼回字典格式，
    让规则引擎不用改代码就能用。

    参数：
        cached: 数据库读出的供应商行
        company_name: 企业名（备用）
    返回：basic_info字典
    """
    return {
        "name": cached.get("name", "") or company_name,
        "registered_capital": cached.get("registered_capital", "") or "",
        "status": cached.get("operating_status", "") or "",
        "establish_date": cached.get("establish_date", "") or "",
        "business_scope": cached.get("business_scope", "") or "",
        "phone": cached.get("phone", "") or "",
        "email": cached.get("email", "") or "",
        "legal_person": cached.get("legal_person", "") or "",
    }


def _query_risk_qual_case(client, company_full_name, result):
    """
    查询初筛所需的3个增量维度：风险总览+资质证书+司法案件

    小白讲解：这是初筛真正需要从天眼查查的数据（搜索阶段没查的）。
    分别调用3次call_tool，每次之间加0.5秒延迟避免频率限制。
    之前还查了capabilities/trademarks/patents，但发现：
    - capabilities：代码注释说不依赖它，直接试调常用工具，所以删掉
    - trademarks/patents：评分规则根本没用到这两个字段，纯属浪费额度，删掉

    参数：
        client: 天眼查客户端
        company_full_name: 企业全称
        result: 结果字典（会往里面写 risk_overview/qualifications/judicial_case）
    """
    # 调用风险总览（核心维度，大部分公司可用）
    risk_text = client.call_tool(company_full_name, "get_risk_overview")
    if risk_text and "请求失败" not in risk_text and "unknown tool" not in risk_text:
        result["risk_overview"] = risk_text
    time.sleep(0.5)

    # 调用资质证书
    qual_text = client.call_tool(
        company_full_name, "get_qualifications",
        {"page": 1, "page_size": 20}
    )
    if qual_text and "请求失败" not in qual_text and "unknown tool" not in qual_text:
        result["qualifications"] = qual_text
    time.sleep(0.5)

    # 调用司法案件（用于检查知识产权侵权败诉，部分公司可用）
    case_text = client.call_tool(
        company_full_name, "get_judicial_case",
        {"page": 1, "page_size": 10}
    )
    if case_text and "请求失败" not in case_text and "unknown tool" not in case_text:
        result["judicial_case"] = case_text


def query_supplier_full_data(company_name, client=None, supplier_id=None):
    """
    查询供应商的完整初筛数据（基础信息+风险+资质+司法案件）

    小白讲解：这是初筛引擎调用的主入口。做了优化——
    如果搜索阶段已经查过天眼查并存到数据库，初筛时直接读库复用basic_info，
    只查风险/资质/司法3个增量维度，省掉search_companies+get_company_basic_profile
    +capabilities+trademarks+patents共5次MCP请求。
    只有手动添加的供应商（数据库没有天眼查数据）才走完整查询流程。

    参数：
        company_name: 企业全称
        client: 可选的ScreeningDataClient实例（传入则复用连接，不传则新建）
        supplier_id: 可选的供应商ID，传了会先读数据库缓存
    返回：供应商数据字典，包含：
        - basic_info: 基础工商信息
        - risk_overview: 风险总览文本
        - qualifications: 资质证书文本
        - judicial_case: 司法案件文本
        - tyc_match_status: 天眼查匹配状态
        - company_id: 企业ID
    """
    # 如果没传client，新建一个
    own_client = False
    if client is None:
        client = ScreeningDataClient()
        client.initialize()
        own_client = True

    result = {
        "basic_info": {},
        "risk_overview": "",
        "qualifications": "",
        "judicial_case": "",
        "tyc_match_status": "not_found",
        "company_id": "",
        "capabilities_text": "",
    }

    # ==================== 优化：优先读库复用搜索阶段的天眼查数据 ====================
    # 小白讲解：搜索阶段已经调过 search_companies + get_company_basic_profile，
    # 结果存在 suppliers 表的 tyc_match_status / business_scope 等字段里。
    # 初筛时直接读库，省掉2次MCP请求（search + profile）。
    # 只有手动添加的供应商（tyc_match_status为空）才触发天眼查补查。
    if supplier_id:
        cached = _load_cached_tyc_data(supplier_id)
        if cached:
            match_status = cached.get("tyc_match_status", "") or ""
            if match_status == "not_found":
                # 搜索阶段已确认天眼查查不到（理论上搜索阶段已剔除，这里兜底）
                result["tyc_match_status"] = "not_found"
                return result
            if match_status and match_status != "error":
                # 匹配成功：从数据库重建basic_info，只查增量维度
                result["tyc_match_status"] = match_status
                result["company_id"] = cached.get("tyc_company_id", "") or ""
                result["basic_info"] = _rebuild_basic_info_from_db(cached, company_name)
                company_full_name = result["basic_info"].get("name", company_name)
                # 只查3个增量维度（风险+资质+司法），省掉search+profile+caps+trademarks+patents
                _query_risk_qual_case(client, company_full_name, result)
                return result
            # match_status为空（手动添加）或error → 走完整流程补查

    # ==================== 完整流程（手动添加的供应商，数据库没有天眼查数据）====================
    try:
        # 第1步：搜索公司，拿company_id
        companies = client.search_companies(company_name)

        # 区分"请求失败"和"确实没找到"
        if companies is None:
            result["tyc_match_status"] = "error"
            result["error"] = "天眼查MCP请求失败（网络超时或频率限制），跳过本次初筛"
            print(f"[初筛] 天眼查请求失败({company_name})，跳过本次初筛，下次可重新初筛")
            return result

        if not companies:
            result["tyc_match_status"] = "not_found"
            return result

        # 严格匹配：1.优先精确同名 2.英文名匹配 3.相似度≥0.6+字号校验
        matched = None
        for company in companies:
            if company.get("name", "").strip() == company_name:
                matched = company
                result["tyc_match_status"] = "exact_match"
                break

        if not matched:
            for company in companies:
                if company.get("match_type", "") == "英文名匹配":
                    matched = company
                    result["tyc_match_status"] = "english_name_match"
                    print(f"[天眼查] 英文名匹配采用：'{company_name}' → '{company.get('name', '')}'")
                    break

        if not matched:
            best_ratio = 0
            best_company = None
            for company in companies:
                cand_name = company.get("name", "").strip()
                if not cand_name:
                    continue
                ratio = difflib.SequenceMatcher(None, company_name, cand_name).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_company = company
            if best_company and best_ratio >= 0.6:
                if _verify_brand_name(company_name, best_company.get("name", "")):
                    matched = best_company
                    result["tyc_match_status"] = "partial_match"
                else:
                    print(f"[天眼查] 字号不匹配({company_name})，候选'{best_company.get('name','')}'，拒绝采用")
                    result["tyc_match_status"] = "not_found"
                    return result
            else:
                print(f"[天眼查] 未匹配({company_name})，最高相似度{best_ratio:.0%}")
                result["tyc_match_status"] = "not_found"
                return result

        result["company_id"] = matched.get("credit_code", "")
        company_full_name = matched.get("name", company_name)

        # 第2步：获取基础工商信息
        basic = client.get_company_basic_profile(company_full_name)
        result["basic_info"] = basic
        time.sleep(0.5)

        # 第3步：查3个增量维度（风险+资质+司法）
        _query_risk_qual_case(client, company_full_name, result)

    except Exception as e:
        result["error"] = str(e)
        print(f"[初筛] 天眼查查询异常({company_name}): {e}")
    finally:
        if own_client:
            pass  # 连接由GC回收，无需显式关闭

    return result


def _extract_tool_names(caps_text):
    """
    从capabilities返回的Markdown文本中提取可用的工具名

    小白讲解：get_company_capabilities返回的是Markdown文本，
    工具名在反引号里，如 `get_risk_overview`。
    这个函数用正则把所有反引号包裹的工具名提取出来。

    参数：caps_text capabilities返回的Markdown文本
    返回：工具名集合，如 {"get_risk_overview", "get_qualifications", ...}
    """
    if not caps_text:
        return set()
    # 匹配反引号包裹的工具名（通常以get_开头）
    tools = re.findall(r'`((?:get_|verify_|check_)\w+)`', caps_text)
    return set(tools)


def parse_risk_detail(risk_text):
    """
    从风险总览文本中解析细分风险维度

    小白讲解：天眼查HTTP MCP没有独立的"经营异常""严重违法失信""失信被执行人"工具，
    这些信息都在 get_risk_overview 返回的总览文本里。
    这个函数用关键字匹配的方式，从文本中提取各细分维度的状态。

    参数：risk_text 风险总览的Markdown文本
    返回：风险维度字典，包含：
        - has_business_exception: 是否有经营异常
        - has_serious_violation: 是否有严重违法失信
        - is_faithless_person: 是否失信被执行人
        - has_judicial_case: 是否有司法案件
        - risk_count: 风险记录数量（估计）
        - detail: 原始文本片段
    """
    detail = {
        "has_business_exception": False,
        "has_serious_violation": False,
        "is_faithless_person": False,
        "has_judicial_case": False,
        "risk_count": 0,
        "detail": risk_text[:500] if risk_text else "",
    }

    if not risk_text:
        return detail

    # 用统一的方法提取每个维度：找关键字后面的数字，>0才为True
    def _check_dimension(keywords):
        """检查指定关键词维度是否有记录（数字>0）"""
        for kw in keywords:
            if kw in risk_text:
                # 找关键词后面的数字（可能隔一些字符）
                match = re.search(kw + r'[^0-9]{0,10}(\d+)', risk_text)
                if match and int(match.group(1)) > 0:
                    return True
        return False

    # 经营异常
    detail["has_business_exception"] = _check_dimension(["经营异常"])
    # 严重违法失信
    detail["has_serious_violation"] = _check_dimension(["严重违法", "严重失信"])
    # 失信被执行人（用整体关键词匹配，避免和"失信"单独匹配到"严重违法失信0条"）
    detail["is_faithless_person"] = _check_dimension(["失信被执行", "被执行人"])
    # 司法案件
    detail["has_judicial_case"] = _check_dimension(["司法案件", "司法文书"])

    # 估计风险总数（从文本中提取所有数字并取较大值）
    numbers = re.findall(r'(\d+)', risk_text)
    if numbers:
        detail["risk_count"] = max(int(n) for n in numbers[:10])  # 取前10个数字中的最大值

    return detail


def parse_capital_wan(capital_str):
    """
    从注册资本字符串中提取数值（万元）

    小白讲解：天眼查返回的注册资本格式多样，如"100万人民币""1000.5万人民币""1亿人民币"。
    这个函数统一转换为万元数值，方便规则引擎做数值比较。

    参数：capital_str 注册资本字符串，如 "100万人民币" 或 "1亿人民币"
    返回：万元数值（float），如 100.0 或 10000.0；无法解析返回 None
    """
    if not capital_str:
        return None

    # 匹配"数字+万"格式，如"100万人民币"→100.0
    wan_match = re.search(r'([\d.]+)\s*万', capital_str)
    if wan_match:
        return float(wan_match.group(1))

    # 匹配"数字+亿"格式，如"1亿人民币"→10000.0（1亿=10000万）
    yi_match = re.search(r'([\d.]+)\s*亿', capital_str)
    if yi_match:
        return float(yi_match.group(1)) * 10000

    # 匹配纯数字（默认按元算，转成万）
    num_match = re.search(r'^([\d.]+)$', capital_str.strip())
    if num_match:
        return float(num_match.group(1)) / 10000

    return None


def parse_established_years(establish_date):
    """
    从成立日期计算经营年限

    小白讲解：天眼查返回成立日期如"2015-03-12"，这个函数计算到当前的经营年限。

    参数：establish_date 成立日期字符串，如"2015-03-12"或"2015年3月"
    返回：经营年限（整数），无法解析返回0
    """
    if not establish_date:
        return 0

    # 提取年份
    year_match = re.search(r'(\d{4})', establish_date)
    if not year_match:
        return 0

    from datetime import datetime
    establish_year = int(year_match.group(1))
    current_year = datetime.now().year
    return current_year - establish_year


def validate_contact_info(phone, email):
    """
    验证联系方式有效性

    小白讲解：检查电话和邮箱格式是否有效。
    电话支持手机号和座机，邮箱检查@符号和域名格式。

    参数：
        phone: 电话号码字符串
        email: 邮箱字符串
    返回：联系方式审计结果字典：
        - has_valid_phone: 是否有有效电话
        - has_valid_email: 是否有有效邮箱
        - has_valid_contact: 是否有任一有效联系方式
        - completeness: 完整度 "both"/"phone_only"/"email_only"/"none"
    """
    result = {
        "has_valid_phone": False,
        "has_valid_email": False,
        "has_valid_contact": False,
        "completeness": "none",
    }

    # 验证电话：手机号 1[3-9]xxxxxxxxx 或 座机 0xx-xxxxxxxx
    if phone:
        phone_clean = phone.strip()
        if re.match(r'^1[3-9]\d{9}$', phone_clean):
            result["has_valid_phone"] = True
        elif re.match(r'^0\d{2,3}-?\d{7,8}$', phone_clean):
            result["has_valid_phone"] = True

    # 验证邮箱：包含@，@后有域名
    if email:
        email_clean = email.strip()
        if re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$', email_clean):
            result["has_valid_email"] = True

    # 综合判断
    result["has_valid_contact"] = result["has_valid_phone"] or result["has_valid_email"]
    if result["has_valid_phone"] and result["has_valid_email"]:
        result["completeness"] = "both"
    elif result["has_valid_phone"]:
        result["completeness"] = "phone_only"
    elif result["has_valid_email"]:
        result["completeness"] = "email_only"

    return result


def search_platform_infringe(company_name):
    """
    互联网搜索平台侵权下架记录（DuckDuckGo）

    小白讲解：天眼查无法提供平台侵权下架记录，需要通过互联网搜索获取。
    用DuckDuckGo搜索企业名+侵权/下架/投诉等关键词，
    返回搜索结果摘要供AI分析。

    参数：company_name 企业名称
    返回：搜索结果文本（标注"互联网来源，可信度需人工复核"）
    """
    try:
        # 构造搜索URL（DuckDuckGo Instant Answer API）
        query = f"{company_name} 侵权 下架 投诉 亚马逊 Temu"
        url = "https://api.duckduckgo.com/"
        params = {
            "q": query,
            "format": "json",
            "no_html": 1,
            "skip_disambig": 1,
        }
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            # 提取相关摘要
            abstract = data.get("Abstract", "")
            related = data.get("RelatedTopics", [])
            results = []
            if abstract:
                results.append(abstract)
            for topic in related[:5]:
                if isinstance(topic, dict) and topic.get("Text"):
                    results.append(topic["Text"])
            if results:
                return f"[互联网来源，可信度需人工复核]\n" + "\n".join(results)
        return f"[互联网来源] 未搜索到{company_name}的平台侵权下架记录"
    except Exception as e:
        return f"[互联网搜索失败] {str(e)}"
