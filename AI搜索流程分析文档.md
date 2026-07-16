# 供应商寻源系统 — AI搜索主流程分析

> 分析时间: 2026-07-16

---

## 一、整体架构概览

整个 AI 搜索流程涉及 **5 个核心文件** 和 **3 个外部服务**：

| 核心文件 | 职责 |
|---------|------|
| `app.py` | Flask路由层，接收前端请求，启动后台搜索线程，用 SSE 推送进度 |
| `supplier_search.py` | **核心**，实现完整搜索流程：1688搜索、MIC搜索、提取公司名、去重、预筛、天眼查补全、DeepSeek 过滤 |
| `ai_helper.py` | 封装 DeepSeek 和智谱的底层 API 调用（`call_deepseek`） |
| `model_config.py` | 从数据库加载 AI 配置到内存（服务商、模型参数、搜索平台启停），支持热更新 |
| `db.py` | 数据库 Schema，定义了 `search_platforms` 表管理平台的启用/禁用 |

| 外部服务 | 用途 |
|---------|------|
| **1688 API** (`skills-gateway.1688.com`) | 供应商搜索，HMAC-SHA256签名认证 |
| **中国制造网 MCP** (`mcp.chexb.com/sse`) | 供应商搜索，SSE长连接 + JSON-RPC 协议 |
| **天眼查 MCP** | 补全工商信息（注册资本、地址、电话、邮箱等） |
| **DeepSeek** | AI过滤、判断供应商类型、生成简介、公司名翻译 |

---

## 二、主流程全景图

下面是从用户点"开始AI搜索"到结果写入数据库的完整流程：

```
┌─────────────────────────────────────────────────────────────┐
│  前端 POST 到 /ai/search-suppliers/<req_id>                   │
│  ├─ 创建 progress_queue → progress_callback → SSE推送给前端   │
│  └─ 启动后台线程 run_search_thread()                          │
└──────────────────────────┬──────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  search_suppliers(keywords_json, product_name, callback)      │
│  （supplier_search.py 第1784行）                               │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ 第1步：解析P0-P3关键词矩阵（7组，每组含中/英文）       │   │
│  │  → search_terms = [(级别, 中文词, 标签, 变体), ...]   │   │
│  │  → 共14个搜索词（7个中文+7个英文）                     │   │
│  └──────────────────┬───────────────────────────────────┘   │
│                     ▼                                        │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ 第2步：并发搜索14个关键词（最大3并发）                  │   │
│  │  ├── 从 DB 读取启用的搜索平台 (enabled_codes)          │   │
│  │  ├── 1688启用 → crawl_1688(关键词, 标签, 变体)        │   │
│  │  └── MIC启用  → crawl_made_in_china(关键词, 标签)     │   │
│  └──────────────────┬───────────────────────────────────┘   │
│                     ▼                                        │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ 第3步：提取公司名 + 跨平台去重 + 程序化预筛             │   │
│  │  ├── extract_company_names()     提取公司名            │   │
│  │  ├── _dedup_cross_platform()     核心字号去重          │   │
│  │  └── _programmatic_prefilter()   正则预筛（零token成本） │   │
│  └──────────────────┬───────────────────────────────────┘   │
│                     ▼                                        │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ 第4步：天眼查 MCP 并发补全工商信息（最大5线程）         │   │
│  │  ├── search_companies(公司名)    精确/模糊匹配         │   │
│  │  ├── get_company_basic_profile() 获取详情              │   │
│  │  └── MIC来源未匹配的公司 → 标记丢弃                     │   │
│  └──────────────────┬───────────────────────────────────┘   │
│                     ▼                                        │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ 第5步：DeepSeek 精细过滤（filter_suppliers_with_ai）  │   │
│  │  ├── 每50家为一批                                      │   │
│  │  ├── 最大8线程并发                                     │   │
│  │  ├── 返回：supplier_type / main_product / intro       │   │
│  │  └── 过滤失败兜底：保留原数据，标记"疑似制造商"        │   │
│  └──────────────────┬───────────────────────────────────┘   │
│                     ▼                                        │
│  返回 suppliers[] → app.py 批量写入 suppliers 表             │
└──────────────────────────────────────────────────────────────┘
```

---

## 三、1688 搜索流程详解

### 3.1 架构与认证

