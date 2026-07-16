"""
AI 核心模块 - 封装大模型的所有 AI 功能

这个文件是系统的"AI大脑"，使用两个模型各取所长：
- 智谱 GLM-4V-Flash：图片识别（免费），把用户上传的图片转成文字描述
- DeepSeek：文本理解、联网搜索、供应商初筛

包含三个核心AI功能：
1. parse_requirement - 解析需求：从文本/文档/图片中提取结构化需求信息
2. search_suppliers  - 搜索供应商：用DuckDuckGo搜索+AI提取供应商信息
3. auto_screening    - 自动初筛：AI做风险排查和资质核实并评分

调用流程：用户输入需求(文本/文档/图片) → AI解析生成需求 → AI搜索供应商 → AI自动初筛
"""

import json
import base64
import requests
import re
import subprocess
import shutil
from bs4 import BeautifulSoup
from model_config import get_model_config, get_provider


# ==================== 网页内容抓取（供AI分析用） ====================

# 小白讲解：匹配文本里的网页链接（http或https开头），用于自动识别用户在需求描述里贴的URL
_URL_REGEX = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)

# 单个网页正文最多保留这么多字符，避免网页太长撑爆AI的上下文窗口
_MAX_CONTENT_PER_URL = 8000

# 所有网页正文合并后的总字数上限，超出则截断（保护AI上下文，避免token浪费）
_MAX_TOTAL_WEB_CONTENT = 20000

# 抓取网页时的请求超时时间（秒），超时就放弃这个网页，不卡住整个解析流程
_FETCH_TIMEOUT = 10

# 模拟正常浏览器的身份标识（有些网站会拒绝没有User-Agent的请求）
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def extract_urls(text):
    """
    从一段文本中提取所有网页链接（URL）

    小白讲解：用户在需求描述里可能随手贴了几个网页链接，
    这个函数用正则表达式把这些链接一个一个找出来，返回去重后的列表。

    参数：
        text: 用户输入的文本（可能包含0个或多个URL）

    返回：URL字符串列表（已去重，保持出现顺序）
    """
    if not text:
        return []
    # 找出所有匹配的URL
    matches = _URL_REGEX.findall(text)
    # 去重并保持顺序（用dict的key天然去重）
    seen = set()
    urls = []
    for url in matches:
        # 去掉末尾可能带的中英文标点和反引号（Markdown代码块标记，避免污染URL）
        clean_url = url.rstrip(".,);:!?。，；：）】》」』\"'`")
        if clean_url not in seen:
            seen.add(clean_url)
            urls.append(clean_url)
    return urls


def fetch_url_content(url):
    """
    抓取单个网页的正文内容（专门针对亚马逊产品页优化，用curl绕过反爬）

    小白讲解：访问用户给的亚马逊链接，把网页下载下来，重点提取产品采购寻源需要的信息：
    产品标题、品牌、材质、尺寸规格、产品特性（About this item）、技术参数等。

    关键技术点：亚马逊会通过TLS指纹识别requests库的请求并返回反爬页面（只有3900字符无产品信息）。
    而curl的TLS指纹和真实浏览器更接近，能稳定拿到完整产品页（100万+字符）。
    所以这里优先用curl抓取，curl不可用时回退到requests。

    参数：
        url: 要抓取的亚马逊产品页链接

    返回：网页正文文字（字符串）。抓取失败时返回带失败说明的短文本，不抛异常，避免中断主流程。
    """
    try:
        # 优先用curl抓取（能绕过亚马逊TLS指纹反爬）
        html = _fetch_with_curl(url)
        used_curl = True
        if not html:
            # curl不可用或抓取失败，回退到requests
            html = _fetch_with_requests(url)
            used_curl = False

        if not html:
            # 两种方式都失败，走URL线索兜底
            # 小白讲解：以前只返回一句"反爬无法抓取"，AI看到不知道怎么提取。
            # 现在把URL线索伪装成亚马逊产品页的结构化格式，AI就能按同样逻辑提取。
            return _build_url_hint_product_info(url)

        # 用BeautifulSoup解析HTML
        soup = BeautifulSoup(html, "html.parser", from_encoding="utf-8")

        # 删除所有非正文标签（脚本、样式、导航、页脚、头部、侧边栏等）
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript", "iframe", "form"]):
            tag.decompose()

        # 优先取网页标题（亚马逊标题含完整产品名+规格+颜色等关键信息）
        title = soup.title.get_text(strip=True) if soup.title else ""

        # 亚马逊专用信息提取：优先抓取产品采购寻源需要的关键区域
        product_info = _extract_amazon_product_info(soup)

        if product_info:
            # 成功提取到亚马逊专用信息，直接用结构化内容
            text = product_info
        else:
            # 通用提取：优先用<main>或<article>标签，没有就取<body>
            main = soup.find("main") or soup.find("article") or soup.body or soup
            text = main.get_text(separator="\n", strip=True)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"\n{3,}", "\n\n", text)

        # 反爬兜底：如果正文太短（少于200字），很可能是被反爬拦截了
        if len(text.strip()) < 200:
            return _build_url_hint_product_info(url)

        # 组装：标题 + 正文
        if title:
            text = f"【网页标题】{title}\n\n【正文内容】\n{text}"

        # 截断超长内容，保护AI上下文
        if len(text) > _MAX_CONTENT_PER_URL:
            text = text[:_MAX_CONTENT_PER_URL] + "\n...(网页内容过长已截断)"

        return text

    except Exception as e:
        url_hint = _extract_product_hint_from_url(url)
        return f"【抓取失败】{url} - 原因: {str(e)[:100]}\nURL产品线索: {url_hint}"


