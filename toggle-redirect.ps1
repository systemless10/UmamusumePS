<#
.SYNOPSIS
    Toggle the private-server DNS redirect for the Umamusume client domains.

.DESCRIPTION
    Flips the hosts-file entry that points the Umamusume client at the local
    private server. Editing the hosts file needs admin, so this self-elevates
    (one UAC prompt) and flushes the DNS cache afterwards.

    -Domain selects which client build's host gets redirected: 'global' is
    api.games.umamusume.com (the Steam client, default, original behavior),
    'jp' is api.games.umamusume.jp (the DMM/mobile client).

.EXAMPLE
    .\toggle-redirect.ps1                     # flip global (default)
    .\toggle-redirect.ps1 on                  # force ON  (client -> private server)
    .\toggle-redirect.ps1 off                 # force OFF (client -> real server)
    .\toggle-redirect.ps1 status              # just report, change nothing
    .\toggle-redirect.ps1 on -Domain jp       # same, but for the JP domain
#>
param(
    [ValidateSet('toggle', 'on', 'off', 'status')]
    [string]$Action = 'toggle',

    [ValidateSet('global', 'jp')]
    [string]$Domain = 'global'
)

$DomainMap  = @{ global = 'api.games.umamusume.com'; jp = 'api.games.umamusume.jp' }
$HostsPath  = "$env:SystemRoot\System32\drivers\etc\hosts"
$TargetHost = $DomainMap[$Domain]
$ActiveLine   = "127.0.0.1 $TargetHost   # private-server redirect ($Domain)"
$DisabledLine = "# 127.0.0.1 $TargetHost   # private-server redirect ($Domain) (disabled)"

function Get-RedirectState {
    if (-not (Test-Path $HostsPath)) { return 'absent' }
    foreach ($line in (Get-Content -Path $HostsPath)) {
        if ($line -match [regex]::Escape($TargetHost)) {
            if ($line -match '^\s*#') { return 'off' } else { return 'on' }
        }
    }
    return 'absent'
}

$state = Get-RedirectState

if ($Action -eq 'status') {
    Write-Host "redirect: $state"
    exit 0
}

if ($Action -eq 'on')      { $target = 'on' }
elseif ($Action -eq 'off') { $target = 'off' }
elseif ($state -eq 'on')   { $target = 'off' }
else                       { $target = 'on' }

if ($state -eq $target) {
    Write-Host "redirect already $target - nothing to do"
    exit 0
}

# Editing hosts requires admin: re-launch this script elevated with an explicit
# target (never 'toggle', so the elevated run can't flip the wrong way).
$isAdmin = ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent() `
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Start-Process powershell -Verb RunAs -Wait -ArgumentList `
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", $target, "-Domain", $Domain
    Write-Host "redirect: $(Get-RedirectState)"
    exit 0
}

$replacement = if ($target -eq 'on') { $ActiveLine } else { $DisabledLine }
$newLines = @()
$written  = $false
foreach ($line in (Get-Content -Path $HostsPath)) {
    if ($line -match [regex]::Escape($TargetHost)) {
        if (-not $written) { $newLines += $replacement; $written = $true }   # collapse duplicates
    }
    else { $newLines += $line }
}
if (-not $written) { $newLines += $replacement }

# Set-Content here has been observed to silently truncate the hosts file to
# 0 bytes under some race (not reproducible on demand, root cause unclear --
# Defender's on-access scanner opening the file mid-write is the leading
# suspect, but no detection event ever correlated). Whatever the cause,
# verify-and-retry made it go away in practice: write, then confirm the
# file is non-empty before trusting it, retrying a few times if not.
$ok = $false
for ($attempt = 1; $attempt -le 8; $attempt++) {
    Set-Content -Path $HostsPath -Value $newLines -Encoding ASCII
    Start-Sleep -Milliseconds 300
    if ((Get-Item $HostsPath).Length -gt 0) { $ok = $true; break }
}
if (-not $ok) {
    Write-Host "redirect: WRITE FAILED -- hosts file is 0 bytes after $attempt attempts. Not flushing DNS. Re-run this script." -ForegroundColor Red
    exit 1
}
ipconfig /flushdns | Out-Null
Write-Host "redirect: $target"
