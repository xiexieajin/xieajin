# 测试"其他"字段提取效果
import sys
sys.path.insert(0, 'd:/pycharm/供应商寻源系统')
import requests
import re

session = requests.Session()

# 登录
session.post('http://localhost:5000/login',
             data={'username': 'xieajin', 'password': 'bsq123'},
             allow_redirects=False, timeout=10)

# 场景1：纯文本需求，含包装/OEM/工艺/质保等要求
print("=" * 60)
print("场景1：纯文本需求（含包装/OEM/工艺/质保等）")
print("=" * 60)
input_text1 = """我们需要采购一批蓝牙音箱，用于户外露营场景。
产品要求：防水IPX7等级，续航10小时以上，重量不超过500g。
材质ABS塑料，尺寸直径80mm高度100mm。
首批采购500台，需要CE和FCC认证，目标市场欧洲。
要求独立包装每箱20件，需要贴牌OEM印我们公司logo，
提供2年质保，表面磨砂工艺防滑处理。"""

print(f"输入: {input_text1[:80]}...")
resp1 = session.post('http://localhost:5000/ai/parse-requirement',
                     data={'input_text': input_text1}, timeout=120)
print(f"状态码: {resp1.status_code}")

# 提取other_requirements的值
content1 = resp1.text
match1 = re.search(r'name="other_requirements"[^>]*>([^<]*)</textarea>', content1, re.DOTALL)
if match1:
    other_val = match1.group(1).strip()
    # HTML实体解码
    other_val = other_val.replace('&#34;', '"').replace('&#39;', "'").replace('&amp;', '&')
    print(f"\n【其他要求字段内容】")
    print(other_val if other_val else "(空)")
    # 检查是否提取到了关键信息
    print("\n--- 关键信息检查 ---")
    checks = ["包装", "OEM", "贴牌", "logo", "质保", "工艺", "磨砂", "防滑", "户外", "露营"]
    for c in checks:
        print(f"  含'{c}': {c in other_val}")
else:
    print("未找到other_requirements字段")

# 场景2：亚马逊链接
print("\n" + "=" * 60)
print("场景2：亚马逊链接")
print("=" * 60)
input_text2 = "参考`https://www.amazon.com/Muwuele-Overbed-Adjustable-Hospital-Standing/dp/B0DWJ5FY8P/ref=sr_1_1?keywords=muwuele&th=1`"
print(f"输入: {input_text2[:70]}...")
resp2 = session.post('http://localhost:5000/ai/parse-requirement',
                     data={'input_text': input_text2}, timeout=120)
print(f"状态码: {resp2.status_code}")

content2 = resp2.text
match2 = re.search(r'name="other_requirements"[^>]*>([^<]*)</textarea>', content2, re.DOTALL)
if match2:
    other_val2 = match2.group(1).strip()
    other_val2 = other_val2.replace('&#34;', '"').replace('&#39;', "'").replace('&amp;', '&')
    print(f"\n【其他要求字段内容】")
    print(other_val2 if other_val2 else "(空)")
    # 检查是否包含了无价值信息（不应该出现）
    print("\n--- 无价值信息检查（不应出现）---")
    no_value = ["品牌", "Muwuele", "价格", "HKD", "评分", "297", "销量", "100+"]
    for nv in no_value:
        found = nv in other_val2
        print(f"  含'{nv}': {found} {'(错误!)' if found else ''}")
else:
    print("未找到other_requirements字段")
