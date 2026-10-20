# 运行入口包装：把 third_party 里的依赖挂上，然后跑指定模块
# 用法：
#   .\scripts\run.ps1 -m agent_budget.analysis.summarize --run-root artifacts/v13_strict
#   .\scripts\run.ps1 -Python <python3.10 路径> -m pytest -q
# 说明：本项目依赖 Python 3.10（third_party/pylibs 内的二进制扩展为 cp310）。
param(
    [string]$Python = "",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$root = Split-Path -Parent $PSScriptRoot

if (-not $Python) {
    # 依次尝试：PATH 里的 python、py 启动器指向的 3.10
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        $ver = & $cmd.Source -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($ver -eq "3.10") { $Python = $cmd.Source }
    }
    if (-not $Python) {
        $py = Get-Command py -ErrorAction SilentlyContinue
        if ($py) {
            $candidate = & $py.Source -3.10 -c "import sys; print(sys.executable)" 2>$null
            if ($candidate) { $Python = $candidate }
        }
    }
    if (-not $Python) {
        throw "未找到 Python 3.10 解释器，请用 -Python 指定（本项目依赖 cp310 二进制扩展）"
    }
}

$paths = @(
    "$root\src",
    "$root\third_party\pylibs",
    "$root\third_party\mini-swe-agent\src",
    "$root\third_party\agent-harness",
    "$root\third_party\agent-harness\agentops\session_eval"
) -join ';'

$env:PYTHONPATH = $paths
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

Push-Location $root
try {
    & $Python @Args
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}

