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
Name: "service"; Description: "Uruchamiaj w tle jako usługę Windows – start z systemem (także bez logowania), restart po awarii (wymaga zgody administratora)"; GroupDescription: "Praca ciągła:"; Flags: unchecked
Name: "autostart"; Description: "Uruchamiaj automatycznie po zalogowaniu do Windows (gdy bez usługi)"; GroupDescription: "Praca ciągła:"
Name: "desktopicon"; Description: "Skrót na pulpicie"; GroupDescription: "Dodatkowo:"; Flags: unchecked

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
; WinSW 2.12.0 (MIT, github.com/winsw/winsw) - pobierany przez workflow do dist\
Source: "..\dist\WinSW-x64.exe"; DestDir: "{app}"; DestName: "SigenPVMonitor-service.exe"; Flags: ignoreversion; Tasks: service

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Folder danych i konfiguracji"; Filename: "{localappdata}\SigenPVMonitor"
Name: "{group}\Odinstaluj {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--no-browser"; Tasks: autostart and not service

[Run]
; usluga: jedno okno zgody administratora - skrypt (zapisany w [Code]) usuwa stara usluge, instaluje i uruchamia nowa
Filename: "{app}\service-install.cmd"; Verb: "runas"; Flags: shellexec runhidden waituntilterminated; Tasks: service; StatusMsg: "Instalowanie usługi Windows..."
Filename: "{app}\{#AppExe}"; Description: "Uruchom {#AppName}"; Flags: nowait postinstall skipifsilent; Tasks: not service
Filename: "http://localhost:8000"; Description: "Otwórz stronę {#AppName}"; Flags: shellexec nowait postinstall skipifsilent; Tasks: service

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C cd /d ""{app}"" && SigenPVMonitor-service.exe stop & SigenPVMonitor-service.exe uninstall"; Verb: "runas"; Flags: shellexec runhidden waituntilterminated; RunOnceId: "RemoveService"; Tasks: service
Filename: "{cmd}"; Parameters: "/C taskkill /IM {#AppExe} /F"; Flags: runhidden; RunOnceId: "StopApp"

[UninstallDelete]
Type: files; Name: "{app}\SigenPVMonitor-service.xml"
Type: files; Name: "{app}\service-install.cmd"
Type: filesandordirs; Name: "{app}\service-logs"

[Code]
function ServiceExists(): Boolean;
var Code: Integer;
begin
  Result := Exec(ExpandConstant('{sys}\sc.exe'), 'query SigenPVMonitor', '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;

{ aktualizacja: zatrzymaj dzialajaca usluge, zanim pliki zostana podmienione }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var Code: Integer;
begin
  Result := '';
  if ServiceExists() then
  begin
    ShellExec('runas', ExpandConstant('{sys}\sc.exe'), 'stop SigenPVMonitor', '', SW_HIDE, ewWaitUntilTerminated, Code);
    Sleep(4000);
  end;
end;

{ konfiguracja WinSW (zapis przed sekcja [Run]): usluga na koncie systemowym, dane w folderze tego uzytkownika (%LOCALAPPDATA%\SigenPVMonitor) }
procedure CurStepChanged(CurStep: TSetupStep);
var X: String;
begin
  if (CurStep = ssInstall) and WizardIsTaskSelected('service') then
  begin
    ForceDirectories(ExpandConstant('{app}'));
    X := '<service>' + #13#10 +
      '  <id>SigenPVMonitor</id>' + #13#10 +
      '  <name>Sigen PV Monitor</name>' + #13#10 +
      '  <description>Sigen PV Monitor - odczyt falownika Sigenergy i strona http://localhost:' + '8000</description>' + #13#10 +
      '  <executable>' + ExpandConstant('{app}\{#AppExe}') + '</executable>' + #13#10 +
      '  <arguments>--no-browser</arguments>' + #13#10 +
      '  <workingdirectory>' + ExpandConstant('{app}') + '</workingdirectory>' + #13#10 +
      '  <env name="SIGEN_SERVICE" value="1"/>' + #13#10 +
      '  <env name="SIGEN_PV_DATA" value="' + ExpandConstant('{localappdata}\SigenPVMonitor') + '"/>' + #13#10 +
      '  <startmode>Automatic</startmode>' + #13#10 +
      '  <delayedAutoStart>true</delayedAutoStart>' + #13#10 +
      '  <onfailure action="restart" delay="10 sec"/>' + #13#10 +
      '  <onfailure action="restart" delay="30 sec"/>' + #13#10 +
      '  <onfailure action="restart" delay="2 min"/>' + #13#10 +
      '  <resetfailure>1 hour</resetfailure>' + #13#10 +
      '  <stoptimeout>20 sec</stoptimeout>' + #13#10 +
      '  <logpath>' + ExpandConstant('{app}\service-logs') + '</logpath>' + #13#10 +
      '  <log mode="roll-by-size"><sizeThreshold>5120</sizeThreshold><keepFiles>3</keepFiles></log>' + #13#10 +
      '</service>' + #13#10;
    ForceDirectories(ExpandConstant('{localappdata}\SigenPVMonitor'));
    SaveStringToFile(ExpandConstant('{app}\SigenPVMonitor-service.xml'), X, False);
    SaveStringToFile(ExpandConstant('{app}\service-install.cmd'),
      '@echo off' + #13#10 + 'cd /d "%~dp0"' + #13#10 +
      'taskkill /IM {#AppExe} /F >nul 2>&1' + #13#10 +
      'sc query SigenPVMonitor >nul 2>&1 && (SigenPVMonitor-service.exe stop & SigenPVMonitor-service.exe uninstall)' + #13#10 +
      'SigenPVMonitor-service.exe install' + #13#10 +
      'sc failureflag SigenPVMonitor 1' + #13#10 +
      'SigenPVMonitor-service.exe start' + #13#10, False);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    if MsgBox('Usunąć także zapisane dane i konfigurację (folder ' + ExpandConstant('{localappdata}\SigenPVMonitor') + ')?',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{localappdata}\SigenPVMonitor'), True, True, True);
end;
