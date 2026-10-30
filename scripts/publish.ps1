<#
.SYNOPSIS
    Create the GitHub repository for this project and push it.

.DESCRIPTION
    One command, from a local-only repo to a published one. It will:

      1. install the GitHub CLI if it is missing (winget, or a clear message if
         winget is not available either);
      2. prompt you to authenticate with `gh auth login` if you are not already;
      3. check what is about to be published -- no media, no runtime state, no
         secrets, nothing large enough for GitHub to reject;
      4. rename the branch to `main`, create the repository and push.

    The repository tracks NO media (`media/` is ignored) and no runtime state
    (`data/`, `playout/`, `xmltv/`, `lineup/`), so what gets published is source,
    config templates and docs. That is what makes it shareable.

    This script never stores a credential. Authentication is `gh auth login`,
    which uses the browser or a token you paste, and keeps the result in gh's own
    store.

.PARAMETER Name
    Repository name. Default `nostalgia-machine`.

.PARAMETER User
    GitHub username or organisation that will own it. Defaults to the account
    `gh` is logged in as, or the value of $env:GITHUB_USER.

.PARAMETER Public
    Publish publicly. Without it you are asked, because a private repo is easier
    to make public later than the other way round.

.PARAMETER Private
    Publish privately without being asked.

.PARAMETER Description
    Repository description. Defaults to the project's one-liner.

.PARAMETER SkipChecks
    Skip the pre-publish audit. Only useful if you know why you need it.

.EXAMPLE
    .\scripts\publish.ps1

.EXAMPLE
    .\scripts\publish.ps1 -Name my-channel -Public

.EXAMPLE
    .\scripts\publish.ps1 -User myorg -Private
#>
[CmdletBinding()]
param(
    [string]$Name = "nostalgia-machine",
    [string]$User = $env:GITHUB_USER,
    [switch]$Public,
    [switch]$Private,
    [string]$Description = "Deterministic 24/7 [adult swim] IPTV channel: scheduler and traffic engine for ErsatzTV Next.",
    [switch]$SkipChecks
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "== $Text" -ForegroundColor Cyan
}

function Write-Note([string]$Text) {
    Write-Host "   $Text" -ForegroundColor DarkGray
}

function Fail([string]$Text) {
    Write-Host ""
    Write-Host "error: $Text" -ForegroundColor Red
    exit 1
}

# -- 1. git ------------------------------------------------------------------

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail "git is not on PATH. Install it first: winget install Git.Git"
}

