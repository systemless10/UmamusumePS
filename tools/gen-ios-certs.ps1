<#
.SYNOPSIS
    Generate an iOS-acceptable CA + leaf cert pair for the private server.

.DESCRIPTION
    The existing server/certs/server.cert.pem is a single self-signed cert with
    CA:TRUE, a 3650-day lifetime and no extendedKeyUsage. Windows and Wine accept
    that; iOS 13+ does not. Three separate problems:

      * no EKU serverAuth        -- iOS requires it on a TLS server cert
      * CA:TRUE used as the leaf -- a cert cannot be both anchor and end entity
      * 3650-day lifetime        -- over Apple's 398-day cap for server certs
                                    (Apple documents an exemption for
                                    user-installed roots, but relying on it buys
                                    nothing when a compliant cert is free)

    So this emits a real two-level chain instead:

      ca.cert.pem      the root. Install THIS on the iPhone. 10 years -- the
                       398-day cap is a server-cert rule, roots are exempt.
      ca.cert.cer      the same root, DER-encoded. Prefer this one for the
                       actual phone install: Safari and AirDrop reliably fire
                       the "Profile Downloaded" flow for .cer, and sometimes
                       just display a .pem as text.
      server.cert.pem  the leaf the server presents. 397 days, SAN, EKU
                       serverAuth. Regenerate with -LeafOnly when it expires;
                       the CA (and so the phone's trust) stays put.
      server.chain.pem leaf + CA concatenated. This is what you actually serve
                       -- iOS will not chase a missing intermediate.
      server.key.pem   the leaf's private key.

    Everything lands in server/certs/ios/ and NOTHING existing is touched, so
    the Steam client keeps working off the old cert while you set this up.

.PARAMETER Domain
    Primary SAN. Defaults to the Global client's host. The JP host is not a
    useful target here -- see docs/HANDOFF_JP_CRYPTO.md for why a JP client
    cannot talk to this server at all, cert or no cert.

.PARAMETER ExtraNames
    Additional SANs. Pass your PC's LAN IP if you ever want to hit the server
    by address rather than by name; it is added as an IP SAN automatically.

.PARAMETER LeafOnly
    Reissue just the leaf from the existing CA. Use this at the 397-day mark --
    it does not disturb the CA, so the iPhone needs no reinstall.

.PARAMETER InstallToWindows
    Also drop the CA into the Windows CurrentUser\Root store. Do this if you
    want ONE cert serving both the PC client and the phone; see the note this
    script prints at the end.

.EXAMPLE
    .\tools\gen-ios-certs.ps1
    .\tools\gen-ios-certs.ps1 -ExtraNames 192.168.1.50 -InstallToWindows
    .\tools\gen-ios-certs.ps1 -LeafOnly
#>
param(
    [string]$Domain = 'api.games.umamusume.com',
    [string[]]$ExtraNames = @(),
    [switch]$LeafOnly,
    [switch]$InstallToWindows
)

$ErrorActionPreference = 'Stop'

$Root    = Split-Path -Parent $PSScriptRoot
$CertDir = Join-Path $Root 'server\certs\ios'

$CaKey    = Join-Path $CertDir 'ca.key.pem'
$CaCert   = Join-Path $CertDir 'ca.cert.pem'
$CaDer    = Join-Path $CertDir 'ca.cert.cer'
$LeafKey  = Join-Path $CertDir 'server.key.pem'
$LeafCsr  = Join-Path $CertDir 'server.csr.pem'
$LeafCert = Join-Path $CertDir 'server.cert.pem'
$Chain    = Join-Path $CertDir 'server.chain.pem'
$ExtFile  = Join-Path $CertDir 'leaf.ext.cnf'

if (-not (Get-Command openssl -ErrorAction SilentlyContinue)) {
    throw "openssl not on PATH. Git for Windows ships one at C:\Program Files\Git\mingw64\bin."
}
if (-not (Test-Path $CertDir)) { New-Item -ItemType Directory -Path $CertDir -Force | Out-Null }

# openssl writes progress chatter ("...+...+++++") to stderr even on success.
# Under Windows PowerShell 5.1, redirecting a native exe's stderr with 2>&1
# wraps every line in an ErrorRecord, and with $ErrorActionPreference='Stop'
# that terminates the script on a command that actually SUCCEEDED. So the
# redirect is done with a function-local preference of 'Continue', and success
# is judged only by exit code -- the sole reliable signal for a native process.
function Invoke-OpenSsl {
    param([string[]]$Arguments)
    $ErrorActionPreference = 'Continue'
    $out = & openssl @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        $out | Out-String | Write-Host
        throw "openssl $($Arguments[0]) failed (exit $LASTEXITCODE)"
    }
}

