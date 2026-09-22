; Stuff Downloader per-user installer (plan §10 M6). Build it with one command:
;
;   .venv\Scripts\python.exe packaging\build_installer.py installer
;
; which builds dist\payload\ and then runs ISCC with AppVersion, PayloadDir and OutputDir set.
; PERSONAL USE ONLY: the payload carries the spotDL environment's wheels. Do not sign, publish
; or share the setup .exe this produces.
;
; Program files go to %LOCALAPPDATA%\Programs\Stuff Downloader. The app's own data
; (%LOCALAPPDATA%\StuffDownloader: settings, runtime\ engine envs, logs) and the user's downloads
; are never listed here, so the uninstaller never removes them.

#ifndef AppVersion
  #error Pass /DAppVersion=<version> (build_installer.py installer does this)
#endif
#ifndef PayloadDir
  #error Pass /DPayloadDir=<dist\payload> (build_installer.py installer does this)
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

#define AppName "Stuff Downloader"
#define AppExe "StuffDownloader.exe"

[Setup]
AppId={{6B0F4C1E-5D2A-4E8B-9C47-3A1F8D2E7B55}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=StuffDownloader-Setup-{#AppVersion}
SetupIconFile=..\src\stuff_downloader\resources\app.ico
UninstallDisplayIcon={app}\{#AppExe}
LicenseFile={#PayloadDir}\licences\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#PayloadDir}\StuffDownloader\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PayloadDir}\runtime-setup\*"; DestDir: "{app}\runtime-setup"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PayloadDir}\licences\*"; DestDir: "{app}\licences"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PayloadDir}\PAYLOAD.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent; Check: RuntimeReady

[UninstallDelete]
; Only files the install itself created inside {app}: bytecode caches and the setup log.
Type: filesandordirs; Name: "{app}\runtime-setup"
Type: files; Name: "{app}\setup-runtime.log"

[Code]
var
  RuntimeOk: Boolean;

function RuntimeReady: Boolean;
begin
  Result := RuntimeOk;
end;

// Build the engine envs under %LOCALAPPDATA%\StuffDownloader\runtime, offline, from the staged
// hash-pinned wheels. Output goes to {app}\setup-runtime.log.
procedure SetupRuntime;
var
  Python, Script, Log, Params: String;
  ResultCode: Integer;
begin
  Python := ExpandConstant('{app}\runtime-setup\python\python.exe');
  Script := ExpandConstant('{app}\runtime-setup\packaging\build_installer.py');
  Log := ExpandConstant('{app}\setup-runtime.log');
  Params := '/C ""' + Python + '" "' + Script + '" setup-runtime > "' + Log + '" 2>&1"';
  WizardForm.StatusLabel.Caption := 'Setting up download engines (this takes a few minutes)...';
  RuntimeOk := Exec(ExpandConstant('{cmd}'), Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
    and (ResultCode = 0);
  if not RuntimeOk then
    SuppressibleMsgBox(
      'The program files were installed, but setting up the download engines failed'
      + ' (exit code ' + IntToStr(ResultCode) + ').' + #13#10#13#10
      + 'Details are in:' + #13#10 + Log + #13#10#13#10
      + 'Run Setup again to retry. Your settings and downloads were not changed.',
      mbError, MB_OK, IDOK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    SetupRuntime;
end;