- **API 网关地址**: `https://skills-gateway.1688.com`
- **接口**: `/api/1688_source_suppliers/1.0.0`
- **认证方式**: HMAC-SHA256 签名（AK从数据库 `ai_providers` 表的 `ali1688` 行读取）
- **每页返回**: 约10家工厂
- **目标数量**: 每个关键词凑够 50 家

### 3.2 签名流程（`_build_1688_sign_headers`，第567行）

```
① AK解码：base64解码 → 前32字符=Secret，后面=AccessKeyID
② 算请求体 MD5（内容指纹）
③ 拼签名头：x-csk-ak, x-csk-time, x-csk-nonce, x-csk-content-md5, x-csk-version
④ 按 key 排序拼成规范格式（每行 "小写key:值"）
⑤ 待签名字符串 = "POST\nMD5\napplication/json\n时间戳\n规范头\nURI"
⑥ HMAC-SHA256(Secret, 待签名字符串) → base64 → 最终签名
```

### 3.3 搜索逻辑（`crawl_1688`，第873行）

```
① 用原始关键词搜索一次 → 获得约10家
② 按公司名去重（seen_names 集合）
③ 如果不到 50 家，用 AI 预生成的"变体关键词"逐个搜索
   比如：主词="电视柜"，变体=["电视柜批发","玻璃电视柜","简约电视柜"...]
④ 每次调用间隔1秒，避免触发限流
⑤ 变体用完仍不够 50 家 → 返回实际数量
```

### 3.4 数据提取（`_extract_1688_factories`，第740行）

从 API 响应中解析 `originResponses → currentPhase="RETRIEVAL" → responseData.data`，提取:

| 字段 | 说明 |
|------|------|
| `companyName` | 公司名称 |
| `oem_mode` | 合作方式（JSON数组，如 `["OEM","ODM"]`） |
| `manufacture_type` | 服务能力（JSON数组，如 `["来样加工","来图加工"]`） |
| `extInfos` | 扩展信息（地区、工厂等级、满意度、月订单量、是否支持打样等） |

**数据质量过滤**: 必须同时有 `oem_mode` 和 `manufacture_type` 才保留。

---

## 四、中国制造网（MIC）搜索流程详解

### 4.1 架构与通信

- **MCP 服务地址**: `https://mcp.chexb.com/sse`
- **通信方式**: **MCP over SSE**（Server-Sent Events 长连接 + JSON-RPC）
- **每页返回**: 10条
- **最大翻页**: 10页（凑够 100 家）
- **仅搜英文关键词**: 中文关键词直接跳过（MIC 是国际站）

### 4.2 SSE 通信机制（`_MicMcpClient`，第69行）

```
① GET /sse → 建立 SSE 长连接 → 从首个 data: 行获取 session 路径
② 后台线程持续读取 SSE 流，把 JSON-RPC 响应放入队列
③ POST 到 session 路径 → 发送 JSON-RPC 请求（返回 202 Accepted）
④ 实际响应通过 SSE 流的 data: 行返回
⑤ 用 call_id 匹配请求和响应
```

### 4.3 搜索逻辑（`crawl_made_in_china_mcp`，第342行）

```
① 判断关键词是否含英文字母
   - 不含 → 中文词，直接返回空（MIC 不搜中文）
   - 含   → 英文词，继续搜索

② 建立 MCP 连接 → initialize → session 就绪

③ 翻页搜索（最多10页，总超时60秒）：
   - 调用 search_suppliers(keyword, page)
   - 每页返回10条
   - 按公司名去重
   - 凑够100家或没数据就停止
   - 限流(429) → 等8秒重新连接重试一次

④ 提取公司信息：
   - name / companyName / supplierName
   - business_type（Manufacturer/Trading Company）
   - main_products（主营产品）
   - location（地区）
   - badges（认证徽章）
```

### 4.4 公司名翻译（`_translate_company_names_batch`，第250行）

MIC 返回的供应商名是英文的，需要翻成中文：

```
① 找出所有含英文字母的公司名 → 去重
② 每批20家发给 DeepSeek 翻译
③ 返回 JSON: {"translations": [{"original":"...", "chinese":"..."}]}
④ 原文更新为 "中文名（English Name）" 格式
```

---

## 五、企业信息查询（天眼查 MCP）

