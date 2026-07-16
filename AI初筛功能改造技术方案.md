# 供应商寻源系统 - AI初筛功能改造技术方案

参考文档：供应商初筛SKILL说明文档
版本：v1.0
日期：2026-07-15

---

## 1. 文档概述

### 1.1 背景与目标

当前系统的AI初筛功能仅将供应商信息和需求信息发送给大模型，依赖单一prompt让AI自由发挥，未按照供应商初筛SKILL文档中的标准化流程执行。本次改造的目标是：将初筛流程从"黑盒AI判断"升级为"可配置规则驱动 + AI语义辅助"的标准化系统。

### 1.2 改造范围

- 新建规则管理模块（规则的增删改查、模板保存与加载）
- 新建初筛执行引擎（逐任务执行、规则匹配、结果汇总）
- 集成天眼查MCP进行工商信息实时查询
- 重构评分模型（对齐SKILL文档的100分评分体系）
- 新增审计日志系统（完整追踪每步决策过程）
- 改造前端初筛页面（增加规则配置交互）

### 1.3 设计决策确认项

以下设计决策已经用户确认：

- 每次初筛前可修改规则，支持保存规则模板供下次复用
- 新增规则时支持表单配置（小白友好）和JSON表达式（灵活高级）两种方式
- 每次执行时通过天眼查MCP实时查询供应商工商信息
- AI仅用于语义理解场景（如产品与经营范围匹配度判断），注册资本、经营状态等明确规则由代码直接判断
- 保留完整的审计日志表，记录每个供应商每个步骤的执行结果

---

## 2. 当前系统问题分析

### 2.1 现有auto_screening函数的问题

当前ai_helper.py中的auto_screening()函数存在以下不足：

- **单一prompt驱动，缺乏结构化流程**：所有判断依赖一段prompt，AI可能遗漏关键检查项
- **一票否决规则不完整**：当前仅3条（经营状态、近3年败诉、平台下架），SKILL文档要求10条
- **评分模型不匹配**：当前使用知识产权40+资质40+基础20的评分体系，SKILL要求注册资金25+经营年限15+匹配度30+联系方式10+风险15+出口5
- **无天眼查实名核验**：没有对供应商公司名称进行天眼查同名主体复核
- **无联系方式有效性审计**：没有检查电话号码和邮箱格式的有效性
- **规则全部写死在代码中**：用户无法调整阈值或增减规则

### 2.2 现有scoring.py的问题

当前评分模块仅支持人工填写表单后的自动算分，无法对接AI自动初筛的完整流程。规则逻辑与代码耦合过紧，修改评分阈值需要直接改代码。

### 2.3 现有数据库表的问题

screenings表缺少以下关键字段：

- 注册资本（registered_capital）：无法存储工商信息查询结果
- 成立时间（established_date）：无法存储精确日期
- 联系方式审计结果（contact_audit）：无法记录电话/邮箱验证状态
- 天眼查匹配状态（tyc_match_status）：无法追踪同名核验是否通过
- 缺少审计日志表：无法追查每次初筛的决策过程

---

## 3. 整体架构设计

### 3.1 架构对比

改造前：用户点击初筛 → 一个prompt调用DeepSeek → AI自由发挥 → 写回结果

改造后：用户配置规则 → 逐任务执行引擎 → 天眼查实时查询 + 代码规则匹配 + AI语义辅助 → 逐步审计 → 写回结果 → 输出报告

核心变化是将初筛流程从"一次性AI调用"转变为"规则驱动、多数据源、多步骤、可审计"的标准化流程。

### 3.2 模块划分

