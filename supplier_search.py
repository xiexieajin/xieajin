"""
供应商搜索模块 - 核心搜索逻辑

这个模块实现了供应商寻源的完整搜索流程：
1. 1688官方API：用AK签名调用searchoffer搜索商品 → workflow(offer_detail)获取商家信息
  （官方API方式，无需Cookie/爬虫，不会被风控，数据最完整）
2. 中国制造网（Made-in-China）：按P0-P3关键词爬取B2B平台搜索页
  （带session保持+4秒慢速间隔，减少验证码触发）
3. 海关贸易数据（topease）：用HS编码+产品关键词搜索海关出口记录
  （streamable-http MCP，stream=True解决JSON截断，按exporterName聚合统计出口量）
4. 用DeepSeek做过滤判断（不推荐、不编造，只从已有公司名中筛选）
5. 用天眼查MCP补全供应商的工商信息（注册资本、地址、电话等）

参考文档：供应商寻源SKILL（飞书文档）
参考项目：https://github.com/next-1688/1688-shopkeeper
"""

import json
import uuid
import re
import time
import hashlib
import hmac
import base64
import queue
import threading
import difflib
import subprocess
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
# 小白讲解：AI配置统一从数据库读取（通过model_config模块），不再从config.py硬编码
# get_search_platforms用于读取启用的搜索平台列表（管理员可在管理中心启停平台）
from model_config import get_provider, get_model_config, get_search_platforms
# DeepSeek 调用统一用 ai_helper 里的 call_deepseek，避免两个文件各写一份导致改漏
from ai_helper import call_deepseek


def _is_1688_ak_configured():
    """检查1688 AK是否已配置（从数据库读取，替代原config.py的函数）"""
    provider = get_provider("ali1688")
    return bool(provider and provider["api_key"] and len(provider["api_key"]) > 50)


def _get_platform_max_results(provider_code, default=100):
    """
    从数据库读取某个搜索平台的"最大搜索数量"配置（管理员可在前端管理页面修改）

    小白讲解：管理员在"模型与平台管理→搜索平台管理"页面里设置每个平台的"最大结果数"，
    这个函数把那个配置读出来，用于控制1688的pageSize和MIC的翻页目标数量。
    管理员改完后保存即生效，不需要改代码、不需要重启。

    参数：
        provider_code: 平台代码，如 "ali1688" 或 "madeinchina"
        default: 配置不存在时的默认值，默认100

    返回：最大搜索数量（整数），如1688返回96、MIC返回100
    """
    # 从model_config缓存的搜索平台列表里查找对应平台
    platforms = get_search_platforms()
    for p in platforms:
        if p.get("provider_code") == provider_code:
            max_results = p.get("max_results", default)
            try:
                max_results = int(max_results)
                # 海关数据每页10条，翻50页最多500条，上限放宽到500；其他平台上限200
                upper_limit = 500 if provider_code == "topease_customs" else 200
                return max(10, min(upper_limit, max_results))
            except (ValueError, TypeError):
                return default
    return default


def _get_1688_ak():
    """获取1688 AK密钥（从数据库读取）"""
    provider = get_provider("ali1688")
    return provider["api_key"] if provider else ""


# ==================== 中国制造网（Made-in-China）MCP服务（主方式）====================
# 参考 https://clawhub.ai/witcheng/skills/sourcing-in-china
# MCP Endpoint: https://mcp.chexb.com/sse
#
# 提供3个工具：
#   - search_products：按关键词搜产品（30条/页），返回产品标题、链接、价格、起订量、供应商名等
#   - search_suppliers：按关键词搜供应商（10条/页）
#   - get_product_detail：获取产品详情页完整信息
#
# 优势：结构化JSON数据，无需正则提取，无验证码风控问题
# 满足飞书文档"先按关键词找产品→再根据产品找供应商"的两步流程

# MCP服务地址
_MIC_MCP_HOST = "https://mcp.chexb.com"
_MIC_MCP_SSE_URL = f"{_MIC_MCP_HOST}/sse"
# 每个关键词目标采集的产品数（飞书文档要求100个）
_MIC_MCP_TARGET_COUNT = 100
# search_products每页返回30个，需要翻4页凑够100个
_MIC_MCP_PAGE_SIZE = 30

# ==================== MIC 调用限速控制 ====================
# 小白讲解：3个关键词并发搜索时，MCP服务会同时去made-in-china.com抓数据，
# 容易触发429限流（Storage TV Cabinet就是第1页就限流导致0家）。
# 用全局锁把所有MIC的search_products调用排成一队，两次调用至少间隔5秒，
# 避免同时请求触发限流。
_mic_call_lock = threading.Lock()           # 全局锁，保证同一时刻只有一个线程在判断/更新调用时间
_mic_last_call_time = 0.0                   # 上次MCP search_products调用的发起时间戳
_MIC_CALL_INTERVAL = 5                      # 两次MCP search_products调用之间的最小间隔秒数

# ==================== MIC 关键词串行锁 ====================
# 小白讲解：3个关键词并发搜索时，即使有5秒限速，3个线程轮流请求made-in-china.com，
# 等于每5秒就有一个请求，加上重试时3个线程同时重试，互相加剧限流，导致后续关键词全部429失败。
# 用这个串行锁让MIC部分一次只搜一个关键词（排队执行），1688部分仍然并发不受影响。
# 这样5秒限速才能真正生效，重试时也不会有多线程同时重试的问题。
_mic_serial_lock = threading.Lock()         # MIC关键词串行锁：同一时刻只有一个关键词在搜MIC


# ==================== 海关贸易数据（topease）MCP服务 ====================
# MCP Endpoint: https://mcp.topease.net/mcp (streamable-http)
# 工具: search_customs_data
# 搜索策略: hs_code=9403 + product_keyword + trade_type=import + stream=True
# 翻页: 每页10条（实测上限），翻50页凑500条
# 聚合: 按exporterName合并，累加出口量，降序排序

# topease API Key（优先从数据库读取，管理员可在管理中心修改；这里作为兜底默认值）
_TOPEASE_API_KEY_DEFAULT = "trdmcp_live_gh-CN9jbAnZrRd99lJR9MNSG8avtLdnXZKoY0NaE8c4"


def _get_topease_api_key():
    """
    获取topease海关数据API密钥（优先从数据库读取，失败时用默认值兜底）

    小白讲解：管理员在"模型与平台管理→搜索平台管理"里看到的"海关贸易数据"，
    其API密钥存在ai_providers表中。这个函数从数据库取密钥，取不到就用代码里写死的默认值。
    """
    try:
        from model_config import get_provider
        provider = get_provider("topease_customs")
        if provider and provider.get("api_key"):
            return provider["api_key"]
    except Exception as e:
        print(f"从数据库读取topease API密钥失败：{e}")
    return _TOPEASE_API_KEY_DEFAULT

# ==================== 海关数据 调用限速控制 ====================
# 小白讲解：与MIC一样，用全局锁保证两次topease调用之间至少间隔5秒，
# 避免触发topease服务的请求频率限制。
_topease_call_lock = threading.Lock()        # 全局锁
_topease_last_call_time = 0.0               # 上次调用时间戳
_TOPEASE_CALL_INTERVAL = 5                   # 5秒间隔

# 海关数据关键词串行锁（与MIC一样，一次只搜一个关键词）
_topease_serial_lock = threading.Lock()


def _wait_topease_rate_limit():
    """
    海关数据调用前限速：确保两次调用间隔至少5秒

    小白讲解：用和MIC完全一样的限速策略。
    用全局锁记录上次调用时间，如果距上次调用不足5秒就补齐。
    """
    global _topease_last_call_time
    with _topease_call_lock:
        now = time.time()
        elapsed = now - _topease_last_call_time
        if elapsed < _TOPEASE_CALL_INTERVAL:
            wait = _TOPEASE_CALL_INTERVAL - elapsed
            print(f"海关数据限速等待：距上次调用{elapsed:.1f}秒，补睡{wait:.1f}秒")
            time.sleep(wait)
        _topease_last_call_time = time.time()


def _wait_mic_rate_limit():
    """
    MIC调用前限速：确保两次MCP search_products调用之间至少间隔5秒

    小白讲解：这个函数在每次调用MCP search_products前执行。
    - 拿到全局锁后，看上次调用是什么时候
    - 如果距离上次调用不到5秒，就补睡"5秒 - 已过时间"
    - 补睡完更新"上次调用时间"为当前时间，然后释放锁去发请求
    这样不管多少个线程并发，MCP的search_products调用都会自动排队，两次调用至少隔5秒，避免触发429限流。
    """
    global _mic_last_call_time
    with _mic_call_lock:
        now = time.time()
        elapsed = now - _mic_last_call_time
        if elapsed < _MIC_CALL_INTERVAL:
            wait = _MIC_CALL_INTERVAL - elapsed
            print(f"MCP限速等待：距上次调用{elapsed:.1f}秒，补睡{wait:.1f}秒后再次调用")
            time.sleep(wait)
        # 更新上次调用时间为"现在"（即即将发起调用的时刻）
        _mic_last_call_time = time.time()


class _MicMcpClient:
    """
    Made-in-China MCP over SSE 客户端（用系统curl实现，绕过Python OpenSSL兼容性问题）

    小白讲解：原来用Python requests库连接MCP的SSE服务，但服务器拒绝Python OpenSSL的TLS指纹，
    导致连接被重置(10054)。系统自带的curl用Windows Schannel做SSL，能正常连上。
    所以这里改用 subprocess 调用系统 curl 来建立SSE长连接和发送POST请求。

    MCP over SSE 通信模式：
    1. 用 curl GET /sse 建立长连接，从首个data:行获取session路径（/messages?sessionId=xxx）
    2. 用 curl POST 到session路径发送JSON-RPC请求（返回202 Accepted）
    3. 实际响应通过SSE长连接的data:行返回（从curl的stdout读取）
    4. 需保持SSE连接不断开，否则session失效
    """

    def __init__(self):
        self.session_path = None
        self.curl_proc = None       # curl子进程，用于维持SSE长连接
        self.sse_thread = None      # 后台线程，持续读取curl的stdout
        self.response_queue = queue.Queue()
        self._stop = False

    def _read_sse(self):
        """后台线程：持续读取curl子进程的stdout，把JSON-RPC响应放入队列"""
        try:
            while not self._stop and self.curl_proc:
                line = self.curl_proc.stdout.readline()
                if not line:
                    # readline返回空说明curl进程已结束
                    if self.curl_proc.poll() is not None:
                        break
                    continue
                line = line.strip()
                if line.startswith("data: "):
                    data_content = line[6:].strip()
                    # 首个data是session路径
                    if data_content.startswith("/messages?sessionId=") and not self.session_path:
                        self.session_path = data_content
                        continue
                    # 后续data是JSON-RPC响应
                    try:
                        resp_data = json.loads(data_content)
                        self.response_queue.put(resp_data)
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            if not self._stop:
                print(f"MCP SSE读取异常: {e}")

    def connect(self, wait_timeout=15):
        """
        用系统curl建立SSE连接并等待session路径

        小白讲解：用 subprocess 启动 curl 进程，让它去连接MCP的SSE服务。
        curl用Windows Schannel做SSL握手，能绕过服务器对Python OpenSSL的拒绝。
        SSE长连接由curl进程维持，我们通过读取curl的stdout来获取SSE事件。
        """
        try:
            # 用系统curl建立SSE长连接
            # -s 静默模式，-N 禁用缓冲（实时输出），--max-time 120 最长保持120秒
            self.curl_proc = subprocess.Popen(
                ["curl", "-s", "-N", "--max-time", "120",
                 "-H", "Accept: text/event-stream", _MIC_MCP_SSE_URL],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,  # 行缓冲，确保每读到一行就输出
            )
        except Exception as e:
            print(f"MCP SSE连接异常（curl启动失败）: {e}")
            return False

        # 检查curl是否启动成功（如果立刻退出说明有问题）
        time.sleep(0.5)
        if self.curl_proc.poll() is not None:
            err = self.curl_proc.stderr.read() if self.curl_proc.stderr else ""
            print(f"MCP SSE连接失败（curl立即退出）: {err[:200]}")
            return False

        # 启动后台线程读取SSE事件
        self.sse_thread = threading.Thread(target=self._read_sse, daemon=True)
        self.sse_thread.start()

        # 等待获取session路径（最多等15秒）
        start = time.time()
        while not self.session_path and time.time() - start < wait_timeout:
            # 如果curl进程在此期间退出，直接失败
            if self.curl_proc.poll() is not None:
                err = self.curl_proc.stderr.read() if self.curl_proc.stderr else ""
                print(f"MCP SSE连接失败（curl提前退出）: {err[:200]}")
                return False
            time.sleep(0.1)

        return bool(self.session_path)

    def call(self, method, params, call_id, timeout=60):
        """用curl POST发送MCP方法调用，等待SSE流返回响应"""
        if not self.session_path:
            return None

        url = f"{_MIC_MCP_HOST}{self.session_path}"
        body = {
            "jsonrpc": "2.0",
            "id": call_id,
            "method": method,
            "params": params
        }
        try:
            # 用系统curl发送POST请求（同样绕过OpenSSL兼容性问题）
            subprocess.run(
                ["curl", "-s", "-X", "POST", url,
                 "-H", "Content-Type: application/json",
                 "-d", json.dumps(body, ensure_ascii=False)],
                capture_output=True,
                timeout=15,
            )
        except Exception as e:
            print(f"MCP POST异常: {e}")
            return None

        # 从队列等待对应id的响应
        start = time.time()
        while time.time() - start < timeout:
            try:
                resp_data = self.response_queue.get(timeout=1)
                if resp_data.get("id") == call_id:
                    return resp_data
                # 不是本次响应，放回队列
                self.response_queue.put(resp_data)
            except queue.Empty:
                # 检查curl进程是否还活着
                if self.curl_proc and self.curl_proc.poll() is not None:
                    print(f"MCP SSE连接已断开（curl进程退出）")
                    return None
                continue
        return None

    def call_tool(self, tool_name, arguments, call_id=1, timeout=60):
        """
        便捷方法：调用tools/call执行指定工具

        返回：
            - 成功：解析后的dict/list/str（JSON内容）
            - 限流(429)：返回特殊标记字符串 "__RATE_LIMIT__"
            - 其他错误：返回None
        """
        result = self.call("tools/call", {
            "name": tool_name,
            "arguments": arguments
        }, call_id=call_id, timeout=timeout)

        if not result or "result" not in result:
            return None

        # 检查MCP工具是否返回错误（isError=true）
        is_error = result["result"].get("isError", False)

        for content in result["result"].get("content", []):
            if content.get("type") == "text":
                text = content.get("text", "")
                # 错误响应：检测429限流
                if is_error:
                    if "429" in text or "rate" in text.lower():
                        return "__RATE_LIMIT__"
                    print(f"MCP工具错误: {text[:200]}")
                    return None
                # 正常响应：解析JSON
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
        return None

    def close(self):
        """关闭SSE连接：停止读取线程，终止curl子进程"""
        self._stop = True
        if self.curl_proc:
            try:
                self.curl_proc.terminate()
                self.curl_proc.wait(timeout=3)
            except Exception:
                try:
                    self.curl_proc.kill()
                except Exception:
                    pass


