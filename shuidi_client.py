"""
水滴信用 MCP 客户端 - 用于查询供应商的工商/风险/资质信息

小白讲解：这个文件是【水滴信用(shuidi)】MCP的客户端类，作用和
supplier_search.py 里的 TianyanchaClient（天眼查客户端）一模一样，
都是用来查企业的工商信息。

但水滴比天眼查有个优点：它的6个端点（data/risk/supplier/qc/bid/sti）
各自负责不同的数据维度，比如：
- shuidi-data: 查工商照面、股东、资质证书（对应天眼查的 get_company_basic_profile）
- shuidi-risk: 查经营异常、失信、司法案件（对应天眼查的 get_risk_overview）

所以这个客户端内置了【多端点路由】：调用"查工商"自动发到 data 端点，
调用"查风险"自动发到 risk 端点，调用方完全不用关心URL是哪个。

技术要点：
1. 协议：Streamable-HTTP MCP（和天眼查一样是HTTP，不同点是用"key"头鉴权而不是"Authorization"）
2. 响应格式：JSON（非天眼查的Markdown），解析逻辑简单很多
3. 工具列表：15+个，对标天眼查但不需要"先查capabilities再猜工具名"的中转
4. 重试策略：和天眼查一样，3次重试+递增间隔，遇到429触发批次暂停

后续使用：
- 搜索阶段：调用 get_company_info 补全工商信息（替代天眼查 get_company_basic_profile）
- 初筛阶段：调用 search_company_risk 查风险，get_company_cert 查资质（替代天眼查对应工具）
"""

import time
import json
import re
import threading
import requests
from model_config import get_provider


def _clean_capital(capital_str):
    """
    注册资本归一化：统一以"万"为单位 + 去掉小数尾零

    小白讲解：水滴返回的注册资本可能有多种单位，如：
    - "4114113.182万人民币"（已是万）
    - "1.000000亿人民币"（亿 → 换算成万）
    - "500.00万元"（万 + 元币种）
    - "207000000元"（纯元 → 换算成万）
    这个函数把数字统一换算成"万"，并去掉小数尾零，统一后缀为"万人民币/万元"：
    - "4114113.182000万人民币" → "4114113.182万人民币"
    - "1.000000亿人民币" → "10000万人民币"
    - "500.00万元" → "500万元"

    参数：capital_str 原始注册资本字符串（可能为空）
    返回：统一为"万"单位的字符串（空则返回空字符串）
    """
    if not capital_str:
        return capital_str
    import re as _re
    # 匹配"数字 + 单位"（单位如 万人民币/亿人民币/万元/元/人民币）
    match = _re.match(r'^([\d.]+)\s*(.*)$', capital_str, _re.DOTALL)
    if not match:
        return capital_str
    num_part, rest = match.group(1), match.group(2).strip()
    try:
        num = float(num_part)
    except ValueError:
        return capital_str

    has_wan = "万" in rest
    has_yi = "亿" in rest

    # 统一换算为"万"单位
    if has_yi:
        # 1亿 = 10000万
        num = num * 10000.0
    elif not has_wan:
        # 无万无亿 → 视为"元"，转成万（除以10000）
        num = num / 10000.0

    # 去尾零，保留至多6位小数避免浮点误差污染
    num_text = format(num, '.6f').rstrip('0').rstrip('.')
    if num_text == "":
        num_text = "0"

    # 统一币种后缀："万人民币" 或 "万元"
    if "元" in rest:
        currency = "人民币" if "人民币" in rest else "元"
    elif "人民币" in rest:
        currency = "人民币"
    else:
        currency = ""

    if currency == "人民币":
        return f"{num_text}万人民币"
    return f"{num_text}万元"


# ==================== 水滴MCP端点配置 ====================
# 小白讲解：水滴把不同数据维度拆成6个独立MCP端点，避免单个端点过大。
# 这个字典定义了"工具名前缀 → 端点URL"的路由规则，
# 比如 get_company_info → data 端点，search_company_risk → risk 端点。
# 后续要加新工具时，在这里加一行就行，工具调用会自动路由到正确端点。

# 工具分类路由表（key是工具名的前缀，value是对应的服务商代码）
# 小白讲解：根据工具名前缀判断该调哪个端点。
# 比如 get_company_info 前缀是 get_company_info → 命中 "get_company_info*" 规则 → data 端点
# 实际测试发现：水滴的 search_company_risk 在 data 端点上也能调用（risk.data.shuidi.cn 子域名是给特定客户用的），
# 所以默认所有工具走 data 端点最稳妥。
TOOL_ROUTING = {
    # data 端点（包含所有15个工具，包括风险查询）
    "data": [
        "get_company_info",      # 企业工商照面（最核心，替代天眼查的get_company_basic_profile）
        "get_company_partner",   # 股东信息
        "get_company_controller",  # 实际控制人
        "get_company_honor",     # 荣誉资质
        "get_company_cert",      # 资质证书（替代天眼查的get_qualifications）
        "get_company_contact",   # 联系方式（电话、邮箱、网址）
        "get_company_investment",  # 对外投资
        "get_company_benificalowner",  # 受益所有人
        "get_person_related_company",  # 人员关联企业
        "search_established_companies",  # 按地区/时间查新设企业
        "search_companies",      # 按地区/状态查企业列表
        "search_selfemployed",   # 按地区/状态查个体户
        "get_stie_score",        # 科创能力评分
        "search_company_risk",   # 经营风险+司法风险+预警信息（替代天眼查的get_risk_overview+get_judicial_case）
        "query_company_data",    # 自然语言统计查询（如"统计全国各省2024年成立企业"）
    ],
    # risk 端点（默认不路由到这，因为工具在data端点也能调用）
    # 小白讲解：保留 risk 路由备用，如果未来水滴把风险工具从data端点剥离，路由表里已经有对应关系
    "risk": [],
}