| 模块名称 | 文件名 | 类型 | 职责说明 |
|----------|--------|------|----------|
| 规则管理模块 | screening_rules.py | 新建 | 规则CRUD、模板保存/加载、规则JSON解析与验证 |
| 初筛执行引擎 | screening_engine.py | 新建 | 编排任务清单、逐条执行规则、汇总结果 |
| 数据查询模块 | screening_data.py | 新建 | 封装天眼查MCP调用、1688数据提取、联系方式验证 |
| 评分计算模块 | scoring.py | 重构 | 基于可配置规则集计算100分评分，替换现有硬编码逻辑 |
| 审计日志模块 | screening_audit.py | 新建 | 记录每次初筛的完整审计轨迹 |
| 初筛路由 | app.py | 修改 | 新增规则管理接口、改造初筛执行接口 |
| AI辅助模块 | ai_helper.py | 修改 | 保留语义匹配相关的AI调用，移除硬编码的初筛prompt |
| 数据库迁移 | db.py | 修改 | 新增规则模板表、规则实例表、审计日志表，扩展screenings表 |

---

## 4. 天眼查数据获取方案（资质与风险数据来源）

天眼查数据通过 **HTTP MCP 协议** 获取（非 tyc CLI）。系统已在 `supplier_search.py` 中实现了 `TianyanchaClient` 类，通过 HTTP 调用天眼查 MCP 服务（`https://mcp.tianyancha.com/v1`），本次改造将扩展该类以覆盖初筛所需的全部工商、风险、资质和知识产权数据。

> **重要架构说明（三步走流程）**：天眼查 MCP 共提供 17 个顶层工具，其中核心数据获取遵循"三步走"流程：
> 1. `search_companies`：用企业名搜索，拿到候选列表和 `company_id`
> 2. `get_company_capabilities`：传入 `company_id`，查询**该企业可用的内部业务工具清单**（每家企业可用工具不同，必须动态发现）
> 3. `call_tool` / `call_tools_batch`：用 capabilities 返回的**真实 tool_name**调用具体维度数据（tool_name 必须逐字复制，不能猜测）
>
> **关键约束**：不能硬编码 tool_name（如 `risk_overview`），必须先查 capabilities 拿到真实工具名（如 `get_risk_overview`）再调用。不同企业的可用工具集可能不同。

### 4.1 基础工商信息查询

用于一票否决规则（注册资本、经营状态、成立时间）和评分（注册资本与企业规模、经营状态与成立年限）：

| MCP工具 | 返回的关键字段 |
|---------|--------------|
| `search_companies`（query=企业名） | 候选企业列表，含 company_id、企业名称、统一社会信用代码 |
| `get_company_basic_profile`（company_name） | 企业基础画像：注册资本、成立日期、经营状态、经营范围、联系方式（电话/邮箱）、地址、法定代表人、企业类型、人员规模 |

> 说明：基础工商信息通过 `get_company_basic_profile` 一次性获取，无需多次调用。现有代码已实现此方法（supplier_search.py），本次复用。

### 4.2 风险信息查询

注：以下风险信息全部通过天眼查MCP获取。HTTP MCP 未提供经营异常/严重违法失信/失信被执行人的独立工具，这些细分维度需从风险总览中解析。

用于一票否决规则（经营异常、严重违法失信、失信被执行人）和评分（风险记录15分）：

| MCP工具 | 数据用途 | 说明 |
|---------|---------|------|
| `get_risk_overview`（call_tool） | 风险总览：企业自身/周边/预警风险信息 | 从返回文本中解析经营异常、失信、违法等细分维度 |
| `get_judicial_case`（call_tool） | 司法案件：检查近3年是否有知识产权侵权败诉（一票否决项） | 部分公司可用，需先查capabilities确认 |
| `get_shell_company_check`（call_tool） | 空壳公司识别：综合分析异常特征 | 加分项，辅助判断企业真实性 |

> **风险细分维度处理方式**：HTTP MCP 没有独立的 business-exception / serious-violation / dishonest-info 工具。执行引擎将从 `get_risk_overview` 返回的风险总览文本中提取关键字（如"经营异常""严重违法""失信""被执行"等）来判断一票否决项。若风险总览文本无法明确判断，则标记为"需人工确认"。

### 4.3 资质与许可信息查询

