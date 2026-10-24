#requires -Version 5.1
[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    Add-Type -AssemblyName System.Security
} catch {
    Write-Error 'Windows 설정 창을 시작할 수 없습니다. Windows PowerShell에서 다시 실행하세요.'
    exit 2
}

[System.Windows.Forms.Application]::EnableVisualStyles()
$projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$localDirectory = [System.IO.Path]::Combine($projectRoot, '.local')
$credentialPath = [System.IO.Path]::Combine($localDirectory, 'credentials.dpapi')
$currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$systemSid = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')

function Set-PrivateDirectoryAcl {
    param([string]$Path)
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    foreach ($sid in @($currentSid, $systemSid)) {
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, [System.Security.AccessControl.FileSystemRights]::FullControl, $inheritance, [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Allow)
        [void]$acl.AddAccessRule($rule)
    }
    [System.IO.Directory]::SetAccessControl($Path, $acl)
}

function Set-PrivateFileAcl {
    param([string]$Path)
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in @($currentSid, $systemSid)) {
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, [System.Security.AccessControl.FileSystemRights]::FullControl, [System.Security.AccessControl.AccessControlType]::Allow)
        [void]$acl.AddAccessRule($rule)
    }
    [System.IO.File]::SetAccessControl($Path, $acl)
}

