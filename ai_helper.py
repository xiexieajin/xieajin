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
from concurrent.futures import ThreadPoolExecutor
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

# Jina Reader / Firecrawl 网页抓取超时时间（秒），比普通请求长一些，因为需要后台解析
_FETCH_WEB_TIMEOUT = 30

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


def _fetch_url_fallback(url):
    """
    兜底方案：用curl/requests抓取网页HTML，解析正文（当Jina/Firecrawl都不可用时）

    小白讲解：这是传统的网页抓取方式。先尝试用curl下载HTML（绕过TLS指纹反爬），
    curl不可用时回退到requests。下载后用BeautifulSoup提取正文。
    对于亚马逊页面会尝试提取结构化产品信息，非亚马逊页面走通用文本提取。

    参数：
        url: 要抓取的网页链接

    返回：网页正文文字（字符串）。抓取失败时返回带失败说明的短文本，不抛异常。
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


def fetch_url_content(url):
    """
    抓取单个网页的正文内容（优先用Jina Reader / Firecrawl并发抓取，不可用时回退curl爬虫）

    小白讲解：系统会同时尝试Jina Reader和Firecrawl两个服务来读取网页内容。
    哪个开启了就用哪个，两个都开了就并发抓取（同时跑，不互相等待），
    结果合并后给AI分析。两个都不可用时回退到传统的curl抓取方式。
    不再限制网站类型，任何URL都支持！

    参数：
        url: 要抓取的网页链接

    返回：网页正文文字（字符串）。所有方式都失败时返回兜底提示。
    """
    jina_cfg = get_provider("jina_reader")
    firecrawl_cfg = get_provider("firecrawl")

    jina_enabled = bool(jina_cfg and jina_cfg.get("is_enabled"))
    firecrawl_enabled = bool(firecrawl_cfg and firecrawl_cfg.get("is_enabled") and firecrawl_cfg.get("api_key"))

    if not jina_enabled and not firecrawl_enabled:
        return _fetch_url_fallback(url)

    # 亚马逊链接优先用curl结构化提取（能精准拿到产品标题/品牌/特性/规格表，
    # 避免Firecrawl返回的大量推荐商品和广告噪音淹没真实产品信息）
    if _is_amazon_url(url):
        fallback = _fetch_url_fallback(url)
        if fallback and len(fallback.strip()) > 200:
            return fallback

    results = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {}
        if jina_enabled:
            futures["jina"] = executor.submit(_fetch_with_jina, url)
        if firecrawl_enabled:
            futures["firecrawl"] = executor.submit(_fetch_with_firecrawl, url, firecrawl_cfg["api_key"])

        for name, future in futures.items():
            try:
                text = future.result(timeout=_FETCH_WEB_TIMEOUT)
                if text and len(text.strip()) > 100:
                    results[name] = text
            except Exception:
                pass

    if results:
        parts = []
        labels = {"jina": "Jina Reader", "firecrawl": "Firecrawl"}
        for name in ["jina", "firecrawl"]:
            if name in results:
                text = results[name]
                if len(text) > _MAX_CONTENT_PER_URL:
                    text = text[:_MAX_CONTENT_PER_URL] + "\n...(内容过长已截断)"
                source_count = len(results)
                if source_count == 1:
                    parts.append(f"【{labels[name]}提取】\n{text}")
                else:
                    parts.append(f"【来源{len(parts) + 1}：{labels[name]}提取】\n{text}")
        return "\n\n---\n\n".join(parts)

    return _fetch_url_fallback(url)


def _fetch_with_jina(url):
    """
    用Jina Reader抓取网页正文（免费、无需API Key、任何网站都支持）

    小白讲解：Jina Reader是一个免费的网页阅读器。只需要在目标URL前面加上
    https://r.jina.ai/，它就会帮你下载网页、提取正文、转成干净的Markdown文本。
    支持任何网站（亚马逊、沃尔玛、1688、速卖通...），不限类型。

    参数：
        url: 要抓取的网页链接

    返回：干净的Markdown格式文本。失败返回空字符串。
    """
    try:
        resp = requests.get(
            f"https://r.jina.ai/{url}",
            headers={"Accept": "text/markdown"},
            timeout=_FETCH_WEB_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.text
        return ""
    except Exception:
        return ""


def _fetch_with_firecrawl(url, api_key):
    """
    用Firecrawl API抓取网页正文（支持JS渲染、专为AI设计）

    小白讲解：Firecrawl是专为AI设计的网页抓取服务。它用无头浏览器渲染页面
    （包括JS动态加载的内容），然后提取正文转成Markdown。比Jina Reader更适合
    复杂的动态页面。需要注册获取API Key（免费额度500次/月）。

    参数：
        url: 要抓取的网页链接
        api_key: Firecrawl的API密钥

    返回：干净的Markdown格式文本。失败返回空字符串。
    """
    try:
        resp = requests.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"url": url, "formats": ["markdown"]},
            timeout=_FETCH_WEB_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("data", {}).get("markdown", "")
        return ""
    except Exception:
        return ""


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


def _detect_url_platform(url):
    """
    识别URL所属电商平台，返回平台信息（用于反爬场景下的针对性引导）

    小白讲解：不同电商平台的反爬强度和URL结构都不同。
    这个函数根据URL域名识别平台，返回：
    - platform: 平台中文名（如"天猫""京东"）
    - is_anti_crawl: 是否强反爬（True=系统大概率抓不到内容）
    - item_id: 从URL提取的商品ID（如果有的话）
    - suggestion: 给用户的针对性建议

    参数：url 网页链接
    返回：dict，包含 platform/is_anti_crawl/item_id/suggestion 四个字段
    """
    from urllib.parse import urlparse, parse_qs
    import re as _re

    # 小白讲解：从URL路径里提取第一段纯数字（商品ID）
    # 例如 "/100012345.html" → "100012345"，"/product/100012345.html" → "100012345"
    # 这样避免把"item""product""offer"等路径段当成ID
    def _extract_first_digits_fn(path_str):
        if not path_str:
            return None
        m = _re.search(r'(\d{4,})', path_str)
        return m.group(1) if m else None

    url_lower = url.lower()
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path or ""
    query = parse_qs(parsed.query)

    # 各平台识别规则（按用户实际可能贴的顺序）
    # 每条：(域名关键词列表, 平台名, 是否强反爬, 商品ID提取函数, 建议)
    rules = [
        # 天猫/淘宝：强反爬+SPA，基本抓不到
        (["detail.tmall.com", "item.taobao.com"], "天猫/淘宝", True,
         lambda: query.get("id", [None])[0],
         "天猫/淘宝商品页有强反爬+需要登录，系统无法自动抓取。建议：1) 改用京东/亚马逊商品链接；2) 上传产品图片或规格书；3) 直接用关键词描述产品。"),
        # 京东：PC版部分能抓到，但SPA居多
        (["item.jd.com", "item.m.jd.com"], "京东", True,
         lambda: _extract_first_digits_fn(path),
         "京东商品页是SPA单页应用，可能抓不到完整信息。建议：1) 上传产品图片；2) 手动补充产品名称和规格；3) 改用亚马逊链接。"),
        # 拼多多：强反爬+需要登录
        (["yangkeduo.com", "pinduoduo.com", "mobile.yangkeduo.com"], "拼多多", True,
         lambda: query.get("goods_id", [None])[0],
         "拼多多商品页需要登录/APP内打开，系统无法抓取。建议：1) 在拼多多APP查看商品后手动补充；2) 上传产品图片；3) 用关键词描述。"),
        # 1688：有反爬但比天猫轻，有时能抓到
        (["detail.1688.com", "offer.1688.com", "m.1688.com"], "1688", True,
         lambda: _extract_first_digits_fn(path),
         "1688商品页有反爬但通常可抓到部分信息。如抓取失败建议：1) 直接用产品关键词在本系统搜索1688供应商；2) 上传产品图片。"),
        # 速卖通：国际站，反爬中等
        (["aliexpress.com", "m.aliexpress.com"], "速卖通", True,
         lambda: _extract_first_digits_fn(path),
         "速卖通商品页反爬中等。建议：1) 上传产品图片；2) 手动补充产品规格；3) 用关键词描述。"),
        # 亚马逊：通常能用curl抓到
        (["amazon.com", "amazon.cn", "amazon.co.jp", "amazon.co.uk", "amazon.de",
          "amazon.fr", "amazon.it", "amazon.es", "amazon.ca", "amazon.com.au",
          "amazon.com.mx", "amazon.in"], "亚马逊", False,
         lambda: None,  # 亚马逊用slug提取，这里不提取ID
         "亚马逊商品页系统通常能抓到，如果失败请上传产品图片。"),
        # 沃尔玛：通常能抓到
        (["walmart.com", "walmart.cn"], "沃尔玛", False,
         lambda: _extract_first_digits_fn(path),
         "沃尔玛商品页通常可抓取，如果失败请上传产品图片。"),
        # eBay：通常能抓到
        (["ebay.com", "ebay.cn"], "eBay", False,
         lambda: None,
         "eBay商品页通常可抓取，如果失败请上传产品图片。"),
        # 小红书：强反爬+SPA
        (["xiaohongshu.com", "xhslink.com"], "小红书", True,
         lambda: path.strip("/").split("/")[-1] if path else None,
         "小红书商品页有强反爬+需要登录。建议：1) 上传产品图片；2) 用关键词描述。"),
        # 抖音/抖店：强反爬+需要登录
        (["douyin.com", "haodanku.com"], "抖音", True,
         lambda: None,
         "抖音商品页需要登录或APP内打开。建议：1) 上传产品图片；2) 用关键词描述。"),
    ]

    # 通用识别：遍历规则匹配域名
    for domains, platform, is_anti, id_fn, suggestion in rules:
        for d in domains:
            if d in host:
                # 提取商品ID（异常时返回None）
                try:
                    item_id = id_fn()
                except Exception:
                    item_id = None
                return {
                    "platform": platform,
                    "is_anti_crawl": is_anti,
                    "item_id": item_id,
                    "suggestion": suggestion,
                }

    # 未识别平台：保守标记为未知，建议手动补充
    return {
        "platform": "未知平台",
        "is_anti_crawl": True,  # 未知平台保守按反爬处理
        "item_id": None,
        "suggestion": "未识别该链接所属平台，可能存在反爬。如抓取失败建议：1) 上传产品图片或规格书；2) 直接用关键词描述产品需求。",
    }


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
    当无法抓取网页正文时，用URL线索拼成结构化产品信息（带平台识别）

    小白讲解：这是兜底方案。当反爬拦截（curl/requests都被挡）时，
    网页正文拿不到，但从URL里能提取两类线索：
    1. 平台信息：识别这是天猫/京东/拼多多等，告诉AI该平台反爬特点
    2. 商品ID/URL slug：让AI至少知道用户参考的是哪个商品

    参数：
        url: 商品页链接
    返回：结构化文本，包含平台/商品ID/反爬状态/建议
    """
    # 1. 识别平台
    platform_info = _detect_url_platform(url)
    platform = platform_info["platform"]
    item_id = platform_info["item_id"]
    is_anti = platform_info["is_anti_crawl"]
    suggestion = platform_info["suggestion"]

    # 2. 从URL路径提取关键词线索（亚马逊等有slug的平台能拿到）
    url_hint = _extract_product_hint_from_url(url)
    hint_words = url_hint.split() if url_hint and url_hint != "无" else []

    # 3. 拼装结构化文本
    parts = []

    # 平台信息块（让AI知道这是什么平台、反爬状态如何）
    parts.append(f"【链接平台】{platform}")
    if item_id:
        parts.append(f"【商品ID】{item_id}")
    parts.append(f"【反爬状态】{'强反爬，系统无法自动抓取商品详情' if is_anti else '通常可抓取，但本次可能失败'}")
    parts.append(f"【系统建议】{suggestion}")

    # URL线索关键词块（如果能从slug提取到）
    if hint_words:
        parts.append(f"【产品关键词线索】{url_hint}（来源：URL路径提取）")
        parts.append("【关键词拆解】")
        for word in hint_words:
            parts.append(f"- {word}")

    parts.append(f"【完整链接】{url}")

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


