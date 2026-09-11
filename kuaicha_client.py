"""
快查365 MCP 客户端 - 用于查询供应商的工商信息（含电话/邮箱/官网）

小白讲解：这个文件是【快查365(kuaicha)】MCP的客户端类，作用和
supplier_search.py 里的 TianyanchaClient（天眼查客户端）一样，用来查企业的工商信息，
但快查有一个天眼查没有的优势：**能用英文公司名直接模糊搜索匹配到中文企业**，
而且一次调用就返回电话/邮箱/官网（天眼查查不到联系方式时还要再调水滴补）。

在系统里的定位（企业信息查询优先级链）：
- 中文供应商：水滴(首选) → 快查(降级) → 天眼查(兜底)
- 英文供应商：快查(首选) → 天眼查(兜底)

技术要点：
1. 协议：Streamable-HTTP MCP（和天眼查/水滴一样），鉴权用 open-authorization: Bearer <KEY>
2. 架构特殊：只有 discover(找工具) + call(调工具) 两个元工具，
   call 的参数是 tool_id + params_to_tool(JSON字符串)，不是直接传工具名
3. 核心工具：basic_get_enterprise_associate（根据关键词模糊搜索匹配企业），
   一次调用返回：corp_name/orgid/creditcode/注册资本/法人/地址/电话/邮箱/官网/经营范围/成立日期
4. 成功标志：status_code == 0（注意！水滴是 ==1，快查是 ==0，别搞混）
5. 限流：全局1秒调用间隔（防429），失败重试3次

后续使用：
- 搜索阶段：调用 get_company_info 补全工商信息（英文供应商首选，中文供应商作水滴降级）
"""

import json
import time
import threading
import requests
from model_config import get_provider
from shuidi_client import _clean_capital


# ==================== 快查MCP 调用间隔限速（防止连续调用触发429）====================
# 小白讲解：快查网关有频率限制，连续发太多请求会返回429。
# 用全局锁保证两次有效调用之间至少间隔 _KUAICHA_CALL_INTERVAL 秒。
_kuaicha_pace_lock = threading.Lock()
_kuaicha_last_call_time = 0.0
_KUAICHA_CALL_INTERVAL = 1.0


def _wait_kuaicha_pace():
    """
    快查MCP调用前间隔限速：确保两次调用之间至少间隔1秒

    小白讲解：所有对快查的请求在发出去前都调一下这个函数，
    用全局锁"排队"，保证任意时刻最多一个请求在发，频率可控。
    """
    global _kuaicha_last_call_time
    with _kuaicha_pace_lock:
        now = time.time()
        elapsed = now - _kuaicha_last_call_time
        if elapsed < _KUAICHA_CALL_INTERVAL:
            time.sleep(_KUAICHA_CALL_INTERVAL - elapsed)
        _kuaicha_last_call_time = time.time()


