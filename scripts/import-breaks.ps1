<#
.SYNOPSIS
    Download vintage commercial breaks from the Internet Archive and split them
    into individual spots the planner can actually use.

.DESCRIPTION
    A recorded commercial break is one long file, but `nostalgia plan` needs
    single spots to fill 15/30-second pods. This script bridges the two:

      1. resolve archive.org identifiers (from -Query, -IdFile or -Id)
      2. download the item's video file
      3. find scene cuts with ffmpeg
      4. keep only the segments whose length looks like a real spot
      5. write them to media/commercials/<year>/, which is what tags the era

    The year folder matters: `catalog._era_from_path` reads the year out of the
    path, so `media/commercials/2003/foo.mp4` becomes era 2003 and
    `ads.era_drift_years` can then decide whether it may air on a 2004 day.

    Nothing is downloaded from anywhere except archive.org, and files you already
    have are skipped, so re-running is cheap.

.PARAMETER Query
    An archive.org search query, e.g. 'title:"commercial break" AND year:[2001 TO 2005]'.

.PARAMETER IdFile
    A text file of archive.org identifiers, one per line (@'...'@ or `ia search --itemlist`).

.PARAMETER Id
    One or more archive.org identifiers given directly.

.PARAMETER Year
    Force the era folder instead of using the item's own date.

.PARAMETER Dest
    Destination root. Defaults to media\commercials.

.PARAMETER Limit
    Stop after N items (useful for a first trial run).

.PARAMETER KeepSource
    Keep the downloaded full-length file in $Dest\_source.

.PARAMETER DryRun
    Print what would happen; download and encode nothing.

.EXAMPLE
    .\scripts\import-breaks.ps1 -DryRun -Limit 5 `
        -Query 'title:"commercial break" AND year:[2002 TO 2004]'

.EXAMPLE
    .\scripts\import-breaks.ps1 -Id randomcartoonnetworkcommercialsvol1september2000

.EXAMPLE
    .\scripts\import-breaks.ps1 -IdFile breaks.txt -Dest D:\media\commercials
#>
[CmdletBinding(DefaultParameterSetName = 'Query')]
param(
    [Parameter(ParameterSetName = 'Query', Mandatory = $true)]
    [string]$Query,

    [Parameter(ParameterSetName = 'IdFile', Mandatory = $true)]
    [string]$IdFile,

    [Parameter(ParameterSetName = 'Id', Mandatory = $true)]
    [string[]]$Id,

    [int]$Year,
    [string]$Dest = 'media\commercials',
    [int]$Limit = 0,
    [int]$MinSeconds = 8,
    [int]$MaxSeconds = 65,
    [double]$SceneThreshold = 0.32,
    [switch]$KeepSource,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# -- tool discovery ---------------------------------------------------------

function Resolve-Tool {
    param([string]$Name)
    # A winget install only updates *new* processes, so refresh PATH once.
    $found = Get-Command $Name -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    $machine = [Environment]::GetEnvironmentVariable('PATH', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('PATH', 'User')
    $env:PATH = "$machine;$user"
    $found = Get-Command $Name -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    throw "$Name not found on PATH. Install ffmpeg: winget install Gyan.FFmpeg"
}

$ffmpeg = Resolve-Tool 'ffmpeg'
$ffprobe = Resolve-Tool 'ffprobe'

$root = Split-Path -Parent $PSScriptRoot
if (-not [System.IO.Path]::IsPathRooted($Dest)) { $Dest = Join-Path $root $Dest }
$work = Join-Path ([System.IO.Path]::GetTempPath()) 'nostalgia-import'
New-Item -ItemType Directory -Force -Path $work | Out-Null

# -- step 1: identifiers ----------------------------------------------------

function Get-Identifiers {
    if ($PSCmdlet.ParameterSetName -eq 'Id') { return $Id }
    if ($PSCmdlet.ParameterSetName -eq 'IdFile') {
        if (-not (Test-Path $IdFile)) { throw "id file not found: $IdFile" }
        return @(Get-Content $IdFile | Where-Object { $_.Trim() -and -not $_.StartsWith('#') } | ForEach-Object { $_.Trim() })
    }
    # The search API caps rows at 10000; paginate so a broad query still works.
    $ids = [System.Collections.Generic.List[string]]::new()
    $page = 1
    $perPage = 100
    do {
        $fields = ($perPage * ($page - 1))
        # Parenthesise the concatenation: without the brackets, -f binds to the
        # second string only and the URL is left with a literal {0}.
        $url = ('https://archive.org/advancedsearch.php?q={0}&fl%5B%5D=identifier' +
                '&rows={1}&page={2}&output=json') -f `
                [uri]::EscapeDataString($Query), $perPage, $page
        $response = Invoke-RestMethod -Uri $url -TimeoutSec 60
        $body = Get-Field $response 'response'
        if ($null -eq $body) { throw "unexpected search response for: $Query" }
        $docs = @(Get-Field $body 'docs')
        if ($docs.Count -eq 0) { break }
        foreach ($doc in $docs) { $ids.Add((Get-Field $doc 'identifier')) }
        if ($Limit -gt 0 -and $ids.Count -ge $Limit) { break }
        $page++
        $found = [int](Get-Field $body 'numFound')
    } while ($fields -lt $found -and $page -le 100)
    if ($Limit -gt 0 -and $ids.Count -gt $Limit) { return @($ids[0..($Limit - 1)]) }
    return @($ids)
}