def fetch_urls_from_text(text, progress_callback=None):
    """
    从文本中找出所有URL并批量抓取网页内容，合并成一段给AI分析用的文本

    小白讲解：这是给parse_requirement调用的总入口。
    它先找出文本里所有网页链接，然后逐个抓取网页内容，拼成一段文字返回。
    Jina Reader和Firecrawl都开启时会同时抓取、合并结果给AI交叉验证。
    不限网站类型（亚马逊、沃尔玛、1688、速卖通等都支持）。
    如果没有URL就返回空字符串（不影响原有解析流程）。

    参数：
        text: 用户输入的需求描述文本（可能包含URL）
        progress_callback: 可选，进度回调函数 f(step, message, status)

    返回：拼接好的网页正文文本。无URL时返回空字符串。
          所有网页内容合计超过_MAX_TOTAL_WEB_CONTENT字数则截断。
    """
    urls = extract_urls(text)
    if not urls:
        return ""

    # 通知前端：检测到链接
    jina_cfg = get_provider("jina_reader")
    firecrawl_cfg = get_provider("firecrawl")
    jina_on = bool(jina_cfg and jina_cfg.get("is_enabled"))
    firecrawl_on = bool(firecrawl_cfg and firecrawl_cfg.get("is_enabled") and firecrawl_cfg.get("api_key"))
    tools = []
    if jina_on:
        tools.append("Jina Reader")
    if firecrawl_on:
        tools.append("Firecrawl")
    if not tools:
        tools.append("内置爬虫")

    if progress_callback:
        progress_callback("urls_found",
            f"🔍 检测到 {len(urls)} 个网页链接，将用 {' + '.join(tools)} 抓取...", "running")

    chunks = []
    total_len = 0
    for idx, url in enumerate(urls, start=1):
        if progress_callback:
            short_url = url if len(url) <= 60 else url[:57] + "..."
            progress_callback("fetching_url",
                f"🌐 正在抓取网页 {idx}/{len(urls)}：{short_url}", "running")
        content = fetch_url_content(url)
        # 通知前端抓取结果（字数=0说明抓取失败）
        content_len = len(content.strip()) if content else 0
        if progress_callback:
            if content_len > 500:
                progress_callback("fetch_ok",
                    f"✅ 网页 {idx} 抓取成功（{content_len} 字）", "running")
            elif content_len > 100:
                progress_callback("fetch_ok",
                    f"⚠️ 网页 {idx} 抓取内容较少（{content_len} 字），部分信息可能缺失", "running")
            else:
                progress_callback("fetch_fail",
                    f"❌ 网页 {idx} 抓取失败（仅 {content_len} 字），将尝试从URL推断", "running")
        chunk = f"--- 网页{idx}：{url} ---\n{content}"
        chunks.append(chunk)
        total_len += len(chunk)
        if total_len >= _MAX_TOTAL_WEB_CONTENT:
            chunks.append(f"\n...(已达到网页内容总字数上限，后续网页不再抓取)")
            break

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


