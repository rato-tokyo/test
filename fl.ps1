[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RootPath,

    [Parameter(Mandatory = $false)]
    [string]$OutputDirectory = (Join-Path $env:USERPROFILE "Desktop\FolderAccessSurvey")
)

$ErrorActionPreference = "Stop"

$rootItem = Get-Item -LiteralPath $RootPath -Force
if (-not $rootItem.PSIsContainer) {
    throw "RootPath must be a directory: $RootPath"
}

$normalizedRoot = $rootItem.FullName.TrimEnd("\")
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$resolvedOutput = (Get-Item -LiteralPath $OutputDirectory -Force).FullName

if ($resolvedOutput -eq $normalizedRoot -or
    $resolvedOutput.StartsWith($normalizedRoot + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "OutputDirectory must be outside RootPath."
}

$windowsIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$tokenSidSet = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
[void]$tokenSidSet.Add($windowsIdentity.User.Value)
foreach ($groupSid in $windowsIdentity.Groups) {
    [void]$tokenSidSet.Add($groupSid.Value)
}

$authenticatedUsersSid = "S-1-5-11"
$listDirectoryRight = [System.Security.AccessControl.FileSystemRights]::ListDirectory
$queue = New-Object 'System.Collections.Generic.Queue[string]'
$queue.Enqueue($normalizedRoot)
$records = New-Object 'System.Collections.Generic.List[object]'

while ($queue.Count -gt 0) {
    $folderPath = $queue.Dequeue()
    $relativePath = if ($folderPath -eq $normalizedRoot) {
        "."
    }
    else {
        $folderPath.Substring($normalizedRoot.Length).TrimStart("\")
    }
    $depth = if ($relativePath -eq ".") { 0 } else { ($relativePath -split "\\").Count }

    $aclStatus = "Readable"
    $estimatedAccess = "Unknown"
    $authenticatedUsersAce = "Absent"
    $matchingAllowSids = New-Object 'System.Collections.Generic.List[string]'
    $matchingDenySids = New-Object 'System.Collections.Generic.List[string]'
    $owner = ""
    $enumerated = $false
    $childDirectoryCount = 0
    $errorMessage = ""

    try {
        $acl = Get-Acl -LiteralPath $folderPath -ErrorAction Stop
        $owner = $acl.Owner
        $rules = $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])

        foreach ($rule in $rules) {
            $ruleSid = $rule.IdentityReference.Value
            if ($ruleSid -eq $authenticatedUsersSid) {
                $authenticatedUsersAce = "Present"
            }

            $grantsListDirectory = (($rule.FileSystemRights -band $listDirectoryRight) -ne 0)
            if ($grantsListDirectory -and $tokenSidSet.Contains($ruleSid)) {
                if ($rule.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Deny) {
                    [void]$matchingDenySids.Add($ruleSid)
                }
                else {
                    [void]$matchingAllowSids.Add($ruleSid)
                }
            }
        }

        if ($matchingDenySids.Count -gt 0) {
            $estimatedAccess = "LikelyDenied"
        }
        elseif ($matchingAllowSids.Count -gt 0) {
            $estimatedAccess = "LikelyAllowed"
        }
        else {
            $estimatedAccess = "NoMatchingListAllow"
        }
    }
    catch {
        $aclStatus = "Unreadable"
        $estimatedAccess = "AclUnreadable"
        $errorMessage = $_.Exception.Message
    }

    if ($estimatedAccess -eq "LikelyAllowed") {
        try {
            $childDirectories = @(
                Get-ChildItem -LiteralPath $folderPath -Directory -Force -ErrorAction Stop |
                    Where-Object { -not ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) }
            )
            $enumerated = $true
            $childDirectoryCount = $childDirectories.Count
            foreach ($childDirectory in $childDirectories) {
                $queue.Enqueue($childDirectory.FullName)
            }
        }
        catch {
            $estimatedAccess = "EnumerationFailed"
            $errorMessage = $_.Exception.Message
        }
    }

    $records.Add([PSCustomObject]@{
        Depth                 = $depth
        RelativePath          = $relativePath
        FullPath              = $folderPath
        AclStatus             = $aclStatus
        EstimatedAccess       = $estimatedAccess
        AuthenticatedUsersAce = $authenticatedUsersAce
        MatchingAllowSids     = ($matchingAllowSids -join ";")
        MatchingDenySids      = ($matchingDenySids -join ";")
        Owner                 = $owner
        Enumerated            = $enumerated
        ChildDirectoryCount   = $childDirectoryCount
        Error                 = $errorMessage
    })
}

$csvPath = Join-Path $resolvedOutput "folder_access.csv"
$treePath = Join-Path $resolvedOutput "folder_tree.txt"
$summaryPath = Join-Path $resolvedOutput "summary.txt"

$records | Export-Csv -LiteralPath $csvPath -NoTypeInformation -Encoding UTF8
$records | ForEach-Object {
    $indent = "  " * $_.Depth
    "$indent[D] $($_.RelativePath) [$($_.EstimatedAccess)]"
} | Set-Content -LiteralPath $treePath -Encoding UTF8

$allowedCount = @($records | Where-Object EstimatedAccess -eq "LikelyAllowed").Count
$skippedCount = @($records | Where-Object EstimatedAccess -ne "LikelyAllowed").Count
@(
    "Root: $normalizedRoot"
    "User SID: $($windowsIdentity.User.Value)"
    "Folders inspected: $($records.Count)"
    "Likely allowed: $allowedCount"
    "Skipped or failed: $skippedCount"
    "Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
) | Set-Content -LiteralPath $summaryPath -Encoding UTF8

[PSCustomObject]@{
    Root            = $normalizedRoot
    OutputDirectory = $resolvedOutput
    FoldersInspected = $records.Count
    LikelyAllowed   = $allowedCount
    SkippedOrFailed = $skippedCount
    CsvPath         = $csvPath
    TreePath        = $treePath
    SummaryPath     = $summaryPath
}