注：以下资质与许可信息全部通过天眼查MCP获取。

用于评分（出口经验、平台经验或资质5分）和资质核实：

| MCP工具 | 数据用途 | 说明 |
|---------|---------|------|
| `get_qualifications`（call_tool） | 资质证书：企业资质信息（证书类型、等级、有效期、状态），含 ISO/CCC/CE/FCC/RoHS 等 | 需先查capabilities确认可用 |
| `get_company_basic_profile` | 经营范围、企业类型：辅助判断是否有进出口/制造能力 | 已有方法，复用 |

> 说明：HTTP MCP 未提供独立的行政许可、招投标、产品信息、招聘信息、供应商客户等工具。出口经验/平台经验判断将结合资质证书（get_qualifications）+ 经营范围（basic_profile）+ AI语义分析综合判断。

### 4.4 知识产权信息查询

注：以下知识产权信息通过天眼查MCP的跨公司搜索工具获取。

用于风险排查（商标/专利风险判断）和资质核实（自有知识产权情况）：

| MCP工具 | 数据用途 | 说明 |
|---------|---------|------|
| `search_trademarks`（query=企业名） | 商标信息：跨公司搜索该企业的商标，判断是否存在品牌侵权风险 | 顶层工具，直接调用 |
| `search_patents`（query=企业名） | 专利信息：跨公司搜索该企业的专利，判断技术实力和制造能力 | 顶层工具，直接调用 |

> 说明：HTTP MCP 未提供独立的 ipr-score（创新力评分）、software-copyright-info（软著）工具。知识产权实力将通过商标数量+专利数量综合评估。

### 4.5 天眼查无法覆盖的数据及处理方式

以下初筛所需数据**无法通过天眼查获取**，需通过替代方案处理：

| 数据需求 | 对应的规则 | 天眼查状态 | 替代方案 |
|----------|-----------|----------|----------|
| 平台侵权下架记录 | veto_platform_infringe（一票否决） | 不支持 | 通过互联网搜索（DuckDuckGo/搜索引擎）检索亚马逊、Temu等跨境电商平台的侵权投诉、下架公告、论坛投诉信息，由AI综合分析后判断。标注为"互联网来源，可信度需人工复核" |
| 1688店铺联系方式 | 联系方式补查 | 不支持 | 从1688供应商发现阶段已抓取的数据中提取（当前系统已有1688数据源） |

### 4.6 联系方式查询与补查优先级

用于联系方式有效性审计（一票否决项和联系方式完整度评分10分）：

| MCP工具 | 数据用途 |
|---------|---------|
| `get_company_basic_profile`（company_name） | 联系方式：企业公开的电话和邮箱，作为联系方式补查的第一优先级数据源 |

当飞书多维表格中供应商的联系方式为空时，按以下优先级进行补查：

1. 天眼查 `get_company_basic_profile`（企业公开联系方式，从基础画像中提取电话/邮箱）
2. 1688店铺联系方式页（从供应商来源数据中提取）
3. 官网、ICP备案、B2B平台（通过互联网搜索获取，需标注"互联网来源，可信度较低"）

仅天眼查来源和1688来源的联系方式可直接采纳；互联网来源需人工确认。

---

## 5. 规则系统设计

### 5.1 规则类型

每条规则属于以下三种类型之一：

- **一票否决（veto）**：触发后该供应商直接淘汰，不进入评分
- **评分项（score）**：计算得分，有满分值和评分逻辑
- **检查项（check）**：仅做信息记录，不直接影响通过/否决（如联系方式补查）

### 5.2 默认规则集（来自SKILL文档）

#### 一票否决规则