def _fetch_with_curl(url):
    """
    用curl命令抓取网页HTML（能绕过亚马逊TLS指纹反爬）

    小白讲解：requests库发请求时，它的"指纹"和真实浏览器不一样，亚马逊能识别出来并拦截。
    curl是系统自带的命令行工具，它的网络指纹更接近真实浏览器，亚马逊拦截不了。
    所以这里用subprocess调用curl来下载网页，比requests更稳定。

    参数：
        url: 要抓取的网页链接

    返回：网页HTML字符串。curl不可用或抓取失败返回空字符串。
    """
    # 检查系统是否有curl（Windows 10+自带curl.exe）
    curl_path = shutil.which("curl")
    if not curl_path:
        return ""

    try:
        # 构造curl命令
        # -s 静默模式 -L 跟随跳转 -A 设置UA --max-time 超时
        cmd = [
            curl_path, "-s", "-L",
            "-A", _USER_AGENT,
            "-H", "Accept-Language: en-US,en;q=0.9",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "--max-time", str(_FETCH_TIMEOUT + 10),  # curl给宽裕点时间
            "-b", "",  # 不发送cookie，避免本地cookie干扰
            url,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=_FETCH_TIMEOUT + 15)
        html = result.stdout.decode("utf-8", errors="ignore")
        # 亚马逊完整产品页至少应该有5万字符，太小说明被拦截了
        if len(html) < 10000:
            return ""
        return html
    except Exception:
        return ""


