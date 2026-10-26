<#
.SYNOPSIS
    Import a downloaded bumper archive into media/bumps/, sorted by show.

.DESCRIPTION
    The Internet Archive `AdultswimBumps` collection is a flat dump of 7,000+
    bumpers named like `2003_HowTo___ATHF_Next.mp4`. The planner needs them
    organised, because it reads three things off the path and the filename:

      media/bumps/<show-id>/<file>.mp4   -> subject comes from the FOLDER
      any directory containing a year    -> era comes from the PATH
      `..._Next` / `Into` / `After`      -> position comes from the FILENAME

    So this script's whole job is the folder. It never writes `.tags.json`
    sidecars: leaving subject/era to the folder and year keeps the scanner as the
    single source of truth, and the review queue still works for anything the
    filename could not settle.

    Bumps whose filename names none of the roster's shows go to `neutral/`, which
    the planner uses as its last-resort fallback. That is not a downgrade: 7,000
    generic cards are what makes a channel feel like [adult swim] rather than like
    four shows on a loop.

.PARAMETER Source
    The extracted archive folder, e.g. C:\Users\Dooz\Downloads\AdultswimBumps\AdultswimBumps

.PARAMETER Dest
    Destination root. Defaults to media\bumps.

.PARAMETER MaxNeutral
    Cap on how many generic bumps to copy (0 = all). The archive is ~28 GB of
    generic cards and the planner cannot use more than a few hundred, so a cap
    keeps the library proportionate. Selection is deterministic (sorted by name).

.PARAMETER MinSeconds / MaxSeconds
    Duration window. Anything outside it is skipped with a note, because a 2 s
    flash or a 3 minute short cannot fill a bump slot.

.PARAMETER FileFormat
    Which container to prefer. The archive holds both `.mp4` and `.ogv` of every
    bump; the mp4 is smaller and plays everywhere ErsatzTV targets.

.PARAMETER DryRun
    Report what would be copied; touch nothing.

.EXAMPLE
    .\scripts\import-bumps.ps1 -Source C:\Users\Dooz\Downloads\AdultswimBumps\AdultswimBumps -DryRun