| 规则编码 | 规则描述 | 用户可修改 | 判断方式 | 数据来源 |
|----------|---------|----------|---------|----------|
| veto_capital | 注册资本 < 100万人民币 | 可改阈值 | 代码 | 天眼查 |
| veto_operating_status | 登记/经营状态不是存续、在业或正常 | 不可改 | 代码 | 天眼查 |
| veto_abnormal | 当前处于经营异常 | 不可改 | 代码 | 天眼查 |
| veto_dishonest | 当前严重违法失信 | 不可改 | 代码 | 天眼查 |
| veto_no_contact | 无有效电话或邮箱 | 可开关 | 代码 | 天眼查+1688 |
| veto_non_manufacturer | 非制造商且无制造能力证据 | 可开关 | AI+代码 | 天眼查+AI |
| veto_product_mismatch | 产品或经营范围明显不匹配 | 可开关 | AI | AI语义判断 |
| veto_faithless_person | 当前为失信被执行人 | 不可改 | 代码 | 天眼查 |
| veto_ip_lawsuit | 近3年有明确知识产权侵权败诉 | 不可改 | 代码 | 天眼查 |
| veto_platform_infringe | 有明确平台侵权下架记录 | 不可改 | 代码+AI | 互联网搜索 |
| veto_capital_unknown | 注册资本未披露 | 不可改 | 代码 | 天眼查（进入人工确认） |

> 关于 veto_platform_infringe（平台侵权下架记录）：该数据无法从天眼查获取。执行引擎将通过DuckDuckGo等搜索引擎检索亚马逊、Temu等跨境电商平台的侵权投诉和下架公告，由AI综合分析判断。由于互联网来源的可靠性不如天眼查，该规则的判断结果默认标注为"需人工复核"，不作为自动否决的充分条件，而是将检索到的证据提交给用户确认后再做决定。

#### 100分评分规则

| 规则编码 | 评分维度 | 满分 | 用户可改 | 判断方式 | 数据来源 |
|----------|---------|------|----------|---------|----------|
| score_capital_scale | 注册资本与企业规模 | 25 | 分值和阈值 | 代码 | 天眼查 |
| score_operating_years | 经营状态与成立年限 | 15 | 分值和阈值 | 代码 | 天眼查 |
| score_product_match | 产品与经营范围匹配 | 30 | 分值和阈值 | AI | AI语义判断 |
| score_contact_complete | 联系方式完整度 | 10 | 分值 | 代码 | 天眼查+1688 |
| score_risk_record | 风险记录 | 15 | 分值和阈值 | 代码 | 天眼查 |
| score_export_exp | 出口经验、平台经验或资质 | 5 | 分值 | 代码+AI | 天眼查+AI |

#### 评分结论阈值（可配置）

| 条件 | 结论 | 说明 |
|------|------|------|
| 触发一票否决 | 未通过初筛 | 不受分数影响，直接淘汰 |
| 总分 ≥ 75 | 已通过初筛 | 默认阈值75，用户可在规则配置页修改 |
| 60 ≤ 总分 ≤ 74 | 需要人工确认 | 默认区间60-74，用户可在规则配置页修改 |
| 总分 < 60 | 未通过初筛 | 默认阈值60，用户可在规则配置页修改 |

---

## 6. 数据库设计

### 6.1 规则模板表 screening_rule_templates

存储系统默认规则模板，系统首次启动通过db.py自动初始化。

| 字段名 | 类型 | 说明 |
|--------|------|------|
| id | INTEGER PK | 自增主键 |
| rule_code | TEXT UNIQUE | 规则编码，如 veto_capital、score_capital_scale |
| rule_name | TEXT | 规则显示名称，如"注册资本一票否决" |
| rule_type | TEXT | 规则类型：veto / score / check |
| rule_category | TEXT | 分类：basic_info / contact / risk / qualification / match / export |
| default_condition | TEXT(JSON) | 默认条件表达式，如 {"field":"reg_capital_wan","operator":"lt","value":100} |
| default_action | TEXT(JSON) | 默认动作，如 {"result":"veto","reason":"注册资本不足100万"} |
| max_score | INTEGER | 评分项满分值（veto类型为NULL） |
| scoring_logic | TEXT | 评分逻辑描述，如"1000万以上满分，100-1000按比例，100以下0分" |
| tyc_commands | TEXT(JSON) | 本规则需要的数据及其天眼查命令 |
| is_configurable | INTEGER | 是否允许用户修改（0=不可改/1=可改） |
| is_enabled | INTEGER | 默认是否启用 |
| sort_order | INTEGER | 排序权重 |
| description | TEXT | 规则详细说明 |

