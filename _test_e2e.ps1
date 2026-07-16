# 端到端测试：AI解析带亚马逊URL的需求
$ErrorActionPreference = 'Stop'
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
try { Invoke-WebRequest -Uri 'http://localhost:5000/login' -Method POST -WebSession $session -Body @{username='xieajin'; password='bsq123'} -MaximumRedirection 0 -ErrorAction SilentlyContinue } catch {}

Write-Host "===== AI解析端到端测试（用户真实亚马逊URL）====="

# 用用户真实输入（带反引号和长URL）
$inputText = "参考``https://www.amazon.com/Muwuele-Overbed-Adjustable-Hospital-Standing/dp/B0DWJ5FY8P/ref=sr_1_1?crid=1S5JG64HXTK42&dib=eyJ2IjoiMSJ9.T6zb&keywords=muwuele&th=1``"

try {
    $resp = Invoke-WebRequest -Uri 'http://localhost:5000/ai/parse-requirement' -Method POST -WebSession $session -Body @{input_text=$inputText} -UseBasicParsing -TimeoutSec 120
    $code = [int]$resp.StatusCode
    $len = $resp.Content.Length
    Write-Host "parse status: $code, length: $len"
    Write-Host ""
    Write-Host "--- keyword check ---"
    $keywords = @("Muwuele", "Overbed", "Adjustable", "Hospital", "Standing")
    foreach ($kw in $keywords) {
        $found = $resp.Content -match $kw
        Write-Host "  has '$kw': $found"
    }
    Write-Host ""
    if ($resp.Content -match 'danger') {
        Write-Host "[WARN] page shows error"
    } else {
        Write-Host "[OK] AI parse completed without error"
    }
} catch {
    Write-Host "request error: $($_.Exception.Message)"
}
