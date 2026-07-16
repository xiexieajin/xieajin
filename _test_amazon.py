# 调研：检查requests拿到的亚马逊HTML到底是产品页还是拦截页
import requests
import re

AMAZON_URL = "https://www.amazon.com/Muwuele-Overbed-Adjustable-Hospital-Standing/dp/B0DWJ5FY8P/ref=sr_1_1?keywords=muwuele&th=1"

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}
resp = requests.get(AMAZON_URL, headers=headers, timeout=15)
html = resp.text
print(f"状态码: {resp.status_code}, HTML长度: {len(html)}")

# 检查关键产品信息
print("\n--- 关键信息检查 ---")
keywords = ["Muwuele", "Overbed", "Adjustable", "Hospital", "Standing",
            "productTitle", "product-title", "About this item", "Brand",
            "B0DWJ5FY8P", "Type the characters", "captcha", "robot"]
for kw in keywords:
    found = kw.lower() in html.lower()
    print(f"  含 '{kw}': {found}")

# 检查title标签
title_match = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL | re.IGNORECASE)
print(f"\n--- title标签 ---")
print(f"  {title_match.group(1).strip() if title_match else '无'}")

# 检查是否是验证码/拦截页
print("\n--- 拦截页特征检查 ---")
block_features = ["captcha", "robot", "Type the characters", "continue shopping",
                  "Sorry, we just need to make sure", "Enter the characters"]
for feat in block_features:
    if feat.lower() in html.lower():
        print(f"  [拦截] 含 '{feat}'")

# 打印HTML中所有可见文本片段（去掉标签后）
print("\n--- 去标签后纯文本（前800字符）---")
from bs4 import BeautifulSoup
soup = BeautifulSoup(html, "html.parser")
for tag in soup(["script", "style", "noscript"]):
    tag.decompose()
text = soup.get_text(separator="\n", strip=True)
text = re.sub(r"\n{2,}", "\n", text)
print(text[:800])
