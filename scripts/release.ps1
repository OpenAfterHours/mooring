<#
.SYNOPSIS
Cut a mooring release: bump the version, commit, tag, and push.

.DESCRIPTION
Bumps the version in pyproject.toml + uv.lock (via `uv version`) and keeps
src/mooring/__init__.py in sync, runs lint and tests, commits the bump,
creates the vX.Y.Z tag, and pushes branch + tag. The pushed tag triggers
.github/workflows/release.yml, which builds mooring.pyz / mooring.exe,
publishes the GitHub Release, and uploads the sdist/wheel to PyPI.

Works in Windows PowerShell 5.1 and pwsh.

.PARAMETER Bump
Which part to bump: patch, minor, or major. Defaults to patch.

.PARAMETER Version
Set an explicit version (X.Y.Z) instead of bumping.

.PARAMETER DryRun
Run the preflight checks and show the version that would be released,
without writing, committing, tagging, or pushing anything.

.EXAMPLE
.\scripts\release.ps1 minor

.EXAMPLE
.\scripts\release.ps1 -Version 1.0.0 -DryRun
#>
[CmdletBinding(DefaultParameterSetName = 'Bump')]
param(
    [Parameter(ParameterSetName = 'Bump', Position = 0)]
    [ValidateSet('patch', 'minor', 'major')]
    [string]$Bump = 'patch',

    [Parameter(ParameterSetName = 'Explicit', Mandatory)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version,

    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot
Set-Location $repoRoot

function Fail([string]$Message) {
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

# -- preflight ----------------------------------------------------------------
$branch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($branch -ne 'master') { Fail "Releases are cut from master (currently on '$branch')." }

if (git status --porcelain) { Fail 'Working tree is not clean; commit or stash first.' }

git fetch origin master --tags --quiet
if ($LASTEXITCODE -ne 0) { Fail 'git fetch failed.' }
$behind = [int](git rev-list --count 'HEAD..origin/master')
if ($behind -gt 0) { Fail "master is $behind commit(s) behind origin/master; pull first." }

$current = (uv version --short | Select-Object -Last 1).Trim()

# -- compute / apply the new version -------------------------------------------
$uvArgs = @('version', '--short')
if ($PSCmdlet.ParameterSetName -eq 'Explicit') { $uvArgs += $Version }
else { $uvArgs += @('--bump', $Bump) }
if ($DryRun) { $uvArgs += '--dry-run' }

$new = (& uv @uvArgs | Select-Object -Last 1)
if ($LASTEXITCODE -ne 0) { Fail 'uv version failed.' }
$new = "$new".Trim()
if ($new -notmatch '^\d+\.\d+\.\d+') { Fail "Unexpected version output from uv: '$new'." }
if ($new -eq $current) { Fail "Version unchanged ($new) - nothing to release." }
if (git tag --list "v$new") { Fail "Tag v$new already exists." }

if ($DryRun) {
    Write-Host "Dry run: would release v$new (currently $current) - bump, commit, tag, push." `
        -ForegroundColor Yellow
    exit 0
}

# -- sync __version__ in the package ------------------------------------------
# [IO.File] writes BOM-less UTF-8; PS 5.1's Set-Content -Encoding utf8 adds a
# BOM, and the repo convention is BOM-less (a BOM breaks marimo notebooks).
$initPath = Join-Path $repoRoot 'src\mooring\__init__.py'
$content = [IO.File]::ReadAllText($initPath)
$updated = $content -replace '__version__ = "[^"]+"', "__version__ = `"$new`""
if ($updated -eq $content) { Fail "Could not find the __version__ line in $initPath." }
[IO.File]::WriteAllText($initPath, $updated)

# -- checks --------------------------------------------------------------------
uv run ruff check src tests
if ($LASTEXITCODE -ne 0) { Fail 'Lint failed; version files are modified but not committed.' }
uv run pytest -q
if ($LASTEXITCODE -ne 0) { Fail 'Tests failed; version files are modified but not committed.' }

# -- commit, tag, push ----------------------------------------------------------
git add pyproject.toml uv.lock src/mooring/__init__.py
git commit -m "release: v$new"
if ($LASTEXITCODE -ne 0) { Fail 'git commit failed.' }
git tag -a "v$new" -m "mooring v$new"
if ($LASTEXITCODE -ne 0) { Fail 'git tag failed.' }
git push origin master "v$new"
if ($LASTEXITCODE -ne 0) { Fail "Push failed; local commit and tag v$new exist - fix and push manually." }

Write-Host ''
Write-Host "Released v$new ($current -> $new)." -ForegroundColor Green
Write-Host 'CI will build artifacts, create the GitHub Release, and publish to PyPI:'
Write-Host "  https://github.com/OpenAfterHours/mooring/actions"