### 5.1 架构（`TianyanchaClient`，第947行）

- **MCP 地址和授权码**: 从数据库 `ai_providers` 表的 `tianyancha` 行读取
- **协议**: MCP JSON-RPC 2.0，使用 `Mcp-Session-Id` 维持会话
- **两个核心工具**:
  - `search_companies(query)` — 搜索企业
  - `get_company_basic_profile(company_name)` — 获取工商详情

### 5.2 查询流程（`_enrich_one_supplier`，第1966行）

```
① 用公司名调用 search_companies → 获取候选列表（Markdown表格）

② 匹配策略（双重降级）：
   a. 精确同名匹配（优先）
   b. difflib.SequenceMatcher 相似度 ≥ 0.6 的模糊匹配

③ 匹配成功 → 调用 get_company_basic_profile → 获取详细信息：
   - registered_capital（注册资本）
   - phone / email（联系方式）
   - address（注册地址）
   - establish_date（成立日期）→ 计算成立年限
   - intro / business_scope（工商简介/经营范围）
   - operating_status（经营状态）
   - legal_person（法定代表人）

④ MIC 特殊处理：
   - 匹配成功 → 用天眼查返回的中文名替换英文名
   - 匹配失败 → 标记 _tyc_not_found=True，后续丢弃
     （英文名查不到国内企业，无保留意义）

⑤ 电话号码校验：
   只接受手机号 ^1[3-9]\d{9}$ 或座机 ^0\d{2,3}-?\d{7,8}$
   手机和座机都有时优先取手机
   "正常电话"标签的优先
```

### 5.3 并发策略

- 每个线程创建独立的天眼查 Client（保证线程安全）
- 最多 **5 个线程** 并发（避免天眼查限流）
- 每个任务 30 秒超时
- 每完成 3 家更新一次前端进度

---

## 六、过滤机制（三层过滤）

系统使用 **三层递进过滤**，从快到慢、从粗到精：

### 6.1 第一层：程序化预筛（`_programmatic_prefilter`，第1459行）

**零 token 成本**，纯正则和平台字段判断。

| 规则 | 逻辑 |
|------|------|
| **规则1** | MCP 的 `business_type == "Manufacturer"` → 直接保留（最可靠） |
| **规则2** | 公司名含贸易类关键词（贸易/商贸/商行/电子商务/经营部/零售/供应链/进出口），且来源信息没有制造证据 → 剔除 |
| **规则3** | 来源信息全是配件/材料词（配件/紧固件/零件/部件/模具/夹具/治具/原材料），且没有制造证据，且 `source_text < 50字符` → 剔除 |
| **默认** | 其他情况一律保留（宁可多留，不漏放） |

### 6.2 第二层：天眼查工商校验

- 经营状态异常的（如"吊销""注销"）在匹配阶段就被标记
- MIC 来源且天眼查未匹配的公司 → 直接丢弃（英文名查不到国内企业）

### 6.3 第三层：DeepSeek 精细过滤（`filter_suppliers_with_ai`，第1673行）

**每批 50 家**，**最大 8 线程**并发。

#### DeepSeek 判断规则（`_filter_one_batch`，第1512行）：

```
1. business_type为"Manufacturer"的直接保留
2. business_type为"Trading Company"的剔除（除非来源信息证明有制造能力）
3. 严格剔除：
   - 贸易公司（商贸/商行/电子商务/经营部/零售/供应链）
   - 配件类（柜脚/五金配件/紧固件/零件/部件）
   - 材料类（岩板/玻璃板/板材/原料/毛坯）
   - 加工服务（来图加工/切割/定制加工）
   - 模具/夹具/治具
   - 装饰品/摆件
4. 主营产品与采购产品不相关 → 剔除
5. 不确定的 → 保留并标记为"疑似制造商"（最重要原则）
6. 不编造新公司，只从候选列表中选
```

#### 输出字段：

| 字段 | 说明 |
|------|------|
| `supplier_type` | "制造商" 或 "疑似制造商" |
| `main_product` | DeepSeek 推断的主营产品 |
| `intro` | 供应商简介 + 注册资本（从天眼查追加） |
| `has_cross_border_exp` | 是否有跨境电商经验 |

#### 容错兜底：