# -- step 2: pick a downloadable file --------------------------------------

$videoPattern = '\.(mp4|mkv|webm|avi|m4v|mpg|mpeg|ogv)$'

function Get-Field {
    param($Object, [string]$Name)
    # Set-StrictMode throws on a missing property, and archive.org metadata is
    # inconsistent: some items have `year`, most only `date`.
    if ($null -eq $Object) { return $null }
    # Filter by name rather than indexing Properties[$Name]: the indexer does not
    # reliably match on PSCustomObject coming out of ConvertFrom-Json.
    $property = $Object.PSObject.Properties | Where-Object { $_.Name -eq $Name } | Select-Object -First 1
    if ($property) { return $property.Value }
    return $null
}

function Get-VideoFiles {
    param($Metadata)
    # Prefer the smallest h.264/mp4 derivative: these are old tapes, and a 200 MB
    # "MPEG4" original is rarely better than the 16 MB derivative for a 480i spot.
    $files = @($Metadata.files | Where-Object { $_.name -match $videoPattern -and [int64]$_.size -gt 200KB })
    if ($files.Count -eq 0) { return @() }
    $ranked = $files | Sort-Object @{
        Expression = {
            $name = $_.name.ToLower()
            $score = 0
            if ($name -match '\.mp4$') { $score -= 1000 }
            if ($name -match '512kb|h\.264') { $score -= 100 }
            if ($name -match 'ia\.mp4$') { $score -= 10 }
            $score
        }
    }, @{ Expression = { [int64]$_.size } }
    return @($ranked)
}

function Get-ItemYear {
    param($Metadata)
    # Archive.org is inconsistent: some items carry `year`, many only `date`.
    $meta = Get-Field $Metadata 'metadata'
    foreach ($field in 'year', 'date', 'publicdate') {
        $value = Get-Field $meta $field
        if ($value -and "$value" -match '\b(19[5-9]\d|20[0-4]\d)\b') { return [int]$Matches[1] }
    }
    return $null
}

function Save-Remote {
    param([string]$Url, [string]$Target)
    if (Test-Path $Target) {
        if ((Get-Item $Target).Length -gt 0) { return $Target }
    }
    $attempt = 0
    while ($true) {
        $attempt++
        try {
            # Archive.org redirects to a node; -UseBasicParsing keeps it cheap on
            # Windows PowerShell, and Invoke-WebRequest follows the 302 for us.
            Invoke-WebRequest -Uri $Url -OutFile $Target -UseBasicParsing -TimeoutSec 600
            return $Target
        }
        catch {
            if ($attempt -ge 3) { throw "download failed after 3 tries: $Url`n$($_.Exception.Message)" }
            Start-Sleep -Seconds (2 * $attempt)
        }
    }
}

# -- step 3: find cut points ------------------------------------------------

