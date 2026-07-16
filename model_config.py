"""
AI 配置内存管理模块

小白讲解：这个文件负责从数据库加载所有AI配置到内存中，让其他模块（ai_helper、supplier_search）
能快速读取配置，不用每次都查数据库。管理员在Web界面修改配置后，调用 refresh_configs() 立即刷新。

核心功能：
1. load_model_configs_from_db() - 启动时从DB加载配置到内存
2. get_model_config(scene_code) - 根据场景代码获取AI模型配置（模型名/思考强度/温度等）
3. get_provider(provider_code) - 获取服务商信息（API地址/密钥）
4. get_search_platforms() - 获取启用的搜索平台列表（按优先级排序）
5. refresh_configs() - 管理员修改配置后刷新内存（热更新，无需重启）
"""

import threading
import pymysql
from config import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE

# ==================== 全局内存配置（线程安全）====================
# 小白讲解：用 threading.Lock 保护全局变量，防止多线程同时修改导致数据错乱
_lock = threading.Lock()

# 服务商字典：{provider_code: {id, provider_name, provider_type, base_url, api_key, is_enabled}}
_providers = {}

# 模型场景配置字典：{scene_code: {model_name, thinking_enabled, thinking_effort, max_tokens, ...}}
_model_configs = {}

# 搜索平台列表（按priority排序）：[{provider_code, priority, max_results, extra_config, ...}]
_search_platforms = []


def _get_db_connection():
    """
    创建MySQL数据库连接（不依赖Flask的g.db，可在任何地方使用）

    小白讲解：用pymysql连接MySQL，DictCursor让查询结果可以用 row["列名"] 访问。
    """
    conn = pymysql.connect(
        host=MYSQL_HOST, port=MYSQL_PORT,
        user=MYSQL_USER, password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE, charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    return conn


def load_model_configs_from_db():
    """
    从数据库加载所有AI配置到内存（应用启动时调用一次）

    小白讲解：把数据库里的配置全部读到内存里，后续读取配置直接从内存取，速度快。
    管理员修改配置后，调用 refresh_configs() 重新加载即可热更新。
    """
    global _providers, _model_configs, _search_platforms
    conn = _get_db_connection()
    cursor = conn.cursor()

    # 1. 加载所有服务商
    cursor.execute("SELECT * FROM ai_providers")
    providers = {}
    for row in cursor.fetchall():
        providers[row["provider_code"]] = {
            "id": row["id"],
            "provider_name": row["provider_name"],
            "provider_type": row["provider_type"],
            "base_url": row["base_url"],
            "api_key": row["api_key"],
            "is_enabled": bool(row["is_enabled"]),
        }

    # 2. 加载所有模型场景配置
    cursor.execute("SELECT * FROM ai_model_configs")
    configs = {}
    for row in cursor.fetchall():
        configs[row["scene_code"]] = {
            "id": row["id"],
            "provider_id": row["provider_id"],
            "scene_code": row["scene_code"],
            "scene_name": row["scene_name"],
            "model_name": row["model_name"],
            "thinking_enabled": bool(row["thinking_enabled"]),
            "thinking_effort": row["thinking_effort"],
            "max_tokens": row["max_tokens"],
            "temperature": row["temperature"],
            "timeout_seconds": row["timeout_seconds"],
            "is_enabled": bool(row["is_enabled"]),
        }

    # 3. 加载搜索平台配置（按priority排序，只取启用的）
    cursor.execute("""
        SELECT sp.*, ap.provider_code, ap.provider_name, ap.base_url, ap.api_key
        FROM search_platforms sp
        JOIN ai_providers ap ON sp.provider_id = ap.id
        WHERE sp.is_enabled = 1 AND ap.is_enabled = 1
        ORDER BY sp.priority ASC
    """)
    platforms = []
    for row in cursor.fetchall():
        platforms.append({
            "provider_code": row["provider_code"],
            "provider_name": row["provider_name"],
            "base_url": row["base_url"],
            "api_key": row["api_key"],
            "priority": row["priority"],
            "max_results": row["max_results"],
            "extra_config": row["extra_config"],
        })

    conn.close()

    # 用锁保护写入操作
    with _lock:
        _providers = providers
        _model_configs = configs
        _search_platforms = platforms

    print(f"[配置加载] 服务商:{len(providers)}个 场景配置:{len(configs)}个 搜索平台:{len(platforms)}个")


def get_model_config(scene_code):
    """
    根据场景代码获取AI模型配置

    小白讲解：ai_helper 和 supplier_search 调用AI时，传一个场景代码（如"req_parse"），
    这个函数返回该场景的模型名、思考强度、温度等配置。

    参数：scene_code 场景代码，如 "req_parse"/"keyword_gen"/"auto_screening" 等
    返回：配置字典，如果场景不存在返回 None
    """
    with _lock:
        return _model_configs.get(scene_code)


def get_provider(provider_code):
    """
    获取服务商信息（API地址、密钥等）

    参数：provider_code 服务商代码，如 "deepseek"/"zhipu"/"ali1688"/"tianyancha"
    返回：服务商字典，如果不存在返回 None
    """
    with _lock:
        return _providers.get(provider_code)


def get_search_platforms():
    """
    获取启用的搜索平台列表（按优先级排序）

    小白讲解：supplier_search 搜索供应商时，调用这个函数获取要搜索的平台列表。
    管理员可以在Web界面启停平台、调整优先级。

    返回：平台列表，每个元素是字典 {provider_code, provider_name, base_url, api_key, ...}
    """
    with _lock:
    # 返回副本防止外部修改内存数据
        return [p.copy() for p in _search_platforms]


def refresh_configs():
    """
    刷新内存配置（管理员修改配置后调用，实现热更新）

    小白讲解：管理员在Web界面修改了API密钥或模型参数后，调用这个函数重新从数据库加载，
    新配置立即生效，不需要重启服务器。
    """
    load_model_configs_from_db()
