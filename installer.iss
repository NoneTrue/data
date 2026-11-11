#define AppName "ChangeVision 2026"
#define AppVersion "2026.11.10.1"
[Setup]
AppId={{6EEBD443-89F7-47EA-8FA7-537F68594C1C}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\ChangeVision2026_SourceBuild
DefaultGroupName=ChangeVision 2026 Source Build
UsePreviousAppDir=no
DisableDirPage=no
OutputDir=installer_output
OutputBaseFilename=ChangeVision2026_SourceBuild_Setup
SetupIconFile=runtime\icons\ChangeVision2026_HQ.ico
UninstallDisplayIcon={app}\runtime\icons\ChangeVision2026_HQ.ico
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
Compression=lzma2
SolidCompression=yes
[Files]
Source: "dist\ChangeVision2026\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Tasks]
Name: "desktopicon"; Description: "Create desktop shortcut"; Flags: unchecked
[Icons]
Name: "{group}\ChangeVision 2026 Source Build"; Filename: "{app}\ChangeVision2026.exe"; WorkingDir: "{app}"; IconFilename: "{app}\runtime\icons\ChangeVision2026_HQ.ico"
Name: "{autodesktop}\ChangeVision 2026 Source Build"; Filename: "{app}\ChangeVision2026.exe"; WorkingDir: "{app}"; IconFilename: "{app}\runtime\icons\ChangeVision2026_HQ.ico"; Tasks: desktopicon
[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\ChangeVision2026.exe')) then
    Result := 'Choose a new empty folder. Existing installations will not be replaced.';
end;
