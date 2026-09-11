# ============================================================
#  供应商寻源系统 - 环境诊断脚本（PowerShell 版）
#
#  小白讲解：这个脚本会逐项检查环境（Python/MySQL/依赖包等），
#  并把检查结果一条条打印出来，最后会停下来等你按任意键关闭窗口。
#  不会闪退。
#
#  执行方法（在 PyCharm 终端里复制粘贴执行）：
#    powershell -ExecutionPolicy Bypass -File "d:\pycharm\供应商寻源系统\诊断.ps1"
# ============================================================

$ErrorActionPreference = "Continue"
$host.UI.RawUI.WindowTitle = "供应商寻源系统 - 环境诊断"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 全局错误计数
$script:ERR_COUNT = 0

# ----------------- 辅助函数 -----------------
# 分隔线（用纯字符串避免表达式解析问题）
$LINE = "============================================"

# 小白讲解：Write-Header 用来打印"分节标题"（带分隔线的那种）
function Write-Header($text) {
    Write-Host ""
    Write-Host $LINE -ForegroundColor Cyan
    Write-Host ("  " + $text) -ForegroundColor Cyan
    Write-Host $LINE -ForegroundColor Cyan
    Write-Host ""
}

# 小白讲解：Write-OK / Write-FAIL / Write-WARN 是三种状态输出，分别用绿色/红色/黄色
function Write-OK($text)   { Write-Host "[OK]   $text" -ForegroundColor Green }
function Write-FAIL($text) { Write-Host "[FAIL] $text" -ForegroundColor Red; $script:ERR_COUNT++ }
function Write-WARN($text) { Write-Host "[WARN] $text" -ForegroundColor Yellow }
function Write-INFO($text) { Write-Host "[INFO] $text" -ForegroundColor Gray }

# ----------------- 开始诊断 -----------------
Write-Header "供应商寻源系统 - 环境诊断"

# 切到脚本所在目录（避免在错误目录执行）
Set-Location -LiteralPath $PSScriptRoot
Write-INFO ("当前目录: " + $PWD)
Write-Host ""

# ---------- 检查 1：项目文件 app.py ----------
Write-Host "[1/6] 检查项目文件 app.py ..." -ForegroundColor Cyan
if (Test-Path -LiteralPath "app.py") {
    Write-OK "找到 app.py"
} else {
    Write-FAIL "找不到 app.py（当前目录不对）"
    Write-Host ("       期望位置: " + $PSScriptRoot + "\app.py") -ForegroundColor Yellow
}

# ---------- 检查 2：Python ----------
Write-Host ""
Write-Host "[2/6] 检查 Python ..." -ForegroundColor Cyan
$pyCmd = $null
if (Get-Command python -ErrorAction SilentlyContinue) { $pyCmd = "python" }
elseif (Get-Command py -ErrorAction SilentlyContinue) { $pyCmd = "py" }

if ($pyCmd) {
    $ver = & $pyCmd --version 2>&1
    Write-OK ("Python 已安装: " + $ver + " （命令: " + $pyCmd + "）")
} else {
    Write-FAIL "未找到 Python"
    Write-Host "       修复: 到 https://www.python.org/downloads/ 下载 Python 3.x" -ForegroundColor Yellow
    Write-Host "       安装时务必勾选 [Add Python to PATH]" -ForegroundColor Yellow
}

# ---------- 检查 3：MySQL 端口 ----------
Write-Host ""
Write-Host "[3/6] 检查 MySQL 数据库（端口 3306）..." -ForegroundColor Cyan
$mysql = netstat -ano | Select-String ":3306 " | Select-String "LISTENING"
if ($mysql) {
    Write-OK "MySQL 正在运行"
} else {
    Write-FAIL "MySQL 未运行（3306 端口未监听）"
    Write-Host "       修复方法 1: 双击 一键启动.bat 会自动启动 MySQL" -ForegroundColor Yellow
    Write-Host "       修复方法 2: 按 Win+R 输入 services.msc 找 MySQL 服务右键启动" -ForegroundColor Yellow
}

# ---------- 检查 4：依赖包 ----------
Write-Host ""
Write-Host "[4/6] 检查 Python 依赖包 ..." -ForegroundColor Cyan
if ($pyCmd) {
    $missing = @()
    # 小白讲解：依次尝试 import 6 个核心包，看哪个报错就是缺哪个
    $mods = @("flask", "requests", "pymysql", "openpyxl", "docx", "bs4")
    foreach ($mod in $mods) {
        $out = & $pyCmd -c ("import " + $mod) 2>&1
        if ($LASTEXITCODE -ne 0) { $missing += $mod }
    }
    if ($missing.Count -eq 0) {
        Write-OK "核心依赖包齐全（flask/requests/pymysql/openpyxl/docx/bs4）"
    } else {
        $missStr = $missing -join ", "
        Write-FAIL ("缺少依赖包: " + $missStr)
        Write-Host "       修复: 在项目目录执行 pip install -r requirements.txt" -ForegroundColor Yellow
    }
} else {
    Write-WARN "跳过（Python 未安装，无法检查）"
}

# ---------- 检查 5：5000 端口 ----------
Write-Host ""
Write-Host "[5/6] 检查 5000 端口占用情况 ..." -ForegroundColor Cyan
$port5000 = netstat -ano | Select-String ":5000 " | Select-String "LISTENING"
if ($port5000) {
    Write-WARN "5000 端口已被占用（系统可能已经在运行）"
    Write-Host "       如果能正常用就忽略。如果想重启：双击 一键启动.bat" -ForegroundColor Yellow
} else {
    Write-OK "5000 端口空闲（系统未运行）"
}

# ---------- 检查 6：.env 文件 ----------
Write-Host ""
Write-Host "[6/6] 检查 .env 配置文件 ..." -ForegroundColor Cyan
if (Test-Path -LiteralPath ".env") {
    Write-OK ".env 文件存在"
} else {
    Write-WARN "缺少 .env 文件"
    Write-Host "       首次部署时需要在项目根目录创建 .env" -ForegroundColor Yellow
}

# ----------------- 总结 -----------------
Write-Header "诊断结果"
if ($script:ERR_COUNT -eq 0) {
    Write-Host "  全部检查通过！" -ForegroundColor Green
    Write-Host "  下一步: 双击 一键启动.bat 启动系统" -ForegroundColor Green
} else {
    Write-Host ("  发现 " + $script:ERR_COUNT + " 个问题，请按上面 [FAIL] 项的提示逐项修复") -ForegroundColor Red
}
Write-Host ""
Write-Host "按任意键关闭窗口 ..." -ForegroundColor Gray
$null = $host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