def _fetch_with_requests(url):
    """
    用requests库抓取网页HTML（curl不可用时的回退方案）

    小白讲解：这是备用方案。当系统没有curl时用requests抓取。
    注意：requests可能被亚马逊反爬拦截（拿不到完整产品页），但在某些情况下仍可用。

    参数：
        url: 要抓取的网页链接

    返回：网页HTML字符串。抓取失败返回空字符串。
    """
    try:
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        resp = requests.get(url, headers=headers, timeout=_FETCH_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        return resp.text
    except Exception:
        return ""


def _extract_amazon_product_info(soup):
    """
    从亚马逊产品页HTML中提取采购寻源需要的关键产品信息

    小白讲解：亚马逊产品页有标准的结构，产品标题、品牌、特性、规格表都在固定的HTML元素里。
    这个函数专门把这些有价值的元素一个个找出来，拼成结构化文本给AI，
    比通用提取更精准，能拿到：产品标题、品牌、About this item卖点、技术规格表等。

    提取的关键区域（按采购寻源价值排序）：
    1. 产品标题（#productTitle / #title）- 含完整产品名+规格+颜色
    2. 品牌信息（#bylineInfo / a#bylineInfo）- 品牌名
    3. 产品特性（#feature-bullets）- About this item 卖点，常含材质/尺寸/功能
    4. 产品详情表（#productDetails_techSpec_section / #detailBulletsWrapper）- 规格参数表
    5. 产品描述（#productDescription）- 详细描述

    参数：
        soup: BeautifulSoup解析后的HTML对象

    返回：结构化的产品信息文本。提取不到任何信息时返回空字符串（回退到通用提取）。
    """
    sections = []

    # 1. 产品标题（亚马逊最重要的字段，含完整产品名+尺寸+材质+颜色等）
    title_elem = soup.find(id="productTitle") or soup.find(id="title")
    if title_elem:
        product_title = title_elem.get_text(strip=True)
        if product_title:
            sections.append(f"【产品标题】{product_title}")

    # 2. 品牌信息（找供应商时品牌是有价值的参考）
    brand_elem = soup.find(id="bylineInfo") or soup.select_one("a#bylineInfo")
    if brand_elem:
        brand_text = brand_elem.get_text(strip=True)
        # 去掉"Visit the xxx Store"等前缀，只留品牌名
        brand_text = re.sub(r"(?i)visit\s+the\s+", "", brand_text)
        brand_text = re.sub(r"(?i)\s+store$", "", brand_text)
        brand_text = re.sub(r"(?i)^brand:\s*", "", brand_text)
        if brand_text:
            sections.append(f"【品牌】{brand_text}")

    # 3. 产品特性/卖点（About this item，常含材质、尺寸、功能等寻源关键信息）
    feature_elem = soup.find(id="feature-bullets")
    if feature_elem:
        # 提取每个卖点条目
        bullets = feature_elem.find_all("li", class_=re.compile(".*", re.I))
        features = []
        for b in bullets:
            txt = b.get_text(strip=True)
            # 过滤掉空的和"Show more"等无意义内容
            if txt and len(txt) > 3 and "show more" not in txt.lower() and "show less" not in txt.lower():
                features.append(f"- {txt}")
        if features:
            sections.append("【产品特性 About this item】\n" + "\n".join(features))

    # 4. 产品详情/技术规格表（含尺寸、重量、材质等精确参数）
    # 亚马逊有两种规格表格式：table格式和bullet格式
    tech_spec = soup.find(id="productDetails_techSpec_section_1") or soup.find(id="productDetails_techSpec_section_2")
    if tech_spec:
        rows = tech_spec.find_all("tr")
        specs = []
        for row in rows:
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2:
                key = cells[0].get_text(strip=True)
                val = cells[1].get_text(strip=True)
                if key and val:
                    specs.append(f"- {key}: {val}")
        if specs:
            sections.append("【技术规格 Product Details】\n" + "\n".join(specs))

    # bullet格式的规格表
    detail_bullets = soup.find(id="detailBulletsWrapper_feature_div") or soup.find(id="detailBullets_feature_div")
    if detail_bullets:
        bullets = detail_bullets.find_all("li")
        specs2 = []
        for b in bullets:
            txt = b.get_text(separator=" ", strip=True)
            # 清理多余空白
            txt = re.sub(r"\s+", " ", txt)
            if txt and len(txt) > 3:
                specs2.append(f"- {txt}")
        if specs2:
            sections.append("【产品参数 Product Information】\n" + "\n".join(specs2[:20]))  # 限制条数避免太长

    # 5. 产品详细描述
    desc_elem = soup.find(id="productDescription")
    if desc_elem:
        desc_text = desc_elem.get_text(separator="\n", strip=True)
        desc_text = re.sub(r"\n{2,}", "\n", desc_text)
        if desc_text and len(desc_text) > 10:
            # 描述可能很长，截断到2000字
            if len(desc_text) > 2000:
                desc_text = desc_text[:2000] + "..."
            sections.append(f"【产品描述】\n{desc_text}")

    # 组装所有提取到的区域
    if sections:
        return "\n\n".join(sections)
    return ""


def _extract_product_hint_from_url(url):
    """
    从URL路径中提取产品关键词线索

    小白讲解：当网站有反爬机制抓不到正文时，URL路径里往往藏有产品信息。
    比如亚马逊链接 .../Muwuele-Overbed-Adjustable-Hospital-Standing/dp/B0DWJ5FY8P/...
    我们把"Muwuele-Overbed-Adjustable-Hospital-Standing"这段提取出来，用横线拆成关键词，
    至少能让AI知道用户参考的产品大概是什么。

    参数：
        url: 网页链接

    返回：从URL路径提取的产品关键词字符串（用空格分隔）。提取不到则返回"无"。
    """
    try:
        # 去掉查询参数（?后面的部分），只看路径部分
        path = url.split("?")[0]
        # 用斜杠分割路径，找出最长的段落（通常是产品标题的slug）
        segments = path.split("/")
        # 过滤掉短段落和纯数字/字母代码（如dp、B0DWJ5FY8P这类ASIN码）
        candidates = []
        for seg in segments:
            # 段落要够长，且包含横线（产品标题slug用横线连接单词）
            if len(seg) > 8 and "-" in seg:
                # 把横线连接的标题拆成单词，过滤掉纯数字段
                words = [w for w in seg.split("-") if w and not w.isdigit() and len(w) > 1]
                if words:
                    candidates.append(" ".join(words))
        return candidates[0] if candidates else "无"
    except Exception:
        return "无"


def _build_url_hint_product_info(url):
    """
    当无法抓取亚马逊网页正文时，用URL路径中的关键词线索拼成结构化产品信息

    小白讲解：这是兜底方案。当亚马逊反爬拦截（curl不可用+requests被挡）时，
    网页正文拿不到，但从URL路径里能提取产品关键词（如"Overbed-Adjustable-Hospital"）。
    把这些关键词伪装成和_extract_amazon_product_info一样的格式块，
    AI就能按同样的提取逻辑处理，不会因为格式不匹配而忽略这些线索。

    参数：
        url: 亚马逊产品页链接
    返回：伪装成亚马逊提取格式的结构化文本
    """
    url_hint = _extract_product_hint_from_url(url)

    # 把URL线索关键词按空格拆成单个词，帮AI更容易识别产品属性
    # 例如 "Muwuele Overbed Adjustable Hospital Standing" →
    #      ["Muwuele", "Overbed", "Adjustable", "Hospital", "Standing"]
    hint_words = url_hint.split() if url_hint and url_hint != "无" else []

    # 构造和亚马逊提取格式一致的输出
    parts = []
    if hint_words:
        # 产品标题用URL线索拼成，加上[URL线索]标记让AI知道数据来源
        parts.append(f"【产品标题】{url_hint}（来源：URL路径提取，产品页正文无法直接获取）")
        # 把关键词列成产品特性，方便AI逐项分析
        parts.append("【产品特性 - 从URL路径关键词提取】")
        for word in hint_words:
            parts.append(f"- 关键词：{word}")
    else:
        parts.append(f"【产品标题】URL路径无法提取有效关键词，请参考完整链接")
    parts.append(f"【参考来源】{url}")

    return "\n".join(parts)


def _is_amazon_url(url):
    """
    判断一个URL是否为亚马逊链接

    小白讲解：系统只支持抓取亚马逊产品页，这个函数用来过滤非亚马逊链接。
    支持 amazon.com（美国站）、amazon.cn（中国站）、amazon.co.jp（日本站）等各区域站点。

    参数：
        url: 网页链接

    返回：是亚马逊链接返回True，否则返回False
    """
    url_lower = url.lower()
    # 亚马逊各区域站点的域名特征
    amazon_domains = ["amazon.com", "amazon.cn", "amazon.co.jp", "amazon.co.uk",
                      "amazon.de", "amazon.fr", "amazon.it", "amazon.es",
                      "amazon.ca", "amazon.com.au", "amazon.com.mx", "amazon.in"]
    for domain in amazon_domains:
        if domain in url_lower:
            return True
    return False


def fetch_urls_from_text(text):
    """
    从文本中找出所有URL并批量抓取网页内容，合并成一段给AI分析用的文本

    小白讲解：这是给parse_requirement调用的总入口。
    它先找出文本里所有网页链接，然后过滤出亚马逊链接（系统只支持亚马逊），
    一个一个去抓取产品页内容，最后把所有网页内容拼成一段文字返回。
    如果没有URL或没有亚马逊URL就返回空字符串（不影响原有解析流程）。

    参数：
        text: 用户输入的需求描述文本（可能包含URL）

    返回：拼接好的网页正文文本。无亚马逊URL时返回空字符串。
          所有网页内容合计超过_MAX_TOTAL_WEB_CONTENT字数则截断。
    """
    urls = extract_urls(text)
    if not urls:
        return ""

    # 过滤：只保留亚马逊链接（系统只支持抓取亚马逊产品页）
    amazon_urls = [u for u in urls if _is_amazon_url(u)]
    non_amazon_urls = [u for u in urls if not _is_amazon_url(u)]

    # 如果有非亚马逊链接，给AI一条提示，让AI告知用户只支持亚马逊
    hint = ""
    if non_amazon_urls and not amazon_urls:
        hint = "（提示：用户贴了非亚马逊链接，但系统仅支持亚马逊链接抓取，请在追问中提醒用户改贴亚马逊产品链接）"
        return hint

    chunks = []
    total_len = 0
    # 逐个抓取亚马逊产品页内容
    for idx, url in enumerate(amazon_urls, start=1):
        content = fetch_url_content(url)
        # 用分隔标记区分不同网页的内容，方便AI识别
        chunk = f"--- 亚马逊产品页{idx}：{url} ---\n{content}"
        chunks.append(chunk)
        total_len += len(chunk)
        # 达到总字数上限就停止抓取（避免抓太多撑爆AI上下文）
        if total_len >= _MAX_TOTAL_WEB_CONTENT:
            chunks.append(f"\n...(已达到网页内容总字数上限，后续网页不再抓取)")
            break

    # 如果还有非亚马逊链接，附上提示
    if non_amazon_urls:
        chunks.append(f"\n（提示：检测到 {len(non_amazon_urls)} 个非亚马逊链接已忽略，系统仅支持亚马逊链接）")

    return "\n\n".join(chunks)


def call_deepseek(messages, scene_code, temperature=None, json_mode=False, max_tokens=None, effort=None):
    """
    调用 DeepSeek API 的基础方法（配置从数据库读取，支持场景化参数）

    小白讲解：这是调用 DeepSeek 大模型的统一入口。管理员可在管理中心修改模型参数。
    传入 scene_code（场景代码），函数会自动从数据库读取该场景的模型名、思考强度、温度等配置。
    temperature/max_tokens/effort 参数可覆盖场景配置（传None时用场景配置的值）。

    参数：
        messages: 对话消息列表，格式如 [{"role": "user", "content": "你好"}]
        scene_code: 场景代码，如 "req_parse"/"keyword_gen"/"auto_screening" 等
        temperature: 温度值（传None用场景配置，思考模式下不生效）
        json_mode: 是否启用 JSON 输出模式（确保返回合法 JSON）
        max_tokens: 最大输出 token 数（传None用场景配置）
        effort: 思考强度（传None用场景配置）

    返回：AI回复的文本内容
    """
    # 从数据库读取场景配置（模型名、思考强度、温度、超时等）
    config = get_model_config(scene_code)
    if not config:
        raise Exception(f"场景配置不存在：{scene_code}，请在管理中心检查AI模型配置")
    if not config["is_enabled"]:
        raise Exception(f"场景{config['scene_name']}已被禁用，请在管理中心启用")

    # 从数据库读取服务商信息（API地址、密钥）
    provider = get_provider("deepseek")
    if not provider or not provider["api_key"]:
        raise Exception("DeepSeek API密钥未配置，请在管理中心配置")
    if not provider["is_enabled"]:
        raise Exception("DeepSeek服务已被禁用，请在管理中心启用")

    # 参数优先级：调用点传入 > 场景配置 > 默认值
    actual_temperature = temperature if temperature is not None else config["temperature"]
    actual_max_tokens = max_tokens if max_tokens is not None else config["max_tokens"]
    actual_effort = effort if effort is not None else config["thinking_effort"]

    # 构造请求体
    body = {
        "model": config["model_name"],
        "messages": messages,
        "temperature": actual_temperature,
        "stream": False,
        "max_tokens": actual_max_tokens,
    }

    # 启用思考模式 + 指定思考强度（仅DeepSeek类模型有效）
    if config["thinking_enabled"] and actual_effort:
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = actual_effort

    # 启用 JSON Output 模式（prompt 中需包含 "json" 字样）
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    # 调用 DeepSeek API（接口格式兼容 OpenAI）
    response = requests.post(
        f"{provider['base_url']}/chat/completions",
        headers={
            "Authorization": f"Bearer {provider['api_key']}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=config["timeout_seconds"],
    )

    # 检查请求是否成功
    if response.status_code != 200:
        raise Exception(f"DeepSeek API 调用失败：{response.status_code} - {response.text}")

    # 返回AI回复的文本
    result = response.json()

    # 小白讲解：打印 DeepSeek 返回的 token 用量与缓存命中情况，方便观察费用和缓存优化效果
    usage = result.get("usage", {})
    if usage:
        hit = usage.get("prompt_cache_hit_tokens", 0)
        miss = usage.get("prompt_cache_miss_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        total_in = hit + miss
        hit_rate = f"{hit * 100 // total_in}%" if total_in > 0 else "0%"
        print(f"[DeepSeek用量] 输入命中缓存:{hit} 未命中:{miss}（命中率{hit_rate}） 输出:{completion}")

    return result["choices"][0]["message"]["content"]


def call_zhipu_vision(image_base64, prompt):
    """
    调用智谱 GLM-4V 图片识别 API - 把图片转成文字描述

    DeepSeek 不支持图片，所以图片识别用智谱的 GLM-4V-Flash（免费模型）。
    配置从数据库读取（场景代码 vision_ocr）。

    参数：
        image_base64: 图片的 base64 编码字符串（不含 data:image 前缀）
        prompt: 给AI的指令，比如"请描述这张图片中的产品信息"

    返回：AI识别出的文字描述
    """
    # 从数据库读取场景配置
    config = get_model_config("vision_ocr")
    if not config:
        raise Exception("图片识别场景配置不存在，请在管理中心检查")

    # 从数据库读取智谱服务商信息
    provider = get_provider("zhipu")
    if not provider or not provider["api_key"]:
        raise Exception("智谱API密钥未配置，请在管理中心配置")

    # 智谱 API 兼容 OpenAI 格式，图片用 image_url 传 base64
    response = requests.post(
        f"{provider['base_url']}/chat/completions",
        headers={
            "Authorization": f"Bearer {provider['api_key']}",
            "Content-Type": "application/json",
        },
        json={
            "model": config["model_name"],
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{image_base64}"
                    }}
                ]
            }],
            "temperature": config["temperature"],
            "max_tokens": config["max_tokens"],
        },
        timeout=config["timeout_seconds"],
    )

    if response.status_code != 200:
        raise Exception(f"智谱图片识别失败：{response.status_code} - {response.text}")

    result = response.json()
    return result["choices"][0]["message"]["content"]