function Get-SceneCuts {
    param([string]$Source, [double]$Threshold, [double]$DurationSeconds)
    $log = & $ffmpeg -hide_banner -nostdin -i $Source -filter:v "select='gt(scene\,$Threshold)',showinfo" -an -f null NUL 2>&1
    $cuts = [System.Collections.Generic.List[double]]::new()
    $cuts.Add(0.0)
    foreach ($line in $log) {
        if ($line -match 'pts_time:([0-9]+\.?[0-9]*)') {
            $t = [double]$Matches[1]
            if ($t -gt 0.05 -and $t -lt ($DurationSeconds - 0.05)) { $cuts.Add($t) }
        }
    }
    $sorted = $cuts | Sort-Object -Unique
    $result = [System.Collections.Generic.List[double]]::new()
    foreach ($t in $sorted) { $result.Add($t) }
    return , $result
}

function Get-ProbeDuration {
    param([string]$Path)
    $value = & $ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 $Path 2>&1
    $parsed = 0.0
    if ([double]::TryParse("$value".Trim(), [ref]$parsed)) { return $parsed }
    return 0.0
}

# Real spots are 15, 30 or 60 seconds. A break tape also has a cut at every shot
# change *inside* those spots, so segments cannot be taken raw -- snapping each
# boundary to the nearest spot length is what recovers the actual commercials.
$spotTargets = @(15, 30, 60)
$snapTolerance = 4.5

function Get-Spots {
    param([double[]]$Cuts, [double]$Duration)

    $spots = [System.Collections.Generic.List[object]]::new()
    $cursor = 0.0
    $guard = 0

    while ($cursor -lt ($Duration - $MinSeconds) -and $guard -lt 500) {
        $guard++
        $remaining = $Duration - $cursor

        # Whatever is left already looks like a single spot: take it whole.
        if ($remaining -le $MaxSeconds) {
            if ($remaining -ge $MinSeconds) {
                $spots.Add([pscustomobject]@{ Start = $cursor; Length = $remaining })
            }
            break
        }

        # Otherwise pick the cut nearest an ideal spot boundary from here.
        $best = $null
        $bestError = [double]::MaxValue
        foreach ($target in $spotTargets) {
            if ($target -gt ($remaining - $MinSeconds)) { continue }
            $ideal = $cursor + $target
            foreach ($cut in $Cuts) {
                if ($cut -le ($cursor + $MinSeconds)) { continue }
                $error = [Math]::Abs($cut - $ideal)
                if ($error -le $snapTolerance -and $error -lt $bestError) {
                    $bestError = $error
                    $best = $cut
                }
            }
        }

        if ($null -eq $best) {
            # No boundary is close enough to a spot length. Emit the remainder as
            # one spot if it fits, otherwise stop rather than guess.
            if ($remaining -le ($MaxSeconds + $snapTolerance)) {
                $spots.Add([pscustomobject]@{ Start = $cursor; Length = [Math]::Min($remaining, $MaxSeconds) })
            }
            break
        }

        $spots.Add([pscustomobject]@{ Start = $cursor; Length = ($best - $cursor) })
        $cursor = $best
    }
    return , $spots
}

# -- main -------------------------------------------------------------------