### 6.2 用户规则实例表 screening_rule_instances

每次初筛时，用户可能修改规则。修改后的规则保存为"实例"，记录本次初筛实际使用的规则配置。

| 字段名 | 类型 | 说明 |
|--------|------|------|
| id | INTEGER PK | 自增主键 |
| requirement_id | INTEGER | 关联需求ID（NULL表示保存为模板） |
| template_id | INTEGER | 来源规则模板ID（关联screening_rule_templates） |
| template_name | TEXT | 模板名称（用户自定义，如"严格模式""宽松模式"） |
| custom_condition | TEXT(JSON) | 用户自定义的条件（覆盖默认），NULL表示使用默认 |
| custom_score_cap | INTEGER | 用户自定义的满分值，NULL表示使用默认 |
| is_enabled | INTEGER | 本次是否启用 |
| updated_at | TEXT | 更新时间 |
| user_id | INTEGER | 所属用户ID（数据隔离） |

### 6.3 审计日志表 screening_audit_logs

每次初筛执行时，记录每个供应商每个任务的执行过程和结果，实现完整的可追溯性。

| 字段名 | 类型 | 说明 |
|--------|------|------|
| id | INTEGER PK | 自增主键 |
| run_id | TEXT | 本次运行批次ID（UUID），用于关联同一次初筛的所有记录 |
| supplier_id | INTEGER | 供应商ID |
| task_code | TEXT | 任务编码，如 tyc_registration_check / contact_audit / veto_capital |
| task_name | TEXT | 任务名称，如"天眼查主体复核" |
| input_data | TEXT(JSON) | 输入数据快照 |
| result_data | TEXT(JSON) | 执行结果 |
| evidence | TEXT | 证据来源说明 |
| status | TEXT | 状态：success / fail / skip / uncertain |
| created_at | TEXT | 创建时间 |
| user_id | INTEGER | 所属用户ID（数据隔离） |

### 6.4 screenings表扩展

现有screenings表需扩展以下字段：

| 新增字段 | 类型 | 说明 |
|----------|------|------|
| tyc_match_status | TEXT | 天眼查同名匹配状态：exact_match / partial_match / not_found / multiple_candidates |
| tyc_company_name | TEXT | 天眼查匹配到的企业全称 |
| registered_capital | REAL | 注册资本（万元） |
| established_date | TEXT | 成立日期 |
| operating_status | TEXT | 经营状态（从天眼查获取） |
| business_scope | TEXT | 经营范围（从天眼查获取） |
| contact_audit_result | TEXT | 联系方式审计结果：valid / partial / invalid / not_available |
| run_id | TEXT | 关联审计日志的批次ID |

---

## 7. 规则条件JSON格式规范

规则条件使用结构化JSON表示，支持简单条件和复合条件。系统提供两种编辑方式：表单编辑（供小白用户使用）和JSON编辑器（供高级用户使用）。

### 7.1 简单条件（单字段比较）

```json
{
  "type": "single",
  "field": "reg_capital_wan",
  "operator": "lt",
  "value": 100,
  "unit": "万元"
}
```

### 7.2 复合条件（多条件组合）

```json
{
  "type": "composite",
  "logic": "and",
  "conditions": [
    { "type": "single", "field": "operating_status", "operator": "neq", "value": "存续" },
    { "type": "single", "field": "operating_status", "operator": "neq", "value": "在业" }
  ]
}
```

### 7.3 支持的操作符

