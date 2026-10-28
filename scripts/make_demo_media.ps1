<#
.SYNOPSIS
    Generate a small, self-contained demo library with real video files.

.DESCRIPTION
    Everything here is produced by ffmpeg from generated sources, so no
    copyrighted material is involved and the repo stays small. It exists to prove
    the whole pipeline works end to end: probe -> plan -> playout -> playback.

    Durations mirror the real thing (11-minute shorts in 15-minute slots,
    22-minute halves in 30-minute slots) so the budget maths is exercised
    properly. Pass -Fast for 5-minute sources, which is enough to see playback
    working without a long encode.

.EXAMPLE
    .\scripts\make_demo_media.ps1 -Fast
#>
[CmdletBinding()]
param(
    [string]$Root = (Split-Path $PSScriptRoot -Parent),
    [switch]$Fast,
    [switch]$KeepExisting
)

$ErrorActionPreference = 'Stop'

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    throw "ffmpeg not found on PATH. Install it first: winget install Gyan.FFmpeg"
}

$media = Join-Path $Root 'media'
if ((Test-Path $media) -and -not $KeepExisting) {
    Write-Host "removing existing media\ ..." -ForegroundColor Yellow
    Remove-Item $media -Recurse -Force
}

# Font paths inside an ffmpeg filter need the drive colon escaped.
$fontRegular = "C\:/Windows/Fonts/arial.ttf"
$fontBold = "C\:/Windows/Fonts/arialbd.ttf"

$shows = @(
    @{ Id = 'aqua-teen-hunger-force'; Title = 'Aqua Teen Hunger Force'; Seconds = 660;  Fast = 300; Colour = '0x1a3a5c'; Seasons = 2; Episodes = 4 }
    @{ Id = 'sealab-2021';          Title = 'Sealab 2021';          Seconds = 660;  Fast = 300; Colour = '0x1c4a3a'; Seasons = 1; Episodes = 4 }
    @{ Id = 'futurama';             Title = 'Futurama';             Seconds = 1320; Fast = 600; Colour = '0x4a1c2a'; Seasons = 2; Episodes = 3 }
    @{ Id = 'king-of-the-hill';     Title = 'King of the Hill';     Seconds = 1320; Fast = 600; Colour = '0x3a3a1c'; Seasons = 1; Episodes = 3 }
)