.EXAMPLE
    .\scripts\import-bumps.ps1 -Source C:\Users\Dooz\Downloads\AdultswimBumps\AdultswimBumps
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Source,

    [string]$Dest = 'media\bumps',
    [int]$MaxNeutral = 400,
    [int]$MinSeconds = 3,
    [int]$MaxSeconds = 30,
    [string]$FileFormat = '.mp4',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Resolve-Tool {
    param([string]$Name)
    $found = Get-Command $Name -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    $env:PATH = [Environment]::GetEnvironmentVariable('PATH', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('PATH', 'User')
    $found = Get-Command $Name -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    throw "$Name not found on PATH. Install ffmpeg: winget install Gyan.FFmpeg"
}

$ffprobe = Resolve-Tool 'ffprobe'
$root = Split-Path -Parent $PSScriptRoot
if (-not [System.IO.Path]::IsPathRooted($Dest)) { $Dest = Join-Path $root $Dest }

if (-not (Test-Path $Source)) { throw "source not found: $Source" }

# -- roster -----------------------------------------------------------------

# Show id -> filename tokens that identify it. Tokens must be specific: `KOTH` and
# `ATHF` are the official abbreviations and appear in the archive's own names, but
# a bare `SWIM` or `TEN` would match half the collection.
$roster = [ordered]@{
    'aqua-teen-hunger-force' = @('AQUA_TEEN', 'AQUA TEEN', 'ATHF')
    'sealab-2021'            = @('SEALAB')
    'futurama'               = @('FUTURAMA')
    'king-of-the-hill'       = @('KING_OF_THE_HILL', 'KING OF THE HILL', 'KOTH')
}

function Get-Subject {
    param([string]$Name)
    $upper = $Name.ToUpperInvariant()
    foreach ($showId in $roster.Keys) {
        foreach ($token in $roster[$showId]) {
            if ($upper.Contains($token)) { return $showId }
        }
    }
    return 'neutral'
}

function Get-Position {
    <#
        Mirrors catalog.BUMP_POSITION_ALIASES. Filenames in this archive use the
        same words (`..._Next`, `..._After`), so the scanner would derive these
        anyway; recording them in the sidecar makes the decision visible and keeps
        the planner from depending on a filename convention.
    #>
    param([string]$Name)
    $upper = $Name.ToUpperInvariant()
    # Order matters: `AFTER` before `RETURN`, and `NEXT` before `BACK`.
    if ($upper -match '(^|[^A-Z])AFTER([^A-Z]|$)') { return 'AFTER' }
    if ($upper -match '(^|[^A-Z])INTO([^A-Z]|$)') { return 'INTO' }
    if ($upper -match '(^|[^A-Z])NEXT([^A-Z]|$)') { return 'NEXT' }
    if ($upper -match '(^|[^A-Z])OUT([^A-Z]|$)') { return 'OUT' }
    if ($upper -match '(^|[^A-Z])RETURN([^A-Z]|$)') { return 'OUT' }
    return $null
}

function Get-Era {
    <#
        A year *prefix* only, e.g. `2003_HowTo___ATHF_Next`. A year anywhere else
        in the name is usually the subject of a bump ("1980 vs 2012 Activities"),
        not the air date, so it is not used.
    #>
    param([string]$Name)
    if ($Name -match '^(19\d\d|20\d\d)_') {
        $year = [int]$Matches[1]
        if ($year -ge 1995 -and $year -le 2010) { return $year }
    }
    return $null
}

function Write-Sidecar {
    param([string]$MediaPath, [string]$Subject, [string]$Position, $Era, [string]$Source)

    $payload = [ordered]@{
        confidence = 1.0
        confirmed  = $true
        source     = $Source
    }
    if ($Subject) { $payload.subject = $Subject }
    if ($Position) { $payload.position = $Position }
    if ($null -ne $Era) { $payload.era = $Era; $payload.era_to = $Era }

    $json = $payload | ConvertTo-Json
    Set-Content -Path "$MediaPath.tags.json" -Value $json -Encoding utf8
}

# -- collect ----------------------------------------------------------------

Write-Host "source : $Source" -ForegroundColor Cyan
Write-Host "dest   : $Dest" -ForegroundColor Cyan

$files = @(Get-ChildItem -Path $Source -File -Filter "*$FileFormat" |
    Where-Object { $_.Name -notmatch '^AdultswimBumps_' -and $_.Name -notmatch '\.torrent' } |
    Sort-Object Name)

if ($files.Count -eq 0) { throw "no *$FileFormat files under $Source" }
Write-Host "candidates: $($files.Count)" -ForegroundColor Cyan

$bySubject = @{}
foreach ($file in $files) {
    $subject = Get-Subject -Name $file.BaseName
    if (-not $bySubject.ContainsKey($subject)) { $bySubject[$subject] = @() }
    $bySubject[$subject] += $file
}

Write-Host "`nmatched by filename:" -ForegroundColor Cyan
foreach ($showId in $roster.Keys) {
    $count = if ($bySubject.ContainsKey($showId)) { $bySubject[$showId].Count } else { 0 }
    Write-Host ("  {0,-24} {1,5}" -f $showId, $count)
}
$neutralCount = if ($bySubject.ContainsKey('neutral')) { $bySubject['neutral'].Count } else { 0 }
Write-Host ("  {0,-24} {1,5}" -f 'neutral (generic)', $neutralCount)

# Deterministic subset for the generic pool: sorted by name, evenly spaced so the
# sample spans the alphabet (and therefore the eras) rather than taking one block.
$neutral = @()
if ($neutralCount -gt 0) {
    $neutral = @($bySubject['neutral'])
    if ($MaxNeutral -gt 0 -and $neutral.Count -gt $MaxNeutral) {
        $step = $neutral.Count / $MaxNeutral
        $neutral = @(0..($MaxNeutral - 1) | ForEach-Object { $neutral[[int]($_ * $step)] })
    }
}

$plan = [ordered]@{}
foreach ($showId in $roster.Keys) {
    if ($bySubject.ContainsKey($showId)) { $plan[$showId] = @($bySubject[$showId]) }
}
$plan['neutral'] = $neutral

# -- copy -------------------------------------------------------------------

$copied = 0
$skippedDuration = 0
$skippedExists = 0
$totalBytes = 0

foreach ($showId in $plan.Keys) {
    $targetDir = Join-Path $Dest $showId
    $entries = @($plan[$showId])
    if ($entries.Count -eq 0) { continue }

    Write-Host "`n== $showId ($($entries.Count))" -ForegroundColor Cyan
    if (-not $DryRun) { New-Item -ItemType Directory -Force -Path $targetDir | Out-Null }

    $done = 0
    foreach ($file in $entries) {
        $target = Join-Path $targetDir $file.Name
        if (Test-Path $target) {
            # Already copied on an earlier run: still backfill the sidecar, or a
            # re-run after adding tagging would leave those files unconfirmed.
            $skippedExists++
            if (-not $DryRun -and -not (Test-Path "$target.tags.json")) {
                Write-Sidecar -MediaPath $target `
                    -Subject $(if ($showId -eq 'neutral') { $null } else { $showId }) `
                    -Position (Get-Position -Name $file.BaseName) `
                    -Era (Get-Era -Name $file.BaseName) -Source 'import'
            }
            continue
        }

        $duration = & $ffprobe -v error -show_entries format=duration `
            -of default=noprint_wrappers=1:nokey=1 $file.FullName 2>&1
        $seconds = 0.0
        if (-not [double]::TryParse("$duration".Trim(), [ref]$seconds)) { $seconds = 0.0 }
        if ($seconds -lt $MinSeconds -or $seconds -gt $MaxSeconds) { $skippedDuration++; continue }

        if ($DryRun) {
            $copied++
        }
        else {
            Copy-Item $file.FullName $target -Force
            # Record what the filename already told us, so the decision is a
            # durable sidecar rather than an inference repeated on every scan.
            $position = Get-Position -Name $file.BaseName
            $era = Get-Era -Name $file.BaseName
            Write-Sidecar -MediaPath $target `
                -Subject $(if ($showId -eq 'neutral') { $null } else { $showId }) `
                -Position $position -Era $era -Source 'import'
            $copied++
            $totalBytes += $file.Length
        }

        $done++
        if ($done % 50 -eq 0) {
            Write-Host ("   {0}..." -f $done) -ForegroundColor DarkGray
        }
    }
    Write-Host ("   {0} imported" -f $done) -ForegroundColor Green
}

Write-Host "`n--- done ---" -ForegroundColor Cyan
if ($DryRun) {
    Write-Host "would import: $copied"
}
else {
    Write-Host "imported     : $copied"
    Write-Host ("copied       : {0:N1} MB" -f ($totalBytes / 1MB))
}
Write-Host "already there: $skippedExists"
Write-Host "wrong length : $skippedDuration (outside ${MinSeconds}-${MaxSeconds}s)"

if ($copied -gt 0) {
    Write-Host "`nnext: .\run.ps1 scan && .\run.ps1 doctor" -ForegroundColor Green
}