# --- the CA -----------------------------------------------------------------
if ($LeafOnly) {
    if (-not (Test-Path $CaCert)) { throw "-LeafOnly needs an existing CA at $CaCert. Run without it first." }
    Write-Host "Reusing existing CA at $CaCert" -ForegroundColor Cyan
}
else {
    if (Test-Path $CaCert) {
        Write-Host 'A CA already exists here.' -ForegroundColor Yellow
        Write-Host 'Replacing it invalidates the copy already trusted on the iPhone --' -ForegroundColor Yellow
        Write-Host 'you would have to delete and reinstall the profile there.' -ForegroundColor Yellow
        Write-Host 'Use -LeafOnly to just reissue the server cert instead.' -ForegroundColor Yellow
        $reply = Read-Host 'Replace the CA anyway? (y/N)'
        if ($reply -ne 'y') { Write-Host 'Aborted.'; exit 0 }
    }
    Write-Host 'Generating root CA (10 years)...' -ForegroundColor Cyan
    Invoke-OpenSsl @(
        'req', '-x509', '-newkey', 'rsa:4096', '-sha256',
        '-keyout', $CaKey, '-out', $CaCert,
        '-days', '3650', '-nodes',
        '-subj', '/CN=UmaPS Local CA/O=UmaPS',
        '-addext', 'basicConstraints=critical,CA:TRUE,pathlen:0',
        '-addext', 'keyUsage=critical,keyCertSign,cRLSign'
    )
    Invoke-OpenSsl @('x509', '-in', $CaCert, '-outform', 'DER', '-out', $CaDer)
}

# --- the leaf ---------------------------------------------------------------
# SANs: DNS entries for names, IP entries for anything that parses as an
# address. iOS ignores CN entirely, so a name missing from here does not work,
# full stop -- no silent fallback to the subject like older stacks had.
$sanEntries = @("DNS:$Domain")
foreach ($name in $ExtraNames) {
    $parsed = [System.Net.IPAddress]::Any
    if ([System.Net.IPAddress]::TryParse($name, [ref]$parsed)) { $sanEntries += "IP:$name" }
    else { $sanEntries += "DNS:$name" }
}
$san = $sanEntries -join ','

@"
basicConstraints = CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = $san
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
"@ | Set-Content -Path $ExtFile -Encoding ASCII

Write-Host "Generating leaf for $san (397 days)..." -ForegroundColor Cyan
Invoke-OpenSsl @(
    'req', '-newkey', 'rsa:2048', '-sha256', '-nodes',
    '-keyout', $LeafKey, '-out', $LeafCsr,
    '-subj', "/CN=$Domain"
)
Invoke-OpenSsl @(
    'x509', '-req', '-sha256',
    '-in', $LeafCsr,
    '-CA', $CaCert, '-CAkey', $CaKey, '-CAcreateserial',
    '-out', $LeafCert,
    '-days', '397',
    '-extfile', $ExtFile
)
Remove-Item $LeafCsr, $ExtFile -ErrorAction SilentlyContinue

# Serve leaf-then-CA. Order matters: the leaf must come first, and omitting the
# CA is the single most common cause of an iOS handshake failing even though
# the root is installed and trusted.
Set-Content -Path $Chain -Value ((Get-Content $LeafCert -Raw) + (Get-Content $CaCert -Raw)) -Encoding ASCII -NoNewline

# --- verify, rather than assume -------------------------------------------
Invoke-OpenSsl @('verify', '-CAfile', $CaCert, $LeafCert)
Write-Host ''
Write-Host 'Chain verified. Leaf:' -ForegroundColor Green
& openssl x509 -in $LeafCert -noout -subject -issuer -dates -ext subjectAltName,extendedKeyUsage,basicConstraints

if ($InstallToWindows) {
    Write-Host ''
    Write-Host 'Installing CA into CurrentUser\Root...' -ForegroundColor Cyan
    Import-Certificate -FilePath $CaCert -CertStoreLocation Cert:\CurrentUser\Root | Out-Null
    Write-Host 'Installed.' -ForegroundColor Green
}

Write-Host ''
Write-Host '--- next steps ------------------------------------------------' -ForegroundColor Cyan
Write-Host "1. Get this onto the iPhone:  $CaDer"
Write-Host '   AirDrop it, or serve it and open the URL in SAFARI (not Chrome).'
Write-Host '2. Settings > Profile Downloaded > Install.'
Write-Host '3. Settings > General > About > Certificate Trust Settings >'
Write-Host '   enable full trust for "UmaPS Local CA".  <-- the step everyone misses;'
Write-Host '   without it the cert is installed but NOT trusted for TLS.'
Write-Host '4. Start the server with:  .\start-server.ps1 -IosCerts'
Write-Host ''
Write-Host 'Note: the PC client still trusts the OLD cert, so -IosCerts serves a'
Write-Host 'chain it does not know. To run one cert for both, re-run this with'
Write-Host '-InstallToWindows and use -IosCerts everywhere.'
