; Inno Setup script for SlowBooks Pro 2026 (Windows installer).
; Built in CI: iscc /DAppVersion=<x.y.z> SlowBooksPro.iss
; Paths are relative to this .iss file (packaging/windows).
;
; All user data (.env, company databases, uploads, backups, logs) lives in
; %LOCALAPPDATA%\SlowBooksPro — this installer never writes there, upgrades
; replace only {app}, and the uninstaller leaves the data intact.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{f67ccf1c-2072-4f4a-9102-4e3897758a97}
AppName=SlowBooks Pro 2026
AppVersion={#AppVersion}
; A modified build (LICENSE allows it); the copyright stays the original author's.
AppPublisher=easygoincajun
AppPublisherURL=https://github.com/easygoincajun/My-SlowBooks-Pro-2026
DefaultDirName={autopf}\SlowBooks Pro 2026
DefaultGroupName=SlowBooks Pro 2026
UninstallDisplayIcon={app}\SlowBooksPro.exe
OutputDir=.
OutputBaseFilename=SlowBooksPro-Setup-x64
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
WizardStyle=modern
; LICENSE section 13: the installer shows the license and requires acceptance.
LicenseFile=..\..\LICENSE
DisableProgramGroupPage=yes
; Ask running instances to close before upgrading. The uvicorn server child
; (--_serve) has no window, so [Code] below also stops it by name.
CloseApplications=yes

; An upgrade must not layer today's bundle over last month's. Inno Setup
; only overwrites files it ships; everything else under {app} survives, so
; every upgrade left the previous build's *.dist-info trees (and any module
; the new build dropped) in _internal\. importlib.metadata then reported
; whichever version it found first — cryptography 48.0.1 while the 50.0.1
; extension was the code running (2.9.0 gate, skytech). User data is never
; under {app} (see the header), so clearing it is safe.
[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "dist\SlowBooksPro\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs
; The license the wizard showed, kept beside the program.
Source: "..\..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"
; Microsoft's Evergreen WebView2 bootstrapper (~2 MB), downloaded by CI.
; Only executed when the runtime is missing (see [Run] Check) — Windows 11
; and most Windows 10 machines already have it.
Source: "MicrosoftEdgeWebView2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{group}\SlowBooks Pro 2026"; Filename: "{app}\SlowBooksPro.exe"
Name: "{group}\Uninstall SlowBooks Pro 2026"; Filename: "{uninstallexe}"
Name: "{autodesktop}\SlowBooks Pro 2026"; Filename: "{app}\SlowBooksPro.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Run]
Filename: "{tmp}\MicrosoftEdgeWebView2Setup.exe"; Parameters: "/silent /install"; \
  StatusMsg: "Installing the Microsoft WebView2 runtime (app window component)..."; \
  Check: not IsWebView2Installed; Flags: waituntilterminated
Filename: "{app}\SlowBooksPro.exe"; Description: "Launch SlowBooks Pro 2026"; Flags: nowait postinstall skipifsilent

[Code]
// WebView2 Evergreen runtime detection — same registry keys Microsoft
// documents (and desktop_launcher.py checks at runtime as the fallback).
function IsWebView2Installed(): Boolean;
var
  Version: String;
begin
  Result :=
    (RegQueryStringValue(HKLM,
      'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
      'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
    (RegQueryStringValue(HKLM,
      'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
      'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
    (RegQueryStringValue(HKCU,
      'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
      'pv', Version) and (Version <> '') and (Version <> '0.0.0.0'));
end;

// The server runs as a second, windowless SlowBooksPro.exe process
// (--_serve). CloseApplications can't reach it (no window, no message
// loop), so stop every SlowBooksPro.exe by name before install/uninstall
// — otherwise file-in-use errors block the upgrade.
procedure KillAppProcesses();
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM SlowBooksPro.exe /T',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  KillAppProcesses();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  KillAppProcesses();
  Result := True;
end;