class KuaichaClient:
    """
    快查365MCP客户端 - 用于查询供应商的工商信息（英文名优先匹配）

    小白讲解：这个类是英文供应商工商补全的首选数据源。
    调用方式：initialize() 握手 → get_company_info(公司名) 一次拿全工商+联系方式。

    使用流程：
    1. client = KuaichaClient()
    2. client.initialize()
    3. info = client.get_company_info("Guangzhou Wankai Furniture Co., Ltd.")
       # info 里直接有 name/phone/email/website/registered_capital 等字段
    """

    def __init__(self):
        # 小白讲解：快查统一走 kuaicha_data 服务商记录（base_url + api_key）
        provider = get_provider("kuaicha_data") or {}
        self.mcp_url = provider.get("base_url", "")
        self.api_key = provider.get("api_key", "")
        self.is_enabled = bool(provider.get("is_enabled"))
        self.session_id = None

        # 公共请求头（用 open-authorization 头鉴权，兼容 Authorization）
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-03-26",
        }
        if self.api_key:
            self.headers["open-authorization"] = f"Bearer {self.api_key}"

    def is_available(self):
        """
        判断快查服务商是否可用（启用了且配置了地址和Key）

        小白讲解：主流程在构建优先级链前会先调用这个方法，
        服务商在后台被停用（is_enabled=0）或没填Key时，快查直接不参与优先级。
        """
        return bool(self.is_enabled and self.mcp_url and self.api_key)

    def initialize(self):
        """
        初始化连接：MCP握手获取 session id

        小白讲解：Streamable-HTTP 协议第一步是 initialize 握手，
        服务器会在响应头返回 mcp-session-id，后续工具调用带上它维持会话。
        快查网关不强制要求 session（每次请求都带鉴权Key），
        但带上 session 更稳，和实测行为一致。
        """
        if not self.mcp_url or not self.api_key:
            print("快查MCP未配置（base_url/api_key），请检查服务商配置（provider_code=kuaicha_data）")
            return False
        try:
            payload = {
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "sourcing-system", "version": "1.0"},
                },
                "id": 1,
            }
            _wait_kuaicha_pace()
            resp = requests.post(self.mcp_url, headers=self.headers,
                                 data=json.dumps(payload).encode("utf-8"), timeout=30)
            if resp.status_code == 200:
                self.session_id = resp.headers.get("mcp-session-id")
                # 发一个 initialized 通知，告知服务器客户端已就绪
                try:
                    notify = {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                        "params": {},
                    }
                    headers = dict(self.headers)
                    if self.session_id:
                        headers["mcp-session-id"] = self.session_id
                    requests.post(self.mcp_url, headers=headers,
                                  data=json.dumps(notify).encode("utf-8"), timeout=30)
                except requests.exceptions.RequestException:
                    pass  # 通知失败不影响后续调用
                return True
            print(f"快查MCP初始化失败：HTTP {resp.status_code} {resp.text[:200]}")
            return False
        except requests.exceptions.RequestException as e:
            print(f"快查MCP初始化异常: {e}")
            return False

    def _call_tool(self, tool_id, arguments):
        """
        调用快查工具（name="call" 元工具 + tool_id + params_to_tool）

        小白讲解：快查的调用方式和天眼查/水滴不一样：
        - 天眼查/水滴：直接 tools/call 传工具名（如 get_company_info）
        - 快查：tools/call 只认两个元工具 discover/call，
          call 的 arguments 里要传 tool_id（来自discover返回）和 params_to_tool（JSON字符串参数）
        这里封装好，上层直接传工具ID和参数字典即可。

        参数：
            tool_id: 快查工具ID，如 "basic_get_enterprise_associate"
            arguments: 参数字典，如 {"query": "公司名"}
        返回：
            - 成功：解析后的 JSON 字典（内层 data.list 等）
            - 失败：返回 None
        """
        if not self.mcp_url or not self.api_key:
            return None

        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "call",
                "arguments": {
                    "tool_id": tool_id,
                    "params_to_tool": json.dumps(arguments, ensure_ascii=False),
                },
            },
            "id": 100,
        }

        headers = dict(self.headers)
        if self.session_id:
            headers["mcp-session-id"] = self.session_id

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                _wait_kuaicha_pace()
                resp = requests.post(self.mcp_url, headers=headers,
                                     data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     timeout=30)
                resp.encoding = "utf-8"

                # 429频率限制：等待后重试
                if resp.status_code == 429:
                    if attempt < max_retries:
                        wait = attempt * 2
                        print(f"快查MCP频率限制(429)，第{attempt}/{max_retries}次重试，等待{wait}秒...")
                        time.sleep(wait)
                        continue
                    print(f"快查MCP频率限制(429)，已重试{max_retries}次仍失败")
                    return None

                # 5xx服务器错误：等待后重试
                if 500 <= resp.status_code < 600:
                    if attempt < max_retries:
                        wait = attempt * 2
                        print(f"快查MCP服务器错误({resp.status_code})，第{attempt}/{max_retries}次重试，等待{wait}秒...")
                        time.sleep(wait)
                        continue
                    print(f"快查MCP服务器错误({resp.status_code})，已重试{max_retries}次仍失败")
                    return None

                # 其他4xx错误（如401 Key无效）：不重试
                if resp.status_code >= 400:
                    print(f"快查MCP返回HTTP {resp.status_code}: {resp.text[:300]}")
                    return None

                # 解析响应（SSE格式：data:{...}，注意"data:"后没有空格，不要用"data: "）
                result = None
                for line in resp.text.split("\n"):
                    line = line.strip()
                    if line.startswith("data:") and line[5:].strip():
                        try:
                            result = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            pass
                        break
                if result is None and resp.text.strip():
                    try:
                        result = resp.json()
                    except json.JSONDecodeError:
                        pass
                return result

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt < max_retries:
                    wait = attempt * 2
                    print(f"快查MCP网络超时({tool_id})，第{attempt}/{max_retries}次重试，等待{wait}秒...: {e}")
                    time.sleep(wait)
                    continue
                print(f"快查MCP网络超时({tool_id})，已重试{max_retries}次仍失败: {e}")
                return None
            except requests.exceptions.RequestException as e:
                print(f"快查MCP请求失败({tool_id}): {e}")
                return None

        return None

    def get_company_info(self, company_name):
        """
        查询企业工商照面信息（英文名/中文名都支持，模糊搜索匹配中文企业）

        小白讲解：这是快查最核心的工具，一次调用返回：
        - 中文公司名(corp_name)、统一社会信用代码、法人、注册资本、成立日期、经营状态
        - 注册地址、经营范围、所属行业
        - 电话(phone_num[])、邮箱(email[])、官网(website[])

        匹配说明：快查本身就是"企业模糊搜索匹配"引擎，用英文名或中文名/简称都能搜，
        返回最匹配的中文企业（等价于天眼查 search_companies 的"英文名匹配"命中）。
        调用方可直接用返回的 corp_name 作为中文名落库。

        参数：company_name - 企业全称（英文或中文）
        返回：字典，解析后的工商信息；失败/未命中返回空字典{}
        """
        if not company_name:
            return {}

        result = self._call_tool("basic_get_enterprise_associate", {"query": company_name})
        if not result or "result" not in result:
            return {}

        content = result["result"].get("content", [])
        if not content:
            return {}

        # 快查的响应是 JSON（text 字段），里面再包一层 raw（转义的JSON字符串）
        text_content = content[0].get("text", "")
        if not text_content:
            return {}
        try:
            outer = json.loads(text_content)
        except (json.JSONDecodeError, TypeError):
            return {"raw": text_content[:3000]}

        # 提取真正的业务数据层（raw 字段里是转义的JSON字符串）
        raw_str = outer.get("raw", "")
        try:
            inner = json.loads(raw_str) if raw_str else outer
        except (json.JSONDecodeError, TypeError):
            return {"raw": raw_str[:3000]}

        # ==================== 校验：成功标志 + 数据非空 ====================
        # 小白讲解：快查业务成功标志是 status_code == 0（注意不是1！水滴才是1）。
        # status_code != 0 或 data.list 为空 → 未命中，返回空字典让上层降级。
        status_code = inner.get("status_code", -1)
        if status_code != 0:
            print(f"  ⚠️ [快查未命中] 输入='{company_name}' status_code={status_code} msg={inner.get('status_msg', '-')}")
            return {}

        data = inner.get("data", {})
        if not isinstance(data, dict):
            return {}
        company_list = data.get("list", []) or []
        if not company_list:
            print(f"  ⚠️ [快查空结果] 输入='{company_name}' 未匹配到企业")
            return {}

        # 取第一条（最匹配）
        first = company_list[0]
        if not isinstance(first, dict):
            return {}

        # ==================== 实体校验：corp_name + orgid 必须存在 ====================
        # 小白讲解：快查没有 searched_company/match_type 字段（天眼查有），
        # 但 corp_name + orgid 是快查匹配引擎确认的实体标识，缺任一都不可信。
        corp_name = first.get("corp_name", "") or ""
        orgid = first.get("orgid", "") or ""
        if not corp_name or not orgid:
            print(f"  ⚠️ [快查实体缺失] 输入='{company_name}' corp_name='{corp_name}' orgid='{orgid}'")
            return {}

        return self._parse_company_info(first, company_name)

    def _parse_company_info(self, item, company_name):
        """
        把快查返回的单条企业数据解析成统一格式的字典

        小白讲解：把快查的字段名映射成和天眼查/水滴一致的字段名
        （name/credit_code/legal_person/registered_capital/...），
        让上层代码（搜索/初筛引擎）不用改就能用。

        注意：快查的注册资本可能是 "101.0000万人民币"，复用水滴的
        _clean_capital 归一化成 "101万人民币"。

        参数：
            item: 快查返回的企业数据字典（list[0]）
            company_name: 兜底用的公司名
        返回：统一格式的工商信息字典
        """
        capital = item.get("capital") or item.get("actual_capital") or ""
        clean_capital = _clean_capital(capital)

        # 电话/邮箱/官网：快查返回的是数组，取第一个
        phones = item.get("phone_num", []) or []
        emails = item.get("email", []) or []
        websites = item.get("website", []) or []
        phone = phones[0] if phones else ""
        email = emails[0] if emails else ""
        website = ";".join(websites) if websites else ""

        result = {
            "name": item.get("corp_name") or company_name,
            "credit_code": item.get("creditcode") or item.get("reg_num") or "",
            "orgid": item.get("orgid", ""),
            "legal_person": item.get("legal_person", ""),
            "registered_capital": clean_capital,
            "establish_date": item.get("established_date", ""),
            "status": item.get("state", ""),
            "company_type": item.get("type") or "",
            "address": item.get("address", ""),
            "business_scope": item.get("operating_scope", ""),
            "industry": item.get("first_industry") or item.get("second_industry") or "",
            "phone": phone,
            "email": email,
            "website": website,
            # 整段原始数据兜底备用
            "raw": json.dumps(item, ensure_ascii=False)[:3000],
        }
        return result


# ==================== 便捷测试函数 ====================

def test_kuaicha_company_info(company_name):
    """
    快速测试：调用 get_company_info 看能不能拿到数据

    小白讲解：独立的测试函数，不需要数据库和Flask环境。
    直接 python kuaicha_client.py 就能跑，验证快查key是否可用。
    """
    print(f"\n===== 快查MCP测试：查询 {company_name} =====")
    client = KuaichaClient()
    if not client.is_available():
        print("快查未启用或未配置，请检查 ai_providers 表里 kuaicha_data 的 is_enabled/base_url/api_key")
        return None
    if not client.initialize():
        print("初始化失败，请检查 kuaicha_data 配置")
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
    # 直接运行本文件时执行：测试一个真实公司（可修改公司名测试其他企业）
    test_kuaicha_company_info("Guangzhou Wankai Furniture Co., Ltd.")
