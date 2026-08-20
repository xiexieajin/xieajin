"""
AI 配置文件模板 — 复制为 config.py 并填入你自己的密钥

使用步骤（小白看这里）：
1. 复制这个文件，重命名为 config.py
2. 把下面所有 "改成你的xxx" 替换成你的真实密钥
3. config.py 已在 .gitignore 中排除，不会被上传到 GitHub

API Key 获取地址：
- 智谱：https://open.bigmodel.cn/  注册后在控制台"API Keys"页面创建（有免费额度）
- MiniMax：https://platform.minimaxi.com/  注册后在控制台创建 API Key（格式如 sk-cp-xxx）
- 天眼查MCP：联系天眼查获取
- 1688 API：在1688开放平台申请
"""

import os
import pymysql

# ==================== MySQL 数据库配置 ====================
MYSQL_HOST = "localhost"
MYSQL_PORT = 3306
MYSQL_USER = "改成你的数据库用户名"
MYSQL_PASSWORD = "改成你的数据库密码"
MYSQL_DATABASE = "sourcing_db"


# ==================== 智谱 GLM 配置（用于图片识别）====================
ZHIPU_API_KEY = "改成你的智谱API密钥"
ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
ZHIPU_VISION_MODEL = "glm-4v-flash"


# ==================== MiniMax 配置（用于文本理解/搜索/初筛）====================
# 首次启动时迁移到数据库 ai_providers 表（provider_code=minimax）
# 小白讲解：Key 优先读环境变量 MINIMAX_API_KEY，其次读小写的 minimax（兼容用户已设置的名字）
MINIMAX_API_KEY = "改成你的MiniMax API密钥（sk-cp-开头）"
MINIMAX_BASE_URL = "https://api.minimaxi.com/v1"
MINIMAX_MODEL = "MiniMax-M3"

# ==================== MiniMax 能力配置（各场景种子参数）====================
# 小白讲解：MiniMax-M3 的思考模式没有 low/medium/high/max 分级，只有开(adaptive)/关(disabled)，
# 温度官方推荐 1.0（已按场景写入数据库种子配置）。
MINIMAX_MAX_TOKENS = 32768
MINIMAX_TIMEOUT = 300


# ==================== 天眼查 MCP 配置（用于供应商工商信息补全）====================
TYC_MCP_URL = "https://mcp.tianyancha.com/v1"
TYC_MCP_AUTH = "改成你的天眼查MCP认证token"


# ==================== 1688 配置（用于供应商搜索 - 官方API方式）====================
ALI_1688_AK = "改成你的1688 AccessKey（Base64编码）"


# ==================== 状态检测函数 ====================
def _query_provider_api_key(provider_code):
    """从数据库查询指定服务商的api_key（内部辅助函数）"""
    try:
        conn = pymysql.connect(
            host=MYSQL_HOST, port=MYSQL_PORT,
            user=MYSQL_USER, password=MYSQL_PASSWORD,
            database=MYSQL_DATABASE, charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor,
        )
        cursor = conn.cursor()
        cursor.execute(
            "SELECT api_key FROM ai_providers WHERE provider_code = %s",
            (provider_code,)
        )
        row = cursor.fetchone()
        conn.close()
        return row["api_key"] if row else None
    except Exception:
        return None


def is_1688_ak_configured():
    """检查1688 AK是否已配置"""
    db_key = _query_provider_api_key("ali1688")
    if db_key is not None:
        return bool(db_key and len(db_key) > 50)
    return bool(ALI_1688_AK and len(ALI_1688_AK) > 50)


def is_api_configured():
    """检查 MiniMax API Key 是否已配置"""
    db_key = _query_provider_api_key("minimax")
    if db_key is not None:
        return bool(db_key and not db_key.startswith("sk-xxxx"))
    return bool(MINIMAX_API_KEY and not MINIMAX_API_KEY.startswith("sk-xxxx"))


def is_vision_configured():
    """检查智谱 API Key 是否已配置（用于图片识别）"""
    db_key = _query_provider_api_key("zhipu")
    if db_key is not None:
        return bool(db_key and not db_key.startswith("xxxx.xxxx"))
    return bool(ZHIPU_API_KEY and not ZHIPU_API_KEY.startswith("xxxx.xxxx"))
