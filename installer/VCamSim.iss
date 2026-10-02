; Inno Setup script for VCamSim.
;
; Build with:  build_installer.bat   (or "ISCC.exe installer\VCamSim.iss")
;
; Produces installer\Output\VCamSim-Setup-<version>.exe, which:
;   * installs the GUI and Windows service (FFmpeg is installed separately)
;   * registers + starts the service (auto-start at boot)
;   * opens the Windows Firewall for inbound ONVIF/RTSP
;   * upgrades any previous version in place, keeping config.yaml
;
; AppId must never change: it is what makes an install an *upgrade* rather
; than a second copy.

#define AppName        "VCamSim"
#define AppVersion     "1.3.0"
#define AppPublisher   "VCamSim"
#define GuiExe         "VCamSim.exe"
#define SvcExe         "VCamSimSvc.exe"
#define SvcName        "VCamSim"

[Setup]
AppId={{7C4B2F1E-9D3A-4E6B-8F52-1A7C0D9E4B33}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
LicenseFile=..\LICENSE
VersionInfoVersion={#AppVersion}
; Deliberately NOT under Program Files: config.yaml, media_cache and
; control.token all live next to the exe, and the GUI must be able to write
; them without elevation.
DefaultDirName=C:\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
OutputDir=Output
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
; the service and the firewall rules both need it
PrivilegesRequired=admin
UninstallDisplayName={#AppName} {#AppVersion}
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startservice"; Description: "Start the VCamSim service now and at every boot"; GroupDescription: "Service:"
Name: "firewall"; Description: "Allow inbound ONVIF/RTSP through Windows Firewall"; GroupDescription: "Service:"

[Files]
Source: "..\dist\{#GuiExe}";          DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\{#SvcExe}";          DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\VCamSim-portable\licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\VCamSim-portable\dependencies.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\VCamSim-portable\VCamSim-source.zip"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\release.md"; DestDir: "{app}"; DestName: "RELEASE.md"; Flags: ignoreversion
Source: "..\config.example.yaml";     DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md";               DestDir: "{app}"; Flags: ignoreversion
; Deliberately NO initial config.yaml. Seeding it from config.example.yaml gave
; a fresh install one camera pointing at the sample path C:\videos\lobby.mp4,
; so the service came up with a camera stuck in "error" before the user had
; touched anything. With no config the app starts empty and says "press
; Generate", and the service simply idles until there is something to serve.
; An existing config.yaml from a previous version is never touched.

[Icons]
Name: "{group}\{#AppName}";            Filename: "{app}\{#GuiExe}"
Name: "{group}\Uninstall {#AppName}";  Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";      Filename: "{app}\{#GuiExe}"; Tasks: desktopicon

[Run]
; --- register the service (the previous one was removed in PrepareToInstall)
Filename: "{sys}\sc.exe"; Parameters: "create {#SvcName} binPath= ""{app}\{#SvcExe}"" start= demand DisplayName= ""VCamSim Virtual ONVIF Cameras"""; Flags: runhidden; StatusMsg: "Registering the VCamSim service..."
Filename: "{sys}\sc.exe"; Parameters: "config {#SvcName} start= auto"; Flags: runhidden; Tasks: startservice
Filename: "{sys}\sc.exe"; Parameters: "description {#SvcName} ""Serves the configured virtual ONVIF/RTSP cameras."""; Flags: runhidden
Filename: "{sys}\sc.exe"; Parameters: "failure {#SvcName} reset= 86400 actions= restart/5000/restart/10000//0"; Flags: runhidden

; --- firewall: one rule per binary, inbound TCP+UDP
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""VCamSim"" dir=in action=allow program=""{app}\{#GuiExe}"" protocol=TCP enable=yes profile=any"; Flags: runhidden; Tasks: firewall; StatusMsg: "Opening the Windows Firewall..."
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""VCamSim"" dir=in action=allow program=""{app}\{#GuiExe}"" protocol=UDP enable=yes profile=any"; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""VCamSim Service"" dir=in action=allow program=""{app}\{#SvcExe}"" protocol=TCP enable=yes profile=any"; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""VCamSim Service"" dir=in action=allow program=""{app}\{#SvcExe}"" protocol=UDP enable=yes profile=any"; Flags: runhidden; Tasks: firewall

; --- start it
Filename: "{sys}\sc.exe"; Parameters: "start {#SvcName}"; Flags: runhidden; Tasks: startservice; StatusMsg: "Starting the VCamSim service..."
Filename: "{app}\{#GuiExe}"; Description: "Open VCamSim now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\sc.exe";     Parameters: "stop {#SvcName}";   Flags: runhidden; RunOnceId: "StopSvc"
Filename: "{sys}\sc.exe";     Parameters: "delete {#SvcName}"; Flags: runhidden; RunOnceId: "DelSvc"
Filename: "{sys}\netsh.exe";  Parameters: "advfirewall firewall delete rule name=""VCamSim"""; Flags: runhidden; RunOnceId: "FwGui"
Filename: "{sys}\netsh.exe";  Parameters: "advfirewall firewall delete rule name=""VCamSim Service"""; Flags: runhidden; RunOnceId: "FwSvc"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\media_cache"
Type: files;          Name: "{app}\control.token"
Type: files;          Name: "{app}\service.log"
Type: files;          Name: "{app}\vcamsim-crash.log"

[Code]
{ ---------------------------------------------------------------------------
  An upgrade must not try to overwrite a running VCamSimSvc.exe, so the old
  service is stopped and de-registered before any file is copied.
  --------------------------------------------------------------------------- }
procedure StopAndRemoveService;
var
  rc: Integer;
begin
  Exec(ExpandConstant('{sys}\sc.exe'), 'stop {#SvcName}', '',
       SW_HIDE, ewWaitUntilTerminated, rc);
  { give the SCM a moment to actually release the binary }
  Sleep(2500);
  Exec(ExpandConstant('{sys}\sc.exe'), 'delete {#SvcName}', '',
       SW_HIDE, ewWaitUntilTerminated, rc);
  Sleep(1200);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopAndRemoveService;
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  StopAndRemoveService;
  Result := True;
end;
