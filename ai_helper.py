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
from model_config import get_model_config, get_provider


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

    # 第三步：合并用户补充的信息（追问第二轮用）
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
- other_requirements 其他要求（无法归类到上述字段的额外要求，如包装方式、特殊工艺、售后服务等）

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
    "other_requirements": "其他要求（选填，无则空）",
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
