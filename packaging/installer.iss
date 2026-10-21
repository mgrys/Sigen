; Instalator Windows (Inno Setup 6). Budowanie: iscc /DAppVersion=1.0.0 packaging\installer.iss
; Wymaga wczesniej zbudowanego dist\SigenPVMonitor.exe (PyInstaller).
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "Sigen PV Monitor"
#define AppExe "SigenPVMonitor.exe"

[Setup]
AppId={{6B1E2C7A-5D3F-4E8B-9A21-3C7F0D9E5B41}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Sigen PV Monitor (projekt open source)
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=SigenPVMonitor-{#AppVersion}-setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "polish"; MessagesFile: "compiler:Languages\Polish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "Uruchamiaj automatycznie po zalogowaniu do Windows (zalecane – ciągły zapis danych)"; GroupDescription: "Dodatkowo:"
Name: "desktopicon"; Description: "Skrót na pulpicie"; GroupDescription: "Dodatkowo:"; Flags: unchecked

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Folder danych i konfiguracji"; Filename: "{localappdata}\SigenPVMonitor"
Name: "{group}\Odinstaluj {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--no-browser"; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; Description: "Uruchom {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM {#AppExe} /F"; Flags: runhidden; RunOnceId: "StopApp"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    if MsgBox('Usunąć także zapisane dane i konfigurację (folder ' + ExpandConstant('{localappdata}\SigenPVMonitor') + ')?',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{localappdata}\SigenPVMonitor'), True, True, True);
end;