def classify_hs_code(product_name):
    """
    用DeepSeek根据产品名称自动归类HS编码

    小白讲解：海关数据搜索需要HS编码才能精准过滤产品。
    不同产品的HS编码不一样（比如电视机柜是9403，LED灯是9405），不能写死。
    这个函数用AI根据产品名称自动判断最可能的HS编码，省去手动查编码表。

    参数：
        product_name: 产品名称，如 "电视机柜"

    返回：hs_code 字符串，如 "9403"，失败时返回空字符串
    """
    try:
        result = call_deepseek(
            messages=[{
                "role": "user",
                "content": (
                    f"请判断以下产品最可能的海关HS编码（前4位即可），返回JSON格式。\n\n"
                    f"产品名称：{product_name}\n\n"
                    f"要求：\n"
                    f"1. 返回前4位HS编码（如9403代表家具），不需要后几位\n"
                    f"2. 如果产品可能属于多个HS编码类别，选最匹配的那个\n"
                    f"3. 同时返回该HS编码的中文描述\n\n"
                    f"返回格式示例：{{\"hs_code\": \"9403\", \"description\": \"家具\"}}"
                ),
            }],
            scene_code="req_parse",  # 复用需求解析场景配置（temperature低，输出稳定）
            json_mode=True,
            max_tokens=512,  # 思考模式需要足够token给推理+输出（100不够）
        )
        data = json.loads(result)
        hs_code = data.get("hs_code", "").strip()
        # 只保留数字（去除可能的空格、点号等）
        hs_code = re.sub(r'[^0-9]', '', hs_code)
        if len(hs_code) >= 4:
            return hs_code[:4]
        print(f"HS编码归类失败：返回格式异常 - {result}")
        return ""
    except Exception as e:
        print(f"HS编码归类异常（产品：{product_name}）：{e}")
        return ""