def extract_json_from_text(text):
    """
    从AI回复的文本中提取JSON内容

    AI有时会在JSON前后加一些说明文字，这个方法把纯JSON提取出来。
    """
    # 尝试找 ```json ... ``` 代码块
    match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    # 尝试直接找 { ... } 部分
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    # 直接尝试解析
    return json.loads(text)


# ==================== 功能1：AI解析需求（按需求确认SKILL逻辑）====================
def parse_requirement(input_text, file_content=None, image_base64=None, previous_data=None):
    """
    AI解析需求 - 严格按照"需求确认SKILL"文档的逻辑

    处理流程：
    1. 图片先用智谱GLM-4V识别为文字
    2. 合并所有文字（用户输入+文档+图片描述+之前补充的信息）
    3. AI提取信息，判断必须确认项（核心功能/材质/规格尺寸）是否齐全
    4. 判断需确认项（目标市场/认证/生产条件）是否已确认
    5. 全部确认→生成需求总结和P0-P3关键词，返回confirmed=True
       有缺失→返回confirmed=False + 缺失项 + 追问问题，不生成关键词

    参数：
        input_text: 用户输入的需求文本描述
        file_content: 从上传文档中提取的文本内容（可选）
        image_base64: 上传图片的base64编码（可选）
        previous_data: 之前AI已识别的信息+用户补充的信息（第二轮追问时用）

    返回：字典，包含 confirmed(是否确认完成) + 各字段 + 缺失项 + 追问问题
    """
    full_text = input_text or ""

    # 第一步：如果有图片，先用智谱GLM-4V识别图片内容
    if image_base64:
        image_prompt = (
            "请仔细识别这张图片中的所有信息。这可能是产品图片、规格书、采购需求文档等。"
            "请详细描述：产品名称、规格参数、材质、数量要求、认证要求、任何文字内容等。"
            "请用中文详细描述你看到的所有信息，不要遗漏。"
        )
        image_description = call_zhipu_vision(image_base64, image_prompt)
        full_text += f"\n\n图片识别内容：\n{image_description}"

    # 第二步：合并文档内容
    if file_content:
        full_text += f"\n\n文档内容：\n{file_content}"

    # 第三步：抓取用户在需求描述里贴的网页链接，把网页正文加入分析文本
    # 小白讲解：DeepSeek本身没有联网能力，所以由我们的代码先把网页内容抓下来，
    # 再把正文文本喂给AI，这样AI就能"看到"网页里的产品规格、参数等信息了。
    # 没有贴URL时返回空字符串，不影响原有流程。
    web_content = fetch_urls_from_text(full_text)
    if web_content:
        full_text += f"\n\n网页内容：\n{web_content}"

    # 第四步：合并用户补充的信息（追问第二轮用）
    if previous_data:
        supplement_text = "用户补充确认的信息：\n"
        for k, v in previous_data.items():
            if v:
                supplement_text += f"{k}: {v}\n"
        full_text += f"\n\n{supplement_text}"

    # 第四步：AI提取信息并判断确认状态
    extract_prompt = f"""你是采购需求确认专家。请分析以下用户提供的采购需求，提取信息并判断确认状态。

用户输入：
{full_text}

【网页内容处理指引】
如果输入中包含"网页内容"或"亚马逊产品页"区块，这是从亚马逊产品页自动抓取的信息，请重点从中提取采购寻源相关字段：
- 产品标题：提取完整产品名，包含材质、尺寸、颜色、功能等关键信息
- 品牌：记录品牌名作为参考
- 产品特性（About this item）：从中提取核心功能、材质、尺寸规格等
- 技术规格/产品参数：从中提取精确的尺寸、重量、材质等参数
- 产品描述：补充提取其他产品特征
提取时请综合网页内容与用户文字描述，网页中的产品信息优先级较高（因为是实际产品参考）。

【重要：URL线索模式处理】
如果网页内容区块中出现"URL路径提取"或"URL路径关键词"等字样，说明系统无法获取产品页全文，
只从URL路径中提取了关键词线索。这种情况下：
- 产品标题：使用URL线索中的产品关键词作为产品名称（这些关键词来自亚马逊产品页的URL标题段，是真实产品名）
- 核心功能/材质/规格：从URL关键词中推断，无法推断的留空（不要编造）
- 确认状态：所有能从URL线索推断的字段都正常填写，无法推断的必须字段标记为missing
- 不要因为"无法抓取正文"就把整个需求放弃，URL线索中的关键词是有效的产品参考信息
如果网页内容提示"非亚马逊链接"或"仅支持亚马逊链接"，请在other_requirements中提醒用户改贴亚马逊产品链接。

【提取规则】
必须确认项（缺一不可，缺失时对应字段留空并标记missing）：
- core_functions 核心功能
- material 材质
- spec_size 规格尺寸

也需确认项（用户可以明确回复"不限制""无要求""暂不提供"，但不能默认空）：
- target_market 目标市场
- required_certs 认证要求
- first_purchase_qty 首批采购量
- acceptable_moq 可接受最小起订量
- min_ship_qty 最小发货量

其他可选项（有则填，无则空）：
- product_name 产品名称（必须用完整概括性名称，包含尺寸/材质/特殊特征等，例如"180mm ABS蓝牙5.0防水便携音箱"而非简单的"蓝牙音箱"）
- product_aliases 行业通用别名
- acceptable_lead_time 可接受生产交期
- other_requirements 其他要求（重要：必须主动提取对找供应商有价值但无法归类到上述字段的信息，不要留空。需提取的信息包括但不限于：包装方式如独立包装/每箱N件、贴牌OEM/ODM要求、特殊工艺要求如防水/防锈/抛光、使用场景如家用/医用/户外、配色/外观要求、售后服务要求如质保年限、其他对寻源有参考价值的信息。注意：品牌、价格、评分、销量等对找供应商无价值的信息不要填入。没有额外要求时才留空）

请返回JSON格式（只返回JSON，不要其他文字）：
{{
    "product_name": "产品名称（完整概括性名称，含尺寸材质特殊特征）",
    "product_aliases": "行业通用别名",
    "core_functions": "核心功能",
    "material": "材质",
    "spec_size": "规格尺寸",
    "first_purchase_qty": "首批采购量",
    "acceptable_moq": "可接受最小起订量",
    "min_ship_qty": "最小发货量",
    "acceptable_lead_time": "可接受生产交期",
    "target_market": "目标市场",
    "required_certs": "认证要求",
    "other_requirements": "其他要求（对寻源有价值的信息：包装方式/OEM要求/特殊工艺/使用场景/配色/质保等，无则空）",
    "missing_required": ["缺失的必须确认项字段名，如core_functions/material/spec_size"],
    "missing_optional": ["未确认的需确认项字段名（用户没明确回复不限制/无要求的）"],
    "confirmed": true或false（必须项全有且需确认项都已确认才为true）
}}"""

    messages = [
        {"role": "system", "content": "你是采购需求确认专家，严格按规则判断需求是否确认完成。"},
        {"role": "user", "content": extract_prompt},
    ]

    result_text = call_deepseek(messages, scene_code="req_parse", temperature=0.2)  # 需求解析是结构化提取
    parsed = extract_json_from_text(result_text)

    # 确保字段完整
    parsed.setdefault("product_name", "")
    parsed.setdefault("core_functions", "")
    parsed.setdefault("material", "")
    parsed.setdefault("spec_size", "")
    parsed.setdefault("first_purchase_qty", "")
    parsed.setdefault("acceptable_moq", "")
    parsed.setdefault("min_ship_qty", "")
    parsed.setdefault("target_market", "")
    parsed.setdefault("required_certs", "")
    parsed.setdefault("other_requirements", "")
    parsed.setdefault("missing_required", [])
    parsed.setdefault("missing_optional", [])
    parsed.setdefault("confirmed", False)

    # 第五步：如果未确认，生成追问问题，不生成关键词
    if not parsed.get("confirmed"):
        parsed["requirement_summary"] = ""
        parsed["keywords"] = ""
        parsed["questions"] = _generate_questions(parsed)
        return parsed

    # 第六步：已确认，生成需求总结和P0-P3关键词
    summary_and_keywords = _generate_summary_and_keywords(parsed)
    parsed["requirement_summary"] = summary_and_keywords["requirement_summary"]
    parsed["keywords"] = summary_and_keywords["keywords"]
    parsed["questions"] = []
    return parsed


