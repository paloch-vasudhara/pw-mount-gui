; T6.2d: Windows installer of PW Mount Control (Inno Setup 7).
; Built by:  .venv\Scripts\python tools\package\build.py --installer
; which runs ISCC with  /DAppVersion=<0.1.0+hash> /DAppVersionNumeric=0.1.0.0
;                       /DSourceDir=<dist folder> /DOutputDir=<dist>
; Unsigned (SmartScreen shows "unknown publisher" on other PCs).

#ifndef AppVersion
  #define AppVersion "0.0.0+dev"
#endif
#ifndef AppVersionNumeric
  #define AppVersionNumeric "0.0.0.0"
#endif
#ifndef SourceDir
  #error SourceDir is not set: build with tools\package\build.py --installer
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif

[Setup]
; Fixed forever: upgrades and uninstall are keyed on it. The leading { is doubled for Inno.
AppId={{310D671C-09D3-4893-8539-DEDC985CEDFA}
AppName=PW Mount Control
AppVersion={#AppVersion}
AppPublisher=DELV
VersionInfoVersion={#AppVersionNumeric}
VersionInfoProductName=PW Mount Control
VersionInfoDescription=PW Mount Control Setup
DefaultDirName={autopf}\PW Mount Control
DefaultGroupName=PW Mount Control
DisableProgramGroupPage=yes
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
SetupIconFile={#SourceDir}\app.ico
UninstallDisplayIcon={app}\app.ico
UninstallDisplayName=PW Mount Control
OutputDir={#OutputDir}
OutputBaseFilename=PW-Mount-Control-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
; The running app (launcher mutex) is never killed: the gateway must stop the mount itself.
AppMutex=Local\PWMountControl
CloseApplications=no
RestartApplications=no
SetupMutex=PWMountControlSetup

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[InstallDelete]
; Stale files of an older version. Not {app}\relay: the user may have put mediamtx.exe there.
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\gateway"
Type: filesandordirs; Name: "{app}\analysis"
Type: filesandordirs; Name: "{app}\launcher"
Type: filesandordirs; Name: "{app}\pwi4"
Type: filesandordirs; Name: "{app}\web"
Type: filesandordirs; Name: "{app}\data"

[Icons]
Name: "{autoprograms}\PW Mount Control\PW Mount Control"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m launcher"; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"
Name: "{autoprograms}\PW Mount Control\Uninstall PW Mount Control"; Filename: "{uninstallexe}"
Name: "{autodesktop}\PW Mount Control"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m launcher"; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: "-m launcher"; WorkingDir: "{app}"; Description: "Launch PW Mount Control"; Flags: nowait postinstall skipifsilent runasoriginaluser

[Code]
const
  WebView2Key = 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  Pwi4Rel = '\PlaneWave Instruments\PlaneWave Interface 4\PWI4.exe';

var
  DsnPage: TInputQueryWizardPage;
  Dsn: String;           { held only from the page to ssPostInstall, then cleared }
  DsnHadValue: Boolean;  { for the summary: never the value itself }

{ ---------------------------------------------------------------- checks }

function HasPv(RootKey: Integer): Boolean;
var
  Pv: String;
begin
  Result := RegQueryStringValue(RootKey, WebView2Key, 'pv', Pv) and (Pv <> '') and (Pv <> '0.0.0.0');
end;

function WebView2Present: Boolean;
begin
  Result := HasPv(HKLM32) or HasPv(HKLM64) or HasPv(HKCU);
end;

function Pwi4Present: Boolean;
begin
  Result := FileExists(ExpandConstant('{commonpf32}') + Pwi4Rel) or FileExists(ExpandConstant('{commonpf64}') + Pwi4Rel);
end;

{ ---------------------------------------------------------------- DSN page }

function ValidDsn(const S: String): Boolean;
begin
  Result := (S = '')
    or (((Pos('postgresql://', S) = 1) or (Pos('postgresql+psycopg2://', S) = 1) or (Pos('postgres://', S) = 1))
        and (Pos(#13, S) = 0) and (Pos(#10, S) = 0));
end;

procedure InitializeWizard;
begin
  DsnPage := CreateInputQueryPage(wpSelectDir,
    'Satellite database connection (optional)',
    'The satellite catalogue needs a PostgreSQL login.',
    'Stored in Windows Credential Manager for this Windows user; leave empty to keep the current one (if any). ' +
    'It is never written to a file in the program folder, the registry or a log.');
  DsnPage.Add('Connection string (postgresql://...):', True);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  V: String;
begin
  Result := True;
  if CurPageID = DsnPage.ID then
  begin
    V := Trim(DsnPage.Values[0]);
    if not ValidDsn(V) then
    begin
      MsgBox('The connection string must start with postgresql://, postgresql+psycopg2:// or postgres:// ' +
        'and be on one line. Leave the field empty to keep the current setting.', mbError, MB_OK);
      Result := False;
    end
    else
    begin
      Dsn := V;
      DsnHadValue := V <> '';
    end;
  end;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo, MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
var
  S: String;
begin
  S := MemoDirInfo + NewLine;
  if MemoTasksInfo <> '' then
    S := S + NewLine + MemoTasksInfo + NewLine;
  S := S + NewLine + 'Satellite database login:' + NewLine;
  if DsnHadValue then
    S := S + Space + 'will be stored in Windows Credential Manager (this Windows user)' + NewLine
  else
    S := S + Space + 'not changed (the current one is kept, if any)' + NewLine;
  S := S + NewLine + 'System checks (nothing is installed for you):' + NewLine;
  if WebView2Present then
    S := S + Space + 'Microsoft Edge WebView2 Runtime: found' + NewLine
  else
    S := S + Space + 'Microsoft Edge WebView2 Runtime was not found: the app will open in your web browser instead.' + NewLine +
      Space + 'Install it from https://developer.microsoft.com/microsoft-edge/webview2/ for the app window.' + NewLine;
  if Pwi4Present then
    S := S + Space + 'PlaneWave Interface 4 (PWI4): found' + NewLine
  else
    S := S + Space + 'PlaneWave Interface 4 (PWI4) was not found. Install it from PlaneWave before connecting the mount;' + NewLine +
      Space + 'the rest of the app works without it.' + NewLine;
  Result := S;
end;

{ ---------------------------------------------------------------- storing the DSN }

procedure StoreDsn;
var
  F, App: String;
  Code: Integer;
  Ok: Boolean;
  N: Integer;
begin
  if Dsn = '' then
    Exit;
  F := ExpandConstant('{tmp}\dsn.in');   { Inno's private temp dir, deleted when setup ends }
  App := ExpandConstant('{app}');
  N := Length(Dsn);
  Ok := False;
  try
    if SaveStringToFile(F, Dsn, False) then
      { as the original user, so the login lands in the vault of the user who runs the app }
      Ok := ExecAsOriginalUser(App + '\python\python.exe', '-m analysis.set_dsn --from-file "' + F + '"',
        App, SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
  finally
    if FileExists(F) then
    begin
      SaveStringToFile(F, StringOfChar(' ', N), False);  { python wipes it too; belt and braces }
      DeleteFile(F);
    end;
    Dsn := '';
    DsnPage.Values[0] := '';
  end;
  if not Ok then
    SuppressibleMsgBox('The database login could not be stored. Setup continues; run' + #13#10 +
      '  "' + App + '\python\python.exe" -m analysis.set_dsn' + #13#10 +
      'later (in "' + App + '") to store it.', mbError, MB_OK, IDOK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    StoreDsn;
end;

{ ---------------------------------------------------------------- uninstall }

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  App: String;
  Code: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    { a silent uninstall (/VERYSILENT) keeps the user's data }
    if not UninstallSilent then
      if MsgBox('Also remove your settings, logs and stored logins (database, astrometry key, camera password, ADS-B token)?',
           mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      begin
        App := ExpandConstant('{app}');
        { plain Exec: Inno refuses ExecAsOriginalUser at uninstall time ("Cannot call ... during
          uninstall", user 11-17). The uninstaller runs as the user who started it (elevated with
          the same account under UAC), so the logins and %LOCALAPPDATA% are that user's. }
        if not (Exec(App + '\python\python.exe', '-m launcher.cleanup --all', App, SW_HIDE,
                  ewWaitUntilTerminated, Code) and (Code = 0)) then
          MsgBox('Your settings and stored logins could not be removed automatically. ' +
            'Delete the "pw-mount-control" entries in Windows Credential Manager and the folder ' +
            '%LOCALAPPDATA%\pw-mount-control by hand.', mbInformation, MB_OK);
      end;
  end;
end;
