# 调试：看返回HTML中other_requirements的实际格式
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
# 找other_requirements相关的内容
idx = content.find('other_requirements')
if idx > 0:
    # 打印周围300字符
    start = max(0, idx - 50)
    end = min(len(content), idx + 400)
    print("--- other_requirements 周围内容 ---")
    print(content[start:end])
else:
    print("未找到other_requirements")
    # 检查是否被重定向到登录页
    print(f"含loginForm: {'loginForm' in content}")
    print(f"含alert: {'alert' in content}")
    # 打印所有alert
    alerts = re.findall(r'<div class="alert[^"]*"[^>]*>(.*?)</div>', content, re.DOTALL)
    for a in alerts:
        clean = re.sub(r'<[^>]+>', '', a).strip()
        print(f"alert: {clean[:200]}")