| 操作符 | 含义 |
|--------|------|
| lt | 小于 |
| lte | 小于等于 |
| gt | 大于 |
| gte | 大于等于 |
| eq | 等于 |
| neq | 不等于 |
| in | 值在列表中 |
| not_in | 值不在列表中 |
| contains | 包含子串 |
| not_contains | 不包含子串 |
| is_null | 字段为空或未返回 |
| is_not_null | 字段有值 |

---

## 8. 核心执行流程

### 8.1 初筛执行引擎工作流

初筛执行引擎（screening_engine.py）负责编排以下任务（对齐SKILL文档第6节）：

| 序号 | 任务名称 | 数据来源 | 说明 |
|------|----------|----------|------|
| 1 | 加载初筛规则 | 数据库 | 从screening_rule_instances读取本次规则集 |
| 2 | 读取供应商快照 | 数据库 | 读取需求组下所有待初筛供应商的当前字段 |
| 3 | 建立审计账本 | 本地 | 创建run_id，初始化审计日志 |
| 4 | 天眼查同名主体复核 | 天眼查 | 对每个供应商用tyc company registration-info做同名匹配 |
| 5 | 审计现有联系方式 | 数据库+校验 | 检查供应商表中已有电话/邮箱格式有效性 |
| 6 | 补查缺失联系方式 | 天眼查+1688 | 按优先级依次从天眼查、1688、互联网补查 |
| 7 | 判断联系方式可信度 | 代码 | 天眼查来源可信度高，互联网来源标注需确认 |
| 8 | 判断注册资本 | 天眼查+规则 | 匹配一票否决规则veto_capital |
| 9 | 判断经营状态与异常 | 天眼查+规则 | 匹配veto_operating_status和veto_abnormal |
| 10 | 判断成立时间 | 天眼查+规则 | 不直接淘汰，仅作为风险提示和扣分项 |
| 11 | 判断产品匹配度 | AI | 由AI对比需求产品名称与供应商经营范围/主营产品 |
| 12 | 查询风险记录 | 天眼查 | 逐维度查询经营异常、失信、违法、诉讼等 |
| 13 | 查询出口/平台/资质 | 天眼查 | 查询资质证书、行政许可、招投标、知识产权 |
| 14 | 执行一票否决断言 | 规则引擎 | 逐一匹配所有启用的veto规则 |
| 15 | 执行100分评分 | 规则引擎+AI | 对未否决的供应商，逐条匹配score规则 |
| 16 | 生成初筛结论 | 汇总 | 合并一票否决结果、评分结果、结论 |
| 17 | 执行写入前断言 | 校验 | 检查所有供应商都有结论、否决/通过/人工确认分类正确 |
| 18 | 写回多维表格 | 数据库 | 更新screenings表和suppliers表的dev_stage |
| 19 | 回读复核 | 数据库 | 重新读取并核验写入结果 |
| 20 | 输出执行报告 | 汇总 | 生成包含通过/否决/待确认数量的报告 |

### 8.2 AI的使用边界

AI（DeepSeek）仅在以下场景中参与判断，其他所有判断均由代码直接完成：

- **产品与经营范围匹配度判断**（score_product_match评分项30分）：需要语义理解，判断供应商的经营范围/主营产品是否与采购需求匹配
- **制造能力判断**（veto_non_manufacturer一票否决项）：需要综合分析供应商的注册类型、经营范围、资质、招聘信息等，判断是否为制造商
- **平台侵权下架记录分析**（veto_platform_infringe一票否决项）：需要理解互联网搜索结果中平台投诉和侵权记录的文本内容。此项数据天眼查无法提供，需通过搜索引擎获取后由AI分析
- **资质证书分类和评价**（score_export_exp评分项5分）：需要识别天眼查返回的资质列表中哪些与出口/目标市场相关

### 8.3 方案B：资本硬否决优先

引擎内置优化逻辑：如果供应商已命中"注册资本<100万"一票否决，且同时没有有效联系方式，则直接判定为"未通过初筛"，跳过后续的互联网联系方式补查和AI匹配度判断，减少低价值供应商的处理成本。

---