def _generate_questions(parsed):
    """
    根据缺失项生成追问问题（给用户看的中文问题）

    必须项缺失：必须追问
    需确认项缺失：追问，提示可以回复"不限制/无要求"
    """
    questions = []
    field_names = {
        "core_functions": "核心功能",
        "material": "材质",
        "spec_size": "规格尺寸",
        "target_market": "目标市场",
        "required_certs": "认证要求",
        "first_purchase_qty": "首批采购量",
        "acceptable_moq": "可接受最小起订量",
        "min_ship_qty": "最小发货量",
    }

    # 必须项缺失
    for field in parsed.get("missing_required", []):
        name = field_names.get(field, field)
        questions.append(f"请补充产品的【{name}】（此项为必填，缺一不可）")

    # 需确认项缺失
    for field in parsed.get("missing_optional", []):
        name = field_names.get(field, field)
        questions.append(f'请确认【{name}】（如无要求可回复"不限制"或"无要求"）')

    return questions


def _generate_summary_and_keywords(parsed):
    """
    生成需求总结和P0-P3分级关键词（仅确认完成后调用）

    关键词规则（固定7组，每组中文+英文）：
    - P0：1组，最精准匹配
    - P1：2组，高匹配核心产品
    - P2：2组，关键特征扩展
    - P3：2组，模糊扩展
    """
    prompt = f"""你是采购需求分析专家。需求已确认完成，请生成需求总结和P0-P3分级搜索关键词。

已确认的需求信息：
- 产品名称：{parsed.get('product_name', '')}
- 核心功能：{parsed.get('core_functions', '')}
- 材质：{parsed.get('material', '')}
- 规格尺寸：{parsed.get('spec_size', '')}
- 目标市场：{parsed.get('target_market', '')}
- 认证要求：{parsed.get('required_certs', '')}
- 首批采购量：{parsed.get('first_purchase_qty', '')}
- 可接受MOQ：{parsed.get('acceptable_moq', '')}
- 最小发货量：{parsed.get('min_ship_qty', '')}

请生成：

1. requirement_summary：用一段话完整总结这个采购需求

2. keywords：P0-P3分级关键词，固定7组，每组包含中文、英文关键词和15个搜索变体。
   【重要】所有关键词和变体都要适合B2B平台搜索（1688、中国制造网），必须遵守以下规则：
   - 严禁包含尺寸数值（如1800mm、5cm等），尺寸对平台搜索毫无帮助
   - 严禁包含数量要求（如100个、首批500等）
   - 严禁把多个规格用斜杠/顿号拼成一长串（错误示例："茶色玻璃/亚克力三抽屉电视柜"）
   - 关键词必须是"品类名+核心特征"的简洁组合，能直接作为搜索词用
   - 越往下级别词越短，从P0到P3递减式简化
   - variants是15个与该关键词相关的搜索变体，角度要不同，用于1688多次搜索凑够50家供应商
   - variants变体类型包括：后缀变体（加"批发""定制""厂家""加工厂""直销"等）、
     材质变体、风格变体、用途变体、品类扩展变体等
   - variants里不要有重复的词，每个变体都要和原关键词相关
   - 【核心规则】P0-P3各级关键词中都必须包含产品的主品类词（如"电视柜""充电宝""蓝牙音箱""跑步机"等），
     产品的功能/特征/附属物（如"储物""带灯""插座""下翻门""蓝牙""防水"等）可以放在品类词前作修饰，
     但绝不能替代品类词独立成为关键词
   - 【核心规则】P3是最简品类词，必须描述"这个产品是什么品类"而非"这个产品有什么功能"，
     正确示例（电视柜→"电视柜"/"客厅柜"），错误示例（电视柜→"储物柜""灯带柜"）；
     正确示例（智能手机→"手机"/"智能机"），错误示例（智能手机→"触摸屏""拍照设备"）；
     正确示例（蓝牙音箱→"音箱"/"扬声器"），错误示例（蓝牙音箱→"蓝牙设备""发声器"）

用JSON格式：
{{
    "P0": {{"cn": "完整产品名（6-10字）", "en": "完整英文产品名", "variants": ["变体1","变体2",...共15个]}},
    "P1_1": {{"cn": "核心产品名（4-8字）", "en": "英文核心产品1", "variants": ["变体1","变体2",...共15个]}},
    "P1_2": {{"cn": "另一角度核心产品名（4-8字）", "en": "英文核心产品2", "variants": ["变体1","变体2",...共15个]}},
    "P2_1": {{"cn": "关键特征短词（3-6字）", "en": "英文短词1", "variants": ["变体1","变体2",...共15个]}},
    "P2_2": {{"cn": "另一关键特征短词（3-6字）", "en": "英文短词2", "variants": ["变体1","变体2",...共15个]}},
    "P3_1": {{"cn": "最简品类词（2-4字）", "en": "英文最简词1", "variants": ["变体1","变体2",...共15个]}},
    "P3_2": {{"cn": "另一最简品类词（2-4字）", "en": "英文最简词2", "variants": ["变体1","变体2",...共15个]}}
}}

分级说明与示例（以"1800mm茶色玻璃三抽屉三下翻门带插座灯带电视柜"为例）：
- P0：完整产品名（保留核心特征，去掉尺寸和数量），如"茶色玻璃下翻门电视柜"
- P1：核心产品名（去掉部分特征，2组不同角度），如"下翻门电视柜"/"玻璃电视柜"
- P2：关键特征短词（主品类+单个特征），如"电视柜定制"/"带灯电视柜"
- P3：最简品类词（只保留核心品类词本身），如P3_1="电视柜"，P3_2也="电视柜"
  （如果确实找不到第二个合理的品类词，P3_2可以和P3_1相同，宁可他俩一样也不要硬凑一个不相关的词。
   注意：P3绝不能用"储物柜""客厅柜"等——这些都是偷换概念，把"带XX功能的A"换成了另一个品类B）

请返回JSON格式（只返回JSON）：
{{
    "requirement_summary": "需求总结文字",
    "keywords": {{...上面的P0-P3结构...}}
}}"""

    messages = [
        {"role": "system", "content": "你是采购需求分析专家，擅长生成供应商搜索关键词。"},
        {"role": "user", "content": prompt},
    ]

    result_text = call_deepseek(messages, scene_code="keyword_gen", temperature=0.4)  # 关键词生成是规则明确任务
    return extract_json_from_text(result_text)


