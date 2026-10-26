<#
.SYNOPSIS
    Run Nostalgia Machine natively on Windows, without Docker.

.DESCRIPTION
    ErsatzTV Next ships a windows-x64 zip, so a native install needs no
    containers. This script creates a virtualenv, installs the scheduler, and
    can either build the plan or start the web UI.

    ErsatzTV itself is expected at .\ersatztv\ersatztv.exe. Download the
    windows-x64 zip from https://github.com/ErsatzTV/next/releases and extract
    it there (it must sit next to its own ffmpeg.exe).

.EXAMPLE
    .\run.ps1 setup
    .\run.ps1 scan
    .\run.ps1 emit
    .\run.ps1 stream
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('setup', 'scan', 'emit', 'plan', 'doctor', 'bumps', 'import', 'web', 'stream', 'serve-ersatztv')]
    [string]$Task = 'emit',

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$venv = Join-Path $root '.venv'
$python = Join-Path $venv 'Scripts\python.exe'

function Ensure-Venv {
    if (-not (Test-Path $python)) {
        Write-Host "creating virtualenv..." -ForegroundColor Cyan
        python -m venv $venv
        & $python -m pip install --quiet --upgrade pip
    }
    # Check for the installed distribution, not an importable module: the CLI runs
    # off PYTHONPATH, so `import nostalgia` succeeds even when nothing is installed
    # and the venv would be reinstalled on every single invocation.
    & $python -c "import importlib.metadata as m; m.version('nostalgia-machine')" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "installing nostalgia-machine..." -ForegroundColor Cyan
        & $python -m pip install --quiet -e "$root[web]"
        if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
    }
}

function Invoke-Nostalgia {
    param([string[]]$Arguments)
    $env:PYTHONPATH = Join-Path $root 'src'
    $script:LastCode = 0
    Push-Location $root
    try {
        # Write straight to the host. Using `return $LASTEXITCODE` here would make
        # the caller's assignment capture the command's entire output as well, and
        # piping to Out-Null at the call site would discard that output along with
        # the exit code -- which is how this script came to print nothing at all.
        & $python -m nostalgia.cli --root $root @Arguments | Out-Host
        $script:LastCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
}

switch ($Task) {
    'setup' {
        Ensure-Venv
        Write-Host "`nNostalgia Machine is installed." -ForegroundColor Green
        Write-Host "Next: put media in media\shows\<show-id>\, then run '.\run.ps1 scan'"
        break
    }
    'scan' {
        Ensure-Venv
        Invoke-Nostalgia (@('scan') + $Rest)
        break
    }
    'plan' {
        Ensure-Venv
        Invoke-Nostalgia (@('plan', '--show') + $Rest)
        break
    }
    'emit' {
        Ensure-Venv
        Invoke-Nostalgia (@('emit') + $Rest)
        break
    }
    'doctor' {
        Ensure-Venv
        Invoke-Nostalgia @('doctor')
        break
    }
    'bumps' {
        Ensure-Venv
        Invoke-Nostalgia (@('bumps') + $Rest)
        break
    }
    'import' {
        Ensure-Venv
        Invoke-Nostalgia (@('import') + $Rest)
        break
    }
    'web' {
        Ensure-Venv
        Invoke-Nostalgia (@('serve', '--port', '8099') + $Rest)
        break
    }
    'stream' {
        $exe = Join-Path $root 'ersatztv\ersatztv.exe'
        if (-not (Test-Path $exe)) {
            throw "ErsatzTV Next not found at $exe. Download the windows-x64 zip from https://github.com/ErsatzTV/next/releases and extract it into .\ersatztv\"
        }
        # Emit writes the streamer's config into lineup\, not into playout\.
        $lineup = Join-Path $root 'lineup\lineup.json'
        if (-not (Test-Path $lineup)) {
            throw "No lineup yet. Run '.\run.ps1 emit' first."
        }
        Write-Host "serving http://localhost:8409/channels.m3u" -ForegroundColor Cyan
        & $exe $lineup
        break
    }
}

# Propagate the scheduler's exit code so `.\run.ps1 emit; $LASTEXITCODE` is
# usable from a script or a CI step.
if ($script:LastCode -ne 0) { exit $script:LastCode }
