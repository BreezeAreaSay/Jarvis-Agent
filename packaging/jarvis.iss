; Установщик Jarvis (Inno Setup 6): на одного пользователя, без прав администратора.
;
; Компиляция (из корня репозитория; так её делают .github/workflows/build.yml и scripts/build.ps1):
;   iscc /DVersion=1.0.0 /DSourceDir=<repo>\build\dist\Jarvis /DOutputDir=<repo>\build\installer
;        /DIconFile=<repo>\build\jarvis.ico packaging\jarvis.iss
; Параметры /D (полный набор):
;   Version   — версия ([project].version из pyproject.toml), ОБЯЗАТЕЛЬНО; идёт в имя Jarvis-Setup-<Version>.exe;
;   SourceDir — папка onedir PyInstaller (Jarvis.exe, jarvis-cli.exe, _internal\), по умолчанию ..\build\dist\Jarvis;
;   OutputDir — куда положить установщик, по умолчанию ..\build\installer;
;   IconFile  — иконка установщика (.ico из packaging\make_icon.py), по умолчанию ..\build\jarvis.ico.
; Относительные пути Inno Setup считает от папки этого файла (packaging\) — лучше передавать абсолютные.
; Файл — UTF-8 с BOM: без BOM старые ISCC читают кириллицу в кодовой странице ANSI.
;
; Что делает: ставит в {localappdata}\Programs\Jarvis, ярлык в «Пуск» пользователя, флажок «Запускать при входе
; в Windows» — ярлык Jarvis.lnk в папке «Автозагрузка» (то же, что делает `jarvis-cli autostart on`), деинсталлятор.
; Не HKCU\…\Run: антивирусы часто запрещают неподписанному установщику запись туда («RegCreateKeyEx: код 5»);
; старое значение Run из прошлых сборок убирается без сообщений об ошибке.
; Перед установкой и удалением закрывает Jarvis.exe и jarvis-cli.exe (taskkill без /T: дерево не трогаем —
; приложения, открытые через Jarvis, должны остаться; llama-server и codex завершит Job Object Jarvis).
; Каталог данных (JARVIS_DATA_DIR, по умолчанию %LOCALAPPDATA%\Jarvis) при удалении НЕ трогается.

#ifndef Version
  #error Не задана версия: iscc /DVersion=1.0.0 ... packaging\jarvis.iss
#endif
#ifndef SourceDir
  #define SourceDir "..\build\dist\Jarvis"
#endif
#ifndef OutputDir
  #define OutputDir "..\build\installer"
#endif
#ifndef IconFile
  #define IconFile "..\build\jarvis.ico"
#endif

#define AppName "Jarvis"
#define AppExe "Jarvis.exe"
#define CliExe "jarvis-cli.exe"
#define RunKey "Software\Microsoft\Windows\CurrentVersion\Run"

[Setup]
AppId={{6E0F3C2A-5B7D-4F1E-9A8C-2D4B6F8A1C3E}
AppName={#AppName}
AppVersion={#Version}
AppVerName={#AppName} {#Version}
AppPublisher={#AppName}
VersionInfoVersion={#Version}
VersionInfoProductName={#AppName}
VersionInfoDescription=Установщик Jarvis
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\Jarvis
DisableProgramGroupPage=yes
DisableDirPage=auto
UsePreviousAppDir=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=Jarvis-Setup-{#Version}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "autostart"; Description: "Запускать при входе в Windows"; GroupDescription: "Дополнительно:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\Jarvis"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Comment: "Jarvis — помощник"
; автозапуск — ярлык в «Автозагрузке» (как winapp.set_autostart); при удалении убирается вместе с программой
Name: "{userstartup}\Jarvis"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Comment: "Jarvis — помощник"; Tasks: autostart

[InstallDelete]
; флажок автозапуска снят при повторной установке — убрать ярлык прошлой установки или переключателя в трее
Type: files; Name: "{userstartup}\Jarvis.lnk"; Tasks: not autostart

[UninstallDelete]
; ярлык мог создать переключатель «Автозапуск» в трее — Inno о нём не знает
Type: files; Name: "{userstartup}\Jarvis.lnk"

[Registry]
; старый автозапуск через HKCU\…\Run (прошлые сборки) — убрать; запись туда может быть запрещена — noerror
Root: HKCU; Subkey: "{#RunKey}"; ValueType: none; ValueName: "Jarvis"; Flags: deletevalue uninsdeletevalue noerror dontcreatekey

[Run]
Filename: "{app}\{#AppExe}"; Description: "Запустить Jarvis"; WorkingDir: "{app}"; Flags: postinstall nowait skipifsilent

[UninstallRun]
; после подтверждения удаления, до удаления файлов
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExe} /IM {#CliExe}"; Flags: runhidden waituntilterminated; RunOnceId: "StopJarvis"

[Code]
{ Закрыть запущенные Jarvis.exe и jarvis-cli.exe (свои процессы пользователя), чтобы файлы не были заняты.
  Код 128 — какого-то из двух процессов не было (так бывает и когда второй найден и завершён), это нормально.
  Restart Manager (CloseApplications) — вторая страховка. }
procedure StopJarvis();
var
  ResultCode: Integer;
begin
  if Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe} /IM {#CliExe}', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode) then
    Log(Format('taskkill: код %d', [ResultCode]))
  else
    Log('taskkill не запустился');
  Sleep(500);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopJarvis();
  Result := '';
end;