# ==================== 水滴MCP 配额耗尽识别 ====================
# 小白讲解：水滴账户额度/订单会用完（业务错误 status_code=30004 "当前订单已消费完毕"）。
# 此时继续逐家调用只是空转，更重要的是它会被误判成"企业查询无结果"而剔除供应商。
# 这里用一个全局标志标记"配额已耗尽"，让上层不再逐家误判剔除。
_shuidi_quota_exhausted = False
# 判定配额耗尽的 message 关键词（兼容多种文案）
_QUOTA_KEYWORDS = (
    "消费完毕", "额度已用完", "配额已用", "次数已用完", "用量已用完",
    "订单已消费", "quota", "exhausted", "用完", "无余额",
)


def is_shuidi_quota_exhausted():
    """判断水滴MCP是否已触发配额耗尽（全局标志）"""
    return _shuidi_quota_exhausted


def _scan_quota_flag(result):
    """
    扫描单次响应，若命中配额耗尽错误则置全局标志并给出明确提示

    参数：result - _call 返回的 MCP 响应 dict（含 result.content[].text）
    """
    global _shuidi_quota_exhausted
    if _shuidi_quota_exhausted:
        return
    if not result or "result" not in result:
        return
    content = result["result"].get("content", [])
    if not content:
        return
    text = content[0].get("text", "")
    if not text:
        return
    try:
        inner = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return
    code = inner.get("status_code") or 0
    msg = str(inner.get("status_message", "") or "")
    # status_code != 1（不是成功）且 message 命中配额关键词 → 判定配额耗尽
    if code != 1 and code != 0 and any(k in msg for k in _QUOTA_KEYWORDS):
        _shuidi_quota_exhausted = True
        print(f"🚨 水滴MCP配额已耗尽(status_code={code}): {msg}")
        print("🚨 后续水滴查询将被跳过；请在服务商管理中补充水滴信用额度或更换Key后重试。")


def _reset_shuidi_quota_flag():
    """重置配额耗尽标志（新批次/更换Key后调用）"""
    global _shuidi_quota_exhausted
    _shuidi_quota_exhausted = False


def _resolve_tool_endpoint(tool_name, providers_config):
    """
    统一使用 data 端点（水滴所有工具都走同一个 shuidi_data 服务商）

    小白讲解：实测水滴的工商/风险/资质等所有工具都能通过 data 端点调用，
    无需再用多个端点。所以这里不再按工具名路由，统一返回 data 端点的
    base_url 和 api_key（对应服务商管理里唯一的一条"水滴信用"记录）。

    参数：
        tool_name: 工具名（保留参数，仅为兼容；不再参与路由）
        providers_config: 服务商配置字典，key是"data"，value是 {base_url, api_key}
    返回：
        (base_url, api_key) 元组；未配置返回 (None, None)
    """
    provider = providers_config.get("data", {}) or {}
    return provider.get("base_url", ""), provider.get("api_key", "")


# ==================== 水滴MCP 批次级限流控制 ====================
# 小白讲解：和天眼查一样的设计 - 429累计3次触发30秒批次暂停
# 避免一个供应商触发了429就杀掉整个批次的所有供应商
_shuidi_429_count = 0                              # 本批次累计429次数（全局）
_shuidi_batch_pause_until = 0.0                    # 批次暂停截止时间戳（全局）
_shuidi_batch_lock = threading.Lock()              # 保护计数器和暂停时间的线程锁
_SHUIDI_429_THRESHOLD = 3                          # 累计3次429触发批次暂停
_SHUIDI_BATCH_PAUSE_SECONDS = 30                   # 批次暂停30秒（给水滴MCP冷却时间）

# ==================== 水滴MCP 调用间隔限速（防止连续调用触发429）===================
# 小白讲解：水滴MCP有频率限制，连续发太多请求会返回429。
# 用全局锁保证两次有效调用之间至少间隔 _SHUIDI_CALL_INTERVAL 秒，
# 避免批量补全几百家供应商时瞬间打爆水滴额度。
_shuidi_pace_lock = threading.Lock()               # 全局间隔锁
_shuidi_last_call_time = 0.0                       # 上次有效调用时间戳
_SHUIDI_CALL_INTERVAL = 1.0                        # 两次调用最小间隔秒数


