# 调研测试：验证不同方案能否真正抓到亚马逊网页正文
import requests
import time

AMAZON_URL = "https://www.amazon.com/Muwuele-Overbed-Adjustable-Hospital-Standing/dp/B0DWJ5FY8P/ref=sr_1_1?keywords=muwuele&th=1"

print("=" * 60)
print("方案A：Jina AI Reader（免费AI阅读器服务）")
print("=" * 60)
print("原理：调用 https://r.jina.ai/{url} 把网页转成LLM友好的纯文本")
print()
try:
    start = time.time()
    # Jina AI Reader 专门为AI/LLM设计，能绕过大部分反爬
    jina_url = f"https://r.jina.ai/{AMAZON_URL}"
    resp = requests.get(jina_url, timeout=30, headers={"Accept": "text/plain"})
    elapsed = time.time() - start
    print(f"状态码: {resp.status_code}")
    print(f"响应长度: {len(resp.text)} 字符")
    print(f"耗时: {elapsed:.1f} 秒")
    print()
    print("--- 前600字符 ---")
    print(resp.text[:600])
    print()
    # 关键信息检查
    print("--- 关键信息检查 ---")
    keywords = ["Muwuele", "Overbed", "Adjustable", "Hospital", "Standing", "Brand", "Price", "About this item"]
    for kw in keywords:
        found = kw.lower() in resp.text.lower()
        print(f"  含 '{kw}': {found}")
except Exception as e:
    print(f"请求失败: {e}")

print()
print("=" * 60)
print("方案B：增强requests（对比基线）")
print("=" * 60)
try:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    resp2 = requests.get(AMAZON_URL, headers=headers, timeout=15)
    print(f"状态码: {resp2.status_code}")
    print(f"响应长度: {len(resp2.text)} 字符")
    print("--- 前300字符 ---")
    print(resp2.text[:300])
except Exception as e:
    print(f"请求失败: {e}")
