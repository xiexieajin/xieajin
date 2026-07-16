# 列出所有表单字段
import sys
sys.path.insert(0, 'd:/pycharm/供应商寻源系统')
import requests
import re

session = requests.Session()
session.post('http://localhost:5000/login',
             data={'username': 'xieajin', 'password': 'bsq123'},
             allow_redirects=False, timeout=10)

input_text = """我们需要采购一批蓝牙音箱，用于户外露营场景。
产品要求：防水IPX7等级，续航10小时以上，重量不超过500g。
材质ABS塑料，尺寸直径80mm高度100mm。
首批采购500台，需要CE和FCC认证，目标市场欧洲。
要求独立包装每箱20件，需要贴牌OEM印我们公司logo，
提供2年质保，表面磨砂工艺防滑处理。"""

resp = session.post('http://localhost:5000/ai/parse-requirement',
                    data={'input_text': input_text}, timeout=120)
content = resp.text

# 列出所有input和textarea的name和value
print("--- 所有表单字段 ---")
inputs = re.findall(r'<(?:input|textarea)[^>]*name="([^"]+)"[^>]*', content)
print(f"字段名列表: {inputs}")

# 找其他/other相关
print("\n--- 含'其他'或'other'的行 ---")
for line in content.split('\n'):
    if '其他' in line or 'other' in line.lower():
        clean = re.sub(r'\s+', ' ', line.strip())
        if clean:
            print(clean[:200])
