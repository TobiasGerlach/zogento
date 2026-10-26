; Inno-Setup-Skript für den Windows-Installer der Bibliothek.
; Installiert pro Benutzer (keine Administratorrechte), legt einen Startmenü-Eintrag
; an und trägt den Programmordner in den Benutzer-PATH ein, damit `bib` im Terminal geht.
; Gebaut von packaging/build.ps1:  ISCC.exe /DMyAppVersion=<version> packaging\windows\bibliothek.iss

#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif

[Setup]
AppId={{6F2C1B8E-4E0B-4C4B-9C61-2B7B5D0E9A31}
AppName=Bibliothek
AppVersion={#MyAppVersion}
AppVerName=Bibliothek {#MyAppVersion}
AppPublisher=Tobias Gerlach
DefaultDirName={localappdata}\Programs\Bibliothek
DefaultGroupName=Bibliothek
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=Bibliothek-{#MyAppVersion}-windows-setup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\Bibliothek.exe
Compression=lzma2
SolidCompression=yes
LZMANumBlockThreads=4
ChangesEnvironment=yes
WizardStyle=modern

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\..\dist\Bibliothek\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Bibliothek"; Filename: "{app}\Bibliothek.exe"
Name: "{autodesktop}\Bibliothek"; Filename: "{app}\Bibliothek.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Check: NeedsAddPath(ExpandConstant('{app}'))

[Run]
Filename: "{app}\Bibliothek.exe"; Description: "{cm:LaunchProgram,Bibliothek}"; Flags: nowait postinstall skipifsilent

[Code]
function NeedsAddPath(Dir: string): Boolean;
var
  Paths: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Paths) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Paths) + ';') = 0;
end;

procedure RemovePath(Dir: string);
var
  Paths: string;
  P: Integer;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Paths) then
    exit;
  Paths := ';' + Paths + ';';
  P := Pos(';' + Uppercase(Dir) + ';', Uppercase(Paths));
  if P = 0 then
    exit;
  Delete(Paths, P, Length(Dir) + 1);
  { führendes und abschließendes Semikolon wieder entfernen }
  if (Length(Paths) > 0) and (Paths[1] = ';') then
    Delete(Paths, 1, 1);
  if (Length(Paths) > 0) and (Paths[Length(Paths)] = ';') then
    Delete(Paths, Length(Paths), 1);
  RegWriteExpandStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Paths);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemovePath(ExpandConstant('{app}'));
end;
