<#
.SYNOPSIS
    Create the GitHub repository for this project and push it.

.DESCRIPTION
    Local-only repo, no media. This creates the remote and pushes `master`.

    The repo deliberately tracks NO media (`media/` is ignored) and no runtime
    state (`data/`, `playout/`, `xmltv/`, `lineup/`), so what gets published is
    source, config templates and docs. That is what makes it shareable.

    Pushing needs credentials. This script does not store a token anywhere: it
    lets `git push` prompt, or you can log in once with `gh auth login`.

.PARAMETER Name
    Repository name. Default `nostalgia-machine`.

.PARAMETER User
    GitHub username or organisation that will own it. Defaults to the account
    `gh` is logged in as, or the value of $env:GITHUB_USER.

.PARAMETER Public
    Publish publicly. Default is private, because it is easier to make a private
    repo public later than the other way round.

.EXAMPLE
    .\scripts\publish.ps1 -User yourname

.EXAMPLE
    .\scripts\publish.ps1 -User yourname -Public
#>
[CmdletBinding()]
param(
    [string]$Name = "nostalgia-machine",
    [string]$User = $env:GITHUB_USER,
    [switch]$Public
)

$ErrorActionPreference = "Stop"
Push-Location (Join-Path $PSScriptRoot "..")
try {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "git is not on PATH."
    }

    $existing = git remote get-url origin 2>$null
    if ($existing) {
        throw "This repo already has an 'origin' remote ($existing). Remove it first, or push manually: git push -u origin master"
    }

    $visibility = if ($Public) { "public" } else { "private" }

    if (-not $User) {
        if (Get-Command gh -ErrorAction SilentlyContinue) {
            $User = (gh api user --jq .login 2>$null)
        }
        if (-not $User) {
            throw "No GitHub user given. Pass -User <your-github-username>, or set `$env:GITHUB_USER."
        }
    }

    # Sanity-check what is about to be published, because the failure mode of
    # getting this wrong is a 2 GB repo, not an error message.
    $big = git ls-files | Where-Object { Test-Path $_ } |
        ForEach-Object { [PSCustomObject]@{ Size = (Get-Item $_).Length; Path = $_ } } |
        Where-Object { $_.Size -gt 5MB }
    if ($big) {
        Write-Warning "These tracked files exceed 5 MB and GitHub will reject anything over 100 MB:"
        $big | ForEach-Object { Write-Warning ("  {0:N1} MB  {1}" -f ($_.Size / 1MB), $_.Path) }
    }

    if (Get-Command gh -ErrorAction SilentlyContinue) {
        Write-Host "Creating $visibility repo $User/$Name and pushing master via gh ..." -ForegroundColor Cyan
        gh repo create "$User/$Name" "--$visibility" --source . --remote origin --push --description "Deterministic 24/7 [adult swim] IPTV channel: scheduler and traffic engine for ErsatzTV Next."
    }
    else {
        # No gh: add the remote and let git prompt for credentials on push. HTTPS
        # is used rather than SSH because a fresh machine is far more likely to
        # have a credential helper than an SSH key already trusted by GitHub.
        $url = "https://github.com/$User/$Name.git"
        Write-Host "gh is not installed, so create the empty repo yourself first:" -ForegroundColor Yellow
        Write-Host "  https://github.com/new  ->  name: $Name  ->  $visibility  ->  create (no README)" -ForegroundColor Yellow
        Read-Host "Press Enter once the empty repo exists"
        git remote add origin $url
        git push -u origin master
    }

    Write-Host ""
    Write-Host "Done: https://github.com/$User/$Name" -ForegroundColor Green
}
finally {
    Pop-Location
}
