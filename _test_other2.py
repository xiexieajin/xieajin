# 完整测试：其他字段提取+追问页显示
import sys
sys.path.insert(0, 'd:/pycharm/供应商寻源系统')
import requests
import re

session = requests.Session()
session.post('http://localhost:5000/login',
             data={'username': 'xieajin', 'password': 'bsq123'},
             allow_redirects=False, timeout=10)

def extract_field(content, field_name):
    """提取表单字段的值"""
    # textarea
    m = re.search(rf'name="{field_name}"[^>]*>([^<]*)</textarea>', content, re.DOTALL)
    if m:
        return m.group(1).strip()
    # input
    m = re.search(rf'name="{field_name}"[^>]*value="([^"]*)"', content)
    if m:
        return m.group(1).strip()
    return None

# 场景1：纯文本需求（含包装/OEM/工艺/质保等）
print("=" * 60)
print("场景1：纯文本需求（含包装/OEM/工艺/质保等）")
print("=" * 60)
input_text1 = """我们需要采购一批蓝牙音箱，用于户外露营场景。
产品要求：防水IPX7等级，续航10小时以上，重量不超过500g。
材质ABS塑料，尺寸直径80mm高度100mm。
首批采购500台，需要CE和FCC认证，目标市场欧洲。
要求独立包装每箱20件，需要贴牌OEM印我们公司logo，
提供2年质保，表面磨砂工艺防滑处理。"""

resp1 = session.post('http://localhost:5000/ai/parse-requirement',
                     data={'input_text': input_text1}, timeout=120)
print(f"状态码: {resp1.status_code}")

# 检查追问页是否有其他要求字段
has_other = 'other_requirements' in resp1.text
print(f"追问页含'其他要求'字段: {has_other}")

# 提取other_requirements值
other_val = extract_field(resp1.text, 'other_requirements')
if other_val:
    other_val_clean = other_val.replace('&#34;', '"').replace('&#39;', "'").replace('&amp;', '&')
    print(f"\n【其他要求字段值】")
    print(other_val_clean)
    print("\n--- 关键信息检查 ---")
    checks = ["包装", "OEM", "贴牌", "logo", "质保", "工艺", "磨砂", "防滑", "户外", "露营"]
    found = [c for c in checks if c in other_val_clean]
    not_found = [c for c in checks if c not in other_val_clean]
    print(f"  命中: {found}")
    print(f"  未命中: {not_found}")
else:
    print("其他要求字段值为空")

# 也检查其他新增字段
print(f"\n含acceptable_lead_time字段: {'acceptable_lead_time' in resp1.text}")
print(f"含product_aliases字段: {'product_aliases' in resp1.text}")

# 场景2：亚马逊链接
print("\n" + "=" * 60)
print("场景2：亚马逊链接")
print("=" * 60)
input_text2 = "参考`https://www.amazon.com/Muwuele-Overbed-Adjustable-Hospital-Standing/dp/B0DWJ5FY8P/ref=sr_1_1?keywords=muwuele&th=1`"
resp2 = session.post('http://localhost:5000/ai/parse-requirement',
                     data={'input_text': input_text2}, timeout=120)
print(f"状态码: {resp2.status_code}")

other_val2 = extract_field(resp2.text, 'other_requirements')
if other_val2:
    other_val2_clean = other_val2.replace('&#34;', '"').replace('&#39;', "'").replace('&amp;', '&')
    print(f"\n【其他要求字段值】")
    print(other_val2_clean)
    # 检查不应包含的无价值信息
    print("\n--- 无价值信息检查（不应出现）---")
    no_value = ["品牌", "Muwuele", "价格", "HKD", "评分", "297", "销量", "100+", "stars"]
    for nv in no_value:
        found = nv in other_val2_clean
        print(f"  含'{nv}': {found} {'(错误!)' if found else ''}")
else:
    print("其他要求字段值为空")