function Save-ProtectedCredentials {
    param(
        [string]$ClientId,
        [string]$ClientSecret,
        [AllowNull()][string]$AccountSeq,
        [bool]$EnableTrading
    )
    $plaintextBytes = $null
    $protectedBytes = $null
    $temporaryPath = [System.IO.Path]::Combine($localDirectory, ([guid]::NewGuid().ToString('N') + '.tmp'))
    $aclRestricted = $true
    try {
        [void][System.IO.Directory]::CreateDirectory($localDirectory)
        try { Set-PrivateDirectoryAcl -Path $localDirectory } catch { $aclRestricted = $false }

        $payload = [ordered]@{
            client_id = $ClientId
            client_secret = $ClientSecret
            account_seq = $AccountSeq
            enable_trading = $EnableTrading
        } | ConvertTo-Json -Compress
        $plaintextBytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
        $protectedBytes = [System.Security.Cryptography.ProtectedData]::Protect($plaintextBytes, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
        $encoded = [Convert]::ToBase64String($protectedBytes)
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($temporaryPath, $encoded, $utf8)
        try { Set-PrivateFileAcl -Path $temporaryPath } catch { $aclRestricted = $false }

        if ([System.IO.File]::Exists($credentialPath)) {
            [System.IO.File]::Replace($temporaryPath, $credentialPath, $null)
        } else {
            [System.IO.File]::Move($temporaryPath, $credentialPath)
        }
        try { Set-PrivateFileAcl -Path $credentialPath } catch { $aclRestricted = $false }
        return $aclRestricted
    } finally {
        if ($null -ne $plaintextBytes) { [Array]::Clear($plaintextBytes, 0, $plaintextBytes.Length) }
        if ($null -ne $protectedBytes) { [Array]::Clear($protectedBytes, 0, $protectedBytes.Length) }
        $payload = $null
        $encoded = $null
        if ([System.IO.File]::Exists($temporaryPath)) {
            try { [System.IO.File]::Delete($temporaryPath) } catch { }
        }
    }
}

$form = New-Object System.Windows.Forms.Form
$form.Text = '토스증권 MCP 연결 설정'
$form.ClientSize = New-Object System.Drawing.Size(580, 490)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.ShowInTaskbar = $true
$form.Add_Shown({
    $form.WindowState = [System.Windows.Forms.FormWindowState]::Normal
    $form.TopMost = $true
    $form.Activate()
    $form.BringToFront()
    $idBox.Focus()
})
$form.Font = New-Object System.Drawing.Font('Malgun Gothic', 9)
$form.DialogResult = [System.Windows.Forms.DialogResult]::Cancel

$intro = New-Object System.Windows.Forms.Label
$intro.Location = New-Object System.Drawing.Point(22, 18)
$intro.Size = New-Object System.Drawing.Size(536, 56)
$intro.Text = "토스증권 WTS의 설정 → Open API에서 Client ID와 Secret을 발급하세요.`r`n같은 화면의 허용 IP 관리에 이 PC의 공인 IP를 등록해야 연결됩니다."
$form.Controls.Add($intro)

$docs = New-Object System.Windows.Forms.LinkLabel
$docs.Location = New-Object System.Drawing.Point(22, 77)
$docs.Size = New-Object System.Drawing.Size(530, 25)
$docs.Text = '공식 Open API 안내 열기'
$docs.Add_LinkClicked({
    try { Start-Process 'https://developers.tossinvest.com/' | Out-Null } catch {
        [void][System.Windows.Forms.MessageBox]::Show($form, '브라우저에서 https://developers.tossinvest.com/ 에 접속하세요.', '공식 문서')
    }
})
$form.Controls.Add($docs)

$idLabel = New-Object System.Windows.Forms.Label
$idLabel.Location = New-Object System.Drawing.Point(22, 115)
$idLabel.Size = New-Object System.Drawing.Size(530, 22)
$idLabel.Text = 'Client ID (필수)'
$form.Controls.Add($idLabel)
$idBox = New-Object System.Windows.Forms.TextBox
$idBox.Location = New-Object System.Drawing.Point(22, 139)
$idBox.Size = New-Object System.Drawing.Size(536, 25)
$idBox.TabIndex = 0
$form.Controls.Add($idBox)

$secretLabel = New-Object System.Windows.Forms.Label
$secretLabel.Location = New-Object System.Drawing.Point(22, 176)
$secretLabel.Size = New-Object System.Drawing.Size(530, 22)
$secretLabel.Text = 'Client Secret (필수, 입력 내용은 가려집니다)'
$form.Controls.Add($secretLabel)
$secretBox = New-Object System.Windows.Forms.TextBox
$secretBox.Location = New-Object System.Drawing.Point(22, 200)
$secretBox.Size = New-Object System.Drawing.Size(536, 25)
$secretBox.UseSystemPasswordChar = $true
$secretBox.TabIndex = 1
$form.Controls.Add($secretBox)

$accountLabel = New-Object System.Windows.Forms.Label
$accountLabel.Location = New-Object System.Drawing.Point(22, 237)
$accountLabel.Size = New-Object System.Drawing.Size(536, 22)
$accountLabel.Text = '기본 accountSeq (선택, 계좌 조회로 확인한 계좌 식별값)'
$form.Controls.Add($accountLabel)
$accountBox = New-Object System.Windows.Forms.TextBox
$accountBox.Location = New-Object System.Drawing.Point(22, 261)
$accountBox.Size = New-Object System.Drawing.Size(536, 25)
$accountBox.TabIndex = 2
$form.Controls.Add($accountBox)

$tradingBox = New-Object System.Windows.Forms.CheckBox
$tradingBox.Location = New-Object System.Drawing.Point(22, 305)
$tradingBox.Size = New-Object System.Drawing.Size(536, 27)
$tradingBox.Text = '실제 주문 기능 사용 (기본값: 끔)'
$tradingBox.Checked = $false
$tradingBox.TabIndex = 3
$form.Controls.Add($tradingBox)
$tradingHelp = New-Object System.Windows.Forms.Label
$tradingHelp.Location = New-Object System.Drawing.Point(22, 335)
$tradingHelp.Size = New-Object System.Drawing.Size(536, 42)
$tradingHelp.Text = "켜면 일반 주문과 조건주문의 생성·정정·취소 6개 기능을 사용할 수 있습니다.`r`n이 설정을 저장하는 것만으로 주문이 실행되지는 않습니다."
$form.Controls.Add($tradingHelp)

$storageHelp = New-Object System.Windows.Forms.Label
$storageHelp.Location = New-Object System.Drawing.Point(22, 388)
$storageHelp.Size = New-Object System.Drawing.Size(536, 35)
$storageHelp.ForeColor = [System.Drawing.Color]::DimGray
$storageHelp.Text = '인증 정보는 이 Windows 사용자 계정의 DPAPI로 암호화하여 로컬에 저장합니다.'
$form.Controls.Add($storageHelp)

$saveButton = New-Object System.Windows.Forms.Button
$saveButton.Text = '저장'
$saveButton.Location = New-Object System.Drawing.Point(358, 438)
$saveButton.Size = New-Object System.Drawing.Size(95, 32)
$saveButton.TabIndex = 4
$form.Controls.Add($saveButton)
$cancelButton = New-Object System.Windows.Forms.Button
$cancelButton.Text = '취소'
$cancelButton.Location = New-Object System.Drawing.Point(463, 438)
$cancelButton.Size = New-Object System.Drawing.Size(95, 32)
$cancelButton.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$cancelButton.TabIndex = 5
$form.Controls.Add($cancelButton)
$form.AcceptButton = $saveButton
$form.CancelButton = $cancelButton

$saveButton.Add_Click({
    $clientId = $idBox.Text.Trim()
    $clientSecret = $secretBox.Text.Trim()
    $accountSeq = $accountBox.Text.Trim()
    if ([string]::IsNullOrWhiteSpace($clientId) -or [string]::IsNullOrWhiteSpace($clientSecret)) {
        [void][System.Windows.Forms.MessageBox]::Show($form, 'Client ID와 Client Secret을 입력하세요.', '필수 입력', [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Information)
        return
    }
    [long]$parsedAccount = 0
    if ($accountSeq.Length -gt 0 -and (-not [long]::TryParse($accountSeq, [ref]$parsedAccount) -or $parsedAccount -le 0 -or $accountSeq -notmatch '^\d+$')) {
        [void][System.Windows.Forms.MessageBox]::Show($form, 'accountSeq는 양의 정수로 입력하거나 비워 두세요.', '입력 확인', [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Information)
        return
    }
    if ($accountSeq.Length -eq 0) { $accountSeq = $null }
    $saveButton.Enabled = $false
    try {
        $aclRestricted = Save-ProtectedCredentials -ClientId $clientId -ClientSecret $clientSecret -AccountSeq $accountSeq -EnableTrading $tradingBox.Checked
        $message = '연결 설정을 암호화하여 저장했습니다.'
        if (-not $aclRestricted) { $message += "`r`nWindows 파일 권한 제한은 적용하지 못했지만 DPAPI 암호화는 완료되었습니다." }
        [void][System.Windows.Forms.MessageBox]::Show($form, $message, '저장 완료', [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Information)
        $secretBox.Clear()
        $form.DialogResult = [System.Windows.Forms.DialogResult]::OK
        $form.Close()
    } catch {
        [void][System.Windows.Forms.MessageBox]::Show($form, '설정을 저장하지 못했습니다. 이 폴더에 대한 쓰기 권한을 확인한 뒤 다시 시도하세요.', '저장 실패', [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error)
    } finally {
        $clientSecret = $null
        $saveButton.Enabled = $true
    }
})

try {
    $result = $form.ShowDialog()
} finally {
    $secretBox.Clear()
    $form.Dispose()
}
if ($result -eq [System.Windows.Forms.DialogResult]::OK) { exit 0 }
exit 1
