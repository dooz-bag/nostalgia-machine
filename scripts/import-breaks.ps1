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

.PARAMETER FromFolder
    Process break tapes you already downloaded (JDownloader, a browser, anything)
    instead of fetching them: only the split-and-file step runs.

    The year is taken from each filename or its folder, so
    `October 2003 break.mp4` lands in 2003. Use -Year to force one folder when the
    names carry no year.

.EXAMPLE
    .\scripts\import-breaks.ps1 -FromFolder D:\downloads\breaks

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

    [Parameter(ParameterSetName = 'FromFolder', Mandatory = $true)]
    [string]$FromFolder,

    [int]$Year,
    [string]$Dest = 'media\commercials',
    [int]$Limit = 0,
    [int]$MinSeconds = 8,
    [int]$MaxSeconds = 65,
    #: Skip any item whose only video file is larger than this. Full recordings
    #: ("Red Heat, Partial Movie with Commercials") are 2 GB and yield a handful of
    #: spots, which is a bad trade when a 20 MB break tape yields as many.
    [int]$MaxSourceMB = 400,
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
    <#
        Best guess at the *content* year, not the upload year.

        Titles are checked first and `publicdate` is checked last on purpose: an
        item titled "Super Cartoon Sunday (Break #2, 1984)" otherwise landed in a
        2007 folder, because that is when someone uploaded it. For a channel whose
        whole point is period accuracy, filing a 1984 tape as 2007 is worse than
        skipping the item.
    #>
    param($Metadata)
    $meta = Get-Field $Metadata 'metadata'

    foreach ($field in 'title', 'year', 'date') {
        $value = Get-Field $meta $field
        if ($value -and "$value" -match '\b(19[5-9]\d|20[0-4]\d)\b') { return [int]$Matches[1] }
    }
    # Last resort only: an upload date says nothing trustworthy about the content.
    $value = Get-Field $meta 'publicdate'
    if ($value -and "$value" -match '\b(19[5-9]\d|20[0-4]\d)\b') { return [int]$Matches[1] }
    return $null
}

