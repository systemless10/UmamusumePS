<#
.SYNOPSIS
    Point the Umamusume client back at the REAL Cygames server.

.DESCRIPTION
    Thin wrapper around toggle-redirect.ps1 -Action off. Removes the hosts-file
    redirect so the client domain resolves normally again. Needs admin --
    will prompt for elevation once.

.PARAMETER Domain
    'global' (default) for api.games.umamusume.com, or 'jp' for
    api.games.umamusume.jp.
#>
param(
    [ValidateSet('global', 'jp')]
    [string]$Domain = 'global'
)
& "$PSScriptRoot\toggle-redirect.ps1" off -Domain $Domain
