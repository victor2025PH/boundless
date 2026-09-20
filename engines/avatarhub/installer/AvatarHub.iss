; ============================================================
;  无界幻境 BOUNDLESS Studio — Inno Setup 安装脚本（薄核心）
;  仅安装「控制台 exe + 编排脚本 + 前端 + manifest」（约几十 MB）；
;  绝不打包 conda 环境与模型（几十 GB）——首次启动由「首启向导」按档从下载站
;  拉取预打包(conda-pack)的自包含环境与模型，无需用户机安装 conda。
;  每用户安装到可写目录，免管理员，程序可在自身目录旁写 logs/config/runtime。
;  编译：temp\_compile_iss.cmd（调用 ISCC）。便携版见根目录 make_portable.py。
; ============================================================
;
; 品牌（2026-07-13 起）：无界科技 BOUNDLESS 全线品牌 —— 本产品为「幻境」系
; （幻颜 FaceX 换脸 / 幻声 VoiceX 克隆音 / 幻影 LiveX 直播分身）套件控制台。
; 品牌资产源：117:D:\workspace\brand-assets（母版库，一键重建），本仓 assets\brand\ 为拷贝。
;
; 双语（1.0.6 起）：按系统 UI 语言自动挑中文/英文（ShowLanguageDialog=no 不额外弹窗），
; 非中文系统一律走英文（en 排第一 = 兜底）。产品名按语言取短名：
;   中文桌面/开始菜单 = 「无界幻境」，英文 = 「BOUNDLESS Studio」——
; 长名「无界幻境 BOUNDLESS Studio」只留给卸载列表全称（AppNameFull）。

#define AppVersion "1.1.0"
#define AppURL "https://ai26.sbs"
#define SupportChannel "https://t.me/hykj7"
#define SupportGroup "https://t.me/hykjz"
#define AppExe "AvatarHub.exe"