# ==================== 功能1：AI解析需求（按需求确认SKILL逻辑）====================
def parse_requirement(input_text, file_content=None, image_base64=None, previous_data=None, progress_callback=None):
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
        progress_callback: 可选，进度回调 f(step, message, status)，status为 running/done/error

    返回：字典，包含 confirmed(是否确认完成) + 各字段 + 缺失项 + 追问问题
    """
    full_text = input_text or ""

    # 第一步：如果有图片，先用智谱GLM-4V识别图片内容
    if image_base64:
        if progress_callback:
            progress_callback("ocr_image", "🖼️ 正在用智谱AI识别图片内容...", "running")
        image_prompt = (
            "请仔细识别这张图片中的所有信息。这可能是产品图片、规格书、采购需求文档等。"
            "请详细描述：产品名称、规格参数、材质、数量要求、认证要求、任何文字内容等。"
            "请用中文详细描述你看到的所有信息，不要遗漏。"
        )
        image_description = call_zhipu_vision(image_base64, image_prompt)
        full_text += f"\n\n图片识别内容：\n{image_description}"

    # 第二步：合并文档内容
    if file_content:
        if progress_callback:
            progress_callback("merge_doc", "📄 正在合并上传的文档内容...", "running")
        full_text += f"\n\n文档内容：\n{file_content}"

    # 第三步：抓取用户在需求描述里贴的网页链接，把网页正文加入分析文本
    if progress_callback:
        progress_callback("fetch_web", "🌐 正在检查需求中的网页链接...", "running")
    web_content = fetch_urls_from_text(full_text, progress_callback=progress_callback)
    if web_content:
        full_text += f"\n\n网页内容：\n{web_content}"

    # 第四步：合并用户补充的信息（追问第二轮用）
    if previous_data:
        supplement_text = "用户补充确认的信息：\n"
        for k, v in previous_data.items():
            if v:
                supplement_text += f"{k}: {v}\n"
        full_text += f"\n\n{supplement_text}"

    # 第五步：AI提取信息并判断确认状态
    if progress_callback:
        progress_callback("ai_analyze", "🤖 AI 正在分析需求（DeepSeek深度思考中，请耐心等待）...", "running")
    extract_prompt = f"""分析以下内容，提取产品采购需求。