$total = 0
foreach ($show in $shows) {
    $duration = if ($Fast) { $show.Fast } else { $show.Seconds }
    for ($season = 1; $season -le $show.Seasons; $season++) {
        for ($episode = 1; $episode -le $show.Episodes; $episode++) {
            $dir = Join-Path $media ("shows\{0}\Season {1:D2}" -f $show.Id, $season)
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
            $target = Join-Path $dir ("{0} - S{1:D2}E{2:D2}.mkv" -f $show.Id, $season, $episode)
            if ((Test-Path $target) -and $KeepExisting) { continue }

            $label = "{0} S{1:D2}E{2:D2}" -f $show.Title, $season, $episode
            # A slowly ticking frame counter makes it obvious the stream is live
            # without needing the pts expression (whose escaping is fragile).
            $vf = "drawtext=text='$label':fontfile='$fontBold':fontcolor=white:fontsize=26:x=(w-text_w)/2:y=(h-text_h)/2," +
                  "drawtext=text='frame %{n}':fontfile='$fontRegular':fontcolor=0xcccccc:fontsize=18:x=(w-text_w)/2:y=h-40"

            & ffmpeg -hide_banner -loglevel error -y `
                -f lavfi -i "color=c=$($show.Colour):s=480x360:r=10:d=$duration" `
                -f lavfi -i "sine=frequency=420:sample_rate=48000:duration=$duration" `
                -vf $vf `
                -c:v libx264 -preset ultrafast -crf 34 -pix_fmt yuv420p `
                -c:a aac -b:a 48k -ac 2 -shortest $target
            if ($LASTEXITCODE -ne 0) { throw "ffmpeg failed for $target" }
            $total++
            Write-Host ("  {0}" -f (Split-Path $target -Leaf)) -ForegroundColor DarkGray
        }
    }
}
Write-Host "wrote $total episode(s)" -ForegroundColor Green

# Bumps: black cards, one folder per show, named by position.
$bumpSeconds = 4
$bumpCount = 0
foreach ($show in $shows) {
    foreach ($position in @('into', 'after', 'next')) {
        $dir = Join-Path $media ("bumps\{0}" -f $show.Id)
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $target = Join-Path $dir "$position-01.mkv"
        if ((Test-Path $target) -and $KeepExisting) { continue }
        # `after` sits where the episode has already ended, so it must not promise
        # a return -- "we now return to" belongs to the mid-break (`out`) card.
        $word = switch ($position) {
            'into' { 'STAY TUNED' }
            'after' { 'YOU JUST WATCHED' }
            'next' { 'NEXT' }
        }
        $vf = "drawtext=text='[ADULT SWIM]':fontfile='$fontRegular':fontcolor=white:fontsize=20:x=60:y=150," +
              "drawtext=text='$word':fontfile='$fontRegular':fontcolor=white:fontsize=15:x=60:y=185," +
              "drawtext=text='$($show.Title)':fontfile='$fontBold':fontcolor=white:fontsize=22:x=60:y=212"
        & ffmpeg -hide_banner -loglevel error -y `
            -f lavfi -i "color=c=black:s=480x360:r=10:d=$bumpSeconds" `
            -f lavfi -i "anullsrc=channel_layout=stereo:sample_rate=48000:d=$bumpSeconds" `
            -vf $vf -c:v libx264 -preset ultrafast -crf 30 -pix_fmt yuv420p `
            -c:a aac -b:a 48k -shortest $target
        if ($LASTEXITCODE -ne 0) { throw "ffmpeg failed for $target" }
        $bumpCount++
    }
}

# Neutral bumps: no show named.
$neutralDir = Join-Path $media 'bumps\neutral'
New-Item -ItemType Directory -Force -Path $neutralDir | Out-Null
for ($i = 1; $i -le 4; $i++) {
    $target = Join-Path $neutralDir ("card-{0:D2}.mkv" -f $i)
    if ((Test-Path $target) -and $KeepExisting) { continue }
    & ffmpeg -hide_banner -loglevel error -y `
        -f lavfi -i "color=c=black:s=480x360:r=10:d=$bumpSeconds" `
        -f lavfi -i "anullsrc=channel_layout=stereo:sample_rate=48000:d=$bumpSeconds" `
        -vf "drawtext=text='[ADULT SWIM]':fontfile='$fontRegular':fontcolor=white:fontsize=20:x=60:y=160" `
        -c:v libx264 -preset ultrafast -crf 30 -pix_fmt yuv420p `
        -c:a aac -b:a 48k -shortest $target
    if ($LASTEXITCODE -ne 0) { throw "ffmpeg failed for $target" }
    $bumpCount++
}
Write-Host "wrote $bumpCount bump(s)" -ForegroundColor Green

# Commercials: 30s and 15s spots in era folders.
$adCount = 0
foreach ($year in @(2003, 2005)) {
    $dir = Join-Path $media ("commercials\{0}" -f $year)
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    for ($i = 1; $i -le 6; $i++) {
        foreach ($length in @(30, 15)) {
            $target = Join-Path $dir ("ad-{0:D2}-{1}s.mkv" -f $i, $length)
            if ((Test-Path $target) -and $KeepExisting) { continue }
            $label = "AD {0:D2} ({1}s) {2}" -f $i, $length, $year
            & ffmpeg -hide_banner -loglevel error -y `
                -f lavfi -i "color=c=0x222222:s=480x360:r=10:d=$length" `
                -f lavfi -i "sine=frequency=880:sample_rate=48000:duration=$length" `
                -vf "drawtext=text='$label':fontfile='$fontRegular':fontcolor=0xdddddd:fontsize=22:x=(w-text_w)/2:y=(h-text_h)/2" `
                -c:v libx264 -preset ultrafast -crf 34 -pix_fmt yuv420p `
                -c:a aac -b:a 48k -shortest $target
            if ($LASTEXITCODE -ne 0) { throw "ffmpeg failed for $target" }
            $adCount++
        }
    }
}
Write-Host "wrote $adCount commercial(s)" -ForegroundColor Green

# An ident, the last-resort filler.
$identDir = Join-Path $media 'idents'
New-Item -ItemType Directory -Force -Path $identDir | Out-Null
& ffmpeg -hide_banner -loglevel error -y `
    -f lavfi -i "color=c=black:s=480x360:r=10:d=5" `
    -f lavfi -i "anullsrc=channel_layout=stereo:sample_rate=48000:d=5" `
    -vf "drawtext=text='[adult swim]':fontfile='$fontRegular':fontcolor=white:fontsize=24:x=(w-text_w)/2:y=(h-text_h)/2" `
    -c:v libx264 -preset ultrafast -crf 30 -pix_fmt yuv420p `
    -c:a aac -b:a 48k -shortest (Join-Path $identDir 'station-id.mkv')

Write-Host "`ndemo media ready in $media" -ForegroundColor Green
Write-Host "next: .\run.ps1 scan; .\run.ps1 plan --show"
