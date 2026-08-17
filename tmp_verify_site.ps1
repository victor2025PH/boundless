$ErrorActionPreference = 'Continue'
$html = (Invoke-WebRequest -Uri 'https://bd2026.cc/' -UseBasicParsing -TimeoutSec 30).Content
Write-Output ("homepage html length: " + $html.Length)
if ($html -like '*href="/download"*' -or $html -like '*href="/en/download"*') {
    Write-Output 'HOMEPAGE-HAS-DOWNLOAD-LINK'
} else {
    Write-Output 'HOMEPAGE-NO-DOWNLOAD-LINK'
    $idx = $html.IndexOf('nav')
    if ($idx -ge 0) {
        $s = [Math]::Max(0, $idx - 100)
        Write-Output $html.Substring($s, [Math]::Min(800, $html.Length - $s))
    }
}