【说明】
用户可能只粘贴了产品链接，下面的"网页内容"区块就是系统自动从该链接抓取的产品页信息。
你的任务：从网页内容中直接提取产品参数，有数据的字段必须填写，不能空着。

{full_text}

【提取规则】
从网页内容中照着找，网页写什么你就填什么：
- product_name: 产品名称（含品牌+品类+关键材质/尺寸特征）
- product_aliases: 行业别名（网页提到的其他叫法）
- core_functions: 核心功能（产品能干什么，从标题/描述提取）
- material: 材质（从材质表提取，如"橡胶木""不锈钢""ABS塑料"）
- spec_size: 规格尺寸（长宽高重等，从规格表提取）
- target_market: 目标市场（从销售平台推断，如"亚马逊美国站"）
- required_certs: 认证（网页提到什么认证就填，没提就空）
- first_purchase_qty: 首批采购量（用户没指定就空）
- acceptable_moq: 可接受MOQ（用户没指定就空）
- min_ship_qty: 最小发货量（用户没指定就空）
- acceptable_lead_time: 生产交期（用户没指定就空）
- other_requirements: 其他（包装/OEM/使用场景/配色/质保等）


网页有数据但你没填 = 漏填。请对照网页内容逐项检查每个字段。

【反爬空壳识别 - 重要】
如果"网页内容"区块出现以下特征，说明该链接反爬严格（如天猫/淘宝商品页、需要登录的站点等），抓取到的不是真实商品信息：
- 内容里只有"店铺 客服 收藏 加入购物车 立即购买""商品详情页"等导航词
- 出现"URL路径无法提取有效关键词""抓取失败""反爬"等抓取失败提示
- 内容很短（不到200字）且无具体产品参数

遇到这种情况：
- product_name / core_functions / material / spec_size 等字段留空（不要瞎编）
- confirmed 设为 false
- missing_required 必须包含 ["product_name", "core_functions", "material", "spec_size"]
- 追问问题里要明确告知用户："该链接（如天猫/淘宝商品页）反爬严格，系统无法自动提取商品信息，请补充以下关键字段：产品名称、核心功能、材质、规格尺寸等。也可改用其他平台商品链接（如亚马逊、京东）或上传产品图片/规格书。"

