[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,

    [ValidatePattern("^[A-Z]$")]
    [string]$DriveLetter = "R"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Wait-Until {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Condition,
        [Parameter(Mandatory = $true)]
        [int]$TimeoutSeconds,
        [Parameter(Mandatory = $true)]
        [string]$FailureMessage
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (& $Condition) {
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw $FailureMessage
}

function Get-LauncherProcesses {
    param([string]$ExecutablePath)

    if (-not $ExecutablePath) {
        return @()
    }
    return @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        try {
            $_.Path -eq $ExecutablePath
        } catch {
            $false
        }
    })
}

function Stop-LauncherProcesses {
    param([string]$ExecutablePath)

    $processes = @(Get-LauncherProcesses -ExecutablePath $ExecutablePath)
    if ($processes.Count) {
        $processes | Stop-Process -Force
        Wait-Until -TimeoutSeconds 15 -FailureMessage "まあ丸进程没有按时退出" -Condition {
            @(Get-LauncherProcesses -ExecutablePath $ExecutablePath).Count -eq 0
        }
    }
}

$installer = (Resolve-Path -LiteralPath $InstallerPath).Path
$scratchBase = if ($env:RUNNER_TEMP) {
    [System.IO.Path]::GetFullPath($env:RUNNER_TEMP)
} else {
    [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
}
$smokeRoot = Join-Path $scratchBase "maamaru-installer-smoke"
if (Test-Path -LiteralPath $smokeRoot) {
    throw "安装冒烟目录已经存在，拒绝覆盖：$smokeRoot"
}

$drive = "${DriveLetter}:"
$driveRoot = "${drive}\\"
if (Test-Path -LiteralPath $driveRoot) {
    throw "测试盘符已经被占用：$drive"
}

$installVolume = Join-Path $smokeRoot "install-volume"
$fakeLocalAppData = Join-Path $smokeRoot "user-localappdata"
$installerLog = Join-Path $smokeRoot "installer.log"
$uninstallerLog = Join-Path $smokeRoot "uninstaller.log"
New-Item -ItemType Directory -Path $installVolume, $fakeLocalAppData -Force | Out-Null

$previousLocalAppData = $env:LOCALAPPDATA
$hadDataOverride = Test-Path Env:MAAMARU_DATA_DIR
$previousDataOverride = $env:MAAMARU_DATA_DIR
$driveMapped = $false
$launcher = $null

try {
    & subst.exe $drive $installVolume
    if ($LASTEXITCODE -ne 0) {
        throw "无法创建隔离测试盘符 $drive"
    }
    $driveMapped = $true

    $installDir = Join-Path $driveRoot "Programs\Maamaru"
    $physicalInstallDir = Join-Path $installVolume "Programs\Maamaru"
    $install = Start-Process -FilePath $installer -WindowStyle Hidden -PassThru -ArgumentList @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/SP-",
        "/DIR=`"$installDir`"",
        "/LOG=`"$installerLog`""
    )
    # -Wait 没有超时兜底：万一出现未被静默抑制的窗口，10 分钟后按失败处理而不是挂死 CI
    if (-not $install.WaitForExit(600000)) {
        Stop-Process -Id $install.Id -Force -ErrorAction SilentlyContinue
        throw "安装器 10 分钟未结束，疑似出现未被静默抑制的窗口"
    }
    if ($install.ExitCode -ne 0) {
        throw "安装器返回错误码 $($install.ExitCode)"
    }

    # subst 盘符在进程列表里会还原成实际路径。后续用实际路径启动和清理，
    # 同时仍由安装器通过 R: 写入，覆盖“安装到非系统盘”的真实场景。
    $launcher = Join-Path $physicalInstallDir "まあ丸启动器.exe"
    $manifest = Join-Path $physicalInstallDir "manifest.json"
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw "安装后没有找到启动器：$launcher"
    }
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
        throw "安装后没有找到发布清单：$manifest"
    }

    # 用一个全新的 LOCALAPPDATA 验证普通安装版的数据路径，不依赖开发机环境。
    $env:LOCALAPPDATA = $fakeLocalAppData
    Remove-Item Env:MAAMARU_DATA_DIR -ErrorAction SilentlyContinue
    $dataRoot = Join-Path $fakeLocalAppData "Maamaru"
    $expectedData = @(
        (Join-Path $dataRoot "data-version.json"),
        (Join-Path $dataRoot "config\touken.json"),
        (Join-Path $dataRoot "config\panel.json"),
        (Join-Path $dataRoot "config\expedition.json")
    )

    $launcherProcess = Start-Process -FilePath $launcher -WorkingDirectory $physicalInstallDir -WindowStyle Hidden -PassThru
    Wait-Until -TimeoutSeconds 30 -FailureMessage "首次启动没有建立完整的用户数据" -Condition {
        @($expectedData | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) }).Count -eq 0
    }
    if ($launcherProcess.HasExited) {
        throw "启动器在首次启动检查期间提前退出，错误码 $($launcherProcess.ExitCode)"
    }

    $config = Get-Content -Raw -LiteralPath (Join-Path $dataRoot "config\touken.json") | ConvertFrom-Json
    if ($config.adb_path -or $config.emulator_manager) {
        throw "全新安装意外带入了开发机的模拟器配置"
    }
    Stop-LauncherProcesses -ExecutablePath $launcher

    # 直接进入面板模式，验证内置 Python/依赖能够在没有 ADB 和模拟器时提供页面。
    $panelProcess = Start-Process -FilePath $launcher -ArgumentList "--panel" -WorkingDirectory $physicalInstallDir -WindowStyle Hidden -PassThru
    Wait-Until -TimeoutSeconds 30 -FailureMessage "安装后的面板没有在 8080 端口就绪" -Condition {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8080/api/status" -TimeoutSec 2
            $response.StatusCode -eq 200
        } catch {
            $false
        }
    }
    if ($panelProcess.HasExited) {
        throw "面板在就绪检查期间提前退出，错误码 $($panelProcess.ExitCode)"
    }
    Stop-LauncherProcesses -ExecutablePath $launcher

    $sentinel = Join-Path $dataRoot "installer-smoke-preserve.txt"
    Set-Content -LiteralPath $sentinel -Value "uninstall must preserve user data" -Encoding utf8
    $uninstaller = Get-ChildItem -LiteralPath $physicalInstallDir -Filter "unins*.exe" -File | Select-Object -First 1
    if (-not $uninstaller) {
        throw "安装目录里没有找到卸载程序"
    }
    $uninstall = Start-Process -FilePath $uninstaller.FullName -WindowStyle Hidden -PassThru -ArgumentList @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/LOG=`"$uninstallerLog`""
    )
    if (-not $uninstall.WaitForExit(120000)) {
        Stop-Process -Id $uninstall.Id -Force -ErrorAction SilentlyContinue
        throw "卸载程序 2 分钟未结束，疑似出现未被静默抑制的窗口"
    }
    if ($uninstall.ExitCode -ne 0) {
        throw "卸载程序返回错误码 $($uninstall.ExitCode)"
    }
    Wait-Until -TimeoutSeconds 30 -FailureMessage "卸载后程序文件仍然存在" -Condition {
        -not (Test-Path -LiteralPath $launcher)
    }
    if (-not (Test-Path -LiteralPath $sentinel -PathType Leaf)) {
        throw "卸载过程删除了用户数据"
    }

    $result = [ordered]@{
        installer = Split-Path -Leaf $installer
        install_drive = $drive
        installed_program_files = $true
        initialized_user_data = $true
        panel_ready_without_emulator = $true
        uninstall_preserved_user_data = $true
    }
    $result | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $smokeRoot "result.json") -Encoding utf8
    $result | ConvertTo-Json -Compress | Write-Output
} finally {
    Stop-LauncherProcesses -ExecutablePath $launcher
    if ($null -eq $previousLocalAppData) {
        Remove-Item Env:LOCALAPPDATA -ErrorAction SilentlyContinue
    } else {
        $env:LOCALAPPDATA = $previousLocalAppData
    }
    if ($hadDataOverride) {
        $env:MAAMARU_DATA_DIR = $previousDataOverride
    } else {
        Remove-Item Env:MAAMARU_DATA_DIR -ErrorAction SilentlyContinue
    }
    if ($driveMapped) {
        & subst.exe $drive /D
    }
}