- 首次失败 → 等 1 秒重试一次
- 重试仍失败 → **不丢弃数据**，原始公司保留为"疑似制造商"，标记 `filter_failed=True`

#### DeepSeek 缓存优化：

Prompt 结构按"固定内容在前、变化内容在后"组织，判断规则部分固定不变，能跨批次命中 DeepSeek 的上下文缓存（命中 0.025元/百万tokens vs 未命中 3元/百万tokens）。

---

## 七、跨平台去重（`_dedup_cross_platform`，第1427行）

1688 和 MIC 可能搜到同一家公司（同一工厂在两个平台都注册了）。

**去重策略**：
1. 提取"核心字号"（去掉括号英文、公司后缀"有限公司/制品厂"等、地域前缀）
2. 核心字号相同 → 同一家公司
3. 保留 `source_text` 更长的（信息更丰富，DeepSeek 能做出更准判断）
4. 核心名 < 2 字符的 → 不参与去重（太短无法可靠比对）

---

## 八、配置管理

### 8.1 搜索平台启停

数据库 `search_platforms` 表：

| 字段 | 说明 |
|------|------|
| `provider_code` | 平台代码（ali1688 / madeinchina） |
| `is_enabled` | 是否启用（管理员在管理中心切换） |
| `priority` | 搜索优先级 |
| `max_results` | 最大结果数 |

`search_suppliers` 主函数中从 DB 读取启用列表后，关闭的平台直接跳过，不浪费 API 调用。

### 8.2 AI 模型配置

`ai_model_configs` 表管理每个场景的模型参数：

| scene_code | 用途 |
|------------|------|
| `supplier_filter_v2` | AI过滤（temperature=0.2, JSON模式） |
| `supplier_translate` | 公司名翻译 |

所有配置通过 `model_config.py` 加载到内存，管理员修改后调用 `refresh_configs()` 热更新。

---

## 九、前端交互（SSE 进度推送）

前端通过 **Server-Sent Events (SSE)** 实时接收后端搜索进度：

```
POST /ai/search-suppliers/<id>
  ↓
后端创建 progress_queue + 后台线程
  ↓
前端 eventSource 监听 SSE 流
  ↓
收到进度消息：
  - 搜索关键词： [1/14] 关键词'玻璃柜'完成：本次新增34家，累计120家，已用时45秒
  - 提取+预筛：   正在提取和预筛供应商（共350家），已用时2分15秒
  - 天眼查补全：   天眼查补全中... 5/200，已用时5分30秒
  - AI过滤：      AI过滤中... 已完成2/4批，已用时8分10秒
  - 完成：        搜索完成！共保存120家供应商
```

每秒发送心跳保持连接，防止 waitress 代理超时断开。

---

## 十、完整调用链路一览

```
用户点击"开始AI搜索"
  │
  ▼
app.py:ai_search_suppliers()                     [Flask路由]
  │
  ├─ GET  → 渲染 search_suppliers.html            [配置页面]
  │
  └─ POST → 启动后台线程
      │
      ▼
  supplier_search.py:search_suppliers()          [主入口]
      │
      ├─ ① 解析 P0-P3 关键词矩阵（7组×2中英文=14个搜索词）
      │
      ├─ ② 并发搜索（3线程，每个关键词内1688+MIC并行）
      │     ├── crawl_1688()          → 1688 API (skills-gateway)
      │     └── crawl_made_in_china() → MIC MCP (mcp.chexb.com)
      │
      ├─ ③ 提取公司名 + 跨平台去重 + 程序化预筛
      │     ├── extract_company_names()
      │     ├── _dedup_cross_platform()
      │     └── _programmatic_prefilter()
      │
      ├─ ④ 天眼查 MCP 并发补全（5线程）
      │     ├── TianyanchaClient.search_companies()
      │     ├── TianyanchaClient.get_company_basic_profile()
      │     └── 丢弃 MIC 未匹配公司
      │
      ├─ ⑤ DeepSeek 精细过滤（8线程，每批50家）
      │     └── filter_suppliers_with_ai() → _filter_one_batch()
      │           └── call_deepseek(scene_code="supplier_filter_v2")
      │
      └─ 返回 suppliers[] → app.py 批量写入数据库
            └── INSERT INTO suppliers (name,intro,phone,email,...)
```