JSON格式（只返回JSON）：
{{
    "product_name": "完整产品名",
    "product_aliases": "行业别名",
    "core_functions": "核心功能",
    "material": "材质",
    "spec_size": "规格尺寸",
    "first_purchase_qty": "",
    "acceptable_moq": "",
    "min_ship_qty": "",
    "acceptable_lead_time": "",
    "target_market": "",
    "required_certs": "",
    "other_requirements": "",
    "missing_required": [],
    "missing_optional": [],
    "confirmed": false  ← 改为true如果material/core_functions/spec_size都从网页中提取到了
}}"""

    messages = [
        {"role": "system", "content": "你是产品采购需求分析助手。从给定的内容中提取产品参数，网页里有数据的字段必须填写，不要漏。"},
        {"role": "user", "content": extract_prompt},
    ]

    # 小白讲解：temperature=None 表示用数据库场景配置里的温度值（管理员可在管理中心调整）
    result_text = call_deepseek(messages, scene_code="req_parse", temperature=None)  # 需求解析是结构化提取
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
        # 小白讲解：把用户输入的URL一起传过去，便于生成平台针对性的追问
        # 例如天猫链接会提示"改用京东/亚马逊链接"，京东会提示"上传产品图片"等
        user_urls = extract_urls(full_text)
        parsed["questions"] = _generate_questions(parsed, user_urls=user_urls)
        if progress_callback:
            progress_callback("step_done", "✅ 解析完成（需要补充信息），正在整理结果...", "running")
        return parsed

    # 第六步：已确认，生成需求总结和P0-P3关键词
    if progress_callback:
        progress_callback("gen_summary", "📝 正在生成需求总结和搜索关键词...", "running")
    summary_and_keywords = _generate_summary_and_keywords(parsed)
    parsed["requirement_summary"] = summary_and_keywords["requirement_summary"]
    parsed["keywords"] = summary_and_keywords["keywords"]
    parsed["questions"] = []
    if progress_callback:
        progress_callback("step_done", "✅ 解析完成，正在整理结果...", "running")
    return parsed


def _generate_questions(parsed, user_urls=None):
    """
    根据缺失项生成追问问题（给用户看的中文问题）

    必须项缺失：必须追问
    需确认项缺失：追问，提示可以回复"不限制/无要求"

    小白讲解：如果传了 user_urls，会识别每个链接所属平台，
    在追问前加一条"反爬说明"问题，让用户知道为什么字段为空、该怎么补救。
    例如天猫链接会提示"该链接反爬无法自动抓取，建议改用京东/亚马逊链接或上传图片"。

    参数：
        parsed: AI解析结果字典
        user_urls: 用户输入中提取的URL列表（可选）
    返回：追问问题字符串列表
    """
    questions = []
    field_names = {
        "product_name": "产品名称",
        "product_aliases": "行业通用别名",
        "core_functions": "核心功能",
        "material": "材质",
        "spec_size": "规格尺寸",
        "target_market": "目标市场",
        "required_certs": "认证要求",
        "first_purchase_qty": "首批采购量",
        "acceptable_moq": "可接受最小起订量",
        "min_ship_qty": "最小发货量",
        "acceptable_lead_time": "可接受生产交期",
        "other_requirements": "其他要求",
    }

    # 如果有URL且识别到强反爬平台，先加一条针对性提示问题
    # 小白讲解：这条问题告诉用户"为什么字段都是空的"，并给出可操作建议
    if user_urls:
        for url in user_urls[:3]:  # 最多看前3个链接
            platform_info = _detect_url_platform(url)
            if platform_info["is_anti_crawl"]:
                platform = platform_info["platform"]
                item_id = platform_info.get("item_id")
                suggestion = platform_info["suggestion"]
                # 拼装一条醒目的提示问题
                id_hint = f"（商品ID: {item_id}）" if item_id else ""
                questions.append(
                    f"⚠️ 您贴的{platform}链接{id_hint}有强反爬，系统无法自动提取商品信息。{suggestion}"
                )
                # 只提示一次，避免多个链接重复刷屏
                break

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

    # 小白讲解：temperature=None 表示用数据库场景配置里的温度值（管理员可在管理中心调整）
    result_text = call_deepseek(messages, scene_code="keyword_gen", temperature=None)  # 关键词生成是规则明确任务
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

    # 小白讲解：temperature=None 表示用数据库场景配置里的温度值（管理员可在管理中心调整）
    result_text = call_deepseek(messages, scene_code="auto_screening", temperature=None)  # 风险评估需综合判断
    return extract_json_from_text(result_text)
