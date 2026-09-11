# ============================================================
#  供应商寻源系统 - 一键启动器（PowerShell 版，绝对不闪退）
#
#  小白讲解：这个脚本会做 4 件事：
#    1. 检测 MySQL 是否在运行，没运行就尝试启动
#    2. 关闭旧的 5000 端口进程（重复运行 = 重启系统）
#    3. 在新窗口中启动 python app.py
#    4. 自动打开浏览器访问 http://127.0.0.1:5000
#
#  执行方法（任选一种）：
#    A. 在 PyCharm 终端里执行：
#       powershell -ExecutionPolicy Bypass -File "d:\pycharm\供应商寻源系统\启动.ps1"
#    B. 在文件管理器里右键本文件 → "使用 PowerShell 运行"
#
#  注意：启动 MySQL 服务需要管理员权限。脚本会自动请求 UAC。
# ============================================================

# 设置控制台编码为 UTF-8（防止中文乱码）
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$host.UI.RawUI.WindowTitle = "供应商寻源系统 - 一键启动器"

# 分隔线
$LINE = "============================================"

# ---------- 第 0 步：检查管理员权限 ----------
# 小白讲解：启动 MySQL 服务必须有管理员权限
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host ""
    Write-Host "需要管理员权限来启动 MySQL 服务..." -ForegroundColor Yellow
    Write-Host "正在请求 UAC 确认，请在弹窗里点【是】" -ForegroundColor Yellow
    Write-Host ""

    # 用 Start-Process 以管理员身份重新启动本脚本
    $scriptPath = $MyInvocation.MyCommand.Path
    Start-Process -FilePath "powershell" -ArgumentList "-ExecutionPolicy", "Bypass", "-File", "`"$scriptPath`"" -Verb RunAs
    exit
}

# ---------- 进入项目目录 ----------
Set-Location -LiteralPath $PSScriptRoot
Clear-Host
Write-Host $LINE -ForegroundColor Cyan
Write-Host "     供应商寻源系统 - 一键启动器" -ForegroundColor Cyan
Write-Host $LINE -ForegroundColor Cyan
Write-Host ""

# ---------- 第 1 步：检测并启动 MySQL ----------
Write-Host "[1/4] 检测 MySQL 数据库..." -ForegroundColor Cyan
$mysqlListening = netstat -ano | Select-String ":3306 " | Select-String "LISTENING"
if ($mysqlListening) {
    Write-Host "     [OK] MySQL 已在运行" -ForegroundColor Green
} else {
    Write-Host "     MySQL 未运行，正在查找服务..." -ForegroundColor Yellow

    # 用 sc query 查找 MySQL 服务
    $services = sc query state= all | Select-String "SERVICE_NAME" | Select-String -Pattern "mysql" -CaseSensitive:$false
    $serviceName = $null
    foreach ($line in $services) {
        if ($line -match "SERVICE_NAME:\s*(\S+)") {
            $serviceName = $Matches[1]
            break
        }
    }

    if (-not $serviceName) {
        Write-Host ""
        Write-Host "     [错误] 没找到 MySQL 服务！" -ForegroundColor Red
        Write-Host "     请确认 MySQL 已安装。可手动启动：Win+R → services.msc → 找 MySQL 右键启动" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "按任意键关闭..." -ForegroundColor Gray
        $null = $host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
        exit 1
    }

    Write-Host "     找到服务: $serviceName ，正在启动..." -ForegroundColor Yellow
    net start $serviceName 2>&1 | Out-Null

    # 等待端口就绪，最多 15 秒
    $wait = 0
    while ($wait -lt 15) {
        Start-Sleep -Seconds 1
        $wait++
        $ok = netstat -ano | Select-String ":3306 " | Select-String "LISTENING"
        if ($ok) { break }
    }

    $mysqlListening = netstat -ano | Select-String ":3306 " | Select-String "LISTENING"
    if ($mysqlListening) {
        Write-Host "     [OK] MySQL 启动成功（用了 $wait 秒）" -ForegroundColor Green
    } else {
        Write-Host "     [错误] MySQL 启动超时" -ForegroundColor Red
        Write-Host "     请手动检查服务是否正常运行" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "按任意键关闭..." -ForegroundColor Gray
        $null = $host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
        exit 1
    }
}

# ---------- 第 2 步：关闭旧的 5000 端口进程 ----------
Write-Host ""
Write-Host "[2/4] 清理旧进程..." -ForegroundColor Cyan
$oldProcesses = netstat -ano | Select-String ":5000 " | Select-String "LISTENING"
$killed = 0
foreach ($line in $oldProcesses) {
    if ($line -match "\s(\d+)\s*$") {
        # 注意：不能叫 $pid，因为 $pid 是 PowerShell 内置只读自动变量（当前进程ID），赋值会报错
        $targetPid = $Matches[1]
        taskkill /F /T /PID $targetPid 2>&1 | Out-Null
        $killed++
    }
}
if ($killed -gt 0) {
    Start-Sleep -Seconds 2
    Write-Host "     已关闭 $killed 个旧进程" -ForegroundColor Green
} else {
    Write-Host "     没有旧进程" -ForegroundColor Green
}

# ---------- 第 3 步：启动 Python 系统 ----------
Write-Host ""
Write-Host "[3/4] 启动 Python 系统..." -ForegroundColor Cyan
$pyExe = $null
if (Get-Command python -ErrorAction SilentlyContinue) { $pyExe = (Get-Command python).Source }
elseif (Get-Command py -ErrorAction SilentlyContinue) { $pyExe = (Get-Command py).Source }

if (-not $pyExe) {
    Write-Host "     [错误] 没找到 Python！" -ForegroundColor Red
    Write-Host "按任意键关闭..." -ForegroundColor Gray
    $null = $host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    exit 1
}

# 在新窗口里启动系统
$proc = Start-Process -FilePath $pyExe -ArgumentList "app.py" -WorkingDirectory $PSScriptRoot -WindowStyle Normal -PassThru
Write-Host "     Python 进程已启动 (PID=$($proc.Id))，新窗口里能看到运行日志" -ForegroundColor Green

# 等待 5000 端口就绪，最多 30 秒
$wait = 0
$ready = $false
while ($wait -lt 30) {
    Start-Sleep -Seconds 1
    $wait++
    $ok = netstat -ano | Select-String ":5000 " | Select-String "LISTENING"
    if ($ok) { $ready = $true; break }
}

if ($ready) {
    Write-Host "     [OK] 系统就绪（用了 $wait 秒）" -ForegroundColor Green
} else {
    Write-Host "     [警告] 30秒内未就绪，请查看刚才弹出的服务窗口" -ForegroundColor Yellow
}

# ---------- 第 4 步：打开浏览器 ----------
Write-Host ""
Write-Host "[4/4] 打开浏览器..." -ForegroundColor Cyan
Start-Process "http://127.0.0.1:5000"
Write-Host "     已打开浏览器" -ForegroundColor Green

Write-Host ""
Write-Host $LINE -ForegroundColor Cyan
Write-Host "   全部启动完成！" -ForegroundColor Green
Write-Host ""
Write-Host "   访问地址: http://127.0.0.1:5000" -ForegroundColor White
Write-Host "   停止系统: 关闭刚才弹出的 Python 窗口" -ForegroundColor White
Write-Host "   重启系统: 再次双击本启动器" -ForegroundColor White
Write-Host $LINE -ForegroundColor Cyan
Write-Host ""
Write-Host "本窗口 5 秒后自动关闭..." -ForegroundColor Gray
Start-Sleep -Seconds 5
