# run_simulate.ps1 - launch the HK strategy in moomoo simulated trading.

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:TRADE_ENV = 'SIMULATE'
$env:MAX_POSITIONS = '0'
$env:FEISHU_CHAT_ID = 'oc_bc9a36b4392dbe632fb4e50a3ef7ef17'
Remove-Item Env:\ALLOW_REAL_TRADING -ErrorAction SilentlyContinue
Remove-Item Env:\TRADE_PASSWORD -ErrorAction SilentlyContinue

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

python -m hk_strategy.main
