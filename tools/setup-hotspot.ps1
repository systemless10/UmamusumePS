<#
.SYNOPSIS
    Prepare Windows Mobile Hotspot so the iPhone can reach this server on a
    network that blocks client-to-client traffic.

.DESCRIPTION
    Every Wi-Fi network worth using has AP isolation (guest/university/corporate
    networks nearly always do), which blocks phone->PC traffic outright. There is
    no setting on the phone that defeats it.

    The way around it is to stop sharing a network at all: this PC becomes the
    access point, the phone joins THAT, and the isolated upstream network never
    carries phone->PC traffic in the first place. This adapter reports
    "Number of Concurrent Channels Supported : 2", so it can stay joined to your
    normal Wi-Fi for internet while simultaneously hosting the hotspot.

    THE PART THAT ACTUALLY NEEDS A SCRIPT
    -------------------------------------
    Turning the hotspot on is two clicks in Settings, so this does not automate
    that. What it does handle is the non-obvious collision behind it:

    Mobile Hotspot is built on Internet Connection Sharing, and ICS runs its own
    DNS proxy bound to 192.168.137.1:53. That is the exact address and port
    tools/lan_dns.py needs. With ICS holding it, lan_dns.py cannot bind, and if
    you point the phone at the ICS proxy instead it resolves the game's domain
    honestly -- straight past your redirect. So ICS's DNS proxy has to be turned
    off while leaving its DHCP alone (the phone still needs an address).

    That is the EnableDnsProxy value this sets. DHCP stays on.

.PARAMETER Revert
    Restore ICS's DNS proxy (removes the EnableDnsProxy override). Use this if
    you want Mobile Hotspot behaving normally again for other purposes.

.PARAMETER Status
    Report only; change nothing.

.EXAMPLE
    .\tools\setup-hotspot.ps1 -Status
    .\tools\setup-hotspot.ps1
    .\tools\setup-hotspot.ps1 -Revert
#>
param(
    [switch]$Revert,
    [switch]$Status,
    [switch]$FixPower
)

$ErrorActionPreference = 'Stop'

$IcsParams = 'HKLM:\SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters'