def _wait_shuidi_pace():
    """
    水滴MCP调用前间隔限速：确保两次调用之间至少间隔 _SHUIDI_CALL_INTERVAL 秒

    小白讲解：所有对水滴的请求在发出去前都调一下这个函数，
    用全局锁"排队"，保证任意时刻最多一个请求在发，且每分钟频率可控。
    """
    global _shuidi_last_call_time
    with _shuidi_pace_lock:
        now = time.time()
        elapsed = now - _shuidi_last_call_time
        if elapsed < _SHUIDI_CALL_INTERVAL:
            wait = _SHUIDI_CALL_INTERVAL - elapsed
            time.sleep(wait)
        _shuidi_last_call_time = time.time()


def _check_shuidi_batch_pause():
    """
    检查水滴MCP批次暂停状态：如果在暂停期就sleep等待到结束

    小白讲解：和水滴版的 _check_tyc_batch_pause 完全一样的作用，
    保证所有线程在水滴"过载"时自动暂停。
    """
    global _shuidi_batch_pause_until
    now = time.time()
    if _shuidi_batch_pause_until > now:
        wait = _shuidi_batch_pause_until - now
        print(f"水滴MCP批次暂停中，等待{wait:.1f}秒...")
        time.sleep(wait)


def _record_shuidi_429():
    """
    记录一次水滴MCP的429响应，累计达到阈值时触发批次暂停

    小白讲解：和水滴版的 _record_tyc_429 完全一样的作用。
    """
    global _shuidi_429_count, _shuidi_batch_pause_until
    with _shuidi_batch_lock:
        _shuidi_429_count += 1
        if _shuidi_429_count >= _SHUIDI_429_THRESHOLD:
            _shuidi_batch_pause_until = time.time() + _SHUIDI_BATCH_PAUSE_SECONDS
            print(f"水滴MCP累计{_shuidi_429_count}次429，触发批次暂停{_SHUIDI_BATCH_PAUSE_SECONDS}秒")
            _shuidi_429_count = 0  # 重置计数，避免下次又立刻触发


def _reset_shuidi_429_counter():
    """
    重置水滴MCP的429计数器（新批次开始时调用）

    小白讲解：每次新批次搜索开始时调用，避免上一次批次的429计数影响本次。
    """
    global _shuidi_429_count
    with _shuidi_batch_lock:
        _shuidi_429_count = 0


