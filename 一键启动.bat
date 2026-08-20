@echo off
setlocal enableextensions
title 供应商寻源系统 - 一键启动器

REM ============================================================
REM  供应商寻源系统 - 一键启动器（双击运行即可）
REM
REM  小白讲解：这个脚本双击后会自动完成 4 件事：
REM    1. 检测 MySQL 数据库是否在运行，没运行就自动启动它
REM    2. 关闭旧的网页服务进程（所以重复点击 = 重启系统）
REM    3. 在新窗口中启动网页系统（那个窗口别关，关了系统就停了）
REM    4. 自动打开浏览器访问 http://127.0.0.1:5000
REM
REM  注意：启动 MySQL 服务需要管理员权限，
REM        运行时会弹出 UAC 确认窗口，点"是"即可
REM ============================================================

REM ---------- 第 0 步：检查是否拥有管理员权限 ----------
REM 小白讲解：启动 Windows 服务（MySQL）必须有管理员权限。
REM net session 这条命令只有管理员能跑成功，用它来判断身份
net session >nul 2>&1
if %errorlevel%==0 goto :HAVE_ADMIN

echo.
echo  正在请求管理员权限，用于启动 MySQL 服务...
echo  弹出确认窗口时，请点击"是"
echo.
REM 小白讲解：通过 PowerShell 重新以管理员身份运行本脚本
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" >nul 2>&1
if not %errorlevel%==0 (
    echo  [错误] 未获得管理员权限！
    echo  请手动右键本文件，选择"以管理员身份运行"
    echo.
    pause
)
exit /b

:HAVE_ADMIN
REM 小白讲解：%~dp0 代表本文件所在的文件夹，确保能正确定位到项目
cd /d "%~dp0"
cls
echo ============================================
echo     供应商寻源系统 - 一键启动器
echo ============================================
echo.

REM ---------- 第 1 步：检测并启动 MySQL 数据库 ----------
echo [1/4] 检测 MySQL 数据库...
call :CHECK_MYSQL_PORT
if %errorlevel%==0 (
    echo        MySQL 已在运行，无需启动
    goto :STEP2
)

echo        MySQL 未运行，正在查找 MySQL 服务...
REM 小白讲解：在 Windows 服务列表里找名字带 mysql 的服务，比如 MySQL80
set "MYSQL_SERVICE="
for /f "tokens=2 delims=: " %%s in ('sc query state^= all ^| findstr /i "SERVICE_NAME" ^| findstr /i "mysql"') do (
    if not defined MYSQL_SERVICE set "MYSQL_SERVICE=%%s"
)

if not defined MYSQL_SERVICE (
    echo.
    echo  [错误] 没有找到 MySQL 服务！
    echo  请确认 MySQL 已安装。手动启动方法：
    echo    按 Win+R 输入 services.msc 回车，找到 MySQL 服务右键启动
    echo.
    pause
    exit /b 1
)

echo        找到服务：%MYSQL_SERVICE% ，正在启动...
net start "%MYSQL_SERVICE%" >nul 2>&1

REM 小白讲解：服务启动后端口要过几秒才就绪，这里最多等 15 秒
set /a MYSQL_WAIT=0
:WAIT_MYSQL
call :CHECK_MYSQL_PORT
if %errorlevel%==0 goto :MYSQL_READY
set /a MYSQL_WAIT+=1
if %MYSQL_WAIT% geq 15 goto :MYSQL_FAIL
ping -n 2 127.0.0.1 >nul
goto :WAIT_MYSQL

:MYSQL_READY
echo        MySQL 启动成功！
goto :STEP2

:MYSQL_FAIL
echo.
echo  [错误] MySQL 服务已启动但数据库端口一直没就绪，等了 15 秒
echo  可能是 MySQL 自身配置问题，请手动检查服务
echo.
pause
exit /b 1

REM ---------- 第 2 步：关闭旧的网页服务进程，实现"重启" ----------
:STEP2
echo.
echo [2/4] 清理旧的网页服务进程...
set "FOUND_OLD=0"
REM 小白讲解：查找占用 5000 端口的进程，就是正在运行的旧系统，强制结束它
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /c:":5000 " ^| findstr "LISTENING"') do (
    taskkill /F /T /PID %%p >nul 2>&1
    set "FOUND_OLD=1"
)
if "%FOUND_OLD%"=="1" (
    ping -n 3 127.0.0.1 >nul
    echo        已关闭旧进程，稍后重新启动
) else (
    echo        没有旧进程在运行
)

REM ---------- 第 3 步：启动网页系统 ----------
echo.
echo [3/4] 启动网页系统...

REM 小白讲解：先确认电脑上装了 Python，先试 python 命令，再试 py 命令
set "PYTHON_CMD="
where python >nul 2>&1
if %errorlevel%==0 set "PYTHON_CMD=python"
if not defined PYTHON_CMD (
    where py >nul 2>&1
    if %errorlevel%==0 set "PYTHON_CMD=py"
)
if not defined PYTHON_CMD (
    echo.
    echo  [错误] 没有找到 Python！请确认已安装 Python 并勾选过 Add to PATH
    echo.
    pause
    exit /b 1
)

REM 小白讲解：在新的窗口里启动系统，窗口里能看到运行日志
REM chcp 65001 是为了让日志里的中文不乱码
start "供应商寻源系统 - 服务运行中,请勿关闭" cmd /k "chcp 65001 >nul && %PYTHON_CMD% app.py"

REM 小白讲解：系统启动需要几秒钟，这里循环等待端口就绪，最多 30 秒
set /a WEB_WAIT=0
:WAIT_WEB
netstat -ano | findstr /c:":5000 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 goto :WEB_READY
set /a WEB_WAIT+=1
if %WEB_WAIT% geq 30 goto :WEB_FAIL
ping -n 2 127.0.0.1 >nul
goto :WAIT_WEB

:WEB_READY
echo        网页系统启动成功！

REM ---------- 第 4 步：自动打开浏览器 ----------
echo.
echo [4/4] 正在打开浏览器...
start "" "http://127.0.0.1:5000"

echo.
echo ============================================
echo   全部启动完成！
echo.
echo   访问地址：http://127.0.0.1:5000
echo   停止系统：关闭标题为"服务运行中"的那个窗口
echo   重启系统：再次双击本启动器即可
echo ============================================
echo.
echo  本窗口 5 秒后自动关闭...
ping -n 6 127.0.0.1 >nul
exit /b 0

:WEB_FAIL
echo.
echo  [警告] 网页系统启动超时，超过 30 秒端口仍未就绪
echo  请查看刚才弹出的服务窗口里的报错信息，常见原因：
echo    1. 依赖没装全：在项目目录运行 pip install -r requirements.txt
echo    2. 数据库连不上：确认 MySQL 服务已启动
echo.
pause
exit /b 1

REM ============================================================
REM  子函数：检测 MySQL 端口 3306 是否有人在监听
REM  小白讲解：MySQL 默认用 3306 端口通信，只要这个端口
REM  处于 LISTENING 监听状态，就说明数据库正在运行
REM ============================================================
:CHECK_MYSQL_PORT
netstat -ano | findstr /c:":3306 " | findstr "LISTENING" >nul 2>&1
exit /b %errorlevel%