## 9. 前端界面设计

### 9.1 改造范围

- 新增页面：`templates/screening/rule_config.html`（规则配置页）
- 改造页面：`templates/ai/auto_screening.html`（增加规则预览和修改入口）
- 新增路由：规则CRUD接口、模板保存/加载接口

### 9.2 规则配置页交互设计

页面分为三个区域：

**一票否决规则区**：以卡片列表展示所有veto类型规则，每条规则卡片显示规则名、条件（可点击编辑）、启用/禁用开关。可修改的规则显示编辑按钮，不可改的规则显示锁定图标。底部有"新增一票否决规则"按钮。

**评分规则区**：以卡片列表展示所有score类型规则，每条规则卡片显示规则名、满分值和评分逻辑（可点击修改分值）、启用/禁用开关。底部有"新增评分规则"按钮。

**通过标准区**：输入框设置≥XX分通过、XX-YY分人工确认、<XX分未通过的阈值。

**操作按钮区**："恢复默认配置""保存为模板""从模板加载""开始初筛"四个按钮。

### 9.3 规则编辑弹窗

新增规则时弹出表单，支持两种模式：

- **表单模式（默认）**：下拉选择数据字段 → 下拉选择操作符 → 输入比较值 → 输入否决原因/评分逻辑描述
- **JSON模式（切换）**：直接编辑规则的JSON条件表达式，实时预览解析结果

### 9.4 自动初筛确认页改造

在现有的确认页面（auto_screening.html）中增加：

- 本次使用的规则预览表（列出所有启用的一票否决规则和评分规则）
- "修改规则"按钮，点击跳转到规则配置页
- "选择模板"下拉框，可选择之前保存的规则模板
- 保留现有的待初筛数量显示和开始按钮

---

## 10. 技术实现要点

### 10.1 天眼查MCP调用方式

screening_data.py 中通过 HTTP 调用天眼查 MCP 服务（复用并扩展现有 `TianyanchaClient` 类，位于 supplier_search.py）。**不使用 tyc CLI 和 subprocess**。

调用架构（三步走流程）：

```
1. search_companies(企业名) → 拿到 company_id 和企业全称
2. get_company_capabilities(company_id) → 拿到该企业可用的内部业务工具清单（真实tool_name）
3. call_tool(company_name, tool_name, arguments) → 调用具体维度获取数据
   或 call_tools_batch(company_name, calls[]) → 批量调用（每批最多3个独立维度）
```

前置检查：

```python
# 启动时检查天眼查MCP配置是否就位（从数据库读取）
from model_config import get_provider
provider = get_provider("tianyancha")
if not provider or not provider.get("base_url") or not provider.get("api_key"):
    raise Exception("天眼查MCP未配置，请在管理中心配置tianyancha服务商的base_url和api_key")
```

安全注意事项：

- 天眼查MCP的授权码存储在数据库 ai_providers 表中（provider_code=tianyancha），不暴露给前端
- 企业名称作为 JSON 参数传入 HTTP 请求体，天然防止 shell 注入（不涉及 shell 执行）
- 使用 requests.post() 发送 JSON 请求，timeout 设置为 30-60 秒
- call_tools_batch 每批最多 3 个工具，避免单次请求过载
- 同一企业的同一工具默认只调用一次，避免重复请求触发频率限制
- 天眼查调用之间加适当延迟（建议 0.5-1 秒），避免触发频率限制

### 10.2 异步执行与进度反馈

初筛执行是一个长任务（N个供应商 × 多个天眼查查询 + AI调用），需要考虑以下几点：

- 使用后台任务队列，避免HTTP请求超时
- 前端使用轮询或WebSocket获取实时进度（当前处理第X家供应商、当前任务名称）
- 天眼查调用之间加适当的延迟（建议0.5-1秒），避免触发频率限制
- 每个供应商的初筛为独立任务，失败不影响其他供应商

### 10.3 错误处理策略