function Save-Remote {
    <#
        Download with parallel HTTP range requests.

        archive.org throttles *per connection* at roughly 0.4 MB/s regardless of
        available bandwidth, so a single stream makes a multi-GB import take hours.
        Six concurrent ranges measured ~1.1 MB/s, a 3x speed-up for the same file.

        Invoke-WebRequest -OutFile is also avoided on purpose: it buffers in memory,
        so the target file sits at 0 bytes for the whole transfer and the only way
        to see progress is to watch RAM.
    #>
    param([string]$Url, [string]$Target, [int]$Streams = 4)

    if ((Test-Path $Target) -and (Get-Item $Target).Length -gt 0) { return $Target }

    # How big is it, and does it support ranges?
    $head = curl.exe -sIL -o NUL -w '%{http_code} %{size_download} %{header_json}' --max-time 60 $Url 2>&1
    $size = 0
    $rangeOk = $false
    $headText = ($head | Out-String)
    if ($headText -match '"content-length"\s*:\s*\[\s*"(\d+)"') { $size = [int64]$Matches[1] }
    if ($headText -match '"accept-ranges"\s*:\s*\[\s*"bytes"') { $rangeOk = $true }

    if ($size -le 0 -or -not $rangeOk -or $Streams -le 1) {
        # Plain sequential download.
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            curl.exe -s -L --retry 2 --retry-delay 2 --max-time 1800 -o $Target $Url
            if ((Test-Path $Target) -and (Get-Item $Target).Length -gt 0) { return $Target }
            Start-Sleep -Seconds (2 * $attempt)
        }
        throw "download failed after 3 tries: $Url"
    }

    $chunk = [int64][Math]::Ceiling($size / $Streams)
    $parts = @()
    $jobs = @()
    for ($i = 0; $i -lt $Streams; $i++) {
        $start = $i * $chunk
        $end = [Math]::Min($start + $chunk - 1, $size - 1)
        if ($start -gt $end) { continue }
        $partPath = "$Target.part$i"
        $parts += $partPath
        $jobs += Start-Job -ScriptBlock {
            param($srcUrl, $rangeStart, $rangeEnd, $outPath)
            # Careful with parameter names here: PowerShell splats an array after a
            # native command, so a variable called $u would be rendered as -u and
            # swallow the URL.
            & curl.exe -s -L --retry 3 --retry-delay 2 --max-time 1800 `
                -r "${rangeStart}-${rangeEnd}" -o $outPath $srcUrl
            if (Test-Path $outPath) { (Get-Item $outPath).Length } else { 0 }
        } -ArgumentList $Url, $start, $end, $partPath
    }

    try {
        $null = $jobs | Wait-Job -Timeout 3600
        $sizes = @($jobs | Receive-Job)
        $got = ($sizes | Measure-Object -Sum).Sum
        if ($got -lt $size) {
            throw "incomplete download: got $got of $size bytes"
        }
    }
    finally {
        $jobs | Remove-Job -Force -ErrorAction SilentlyContinue
    }

    # Concatenate in order.
    $out = [System.IO.File]::Create($Target)
    try {
        foreach ($partPath in $parts) {
            $in = [System.IO.File]::OpenRead($partPath)
            try { $in.CopyTo($out) } finally { $in.Dispose() }
        }
    }
    finally { $out.Dispose() }
    $parts | ForEach-Object { Remove-Item $_ -Force -ErrorAction SilentlyContinue }
    return $Target
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
#: A segment longer than this is a promo or an in-house segment, not a spot. It is
#: still useful ("we now return to..." cards live here) but must not be emitted as
#: a commercial, so it is skipped rather than truncated.
$tailTolerance = 6.0

function Get-Spots {
    param([double[]]$Cuts, [double]$Duration)

    $spots = [System.Collections.Generic.List[object]]::new()
    $cursor = 0.0
    $guard = 0

    while ($cursor -lt ($Duration - $MinSeconds) -and $guard -lt 500) {
        $guard++
        $remaining = $Duration - $cursor

        # Whatever is left already looks like a single spot: take it whole. The
        # upper bound allows a little over 60s so a 63s promo is kept rather than
        # thrown away for being four seconds long.
        if ($remaining -le ($MaxSeconds + $tailTolerance)) {
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
            # Nothing lines up with 15/30/60s. Sports and news breaks break this:
            # a long static graphic can run 60+ seconds with no cut inside a spot
            # length, so strict snapping stops dead and throws the rest away.
            # Fall back to the cut that yields a length *within* the acceptable
            # range, preferring one close to a real spot length.
            $fallback = $null
            $fallbackScore = [double]::MaxValue
            foreach ($cut in $Cuts) {
                if ($cut -le ($cursor + $MinSeconds)) { continue }
                $length = $cut - $cursor
                if ($length -gt ($MaxSeconds + $tailTolerance)) { continue }
                # Distance from the nearest ideal spot length.
                $score = [double]::MaxValue
                foreach ($target in $spotTargets) { $score = [Math]::Min($score, [Math]::Abs($length - $target)) }
                if ($score -lt $fallbackScore) { $fallbackScore = $score; $fallback = $cut }
            }
            if ($null -ne $fallback) {
                $spots.Add([pscustomobject]@{ Start = $cursor; Length = ($fallback - $cursor) })
                $cursor = $fallback
                continue
            }

            # Genuinely nothing usable: keep the remainder only if it is a sane
            # length, otherwise stop rather than emit a four-minute "commercial".
            if ($remaining -ge $MinSeconds -and $remaining -le ($MaxSeconds + $tailTolerance)) {
                $spots.Add([pscustomobject]@{ Start = $cursor; Length = $remaining })
            }
            break
        }

        $spots.Add([pscustomobject]@{ Start = $cursor; Length = ($best - $cursor) })
        $cursor = $best
    }
    return , $spots
}

# -- main -------------------------------------------------------------------

function Split-Source {
    <#
        Cut one break tape into spots and file them under *YearDir*.

        Shared by the download path and -FromFolder, so a tape you fetched with
        JDownloader is processed exactly like one this script fetched itself.
    #>
    param([string]$Source, [string]$Slug, [string]$YearDir, [double]$Duration)

    $cuts = Get-SceneCuts -Source $Source -Threshold $SceneThreshold -DurationSeconds $Duration
    $spots = Get-Spots -Cuts $cuts.ToArray() -Duration $Duration
    $covered = 0.0
    foreach ($spot in $spots) { $covered += $spot.Length }
    Write-Host ('   {0} shot cut(s) -> {1} spot(s), {2:N0}s of tape left over' -f `
        $cuts.Count, $spots.Count, ($Duration - $covered)) -ForegroundColor DarkGray

    $kept = 0
    $index = 0
    foreach ($spot in $spots) {
        $index++
        $target = Join-Path $YearDir ('{0}-{1:d3}.mp4' -f $Slug, $index)
        if (Test-Path $target) { $kept++; continue }
        # Re-encode instead of stream-copying: a keyframe cut leaves each spot up
        # to a GOP short, and the planner trusts the real duration.
        & $ffmpeg -hide_banner -nostdin -loglevel error -ss ('{0:F3}' -f $spot.Start) `
            -t ('{0:F3}' -f $spot.Length) -i $Source `
            -c:v libx264 -preset veryfast -crf 20 -c:a aac -b:a 128k `
            -movflags +faststart $target
        if ($LASTEXITCODE -eq 0 -and (Test-Path $target)) { $kept++ }
    }
    return $kept
}

function Get-YearFromName {
    param([string]$Text)
    if ($Text -match '\b(19[5-9]\d|20[0-4]\d)\b') { return [int]$Matches[1] }
    return 0
}

function Convert-Folder {
    <#
        Split break tapes that are already on disk.

        JDownloader (and a browser, and curl -r) fetch faster than this script's
        range requests, so the useful split of labour is: let them download, and
        use this for the part they cannot do -- cutting a 3-minute break into
        individual 15/30/60-second spots.
    #>
    param([string]$Folder, [int]$ForcedYear)

    if (-not (Test-Path $Folder)) { throw "folder not found: $Folder" }
    $sources = @(Get-ChildItem -Path $Folder -Recurse -File |
        Where-Object { $_.Extension -match '^\.(mp4|mkv|webm|avi|m4v|mpg|mpeg|ogv)$' } |
        Sort-Object Name)
    if ($sources.Count -eq 0) { throw "no video files under $Folder" }

    Write-Host "files to split: $($sources.Count)" -ForegroundColor Cyan
    $total = 0
    foreach ($file in $sources) {
        $year = if ($ForcedYear -gt 0) { $ForcedYear } else { Get-YearFromName $file.FullName }
        Write-Host "`n== $($file.Name)" -ForegroundColor Cyan
        if ($year -le 0) {
            Write-Warning '   no year in the name or path; skipping (use -Year to force one)'
            continue
        }
        $duration = Get-ProbeDuration $file.FullName
        if ($duration -le 0) { Write-Warning '   cannot probe duration; skipping'; continue }
        Write-Host ('   {0:N1}s -> era {1}' -f $duration, $year) -ForegroundColor DarkGray

        $yearDir = Join-Path $Dest "$year"
        New-Item -ItemType Directory -Force -Path $yearDir | Out-Null
        $slug = ($file.BaseName -replace '[^a-zA-Z0-9\-_]', '-').ToLower()

        if ($DryRun) {
            Write-Host "   would split into $yearDir" -ForegroundColor Yellow
            continue
        }

        if ($duration -le $MaxSeconds) {
            $target = Join-Path $yearDir "$slug.mp4"
            if (-not (Test-Path $target)) {
                & $ffmpeg -hide_banner -nostdin -loglevel error -i $file.FullName `
                    -c:v libx264 -preset veryfast -crf 20 -c:a aac -b:a 128k `
                    -movflags +faststart $target
            }
            $total += 1
            Write-Host '   + 1 spot (already single)' -ForegroundColor Green
            continue
        }

        $kept = Split-Source -Source $file.FullName -Slug $slug -YearDir $yearDir -Duration $duration
        $total += $kept
        Write-Host "   + $kept spot(s) into $year" -ForegroundColor Green
    }
    return $total
}

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

    # Prefer a smaller derivative if the item has one; skip the item entirely if
    # every option is a full-length recording.
    $file = $null
    foreach ($candidate in $videos) {
        if (([int64]$candidate.size / 1MB) -le $MaxSourceMB) { $file = $candidate; break }
    }
    if (-not $file) {
        $smallest = [math]::Round(([int64]$videos[0].size / 1MB), 0)
        Write-Host "   skipped: smallest file is ${smallest} MB (limit ${MaxSourceMB} MB)" -ForegroundColor DarkYellow
        return 0
    }
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

    $kept = Split-Source -Source $local -Slug $slug -YearDir $yearDir -Duration $duration

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

if ($PSCmdlet.ParameterSetName -eq 'FromFolder') {
    # Already-downloaded tape: no fetching, just split and file.
    Write-Host "source folder: $FromFolder" -ForegroundColor Cyan
    Write-Host "destination: $Dest" -ForegroundColor Cyan
    $written = Convert-Folder -Folder $FromFolder -ForcedYear $Year
    Write-Host "`n--- done ---" -ForegroundColor Cyan
    Write-Host "spots written: $written"
    if ($written -gt 0) {
        Write-Host 'next: .\run.ps1 scan && .\run.ps1 plan --date 2003-06-16' -ForegroundColor Green
    }
    return
}

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
