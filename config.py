"""
AI 配置文件 - 首次启动时作为种子数据迁移到数据库

小白讲解：这个文件现在只用于「第一次启动系统」时，把API密钥和模型参数
迁移到数据库的 ai_providers / ai_model_configs 表中。迁移完成后，所有配置都从
数据库读取（通过 model_config 模块），管理员可在「管理中心 → 模型与平台管理」
页面修改配置，修改后立即生效，无需修改本文件也无需重启服务器。

【安全说明】所有敏感信息（密钥、密码）都通过环境变量或 .env 文件读取。
本文件不包含任何真实密钥，可以安全上传到GitHub。
- 本地开发：在项目根目录创建 .env 文件（已在.gitignore中排除），填入真实密钥
- 云端部署：在部署平台后台配置环境变量

API Key 获取地址：
- 智谱：https://open.bigmodel.cn/  注册后在控制台"API Keys"页面创建（有免费额度）
- DeepSeek：https://platform.deepseek.com/  注册后在"API Keys"页面创建
"""

import os
import pymysql

# ==================== 加载 .env 文件（本地开发用）====================
# 小白讲解：这行代码会自动读取项目根目录下的 .env 文件，把里面的配置加载成环境变量。
# .env 文件不会被上传到GitHub（已在.gitignore中排除），所以密钥很安全。
# 部署到云端时不需要.env文件，直接在平台后台配置环境变量即可。
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # 如果没安装python-dotenv，跳过（云端部署时可能不需要）
    pass


# ==================== MySQL 数据库配置 ====================
# 小白讲解：从SQLite迁移到MySQL，解决并发写入时"database is locked"问题。
# 优先读环境变量（部署时用），没有环境变量就用默认值（本地开发用）。
MYSQL_HOST = os.environ.get("MYSQL_HOST", "localhost")
MYSQL_PORT = int(os.environ.get("MYSQL_PORT", "3306"))
MYSQL_USER = os.environ.get("MYSQL_USER", "sourcing")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "sourcing_db")


# ==================== 智谱 GLM 配置（用于图片识别）====================
# 首次启动时迁移到数据库 ai_providers 表（provider_code=zhipu）
ZHIPU_API_KEY = os.environ.get("ZHIPU_API_KEY", "")
ZHIPU_BASE_URL = os.environ.get("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
ZHIPU_VISION_MODEL = os.environ.get("ZHIPU_VISION_MODEL", "glm-4v-flash")


# ==================== DeepSeek 配置（用于文本理解/搜索/初筛）====================
# 首次启动时迁移到数据库 ai_providers 表（provider_code=deepseek）
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")


# ==================== DeepSeek 能力拉满配置（追求最高质量）====================
# 首次启动时迁移到数据库 ai_model_configs 表（每个场景独立配置）
DEEPSEEK_THINKING_ENABLED = True
DEEPSEEK_THINKING_EFFORT = "max"

# 思考强度分级：high=默认，max=最强（已废弃简单/复杂两档，改为每场景独立配置）
DEEPSEEK_EFFORT_SIMPLE = "high"
DEEPSEEK_EFFORT_COMPLEX = "max"

DEEPSEEK_MAX_TOKENS = 32768
DEEPSEEK_TIMEOUT = 300


# ==================== 天眼查 MCP 配置（用于供应商工商信息补全）====================
# 首次启动时迁移到数据库 ai_providers 表（provider_code=tianyancha）
TYC_MCP_URL = os.environ.get("TYC_MCP_URL", "https://mcp.tianyancha.com/v1")
TYC_MCP_AUTH = os.environ.get("TYC_MCP_AUTH", "")


# ==================== 1688 配置（用于供应商搜索 - 官方API方式）====================
# 首次启动时迁移到数据库 ai_providers 表（provider_code=ali1688）
ALI_1688_AK = os.environ.get("ALI_1688_AK", "")


# ==================== 状态检测函数（优先从数据库读取，回退到硬编码）====================
def _query_provider_api_key(provider_code):
    """
    从数据库查询指定服务商的api_key（内部辅助函数）

    小白讲解：优先从数据库读取（保证管理员修改后状态显示同步），数据库不存在时返回None。
    """
    try:
        conn = pymysql.connect(
            host=MYSQL_HOST, port=MYSQL_PORT,
            user=MYSQL_USER, password=MYSQL_PASSWORD,
            database=MYSQL_DATABASE, charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor,
        )
        cursor = conn.cursor()
        cursor.execute("SELECT api_key FROM ai_providers WHERE provider_code = %s", (provider_code,))
        row = cursor.fetchone()
        conn.close()
        return row["api_key"] if row else None
    except Exception:
        return None


def is_1688_ak_configured():
    """
    检查1688 AK是否已配置

    小白讲解：优先从数据库读取（管理中心修改后立即反映），数据库不存在时回退到硬编码。
    """
    db_key = _query_provider_api_key("ali1688")
    if db_key is not None:
        return bool(db_key and len(db_key) > 50)
    # 回退到环境变量配置
    return bool(ALI_1688_AK and len(ALI_1688_AK) > 50)


def is_api_configured():
    """
    检查 DeepSeek API Key 是否已配置

    小白讲解：优先从数据库读取（管理中心修改后立即反映），数据库不存在时回退到环境变量。
    """
    db_key = _query_provider_api_key("deepseek")
    if db_key is not None:
        return bool(db_key and not db_key.startswith("sk-xxxx"))
    return bool(DEEPSEEK_API_KEY and not DEEPSEEK_API_KEY.startswith("sk-xxxx"))


def is_vision_configured():
    """
    检查智谱 API Key 是否已配置（用于图片识别）

    小白讲解：优先从数据库读取（管理中心修改后立即反映），数据库不存在时回退到环境变量。
    """
    db_key = _query_provider_api_key("zhipu")
    if db_key is not None:
        return bool(db_key and not db_key.startswith("xxxx.xxxx"))
    return bool(ZHIPU_API_KEY and not ZHIPU_API_KEY.startswith("xxxx.xxxx"))