def _translate_keyword_to_english(keyword):
    """
    用DeepSeek把中文关键词翻译成英文（MCP服务基于国际站，英文搜索效果最好）

    参数：keyword 中文关键词（如"玻璃电视柜"）
    返回：英文关键词（如"glass TV stand"），翻译失败返回原关键词

    注意：deepseek-v4-pro是推理模型，输出分reasoning_content（推理）和content（回复）两部分，
    max_tokens太小会导致推理过程用完token，实际回复content为空。翻译任务需用较大max_tokens。
    """
    # 简单英文判断（已含英文字母的直接返回）
    if re.search(r'[a-zA-Z]', keyword):
        return keyword
    try:
        result = call_deepseek(
            [{"role": "user", "content": f"把以下中文产品关键词翻译成英文搜索词，只返回翻译结果不要其他内容，不要解释。例如：玻璃电视柜→glass TV stand，蓝牙音箱→bluetooth speaker。关键词：{keyword}"}],
            scene_code="supplier_translate",
            temperature=0.1,
        )
        english_kw = result.strip().strip('"').strip("'").strip()
        # 去掉翻译中可能的多余内容（只取第一行）
        english_kw = english_kw.split('\n')[0].strip()
        if english_kw and len(english_kw) < 100:
            print(f"关键词翻译：'{keyword}' → '{english_kw}'")
            return english_kw
    except Exception as e:
        print(f"关键词翻译失败: {e}")
    return keyword


def _translate_company_names_batch(suppliers_list, batch_size=20):
    """
    用DeepSeek把MCP返回的英文公司名批量翻译成中文（括号保留英文原名）

    小白讲解：中国制造网(MCP)返回的供应商名是英文的（如"Shenzhen XYZ Technology Co., Ltd."），
    业务部门看英文不方便，需要翻译成中文，格式："深圳市XYZ科技有限公司（Shenzhen XYZ Technology Co., Ltd.）"。

    为了避免逐个调用太慢，采用批量翻译：每批20家公司一次性发给DeepSeek，返回JSON映射。
    翻译失败的公司保留原英文名。

    参数：
        suppliers_list: 供应商列表（会原地修改每条的name字段）
        batch_size: 每批翻译的公司数量，默认20

    返回：翻译后的供应商列表（同名对象的name已更新）
    """
    # 只翻译英文名（含英文字母的公司名），中文名跳过
    to_translate = []
    for s in suppliers_list:
        name = s.get("name", "")
        if name and re.search(r'[a-zA-Z]', name):
            to_translate.append(name)

    if not to_translate:
        return suppliers_list

    # 去重后翻译（同一家公司可能出现多次）
    unique_names = list(set(to_translate))
    print(f"MCP供应商英文名批量翻译：共{len(unique_names)}个唯一英文名需要翻译")

    # 中文翻译结果映射：英文名 -> 中文名
    name_map = {}

    # 分批调用DeepSeek翻译
    for i in range(0, len(unique_names), batch_size):
        batch = unique_names[i:i + batch_size]
        # 构造公司名列表文本
        names_text = "\n".join(f"{idx+1}. {name}" for idx, name in enumerate(batch))

        prompt = f"""请把下面的英文公司名翻译成对应的中文公司名。
要求：
1. 按中国大陆工商注册习惯翻译，如"Co., Ltd."翻译成"有限公司"，"Technology"翻译成"科技"
2. 只返回翻译后的中文名，不要加任何解释或编号
3. 每行一个中文名，顺序和输入一一对应
4. 如果某个公司名无法准确翻译，返回原文（保留英文）

【英文公司名列表】
{names_text}

【请返回JSON对象】（只返回JSON）
{{
    "translations": [
        {{"original": "英文原名", "chinese": "中文翻译"}}
    ]
}}"""

        messages = [
            {"role": "system", "content": "你是专业的公司名称翻译专家，擅长把英文公司名翻译成符合中国工商注册习惯的中文名。"},
            {"role": "user", "content": prompt},
        ]

        try:
            # 翻译是简单任务，场景配置中已用high强度
            result_text = call_deepseek(messages, scene_code="supplier_translate", temperature=0.1, json_mode=True)
            result = json.loads(result_text)
            translations = result.get("translations", [])
            if isinstance(translations, dict):
                translations = [translations]
            for item in translations:
                original = item.get("original", "").strip()
                chinese = item.get("chinese", "").strip()
                if original and chinese:
                    name_map[original] = chinese
            print(f"MCP批量翻译第{i//batch_size+1}批：{len(batch)}个 → 成功翻译{len([t for t in translations if t.get('chinese')])}个")
        except Exception as e:
            print(f"MCP批量翻译第{i//batch_size+1}批失败: {e}")

    # 把翻译后的中文名更新到供应商列表（格式：中文名（英文名））
    translated_count = 0
    for s in suppliers_list:
        name = s.get("name", "")
        if name in name_map:
            chinese_name = name_map[name]
            # 中文名和英文名不同时才加括号（避免翻译返回原英文的情况）
            if chinese_name and chinese_name != name:
                s["name"] = f"{chinese_name}（{name}）"
                translated_count += 1

    print(f"MCP供应商名翻译完成：{translated_count}/{len(to_translate)}家已翻译成中文（括号保留英文原名）")
    return suppliers_list


def _translate_product_names_batch(suppliers_list, batch_size=30):
    """
    用DeepSeek把MCP返回的英文产品名批量翻译成中文

    小白讲解：中国制造网(MCP)的产品名是英文的（如"Small Size White Wood TV Stand with Glass Doors"），
    业务部门看英文不方便，需要翻译成中文，格式："小型白色木质玻璃门电视柜"。

    为了避免逐个调用太慢，采用批量翻译：每批30个产品名一次性发给DeepSeek，返回JSON映射。
    翻译失败的产品保留原英文名。

    参数：
        suppliers_list: 供应商列表（会原地修改每条的product_title字段）
        batch_size: 每批翻译的产品名数量，默认30

    返回：翻译后的供应商列表（同名对象的product_title已更新为中文）
    """
    # 只翻译英文产品名（含英文字母的），中文名跳过
    to_translate = []
    for s in suppliers_list:
        title = s.get("product_title", "")
        if title and re.search(r'[a-zA-Z]', title):
            to_translate.append(title)

    if not to_translate:
        return suppliers_list

    # 去重后翻译（同一个产品名可能出现多次）
    unique_titles = list(set(to_translate))
    print(f"MCP产品名批量翻译：共{len(unique_titles)}个唯一英文产品名需要翻译")

    # 中文翻译结果映射：英文产品名 -> 中文产品名
    title_map = {}

    # 分批调用DeepSeek翻译
    for i in range(0, len(unique_titles), batch_size):
        batch = unique_titles[i:i + batch_size]
        # 构造产品名列表文本
        titles_text = "\n".join(f"{idx+1}. {title}" for idx, title in enumerate(batch))

        prompt = f"""请把下面的英文产品名翻译成对应的中文产品名。
要求：
1. 按中国大陆电商习惯翻译，简洁准确，如"TV Stand"翻译成"电视柜"，"Bluetooth Speaker"翻译成"蓝牙音箱"
2. 只返回翻译后的中文名，不要加任何解释或编号
3. 每行一个中文名，顺序和输入一一对应
4. 如果某个产品名无法准确翻译，返回原文（保留英文）

【英文产品名列表】
{titles_text}

【请返回JSON对象】（只返回JSON）
{{
    "translations": [
        {{"original": "英文原名", "chinese": "中文翻译"}}
    ]
}}"""

        messages = [
            {"role": "system", "content": "你是专业的产品名称翻译专家，擅长把英文电商产品名翻译成符合中国大陆电商习惯的中文名。"},
            {"role": "user", "content": prompt},
        ]

        try:
            # 翻译是简单任务，用high强度即可
            # 小白讲解：这里显式传max_tokens=2048覆盖场景配置，避免JSON输出被截断导致解析失败
            result_text = call_deepseek(messages, scene_code="supplier_translate", temperature=0.1, json_mode=True, max_tokens=2048)
            result = json.loads(result_text)
            translations = result.get("translations", [])
            if isinstance(translations, dict):
                translations = [translations]
            for item in translations:
                original = item.get("original", "").strip()
                chinese = item.get("chinese", "").strip()
                if original and chinese:
                    title_map[original] = chinese
            print(f"MCP产品名翻译第{i//batch_size+1}批：{len(batch)}个 → 成功翻译{len([t for t in translations if t.get('chinese')])}个")
        except Exception as e:
            print(f"MCP产品名翻译第{i//batch_size+1}批失败: {e}")

    # 把翻译后的中文名更新到供应商列表
    translated_count = 0
    for s in suppliers_list:
        title = s.get("product_title", "")
        if title in title_map:
            chinese_title = title_map[title]
            if chinese_title and chinese_title != title:
                s["product_title"] = chinese_title
                translated_count += 1

    print(f"MCP产品名翻译完成：{translated_count}/{len(to_translate)}个已翻译成中文")
    return suppliers_list


# ==================== 海关贸易数据（topease）搜索 ====================

def crawl_topease_customs(keyword, hit_keyword="", variants=None, hs_code=""):
    """
    海关数据搜索入口函数（与 crawl_1688 / crawl_made_in_china 并列）

    小白讲解：和MIC一样，先检查关键词是否包含英文字母（海关数据产品描述全是英文），
    中文关键词直接跳过。然后用串行锁排队，一次只搜一个关键词，避免topease限流。

    搜索策略：
    - hs_code 从需求配置读取（DeepSeek在需求确认时归类），没有则不传
    - product_keyword 用英文关键词
    - trade_type=import（用进口数据反推中国出口商）
    - country 不填（搜全球海关数据）
    - stream=True（解决JSON截断问题）
    - 翻50页拿500条
    - 按exporterName聚合统计出口量
    - 过滤物流公司
    """
    # 跳过中文关键词（海关数据产品描述全是英文，中文搜不到）
    if not re.search(r'[a-zA-Z]', keyword):
        print(f"海关数据跳过中文关键词：'{keyword}'")
        return []

    with _topease_serial_lock:
        return _crawl_topease_customs_impl(keyword, hit_keyword, hs_code)


