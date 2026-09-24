[CmdletBinding()]
param(
    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'

function Get-NormalizedPath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path).TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
}

function Assert-NotReparsePoint([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path -Force
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Refusing to follow a symlink or reparse point: $Path"
        }
    }
}

$sandboxRoot = Get-NormalizedPath 'C:\project\ubuntu_sandbox'
$destination = Get-NormalizedPath (Join-Path $sandboxRoot 'remote_agent_com_sys')
$repo = Get-NormalizedPath $RepositoryRoot

if (-not (Test-Path -LiteralPath (Join-Path $repo '.git'))) {
    throw "Repository root does not contain .git: $repo"
}
if ($destination -ne (Get-NormalizedPath 'C:\project\ubuntu_sandbox\remote_agent_com_sys')) {
    throw "Destination is not the dedicated sandbox subdirectory: $destination"
}
if (-not $destination.StartsWith($sandboxRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Destination escaped the named sandbox root: $destination"
}

Assert-NotReparsePoint $sandboxRoot
Assert-NotReparsePoint $destination

$gitPaths = & git -C $repo ls-files --cached
if ($LASTEXITCODE -ne 0) {
    throw 'git ls-files failed; refusing to copy an unverified file list.'
}

$managedPaths = @(
    foreach ($gitPath in $gitPaths) {
        $relative = $gitPath.Replace('/', '\')
        if ([string]::IsNullOrWhiteSpace($relative) -or $relative.StartsWith('\') -or $relative -match '(^|\\)\.\.(\\|$)') {
            continue
        }

        $parts = $relative -split '\\'
        $first = $parts[0].ToLowerInvariant()
        $extension = [System.IO.Path]::GetExtension($relative).ToLowerInvariant()
        $isAllowedDirectory = ($first -in @('remote_agent', 'tests', 'docs')) -and ($extension -in @('.py', '.md', '.txt', '.toml', '.cfg', '.ini', '.json', '.yaml', '.yml', '.example'))
        $leaf = [System.IO.Path]::GetFileName($relative).ToLowerInvariant()
        $isAllowedRootFile = ($parts.Count -eq 1) -and (
            $leaf -eq 'readme.md' -or
            $leaf -eq 'pyproject.toml' -or
            $leaf -eq 'setup.cfg' -or
            $leaf -eq 'setup.py' -or
            $leaf -eq '.env.example' -or
            $leaf -match '^requirements[^\\]*\.txt$'
        )

        if ($isAllowedDirectory -or $isAllowedRootFile) {
            Write-Output $relative
        }
    }
)

if ($managedPaths.Count -eq 0) {
    throw 'No tracked source, documentation, test, or configuration files matched the copy allowlist.'
}

if (-not (Test-Path -LiteralPath $destination)) {
    New-Item -ItemType Directory -Path $destination | Out-Null
}

foreach ($relative in $managedPaths) {
    $sourcePath = Join-Path $repo $relative
    $targetPath = Join-Path $destination $relative
    $resolvedSource = Get-NormalizedPath $sourcePath
    $resolvedTarget = Get-NormalizedPath $targetPath

    if (-not $resolvedSource.StartsWith($repo + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Source path escaped repository root: $relative"
    }
    if (-not $resolvedTarget.StartsWith($destination + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Target path escaped the dedicated copy: $relative"
    }
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw "Tracked source file is missing: $relative"
    }

    $sourceCursor = $repo
    foreach ($segment in ($relative -split '\\')) {
        $sourceCursor = Join-Path $sourceCursor $segment
        Assert-NotReparsePoint $sourceCursor
    }
    $sourceItem = Get-Item -LiteralPath $sourcePath -Force
    if ($sourceItem.PSIsContainer) {
        throw "Expected a source file: $relative"
    }

    $parent = Split-Path -Parent $targetPath
    $relativeParent = Split-Path -Parent $relative
    if ($relativeParent -and $relativeParent -ne '.') {
        $cursor = $destination
        foreach ($segment in ($relativeParent -split '\\')) {
            $cursor = Join-Path $cursor $segment
            Assert-NotReparsePoint $cursor
            if (-not (Test-Path -LiteralPath $cursor)) {
                New-Item -ItemType Directory -Path $cursor | Out-Null
            }
        }
    }

    Assert-NotReparsePoint $targetPath
    Copy-Item -LiteralPath $sourcePath -Destination $targetPath -Force
}

Write-Host "Copied $($managedPaths.Count) tracked project files to $destination"
