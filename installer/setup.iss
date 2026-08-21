; Minecraft 模组云端同步 - Inno Setup 安装程序
; 编译: ISCC.exe installer\setup.iss
; 编译前先运行: python build.py（生成 release\ 目录）

#define MyAppName "Minecraft 模组云端同步"
#define MyAppNameShort "MinecraftModsCloudSync"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "By CallMeACookieWYQ"
#define MyAppExeServer "MinecraftSyncServer.exe"
#define MyAppExeClient "MinecraftSyncClient.exe"
#define MyAppId "{{9E3A5C1D-4B7A-4A2E-9D6C-3F0E8B2A7C11}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppVerName={#MyAppName} {#MyAppVersion}
DefaultDirName={autopf}\{#MyAppNameShort}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=no
DisableWelcomePage=no
OutputDir=..\release
OutputBaseFilename=MinecraftModsCloudSync_Setup_{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeClient}
PrivilegesRequired=admin
; 安装包自身图标（生成图标后放置于 release\installer_assets\）
SetupIconFile=..\release\installer_assets\icon_server.ico

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Types]
Name: "full"; Description: "完整安装（客户端 + 服务端）"
Name: "custom"; Description: "自定义安装"; Flags: iscustom

[Components]
Name: "client"; Description: "客户端（模组自动更新同步）"; Types: full custom
Name: "server"; Description: "服务端工具（待办发布 + 文件同步）"; Types: full custom

[Tasks]
Name: "client_desktopicon"; Description: "为「客户端」创建桌面快捷方式"; Components: client; Flags: unchecked
Name: "server_desktopicon"; Description: "为「服务端工具」创建桌面快捷方式"; Components: server; Flags: unchecked
Name: "autostart"; Description: "开机自启动已安装的组件（最小化到托盘）"; Flags: unchecked

[Files]
; 服务端
Source: "..\release\MinecraftSyncServer\{#MyAppExeServer}"; DestDir: "{app}"; Components: server
; 客户端
Source: "..\release\MinecraftSyncClient\{#MyAppExeClient}"; DestDir: "{app}"; Components: client

[Icons]
; 开始菜单（始终创建）
Name: "{group}\服务端工具"; Filename: "{app}\{#MyAppExeServer}"; Components: server
Name: "{group}\客户端"; Filename: "{app}\{#MyAppExeClient}"; Components: client
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
; 桌面快捷方式（按勾选任务创建）
Name: "{autodesktop}\客户端 - {#MyAppName}"; Filename: "{app}\{#MyAppExeClient}"; Tasks: client_desktopicon
Name: "{autodesktop}\服务端工具 - {#MyAppName}"; Filename: "{app}\{#MyAppExeServer}"; Tasks: server_desktopicon

[Registry]
; 开机自启（最小化到托盘）
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#MyAppNameShort}Client"; ValueData: """{app}\{#MyAppExeClient}"" --tray"; Flags: uninsdeletevalue; Tasks: autostart; Components: client
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#MyAppNameShort}Server"; ValueData: """{app}\{#MyAppExeServer}"" --tray"; Flags: uninsdeletevalue; Tasks: autostart; Components: server

[Run]
Filename: "{app}\{#MyAppExeClient}"; Description: "立即运行客户端"; Flags: nowait postinstall skipifsilent; Components: client
Filename: "{app}\{#MyAppExeServer}"; Description: "立即运行服务端工具"; Flags: nowait postinstall skipifsilent; Components: server

[Code]
// 卸载时询问是否删除本机配置与日志（数据位于安装目录下的隐藏文件夹 .mc-sync-data）
procedure CurUninstallStepChanged(CurStep: TUninstallStep);
var
  DataRoot: String;
begin
  if CurStep = usUninstall then
  begin
    DataRoot := ExpandConstant('{app}\.mc-sync-data');
    if MsgBox('是否同时删除本机已保存的服务器配置？' + #13#10 +
              '（选择"是"将删除安装目录下 .mc-sync-data\config 中的全部配置）' + #13#10 +
              '选择"否"则保留，以便重新安装后继续使用。',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    begin
      if DirExists(DataRoot + '\config') then
        DelTree(DataRoot + '\config', True, True, True);
    end;
    if MsgBox('是否同时删除本机产生的日志文件？' + #13#10 +
              '（选择"是"将删除安装目录下 .mc-sync-data\logs 中的全部日志）',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    begin
      if DirExists(DataRoot + '\logs') then
        DelTree(DataRoot + '\logs', True, True, True);
    end;
  end;
end;
