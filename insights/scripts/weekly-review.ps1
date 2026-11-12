<#
Runs the weekly agent-efficiency review.

Register on this machine:
schtasks /Create /TN "watchdog-insights weekly review" /SC WEEKLY /D MON /ST 09:00 /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File <repo>\scripts\weekly-review.ps1"

Notes:
- The analyze steps require the claude CLI to be available for the configured claude-cli backend.
- The task must run in the user's context so it can read local Claude/Codex session files.
- uv uses the user-level UV_CACHE_DIR environment variable when it is set.
- Review reports are written under reports/, which is intentionally git-ignored.
#>

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$ReportDir = Join-Path $RepoRoot "reports"
$Stamp = Get-Date -Format "yyyy-MM-dd-HHmmss"
$ReportPath = Join-Path $ReportDir "weekly-review-$Stamp.md"

function Format-CommandLine {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$CommandArgs
    )

    return "uv " + ($CommandArgs -join " ")
}

function Invoke-ReviewStep {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [string[]]$AsiArgs,

        [Parameter(Mandatory = $true)]
        [object]$ReportLines
    )

    $UvArgs = @("run") + $AsiArgs
    $CommandLine = Format-CommandLine -CommandArgs $UvArgs
    $StartedAt = Get-Date

    $ReportLines.Add("## $Name")
    $ReportLines.Add("")
    $ReportLines.Add("Command: ``$CommandLine``")
    $ReportLines.Add("")

    Push-Location $RepoRoot
    try {
        $Output = & uv @UvArgs 2>&1
        $ExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    $FinishedAt = Get-Date
    $Duration = [int]($FinishedAt - $StartedAt).TotalSeconds
    $ReportLines.Add("Exit code: $ExitCode")
    $ReportLines.Add("Duration: $Duration seconds")
    $ReportLines.Add("")
    $ReportLines.Add('```text')
    foreach ($Line in $Output) {
        $ReportLines.Add([string]$Line)
    }
    $ReportLines.Add('```')
    $ReportLines.Add("")

    if ($ExitCode -ne 0) {
        throw "$Name failed with exit code $ExitCode"
    }
}

New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null

$ReportLines = [System.Collections.Generic.List[string]]::new()
$ReportLines.Add("# Weekly agent-efficiency review")
$ReportLines.Add("")
$ReportLines.Add("Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')")
$ReportLines.Add("Repository: $RepoRoot")
$ReportLines.Add("")

try {
    Invoke-ReviewStep -Name "Import sessions" -AsiArgs @("wdi", "import") -ReportLines $ReportLines
    Invoke-ReviewStep -Name "Stats" -AsiArgs @("wdi", "stats") -ReportLines $ReportLines
    Invoke-ReviewStep `
        -Name "Prompt quality analysis" `
        -AsiArgs @("wdi", "analyze", "--type", "prompt-quality", "--limit", "10", "--yes") `
        -ReportLines $ReportLines
    Invoke-ReviewStep `
        -Name "Agent efficiency analysis" `
        -AsiArgs @("wdi", "analyze", "--type", "agent-efficiency", "--limit", "10", "--yes") `
        -ReportLines $ReportLines

    $ReportLines.Add("## Result")
    $ReportLines.Add("")
    $ReportLines.Add("All review steps completed.")
    $ReportLines.Add("")
}
catch {
    $ReportLines.Add("## Result")
    $ReportLines.Add("")
    $ReportLines.Add("Review failed: $($_.Exception.Message)")
    $ReportLines.Add("")
    $ReportLines | Set-Content -Path $ReportPath -Encoding UTF8
    Write-Error "Weekly review failed. Report written to $ReportPath"
    exit 1
}

$ReportLines | Set-Content -Path $ReportPath -Encoding UTF8
Write-Host "Weekly review report written to $ReportPath"