class ShuidiClient:
    """
    水滴信用MCP客户端 - 用于查询供应商的工商信息、风险信息、资质证书

    小白讲解：这个类是天眼查 TianyanchaClient 的水滴替代品。
    调用方式保持一致：先 initialize() → 再调用具体工具方法（如 get_company_info）。
    不同点：
    1. 鉴权头是 "key"（不是 "Authorization"）
    2. 工具调用前会自动根据工具名路由到正确的端点（data/risk）
    3. 返回是JSON（不是Markdown），解析逻辑简单

    使用流程：
    1. client = ShuidiClient()
    2. client.initialize()
    3. info = client.get_company_info("深圳市XX科技有限公司")  # 自动路由到data端点
    4. risk = client.search_company_risk("深圳市XX科技有限公司")  # 自动路由到risk端点
    """

    def __init__(self):
        # 小白讲解：水滴所有工具统一走 data 端点，只读取唯一一条"水滴信用"(shuidi_data)服务商记录。
        # 服务商管理里只需配置这一条"水滴信用"，提供 base_url 和 key 即可。
        provider = get_provider("shuidi_data") or {}
        self.providers = {
            "data": {
                "base_url": provider.get("base_url", ""),
                "api_key": provider.get("api_key", ""),
            }
        }

        # 公共请求头（用key头鉴权，不是Authorization）
        # 小白讲解：水滴MCP的鉴权方式和天眼查不同，用"key"头而不是"Authorization"
        # 数据格式：JSON-RPC 2.0（和天眼查一致）
        self.headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-03-26",
        }
        # 注意：每个端点的key不同，所以不能放在通用headers里，
        # 每次请求时根据端点动态设置 self.headers["key"]

    def _call(self, tool_name, arguments=None, msg_id=1):
        """
        调用水滴MCP工具的通用方法（自动路由到正确的端点 + 重试 + 限流处理）

        小白讲解：这个方法是所有工具调用的入口，负责：
        1. 根据 tool_name 自动选择 data/risk 端点
        2. 用对应的 key 头鉴权
        3. 发送JSON-RPC 2.0请求
        4. 失败重试（3次，递增间隔）+ 429批次暂停

        参数：
            tool_name: 工具名，如 "get_company_info"
            arguments: 工具参数字典，如 {"company_name": "xxx公司"}
            msg_id: 请求ID，默认1
        返回：
            - 成功：解析后的JSON字典
            - 请求失败（网络/限流）：返回None
        """
        # 第1步：根据工具名解析端点
        base_url, api_key = _resolve_tool_endpoint(tool_name, self.providers)
        if not base_url or not api_key:
            print(f"水滴MCP的{tool_name}路由未配置端点URL或Key，请检查服务商配置")
            return None

        # 第2步：构造请求（设置当前端点的key头）
        headers = dict(self.headers)
        headers["key"] = api_key

        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments or {},
            },
            "id": msg_id,
        }

        # 第3步：批次暂停检查（429触发后自动等待）+ 全局调用间隔限速
        _check_shuidi_batch_pause()
        _wait_shuidi_pace()

        # 小白讲解：若已确认配额耗尽，直接返回，不再发请求空转
        if is_shuidi_quota_exhausted():
            return None

        # 第4步：重试发送请求（最多3次，递增间隔）
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                # 小白讲解：用 bytes 直接发送 JSON，确保中文不丢编码
                # 响应也要强制按 utf-8 解码（避免水滴响应头没charset导致中文乱码）
                body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                resp = requests.post(
                    base_url, headers=headers, data=body_bytes, timeout=30
                )
                # 强制 utf-8 解码响应（避免 requests 默认按 latin-1 解读）
                resp.encoding = "utf-8"

                # 429频率限制：等待后重试
                if resp.status_code == 429:
                    _record_shuidi_429()
                    if attempt < max_retries:
                        wait = attempt * 3
                        print(f"水滴MCP频率限制(429)，第{attempt}/{max_retries}次重试，等待{wait}秒...")
                        time.sleep(wait)
                        _check_shuidi_batch_pause()
                        continue
                    else:
                        print(f"水滴MCP频率限制(429)，已重试{max_retries}次仍失败")
                        return None

                # 5xx服务器错误：等待后重试
                if 500 <= resp.status_code < 600:
                    if attempt < max_retries:
                        wait = attempt * 3
                        print(f"水滴MCP服务器错误({resp.status_code})，第{attempt}/{max_retries}次重试，等待{wait}秒...")
                        time.sleep(wait)
                        continue
                    else:
                        print(f"水滴MCP服务器错误({resp.status_code})，已重试{max_retries}次仍失败")
                        return None

                # 其他4xx错误：不重试
                if resp.status_code >= 400:
                    print(f"水滴MCP返回HTTP {resp.status_code}: {resp.text[:300]}")
                    return None

                # 解析响应（可能是JSON或SSE格式）
                ct = resp.headers.get("content-type", "")
                result = None
                if "event-stream" in ct:
                    for line in resp.text.split("\n"):
                        if line.startswith("data: ") and line[6:].strip():
                            try:
                                result = json.loads(line[6:].strip())
                            except json.JSONDecodeError:
                                pass
                            break
                else:
                    if resp.text.strip():
                        try:
                            result = resp.json()
                        except json.JSONDecodeError:
                            pass

                # 小白讲解：扫描响应，若命中"配额耗尽"错误则置全局标志
                _scan_quota_flag(result)
                return result

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                # 网络超时：等待后重试
                if attempt < max_retries:
                    wait = attempt * 3
                    print(f"水滴MCP网络超时({tool_name})，第{attempt}/{max_retries}次重试，等待{wait}秒...: {e}")
                    time.sleep(wait)
                    continue
                else:
                    print(f"水滴MCP网络超时({tool_name})，已重试{max_retries}次仍失败: {e}")
                    return None
            except requests.exceptions.RequestException as e:
                print(f"水滴MCP请求失败({tool_name}): {e}")
                return None
            except json.JSONDecodeError as e:
                print(f"水滴MCP响应解析失败({tool_name}): {e}")
                return None

        return None

    def initialize(self):
        """
        初始化连接（占位方法，保持和天眼查客户端的接口一致）

        小白讲解：天眼查MCP需要先initialize拿到session ID才能发工具调用，
        水滴MCP（Streamable-HTTP）每个请求自带鉴权key，不需要维持session，
        所以这个方法留空不做事，只是为了和天眼查客户端接口对齐。
        """
        # 检查至少data端点已配置
        if not self.providers.get("data", {}).get("base_url"):
            print("水滴MCP的data端点未配置，请检查服务商配置（provider_code=shuidi_data）")
            return False
        return True

    # ==================== 业务方法：调用具体工具 ====================

    def get_company_info(self, company_name):
        """
        查询企业工商照面信息（替代天眼查的 get_company_basic_profile）

        小白讲解：这是水滴最核心的工具，一次性返回企业的：
        - 企业名称、统一社会信用代码、法定代表人、注册资本
        - 成立日期、企业类型、所属行业、经营状态
        - 注册地址、经营范围、联系方式（电话/邮箱）
        - 登记机关、核准日期等

        严格校验：水滴没有 match_type 字段，初筛代码必须自己用 searched_company 做严格校验。
        如果水滴返回的公司名（searched_company）和我们传入的不匹配，返回空字典当作"查不到"处理。

        参数：company_name - 企业全称
        返回：字典，解析后的工商信息；失败/校验不通过返回空字典{}
        """
        result = self._call("get_company_info", {"company_name": company_name}, msg_id=100)
        if not result or "result" not in result:
            return {}

        content = result["result"].get("content", [])
        if not content:
            return {}

        # 水滴的响应是结构化JSON（外层包了一层 {"text": "..."}）
        text_content = content[0].get("text", "")
        if not text_content:
            return {}

        # 解析JSON文本
        try:
            data = json.loads(text_content)
        except (json.JSONDecodeError, TypeError):
            return {"raw": text_content[:3000]}

        # ==================== 严格校验：防止水滴模糊匹配误中 ====================
        # 小白讲解：水滴返回的 searched_company 是它"实际查到"的公司，
        # 如果和输入的公司名不匹配（比如"鑫鑫塑料"匹配到"博鑫亮塑料"），
        # 这种数据不能信任，必须返回空字典让上层当作"查不到"处理。

        # 第一道校验：status_code 必须 == 1（业务查询成功）
        # 小白讲解：水滴即使没找到数据（status_code=2），也会返回 searched_company=输入名，
        # 这种情况下严格校验"完全相等"会通过，但 data 是空的。
        # 必须先看 status_code，否则会被"假命中"骗到。
        status_code = data.get("status_code", 0)
        if status_code != 1:
            print(f"  ⚠️ [水滴未命中] 输入='{company_name}' status_code={status_code} message={data.get('status_message', '-')}")
            return {}

        # 第二道校验：data 字段必须是 dict 且非空
        # 小白讲解：有时候 status_code=1 但 data={}（边缘情况），这种也不能算命中
        inner_data = data.get("data", {})
        if not isinstance(inner_data, dict) or not inner_data:
            print(f"  ⚠️ [水滴空数据] 输入='{company_name}' data 字段为空")
            return {}

        # 第三道校验：searched_company 严格匹配（防模糊误中）
        searched_company = data.get("searched_company", "")
        is_match, reason = self._verify_brand_match(company_name, searched_company)
        if not is_match:
            print(f"  ⚠️ [严格校验拒绝] 输入='{company_name}' 水滴返回='{searched_company}' 原因={reason}")
            return {}

        return self._parse_company_info(data, company_name)

    def search_company_risk(self, company_name):
        """
        查询企业风险信息（替代天眼查的 get_risk_overview + get_judicial_case）

        小白讲解：水滴把所有风险维度放在一个工具里，包括：
        - 经营风险（经营异常、严重违法、严重失信）
        - 司法风险（立案、开庭、判决、执行）
        - 预警信息

        严格校验：风险数据如果来自误中的公司，可能让初筛误判。
        所以同样要用 searched_company 做校验，不匹配返回空字典。

        参数：company_name - 企业全称
        返回：字典，包含 risk_count / risk_types 等结构化风险数据；
              失败/校验不通过/无风险返回空字典{}
        """
        result = self._call("search_company_risk", {"company_name": company_name}, msg_id=200)
        if not result or "result" not in result:
            return {}

        content = result["result"].get("content", [])
        if not content:
            return {}

        text_content = content[0].get("text", "")
        if not text_content:
            return {}

        try:
            data = json.loads(text_content)
        except (json.JSONDecodeError, TypeError):
            return {"raw": text_content[:3000]}

        # 三道校验（status_code == 1 + data非空 + searched_company匹配）
        status_code = data.get("status_code", 0)
        if status_code != 1:
            print(f"  ⚠️ [水滴未命中-风险] 输入='{company_name}' status_code={status_code}")
            return {}
        inner_data = data.get("data", {})
        if not isinstance(inner_data, dict) or not inner_data:
            print(f"  ⚠️ [水滴空数据-风险] 输入='{company_name}'")
            return {}

        searched_company = data.get("searched_company", "")
        is_match, reason = self._verify_brand_match(company_name, searched_company)
        if not is_match:
            print(f"  ⚠️ [严格校验拒绝-风险] 输入='{company_name}' 水滴返回='{searched_company}' 原因={reason}")
            return {}

        return self._parse_risk_info(data)

    def get_company_cert(self, company_name):
        """
        查询企业资质证书（替代天眼查的 get_qualifications）

        小白讲解：返回企业的资质证书列表（ISO/CE/FCC等）。

        严格校验：资质证书数据如果来自误中的公司，可能让评分规则误判。
        所以同样要做 searched_company 校验，不匹配返回空字典。

        参数：company_name - 企业全称
        返回：字典，包含证书列表；失败/校验不通过返回空字典{}
        """
        result = self._call("get_company_cert", {"company_name": company_name}, msg_id=300)
        if not result or "result" not in result:
            return {}

        content = result["result"].get("content", [])
        if not content:
            return {}

        text_content = content[0].get("text", "")
        if not text_content:
            return {}

        try:
            data = json.loads(text_content)
        except (json.JSONDecodeError, TypeError):
            return {"raw": text_content[:3000]}

        # 三道校验（status_code == 1 + data非空 + searched_company匹配）
        status_code = data.get("status_code", 0)
        if status_code != 1:
            print(f"  ⚠️ [水滴未命中-证书] 输入='{company_name}' status_code={status_code} message={data.get('status_message', '-')}")
            return {}
        inner_data = data.get("data", {})
        if not isinstance(inner_data, dict) or not inner_data:
            print(f"  ⚠️ [水滴空数据-证书] 输入='{company_name}'")
            return {}

        searched_company = data.get("searched_company", "")
        is_match, reason = self._verify_brand_match(company_name, searched_company)
        if not is_match:
            print(f"  ⚠️ [严格校验拒绝-证书] 输入='{company_name}' 水滴返回='{searched_company}' 原因={reason}")
            return {}

        return data

    def get_company_contact(self, company_name):
        """
        查询企业联系方式（电话/邮箱/网站） - 水滴单独的 get_company_contact 工具

        小白讲解：水滴的 get_company_info 不返回电话/邮箱，需要单独调用 get_company_contact。
        实测返回结构：{"phones":[{"phone":"0757-...","data_source":"2025年报"}],
                        "emails":[{"email":"...@qq.com","data_source":"2023年报"}],
                        "websites":[{"website":"..."}]}

        也做严格校验（searched_company 不匹配拒绝），返回空字典。

        参数：company_name - 企业全称
        返回：字典，含 phone / email / websites；失败/校验不通过返回空字典
        """
        result = self._call("get_company_contact", {"company_name": company_name}, msg_id=900)
        if not result or "result" not in result:
            return {}

        content = result["result"].get("content", [])
        if not content:
            return {}

        text_content = content[0].get("text", "")
        if not text_content:
            return {}

        try:
            data = json.loads(text_content)
        except (json.JSONDecodeError, TypeError):
            return {"raw": text_content[:3000]}

        # 三道校验
        status_code = data.get("status_code", 0)
        if status_code != 1:
            print(f"  ⚠️ [水滴未命中-联系方式] 输入='{company_name}' status_code={status_code}")
            return {}
        inner_data = data.get("data", {})
        if not isinstance(inner_data, dict) or not inner_data:
            print(f"  ⚠️ [水滴空数据-联系方式] 输入='{company_name}'")
            return {}

        searched_company = data.get("searched_company", "")
        is_match, reason = self._verify_brand_match(company_name, searched_company)
        if not is_match:
            print(f"  ⚠️ [严格校验拒绝-联系方式] 输入='{company_name}' 水滴返回='{searched_company}' 原因={reason}")
            return {}

        # 解析 phones/emails/websites
        contact = {"phone": "", "email": "", "websites": []}
        phones = inner_data.get("phones", []) or []
        for p in phones:
            if isinstance(p, dict) and p.get("phone"):
                contact["phone"] = p["phone"]
                break
        emails = inner_data.get("emails", []) or []
        for e in emails:
            if isinstance(e, dict) and e.get("email"):
                contact["email"] = e["email"]
                break
        websites = inner_data.get("websites", []) or []
        for w in websites:
            if isinstance(w, dict) and w.get("website"):
                contact["websites"].append(w["website"])
        return contact

    def search_companies(self, keyword, **kwargs):
        """
        按关键词搜索企业列表（替代天眼查的 search_companies）

        小白讲解：水滴的search_companies和天眼查的语义不太一样：
        - 天眼查：传公司名，返回候选公司列表（用于精确匹配）
        - 水滴：传地区/状态/行业等筛选条件，返回企业列表（用于按条件筛选）

        所以这里方法名沿用但语义不同，调用方需要根据实际场景使用。

        参数：keyword 关键词（公司名/地区/行业等）
        返回：企业列表，失败返回None
        """
        arguments = {"keyword": keyword}
        arguments.update(kwargs)
        result = self._call("search_companies", arguments, msg_id=400)
        if not result or "result" not in result:
            return None

        content = result["result"].get("content", [])
        if not content:
            return []

        text_content = content[0].get("text", "")
        try:
            return json.loads(text_content)
        except (json.JSONDecodeError, TypeError):
            return {"raw": text_content[:3000]}

    # ==================== 响应解析方法 ====================

    def _extract_core_name(self, name):
        """
        提取公司名的核心字号（去除地域/括号/组织形式）

        小白讲解：复用天眼查的逻辑（[screening_data.py:41](file:///d:/pycharm/供应商寻源系统/screening_data.py#L41-L46)），
        用于做严格校验前先把两个公司名归一化成"核心字号"。
        比如"深圳市华为技术有限公司"和"华为技术有限公司"的核心字号都是"华为技术"。

        参数：name 原始公司名
        返回：核心字号字符串
        """
        if not name:
            return ""
        # 去括号（中英文括号都处理，去掉括号及内容）
        core = re.sub(r'[（(].*?[)）]', '', name)
        # 去组织形式后缀（长的先去，避免"有限责任公司"被截成"有限责任"）
        for suffix in ['有限责任公司', '股份有限公司', '有限公司', '股份公司', '责任公司',
                       '制品厂', '加工厂', '制造厂', '家具厂', '玻璃厂', '五金厂',
                       '电子厂', '塑胶厂', '模具厂', '机械厂', '金属制品厂', '木业厂',
                       '建材厂', '陶瓷厂', '厂']:
            if core.endswith(suffix):
                core = core[:-len(suffix)]
                break
        # 去地域前缀（省/市/区/县，全部剥离）
        core = re.sub(r'[\u4e00-\u9fa5]{2,4}(?:省|市|区|县)', '', core)
        return core.strip().lower()

    def _verify_brand_match(self, input_name, searched_name, strict=False):
        """
        严格校验：判断水滴返回的 searched_company 和我们输入的公司名是否是同一家

        小白讲解：水滴没有 match_type 字段，初筛代码必须自己用 searched_company 做校验。
        校验逻辑（从宽松到严格）：
        1. 完全相等 → 同一家
        2. 核心字号包含关系（如"鑫鑫" ⊆ "鑫鑫塑料"）→ 同一家
        3. 都不匹配 → 不是同一家，拒绝返回数据

        测试案例：
        - 输入"华为技术有限公司" vs 水滴返回"华为技术有限公司" → ✅ 是同一家
        - 输入"鑫鑫塑料制品厂" vs 水滴返回"博鑫亮塑料制品厂" → ❌ 不是同一家
        - 输入"Apple Inc." vs 水滴返回"APPLE AVE INC.,LIMITED" → ❌ 不是同一家

        参数：
            input_name: 我们传给水滴的公司名（来自1688/MIC搜索结果）
            searched_name: 水滴实际查到的公司名（响应里的 searched_company 字段）
            strict: 严格模式（默认False，留作后续扩展）
        返回：
            (is_match: bool, reason: str)
            - is_match=True 表示可以信任水滴的返回数据
            - is_match=False 表示这是误中，应当拒绝
        """
        if not input_name or not searched_name:
            return False, "输入或返回公司名为空"

        # 完全相等（最快路径）
        if input_name.strip() == searched_name.strip():
            return True, "完全相等"

        # 核心字号包含关系
        # 小白讲解：复用天眼查的 _verify_brand_name 逻辑——
        # 双方去掉地域前缀和组织形式后缀后，看一方的核心字号是否包含另一方。
        # 比如"华为" ⊆ "华为技术" → 同一家
        input_core = self._extract_core_name(input_name)
        searched_core = self._extract_core_name(searched_name)

        if not input_core or not searched_core:
            return False, "核心字号提取失败"

        # 一方包含另一方（任一方向包含即可视为同一家）
        if input_core in searched_core or searched_core in input_core:
            return True, "核心字号包含"

        # 都不匹配 → 不是同一家
        return False, f"核心字号不匹配（输入核心='{input_core}'，水滴核心='{searched_core}'）"

    def _parse_company_info(self, data, company_name):
        """
        把水滴get_company_info返回的JSON解析成统一格式的字典

        小白讲解：水滴返回的字段名可能略有差异（中文/英文混合），
        这里统一映射成和天眼查一致的字段名（registered_capital / status / phone / email），
        让上层代码（搜索/初筛引擎）不用改就能用。

        重要：水滴的响应有两层 data！最外层是MCP标准格式（jsonrpc/result/content），
        我们传入的 data 已经是内层 text 解析后的字典，
        但真正的工商字段还在 data["data"] 这一层下。
        第一次测试时这个bug导致所有工商字段都为空。

        参数：
            data: 水滴内层text解析后的字典（含 status_code / data 字段）
            company_name: 兜底用的公司名（响应里没公司名时用）
        返回：统一格式的工商信息字典
        """
        if not isinstance(data, dict):
            return {}

        # 提取真正的工商数据层
        # 小白讲解：data 是 text 解析后的内容，里面 status_code / data / searched_company 都是顶层，
        # 但 capital / legal_person 这些工商字段在 data["data"] 里。
        # 修复前直接 data.get("legal_person") 拿到空字符串，因为字段在 data["data"] 下
        inner_data = data.get("data", {}) if isinstance(data.get("data"), dict) else data

        # 注册资本：去尾零（如 "4114113.182000万人民币" → "4114113.182万人民币"）
        raw_capital = inner_data.get("capital") or inner_data.get("registered_capital") or inner_data.get("regCapital") or inner_data.get("注册资本") or ""
        clean_capital = _clean_capital(raw_capital)

        # 小白讲解：字段映射表 - 水滴的字段名 → 我们系统统一的字段名
        # 这样上层代码不用关心数据源是天眼查还是水滴
        result = {
            "name": inner_data.get("company_name") or inner_data.get("name") or company_name,
            "credit_code": inner_data.get("credit_code") or inner_data.get("credit_no") or inner_data.get("统一社会信用代码") or "",
            "legal_person": inner_data.get("legal_person") or inner_data.get("legalPerson") or inner_data.get("法定代表人") or "",
            "registered_capital": clean_capital,
            "establish_date": inner_data.get("establish_date") or inner_data.get("establishDate") or inner_data.get("成立日期") or "",
            "status": inner_data.get("company_status") or inner_data.get("status") or inner_data.get("operating_status") or inner_data.get("经营状态") or "",
            "company_type": inner_data.get("company_type") or inner_data.get("entType") or inner_data.get("企业类型") or "",
            "address": inner_data.get("company_address") or inner_data.get("address") or inner_data.get("regAddress") or inner_data.get("注册地址") or "",
            "business_scope": inner_data.get("business_scope") or inner_data.get("businessScope") or inner_data.get("经营范围") or "",
            "industry": inner_data.get("industry") or inner_data.get("所属行业") or "",
            "phone": inner_data.get("phone") or inner_data.get("联系电话") or "",
            "email": inner_data.get("email") or inner_data.get("邮箱") or "",
            # 整段原始数据兜底备用
            "raw": json.dumps(data, ensure_ascii=False)[:3000],
        }
        return result

    def _parse_risk_info(self, data):
        """
        把水滴search_company_risk返回的JSON解析成风险维度字典

        小白讲解：水滴把所有风险信息放在一个工具里，
        这里按照初筛引擎期望的格式拆开：
        - has_business_exception: 是否有经营异常
        - has_serious_violation: 是否有严重违法
        - is_faithless_person: 是否失信被执行人
        - has_judicial_case: 是否有司法案件
        - risk_count: 风险记录总数估计
        - detail: 原始摘要文本

        参数：data 水滴返回的JSON数据
        返回：风险维度字典
        """
        if not isinstance(data, dict):
            return {}

        detail = {
            "has_business_exception": False,
            "has_serious_violation": False,
            "is_faithless_person": False,
            "has_judicial_case": False,
            "risk_count": 0,
            "detail": "",
        }

        # 把整个JSON转成字符串，用关键字匹配来判断各风险维度
        # 小白讲解：水滴响应是结构化的，但字段名可能有中英文差异，
        # 用关键字匹配最稳健（兼容任何字段命名）
        text = json.dumps(data, ensure_ascii=False)

        def _check_dimension(keywords):
            """检查指定关键词维度是否有记录"""
            for kw in keywords:
                if kw in text:
                    # 找关键词后面的数字（可能隔一些字符）
                    match = re.search(kw + r'[^0-9]{0,15}(\d+)', text)
                    if match and int(match.group(1)) > 0:
                        return True
            return False

        # 各风险维度判断（关键字兼容中英文）
        detail["has_business_exception"] = _check_dimension(["经营异常", "businessException"])
        detail["has_serious_violation"] = _check_dimension(["严重违法", "seriousViolation", "严重失信"])
        detail["is_faithless_person"] = _check_dimension(["失信被执行", "faithless", "被执行人"])
        detail["has_judicial_case"] = _check_dimension(["司法案件", "judicialCase", "司法文书", "开庭公告"])

        # ==================== 计算该公司真实风险总数 ====================
        # 小白讲解：水滴返回 data.self_risk.risk_count 是"该公司自我风险"的总数，
        # 或 data.self_risk.events[] 里每个事件类型的 event_count。
        # 之前用"取全文本最大数字"会误把"周边风险/市场统计"的全局大数(如829929)当作该公司风险，
        # 导致风险评分虚高。改成只统计该公司自身风险事件的计数总和。
        self_risk = {}
        if isinstance(data.get("data"), dict):
            self_risk = data["data"].get("self_risk", {}) or {}
        risk_count = 0
        # 方式1：直接取 self_risk.risk_count（若它是合理小值）
        direct_count = self_risk.get("risk_count", 0)
        try:
            direct_count = int(direct_count)
        except (TypeError, ValueError):
            direct_count = 0
        # 方式2：累加 events[].event_count（该公司的各类风险条数）
        events = []
        if isinstance(self_risk.get("events"), list):
            events = self_risk["events"]
        event_sum = 0
        for ev in events:
            if isinstance(ev, dict):
                try:
                    event_sum += int(ev.get("event_count", 0) or 0)
                except (TypeError, ValueError):
                    pass
        # 取两者中合理的那个：直接数若在事件和附近则用，否则用事件和
        if direct_count and abs(direct_count - event_sum) <= max(200, event_sum):
            risk_count = direct_count
        else:
            risk_count = event_sum if event_sum else direct_count
        if risk_count > 0:
            detail["risk_count"] = risk_count

        detail["detail"] = text[:1000]
        # 小白讲解：保留完整原始JSON文本（不小于4000字符），
        # 供初筛 parse_risk_detail 做关键字匹配（"经营异常/严重违法/失信"等）。
        # 之前只截1000导致部分风险关键词被截掉，初筛误判为无风险。
        detail["raw"] = text[:4000]
        return detail


# ==================== 便捷测试函数 ====================

def test_shuidi_company_info(company_name):
    """
    快速测试：调用 get_company_info 看能不能拿到数据

    小白讲解：这是一个独立的测试函数，不需要数据库和Flask环境。
    直接 python shuidi_client.py 就能跑，验证水滴key是否可用。
    """
    print(f"\n===== 水滴MCP测试：查询 {company_name} =====")
    client = ShuidiClient()
    if not client.initialize():
        print("初始化失败，请检查 ai_providers 表里 shuidi_data 的 base_url 和 api_key")
        return None

    info = client.get_company_info(company_name)
    if not info:
        print("查询失败：返回空字典（可能是key无效、网络问题或该公司不存在）")
        return None

    print("\n查询成功！返回字段：")
    for key, value in info.items():
        if key == "raw":
            continue  # raw字段太长，不打印
        print(f"  {key}: {value}")

    return info


if __name__ == "__main__":
    # 直接运行本文件时执行：测试一个真实公司
    # 小白讲解：可以修改这里的公司名测试其他企业
    test_shuidi_company_info("深圳市华为技术有限公司")