[Setup]
AppId={{8F2A6C1E-4B7D-4E9A-9C3F-2A1B5D6E7F80}
AppName={cm:AppNameFull}
AppVersion={#AppVersion}
AppVerName={cm:AppNameFull} {#AppVersion}
AppPublisher={cm:PublisherName}
AppPublisherURL={#AppURL}
AppSupportURL={#SupportGroup}
AppUpdatesURL={#AppURL}
AppCopyright=© 2026 BOUNDLESS 无界科技
DefaultDirName={autopf}\AvatarHub
DefaultGroupName={cm:AppNameShort}
AllowNoIcons=yes
; 强制每用户安装（%LOCALAPPDATA%\Programs）：本产品把模型/日志/配置写在安装目录旁，
; 绝不能装进 Program Files（只读→首启即崩）。故不再提供"为所有用户安装"选项。
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=AvatarHub-Setup-{#AppVersion}
SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={cm:AppNameFull}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 品牌向导视觉：左竖幅直接取品牌库 story 竖版（深空点阵 + ∞ 主标 + 口号 + 七产品图标行），
; 右上小标 = ∞ 主标；各提供 100/150/200% 三档，Inno 按 DPI 自动挑选
WizardImageFile=wiz-large-100.bmp,wiz-large-150.bmp,wiz-large-200.bmp
WizardSmallImageFile=wiz-small-100.bmp,wiz-small-150.bmp,wiz-small-200.bmp
; 开启欢迎页（modern 默认关）——首屏即品牌竖幅 + 双语欢迎语，比直接跳协议页体面
DisableWelcomePage=no
; 向导整体放大 20%：产品矩阵大图页更舒展（Inno 会同步缩放横幅位图）
WizardSizePercent=120
DisableProgramGroupPage=yes
; 升级安装不沿用上一版的组名/语言（Inno 默认 UsePrevious*=yes 会把图标塞回
; 旧品牌组「数字人实时对话系统」——1.0.7 在 198 实锤）；品牌换代必须强制新值
UsePreviousGroup=no
UsePreviousLanguage=no
; 语言自动检测：不弹语言选择框，按系统 UI 语言挑；挑不中 → 列表第一个（en）
ShowLanguageDialog=no
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany=BOUNDLESS Technology
VersionInfoDescription=BOUNDLESS Studio Setup
VersionInfoProductName=BOUNDLESS Studio

[Languages]
; en 在前 = 非中文系统的兜底语言；InfoBefore 安装说明也按语言分文件
Name: "en"; MessagesFile: "compiler:Default.isl"; InfoBeforeFile: "install_notes_en.txt"
Name: "cn"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"; InfoBeforeFile: "install_notes.txt"

[Messages]
; 每页左下角的品牌落款
en.BeveledLabel=BOUNDLESS Technology · ai26.sbs
cn.BeveledLabel=无界科技 BOUNDLESS · ai26.sbs
; 欢迎页文案（[name] = AppNameFull）
en.WelcomeLabel1=Welcome to [name]
cn.WelcomeLabel1=欢迎安装 [name]
en.WelcomeLabel2=Digital-human live suite by BOUNDLESS Technology:%nFaceX face swap · VoiceX voice cloning · LiveX live avatar.%n%nThis wizard installs the console (tens of MB). AI runtime components are downloaded automatically on first launch, sized to your GPU.%n%nClick Next to continue.
cn.WelcomeLabel2=无界科技「幻境」系数字人直播工作室：%n幻颜换脸 · 幻声克隆音 · 幻影直播分身。%n%n本向导只安装控制台（几十 MB），AI 运行组件在首次启动时按你的显卡自动下载。%n%n点击「下一步」继续。

[CustomMessages]
; —— 产品命名（短名进桌面/开始菜单，全称进卸载列表/向导标题）——
en.AppNameShort=BOUNDLESS Studio
cn.AppNameShort=无界幻境
en.AppNameFull=BOUNDLESS Studio
cn.AppNameFull=无界幻境 BOUNDLESS Studio
en.PublisherName=BOUNDLESS Technology
cn.PublisherName=无界科技 BOUNDLESS
; —— 开始菜单附加入口 ——
en.SMSite=BOUNDLESS Website
cn.SMSite=官网 · 无界科技
en.SMSupport=Contact Support (Telegram)
cn.SMSupport=联系客服（Telegram 群）
en.RunConsole=Launch the console now
cn.RunConsole=立即启动控制台
; —— 产品矩阵页 ——
en.BrandPageTitle=Meet BOUNDLESS
cn.BrandPageTitle=认识无界科技 BOUNDLESS
en.BrandPageSub=Communication, Boundless. — 3 product series, 7 products; this suite is the Studio series
cn.BrandPageSub=让沟通，无界 —— 三大产品系 · 七款产品，本套件属于「幻境」系
en.BrandIntro=Growth (ReachX outreach · ChatX AI sales) | Studio (FaceX face swap · VoiceX voice clone · LiveX live avatar) | Lingo (LingoX chat translation · VoxX interpreting)
cn.BrandIntro=智连系（智拓获客 · 智聊AI成交）｜幻境系（幻颜换脸 · 幻声克隆音 · 幻影直播分身）｜通达系（通译翻译 · 通传同传）
en.ContactLabel=Website && support:
cn.ContactLabel=官网与客服：
en.GroupLinkCap=Telegram support group t.me/hykjz
cn.GroupLinkCap=Telegram 客服群 t.me/hykjz
; —— 安装完成弹窗（%n = 换行，代码里 CRLF() 展开）——
en.DoneOk=Setup complete.%nOn first launch the app detects your GPU and downloads the required runtime components once (about 11-35 GB depending on edition, with progress and ETA; later launches skip this).%nPlease keep the network up and leave enough disk space. The console opens automatically when ready.%n%nWebsite: {#AppURL}%nTelegram channel: {#SupportChannel}%nTelegram support group: {#SupportGroup}%n(Shortcuts to the website and support are also in the Start menu.)
cn.DoneOk=安装完成。%n首次启动会自动检测你的显卡，并一次性下载所需的运行组件%n（按版本约 11–35 GB，带进度和剩余时间提示，之后启动无需重复下载）。%n请保持网络畅通并预留足够磁盘空间，完成后自动进入控制台。%n%n官网：{#AppURL}%nTelegram 官方频道：{#SupportChannel}%nTelegram 客服交流群：{#SupportGroup}%n（开始菜单里也有「官网」与「联系客服」快捷入口）
en.DoneNoManifest=Setup finished, but the component manifest is missing, so runtime components cannot be downloaded automatically.%nPlease re-download the full installer, or contact support: {#AppURL}
cn.DoneNoManifest=安装完成，但缺少组件清单文件，程序将无法自动下载运行组件。%n请重新下载完整安装包，或联系官网客服：{#AppURL}

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; 控制台主程序（PyInstaller 打包的 exe）
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
; 编排与服务脚本（根目录 .py，不递归 —— 不含模型源码树/测试/构建/开发/厂商脚本）
Source: "..\*.py"; DestDir: "{app}"; Flags: ignoreversion; Excludes: "test_*.py,run_all_tests.py,build_packs.py,make_portable.py,make_release.py,pack_acceptance.py,pack_gui_acceptance.py,gate.py,make_manual.py,license_admin.py,license_server.py,_*.py"
; 启动 / 环境脚本（剔除机密与构建脚本；env_config/deploy.env 是【我们集群的 LAN 路由表】，
; 装到客户机会让服务探活指向 192.168.0.x 的生产机——1.0.3 在标机上出现过「假成功/假失败」，
; 单机客户只需 app_config 的 localhost 默认值，故一并剔除）
Source: "..\*.bat"; DestDir: "{app}"; Flags: ignoreversion; Excludes: "secrets.bat,build_launcher.bat,sign_artifacts.bat,gate.bat,env_config.bat,deploy.env.bat"
; 分发清单（首启向导据此按档下载环境/模型包）
Source: "..\dist\manifest.json"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
; 捆绑预热脸：faces 为空的新装机上，换脸引擎开机 cuDNN 预热与 Hub 容灾金丝雀都依赖它
; （缺失=预热跳过，用户首次换脸付 ~6s 冷启；2026-07-13 在 140 装机实锤）
Source: "..\_warmup_face.jpg"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
; 配置模板
Source: "..\config.example.json"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
; 运行期工具脚本（Hub 子进程依赖：dev_probe 设备枚举 / topology_lint / stability_report 等。
; 1.0.4 首发漏了整个 tools\ → 客户机「设备体检」永远报「未枚举到设备/未检测到 VB-Cable」
; （2026-07-13 在 198 实锤：dev_probe.py 缺失，rc=2 can't open file）。_*.py 是开发脚本不随包。）
Source: "..\tools\*.py"; DestDir: "{app}\tools"; Flags: ignoreversion; Excludes: "_*.py"
; 面向用户的成品不带 README/交付清单等开发者文档（太专业，客户不需要）
; 前端页面
; ui_src = 控制台拆分源目录（产物 ui.html 已随包），客户机不带源——与 build_packs.APP_TREE_EXCLUDES 同步
Source: "..\static\*"; DestDir: "{app}\static"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "\ui_src\*"
; 启动角色包（占位形象+音色）：全新装机角色库为空时，Hub 首启一次性播种，让同传/换脸开箱即有可用角色。
; 占位素材（edge-tts 合成人声 + AI 生成虚拟脸），正式商用发布前用已授权素材替换同名文件即可。约 1MB。
Source: "..\data\starter_profiles\*"; DestDir: "{app}\data\starter_profiles"; Flags: ignoreversion recursesubdirs createallsubdirs
; 各环境依赖基线（供目标机 provision）
Source: "..\requirements\*"; DestDir: "{app}\requirements"; Flags: ignoreversion recursesubdirs createallsubdirs
; 图标资源
Source: "..\assets\app.ico"; DestDir: "{app}\assets"; Flags: ignoreversion
; 品牌资源：控制台 Hero 真实 logo（launcher 按约定读 data\brand_logo.png）
Source: "..\data\brand_logo.png"; DestDir: "{app}\data"; Flags: ignoreversion skipifsourcedoesntexist
; 运行期品牌图标（能力卡产品图标 ∞ 托盘标；母版库 117:D:\workspace\brand-assets，
; 只带 KB 级小图——大 poster/公司横幅是安装器/物料专用，不进运行目录）
Source: "..\assets\brand\voicex-128.png"; DestDir: "{app}\assets\brand"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\assets\brand\chatx-128.png"; DestDir: "{app}\assets\brand"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\assets\brand\facex-128.png"; DestDir: "{app}\assets\brand"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\assets\brand\livex-128.png"; DestDir: "{app}\assets\brand"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\assets\brand\boundless-mark-256.png"; DestDir: "{app}\assets\brand"; Flags: ignoreversion skipifsourcedoesntexist
; 白标/品牌配置（名称 · 产品线 · 官网客服联系方式）：网页控制台与启动器共用单一真相。
; onlyifdoesntexist —— 客户已在「设置→白标」里改过的不覆盖
Source: "..\data\brand.json"; DestDir: "{app}\data"; Flags: onlyifdoesntexist skipifsourcedoesntexist
; 授权强制标记（1.0.8 商业化收口）：客户成品机默认强制授权（14 天全功能试用→到期软着陆）。
; 内部/开发机跑源码树无此文件 = 保持评估模式；排障可用 AVATARHUB_LICENSE_ENFORCE=0 覆盖
Source: "license_enforce.flag"; DestDir: "{app}"; Flags: ignoreversion
; 系列产品矩阵大图（安装向导「认识无界产品家族」页用，运行期不需要）
Source: "brand-poster.bmp"; Flags: dontcopy

[InstallDelete]
; 旧版本(≤1.0.3)装过的开发者文档与集群路由脚本，升级安装时一并清掉
Type: files; Name: "{app}\README.md"
Type: files; Name: "{app}\交付与验收清单.md"
Type: files; Name: "{app}\env_config.bat"
Type: files; Name: "{app}\deploy.env.bat"
; 历代品牌名的桌面/开始菜单快捷方式，升级时清掉避免多套图标并存：
;   ≤1.0.4 = 数字人实时对话系统；1.0.5 = 无界幻境 BOUNDLESS Studio（长名）
Type: files; Name: "{autodesktop}\数字人实时对话系统.lnk"
Type: filesandordirs; Name: "{autoprograms}\数字人实时对话系统"
Type: files; Name: "{autodesktop}\无界幻境 BOUNDLESS Studio.lnk"
Type: filesandordirs; Name: "{autoprograms}\无界幻境 BOUNDLESS Studio"

[Icons]
Name: "{group}\{cm:AppNameShort}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{cm:SMSite}"; Filename: "{#AppURL}"
Name: "{group}\{cm:SMSupport}"; Filename: "{#SupportGroup}"
Name: "{group}\{cm:UninstallProgram,{cm:AppNameShort}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{cm:AppNameShort}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:RunConsole}"; Flags: nowait postinstall skipifsilent

[Code]
var
  BrandPage: TWizardPage;

function ManifestFound(): Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\manifest.json'));
end;

{ 自定义消息里的 %n → 真换行（MsgBox 用；Inno 只对内置 message 自动换）}
function CRLF(const S: String): String;
begin
  Result := S;
  StringChangeEx(Result, '%n', #13#10, True);
end;

procedure OpenSiteClick(Sender: TObject);
var ec: Integer;
begin
  ShellExec('open', '{#AppURL}', '', '', SW_SHOWNORMAL, ewNoWait, ec);
end;

procedure OpenGroupClick(Sender: TObject);
var ec: Integer;
begin
  ShellExec('open', '{#SupportGroup}', '', '', SW_SHOWNORMAL, ewNoWait, ec);
end;

{ 「认识无界产品家族」页：系列产品矩阵大图 + 官网/客服直达。
  让用户在安装等待前就看到我们是三系七款的产品矩阵（品牌可信度），
  大图为 brand-assets 母版库的 matrix-poster（讲全线产品的标准物料）。 }
procedure InitializeWizard();
var
  Img: TBitmapImage;
  Intro, Contact: TNewStaticText;
  SiteLink, GroupLink: TNewStaticText;
  W, H: Integer;
begin
  BrandPage := CreateCustomPage(wpInfoBefore,
    ExpandConstant('{cm:BrandPageTitle}'),
    ExpandConstant('{cm:BrandPageSub}'));

  Intro := TNewStaticText.Create(BrandPage);
  Intro.Parent := BrandPage.Surface;
  Intro.Left := 0;
  Intro.Top := 0;
  Intro.Width := BrandPage.SurfaceWidth;
  Intro.AutoSize := False;
  Intro.WordWrap := True;
  Intro.Height := ScaleY(30);
  Intro.Caption := ExpandConstant('{cm:BrandIntro}');

  ExtractTemporaryFile('brand-poster.bmp');
  Img := TBitmapImage.Create(BrandPage);
  Img.Parent := BrandPage.Surface;
  Img.Bitmap.LoadFromFile(ExpandConstant('{tmp}\brand-poster.bmp'));
  W := BrandPage.SurfaceWidth;
  H := (W * 630) div 1120;
  if H > BrandPage.SurfaceHeight - ScaleY(30) - ScaleY(42) then
  begin
    H := BrandPage.SurfaceHeight - ScaleY(30) - ScaleY(42);
    W := (H * 1120) div 630;
  end;
  Img.Stretch := True;
  Img.Left := (BrandPage.SurfaceWidth - W) div 2;
  Img.Top := ScaleY(30);
  Img.Width := W;
  Img.Height := H;

  Contact := TNewStaticText.Create(BrandPage);
  Contact.Parent := BrandPage.Surface;
  Contact.Left := 0;
  Contact.Top := Img.Top + Img.Height + ScaleY(10);
  Contact.Caption := ExpandConstant('{cm:ContactLabel}');

  SiteLink := TNewStaticText.Create(BrandPage);
  SiteLink.Parent := BrandPage.Surface;
  SiteLink.Left := Contact.Left + Contact.Width + ScaleX(6);
  SiteLink.Top := Contact.Top;
  SiteLink.Caption := '{#AppURL}';
  SiteLink.Cursor := crHand;
  SiteLink.Font.Color := clBlue;
  SiteLink.Font.Style := [fsUnderline];
  SiteLink.OnClick := @OpenSiteClick;

  GroupLink := TNewStaticText.Create(BrandPage);
  GroupLink.Parent := BrandPage.Surface;
  GroupLink.Left := SiteLink.Left + SiteLink.Width + ScaleX(18);
  GroupLink.Top := Contact.Top;
  GroupLink.Caption := ExpandConstant('{cm:GroupLinkCap}');
  GroupLink.Cursor := crHand;
  GroupLink.Font.Color := clBlue;
  GroupLink.Font.Style := [fsUnderline];
  GroupLink.OnClick := @OpenGroupClick;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) then
  begin
    { 静默/无人值守安装不弹窗，避免自动化部署阻塞在 MsgBox 等待 OK }
    if WizardSilent() then
      Exit;
    if ManifestFound() then
      MsgBox(CRLF(ExpandConstant('{cm:DoneOk}')), mbInformation, MB_OK)
    else
      MsgBox(CRLF(ExpandConstant('{cm:DoneNoManifest}')), mbInformation, MB_OK);
  end;
end;