def _crawl_topease_customs_impl(keyword, hit_keyword, hs_code=""):
    """
    海关数据搜索实际实现

    小白讲解：topease是streamable-http MCP，不需要像MIC那样用curl+SSE。
    直接用requests.post + stream=True调用，每次返回SSE格式响应，
    用正则提取data:开头的行拼接后解析JSON。

    流程：
    1. 初始化握手，获取mcp-session-id
    2. 翻50页搜索，每页10条，5秒间隔
    3. 按exporterName聚合，只保留exportCountry=China
    4. 过滤物流公司
    5. 返回候选池格式（15个字段，对齐1688/MIC）
    """
    print(f"海关数据搜索关键词：'{keyword}'")

    # 从数据库读取API密钥（管理员可在管理中心修改）
    topease_api_key = _get_topease_api_key()

    # 1. 初始化MCP会话
    try:
        resp = requests.post(
            "https://mcp.topease.net/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {topease_api_key}",
            },
            json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "sourcing-system", "version": "1.0"},
                },
            },
            timeout=15,
        )
        session_id = resp.headers.get("mcp-session-id")
        if not session_id:
            print(f"海关数据MCP初始化失败：未获取session-id")
            return []

        # 发送初始化完成通知
        requests.post(
            "https://mcp.topease.net/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {topease_api_key}",
                "mcp-session-id": session_id,
            },
            json={"jsonrpc": "2.0", "id": 2, "method": "notifications/initialized"},
            timeout=10,
        )
        print(f"海关数据MCP初始化成功")
    except Exception as e:
        print(f"海关数据MCP初始化异常: {e}")
        return []

    # 2. 翻页搜索（页数从数据库配置读取：max_results÷每页10条）
    all_records = []
    target_count = _get_platform_max_results("topease_customs", default=100)
    target_pages = max(1, min(50, (target_count + 9) // 10))  # 每页10条，最多50页=500条
    print(f"海关数据目标{target_count}条，每页10条，计划翻{target_pages}页")

    for page in range(1, target_pages + 1):
        # 限速等待（与MIC一样5秒）
        _wait_topease_rate_limit()

        try:
            resp = requests.post(
                "https://mcp.topease.net/mcp",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "Authorization": f"Bearer {topease_api_key}",
                    "mcp-session-id": session_id,
                },
                json={
                    "jsonrpc": "2.0",
                    "id": page + 10,
                    "method": "tools/call",
                    "params": {
                        "name": "search_customs_data",
                        "arguments": {
                            "hs_code": hs_code or "9403",  # 用需求配置的HS编码，没有则默认9403（家具类）
                            "product_keyword": keyword,
                            "trade_type": "import",  # 用进口数据反推中国出口商
                            # country 不填，搜全球海关数据
                            "page_index": page,
                            "page_size": 10,
                            "sort_by": "quantity",
                            "sort_order": "desc",
                        },
                    },
                },
                stream=True,  # 关键：stream=True解决JSON截断
                timeout=30,
            )

            # 读取完整响应体
            body = b""
            for chunk in resp.iter_content(chunk_size=None):
                if chunk:
                    body += chunk
            text = body.decode("utf-8", errors="replace")

            # 解析SSE响应
            blocks = re.findall(r'data:\s*(.+)', text)
            if not blocks:
                print(f"  海关第{page}页: 响应无data块")
                break

            try:
                data = json.loads("".join(blocks))
                structured = data.get("result", {}).get("structuredContent", {})
                records = structured.get("records", [])
                total = structured.get("total", 0)
            except json.JSONDecodeError as e:
                print(f"  海关第{page}页: JSON解析失败: {e}")
                break

            print(f"  海关第{page:2d}页: {len(records)}条 (总{total}) 累计{len(all_records)}")

            if not records:
                break
            all_records.extend(records)

            if len(all_records) >= 500:
                break

        except requests.Timeout:
            print(f"  海关第{page}页: 超时，停止翻页")
            break
        except Exception as e:
            print(f"  海关第{page}页: 异常: {e}")
            break

    if not all_records:
        print(f"海关数据搜索无结果：'{keyword}'")
        return []

    print(f"海关数据搜索完成：{len(all_records)}条记录")

    # 3. 按exporterName聚合，只保留exportCountry=China
    stats = {}
    for r in all_records:
        name = (r.get("exporterName") or "").strip()
        if not name:
            continue
        if (r.get("exportCountry") or "").strip() != "China":
            continue
        if name not in stats:
            stats[name] = {
                "name": name,
                "count": 0,
                "total_qty": 0.0,
                "total_amount": 0.0,
                "products": [],
                "province": (r.get("exporterProvince") or "").strip(),
                "hit_keyword": hit_keyword or keyword,
                "source_platform": "海关数据",
                "business_type": "",
                "location": (r.get("exporterProvince") or "").strip(),
                "badges": "",
                "product_link": "",
                "price": "",
                "moq": "",
            }
        s = stats[name]
        s["count"] += 1
        s["total_qty"] += float(r.get("quantity") or 0)
        s["total_amount"] += float(r.get("amuntusd") or 0)
        desc = (r.get("prodesc") or "").strip()
        if desc:
            s["products"].append(desc[:80])

    # 4. 过滤物流公司
    logistics_kw = ["COURIER", "LOGISTICS", "FORWARDING", "FREIGHT",
                    "SHIPPING", "TRANSPORT", "EXPRESS", "SUPPLY CHAIN"]
    exporters = []
    for e in sorted(stats.values(), key=lambda x: x["total_qty"], reverse=True):
        if any(k in e["name"].upper() for k in logistics_kw):
            continue
        exporters.append(e)

    print(f"海关数据聚合：{len(stats)}家出口商 → 过滤物流后 {len(exporters)}家")

    # 5. 构造候选池格式（15个字段，对齐1688/MIC）
    results = []
    for e in exporters:
        e["customs_export_count"] = e["count"]
        e["customs_total_qty"] = e["total_qty"]
        e["customs_total_amount"] = e["total_amount"]
        e["product_title"] = e["products"][0] if e["products"] else ""
        # content字段拼接（与MIC格式一致，供DeepSeek判断用）
        desc_parts = [f"搜索品类：{keyword}"]
        desc_parts.append(f"出口次数：{e['count']}次")
        desc_parts.append(f"总出口量：{e['total_qty']:.0f}")
        if e["products"]:
            desc_parts.append(f"产品样本：{e['products'][0]}")
        e["content"] = "；".join(desc_parts)
        results.append(e)

    print(f"海关数据候选池：{len(results)}家供应商")
    return results


def crawl_made_in_china_mcp(keyword, hit_keyword="", variants=None):
    """
    用MCP服务从中国制造网搜索产品，再从产品中获取供应商信息（两步流程的第一步：搜产品）

    小白讲解：原来直接用search_suppliers搜供应商，会出现"产品不适配"问题（供应商可能做相关但不完全匹配的产品）。
    现在改成先用search_products搜产品，每条产品自带供应商公司名，这样搜到的供应商一定做这个产品。
    每页30条产品，翻4页就能凑够约100家供应商（去重后约80-100家）。

    参数：
        keyword: 搜索关键词（英文词直接搜；中文词跳过返回空）
        hit_keyword: 命中的P0-P3关键词标签
        variants: 变体关键词列表（此参数已不再使用，保留是为了兼容调用方）

    返回：供应商列表，每条包含 name, content, hit_keyword, business_type, location, badges,
          product_title(产品名), product_link(产品链接), price(价格), moq(起订量)
    """
    # 小白讲解：用正则判断关键词是否含英文字母。
    # 含英文字母 = 英文关键词 → 继续搜索
    # 不含英文字母 = 纯中文关键词 → 直接返回空，MIC跳过不搜
    if not re.search(r'[a-zA-Z]', keyword):
        print(f"MCP跳过中文关键词：'{keyword}'（MIC只用英文关键词搜索）")
        return []

    # 英文关键词已经是英文，不需要再翻译，直接使用
    search_keyword = keyword

    # 小白讲解：MIC串行锁——3个关键词并发搜索时，MIC部分一次只搜一个关键词。
    # 1688部分不受这个锁影响，仍然并发。这样made-in-china.com的5秒限速才能真正生效，
    # 不会出现3个线程轮流请求+同时重试导致的持续429限流。
    # 用with语法自动加锁/解锁，即使搜索出错也能自动释放锁。
    with _mic_serial_lock:
        return _crawl_made_in_china_mcp_impl(keyword, hit_keyword, variants, search_keyword)


def _crawl_made_in_china_mcp_impl(keyword, hit_keyword, variants, search_keyword):
    """
    MIC搜索的实际实现（被crawl_made_in_china_mcp包装，加了串行锁）

    小白讲解：这个函数才是真正干活的，外面的crawl_made_in_china_mcp负责排队拿号（串行锁），
    拿到号了才进来真正搜产品。
    """
    print(f"MCP开始搜索英文关键词（搜产品）：'{search_keyword}'")

    client = _MicMcpClient()
    if not client.connect():
        print(f"MCP服务连接失败")
        return []

    try:
        # 初始化MCP会话
        init = client.call("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "supplier-search", "version": "1.0"}
        }, call_id=0, timeout=15)
        if not init:
            print(f"MCP初始化失败")
            return []

        # 总超时保护：搜产品翻页，限流重试3次每次6秒，需要更长超时
        mcp_start_time = time.time()
        MCP_TOTAL_TIMEOUT = 180

        # 搜索结果存放，用公司名去重
        all_results = []
        seen_names = set()
        # 小白讲解：目标供应商数量从管理中心配置读取（管理员可在"搜索平台管理"页面调整）
        # 每页30条产品，按目标数量计算最多翻几页（向上取整，至少1页，最多10页）
        target_count = _get_platform_max_results("madeinchina", default=100)
        max_pages = max(1, min(10, (target_count + 29) // 30))

        # 翻页搜索：逐页获取，直到凑够目标数量或没有更多数据
        for page in range(1, max_pages + 1):
            # 总超时检查
            if time.time() - mcp_start_time > MCP_TOTAL_TIMEOUT:
                print(f"MCP搜索总超时({MCP_TOTAL_TIMEOUT}秒)，返回已有{len(all_results)}家供应商")
                break
            # 已凑够目标数量就停止翻页
            if len(all_results) >= target_count:
                print(f"MCP已凑够{len(all_results)}家供应商，停止翻页")
                break

            # 小白讲解：调用MCP search_products前先限速，保证两次调用间隔至少5秒，避免3个并发线程同时请求触发429
            _wait_mic_rate_limit()

            result = client.call_tool("search_products", {
                "keyword": search_keyword,
                "page": page
            }, call_id=page, timeout=30)

            # 限流处理：等待10秒后重新连接重试，最多重试3次
            # 小白讲解：made-in-china.com的限流是IP级滑动窗口限制，6秒等待不够恢复，
            # 改成每次等10秒，给made-in-china.com足够的冷却时间。
            # 3次重试都失败才放弃当前关键词，保留已有结果。
            MAX_RETRY = 3
            retry_count = 0
            while result == "__RATE_LIMIT__" and retry_count < MAX_RETRY:
                retry_count += 1
                print(f"MCP search_products第{page}页触发限流(429)，等待10秒后重试（第{retry_count}/{MAX_RETRY}次）...")
                time.sleep(10)
                # 重新连接MCP（SSE连接可能已断开，需要新建连接）
                client.close()
                client = _MicMcpClient()
                if not client.connect():
                    print(f"重新连接MCP失败，跳过此关键词的MCP搜索")
                    break
                client.call("initialize", {
                    "protocolVersion": "2025-03-26", "capabilities": {},
                    "clientInfo": {"name": "supplier-search", "version": "1.0"}
                }, call_id=0, timeout=15)
                result = client.call_tool("search_products", {
                    "keyword": search_keyword,
                    "page": page
                }, call_id=page + retry_count * 100, timeout=30)

            # 3次重试后仍然限流，放弃当前关键词（保留已有结果）
            # 小白讲解：3次重试都失败说明made-in-china.com限流严重，
            # 继续翻下一页大概率也会限流，不如保留已有结果不浪费时间。
            if result == "__RATE_LIMIT__":
                print(f"MCP search_products第{page}页重试{MAX_RETRY}次后仍限流，返回已有{len(all_results)}家")
                break
            if not result or not isinstance(result, dict):
                break

            items = result.get("items", [])
            if not items:
                print(f"MCP search_products'{search_keyword}'第{page}页无数据，停止翻页")
                break

            new_count = 0
            for item in items:
                # 小白讲解：search_products返回的每条是"产品"，但自带供应商公司名。
                # 从产品里提取供应商公司名（supplier字段），用公司名去重
                name = (item.get("supplier") or item.get("supplierName") or
                        item.get("companyName") or item.get("company") or "").strip()
                if not name or name in seen_names:
                    continue
                seen_names.add(name)
                new_count += 1

                # 产品名（英文，后续会批量翻译成中文）
                product_title = (item.get("name") or item.get("title") or
                                 item.get("productName") or "").strip()

                # 产品链接
                product_link = (item.get("url") or item.get("link") or
                                item.get("productUrl") or "").strip()

                # 价格和起订量分开存储（业务页面需要分别显示价格和MOQ）
                price = (item.get("price") or "").strip()
                moq = (item.get("moq") or "").strip()

                # 产品规格属性（字典转成字符串供DeepSeek参考）
                properties = item.get("properties", {})
                if isinstance(properties, dict) and properties:
                    props_text = "；".join(f"{k}:{v}" for k, v in properties.items())
                else:
                    props_text = ""

                # search_products不返回business_type/location/badges，置空（下游.get()兼容）
                business_type = ""
                location = ""
                badges = ""

                # 组合描述供DeepSeek过滤参考
                # 小白讲解：把产品名+规格+价格+搜索关键词组合成描述，
                # DeepSeek能结合"搜的是什么+产品规格+公司名"综合判断供应商是否合适
                desc_parts = []
                desc_parts.append(f"搜索品类：{keyword}")
                if product_title:
                    desc_parts.append(f"产品名：{product_title}")
                if props_text:
                    desc_parts.append(f"规格：{props_text}")
                if price:
                    desc_parts.append(f"价格：{price}")
                if moq:
                    desc_parts.append(f"起订量：{moq}")
                content = "；".join(desc_parts) if desc_parts else name

                all_results.append({
                    "name": name,
                    "content": content,
                    "hit_keyword": hit_keyword or keyword,
                    "business_type": business_type,
                    "location": location,
                    "badges": badges,
                    "product_title": product_title,
                    "product_link": product_link,
                    "price": price,
                    "moq": moq,
                })

            total = result.get("totalItems", 0)
            print(f"MCP search_products'{search_keyword}'第{page}页：{new_count}家供应商，累计{len(all_results)}家（目标{target_count}家，总产品数{total}）")

            if len(all_results) >= target_count:
                break

            time.sleep(1)  # 翻页间隔1秒，避免触发限流

        print(f"MCP搜索完成（搜产品）：英文关键词'{search_keyword}' → {len(all_results)}家供应商")

        # 产品名批量翻译成中文（业务部门看英文不方便）
        if all_results:
            _translate_product_names_batch(all_results)

        return all_results

    finally:
        client.close()


def crawl_made_in_china(keyword, hit_keyword="", variants=None):
    """
    从中国制造网（Made-in-China）获取供应商信息（只用MCP服务）

    完全按SKILL文档逻辑：直接用search_suppliers工具搜索供应商。
    只用英文关键词搜索，中文关键词直接跳过（由 crawl_made_in_china_mcp 内部处理）。
    返回供应商的公司名、业务类型、主营产品、地区、认证徽章。

    参数：
        keyword: 搜索关键词（英文词直接搜；中文词会被跳过返回空）
        hit_keyword: 命中的P0-P3关键词标签
        variants: 变体关键词列表（已不再使用，保留是为了兼容调用方）

    返回：供应商列表，每条包含 name, content, hit_keyword, business_type, location, badges
    """
    return crawl_made_in_china_mcp(keyword, hit_keyword, variants)


# ==================== 1688 官方API（供应商搜索）====================
# 用 skills-gateway.1688.com 网关 + /api/1688_source_suppliers/1.0.0 接口
# 直接传入关键词，返回工厂/供应商列表（公司名、合作方式、服务、地区等）
#
# 注意：原来用 ainext.1688.com 的两步流程(searchoffer+workflow)已弃用，
#   因为 ainext 网关持续返回429限流。改用 skills-gateway 单步API更稳定。
# 签名方式：HMAC-SHA256（AK解码→MD5→拼接签名串→HMAC加密）

# 1688 API网关地址（skills-gateway，与官方SKILL一致）
_1688_API_HOST = "https://skills-gateway.1688.com"
# 供应商搜索API路径（单步，直接返回工厂列表）
_1688_SOURCE_SUPPLIERS_URI = "/api/1688_source_suppliers/1.0.0"
# 小白讲解：新增"搜产品"接口，先搜产品再从产品里拿供应商公司名，
# 这样搜到的供应商一定做这个产品，解决"产品不适配"问题
_1688_FIND_PRODUCT_URI = "/api/find_product/1.0.0"
# 签名版本号（与官方SKILL一致用1.0.0）
_SKILL_VERSION = "1.0.0"

# ==================== 1688 调用限速控制 ====================
# 小白讲解：因为多个关键词是并发搜索的（最多3个线程同时跑），如果每个线程
# 各自sleep(5)秒，3个线程会在5秒后同时发请求，照样触发限流。所以用一把
# 全局锁把所有1688调用排成一队，按"上次调用时间"补睡剩余时间，真正错开。
_1688_call_lock = threading.Lock()       # 全局锁，保证同一时刻只有一个线程在判断/更新调用时间
_1688_last_call_time = 0.0               # 上次1688 API调用的完成时间戳
_1688_CALL_INTERVAL = 5                  # 两次1688搜索调用之间的最小间隔秒数


def _wait_1688_rate_limit():
    """
    1688调用前限速：确保两次1688 API搜索调用之间至少间隔5秒

    小白讲解：这个函数在每次调用1688搜索接口前执行。
    - 拿到全局锁后，看上次调用是什么时候
    - 如果距离上次调用不到5秒，就补睡"5秒 - 已过时间"
    - 补睡完更新"上次调用时间"为当前时间，然后释放锁去发请求
    这样不管多少个线程并发，1688的调用都会自动排队，两次调用至少隔5秒，避免被限流(429)。
    """
    global _1688_last_call_time
    with _1688_call_lock:
        now = time.time()
        elapsed = now - _1688_last_call_time
        if elapsed < _1688_CALL_INTERVAL:
            wait = _1688_CALL_INTERVAL - elapsed
            print(f"1688限速等待：距上次调用{elapsed:.1f}秒，补睡{wait:.1f}秒后再次调用")
            time.sleep(wait)
        # 更新上次调用时间为"现在"（即即将发起调用的时刻）
        _1688_last_call_time = time.time()


def _parse_1688_ak():
    """
    解析1688的AK（Access Key）

    小白讲解：AK是一个base64编码的字符串，解码后：
    - 前32个字符 = AccessKeySecret（用于签名加密的"密钥"）
    - 后面的字符 = AccessKeyID（标识"你是谁"）

    返回：(access_key_id, access_key_secret)
    如果AK未配置返回 (None, None)

    小白讲解：AK现在从数据库读取（通过_get_1688_ak），不再用config.py的硬编码，
    管理员可在管理中心修改1688服务商的api_key实现热更新。
    """
    ak = _get_1688_ak()
    if not _is_1688_ak_configured():
        return None, None
    try:
        decoded = base64.urlsafe_b64decode(ak).decode("utf-8")
    except Exception:
        decoded = ak
    if len(decoded) < 32:
        return None, None
    return decoded[32:], decoded[:32]


def _build_1688_sign_headers(uri, body_str):
    """
    构造1688 API的带签名请求头

    小白讲解签名流程（5步）：
    1. 算请求体的MD5作为"内容指纹"（防止数据被篡改）
    2. 拼签名头：时间戳、随机数nonce、AK身份、MD5、版本号
    3. 把签名头按key排序，拼成规范格式（每行"小写key:值"）
    4. 拼出"待签名字符串"：POST\n+MD5\n+content-type\n+时间戳\n+签名头+URI
    5. 用Secret对这个字符串做HMAC-SHA256加密，得到签名

    返回：完整的请求头字典（含签名）
    """
    ak_id, ak_secret = _parse_1688_ak()
    if not ak_id or not ak_secret:
        return None

    content_md5 = base64.b64encode(
        hashlib.md5(body_str.encode('utf-8')).digest()
    ).decode('utf-8')
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex[:8]
    csk_headers = {
        "x-csk-ak": ak_id,
        "x-csk-time": timestamp,
        "x-csk-nonce": nonce,
        "x-csk-content-md5": content_md5,
        "x-csk-version": _SKILL_VERSION,
    }
    canonicalized_headers = ""
    for key in sorted(csk_headers.keys()):
        canonicalized_headers += f"{key.lower()}:{csk_headers[key].strip()}\n"
    string_to_sign = (
        "POST" + "\n" +
        content_md5 + "\n" +
        "application/json" + "\n" +
        timestamp + "\n" +
        canonicalized_headers +
        uri
    )
    signature = hmac.new(
        ak_secret.encode('utf-8'),
        string_to_sign.encode('utf-8'),
        hashlib.sha256
    ).digest()
    sign_base64 = base64.b64encode(signature).decode('utf-8')
    return {
        "Content-Type": "application/json",
        "x-csk-sign": sign_base64,
        **csk_headers,
    }


def _1688_api_post(uri, body_obj, timeout=60, max_retries=2):
    """
    1688 API通用POST请求（带签名 + 流式响应 + 限流重试）

    参考官方 skills/1688-source-suppliers/scripts/_http.py 的 api_post_stream 实现：
    - 使用 stream=True 流式接收响应（供应商数据较大，避免响应被截断）
    - 分块拼接后解析JSON
    - 遇到限流(429/msgCode含429)时自动等待重试，最多重试2次

    性能优化：限流等待从30/60/120秒减到5/10秒，重试次数从3减到2
    （限流后重试也经常失败，快速返回让搜索流程继续，用MCP的结果补上）

    参数：
        uri: API路径（如 /api/1688_source_suppliers/1.0.0）
        body_obj: 请求体字典
        timeout: 超时秒数（默认60秒，流式响应较慢）
        max_retries: 遇到限流时最大重试次数

    返回：解析后的JSON字典，失败返回None
    """
    import time as _time

    body_str = json.dumps(body_obj, ensure_ascii=False)

    for attempt in range(max_retries):
        # 每次重试都重新生成签名（因为时间戳变了）
        headers = _build_1688_sign_headers(uri, body_str)
        if not headers:
            return None
        try:
            # 流式请求（参考官方实现：stream=True + iter_content 拼接）
            resp = requests.post(
                _1688_API_HOST + uri,
                data=body_str.encode('utf-8'),
                headers=headers,
                timeout=timeout,
                stream=True,
            )
            if resp.status_code != 200:
                print(f"1688 API HTTP {resp.status_code}（第{attempt+1}次）")
                if resp.status_code == 429 and attempt < max_retries - 1:
                    # 限流等待时间递增：5秒、10秒（快速重试，不卡死整个搜索流程）
                    wait = 5 * (2 ** attempt)
                    print(f"1688 API限流(429)，等待{wait}秒后重试（第{attempt+1}/{max_retries}次）...")
                    _time.sleep(wait)
                    continue
                return None

            # 流式分块拼接完整内容
            all_chunks = []
            try:
                for chunk in resp.iter_content(chunk_size=None, decode_unicode=True):
                    if chunk:
                        all_chunks.append(chunk)
            except Exception as e:
                print(f"1688 API流式读取异常: {e}")
                if attempt < max_retries - 1:
                    _time.sleep(3)
                    continue
                return None

            full_content = "".join(all_chunks)
            if not full_content:
                print(f"1688 API未获取到流式数据（第{attempt+1}次）")
                if attempt < max_retries - 1:
                    _time.sleep(3)
                    continue
                return None

            data = json.loads(full_content)

            # 业务错误处理（HTTP 200 但 success=false）
            if not data.get("success"):
                msg_code = str(data.get("msgCode", ""))
                msg_info = data.get("msgInfo", "")
                # 限流时等待重试（rateLimit是1688的限流标识）
                if "429" in msg_code or "rateLimit" in str(msg_info).lower() or "rate" in str(msg_info).lower():
                    # 限流等待时间递增：5秒、10秒（快速重试，不卡死整个搜索流程）
                    wait = 5 * (2 ** attempt)
                    print(f"1688 API限流({msg_code})，等待{wait}秒后重试（第{attempt+1}/{max_retries}次）...")
                    _time.sleep(wait)
                    continue
                # 其他业务错误不重试，直接返回
                print(f"1688 API业务错误: {msg_code} - {msg_info}")
                return data

            return data

        except json.JSONDecodeError as e:
            print(f"1688 API JSON解析失败: {e}（第{attempt+1}次）")
            if attempt < max_retries - 1:
                _time.sleep(3)
                continue
            return None
        except Exception as e:
            print(f"1688 API请求异常: {e}（第{attempt+1}次）")
            if attempt < max_retries - 1:
                _time.sleep(3)
                continue
            return None

    return None


def _parse_json_field(field_value):
    """
    解析1688 API返回的JSON数组字段

    小白讲解：1688返回的某些字段是字符串形式的JSON数组，
    比如 '[\"OEM\",\"ODM\"]'，需要解析成真正的列表 ["OEM", "ODM"]。
    """
    if not field_value:
        return []
    try:
        result = json.loads(field_value)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _extract_1688_factories(result):
    """
    从1688 API响应中提取工厂/供应商数据

    小白讲解：1688的API返回数据是嵌套结构，需要逐层剥开：
    1. 先找 originResponses 数组
    2. 在里面找 currentPhase == "RETRIEVAL" 的元素
    3. 取 responseData.data 就是工厂列表

    返回：工厂字典列表，每个包含 companyName、cooperationMode、services 等
    """
    # 尝试两种路径找 originResponses
    origin = result.get("originResponses", [])
    if not origin:
        origin = result.get("data", {}).get("result", {}).get("originResponses", [])

    for item in origin:
        if not isinstance(item, dict):
            continue
        if item.get("currentPhase") == "RETRIEVAL":
            resp_data = item.get("responseData", {})
            if isinstance(resp_data, dict):
                data_list = resp_data.get("data", [])
                if isinstance(data_list, list) and len(data_list) > 0:
                    return data_list
    return []


def _crawl_1688_once(keyword, hit_keyword=""):
    """
    单次调用1688"搜产品"API，从产品中提取供应商信息（一次返回约30家）

    小白讲解：原来调供应商搜索接口（source_suppliers），会出现"产品不适配"问题。
    现在改成调"搜产品"接口（find_product），每条产品自带供应商公司名(company字段)，
    这样搜到的供应商一定做这个产品。一次返回30个产品，外层 crawl_1688 用变体词多次调用凑够50家。

    参数：
        keyword: 搜索关键词（如"玻璃电视柜"）
        hit_keyword: 命中的P0-P3关键词标签

    返回：供应商列表，每条包含：
        - name: 公司名称
        - content: 供应商详细描述（供DeepSeek过滤参考）
        - hit_keyword: 命中关键词
        - product_title: 产品名称（中文，无需翻译）
        - product_link: 产品链接
        - price: 价格
        - moq: 起订量
    """
    if not _is_1688_ak_configured():
        print(f"1688 AK未配置，跳过'{keyword}'的搜索（请在管理中心配置1688服务商的api_key）")
        return []

    # 调用 skills-gateway "搜产品"API
    # 小白讲解：scoreLevel=high保证相关性高
    # pageSize从管理中心配置读取（管理员可在"搜索平台管理"页面调整最大结果数）
    max_results = _get_platform_max_results("ali1688", default=100)

    # 小白讲解：调用1688搜索接口前先限速，保证两次调用间隔至少5秒，避免触发限流(429)
    # 即使3个关键词并发搜索，这里也会自动排队错开，不会同时发请求
    _wait_1688_rate_limit()

    data = _1688_api_post(_1688_FIND_PRODUCT_URI, {
        "query": keyword,
        "pageSize": max_results,
        "purchaseAmount": 1,
        "scoreLevel": "high",
    }, timeout=60)
    if not data or not data.get("success"):
        print(f"1688搜产品'{keyword}'失败: {data.get('msgInfo') if data else '无响应'}")
        return []

    # 小白讲解：find_product的产品列表在 data.data 里（嵌套一层）
    # 响应结构：{success, data: {data: [产品列表], count, intent}}
    outer_data = data.get("data") or {}
    if isinstance(outer_data, dict):
        products = outer_data.get("data") or []
    elif isinstance(outer_data, list):
        products = outer_data
    else:
        products = []

    if not products:
        print(f"1688搜产品'{keyword}'未获取到产品")
        return []

    print(f"1688搜产品'{keyword}'：获取到 {len(products)} 个产品")

    # 组装供应商数据（从产品中提取供应商公司名和产品信息）
    results = []
    for product in products:
        if not isinstance(product, dict):
            continue

        # 小白讲解：从产品里提取供应商公司名（company字段）
        company_name = (product.get("company") or product.get("supplier") or "").strip()
        if not company_name:
            continue

        # 产品信息（1688产品名是中文，不需要翻译）
        product_title = (product.get("title") or product.get("name") or "").strip()
        product_link = (product.get("detailUrl") or product.get("url") or "").strip()
        # 价格和起订量分开存储（1688返回 currentPrice 和 quantityBegin）
        price = str(product.get("currentPrice") or product.get("price") or "").strip()
        moq = str(product.get("quantityBegin") or product.get("moq") or "").strip()
        if moq and not moq.endswith("件"):
            moq = f"{moq}件"
        sku_title = (product.get("skuTitle") or "").strip()

        # 组装供应商描述（供DeepSeek过滤参考）
        # 小白讲解：把搜索品类+产品名+规格组合成描述，
        # DeepSeek能结合"搜的是什么+具体产品+公司名"综合判断供应商是否合适
        desc_parts = []
        desc_parts.append(f"搜索品类：{keyword}")
        if product_title:
            desc_parts.append(f"产品名：{product_title}")
        if sku_title:
            desc_parts.append(f"规格：{sku_title}")
        if price:
            desc_parts.append(f"价格：¥{price}")
        if moq:
            desc_parts.append(f"起订量：{moq}")
        content = "；".join(desc_parts) if desc_parts else company_name

        results.append({
            "name": company_name,
            "content": content,
            "hit_keyword": hit_keyword or keyword,
            "product_title": product_title,
            "product_link": product_link,
            "price": price,
            "moq": moq,
        })

    print(f"1688搜产品'{keyword}'：{len(products)}个产品 → {len(results)}家供应商")
    return results


def crawl_1688(keyword, hit_keyword="", variants=None, target_count=50):
    """
    用1688"搜产品"API搜索供应商，直接用原始关键词搜一次（不再用变体词）

    小白讲解：改造前每次只返回约10家供应商，需要用变体词多次搜索凑够50家。
    改造后调"搜产品"接口，一次返回30个产品（约28家供应商），单次就够用，
    不再需要变体词扩搜。variants参数保留只是为了兼容调用方，内部已不使用。

    参数：
        keyword: 原始搜索关键词（如"电视柜"）
        hit_keyword: 命中的P0-P3关键词标签
        variants: AI生成的变体关键词列表（已不再使用，保留参数兼容调用方）
        target_count: 目标供应商数量（已不再使用，保留参数兼容调用方）

    返回：去重后的供应商列表（约28家，一次搜索的实际结果）
    """
    # 直接用原始关键词搜一次，按公司名去重后返回
    all_results = []
    seen_names = set()

    batch = _crawl_1688_once(keyword, hit_keyword)
    for r in batch:
        name = r.get("name", "")
        if name and name not in seen_names:
            seen_names.add(name)
            all_results.append(r)

    print(f"1688'{keyword}'最终：{len(all_results)}家（单次搜产品，不再用变体）")
    return all_results


# ==================== 天眼查 MCP 客户端 ====================
class TianyanchaClient:
    """
    天眼查MCP客户端 - 用于查询供应商的工商信息

    使用流程：
    1. initialize() 初始化连接，获取session ID
    2. search_companies(query) 搜索公司，获取候选列表
    3. get_company_basic_profile(company_name) 获取公司详细信息

    天眼查返回的信息包括：企业名称、注册资本、法定代表人、登记状态、
    工商简介、联系方式（电话/邮箱）、地址等。

    小白讲解：天眼查MCP的url和授权码现在从数据库读取（通过get_provider("tianyancha")），
    管理员可在管理中心修改服务商配置实现热更新，不再用config.py的硬编码。
    """

    def __init__(self):
        self.session_id = None
        # 从数据库读取天眼查MCP配置（base_url=MCP地址，api_key=授权码）
        provider = get_provider("tianyancha") or {}
        self.mcp_url = provider.get("base_url", "")
        self.mcp_auth = provider.get("api_key", "")
        self.headers = {
            "Authorization": self.mcp_auth,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-03-26",
        }

    def _call(self, method, params=None, msg_id=None):
        """
        调用MCP工具的通用方法，自动处理session和响应格式

        小白讲解：这个方法负责和天眼查服务器通信。
        以前请求失败就直接返回None，导致初筛把"网络超时"误判成"查不到企业"。
        现在改成：遇到429频率限制或网络超时时自动重试3次，间隔递增（3秒/6秒/9秒）。
        如果重试3次还是失败，返回None让上层区分处理。
        """
        if self.session_id:
            self.headers["Mcp-Session-Id"] = self.session_id

        payload = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        if msg_id is not None:
            payload["id"] = msg_id

        # 容错：未配置MCP地址时直接返回None，避免requests抛MissingSchema异常
        if not self.mcp_url:
            print("天眼查MCP地址未配置，跳过调用（请在管理中心配置tianyancha服务商的base_url）")
            return None

        # 重试配置：最多3次，间隔递增（3/6/9秒）
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(self.mcp_url, headers=self.headers, json=payload, timeout=30)

                # 429频率限制：等待后重试
                if resp.status_code == 429:
                    if attempt < max_retries:
                        wait = attempt * 3
                        print(f"天眼查MCP频率限制(429)，第{attempt}/{max_retries}次重试，等待{wait}秒...")
                        time.sleep(wait)
                        continue
                    else:
                        print(f"天眼查MCP频率限制(429)，已重试{max_retries}次仍失败")
                        return None

                # 5xx服务器错误：等待后重试
                if 500 <= resp.status_code < 600:
                    if attempt < max_retries:
                        wait = attempt * 3
                        print(f"天眼查MCP服务器错误({resp.status_code})，第{attempt}/{max_retries}次重试，等待{wait}秒...")
                        time.sleep(wait)
                        continue
                    else:
                        print(f"天眼查MCP服务器错误({resp.status_code})，已重试{max_retries}次仍失败")
                        return None

                # 其他4xx错误（如401授权失败）：不重试，直接返回None
                if resp.status_code >= 400:
                    print(f"天眼查MCP返回HTTP {resp.status_code}: {resp.text[:300]}")
                    return None

                # 通知类消息没有响应体
                if msg_id is None:
                    return None

                # 解析响应（可能是JSON或SSE格式）
                ct = resp.headers.get("content-type", "")
                if "event-stream" in ct:
                    for line in resp.text.split("\n"):
                        if line.startswith("data: ") and line[6:].strip():
                            return json.loads(line[6:].strip())
                else:
                    if resp.text.strip():
                        return resp.json()
                return None

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                # 网络超时或连接失败：等待后重试
                if attempt < max_retries:
                    wait = attempt * 3
                    print(f"天眼查MCP网络超时({method})，第{attempt}/{max_retries}次重试，等待{wait}秒...: {e}")
                    time.sleep(wait)
                    continue
                else:
                    print(f"天眼查MCP网络超时({method})，已重试{max_retries}次仍失败: {e}")
                    return None
            except requests.exceptions.RequestException as e:
                print(f"天眼查MCP请求失败({method}): {e}")
                return None
            except json.JSONDecodeError as e:
                print(f"天眼查MCP响应解析失败({method}): {e}")
                return None

        return None

    def initialize(self):
        """初始化MCP连接，获取session ID"""
        # 容错：未配置MCP地址时直接返回False
        if not self.mcp_url:
            print("天眼查MCP地址未配置，跳过初始化")
            return False
        try:
            resp = requests.post(self.mcp_url, headers=self.headers, json={
                "jsonrpc": "2.0", "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "sourcing-system", "version": "1.0"},
                },
                "id": 1,
            }, timeout=15)
            self.session_id = resp.headers.get("Mcp-Session-Id", "")
            if self.session_id:
                self.headers["Mcp-Session-Id"] = self.session_id
                # 发送初始化完成通知
                self._call("notifications/initialized")
            return self.session_id is not None
        except requests.exceptions.RequestException as e:
            print(f"天眼查MCP初始化失败: {e}")
            return False

    def search_companies(self, query):
        """
        搜索公司 - 用关键词搜索天眼查企业数据库

        参数：query - 公司名关键词
        返回：
            - 成功：企业候选列表（可能为空列表[]，表示确实没找到）
            - 请求失败：返回None（区别于空列表，让上层能区分"网络失败"和"确实没找到"）
        """
        if not self.session_id:
            self.initialize()
        result = self._call("tools/call", {
            "name": "search_companies",
            "arguments": {"query": query},
        }, msg_id=100)
        # 请求失败（网络超时/频率限制等）返回None，让上层区分处理
        if result is None:
            return None
        if not result or "result" not in result:
            return None
        # 解析返回的文本（Markdown表格格式）
        content = result["result"].get("content", [])
        if content:
            return self._parse_companies(content[0].get("text", ""))
        return []

    def get_company_basic_profile(self, company_name):
        """
        获取公司基础工商信息 - 包括注册资本、简介、联系方式、地址等

        参数：company_name - 企业全称（从search_companies结果中获取）
        返回：公司详细信息字典
        """
        if not self.session_id:
            self.initialize()
        result = self._call("tools/call", {
            "name": "get_company_basic_profile",
            "arguments": {"company_name": company_name},
        }, msg_id=200)
        if not result or "result" not in result:
            return {}
        content = result["result"].get("content", [])
        if content:
            return self._parse_basic_profile(content[0].get("text", ""))
        return {}

    def _parse_companies(self, text):
        """
        从天眼查返回的Markdown表格中解析企业候选列表

        小白讲解：天眼查返回的是Markdown表格，格式如下：
        | 序号 | 企业名称 | 统一社会信用代码 | 登记状态 | 法定代表人 | 注册资本 | ... |
        |---|---|---|---|---|---|
        | 1 | xxx公司 | 91xxx | 存续 | 张三 | 100万人民币 |

        之前代码按"列位置"硬编码取值（如 cells[5] 当注册资本），
        问题：如果某家公司某列为空（导致列数减少）或表头列顺序变化，
              就会把"成立日期"等字段误读成注册资本。

        修复思路：先解析表头行确定每列的列名和位置，
                  再按列名取值（不再依赖固定位置），并校验注册资本不是日期格式。
        """
        companies = []
        lines = text.split("\n")

        # 第一步：找到表头行，建立"列名 -> 列索引"的映射
        # 表头行特征：包含"企业名称"和"注册资本"等关键字
        header_index = {}  # 列名 -> 列下标
        for line in lines:
            if line.startswith("|") and "企业名称" in line:
                # 按竖线分割，去掉首尾空单元格
                raw_cells = [c.strip() for c in line.split("|")]
                raw_cells = [c for c in raw_cells if c != ""]
                for idx, cell in enumerate(raw_cells):
                    # 把列名标准化（去空格、统一关键字），方便后面匹配
                    cell_clean = cell.replace(" ", "").strip()
                    header_index[cell_clean] = idx
                break

        # 第二步：按列名取数据行
        for line in lines:
            if not line.startswith("|"):
                continue
            # 跳过表头行和分隔行
            if "企业名称" in line or "---" in line:
                continue

            # 按竖线分割数据行（注意：空单元格也要保留位置，所以不能像表头那样过滤）
            # split("|") 后首尾会有空字符串，需要处理
            parts = line.split("|")
            # 去掉首尾的空字符串（Markdown表格首尾都是竖线）
            if parts and parts[0] == "":
                parts = parts[1:]
            if parts and parts[-1] == "":
                parts = parts[:-1]
            cells = [c.strip() for c in parts]

            # 没有表头映射时，回退到旧的硬编码逻辑（兼容旧版天眼查返回格式）
            if not header_index:
                if len(cells) >= 6:
                    # 校验"注册资本"列不是日期格式（防止列错位把成立日期当注册资本）
                    capital = cells[5]
                    if re.match(r"^\d{4}[-/年]\d{1,2}[-/月]?\d{0,2}日?$", capital):
                        capital = ""  # 是日期格式，置空避免脏数据
                    # 从原始行文本中提取匹配类型（天眼查用英文名搜索时会在表格中标注）
                    # 小白讲解：fallback路径也必须解析match_type，否则MIC英文公司名
                    # 后续的"英文名匹配"逻辑永远无法触发，全部走not_found
                    match_type = ""
                    if "英文名匹配" in line:
                        match_type = "英文名匹配"
                    elif "精确同名" in line:
                        match_type = "精确同名"
                    companies.append({
                        "name": cells[1],
                        "credit_code": cells[2],
                        "status": cells[3],
                        "legal_person": cells[4],
                        "registered_capital": capital,
                        "match_type": match_type,
                    })
                continue

            # 有表头映射时，按列名取值（更稳健，不怕列顺序变化或空单元格）
            def _get_cell(col_name):
                """根据列名取单元格值，列不存在时返回空字符串"""
                idx = header_index.get(col_name)
                if idx is None or idx >= len(cells):
                    return ""
                return cells[idx]

            name = _get_cell("企业名称")
            if not name:
                continue

            capital = _get_cell("注册资本")
            # 校验注册资本不是日期格式（防止天眼查返回的某些脏数据把日期误读成注册资本）
            if re.match(r"^\d{4}[-/年]\d{1,2}[-/月]?\d{0,2}日?$", capital):
                capital = ""

            companies.append({
                "name": name,
                "credit_code": _get_cell("统一社会信用代码"),
                "status": _get_cell("登记状态"),
                "legal_person": _get_cell("法定代表人"),
                "registered_capital": capital,
                # 匹配类型：天眼查会告诉我们是怎么匹配到这家公司的，
                # 例如"英文名匹配"表示用英文名查到了对应的中文名公司。
                # 这个字段对MIC等英文公司名的匹配非常关键，后面匹配逻辑会用到。
                "match_type": _get_cell("匹配类型"),
            })
        return companies

    def _parse_basic_profile(self, text):
        """
        从天眼查返回的Markdown文本中解析公司基础信息

        天眼查MCP返回的是Markdown格式，字段在表格里，例如：
        | 字段 | 值 |
        |---|---|
        | 联系电话 | 0769-87003190 |
        | 邮箱 | xxx@qq.com |
        | 注册资本 | 50万人民币 |

        还有一个"联系电话明细"表格，包含多个电话号码（座机和手机）。
        按文档2要求：电话格式只接受手机号 ^1[3-9]\\d{9}$ 或座机 ^0\\d{2,3}-?\\d{7,8}$，
        手机和座机都有时优先取手机。
        """
        info = {}

        # 第一部分：解析"概览字段"表格，提取主要字段
        # 天眼查返回格式：| 字段名 | 值 |
        field_patterns = {
            "registered_capital": [r"\|\s*注册资本\s*\|\s*([^|]+?)\s*\|"],
            "email": [r"\|\s*邮箱\s*\|\s*([^|]+?)\s*\|"],
            "address": [r"\|\s*注册地址\s*\|\s*([^|]+?)\s*\|"],
            "legal_person": [r"\|\s*法定代表人\s*\|\s*([^|]+?)\s*\|"],
            "status": [r"\|\s*登记状态\s*\|\s*([^|]+?)\s*\|"],
            "business_scope": [r"\|\s*经营范围\s*\|\s*([^|]+?)\s*\|"],
            "establish_date": [r"\|\s*成立日期\s*\|\s*([^|]+?)\s*\|"],
            "company_type": [r"\|\s*企业类型\s*\|\s*([^|]+?)\s*\|"],
        }
        for field, patterns in field_patterns.items():
            for pattern in patterns:
                match = re.search(pattern, text)
                if match:
                    info[field] = match.group(1).strip()
                    break

        # 第二部分：提取企业简介（在"企业简介"章节的profile字段）
        # 格式：| profile | xxx |
        intro_match = re.search(r"\|\s*profile\s*\|\s*([^|]+?)\s*\|", text)
        if intro_match:
            info["intro"] = intro_match.group(1).strip()

        # 第三部分：提取联系电话（按文档2的格式校验规则）
        # 优先从"联系电话明细"表格提取手机号，其次座机
        # 明细表格格式：| # | 来源 | 号码类型 | 联系电话 | 标签 |
        phone = self._extract_valid_phone(text)
        if phone:
            info["phone"] = phone

        # 如果明细表没有，尝试从概览表取"联系电话"字段
        if "phone" not in info:
            phone_match = re.search(r"\|\s*联系电话\s*\|\s*([^|]+?)\s*\|", text)
            if phone_match:
                raw_phone = phone_match.group(1).strip()
                validated = self._validate_phone(raw_phone)
                if validated:
                    info["phone"] = validated

        # 整段文本作为raw备用（截取前3000字符避免太长）
        info["raw"] = text[:3000]
        return info

    def _extract_valid_phone(self, text):
        """
        从"联系电话明细"表格中提取有效电话号码

        按文档2规则：
        - 只接受手机号 ^1[3-9]\\d{9}$ 或座机 ^0\\d{2,3}-?\\d{7,8}$
        - 手机和座机都有时优先取手机
        - "号码类型"为"正常电话"的优先（标签含"号码可正常联系"）
        """
        # 匹配明细表格行：| # | 来源 | 号码类型 | 联系电话 | 标签 |
        # 号码类型可能是：大陆座机、正常电话、大陆手机等
        rows = re.findall(
            r"\|\s*\d+\s*\|\s*([^|]+)\|\s*([^|]+)\|\s*([\d\-]+)\s*\|\s*([^|]+)\|",
            text
        )

        mobile = ""    # 手机号
        landline = ""  # 座机号
        for source, num_type, number, label in rows:
            number = number.strip()
            # 手机号：1开头的11位数字
            if re.match(r"^1[3-9]\d{9}$", number):
                # 优先取"正常电话"标签的
                if "正常" in label or not mobile:
                    mobile = number
            # 座机：0开头，格式 0XX-XXXXXXXX 或 0XXXXXXXX
            elif re.match(r"^0\d{2,3}-?\d{7,8}$", number):
                if "正常" in label or not landline:
                    landline = number

        # 手机优先，其次座机
        return mobile if mobile else landline

    def _validate_phone(self, raw_phone):
        """校验电话号码格式，只接受手机号或合法座机"""
        raw_phone = raw_phone.strip()
        # 手机号
        if re.match(r"^1[3-9]\d{9}$", raw_phone):
            return raw_phone
        # 座机
        if re.match(r"^0\d{2,3}-?\d{7,8}$", raw_phone):
            return raw_phone
        # 尝试从字符串中提取手机号
        mobile_match = re.search(r"1[3-9]\d{9}", raw_phone)
        if mobile_match:
            return mobile_match.group()
        # 尝试提取座机
        landline_match = re.search(r"0\d{2,3}-?\d{7,8}", raw_phone)
        if landline_match:
            return landline_match.group()
        return ""


# ==================== DeepSeek 提取+过滤供应商 ====================
# 小白讲解：DeepSeek 的调用统一走 ai_helper.call_deepseek，这里不再重复定义
# 好处是只维护一份代码，状态码检查、思考模式、缓存日志等都集中在一处


def _smart_truncate(text, max_len=400):
    """
    智能截断文本：按分号切句，保留完整句子直到接近上限

    小白讲解：原来直接 [:200] 硬截断，会把一句话从中间切断，
    DeepSeek 看到半截信息容易误判供应商类型。
    现在按中英文分号（；;）一句一句加，加到接近 400 字符就停，
    保证每句话都是完整的。如果单句本身就超过上限，才对那句硬截断。

    参数：
        text: 原始文本
        max_len: 最大字符数，默认 400
    返回：截断后的文本（空文本返回空字符串）
    """
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    # 按中英文分号切句，保留完整句子
    sentences = re.split(r'[；;]', text)
    result = ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        # 加上这句（含分隔符）没超上限就加进去
        if len(result) + len(s) + 1 <= max_len:
            result += s + "；"
        else:
            # 这句加不下：若一句都没收进来（第一句就超长），硬截断这句
            if not result:
                result = s[:max_len]
            break
    return result.rstrip("；") or text[:max_len]


def extract_company_names(search_results):
    """
    从搜索结果中提取真实公司名称

    支持两种数据来源：
    1. 1688官方API：已包含准确的name字段（公司名），直接使用，无需正则提取
    2. 中国制造网爬虫：从title和content文本中用正则提取公司名

    中国公司名称格式：地域 + 字号 + 行业 + 组织形式
    组织形式包括：有限公司、有限责任公司、厂、制品厂、加工厂、制造厂等

    参数：
        search_results: 搜索结果列表（来自1688 API和中国制造网爬虫）

    返回：去重后的公司名称列表，每条附带来源信息
    """
    # 公司名称的组织形式后缀（按优先级排列）
    # 有限公司/有限责任公司 是最规范的，优先匹配
    # 各种工厂名称次之
    company_patterns = [
        # xxx有限公司（含地域、字号、行业）
        r'([\u4e00-\u9fa5]{2,4}(?:省|市|区|县)?[\u4e00-\u9fa5]{2,10}(?:有限公司|有限责任公司|股份有限公司|股份公司))',
        # xxx厂/制品厂/加工厂/制造厂/家具厂/玻璃厂等（含地域）
        r'([\u4e00-\u9fa5]{2,4}(?:省|市|区|县)?[\u4e00-\u9fa5]{2,10}(?:制品厂|加工厂|制造厂|家具厂|玻璃厂|五金厂|电子厂|塑胶厂|模具厂|机械厂|金属制品厂|木业厂|建材厂|陶瓷厂))',
        # xxx厂（较短的工厂名）
        r'([\u4e00-\u9fa5]{4,12}厂)',
    ]

    seen_names = set()
    companies = []

    for r in search_results:
        hit_kw = r.get("hit_keyword", "")
        source_platform = r.get("source_platform", "")

        # 情况1：1688 API和MCP返回的数据已有准确的name字段，直接使用
        # （API/MCP返回的公司名最准确，不需要正则重新提取）
        if r.get("name"):
            name = r["name"].strip()
            if name and name not in seen_names and 4 <= len(name) <= 40:
                seen_names.add(name)
                company = {
                    "name": name,
                    "hit_keyword": hit_kw,
                    "source_platform": source_platform,
                    "source_text": _smart_truncate(r.get("content", ""), 400),
                    "business_type": r.get("business_type", ""),  # MCP的businessType字段
                    "location": r.get("location", ""),             # MCP的location字段
                    "badges": r.get("badges", ""),                 # MCP的badges字段
                    # 产品字段（MCP搜产品方式才有，1688无此字段）
                    "product_title": r.get("product_title", ""),
                    "product_link": r.get("product_link", ""),
                    "price": r.get("price", ""),
                    "moq": r.get("moq", ""),
                    # 海关出口数据字段（海关数据来源才有，其他平台为0）
                    "customs_export_count": r.get("customs_export_count", 0),
                    "customs_total_qty": r.get("customs_total_qty", 0),
                    "customs_total_amount": r.get("customs_total_amount", 0),
                }
                companies.append(company)
            continue

        # 情况2：中国制造网爬虫的数据，从文本中用正则提取公司名
        text = r.get("content", "")

        for pattern in company_patterns:
            matches = re.findall(pattern, text)
            for name in matches:
                name = name.strip()
                # 过滤掉太短或太长的名称
                if len(name) < 4 or len(name) > 30:
                    continue
                # 过滤掉一些常见的非公司名误匹配
                if any(skip in name for skip in ["本公司", "该厂", "我厂", "原厂", "厂家", "工厂店", "源头厂"]):
                    continue
                if name not in seen_names:
                    seen_names.add(name)
                    companies.append({
                        "name": name,
                        "hit_keyword": hit_kw,
                        "source_platform": source_platform,  # 来源平台（1688/Made-in-China）
                        "source_text": _smart_truncate(text, 400),    # 保留来源文本供DeepSeek判断（智能截断）
                    })

    return companies


def _extract_core_name(name):
    """
    提取公司名的中文核心字号，用于跨平台去重

    小白讲解：1688 返回"深圳市XX科技有限公司"，MCP 翻译后变成
    "深圳市XX科技有限公司（Shenzhen XX Technology Co., Ltd.）"，
    字符串不一样但其实是同一家公司。去掉括号里的英文、去掉"有限公司"等后缀后，
    剩下的"深圳市XX科技"就是核心字号，用它来比较就能识别同一家公司。

    参数：name 原始公司名
    返回：核心字号（去空格、转小写）
    """
    if not name:
        return ""
    # 去掉括号及括号内内容（中英文括号都处理，把 MCP 翻译后的英文部分去掉）
    core = re.sub(r'[（(].*?[)）]', '', name)
    # 去掉常见公司组织形式后缀（长的先去，避免"有限责任公司"被当成"公司"去掉只剩"有限责任"）
    for suffix in ['有限责任公司', '股份有限公司', '有限公司', '股份公司', '责任公司',
                   '制品厂', '加工厂', '制造厂', '家具厂', '玻璃厂', '五金厂',
                   '电子厂', '塑胶厂', '模具厂', '机械厂', '金属制品厂', '木业厂',
                   '建材厂', '陶瓷厂', '厂']:
        if core.endswith(suffix):
            core = core[:-len(suffix)]
            break
    # 去掉地域前缀（如"广东省""深圳市""浙江"等），只留字号
    # 小白讲解：去掉 ^ （起始锚定），改为全局匹配所有地域层级。
    # 公司名可能有多层地域如"广东省深圳市龙岗区"，需要全部剥离，
    # 否则字号比较时残留的"龙岗区"会干扰匹配。
    core = re.sub(r'[\u4e00-\u9fa5]{2,4}(?:省|市|区|县)', '', core)
    return core.strip().lower()


def _dedup_cross_platform(companies):
    """
    跨平台去重：1688 和 MCP 可能搜到同一家公司，按核心字号去重

    小白讲解：两个平台搜到的结果合并后，可能有重复的公司（同一家工厂在两个平台都注册了）。
    用核心字号做 key，遇到重复的保留 source_text 更长（信息更丰富）的那条，
    这样 DeepSeek 过滤时能拿到更完整的信息，也避免天眼查重复查询。

    参数：companies 公司列表（extract_company_names 的返回值）
    返回：去重后的公司列表
    """
    seen_cores = {}   # 核心字号 -> 公司数据
    no_core_list = []  # 核心名太短无法可靠去重的，直接保留
    for c in companies:
        core = _extract_core_name(c.get("name", ""))
        if len(core) < 2:
            # 核心名太短（如只有1个字）无法可靠去重，直接保留不参与比对
            no_core_list.append(c)
            continue
        if core in seen_cores:
            # 已存在：比较信息丰富度，保留 source_text 更长的
            existing = seen_cores[core]
            if len(c.get("source_text", "")) > len(existing.get("source_text", "")):
                seen_cores[core] = c
        else:
            seen_cores[core] = c
    deduped = list(seen_cores.values()) + no_core_list
    if len(deduped) < len(companies):
        print(f"跨平台去重：{len(companies)}家 → {len(deduped)}家（去掉{len(companies) - len(deduped)}家重复）")
    return deduped


def _programmatic_prefilter(companies, product_name):
    """
    程序化预筛：用正则和平台字段快速剔除明显不合格的供应商，不调 AI（零 token 成本）

    小白讲解：在交给天眼查和 DeepSeek 之前，先用简单规则把明显是贸易商、配件厂的去掉，
    这样能减少天眼查查询次数和 DeepSeek API 调用，省钱省时间。
    重要原则：宁可保留不确定的，只剔除非常明显的（有制造能力证据的一律保留）。

    参数：
        companies: 公司列表（extract_company_names + 去重后的）
        product_name: 采购产品名称
    返回：预筛后的公司列表
    """
    # 明确的贸易类公司名关键词
    trade_keywords = ["贸易", "商贸", "商行", "电子商务", "经营部", "零售", "供应链", "进出口"]
    # 明确的配件/材料类关键词
    accessory_keywords = ["配件", "紧固件", "零件", "部件", "模具", "夹具", "治具", "原材料"]

    kept = []
    removed = 0
    for c in companies:
        name = c.get("name", "")
        source_text = c.get("source_text", "")
        business_type = c.get("business_type", "")

        # 规则1：MCP 的 business_type 为 "Manufacturer" 的直接保留（平台已标注是制造商，最可靠）
        if business_type == "Manufacturer":
            kept.append(c)
            continue

        # 规则2：公司名含贸易类关键词，且来源信息没有制造能力证据的，剔除
        # （如果来源信息里有 OEM/ODM/工厂/制造等字样，说明虽叫贸易但有制造能力，保留）
        is_trade_name = any(kw in name for kw in trade_keywords)
        has_manufacturing_evidence = any(kw in source_text for kw in ["OEM", "ODM", "工厂", "制造", "生产", "Manufacturer"])
        if is_trade_name and not has_manufacturing_evidence:
            removed += 1
            continue

        # 规则3：来源信息很短且全是配件/材料词（没有制造能力证据），剔除
        is_accessory_only = (any(kw in source_text for kw in accessory_keywords)
                             and not has_manufacturing_evidence)
        if is_accessory_only and len(source_text) < 50:
            removed += 1
            continue

        # 其他情况一律保留（不确定的交给 DeepSeek 判断，宁可多留不能漏放）
        kept.append(c)

    if removed > 0:
        print(f"程序化预筛：{len(companies)}家 → {len(kept)}家（剔除{removed}家明显贸易/配件商）")
    return kept


def _filter_one_batch(batch, product_name):
    """
    处理一个批次的AI过滤（供并发调用）

    小白讲解：把一批公司名发给DeepSeek，让它一次性判断：
    - 是不是制造商（剔除贸易/零售）
    - 是不是卖完整产品（剔除配件/材料/加工）
    - 不确定的保留（宁可保留不漏放）

    合并了原来的"普通过滤+强化过滤"两步为一步，减少一半API调用。

    参数：
        batch: 一批公司数据列表（最多50家）
        product_name: 采购产品名称

    返回：DeepSeek判断合格的公司列表
    """
    companies_text = ""
    for i, c in enumerate(batch, 1):
        business_type = c.get("business_type", "")
        # 小白讲解：天眼查补全的工商信息也一起给 DeepSeek，让它能看到注册资本/经营状态等做更准判断
        capital = c.get("registered_capital", "")
        status = c.get("operating_status", "")
        establish_years = c.get("establish_years", "")
        address = c.get("factory_address", "")
        biz_info = ""
        if capital:
            biz_info += f"注册资本：{capital}；"
        if status:
            biz_info += f"经营状态：{status}；"
        if establish_years:
            biz_info += f"成立年限：{establish_years}年；"
        if address:
            biz_info += f"注册地址：{address}；"
        companies_text += f"{i}. 公司名称：{c['name']}\n   命中关键词：{c['hit_keyword']}\n   业务类型：{business_type or '未知'}\n   来源信息：{c['source_text']}\n   工商信息：{biz_info or '未获取'}\n\n"

    # 小白讲解：prompt 结构按"固定内容在前、变化内容在后"组织，可提高 DeepSeek 上下文缓存命中率。
    # DeepSeek 缓存规则：后续请求只有完整匹配之前请求的前缀才计入"缓存命中"（命中0.025元 vs 未命中3元/百万tokens）。
    # 所以把固定的判断规则放前面，每批都变化的公司列表放最后，规则部分就能跨批次命中缓存。
    prompt = f"""你是采购供应商寻源专家。请判断候选公司是否合格，只返回合格的公司，不要推荐任何新公司。

【判断规则】
1. 业务类型判断（MCP的business_type字段可直接参考）：
   - business_type为"Manufacturer"的，直接判定为制造商，保留
   - business_type为"Trading Company"的，剔除（除非来源信息明确显示有制造能力）
   - business_type未知的，按下面规则判断
2. 供应商类型只保留"制造商"或"疑似制造商"，剔除：
   - 贸易公司、商贸公司、商行、电子商务公司
   - 经营部、零售、供应链公司
   （除非来源信息明确显示有制造能力）
3. 严格剔除以下类型（这是硬性规则，不可放宽）：
   - 配件：柜脚、五金配件、插座、灯带单卖、紧固件、零件、部件
   - 材料：岩板、玻璃板、亚克力板、门板、板材、原料、毛坯
   - 加工服务：来图加工、切割、代工、定制加工
   - 模具、夹具、治具
   - 装饰品、摆件
4. 主营产品与采购产品明显不相关的，剔除
   （如采购电视柜，但供应商主营是灯饰、服装、食品等）
5. 【重要】不确定是否做完整产品的，保留（标记为"疑似制造商"），不要剔除
6. 【1688数据特点】1688平台来源信息通常不含"主营产品"文字（只有搜索品类+合作方式+工厂能力），
   其中"搜索品类"是用户搜索的产品品类。请结合公司名+搜索品类+工厂能力谨慎推断 main_product，
   无法确定主营产品时填"待确认"，不要凭空编造。
7. 跨境电商经验：根据公司名、产品和来源信息判断，有则true，不确定则false
8. 不要编造任何新公司，只从下面列表中选择

【请返回JSON对象】（只返回JSON，格式如下）
{{
    "suppliers": [
        {{
            "name": "公司名称（原样返回）",
            "supplier_type": "制造商"或"疑似制造商",
            "main_product": "主营产品（一句话）",
            "intro": "供应商简介（一句话，含主营产品）",
            "has_cross_border_exp": true或false
        }}
    ]
}}
如果这批公司都不合格，suppliers返回空数组。

【当前采购产品】
{product_name}

【候选公司列表】
{companies_text}"""

    messages = [
        {"role": "system", "content": "你是供应商寻源专家，擅长判断供应商资质。你只能从用户提供的列表中选择，绝不推荐新公司。不确定是否合格的供应商应保留。"},
        {"role": "user", "content": prompt},
    ]

    # 小白讲解：网络抖动或DeepSeek服务异常时，原来直接返回空数组导致50家公司丢失
    # 现在改为：失败后重试1次，仍失败则返回原始公司数据并标记"过滤失败-保留"，宁可多留不能漏放
    for attempt in range(2):  # 最多尝试2次（1次正常+1次重试）
        try:
            # 启用JSON Output模式，确保返回合法JSON
            # 小白讲解：supplier_filter_v2场景配置在数据库中，管理员可在管理中心调整思考强度等参数
            result_text = call_deepseek(messages, scene_code="supplier_filter_v2", temperature=0.2, json_mode=True)
            result = json.loads(result_text)
            batch_filtered = result.get("suppliers", [])
            if isinstance(batch_filtered, dict):
                batch_filtered = [batch_filtered]

            # 把来源信息合并回去
            name_to_company = {c["name"]: c for c in batch}
            for s in batch_filtered:
                orig = name_to_company.get(s.get("name", ""))
                if orig:
                    s["hit_keyword"] = orig["hit_keyword"]
                    s["source"] = orig.get("source_platform") or "B2B平台"
                    # 小白讲解：把天眼查补全的工商字段传到过滤后的供应商数据上
                    s["registered_capital"] = orig.get("registered_capital", "")
                    s["operating_status"] = orig.get("operating_status", "")
                    s["legal_person"] = orig.get("legal_person", "")
                    s["establish_years"] = orig.get("establish_years", "")
                    s["establish_date"] = orig.get("establish_date", "")
                    s["factory_address"] = orig.get("factory_address", "") or s.get("factory_address", "")
                    s["phone"] = orig.get("phone", "") or s.get("phone", "")
                    s["email"] = orig.get("email", "") or s.get("email", "")
                    s["contact_status"] = orig.get("contact_status", "未获取")
                    # 天眼查工商简介存到临时字段，后续追加到 DeepSeek 生成的 intro 后面（问题3的追加方案）
                    if orig.get("_tyc_business_intro"):
                        s["_tyc_business_intro"] = orig["_tyc_business_intro"]
                    # 产品字段透传（MCP搜产品方式才有，1688无此字段为空）
                    s["product_title"] = orig.get("product_title", "")
                    s["product_link"] = orig.get("product_link", "")
                    s["price"] = orig.get("price", "")
                    s["moq"] = orig.get("moq", "")
                    # 海关出口数据字段透传（海关数据来源才有，其他平台为0）
                    s["customs_export_count"] = orig.get("customs_export_count", 0)
                    s["customs_total_qty"] = orig.get("customs_total_qty", 0)
                    s["customs_total_amount"] = orig.get("customs_total_amount", 0)
                else:
                    s["hit_keyword"] = ""
                    s["source"] = "B2B平台"
                    s.setdefault("registered_capital", "")
                    s.setdefault("operating_status", "")
                    s.setdefault("legal_person", "")
                    s.setdefault("establish_years", "")
                    s.setdefault("establish_date", "")
                    s.setdefault("factory_address", "")
                    s.setdefault("phone", "")
                    s.setdefault("email", "")
                    s.setdefault("contact_status", "未获取")
                    s.setdefault("product_title", "")
                    s.setdefault("product_link", "")
                    s.setdefault("price", "")
                    s.setdefault("moq", "")
                s["has_cross_border_exp"] = 1 if s.get("has_cross_border_exp") else 0
            return batch_filtered
        except Exception as e:
            if attempt == 0:
                print(f"批次过滤首次失败，1秒后重试: {e}")
                time.sleep(1)
                continue
            # 重试仍失败：返回原始公司数据并标记，不丢弃数据
            print(f"批次过滤重试仍失败，保留原始{len(batch)}家公司并标记'过滤失败-保留': {e}")
            fallback = []
            for c in batch:
                fallback.append({
                    "name": c["name"],
                    "supplier_type": "疑似制造商",  # 失败时保守保留为疑似制造商
                    "main_product": "",
                    "intro": f"过滤失败-保留：{_smart_truncate(c.get('source_text', ''), 400)}",
                    "has_cross_border_exp": 0,
                    "hit_keyword": c.get("hit_keyword", ""),
                    "source": c.get("source_platform") or "B2B平台",
                    "factory_address": "",
                    "email": "",
                    "phone": "",
                    "filter_failed": True,  # 标记此供应商是过滤失败兜底保留的
                })
            return fallback


def filter_suppliers_with_ai(companies, product_name, keywords_text, progress_callback=None):
    """
    用DeepSeek对已预筛+已补全工商信息的公司列表做精细过滤

    小白讲解：这个函数现在只负责"DeepSeek过滤"这一步（问题1流程重构后的第4步）。
    公司名的提取、跨平台去重、程序化预筛、天眼查工商补全都已经在前面做完了。
    这里把带完整工商信息（注册资本/经营状态/成立年限/注册地址）的公司分批发给 DeepSeek，
    让它基于完整数据做更准确的判断，生成 intro/main_product/supplier_type 等字段。

    性能优化：
    1. 批次50家，减少API调用次数
    2. 并发处理多个批次（用线程池），DeepSeek v4-pro并发限制500，完全够用
    3. 启用JSON Output模式，避免JSON解析失败
    4. "不确定是否做完整产品"的保留（标记疑似制造商），不剔除

    参数：
        companies: 已预筛+已补全工商信息的公司列表
        product_name: 产品名称
        keywords_text: 关键词文本
        progress_callback: 进度回调函数

    返回：过滤后的供应商列表
    """
    if not companies:
        return []

    if progress_callback:
        progress_callback(15, 16, f"正在AI过滤{len(companies)}家公司...")

    # 分批并发调用DeepSeek过滤（每批50家，并发处理）
    BATCH_SIZE = 50
    batches = []
    for i in range(0, len(companies), BATCH_SIZE):
        batches.append(companies[i:i + BATCH_SIZE])

    total_batches = len(batches)
    all_filtered = []

    # 并发处理所有批次（最多8个线程同时跑）
    # v4-pro官方并发上限500，8线程完全够用，比4线程快一倍
    # 注：max强度思考较慢，多并发能有效缩短总等待时间
    with ThreadPoolExecutor(max_workers=min(8, total_batches)) as executor:
        # 提交所有批次任务
        future_to_batch = {
            executor.submit(_filter_one_batch, batch, product_name): idx
            for idx, batch in enumerate(batches)
        }
        # 按完成顺序收集结果
        completed = 0
        for future in as_completed(future_to_batch):
            completed += 1
            batch_idx = future_to_batch[future]
            if progress_callback and total_batches > 1:
                progress_callback(15, 16, f"AI过滤中... 已完成{completed}/{total_batches}批")
            try:
                result = future.result()
                all_filtered.extend(result)
            except Exception as e:
                print(f"批次{batch_idx+1}异常: {e}")

    # 第三步：按供应商名称去重，校验公司名是否在原始提取列表中
    original_names = {c["name"] for c in companies}
    seen_names = set()
    unique_suppliers = []
    for s in all_filtered:
        name = s.get("name", "").strip()
        if not name or name in seen_names:
            continue
        # 校验公司名必须来自原始提取列表（防止AI编造或修改公司名）
        is_valid = False
        if name in original_names:
            is_valid = True
        else:
            for orig_name in original_names:
                if name in orig_name or orig_name in name:
                    s["name"] = orig_name
                    is_valid = True
                    break
        if is_valid:
            seen_names.add(s["name"])
            unique_suppliers.append(s)

    # 把天眼查工商简介追加到 DeepSeek 生成的 intro 后面（问题3的追加方案）
    # 小白讲解：DeepSeek 生成的是采购视角的 intro，天眼查的工商简介作为补充信息追加在后面，两份都保留
    for s in unique_suppliers:
        tyc_intro = s.pop("_tyc_business_intro", "")
        if tyc_intro:
            existing_intro = s.get("intro", "").strip()
            if existing_intro:
                s["intro"] = f"{existing_intro}；工商经营范围：{tyc_intro}"
            else:
                s["intro"] = tyc_intro
        # 确保简介包含注册资本（从天眼查补全的工商数据里取）
        capital = s.get("registered_capital", "")
        intro = s.get("intro", "")
        if capital and intro and "注册资本" not in intro:
            s["intro"] = f"{intro}注册资本：{capital}。"
        elif capital and not intro:
            s["intro"] = f"注册资本：{capital}。"
        elif not capital and not intro:
            s["intro"] = "天眼查未返回/未披露注册资本。"

    if progress_callback:
        progress_callback(15, 16, f"AI过滤完成：{len(companies)}家 → 保留{len(unique_suppliers)}家")

    return unique_suppliers




# ==================== 主搜索函数 ====================
def search_suppliers(keywords_json, product_name, progress_callback=None, hs_code="", cancel_checker=None):
    """
    供应商搜索主函数 - 完整的P0-P3关键词矩阵搜索流程

    流程：
    1. 解析P0-P3关键词（7组，每组中英文）
    2. 对每个关键词用智谱web_search搜索（中文优先）
    3. 合并所有搜索结果，用DeepSeek提取+过滤供应商
    4. 对每个供应商用天眼查MCP补全工商信息
    5. 返回供应商列表

    参数：
        keywords_json: P0-P3关键词JSON字符串
        product_name: 产品名称
        progress_callback: 进度回调函数，接收(当前步骤, 总步骤, 描述)参数
        hs_code: 产品的HS编码（如9403），用于海关数据搜索精确过滤，传空字符串表示不限制HS编码
        cancel_checker: 取消检查函数，返回True时停止后续搜索阶段

    返回：供应商列表
    """
    # 第一步：解析关键词
    if isinstance(keywords_json, str):
        keywords = json.loads(keywords_json)
    else:
        keywords = keywords_json

    # 构建搜索关键词列表（P0-P3，中文优先）
    # 每个元素是 (关键词级别, 搜索词, 命中关键词标签, 变体列表)
    # 中文词带变体（用于1688凑够50家），英文词暂不带变体
    search_terms = []
    for key in ["P0", "P1_1", "P1_2", "P2_1", "P2_2", "P3_1", "P3_2"]:
        if key in keywords and keywords[key]:
            entry = keywords[key]
            cn = entry.get("cn", "")
            en = entry.get("en", "")
            variants = entry.get("variants", [])
            if cn:
                search_terms.append((key, cn, cn, variants))
            if en:
                search_terms.append((key + "_en", en, en, []))

    total_steps = 4  # 搜索 + 预筛 + 天眼查补全 + DeepSeek过滤
    current_step = 0

    # 小白讲解：记录搜索开始时间，用于在进度描述里显示"已用时XX秒"，让用户知道没卡死
    search_start_time = time.time()

    def _cancelled():
        """判断用户是否已请求取消本次搜索。"""
        return bool(cancel_checker and cancel_checker())

    def _elapsed():
        """计算已用时秒数，返回中文描述字符串"""
        secs = int(time.time() - search_start_time)
        if secs < 60:
            return f"已用时{secs}秒"
        return f"已用时{secs // 60}分{secs % 60}秒"

    total_terms = len(search_terms)

    # 小白讲解：从数据库读取启用的搜索平台（管理员可在管理中心启停平台）
    # 只搜索启用的平台，关闭的平台不参与搜索（修复管理中心关闭MIC后仍搜索MIC的bug）
    enabled_platforms = get_search_platforms()
    enabled_codes = {p["provider_code"] for p in enabled_platforms}

    # 第二步：并发搜索所有关键词（启用的平台同时跑，大幅减少总耗时）
    # 性能优化：原来14个关键词串行约14分钟，并发后约2-3分钟
    # 小白讲解：进度描述里不再显示具体平台名称，只告诉用户在搜几个关键词
    if progress_callback:
        if not enabled_codes:
            progress_callback(1, total_steps, f"未启用任何搜索平台，请在管理中心开启至少一个平台")
        else:
            progress_callback(1, total_steps, f"正在并发搜索{total_terms}个关键词，{_elapsed()}...")

    all_results = []
    seen_names = set()  # 按公司名去重
    results_lock = threading.Lock()  # 线程安全锁
    completed_terms = [0]  # 用列表包装以便内部函数能修改（线程安全用锁保护）

    def _search_one_keyword(key, term, hit_kw, variants, term_idx, enabled_codes):
        """单个关键词的搜索任务（按启用平台并行搜索）

        小白讲解：一个关键词按管理中心启用的平台决定搜索哪些平台。
        1688那边会把原始词+变体词都搜一遍凑够50家。
        中国制造网只搜主词+前3个变体。
        海关数据搜全部英文关键词，中文跳过（产品描述全是英文）。
        如果某平台在管理中心被关闭，则跳过不搜索。
        """
        if _cancelled():
            return

        # 报告当前关键词开始搜索（细粒度进度，让用户看到在搜哪个词）
        if progress_callback:
            progress_callback(1, total_steps, f"[{term_idx+1}/{total_terms}] 正在搜索关键词：{term}，{_elapsed()}...")

        local_results = []
        results_1688 = []
        results_mic = []
        results_customs = []

        # 按启用状态决定调用哪些爬虫（关闭的平台直接跳过，不浪费时间和API调用）
        use_1688 = "ali1688" in enabled_codes
        use_mic = "madeinchina" in enabled_codes
        use_customs = "topease_customs" in enabled_codes

        # 海关数据：所有英文关键词都搜（中文跳过），有数据就取，没数据跳过翻页
        should_search_customs = use_customs and re.search(r'[a-zA-Z]', term)

        # 根据启用的平台组合选择并行策略
        if use_1688 and use_mic and should_search_customs:
            # 三平台并行
            with ThreadPoolExecutor(max_workers=3) as pool:
                future_1688 = pool.submit(crawl_1688, term, hit_kw, variants)
                future_mic = pool.submit(crawl_made_in_china, term, hit_kw, variants)
                future_customs = pool.submit(crawl_topease_customs, term, hit_kw, variants, hs_code)
                try:
                    results_1688 = future_1688.result(timeout=300)
                except Exception as e:
                    print(f"1688搜索'{term}'异常: {e}")
                    results_1688 = []
                try:
                    results_mic = future_mic.result(timeout=180)
                except Exception as e:
                    print(f"Made-in-China搜索'{term}'异常: {e}")
                    results_mic = []
                try:
                    results_customs = future_customs.result(timeout=300)
                except Exception as e:
                    print(f"海关数据搜索'{term}'异常: {e}")
                    results_customs = []
        elif use_1688 and use_mic:
            # 1688 + MIC 并行
            with ThreadPoolExecutor(max_workers=2) as pool:
                future_1688 = pool.submit(crawl_1688, term, hit_kw, variants)
                future_mic = pool.submit(crawl_made_in_china, term, hit_kw, variants)
                try:
                    results_1688 = future_1688.result(timeout=300)
                except Exception as e:
                    print(f"1688搜索'{term}'异常: {e}")
                    results_1688 = []
                try:
                    results_mic = future_mic.result(timeout=180)
                except Exception as e:
                    print(f"Made-in-China搜索'{term}'异常: {e}")
                    results_mic = []
        elif use_1688 and should_search_customs:
            # 1688 + 海关并行
            with ThreadPoolExecutor(max_workers=2) as pool:
                future_1688 = pool.submit(crawl_1688, term, hit_kw, variants)
                future_customs = pool.submit(crawl_topease_customs, term, hit_kw, variants, hs_code)
                try:
                    results_1688 = future_1688.result(timeout=300)
                except Exception as e:
                    print(f"1688搜索'{term}'异常: {e}")
                    results_1688 = []
                try:
                    results_customs = future_customs.result(timeout=300)
                except Exception as e:
                    print(f"海关数据搜索'{term}'异常: {e}")
                    results_customs = []
        elif use_mic and should_search_customs:
            # MIC + 海关并行
            with ThreadPoolExecutor(max_workers=2) as pool:
                future_mic = pool.submit(crawl_made_in_china, term, hit_kw, variants)
                future_customs = pool.submit(crawl_topease_customs, term, hit_kw, variants, hs_code)
                try:
                    results_mic = future_mic.result(timeout=180)
                except Exception as e:
                    print(f"Made-in-China搜索'{term}'异常: {e}")
                    results_mic = []
                try:
                    results_customs = future_customs.result(timeout=300)
                except Exception as e:
                    print(f"海关数据搜索'{term}'异常: {e}")
                    results_customs = []
        elif use_1688:
            # 只启用1688：单独搜索1688
            try:
                results_1688 = crawl_1688(term, hit_kw, variants)
            except Exception as e:
                print(f"1688搜索'{term}'异常: {e}")
                results_1688 = []
        elif use_mic:
            # 只启用MIC：单独搜索中国制造网
            try:
                results_mic = crawl_made_in_china(term, hit_kw, variants)
            except Exception as e:
                print(f"Made-in-China搜索'{term}'异常: {e}")
                results_mic = []
        elif should_search_customs:
            # 只启用海关数据
            try:
                results_customs = crawl_topease_customs(term, hit_kw, variants, hs_code)
            except Exception as e:
                print(f"海关数据搜索'{term}'异常: {e}")
                results_customs = []

        if _cancelled():
            return

        for r in results_1688:
            r["source_platform"] = "1688"
            local_results.append(r)
        for r in results_mic:
            r["source_platform"] = "Made-in-China"
            local_results.append(r)
        for r in results_customs:
            r["source_platform"] = "海关数据"
            # 海关数据保留英文原名（天眼查补全时会用英文名匹配）
            if not r.get("name_original_en"):
                r["name_original_en"] = r.get("name", "")
            local_results.append(r)

        # 线程安全地合并结果
        # 小白讲解：记录合并前的数量，用来算"本次新增"多少家（去重后实际加进去的）
        with results_lock:
            before_count = len(all_results)
            for r in local_results:
                name = r.get("name", "")
                if name and name not in seen_names:
                    seen_names.add(name)
                    all_results.append(r)
            new_added = len(all_results) - before_count  # 本次去重后实际新增的家数
            completed_terms[0] += 1
            # 小白讲解：进度描述不再分平台显示"1688=几家，MIC=几家"，
            # 只汇总"本次新增X家，累计Y家"，让业务用户看到总量即可
            if progress_callback:
                progress_callback(1, total_steps,
                    f"[{term_idx+1}/{total_terms}] 关键词'{term}'完成：本次新增{new_added}家，累计{len(all_results)}家，{_elapsed()}")

        print(f"关键词'{term}'搜索完成：本次新增{new_added}家，累计{len(all_results)}家")

    # 并发搜索所有关键词（最多3个关键词同时搜索，避免触发平台限流）
    with ThreadPoolExecutor(max_workers=min(3, len(search_terms))) as executor:
        futures = {
            executor.submit(_search_one_keyword, key, term, hit_kw, variants, idx, enabled_codes): idx
            for idx, (key, term, hit_kw, variants) in enumerate(search_terms)
        }
        for future in as_completed(futures):
            if _cancelled():
                for pending_future in futures:
                    pending_future.cancel()
                break
            try:
                future.result(timeout=180)
            except Exception as e:
                print(f"关键词搜索异常: {e}")

    if _cancelled():
        return []

    print(f"所有关键词搜索完成：共获取{len(all_results)}家供应商（去重前）")

    # 第二步：提取公司名 + 跨平台去重 + 程序化预筛（不调AI，零token成本）
    current_step += 1
    if progress_callback:
        progress_callback(current_step, total_steps, f"正在提取和预筛供应商（共{len(all_results)}家），{_elapsed()}...")

    companies = extract_company_names(all_results)
    if not companies:
        print("未提取到任何公司名称")
        return []
    companies = _dedup_cross_platform(companies)
    companies = _programmatic_prefilter(companies, product_name)
    print(f"预筛完成：{len(all_results)}家搜索结果 → {len(companies)}家候选公司")

    if _cancelled():
        return []

    # 第三步：用天眼查MCP并发补全工商信息（对预筛后的 companies，在 DeepSeek 过滤之前补全）
    # 小白讲解：先补全工商信息再过滤，DeepSeek 就能看到注册资本/经营状态等做更准判断（问题1流程重构）
    current_step += 1
    if progress_callback:
        progress_callback(current_step, total_steps, f"正在用天眼查补全工商信息（共{len(companies)}家），{_elapsed()}...")

    def _enrich_one_supplier(supplier, tyc_client):
        """单个供应商的天眼查补全任务

        不分来源平台，统一处理：
        - 直接用公司名搜索天眼查（MIC英文名也直接搜）
        - 找到 → 补全工商信息。MIC来源额外用天眼查中文名替换英文名
        - 找不到 → 保留该公司，标记"工商数据未匹配"（不丢弃）
        """
        if _cancelled():
            return
        name = supplier.get("name", "").strip()
        source_platform = supplier.get("source_platform", "")
        if not name:
            return

        try:
            # 用天眼查搜索公司（search_companies内部已带3次重试）
            # 小白讲解：search_companies现在会自动重试3次（429/超时），
            # 返回None表示请求彻底失败，返回空列表[]表示确实没找到。
            companies = tyc_client.search_companies(name)
            if companies is None:
                # 请求失败（网络超时/频率限制），等2秒再试1次
                print(f"天眼查请求失败({name})，2秒后重试...")
                time.sleep(2)
                companies = tyc_client.search_companies(name)
            if companies is None:
                # 两次都请求失败，跳过该企业（标记为未匹配，但不当作"确认不存在"）
                print(f"天眼查两次请求均失败({name})，跳过该企业")
                supplier["_tyc_not_found"] = True
                return
            matched_company = None
            match_type = ""  # 记录天眼查匹配类型，供初筛阶段读库复用，避免重复调MCP
            # 1. 优先精确同名匹配
            for company in companies:
                if company.get("name", "").strip() == name:
                    matched_company = company
                    match_type = "exact_match"
                    break
            # 2. 精确同名失败：如果是英文公司名，且天眼查返回了"英文名匹配"标识，
            #    说明天眼查已经用英文名匹配到了对应的中文名公司，直接采用，不再做相似度比较。
            #    小白讲解：MIC（Made-in-China）来的供应商都是英文名，天眼查自己会做英文→中文的匹配，
            #    并在"匹配类型"字段里标注"英文名匹配"。这种情况下中文名和英文名字符串完全不同，
            #    用相似度比较必然失败，所以必须直接采用天眼查的匹配结果。
            if not matched_company:
                for company in companies:
                    if company.get("match_type", "") == "英文名匹配":
                        matched_company = company
                        match_type = "english_name_match"
                        print(f"天眼查英文名匹配采用：'{name}' → '{company.get('name', '')}'")
                        break
            # 3. 上面都没匹配上：做公司名相似度校验，相似度>=0.6 才采用
            # 小白讲解：SequenceMatcher只看字符重叠，不认得公司"字号"才是唯一标识。
            # "深圳市鼎盛科技有限公司"和"深圳市鼎盛电子科技有限公司"相似度~0.64，
            # 但完全是两家公司！所以0.6匹配后必须加字号校验：
            # 把地域前缀和组织形式后缀都去掉，比较剩余字号是否实质性相同。
            if not matched_company and companies:
                best_ratio = 0
                best_company = None
                for company in companies:
                    cand_name = company.get("name", "").strip()
                    if not cand_name:
                        continue
                    ratio = difflib.SequenceMatcher(None, name, cand_name).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_company = company
                if best_company and best_ratio >= 0.6:
                    # 字号校验：剥离地域+组织形式后比较核心字号
                    cand_core = _extract_core_name(best_company.get("name", ""))
                    orig_core = _extract_core_name(name)
                    if cand_core and orig_core and (cand_core in orig_core or orig_core in cand_core):
                        matched_company = best_company
                        match_type = "partial_match"
                    else:
                        print(f"天眼查字号不匹配({name})，候选字号'{cand_core}'≠'{orig_core}'，相似度{best_ratio:.0%}，拒绝采用")
                if not matched_company:
                    print(f"天眼查未匹配({name})，最高相似度{best_ratio:.0%}")

            if matched_company:
                # MIC/海关数据来源：用天眼查返回的中文名替换英文名
                if source_platform in ("Made-in-China", "海关数据"):
                    tyc_cn_name = matched_company.get("name", "").strip()
                    if tyc_cn_name and tyc_cn_name != name:
                        print(f"天眼查匹配（{source_platform}英文→中文）：'{name}' → '{tyc_cn_name}'")
                        supplier["name"] = tyc_cn_name

                supplier["registered_capital"] = matched_company.get("registered_capital", "") or supplier.get("registered_capital", "")
                supplier["operating_status"] = matched_company.get("status", "")
                supplier["legal_person"] = matched_company.get("legal_person", "")
                # 小白讲解：保存天眼查匹配状态和企业ID，初筛阶段直接读库判断是否匹配成功，
                # 不用再调天眼查search_companies，省掉1次MCP请求
                supplier["tyc_match_status"] = match_type
                supplier["tyc_company_id"] = matched_company.get("credit_code", "")

                try:
                    detail = tyc_client.get_company_basic_profile(matched_company["name"])
                    if detail:
                        tyc_intro = detail.get("intro", "")
                        if tyc_intro:
                            # 小白讲解：工商简介先存到临时字段，等 DeepSeek 生成 intro 后再追加（问题3追加方案）
                            # 因为天眼查补全在前、DeepSeek过滤在后，此时 intro 还没生成
                            supplier["_tyc_business_intro"] = tyc_intro
                        tyc_phone = detail.get("phone", "")
                        if tyc_phone:
                            supplier["phone"] = tyc_phone
                        tyc_email = detail.get("email", "")
                        if tyc_email:
                            supplier["email"] = tyc_email
                        tyc_address = detail.get("address", "")
                        if tyc_address and not supplier.get("factory_address"):
                            supplier["factory_address"] = tyc_address
                        # 小白讲解：保存经营范围到供应商表，初筛规则判断制造商/出口经验时要用，
                        # 这样初筛阶段不用再调天眼查get_company_basic_profile取经营范围
                        tyc_scope = detail.get("business_scope", "")
                        if tyc_scope:
                            supplier["business_scope"] = tyc_scope
                        tyc_capital = detail.get("registered_capital", "")
                        if tyc_capital:
                            # 校验注册资本不是日期格式（防止脏数据）
                            if not re.match(r"^\d{4}[-/年]\d{1,2}[-/月]?\d{0,2}日?$", tyc_capital):
                                supplier["registered_capital"] = tyc_capital
                        tyc_establish_date = detail.get("establish_date", "")
                        if tyc_establish_date:
                            # 保存原始成立日期（如"2015-03-12"），供详情页显示
                            supplier["establish_date"] = tyc_establish_date
                            try:
                                year_match = re.search(r'(\d{4})', tyc_establish_date)
                                if year_match:
                                    establish_year = int(year_match.group(1))
                                    current_year = datetime.now().year
                                    years = current_year - establish_year
                                    if years >= 0:
                                        supplier["establish_years"] = str(years)
                            except (ValueError, AttributeError):
                                pass
                except Exception as e:
                    print(f"天眼查详情查询失败({name}): {e}")
            else:
                # 天眼查未匹配：标记剔除（天眼查是初筛唯一数据源，找不到的没必要保留）
                supplier["_tyc_not_found"] = True

            # 计算联系方式状态
            has_phone = bool(supplier.get("phone", "").strip())
            has_email = bool(supplier.get("email", "").strip())
            if has_phone and has_email:
                supplier["contact_status"] = "已获取电话和邮箱"
            elif has_phone:
                supplier["contact_status"] = "已获取电话"
            elif has_email:
                supplier["contact_status"] = "已获取邮箱"
            else:
                supplier["contact_status"] = "未获取"
        except Exception as e:
            print(f"天眼查补全失败({name}): {e}")

    try:
        # 每个线程用独立的天眼查client（避免线程安全问题）
        def _enrich_task(supplier):
            tyc_client = TianyanchaClient()
            tyc_client.initialize()
            _enrich_one_supplier(supplier, tyc_client)

        # 并发补全（最多5个线程，避免天眼查限流）
        with ThreadPoolExecutor(max_workers=min(5, len(companies))) as executor:
            futures = {executor.submit(_enrich_task, s): s for s in companies}
            done_count = 0
            for future in as_completed(futures):
                if _cancelled():
                    for pending_future in futures:
                        pending_future.cancel()
                    break
                try:
                    future.result(timeout=30)
                    done_count += 1
                    if progress_callback and done_count % 3 == 0:
                        progress_callback(current_step, total_steps, f"天眼查补全中... {done_count}/{len(companies)}，{_elapsed()}")
                except Exception as e:
                    print(f"天眼查补全任务异常: {e}")
    except Exception as e:
        print(f"天眼查补全初始化失败: {e}")

    if _cancelled():
        return []

    # 天眼查补全后剔除未匹配的供应商（找不到工商数据的不保留）
    removed_count = 0
    companies = [c for c in companies if not c.pop("_tyc_not_found", False) or (removed_count := removed_count + 1) and False]
    if removed_count > 0 and progress_callback:
        progress_callback(current_step, total_steps,
                          f"天眼查未匹配{removed_count}家已剔除，剩余{len(companies)}家")

    # 第四步：用DeepSeek基于完整工商数据做精细过滤
    # 小白讲解：此时 companies 已带工商信息（注册资本/经营状态/成立年限等），
    # DeepSeek 能基于完整数据做更准确的过滤判断（问题1流程重构的核心）
    current_step += 1
    if progress_callback:
        progress_callback(current_step, total_steps, f"AI正在基于工商信息过滤供应商（共{len(companies)}家），{_elapsed()}...")

    keywords_text = " / ".join([item[1] for item in search_terms])
    # 小白讲解：filter_suppliers_with_ai 内部的进度回调用 15/16 这种奇怪的 step/total，
    # 这里包一层，强制用统一的 current_step/total_steps，并给描述加上已用时
    def _ai_progress_cb(step, total, desc):
        if progress_callback:
            if "已用时" not in desc:
                progress_callback(current_step, total_steps, f"{desc}，{_elapsed()}")
            else:
                progress_callback(current_step, total_steps, desc)
    suppliers = filter_suppliers_with_ai(companies, product_name, keywords_text, _ai_progress_cb if progress_callback else None)

    if _cancelled():
        return []

    # filter_suppliers_with_ai 内部已做去重，这里再保险去重一次
    seen_names = set()
    unique_suppliers = []
    for s in suppliers:
        name = s.get("name", "").strip()
        if name and name not in seen_names:
            seen_names.add(name)
            unique_suppliers.append(s)

    return unique_suppliers
