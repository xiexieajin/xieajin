# 临时测试脚本：验证亚马逊URL的提取和抓取（修复后）
import sys
sys.path.insert(0, 'd:/pycharm/供应商寻源系统')
from ai_helper import extract_urls, fetch_url_content

# 模拟用户真实输入（带反引号包裹的URL）
text = "参考`https://www.amazon.com/Muwuele-Overbed-Adjustable-Hospital-Standing/dp/B0DWJ5FY8P/ref=sr_1_1?crid=1S5JG64HXTK42&dib=eyJ2IjoiMSJ9.T6zb&keywords=muwuele&th=1`"

print("===== 1. URL提取测试（反引号清理验证）=====")
urls = extract_urls(text)
print(f"提取到 {len(urls)} 个URL:")
for u in urls:
    print(f"  [{u}]")
    # 验证末尾不再有反引号
    assert not u.endswith("`"), f"反引号未清理: {u[-3:]}"
print("  反引号清理: OK")
print()

# 测试抓取亚马逊完整URL（验证反爬兜底）
print("===== 2. 抓取亚马逊URL（反爬兜底验证）=====")
content = fetch_url_content(urls[0])
print(f"抓取内容长度: {len(content)} 字符")
print("--- 完整内容 ---")
print(content)
print()
print("--- 关键信息检查 ---")
print(f"  含产品线索'Muwuele Overbed': {'Muwuele Overbed' in content or 'Muwuele' in content}")
print(f"  含参考URL: {'参考URL' in content or 'amazon.com' in content}")
