# start-windows.bat 이 호출합니다. 시스템에 Node.js 20 이상이 있으면 그것을 쓰고,
# 없으면 관리자 권한 없이 이 폴더 안(.runtime)에 Node.js 를 내려받아 씁니다.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest 가 훨씬 빨라집니다
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$NodeVersion = "v24.22.3"

function Get-NodeMajor([string]$Exe) {
  try {
    $v = & $Exe -v 2>$null
    if ($v -match '^v(\d+)\.') { return [int]$Matches[1] }
  } catch {}
  return 0
}

# ffmpeg 패키지에 Windows ARM 전용 버전이 없어서, ARM PC에서는 에뮬레이션되는 x64(윈도우 10은 x86) Node 를 씁니다.
$isArm = $env:PROCESSOR_ARCHITECTURE -eq "ARM64" -or $env:PROCESSOR_ARCHITEW6432 -eq "ARM64"
$arch = "x64"
if ($isArm -and [Environment]::OSVersion.Version.Build -lt 22000) {
  $arch = "x86"
  $NodeVersion = "v22.23.4"   # Node 24 부터는 32비트(x86) 윈도우용 빌드가 없습니다
}

$node = $null
$sys = Get-Command node -ErrorAction SilentlyContinue
if ($sys -and (Get-NodeMajor $sys.Source) -ge 20) {
  $sysArch = ""
  try { $sysArch = (& $sys.Source -p "process.arch" 2>$null) } catch {}
  if ($sysArch -ne "arm64") { $node = $sys.Source }
}

$local = Join-Path $Root ".runtime\node\node.exe"
if (-not $node -and (Test-Path $local)) { $node = $local }

if (-not $node) {
  $name = "node-$NodeVersion-win-$arch"
  $runtime = Join-Path $Root ".runtime"
  $zip = Join-Path $runtime "node.zip"
  Write-Host "▶ Node.js 를 내려받는 중 (처음 한 번만)…"
  New-Item -ItemType Directory -Force -Path $runtime | Out-Null
  try {
    Invoke-WebRequest -Uri "https://nodejs.org/dist/$NodeVersion/$name.zip" -OutFile $zip -UseBasicParsing
  } catch {
    Write-Host "✗ Node.js 를 내려받지 못했습니다. 인터넷 연결을 확인하거나 https://nodejs.org 에서 LTS 버전을 설치한 뒤 다시 실행하세요."
    exit 1
  }
  $tmp = Join-Path $runtime "tmp"
  if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
  New-Item -ItemType Directory -Force -Path $tmp | Out-Null
  # Windows 내장 tar 는 260자 경로 제한에 걸리지 않습니다. 없으면 Expand-Archive 로 대신합니다.
  $tar = Join-Path $env:SystemRoot "System32\tar.exe"
  if (Test-Path $tar) {
    & $tar -xf $zip -C $tmp
    if ($LASTEXITCODE -ne 0) { Write-Host "✗ Node.js 압축을 풀지 못했습니다."; exit 1 }
  } else {
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
  }
  $dest = Join-Path $runtime "node"
  if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
  Move-Item (Join-Path $tmp $name) $dest
  Remove-Item -Recurse -Force $tmp
  Remove-Item -Force $zip
  $node = $local
}

$env:Path = (Split-Path -Parent $node) + ";" + $env:Path
& $node (Join-Path $Root "scripts\launch.js")
exit $LASTEXITCODE
