; Downpour's setup program. Build it with tools/make_installer.py, which
; passes AppVersion, IconFile and, when the signed add-on is present,
; FirefoxXpi.
;
; A per-user install, like tools/install.ps1: no administrator rights. The
; browser helper's registration and Chrome's extension entry both live under
; HKEY_CURRENT_USER, so they belong to the same account as the program.

#ifndef AppVersion
  #error Build with tools/make_installer.py
#endif

#define ChromeStoreId "papnanikngmdnacmhecbigmbkbjfaboa"

[Setup]
AppId={{6F452B86-1BE1-493A-9B8A-6B528A881D2C}
AppName=Downpour
AppVersion={#AppVersion}
AppVerName=Downpour {#AppVersion}
AppPublisher=Downpour contributors
AppPublisherURL=https://github.com/matalvernaz/downpour
AppSupportURL=https://github.com/matalvernaz/downpour/issues
DefaultDirName={localappdata}\Programs\Downpour
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#SourcePath}..\dist
OutputBaseFilename=DownpourSetup-{#AppVersion}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\downpour.ico
UninstallDisplayName=Downpour
VersionInfoVersion={#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Downpour is closed in PrepareToInstall, the way its Exit command does it.
CloseApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Messages]
FinishedLabel=Setup has finished installing Downpour on your computer.%n%nIf you chose the Chrome extension, Chrome asks you to turn it on the next time it starts.

[Tasks]
Name: "autostart"; Description: "Start Downpour when Windows starts, in the notification area"
Name: "chrome"; Description: "Add the Downpour extension to Google Chrome from the Chrome Web Store"; Check: ChromeInstalled
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "{#SourcePath}..\dist\Downpour\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#IconFile}"; DestDir: "{app}"; DestName: "downpour.ico"; Flags: ignoreversion
#ifdef FirefoxXpi
Source: "{#FirefoxXpi}"; DestDir: "{app}\firefox"; DestName: "downpour-firefox.xpi"; Flags: ignoreversion
#endif

[Icons]
Name: "{autoprograms}\Downpour"; Filename: "{app}\Downpour.exe"; IconFilename: "{app}\downpour.ico"; Comment: "Downpour download manager"
Name: "{autodesktop}\Downpour"; Filename: "{app}\Downpour.exe"; IconFilename: "{app}\downpour.ico"; Tasks: desktopicon

[Registry]
; Chrome reads this at start-up and offers to turn on the store extension.
Root: HKCU; Subkey: "Software\Google\Chrome\Extensions\{#ChromeStoreId}"; ValueType: string; ValueName: "update_url"; ValueData: "https://clients2.google.com/service/update2/crx"; Flags: uninsdeletekey; Tasks: chrome

[Run]
Filename: "{app}\downpour-cli.exe"; Parameters: "helper install"; Flags: runhidden waituntilterminated; StatusMsg: "Connecting Downpour to your browsers..."
Filename: "{app}\downpour-cli.exe"; Parameters: "autostart on"; Flags: runhidden waituntilterminated; Tasks: autostart
Filename: "{app}\downpour-cli.exe"; Parameters: "autostart off"; Flags: runhidden waituntilterminated; Tasks: not autostart
Filename: "{code:FirefoxExe}"; Parameters: """{app}\firefox\downpour-firefox.xpi"""; Description: "Add the Downpour extension to Firefox (Firefox asks you to confirm)"; Flags: postinstall nowait skipifsilent; Check: FirefoxReady
Filename: "{app}\Downpour.exe"; Description: "Start Downpour"; Flags: postinstall nowait skipifsilent
; Downpour's own updater runs this setup with /SILENT /RELAUNCH.
Filename: "{app}\Downpour.exe"; Flags: nowait; Check: RelaunchRequested

[UninstallRun]
Filename: "{app}\downpour-cli.exe"; Parameters: "quit"; Flags: runhidden waituntilterminated; RunOnceId: "QuitDownpour"
Filename: "{app}\downpour-cli.exe"; Parameters: "helper remove"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveHelper"
Filename: "{app}\downpour-cli.exe"; Parameters: "autostart off"; Flags: runhidden waituntilterminated; RunOnceId: "AutostartOff"

[Code]
function RelaunchRequested: Boolean;
begin
  Result := Pos('/RELAUNCH', Uppercase(GetCmdTail)) > 0;
end;

function ChromeInstalled: Boolean;
begin
  Result := FileExists(ExpandConstant('{commonpf64}\Google\Chrome\Application\chrome.exe'))
    or FileExists(ExpandConstant('{commonpf32}\Google\Chrome\Application\chrome.exe'))
    or FileExists(ExpandConstant('{localappdata}\Google\Chrome\Application\chrome.exe'));
end;

function FirefoxExe(Param: String): String;
begin
  Result := ExpandConstant('{commonpf64}\Mozilla Firefox\firefox.exe');
  if not FileExists(Result) then
    Result := ExpandConstant('{commonpf32}\Mozilla Firefox\firefox.exe');
end;

function FirefoxReady: Boolean;
begin
  Result := FileExists(FirefoxExe('')) and FileExists(ExpandConstant('{app}\firefox\downpour-firefox.xpi'));
end;

{ Close a running Downpour before its files are replaced: first politely,
  as Exit does, so downloads are saved to resume; then whatever is left,
  such as a browser helper in the middle of a message. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
  Cli: String;
begin
  Result := '';
  Cli := ExpandConstant('{app}\downpour-cli.exe');
  if FileExists(Cli) then
    Exec(Cli, 'quit', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM Downpour.exe /F', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM downpour-cli.exe /F', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

{ Downloaded files are never touched. Downpour's own data stays unless the
  user says otherwise; a silent uninstall keeps it. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    { Folders a copy from tools/install.ps1 made before this setup took over
      are not the uninstaller's own; remove them too, but only while empty. }
    DelTree(ExpandConstant('{app}\_internal'), True, False, True);
    DelTree(ExpandConstant('{app}\extension'), True, False, True);
    DelTree(ExpandConstant('{app}\firefox'), True, False, True);
    RemoveDir(ExpandConstant('{app}'));
    Data := ExpandConstant('{localappdata}\Downpour');
    if DirExists(Data) and (SuppressibleMsgBox('Also delete your Downpour settings, download list and logs?' + #13#10#13#10 +
        'They are in ' + Data + '. Files you downloaded are kept either way.',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES) then
      DelTree(Data, True, True, True);
  end;
end;