# ==================== 功能3：AI自动初筛 ====================
def auto_screening(supplier, requirement):
    """
    【已废弃】旧版AI自动初筛 - 单一prompt黑盒判断

    小白讲解：这个函数是旧版初筛，用一段prompt让AI自由发挥判断。
    新版初筛已迁移到 screening_engine.py，采用"规则驱动 + AI语义辅助"的标准化流程：
    - 11条一票否决规则（注册资本/经营状态/经营异常/失信等）
    - 6条评分规则（100分体系：注册资本25+经营年限15+匹配度30+联系方式10+风险15+出口5）
    - 天眼查MCP实时数据采集
    - 完整审计日志

    新代码请调用 screening_engine.run_screening()，本函数仅为向后兼容保留。

    参数：
        supplier: 供应商信息字典
        requirement: 关联的需求信息字典

    返回：初筛结果字典（包含风险排查、资质核实、评分等）
    """
    prompt = f"""你是一个供应商风险评估专家。请对以下供应商进行风险排查和资质核实初筛。

【供应商信息】
名称：{supplier.get('name', '')}
简介：{supplier.get('intro', '')}
主营产品：{supplier.get('main_product', '')}
工厂地址：{supplier.get('factory_address', '')}
经营状态：{supplier.get('operating_status', '存续')}
成立年限：{supplier.get('establish_years', '未知')}
来源：{supplier.get('source', '')}

【采购需求】
产品：{requirement.get('product_name', '')}
目标市场：{requirement.get('target_market', '')}
要求认证：{requirement.get('required_certs', '')}

请进行初筛评估，返回JSON格式（只返回JSON）：
{{
    "trademark_result": "商标查询结果（无风险/潜在风险/明确侵权迹象）",
    "patent_result": "专利查询结果（无风险/潜在风险/明确侵权迹象）",
    "lawsuit_result": "侵权诉讼记录（无记录/有诉讼未败诉/近3年败诉）",
    "platform_infringe": "平台侵权记录（无记录/有投诉未确认/确认侵权下架）",
    "own_ip": "自有知识产权情况",
    "risk_summary": "风险排查总结",
    "cert_authenticity": "目标市场认证情况（列出持有的认证）",
    "test_report": "第三方检测报告（有/无）",
    "customs_qualification": "进出口经营权（有/无）",
    "export_record": "出口备案情况",
    "label_compliance": "标签合规情况",
    "qual_summary": "资质核实总结",
    "establish_years": "成立年限（数字）",
    "screening_note": "初筛整体说明"
}}

评分规则提醒：
- 商标/专利：无风险20分/潜在风险10分/明确侵权0分（各20分，共40分）
- 认证：2项以上20分/1项10分/无0分；检测报告有10分；进出口经营权有10分（共40分）
- 成立年限：≥5年20分/2-5年10分（共20分）
- 一票否决：经营状态非存续/近3年侵权败诉/平台确认侵权下架

请基于供应商信息合理推断，如果信息不足就标注"未知"。"""

    messages = [
        {"role": "system", "content": "你是供应商风险评估专家，擅长知识产权风险排查和资质核实。"},
        {"role": "user", "content": prompt},
    ]

    result_text = call_deepseek(messages, scene_code="auto_screening", temperature=0.2)  # 风险评估需综合判断
    return extract_json_from_text(result_text)
