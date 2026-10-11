# Agent 桌宠启动器：找一个能用的 Python，用 pythonw 静默把桌宠拉起来。
# 直接运行 = 启动；-Install = 加入开机启动；-Uninstall = 取消开机启动。
param([switch]$Install, [switch]$Uninstall, [switch]$CreateDesktopLink)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$startupDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
$startupLink = Join-Path $startupDir "CodexPet.lnk"

function Find-Python {
  param([string]$Leaf)
  if ($env:CODEXPET_PYTHON -and (Test-Path $env:CODEXPET_PYTHON)) { return $env:CODEXPET_PYTHON }
  $roots = Join-Path $env:USERPROFILE ".cache\codex-runtimes"
  if (Test-Path $roots) {
    foreach ($dir in Get-ChildItem $roots -Directory) {
      foreach ($rel in @("dependencies\python\$Leaf", "dependencies\python\python.exe")) {
        $candidate = Join-Path $dir.FullName $rel
        if (Test-Path $candidate) { return $candidate }
      }
    }
  }
  foreach ($name in @($Leaf, "python.exe")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
  }
  return $null
}

if ($Uninstall) {
  try {
    if (Test-Path $startupLink) { Remove-Item $startupLink -Force; Write-Host "[OK] 已取消开机启动。" }
    else { Write-Host "当前没有设置开机启动。" }
  } catch { Write-Host "[X] 取消失败：$($_.Exception.Message)" }
  exit 0
}

if ($Install) {
  try {
    if (-not (Test-Path $startupDir)) { New-Item -ItemType Directory -Force -Path $startupDir | Out-Null }
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($startupLink)
    $link.TargetPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $link.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + (Join-Path $here "pet-launcher.ps1") + '"'
    $link.WorkingDirectory = $here
    $link.IconLocation = Join-Path $here "web\icon.ico"
    $link.Description = "Agent 桌宠"
    $link.Save()
    Write-Host "[OK] 已加入开机启动：$startupLink"
  } catch { Write-Host "[X] 加入开机启动失败：$($_.Exception.Message)" }
  exit 0
}

function New-DesktopLink {
  param([switch]$Explicit)
  $desktopDir = $env:CODEXPET_DESKTOP
  if (-not $desktopDir) { $desktopDir = [Environment]::GetFolderPath("Desktop") }
  if (-not (Test-Path -LiteralPath $desktopDir)) { return $false }
  $desktopLink = Join-Path $desktopDir "Agent 桌宠.lnk"
  if (Test-Path -LiteralPath $desktopLink) {
    if ($Explicit) { Write-Host "[i] 桌面已经有「Agent 桌宠」快捷方式了，没有重复创建。" }
    return $true
  }
  # 桌面上已经有别的名字、但指向本桌宠的快捷方式？不重复建。
  $existingLink = ""
  try {
    $wsScan = New-Object -ComObject WScript.Shell
    foreach ($f in (Get-ChildItem -LiteralPath $desktopDir -Filter *.lnk -ErrorAction SilentlyContinue)) {
      try {
        $sc = $wsScan.CreateShortcut($f.FullName)
        if ($sc.Arguments -like "*pet-launcher.ps1*" -or $sc.TargetPath -like "*$here*") { $existingLink = $f.FullName; break }
      } catch {}
    }
  } catch {}
  if ($existingLink) {
    if ($Explicit) { Write-Host "[i] 桌面已有指向桌宠的快捷方式：$existingLink" }
    return $true
  }
  try {
    $icon = Join-Path $here "web\icon-transparent.ico"
    if (-not (Test-Path -LiteralPath $icon)) { $icon = Join-Path $here "web\icon.ico" }
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut($desktopLink)
    $lnk.TargetPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $lnk.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + (Join-Path $here "pet-launcher.ps1") + '"'
    $lnk.WorkingDirectory = $here
    $lnk.IconLocation = "$icon,0"
    $lnk.Description = "Agent 桌宠：Live2D 形象跟着接入的 Agent 实时状态变化"
    $lnk.Save()
    Write-Host "[OK] 已在桌面创建快捷方式：Agent 桌宠"
    return (Test-Path -LiteralPath $desktopLink)
  } catch {
    Write-Host ("[!] 创建桌面快捷方式失败：" + $_.Exception.Message)
    return $false
  }
}

if ($CreateDesktopLink) {
  if (New-DesktopLink -Explicit) {
    Set-Content -Path (Join-Path $here "desktop-link.done") -Value (Get-Date -Format "yyyy-MM-dd HH:mm:ss") -Encoding ASCII
  }
  exit 0
}

$python = Find-Python -Leaf "pythonw.exe"
if (-not $python) {
  Add-Type -AssemblyName System.Windows.Forms
  [System.Windows.Forms.MessageBox]::Show("没找到 Python 解释器。请把环境变量 CODEXPET_PYTHON 指向 python.exe，或用 诊断.cmd 查看详情。", "Agent 桌宠") | Out-Null
  exit 1
}

# 如果上一次没退干净（比如旧版本不认识停止请求），先按记录下来的 PID 收掉它
$pidFile = Join-Path $here "pet.pid"
if (Test-Path $pidFile) {
  $oldPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
  if ($oldPid -match '^\d+$') {
    $proc = Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue
    if ($proc -and $proc.ProcessName -match 'python') {
      Write-Host "关闭上一个桌宠进程 $oldPid"
      Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
      # 等旧进程真正退出：它的 WebView2 子进程还要一点时间才会放开
      # .webview2 用户数据目录；起太早新实例会报"资源在使用中 / 拒绝访问"。
      $deadline = (Get-Date).AddSeconds(15)
      while ((Get-Process -Id $proc.Id -ErrorAction SilentlyContinue) -and ((Get-Date) -lt $deadline)) {
        Start-Sleep -Milliseconds 250
      }
      Start-Sleep -Milliseconds 1500
    }
  }
}

# 首次启动：桌面上还没有「Agent 桌宠」快捷方式就自动建一个（只做一次）。
# 之后每次启动只看 desktop-link.done 标记；不喜欢桌面快捷方式可以在
# 设置 →「排查」里删掉，删掉后标记仍在，不会再自动重建。
$desktopDone = Join-Path $here "desktop-link.done"
if (-not (Test-Path -LiteralPath $desktopDone)) {
  if (New-DesktopLink) {
    Set-Content -Path $desktopDone -Value (Get-Date -Format "yyyy-MM-dd HH:mm:ss") -Encoding ASCII
  }
}

# 设置页「清除缓存并重启」会先放一个 clear-cache.request：
# 旧进程退出后，在这里删掉 .webview2（WebView2 浏览器缓存）再启动新实例。
$cacheFlag = Join-Path $here "clear-cache.request"
if (Test-Path $cacheFlag) {
  $cacheDir = Join-Path $here ".webview2"
  $cleared = $false
  for ($try = 1; $try -le 5; $try++) {
    try {
      if (Test-Path $cacheDir) {
        Remove-Item -LiteralPath $cacheDir -Recurse -Force -ErrorAction Stop
      }
      $cleared = $true
      break
    } catch {
      Start-Sleep -Milliseconds 1200
    }
  }
  Remove-Item -LiteralPath $cacheFlag -Force -ErrorAction SilentlyContinue
  if ($cleared) { Write-Host "[OK] 已清除 WebView2 缓存" }
  else { Write-Host "[!] 缓存暂时被占用，本次未清除（不影响启动）" }
}

Start-Process -FilePath $python -ArgumentList (Join-Path $here "run.py") -WorkingDirectory $here -WindowStyle Hidden