- **天眼查查询失败**：标记为uncertain状态，该供应商进入"需要人工确认"
- **天眼查返回多个候选**：记录所有候选供用户选择，暂停该供应商的处理
- **AI调用失败**：标记为skip，用规则引擎的剩余规则继续判断，但评分中的AI依赖项（产品匹配度30分）记为0分
- **联系人补查全部失败**：如果veto_no_contact规则启用，则触发一票否决

### 10.4 规则模板管理

- 用户保存的模板存储在screening_rule_instances表中（requirement_id=NULL为模板）
- 模板是独立于需求的，同一用户的所有需求都可以加载已保存的模板
- 系统默认规则不可删除，但用户可以创建衍生模板（修改默认规则的参数后保存为新模板）
- 加载模板时，模板中的所有规则实例覆盖当前配置

---

## 11. 文件改动清单

| 文件路径 | 操作 | 说明 |
|----------|------|------|
| screening_rules.py | 新建 | 规则管理模块：规则CRUD、条件JSON解析与验证、规则模板保存/加载 |
| screening_engine.py | 新建 | 初筛执行引擎：编排任务清单、逐供应商逐规则执行、结果汇总 |
| screening_data.py | 新建 | 数据查询模块：封装tyc CLI调用、联系方式验证、1688数据提取、互联网搜索 |
| screening_audit.py | 新建 | 审计日志模块：创建run_id、记录每步执行结果、生成审计报告 |
| scoring.py | 重构 | 评分计算模块：从硬编码改为基于规则配置动态计算；保留人工表单评分兼容 |
| ai_helper.py | 修改 | 移除auto_screening()的硬编码prompt，保留AI语义判断功能作为引擎的子调用 |
| app.py | 修改 | 新增规则管理路由（/screening/rules/*）、改造初筛路由、新增模板路由 |
| db.py | 修改 | 新增3张表（规则模板、规则实例、审计日志）、扩展screenings表字段 |
| templates/screening/rule_config.html | 新建 | 规则配置页面 |
| templates/ai/auto_screening.html | 修改 | 增加规则预览和修改入口 |

---

## 12. 数据来源汇总

| 数据类别 | 主要来源 | 覆盖情况 |
|----------|----------|----------|
| 注册资本 | 天眼查 tyc company registration-info | 完整覆盖 |
| 经营状态 | 天眼查 tyc company registration-info | 完整覆盖 |
| 成立日期 | 天眼查 tyc company registration-info | 完整覆盖 |
| 经营范围 | 天眼查 tyc company registration-info | 完整覆盖 |
| 经营异常 | 天眼查 tyc risk business-exception | 完整覆盖 |
| 严重违法失信 | 天眼查 tyc risk serious-violation | 完整覆盖 |
| 失信被执行人 | 天眼查 tyc risk dishonest-info | 完整覆盖 |
| 司法诉讼 | 天眼查 tyc risk judicial-documents / judicial-case | 完整覆盖 |
| 行政处罚 | 天眼查 tyc risk administrative-penalty | 完整覆盖 |
| 资质证书 | 天眼查 tyc operation qualifications | 完整覆盖 |
| 进出口经营权 | 天眼查 tyc operation administrative-license | 完整覆盖 |
| 商标/专利 | 天眼查 tyc intellectual_property trademark-info / patent-info / ipr-score | 完整覆盖 |
| 联系方式 | 天眼查 tyc company contact-info + 1688店铺数据 | 完整覆盖 |
| 平台侵权下架记录 | 互联网搜索（DuckDuckGo等） | 部分覆盖（需人工复核） |

---

以上为AI初筛功能改造的完整技术方案。方案的核心思想是"规则驱动、数据透明、用户可控"：将初筛规则从代码中解耦，让用户每次使用时都能灵活调整；通过天眼查MCP实时获取供应商基础信息、风险和资质数据（覆盖除平台侵权下架记录外的全部数据需求）；用审计日志完整记录每一步决策过程，确保初筛结果可追溯、可解释。
