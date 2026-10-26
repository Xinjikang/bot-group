# Windows PowerShell 5.1+; this file is also the standalone public installer.
[CmdletBinding()]
param(
    [string]$InstallDir = $(if ($env:INSTALL_DIR) { $env:INSTALL_DIR } else { Join-Path $env:LOCALAPPDATA 'bot-group\app' }),
    [string]$IndexUrl = 'https://github.com/Xinjikang/bot-group/releases/latest/download/releases.json',
    [string]$PythonPath = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Get-CompatiblePython([string]$Executable, [string[]]$Prefix = @()) {
    if (-not $Executable) { return $null }
    try {
        $probe = @(& $Executable @Prefix -I -c 'import sys,struct; print(sys.executable) if (3,11) <= sys.version_info[:2] < (3,14) and struct.calcsize(''P'')==8 else sys.exit(1)' 2>$null)
        if ($LASTEXITCODE -eq 0 -and $probe.Count -gt 0 -and (Test-Path -LiteralPath $probe[-1] -PathType Leaf)) {
            return [string]$probe[-1]
        }
    } catch { }
    return $null
}

function Get-Download([string]$Url, [string]$Destination, [string]$Sha256 = '') {
    if (([uri]$Url).Scheme -ne 'https') { throw "Download requires HTTPS: $Url" }
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Destination -TimeoutSec 180
    if ($Sha256) {
        $stream = [IO.File]::OpenRead($Destination)
        $hash = [Security.Cryptography.SHA256]::Create()
        try { $actual = [BitConverter]::ToString($hash.ComputeHash($stream)).Replace('-', '') }
        finally { $stream.Dispose(); $hash.Dispose() }
        if ($actual -ne $Sha256) { throw "SHA256 mismatch: $Url" }
    }
}

function Get-ReleaseAsset($Entry, [string]$Field, [string]$BaseUrl) {
    $value = [string]$Entry.$Field
    if (-not $value) { throw "Release is missing $Field" }
    $url = [uri]::new([uri]$BaseUrl, $value)
    if ($url.Scheme -ne 'https') { throw "Release asset requires HTTPS: $Field" }
    return $url.AbsoluteUri
}

function Install-PrivatePython([string]$TemporaryDirectory) {
    $runtime = Join-Path $env:LOCALAPPDATA 'bot-group-runtime\Python313'
    $existing = Get-CompatiblePython (Join-Path $runtime 'python.exe')
    if ($existing) { return $existing }
    Write-Host '[bot-group] Installing Python 3.13.16 for the current user...'
    $installer = Join-Path $TemporaryDirectory 'python-3.13.16-amd64.exe'
    Get-Download 'https://www.python.org/ftp/python/3.13.16/python-3.13.16-amd64.exe' $installer 'fb4f9f5d438b2396da0086dc70b935c530cb578e37adc6d354f7ad2037fee83b'
    $arguments = @('/quiet', 'InstallAllUsers=0', ('TargetDir="{0}"' -f $runtime),
        'PrependPath=0', 'Include_launcher=0', 'AssociateFiles=0', 'Shortcuts=0',
        'Include_test=0', 'Include_doc=0', 'Include_tcltk=0', 'Include_pip=1')
    $process = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -notin @(0, 3010)) { throw "Python installation failed (exit $($process.ExitCode))." }
    $python = Get-CompatiblePython (Join-Path $runtime 'python.exe')
    if (-not $python) { throw 'Python installation did not produce a usable interpreter.' }
    return $python
}

function Install-BotGroup {
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'bot-group requires 64-bit Windows 10 or later.' }
    $target = [IO.Path]::GetFullPath($InstallDir)
    if ($target.Contains('"')) { throw 'The install directory contains an invalid quote.' }
    $temporary = Join-Path ([IO.Path]::GetTempPath()) ('bot-group-setup-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $temporary | Out-Null
    $savedEnvironment = @{}
    foreach ($key in @('BOOTSTRAP_URL', 'BOOTSTRAP_SHA256', 'INSTALL_DIR', 'PYTHONUTF8')) {
        $savedEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
    }
    try {
        Write-Host '[bot-group] Finding the latest release...'
        $indexFile = Join-Path $temporary 'releases.json'
        Get-Download $IndexUrl $indexFile
        $index = Get-Content -LiteralPath $indexFile -Raw | ConvertFrom-Json
        $entry = @($index.releases | Where-Object {
            $_.version -cmatch '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' -and -not $_.draft -and -not $_.prerelease
        } | Sort-Object { [version]$_.version.Substring(1) } -Descending | Select-Object -First 1)
        if ($entry.Count -ne 1) { throw 'No stable release is available.' }
        $release = $entry[0]
        foreach ($field in @('sha256', 'bootstrap_sha256')) {
            if ([string]$release.$field -notmatch '^[0-9a-fA-F]{64}$') { throw "Invalid release checksum: $field" }
        }
        $sourceUrl = Get-ReleaseAsset $release 'source' $IndexUrl
        $bootstrapUrl = Get-ReleaseAsset $release 'bootstrap' $IndexUrl
        $bootstrapFile = Join-Path $temporary 'bootstrap.py'
        Get-Download $bootstrapUrl $bootstrapFile $release.bootstrap_sha256

        $python = Get-CompatiblePython (Join-Path $target '.venv\Scripts\python.exe')
        if (-not $python -and $PythonPath) {
            $python = Get-CompatiblePython $PythonPath
            if (-not $python) { throw 'PythonPath must be a working 64-bit Python 3.11, 3.12 or 3.13.' }
        }
        foreach ($minor in @('3.13', '3.12', '3.11')) {
            if (-not $python) { $python = Get-CompatiblePython 'py' @("-$minor") }
        }
        if (-not $python) {
            $command = Get-Command python -CommandType Application -ErrorAction SilentlyContinue
            if ($command -and $command.Source -notlike '*\WindowsApps\*') {
                $python = Get-CompatiblePython $command.Source
            }
        }
        if (-not $python) { $python = Install-PrivatePython $temporary }
        Write-Host "[bot-group] Installing $($release.version) to $target"
        $env:BOOTSTRAP_URL = $sourceUrl
        $env:BOOTSTRAP_SHA256 = $release.sha256
        $env:INSTALL_DIR = $target
        $env:PYTHONUTF8 = '1'
        & $python -X utf8 $bootstrapFile
        if ($LASTEXITCODE -ne 0) { throw "Installation failed (exit $LASTEXITCODE). Fix the reported error and rerun this command." }
        Write-Host ''
        Write-Host "[bot-group] Installed $($release.version)."
        Write-Host "Start:   $target\start.bat"
        Write-Host "Stop:    $target\stop.bat"
        Write-Host "Upgrade: $target\upgrade.bat"
        Write-Host 'Start opens the web interface in your default browser after the service is ready.'
    } finally {
        foreach ($key in $savedEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($key, $savedEnvironment[$key], 'Process')
        }
        # The unique download directory is separate from the persistent installation.
        $resolved = [IO.Path]::GetFullPath($temporary)
        $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
        if ($resolved.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -and
            [IO.Path]::GetFileName($resolved).StartsWith('bot-group-setup-')) {
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    try { Install-BotGroup } catch { Write-Error $_ -ErrorAction Continue; exit 1 }
}
