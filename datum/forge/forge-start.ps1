# DATUM / FORGE Launch Script
# Starts the FORGE document intelligence loop (from within DATUM)
# Usage: powershell -File forge-start.ps1 [-Action run|once|dry|status|watcher]

param(
    [string]$Action = "run",
    [string]$Path = "."
)

$ForgeDir = "C:\Users\zach\.openclaw\workspace\datum\datum\forge"
$Config = "$ForgeDir\config\local.yaml"
$Python = "python"
$env:PYTHONUTF8 = "1"

Set-Location $ForgeDir

switch ($Action) {
    "run" {
        Write-Host "Starting FORGE continuous loop..." -ForegroundColor Cyan
        & $Python forge.py --config $Config --path $Path
    }
    "once" {
        Write-Host "Running FORGE one cycle..." -ForegroundColor Cyan
        & $Python forge.py --config $Config --path $Path --once
    }
    "dry" {
        Write-Host "Running FORGE dry run..." -ForegroundColor Yellow
        & $Python forge.py --config $Config --path $Path --once --dry-run
    }
    "status" {
        & $Python forge.py --config $Config --status
    }
    "watcher" {
        Write-Host "Starting FORGE file watcher..." -ForegroundColor Cyan
        & $Python watcher.py --path "$ForgeDir\staging" --config $Config
    }
    default {
        Write-Host "Unknown action: $Action" -ForegroundColor Red
        Write-Host "Usage: forge-start.ps1 -Action [run|once|dry|status|watcher]"
    }
}
