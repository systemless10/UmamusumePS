<#
.SYNOPSIS
    Point the Umamusume client at the LOCAL private server.

.DESCRIPTION
    Thin wrapper around toggle-redirect.ps1 -Action on. Adds/re-enables the
    hosts-file redirect so the client domain resolves to 127.0.0.1.
    Needs admin -- will prompt for elevation once.

.PARAMETER Domain
    'global' (default) for api.games.umamusume.com, or 'jp' for
    api.games.umamusume.jp.
#>
param(
    [ValidateSet('global', 'jp')]
    [string]$Domain = 'global'
)
& "$PSScriptRoot\toggle-redirect.ps1" on -Domain $Domain
