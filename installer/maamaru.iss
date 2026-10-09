#define AppVersion GetEnv("MAAMARU_VERSION")
#define PackageDir GetEnv("MAAMARU_PACKAGE_DIR")

[Setup]
AppId={{7A640CC8-4574-4B80-9BAA-37DC1A2AD749}
AppName=まあ丸
AppVersion={#AppVersion}
AppPublisher=まあ丸
AppPublisherURL=https://github.com/qymd-NOT-official/maamaru-engine
AppSupportURL=https://github.com/qymd-NOT-official/maamaru-engine/issues
AppUpdatesURL=https://github.com/qymd-NOT-official/maamaru-engine/releases/latest
DefaultDirName={localappdata}\Programs\Maamaru
DefaultGroupName=まあ丸
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=maamaru-setup-v{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\launcher\assets\maamaru-launcher.ico
SetupLogging=yes
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\まあ丸启动器.exe
VersionInfoVersion={#AppVersion}
VersionInfoProductName=まあ丸
VersionInfoDescription=まあ丸安装器

[Languages]
Name: "chinesesimplified"; MessagesFile: ".\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Files]
Source: "{#PackageDir}\まあ丸启动器.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PackageDir}\manifest.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PackageDir}\update-manifest.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PackageDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\まあ丸"; Filename: "{app}\まあ丸启动器.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\まあ丸"; Filename: "{app}\まあ丸启动器.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\まあ丸启动器.exe"; Description: "启动まあ丸"; Flags: nowait postinstall skipifsilent

; 用户数据不属于安装目录，禁止用 [UninstallDelete] 无条件删除。
; 卸载器只会在用户明确选择“不再使用”并二次确认后，通过下面的安全检查清理。

[Code]
var
  DeleteUserData: Boolean;
  SelectedDataRoot: String;
  PreviousDataRoot: String;

function NormalizeDir(Value: String): String;
begin
  Result := RemoveBackslashUnlessRoot(ExpandFileName(Value));
end;

function IsSameOrParent(ParentPath: String; ChildPath: String): Boolean;
var
  ParentWithSlash: String;
  ChildWithSlash: String;
begin
  ParentWithSlash := AddBackslash(NormalizeDir(ParentPath));
  ChildWithSlash := AddBackslash(NormalizeDir(ChildPath));
  Result := Pos(Uppercase(ParentWithSlash), Uppercase(ChildWithSlash)) = 1;
end;

function IsSafeDataRoot(Value: String): Boolean;
var
  Candidate: String;
  DefaultRoot: String;
begin
  Candidate := NormalizeDir(Value);
  DefaultRoot := NormalizeDir(ExpandConstant('{localappdata}\Maamaru'));
  Result :=
    (Length(Candidate) > 3) and
    not IsSameOrParent(Candidate, ExpandConstant('{localappdata}')) and
    not IsSameOrParent(Candidate, ExpandConstant('{userprofile}')) and
    not IsSameOrParent(Candidate, ExpandConstant('{app}')) and
    not IsSameOrParent(ExpandConstant('{app}'), Candidate) and
    ((CompareText(Candidate, DefaultRoot) = 0) or
     FileExists(AddBackslash(Candidate) + '.maamaru-relocation.json'));
end;

procedure DeleteOneDataRoot(Value: String; var Failed: String);
var
  Candidate: String;
begin
  if Value = '' then
    Exit;
  Candidate := NormalizeDir(Value);
  if not DirExists(Candidate) then
    Exit;
  if not IsSafeDataRoot(Candidate) then
  begin
    Failed := Failed + #13#10 + '安全校验未通过：' + Candidate;
    Exit;
  end;
  if not DelTree(Candidate, True, True, True) then
    Failed := Failed + #13#10 + '删除失败：' + Candidate;
end;

function AskUninstallPurpose(): Integer;
var
  Form: TSetupForm;
  TitleLabel: TNewStaticText;
  KeepRadio: TNewRadioButton;
  KeepNote: TNewStaticText;
  DeleteRadio: TNewRadioButton;
  DeleteNote: TNewStaticText;
  ContinueButton: TNewButton;
  CancelButton: TNewButton;
begin
  Result := -1;
  Form := CreateCustomForm(ScaleX(520), ScaleY(250), False, True);
  try
    Form.Caption := '卸载まあ丸';

    TitleLabel := TNewStaticText.Create(Form);
    TitleLabel.Parent := Form;
    TitleLabel.Left := ScaleX(24);
    TitleLabel.Top := ScaleY(20);
    TitleLabel.Width := ScaleX(472);
    TitleLabel.Caption := '这次卸载是为了什么？';
    TitleLabel.Font.Style := [fsBold];

    KeepRadio := TNewRadioButton.Create(Form);
    KeepRadio.Parent := Form;
    KeepRadio.Left := ScaleX(24);
    KeepRadio.Top := ScaleY(56);
    KeepRadio.Width := ScaleX(472);
    KeepRadio.Caption := '换盘、重装或稍后再用';
    KeepRadio.Checked := True;

    KeepNote := TNewStaticText.Create(Form);
    KeepNote.Parent := Form;
    KeepNote.Left := ScaleX(48);
    KeepNote.Top := ScaleY(80);
    KeepNote.Width := ScaleX(448);
    KeepNote.Caption := '只删除程序，保留配置、日志、运行记录和备份。';
    KeepNote.Font.Color := clGray;

    DeleteRadio := TNewRadioButton.Create(Form);
    DeleteRadio.Parent := Form;
    DeleteRadio.Left := ScaleX(24);
    DeleteRadio.Top := ScaleY(116);
    DeleteRadio.Width := ScaleX(472);
    DeleteRadio.Caption := '不再使用まあ丸';

    DeleteNote := TNewStaticText.Create(Form);
    DeleteNote.Parent := Form;
    DeleteNote.Left := ScaleX(48);
    DeleteNote.Top := ScaleY(140);
    DeleteNote.Width := ScaleX(448);
    DeleteNote.Caption := '删除程序，并在二次确认后永久删除全部用户数据。';
    DeleteNote.Font.Color := clGray;

    ContinueButton := TNewButton.Create(Form);
    ContinueButton.Parent := Form;
    ContinueButton.Width := ScaleX(96);
    ContinueButton.Height := ScaleY(30);
    ContinueButton.Left := Form.ClientWidth - ScaleX(216);
    ContinueButton.Top := Form.ClientHeight - ScaleY(46);
    ContinueButton.Caption := '继续';
    ContinueButton.ModalResult := mrOk;

    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.Width := ScaleX(96);
    CancelButton.Height := ScaleY(30);
    CancelButton.Left := Form.ClientWidth - ScaleX(108);
    CancelButton.Top := Form.ClientHeight - ScaleY(46);
    CancelButton.Caption := '取消';
    CancelButton.ModalResult := mrCancel;

    if Form.ShowModal = mrOk then
    begin
      if DeleteRadio.Checked then
        Result := 1
      else
        Result := 0;
    end;
  finally
    Form.Free;
  end;
end;

function InitializeUninstall(): Boolean;
var
  Choice: Integer;
begin
  SelectedDataRoot := ExpandConstant('{localappdata}\Maamaru');
  PreviousDataRoot := '';
  RegQueryStringValue(HKCU, 'Software\Maamaru', 'DataRoot', SelectedDataRoot);
  RegQueryStringValue(HKCU, 'Software\Maamaru', 'PreviousDataRoot', PreviousDataRoot);

  { 静默卸载（CI 冒烟、批量运维）不弹窗：默认走“保留用户数据”的安全分支。
    自定义窗体不受 /VERYSILENT 抑制，无人值守环境弹窗会永久挂起。 }
  if UninstallSilent then
  begin
    DeleteUserData := False;
    Result := True;
    Exit;
  end;

  Choice := AskUninstallPurpose();
  Result := Choice >= 0;
  DeleteUserData := Choice = 1;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DefaultDataRoot: String;
  Paths: String;
  Failed: String;
begin
  if (CurUninstallStep <> usPostUninstall) or not DeleteUserData then
    Exit;

  DefaultDataRoot := ExpandConstant('{localappdata}\Maamaru');
  Paths := NormalizeDir(SelectedDataRoot);
  if CompareText(NormalizeDir(DefaultDataRoot), NormalizeDir(SelectedDataRoot)) <> 0 then
    Paths := Paths + #13#10 + NormalizeDir(DefaultDataRoot);
  if (PreviousDataRoot <> '') and
     (CompareText(NormalizeDir(PreviousDataRoot), NormalizeDir(SelectedDataRoot)) <> 0) and
     (CompareText(NormalizeDir(PreviousDataRoot), NormalizeDir(DefaultDataRoot)) <> 0) then
    Paths := Paths + #13#10 + NormalizeDir(PreviousDataRoot);

  if MsgBox(
       '下面的配置、日志、运行记录和备份将被永久删除，无法恢复：' + #13#10#13#10 +
       Paths + #13#10#13#10 + '确定继续吗？',
       mbConfirmation, MB_YESNO) <> IDYES then
    Exit;

  Failed := '';
  DeleteOneDataRoot(SelectedDataRoot, Failed);
  if CompareText(NormalizeDir(DefaultDataRoot), NormalizeDir(SelectedDataRoot)) <> 0 then
    DeleteOneDataRoot(DefaultDataRoot, Failed);
  if (PreviousDataRoot <> '') and
     (CompareText(NormalizeDir(PreviousDataRoot), NormalizeDir(SelectedDataRoot)) <> 0) and
     (CompareText(NormalizeDir(PreviousDataRoot), NormalizeDir(DefaultDataRoot)) <> 0) then
    DeleteOneDataRoot(PreviousDataRoot, Failed);

  if Failed <> '' then
    MsgBox('部分用户数据没有删除：' + Failed, mbError, MB_OK)
  else
  begin
    RegDeleteValue(HKCU, 'Software\Maamaru', 'DataRoot');
    RegDeleteValue(HKCU, 'Software\Maamaru', 'PreviousDataRoot');
    MsgBox('程序与用户数据均已删除。', mbInformation, MB_OK);
  end;
end;