function Test-Admin {
    ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-DnsProxyState {
    $v = (Get-ItemProperty -Path $IcsParams -ErrorAction SilentlyContinue).EnableDnsProxy
    # Unset means ENABLED: ICS defaults its DNS proxy on. An absent value here is
    # the problem state, not a neutral one -- which is why this reports it as
    # "on (default)" rather than "not configured".
    if ($null -eq $v) { return 'on (default)' }
    if ($v -eq 0) { return 'off' }
    return "on ($v)"
}

function Get-HotspotIp {
    $scope = (Get-ItemProperty -Path $IcsParams -ErrorAction SilentlyContinue).ScopeAddress
    if ($scope) { return $scope }
    return '192.168.137.1'
}

# --- report -----------------------------------------------------------------
$hotspotIp = Get-HotspotIp
Write-Host ''
Write-Host '--- current state ---------------------------------------------' -ForegroundColor Cyan
Write-Host ("  ICS hotspot address : {0}" -f $hotspotIp)
Write-Host ("  ICS DNS proxy       : {0}" -f (Get-DnsProxyState))
Write-Host ("  SharedAccess svc    : {0}" -f (Get-Service SharedAccess).Status)

# Is the hotspot interface actually NUMBERED? ICS is supposed to hold
# ScopeAddress (192.168.137.1) on it. When ICS falters the adapter drops to a
# 169.254.x.x link-local instead, and the whole thing unravels in a way that
# looks like a phone problem rather than a PC one: no DHCP and no internet for
# the client, and iOS auto-drops Wi-Fi networks that have no internet -- so the
# reported symptom is "my phone keeps disconnecting", several steps removed from
# the actual fault. Checked explicitly because the failure is otherwise invisible.
$hotspotIface = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                Where-Object { $_.IPAddress -eq $hotspotIp }
if ($hotspotIface) {
    Write-Host ("  hotspot interface   : UP, holding {0}" -f $hotspotIp) -ForegroundColor Green
}
else {
    $apipa = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
             Where-Object { $_.IPAddress -like '169.254.*' -and $_.InterfaceAlias -like 'Local Area Connection*' }
    if ($apipa) {
        Write-Host ("  hotspot interface   : BROKEN -- {0} has link-local {1}, not {2}" -f `
                    $apipa[0].InterfaceAlias, $apipa[0].IPAddress, $hotspotIp) -ForegroundColor Red
        Write-Host '                        ICS lost its address. The phone gets no DHCP and' -ForegroundColor Red
        Write-Host '                        no internet, so iOS drops the network. Toggle Mobile' -ForegroundColor Red
        Write-Host '                        Hotspot OFF and ON to make ICS re-number it.' -ForegroundColor Red
    }
    else {
        Write-Host '  hotspot interface   : not present -- Mobile Hotspot is off' -ForegroundColor Yellow
    }
}

$concurrent = (netsh wlan show wirelesscapabilities |
               Select-String 'Number of Concurrent Channels Supported') -replace '.*:\s*', ''
$go = (netsh wlan show wirelesscapabilities |
       Select-String 'Wi-Fi Direct GO') -replace '.*:\s*', ''
Write-Host ("  Wi-Fi Direct GO     : {0}" -f $go)
Write-Host ("  concurrent channels : {0}   (needs to be >1 to host while online)" -f $concurrent)

# Whoever holds UDP 53 right now. Get-NetUDPEndpoint has no OwningProcess-to-name
# mapping of its own, so resolve it -- "something has port 53" is not actionable,
# "the SharedAccess svchost has port 53" is.
$holder = Get-NetUDPEndpoint -LocalPort 53 -ErrorAction SilentlyContinue |
          Select-Object -First 1
if ($holder) {
    $proc = Get-Process -Id $holder.OwningProcess -ErrorAction SilentlyContinue
    Write-Host ("  UDP 53 held by      : {0} (pid {1})" -f $proc.ProcessName, $holder.OwningProcess) -ForegroundColor Yellow
}
else {
    Write-Host '  UDP 53 held by      : nothing -- free for lan_dns.py' -ForegroundColor Green
}
Write-Host ''

if ($Status) { exit 0 }

if (-not (Test-Admin)) {
    Write-Host 'Changing the ICS DNS proxy needs admin. Re-run this in an' -ForegroundColor Red
    Write-Host 'administrator PowerShell.' -ForegroundColor Red
    exit 1
}

# --- -FixPower: stop Windows powering down the hotspot's adapter ------------
# Symptom this addresses, from the System event log:
#   Microsoft-Windows-NDIS 10317 -- "Miniport Microsoft Wi-Fi Direct Virtual
#   Adapter #2 had event Fatal error: The miniport has failed a power
#   transition to operational power"
# The virtual adapter hosting the hotspot gets suspended and never comes back.
# ICS then loses 192.168.137.1, the client loses DHCP and internet, and iOS
# auto-drops Wi-Fi networks with no internet -- surfacing as "my phone keeps
# disconnecting", which points at entirely the wrong machine.
#
# The usual fix is Device Manager > adapter > Power Management > uncheck "Allow
# the computer to turn off this device". THIS REALTEK DRIVER DOES NOT EXPOSE
# THAT TAB (General/Advanced/Driver/Details/Events/Resources only), so the
# checkbox has to be written directly: PnPCapabilities under the adapter's
# class key is what that checkbox actually sets. 24 = 0x18 = bit 0x08 (do not
# allow D3/power-down) + bit 0x10 (do not allow wake), i.e. both boxes cleared.
if ($FixPower) {
    $classKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e972-e325-11ce-bfc1-08002be10318}'
    $touched = 0
    Get-ChildItem $classKey -ErrorAction SilentlyContinue | ForEach-Object {
        $props = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
        # Both the physical radio and the Wi-Fi Direct virtual adapters matter:
        # the virtual one is what actually faulted, but it is backed by the
        # physical radio's power state, so leaving either managed keeps the bug.
        if ($props.DriverDesc -match 'RTL8852|Wi-Fi Direct Virtual') {
            Set-ItemProperty -Path $_.PSPath -Name PnPCapabilities -Value 24 -Type DWord
            Write-Host ("  PnPCapabilities=24 on {0}" -f $props.DriverDesc) -ForegroundColor Green
            $touched++
        }
    }
    if ($touched -eq 0) {
        Write-Host '  no matching adapters found -- nothing changed.' -ForegroundColor Yellow
    }

    # Realtek's own power saver, separate from the Windows checkbox. It offers
    # no "disabled", so Low is the least aggressive setting available.
    $lps = Get-NetAdapterAdvancedProperty -Name 'Wi-Fi' -RegistryKeyword 'LpsEn' -ErrorAction SilentlyContinue
    if ($lps -and $lps.DisplayValue -ne 'Low') {
        Set-NetAdapterAdvancedProperty -Name 'Wi-Fi' -RegistryKeyword 'LpsEn' -DisplayValue 'Low'
        Write-Host ("  Leisure Power Save -> Low (was {0})" -f $lps.DisplayValue) -ForegroundColor Green
    }

    Write-Host ''
    Write-Host 'Power settings written. These take effect when the adapter reloads:' -ForegroundColor Yellow
    Write-Host '  disable + re-enable the Wi-Fi adapter in Device Manager, or reboot.' -ForegroundColor Yellow
    Write-Host 'Then toggle Mobile Hotspot off and on.' -ForegroundColor Yellow
    Write-Host ''
}

# --- change -----------------------------------------------------------------
if ($Revert) {
    Remove-ItemProperty -Path $IcsParams -Name EnableDnsProxy -ErrorAction SilentlyContinue
    Write-Host 'ICS DNS proxy restored to default (on).' -ForegroundColor Green
}
else {
    Set-ItemProperty -Path $IcsParams -Name EnableDnsProxy -Value 0 -Type DWord
    Write-Host 'ICS DNS proxy disabled (DHCP left alone).' -ForegroundColor Green
}

# The service reads these at start, so a running ICS keeps the old behaviour
# until bounced. If it is stopped, leave it -- toggling the hotspot starts it.
$svc = Get-Service SharedAccess
if ($svc.Status -eq 'Running') {
    Write-Host 'Restarting SharedAccess so the change takes effect...' -ForegroundColor Cyan
    Restart-Service SharedAccess -Force
    Write-Host 'Restarted. Toggle Mobile Hotspot off and on again as well.' -ForegroundColor Yellow
}

# --- firewall ---------------------------------------------------------------
# Without these the phone's packets are dropped with no error anywhere: no log
# line on this side, no refusal on the phone's, just a game that hangs. It is
# the single most common way this setup appears broken while being correct.
#
# Both rules are SCOPED TO THE HOTSPOT SUBNET on purpose. The Wi-Fi interface is
# categorised Public (university/cafe/corporate), and an unscoped "allow 443
# inbound" would expose this server to every stranger on that network. RemoteAddress
# keeps it reachable only from devices attached to this PC's own hotspot.
$scopePrefix = ($hotspotIp -replace '\.\d+$', '.0/24')

# 8080 is for handing the CA file to the phone. Windows cannot AirDrop, and the
# phone must fetch ca.cert.cer from somewhere before it can trust anything --
# but every port is blocked inbound by default, so without this the download
# just hangs. Same subnet scoping as the rest.
$rules = @(
    @{ Name = 'UmaPS server (hotspot)'; Port = 443;  Protocol = 'TCP' },
    @{ Name = 'UmaPS DNS (hotspot)';    Port = 53;   Protocol = 'UDP' },
    @{ Name = 'UmaPS cert serve';       Port = 8080; Protocol = 'TCP' }
)
foreach ($rule in $rules) {
    $existing = Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue
    if ($Revert) {
        if ($existing) {
            Remove-NetFirewallRule -DisplayName $rule.Name
            Write-Host ("Removed firewall rule '{0}'." -f $rule.Name) -ForegroundColor Green
        }
        continue
    }
    if ($existing) {
        Write-Host ("Firewall rule '{0}' already present." -f $rule.Name) -ForegroundColor Green
        continue
    }
    New-NetFirewallRule -DisplayName $rule.Name `
        -Direction Inbound -Action Allow `
        -Protocol $rule.Protocol -LocalPort $rule.Port `
        -RemoteAddress $scopePrefix `
        -Profile Any | Out-Null
    Write-Host ("Added firewall rule '{0}' ({1}/{2}, from {3} only)." -f `
                $rule.Name, $rule.Port, $rule.Protocol, $scopePrefix) -ForegroundColor Green
}

Write-Host ''
Write-Host '--- next steps ------------------------------------------------' -ForegroundColor Cyan
Write-Host '1. Settings > Network & internet > Mobile hotspot > ON'
Write-Host '   Share over: Wi-Fi.  Note the network name and password.'
Write-Host '2. Join that hotspot from the iPhone.'
Write-Host '3. On the iPhone: Settings > Wi-Fi > (i) > Configure DNS > Manual,'
Write-Host ("   remove all entries, add:  {0}" -f $hotspotIp)
Write-Host '4. Run the DNS responder (as admin), pinned to the hotspot address:'
Write-Host ("   server\.venv\Scripts\python.exe tools\lan_dns.py --ip {0}" -f $hotspotIp)
Write-Host '5. Start the server:  .\start-server.ps1 -IosCerts'
Write-Host ''
Write-Host 'Step 4 needs --ip explicitly: auto-detection finds the address that' -ForegroundColor Yellow
Write-Host 'reaches the INTERNET (your upstream Wi-Fi), not the hotspot one the' -ForegroundColor Yellow
Write-Host 'phone will actually be talking to. lan_dns.py now detects the hotspot' -ForegroundColor Yellow
Write-Host 'address itself and warns, but passing it is the certain route.' -ForegroundColor Yellow