Push-Location (Join-Path $PSScriptRoot "..")
try {
    $root = (Get-Location).Path
    Write-Step "Repository"
    Write-Note $root

    if (-not (Test-Path ".git")) {
        Fail "this is not a git repository (no .git here)."
    }

    $existing = git remote get-url origin 2>$null
    if ($existing) {
        Fail "an 'origin' remote already exists ($existing). Remove it first, or push manually: git push -u origin main"
    }

    $dirty = git status --porcelain
    if ($dirty) {
        Write-Host ""
        Write-Host "warning: there are uncommitted changes. They will NOT be published." -ForegroundColor Yellow
        $dirty | Select-Object -First 10 | ForEach-Object { Write-Note $_ }
        if (($dirty | Measure-Object).Count -gt 10) { Write-Note "... and more" }
        $answer = Read-Host "Continue anyway? [y/N]"
        if ($answer -notmatch '^(y|yes)$') { Fail "stopped; commit or stash first." }
    }

    # -- 2. the GitHub CLI ---------------------------------------------------

    Write-Step "GitHub CLI"
    # A portable install (the zip, or a winget install whose PATH has not reached
    # this process) is not on PATH, so look where one would be before deciding it
    # is missing.
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        foreach ($candidate in @(
            (Join-Path $env:ProgramFiles "GitHub CLI\gh.exe"),
            (Join-Path $env:LOCALAPPDATA "Programs\gh\gh.exe")
        )) {
            if (Test-Path $candidate) {
                $env:Path = "$(Split-Path $candidate);$env:Path"
                break
            }
        }
    }
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        Write-Note "gh is not installed."
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            $answer = Read-Host "Install it now with winget? [Y/n]"
            if ($answer -match '^(n|no)$') {
                Fail "gh is required. Install it yourself (winget install GitHub.cli) and re-run."
            }
            Write-Note "installing GitHub.cli ..."
            winget install --id GitHub.cli --source winget --accept-package-agreements --accept-source-agreements --silent
            # winget updates the machine PATH, but not this process's copy of it.
            $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
            $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
            $env:Path = "$machine;$userPath"
            if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
                $candidate = Join-Path $env:ProgramFiles "GitHub CLI\gh.exe"
                if (Test-Path $candidate) {
                    $env:Path = "$(Split-Path $candidate);$env:Path"
                }
            }
            if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
                Fail "gh installed but not on PATH in this session. Open a new terminal and re-run."
            }
        }
        else {
            Fail "gh is required and winget is not available. Install it from https://cli.github.com and re-run."
        }
    }
    Write-Note ("gh " + ((gh --version | Select-Object -First 1) -replace '^gh version\s+', ''))

    # -- 3. authentication ---------------------------------------------------

    Write-Step "Authentication"
    gh auth status 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Note "not signed in to GitHub."
        Write-Host ""
        Write-Host "   Signing in now. Choose 'Login with a web browser' (a code is shown," -ForegroundColor Yellow
        Write-Host "   you paste it at github.com/login/device) or paste a token instead." -ForegroundColor Yellow
        Write-Host ""
        $answer = Read-Host "Sign in to GitHub now? [Y/n]"
        if ($answer -match '^(n|no)$') {
            Fail "authentication is required to create the repository."
        }
        # Interactive on purpose: gh drives its own browser/token flow, and this
        # script must not handle a credential itself.
        gh auth login --hostname github.com --git-protocol https --web
        if ($LASTEXITCODE -ne 0) {
            Fail "gh auth login did not complete. Run it yourself, then re-run this script."
        }
    }
    else {
        $who = (gh api user --jq .login 2>$null)
        Write-Note "signed in as $who"
    }

    if (-not $User) {
        $User = (gh api user --jq .login 2>$null)
    }
    if (-not $User) {
        Fail "could not work out which account to publish under. Pass -User <your-github-username>."
    }

    # -- 4. what is about to be published ------------------------------------

    if (-not $SkipChecks) {
        Write-Step "Checking what will be published"

        $tracked = git ls-files
        $count = ($tracked | Measure-Object).Count
        Write-Note "$count tracked file(s)"

        # Media and runtime state must never be tracked. This is the check that
        # matters: the failure mode is a multi-gigabyte repo, not an error.
        $forbidden = $tracked | Where-Object {
            $_ -match '^(media|data|playout|xmltv|lineup|hls|ersatztv)/' -or
            $_ -match '\.(mkv|mp4|m4v|avi|mov|webm|mp3|m4a|wav|flac|ogg|aac|ts)$' -or
            $_ -match '(^|/)\.env$'
        }
        if ($forbidden) {
            Write-Host ""
            Write-Host "error: these tracked files must not be published:" -ForegroundColor Red
            $forbidden | Select-Object -First 20 | ForEach-Object { Write-Note $_ }
            Fail "remove them from the index (git rm --cached <path>) and re-run."
        }
        Write-Note "no media, runtime state or .env tracked"

        $big = $tracked | Where-Object { Test-Path $_ } |
            ForEach-Object { [PSCustomObject]@{ Size = (Get-Item $_).Length; Path = $_ } } |
            Where-Object { $_.Size -gt 5MB }
        if ($big) {
            Write-Host ""
            Write-Host "warning: these tracked files exceed 5 MB (GitHub rejects over 100 MB):" -ForegroundColor Yellow
            $big | ForEach-Object { Write-Note ("{0:N1} MB  {1}" -f ($_.Size / 1MB), $_.Path) }
            $answer = Read-Host "Continue? [y/N]"
            if ($answer -notmatch '^(y|yes)$') { Fail "stopped." }
        }

        # A crude secret sweep. It is not a scanner; it catches the mistake that
        # actually happens, which is a token pasted into a config or a doc.
        $hits = git grep -nEI "(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----)" -- . 2>$null
        if ($hits) {
            Write-Host ""
            Write-Host "error: something that looks like a credential is tracked:" -ForegroundColor Red
            $hits | Select-Object -First 10 | ForEach-Object { Write-Note $_ }
            Fail "remove it, and rotate the credential -- it is in your git history now."
        }
        Write-Note "no credential-shaped strings found"
    }

    # -- 5. visibility -------------------------------------------------------

    if ($Public -and $Private) {
        Fail "pass -Public or -Private, not both."
    }
    if (-not $Public -and -not $Private) {
        Write-Step "Visibility"
        Write-Host "   Public: anyone can read and clone it. Private: only you." -ForegroundColor DarkGray
        $answer = Read-Host "Publish publicly? [y/N]"
        $Public = $answer -match '^(y|yes)$'
    }
    $visibility = if ($Public) { "public" } else { "private" }

    # -- 6. branch, create, push --------------------------------------------

    Write-Step "Branch"
    $branch = git branch --show-current
    if ($branch -ne "main") {
        Write-Note "renaming '$branch' to 'main' (what a new reader and CI expect)"
        git branch -M main
        if ($LASTEXITCODE -ne 0) { Fail "could not rename the branch." }
    }
    else {
        Write-Note "already on main"
    }

    Write-Step "Creating $User/$Name ($visibility)"
    gh repo create "$User/$Name" "--$visibility" --source . --remote origin --push --description $Description
    if ($LASTEXITCODE -ne 0) {
        Fail "gh repo create failed. Nothing was pushed; the remote may or may not exist."
    }

    Write-Host ""
    Write-Host "Published: https://github.com/$User/$Name" -ForegroundColor Green
    Write-Host ""
    Write-Host "Next, if you want it to look finished:" -ForegroundColor DarkGray
    Write-Host "  - add topics:  gh repo edit $User/$Name --add-topic iptv,adult-swim,ersatztv,selfhosted" -ForegroundColor DarkGray
    Write-Host "  - watch CI:    gh run watch" -ForegroundColor DarkGray
}
finally {
    Pop-Location
}
