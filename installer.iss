; -*- coding: utf-8 -*-
; ============================================================
; 鲁岳企业服务·综合智能平台 — Windows 安装脚本 (Inno Setup)
; ============================================================
; 编译方法（任选其一）：
;   1. 安装 Inno Setup 6+，双击此文件运行
;   2. 命令行: "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
; ============================================================

#define MyAppName "鲁岳企业服务·综合智能平台"
#define MyAppShortName "LY重点群体涉税申报综合智能平台"
#define MyAppVersion "1.1.31"
#define MyAppPublisher "鲁岳企业服务"
#define MyAppURL "https://github.com/luyue-enterprise-platform/luyue-enterprise-platform"
#define MyAppExeName "鲁岳企业服务_综合智能平台.exe"

[Setup]
AppId={{B7F2C9E3-8A4D-4E1F-9C5B-6D2E8A3F1B7C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
; 默认安装到用户目录（无需管理员权限，避免 Program Files 写入权限问题）
DefaultDirName={localappdata}\Programs\{#MyAppShortName}
DefaultGroupName={#MyAppShortName}
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=output_installer
OutputBaseFilename=LY综合智能平台_安装程序_v{#MyAppVersion}
SetupIconFile=static\assets\logo.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
WizardSizePercent=110
WindowVisible=no
UsePreviousAppDir=yes
Uninstallable=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
VersionInfoVersion={#MyAppVersion}.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} 安装程序
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
MinVersion=10.0

[Languages]
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "其他快捷方式:"
Name: "startup"; Description: "加入开机启动（可选）"; GroupDescription: "其他快捷方式:"

[Files]
; 主程序 EXE
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
; 配置文件（云端认证）— 从项目根目录包含，确保所有电脑都有远程认证配置
Source: "auth_config.json"; DestDir: "{app}"; Flags: ignoreversion onlyifdoesntexist
; WebView2 Runtime 安装包（安装过程中自动安装，解决其他电脑无法启动问题）
Source: "MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: ignoreversion skipifsourcedoesntexist; Check: WebView2NotInstalled
; 版本信息
Source: "version.json"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
; 卸载程序图标（Inno Setup 自动生成）
Source: "static\assets\logo.ico"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
; 用户级安装：使用 {userdesktop}/{userstartup} 而非系统级路径
; 显式指定 IconFilename 确保桌面快捷方式显示正确的金色 LOGO
Name: "{userdesktop}\{#MyAppShortName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\logo.ico"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppShortName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\logo.ico"; Tasks: startup; Flags: runmaximized
Name: "{group}\{#MyAppShortName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\logo.ico"
Name: "{group}\卸载 {#MyAppShortName}"; Filename: "{uninstallexe}"

[Run]
; 安装 WebView2 Runtime（如果未安装）——静默安装，无需用户交互
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "正在安装 WebView2 运行时组件..."; Check: WebView2NotInstalled; Flags: waituntilterminated
; 安装完成后询问是否立即运行
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 卸载时可选清理用户数据
Filename: "{cmd}"; Parameters: "/C choice /M ""是否同时删除用户数据(数据/上传/输出/日志)？按 Y 确认，其他键跳过"" /N /T 10 /D N"; Flags: runhidden

[Code]
// 检查 WebView2 是否已安装
function WebView2NotInstalled(): Boolean;
var
  RegKey: String;
begin
  Result := True;
  RegKey := 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  if RegKeyExists(HKLM, RegKey) or RegKeyExists(HKCU, RegKey) then
    Result := False;
end;

// 安装前确认
function InitializeSetup(): Boolean;
begin
  Result := True;
end;

// 卸载前确认
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  UserDataDir: String;
begin
  if CurUninstallStep = usUninstall then begin
    UserDataDir := ExpandConstant('{app}\data');
    if DirExists(UserDataDir) then begin
      DelTree(UserDataDir, True, False, True);
    end;
    if FileExists(ExpandConstant('{app}\auth_config.json')) then
      DeleteFile(ExpandConstant('{app}\auth_config.json'));
  end;
end;