function Convert-Item {
    param([string]$Identifier, [int]$ForcedYear)

    Write-Host "`n== $Identifier" -ForegroundColor Cyan
    $metadata = Invoke-RestMethod -Uri "https://archive.org/metadata/$Identifier" -TimeoutSec 60
    $title = Get-Field (Get-Field $metadata 'metadata') 'title'
    if ($title) { Write-Host "   $title" -ForegroundColor DarkGray }

    $year = if ($PSBoundParameters.ContainsKey('ForcedYear') -and $ForcedYear -gt 0) { $ForcedYear } else { Get-ItemYear $metadata }
    if (-not $year) {
        Write-Warning "   no year in metadata; skipping (use -Year to override)"
        return 0
    }
    if ($PSBoundParameters.ContainsKey('ForcedYear') -and $ForcedYear -gt 0) { $year = $ForcedYear }

    $videos = Get-VideoFiles $metadata
    if ($videos.Count -eq 0) { Write-Warning '   no video file'; return 0 }
    $file = $videos[0]
    Write-Host ('   file: {0} ({1:N1} MB)' -f $file.name, ([int64]$file.size / 1MB)) -ForegroundColor DarkGray

    $yearDir = Join-Path $Dest "$year"
    New-Item -ItemType Directory -Force -Path $yearDir | Out-Null

    if ($DryRun) {
        Write-Host "   would download to $yearDir" -ForegroundColor Yellow
        return 0
    }

    $slug = ($Identifier -replace '[^a-zA-Z0-9\-_]', '-').ToLower()
    $local = Join-Path $work "$slug$([System.IO.Path]::GetExtension($file.name))"
    $url = "https://archive.org/download/$Identifier/$([uri]::EscapeDataString($file.name))"
    Save-Remote -Url $url -Target $local | Out-Null

    $duration = Get-ProbeDuration $local
    if ($duration -le 0) { Write-Warning "   could not probe duration; skipping"; return 0 }
    Write-Host ('   duration: {0:N1}s' -f $duration) -ForegroundColor DarkGray

    # Short enough to be a single spot already? Keep it whole.
    if ($duration -le $MaxSeconds) {
        $target = Join-Path $yearDir "$slug.mp4"
        if (Test-Path $target) { Write-Host "   already have $([System.IO.Path]::GetFileName($target))" -ForegroundColor DarkGray }
        else {
            & $ffmpeg -hide_banner -nostdin -loglevel error -i $local -c copy -movflags +faststart $target
            Write-Host "   + 1 spot (already single)" -ForegroundColor Green
        }
        if (-not $KeepSource) { Remove-Item $local -Force -ErrorAction SilentlyContinue }
        return 1
    }

    # Otherwise snap cut points to real spot lengths and cut on those.
    $cuts = Get-SceneCuts -Source $local -Threshold $SceneThreshold -DurationSeconds $duration
    $spots = Get-Spots -Cuts $cuts.ToArray() -Duration $duration
    Write-Host ('   {0} shot cut(s) -> {1} spot(s)' -f $cuts.Count, $spots.Count) -ForegroundColor DarkGray

    $kept = 0
    $index = 0
    foreach ($spot in $spots) {
        $index++
        $target = Join-Path $yearDir ('{0}-{1:d3}.mp4' -f $slug, $index)
        if (Test-Path $target) { $kept++; continue }
        # Re-encode instead of stream-copying: a keyframe cut leaves each spot up
        # to a GOP short, and the planner trusts the real duration. These are
        # 480i-era tapes, so this is cheap.
        & $ffmpeg -hide_banner -nostdin -loglevel error -ss ('{0:F3}' -f $spot.Start) `
            -t ('{0:F3}' -f $spot.Length) -i $local `
            -c:v libx264 -preset veryfast -crf 20 -c:a aac -b:a 128k `
            -movflags +faststart $target
        if ($LASTEXITCODE -eq 0 -and (Test-Path $target)) { $kept++ }
    }

    if ($KeepSource) {
        $keepDir = Join-Path $Dest '_source'
        New-Item -ItemType Directory -Force -Path $keepDir | Out-Null
        Copy-Item $local (Join-Path $keepDir (Split-Path -Leaf $local)) -Force
    }
    Remove-Item $local -Force -ErrorAction SilentlyContinue

    Write-Host "   + $kept spot(s) into $year" -ForegroundColor Green
    if ($kept -eq 0) { Write-Warning "   no segment landed between $MinSeconds and ${MaxSeconds}s; try a lower -SceneThreshold" }
    return $kept
}

# -- run --------------------------------------------------------------------

# Wrap in @() so a single identifier is still an array: Set-StrictMode turns a
# bare string's .Count into a hard error.
$identifiers = @(Get-Identifiers)
if ($identifiers.Count -eq 0) { throw 'no identifiers matched' }
Write-Host "items to process: $($identifiers.Count)" -ForegroundColor Cyan
Write-Host "destination: $Dest" -ForegroundColor Cyan

$total = 0
$failed = @()
foreach ($identifier in $identifiers) {
    try {
        if ($Year -gt 0) { $total += (Convert-Item -Identifier $identifier -ForcedYear $Year) }
        else { $total += (Convert-Item -Identifier $identifier) }
    }
    catch {
        Write-Warning "   failed: $($_.Exception.Message)"
        $failed += $identifier
    }
    Start-Sleep -Milliseconds 250   # be polite to archive.org
}

Write-Host "`n--- done ---" -ForegroundColor Cyan
Write-Host "spots written: $total"
if ($failed.Count -gt 0) {
    Write-Host "failed items: $($failed -join ', ')" -ForegroundColor Yellow
}
if ($total -gt 0) {
    Write-Host "next: .\run.ps1 scan && .\run.ps1 plan --date 2003-06-16" -ForegroundColor Green
}
