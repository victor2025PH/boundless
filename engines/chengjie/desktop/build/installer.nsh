; =============================================================================
; ChatX custom NSIS include -- wizard chrome + "data disposition" pages + wipe
; =============================================================================
; WHY THIS FILE EXISTS
;   The stock electron-builder uninstaller removes the PROGRAM only; every bit
;   of user data survives under %APPDATA% (that is the upgrade-self-heal
;   contract and stays the DEFAULT). This include adds an explicit, honest
;   choice at uninstall time:
;     [default] keep data  -> upgrade/reinstall continues where the user left
;     [opt-in ] wipe data  -> next install is factory fresh (databases, logins,
;                             kb/personas/albums/voices, settings, update cache)
;   Since 2026-09-05 it also owns the wizard's LOOK: brand sidebar/header
;   bitmaps (brand-assets/build_installer_art.py), DPI awareness, fonts, page
;   flow (welcome / notice / forced per-user mode / finish copy) and the token
;   colours used on the custom pages. See the "Brand / wizard chrome" block and
;   the "INSTALLER PAGE FLOW" note below; all of it is gate-pinned by
;   test/uninstall-nsh-invariants.test.js.
;
; CONTRACT (pinned by test/uninstall-nsh-invariants.test.js -- keep in sync):
;   C1  An UPDATE never wipes: the template already calls the old uninstaller
;       with /S /KEEP_APP_DATA, and we ALSO hard-guard on ${isUpdated} before
;       any RMDir here (belt + suspenders).
;   C2  Silent uninstall (/S) keeps data unless --delete-app-data is given
;       explicitly (ops channel; matches deploy/desktop/uninstall_chatx_node.ps1
;       semantics: program-only by default).
;   C3  Deletion targets are macro-derived (${APP_FILENAME}/${APP_PACKAGE_NAME});
;       NEVER a CJK literal path in code (PS5.1-GBK family of lessons).
;   C4  Before wiping we reap the whole process family BY PATH (backend +
;       sidecars run as the shell exe via ELECTRON_RUN_AS_NODE, so they never
;       match the main-exe check NSIS does) while EXCLUDING '*Uninstall*' --
;       under `_?=` the uninstaller runs in place and would otherwise kill
;       itself mid-wipe.
;   C5  After wiping we verify and report honestly (locked leftovers are named,
;       never silently ignored).
;   KEEP-LIST DOC: the "what is kept" copy below must stay in sync with
;   deploy/desktop/restore_chatx_accounts_node.ps1 (the authoritative
;   login/account artifact list) -- both sides carry a cross-reference note.
;
; ENCODING: file is UTF-8 WITH BOM (CJK LangStrings; makensis without BOM
; falls back to ACP=GBK and mangles them). Code/comments stay ASCII.
; =============================================================================

!include "nsDialogs.nsh"
!include "FileFunc.nsh"
!include "WinMessages.nsh"

; -----------------------------------------------------------------------------
; Brand / wizard chrome (2026-09-05). This include is processed BEFORE the
; template pulls in MUI2.nsh, so MUI_* interface settings defined here are
; honoured by every page (stock or custom).
;   ManifestDPIAware   seat standard is 4K@200%: without it the whole wizard is
;                      DWM bitmap-stretched (blurry text AND blurry artwork).
;                      The sidebar/header bitmaps are shipped at 2x for the
;                      same reason (brand-assets/build_installer_art.py; MUI
;                      FitControl scales them onto the DPI-scaled control).
;   MUI_(UN)ABORTWARNING  Cancel / X asks before quitting (was: instant exit,
;                      even with a data-disposition choice half made).
;   MUI_BGCOLOR/TEXTCOLOR  header band + welcome/finish pages: white + brand
;                      ink. Deliberately NOT a dark chrome: with visual styles
;                      on, SetCtlColors cannot recolour check/radio text (MUI2
;                      Finish.nsh, bug #443) so a dark finish page would render
;                      the "Run" checkbox black-on-dark. Deep space lives in
;                      the sidebar artwork instead.
;   CX_C_*             the ONLY colours SetCtlColors may use below (gate-pinned).
;                      HTML RRGGBB order -- MUI2's own MUI_FINISHPAGE_LINK_COLOR
;                      default 000080 (navy) proves the byte order; the old
;                      0x1F1FBF "red" here was actually a dark blue. Values are
;                      brand tokens chosen for >= 4.5:1 on the dialog face
;                      (#F0F0F0): gray-600 secondary text, growth-700 links,
;                      error-700 destructive warning. growth-500 #1E8CF2 is
;                      only 3.0:1 at 9pt, so it stays in the artwork.
; -----------------------------------------------------------------------------
ManifestDPIAware true
; Window titles. NSIS derives them from Name ("智聊"), which gives English users
; "智聊 Setup". The product name itself must stay CJK (registry keys, shortcuts,
; every stock MUI sentence), but the caption can follow the wizard language.
Caption "$(cxCaption)"
UninstallCaption "$(cxUnCaption)"
!define MUI_ABORTWARNING
!define MUI_UNABORTWARNING
!define MUI_BGCOLOR   "FFFFFF"
!define MUI_TEXTCOLOR "0B1020"
!define CX_C_TEXT2 "4B5563"
!define CX_C_LINK  "0B64B7"
!define CX_C_WARN  "B91C1C"

; -----------------------------------------------------------------------------
; Flavor badge. build/write-build-info.js (predist) writes build/flavor.nsh with
; `!define CX_FLAVOR "internal|clean|lite"`. Every non-clean wizard carries an
; amber "内测版 INTERNAL" pill on the sidebar + header bitmaps and a suffix on
; the branding line, so a human can tell an internal package from a clean one
; at a glance (they used to look identical). A missing flavor.nsh (bare
; `electron-builder` run without predist) counts as internal: the badge exists
; to stop internal packages masquerading as clean, so the failure mode must be
; "badge shown", never "badge missing". electron-builder passes the bitmap
; paths as -D defines before this include, hence the !undef/!define swap.
; -----------------------------------------------------------------------------
!include /NONFATAL "flavor.nsh"
!ifndef CX_FLAVOR
  !define CX_FLAVOR "internal"
!endif
!if "${CX_FLAVOR}" != "clean"
  !define CX_BADGE
  !ifdef MUI_WELCOMEFINISHPAGE_BITMAP
    !undef MUI_WELCOMEFINISHPAGE_BITMAP
  !endif
  !define MUI_WELCOMEFINISHPAGE_BITMAP "${BUILD_RESOURCES_DIR}\installerSidebar-internal.bmp"
  !ifdef MUI_UNWELCOMEFINISHPAGE_BITMAP
    !undef MUI_UNWELCOMEFINISHPAGE_BITMAP
  !endif
  !define MUI_UNWELCOMEFINISHPAGE_BITMAP "${BUILD_RESOURCES_DIR}\installerSidebar-internal.bmp"
  !ifdef MUI_HEADERIMAGE_BITMAP
    !undef MUI_HEADERIMAGE_BITMAP
  !endif
  !define MUI_HEADERIMAGE_BITMAP "${BUILD_RESOURCES_DIR}\installerHeader-internal.bmp"
  !define CX_BRAND_SUFFIX "  ·  $(cxFlavorInternal)"
!else
  !define CX_BRAND_SUFFIX ""
!endif

; Finish pages. MUI_FINISHPAGE_* settings are consumed by BOTH MUI_PAGE_FINISH
; (installer unit) and MUI_UNPAGE_FINISH (uninstaller unit) -- electron-builder
; inserts both after this include -- so branch on the compile unit, or the
; uninstaller would announce "ChatX is installed". Text is a LangString so it
; follows the wizard language at runtime.
; The finish text is a VARIABLE, not a fixed LangString: customInit/customUnInit
; seed it with the default copy and the wipe code paths overwrite it with the
; honest result ("all erased" / "these paths survived, scheduled for removal"),
; so the outcome is read on the last page instead of in a modal MessageBox
; popping over the wizard (2026-09-05 round 2). Silent channels never show a
; finish page, so their reporting stays DetailPrint + wipe log.
!ifndef BUILD_UNINSTALLER
  !define MUI_FINISHPAGE_TITLE "$(cxFinTitle)"
  !define MUI_FINISHPAGE_TEXT "$cxInsFinMsg"
  !define MUI_FINISHPAGE_TEXT_LARGE
  !define MUI_FINISHPAGE_RUN_TEXT "$(cxFinRun)"
  !define MUI_FINISHPAGE_LINK "$(cxFinLink)"
  !define MUI_FINISHPAGE_LINK_LOCATION "https://bd2026.cc/download/chatx"
!else
  !define MUI_FINISHPAGE_TITLE "$cxUnFinTitle"
  !define MUI_FINISHPAGE_TEXT "$cxUnFinMsg"
  ; leftovers were RMDir'd with /REBOOTOK, which raises the reboot flag and
  ; switches MUI to the reboot variant of the page: same message there, and
  ; "restart later" preselected (the HKCU RunOnce fallback fires at next logon
  ; anyway, so nobody needs to reboot on the spot).
  !define MUI_FINISHPAGE_TEXT_REBOOT "$cxUnFinMsg"
  !define MUI_FINISHPAGE_REBOOTLATER_DEFAULT
  !define MUI_FINISHPAGE_TEXT_LARGE
  !define MUI_FINISHPAGE_LINK "$(cxUnFinLink)"
  !define MUI_FINISHPAGE_LINK_LOCATION "https://bd2026.cc/download/chatx"
!endif
!define MUI_FINISHPAGE_LINK_COLOR "${CX_C_LINK}"

; Seed the finish-page variables (declared inside the page macros, see the -WX
; note there) before any page runs. Both hooks run in .onInit / un.onInit.
!macro customInit
  StrCpy $cxInsFinMsg "$(cxFinText)"
!macroend
!macro customUnInit
  StrCpy $cxUnFinTitle "$(cxUnFinTitle)"
  StrCpy $cxUnFinMsg "$(cxUnFinText)"
!macroend

; Branding line (bottom-left; stock "智聊 1.0.73"). The template issues
; BrandingText AFTER this include, so it cannot be overridden at compile time --
; rewrite the control at GUI init instead, with the company name in front (the
; parent brand is the trust anchor for an unsigned newcomer). The row is TWO
; controls that must carry the same text (measured with makensis probes,
; 2026-09-05): 1028 is the visible text; 1256 (MUI's $mui.Branding.Text) sits
; above the etched line 1035 and paints an opaque box exactly over its own text
; extent -- it is what hides the line behind the words. Writing only 1028 gives
; a strike-through (line drawn across the longer text); writing only 1256
; changes nothing visible.
; One function per compile unit: an unreferenced function is warning 6010 and
; -WX makes that fatal, so the installer unit must not see un.cxGuiInit and the
; uninstaller unit must not see cxGuiInit.
!macro cxSetBranding
  GetDlgItem $0 $HWNDPARENT 1028
  SendMessage $0 ${WM_SETTEXT} 0 "STR:$(cxBranding)${CX_BRAND_SUFFIX}"
  GetDlgItem $0 $HWNDPARENT 1256
  SendMessage $0 ${WM_SETTEXT} 0 "STR:$(cxBranding)${CX_BRAND_SUFFIX} "
!macroend
!ifndef BUILD_UNINSTALLER
  !define MUI_CUSTOMFUNCTION_GUIINIT cxGuiInit
  Function cxGuiInit
    !insertmacro cxSetBranding
  FunctionEnd
!else
  !define MUI_CUSTOMFUNCTION_UNGUIINIT un.cxGuiInit
  Function un.cxGuiInit
    !insertmacro cxSetBranding
  FunctionEnd
!endif

; Install-mode page ("who should this application be installed for?"): the
; product is per-user by design (perMachine:false, data under %APPDATA%, fleet
; tooling assumes %LOCALAPPDATA%\Programs) so the question is pure noise for
; users, and picking "all users" produces an install the ops scripts do not
; expect. Force per-user and skip the page -- EXCEPT when a legacy per-machine
; install is present, where the stock page must still let the user pick it
; (otherwise we would install a second copy next to it).
!macro customInstallMode
  ${If} $hasPerMachineInstallation != "1"
    StrCpy $isForceCurrentInstall "1"
  ${EndIf}
!macroend

; APP_PACKAGE_NAME is only defined by electron-builder when package.json name
; differs from productName; guard so the reap/delete lines below can always
; reference it (falls back to the product dir name).
!ifndef APP_PACKAGE_NAME
  !define APP_PACKAGE_NAME "${APP_FILENAME}"
!endif

; Which dir does the USER see as "your data"? The packaged userData dir is
; %APPDATA%\<productName> (CJK). electron-builder only sets APP_FILENAME to the
; product name when it is ASCII-safe (getWindowsInstallationDirName); for this
; product it falls back to the package name, so $APPDATA\${APP_FILENAME} is the
; DEV-mode dir that usually does not even exist on a user machine. Everything
; user-facing (path label, open-folder link) and the post-wipe verify must
; prefer APP_PRODUCT_FILENAME (the real CJK dir) whenever it is defined.
!ifdef APP_PRODUCT_FILENAME
  !define CX_USERDATA_DIR "$APPDATA\${APP_PRODUCT_FILENAME}"
!else
  !define CX_USERDATA_DIR "$APPDATA\${APP_FILENAME}"
!endif

; Field-measured packaged userData (117 dogfood + ops runbook both pin it):
; Electron resolves app.name from package.json "name" (= APP_PACKAGE_NAME), NOT
; the CJK productName, so logins/partitions/backend data actually live under
; %APPDATA%\<package-name>. The CJK product dir stays covered by wipe (belt),
; but any "does data exist?" check MUST also look at the package dir or the
; install-time data page would never trigger on real machines.
!ifdef APP_PACKAGE_NAME
  !define CX_DATA_DIR_PKG "$APPDATA\${APP_PACKAGE_NAME}"
!else
  !define CX_DATA_DIR_PKG "$APPDATA\${APP_FILENAME}"
!endif

; -----------------------------------------------------------------------------
; C6 (2026-08-24, impl64 P0-1 "upgrade storm" -- the INSTALLER half):
; reap the whole process family BEFORE the old version is uninstalled and files
; are replaced. The stock _CHECK_APP_RUNNING only handles
; ${APP_EXECUTABLE_FILENAME} (shell + ELECTRON_RUN_AS_NODE sidecars share that
; image name) -- the PyInstaller backend.exe is NOT covered. A surviving old
; backend keeps port 18799 + the pyrogram session files open; the freshly
; installed backend races it and Telegram force-revokes the shared auth key
; (AuthKeyDuplicated) => every TG account must QR-login again after every
; upgrade. The app-side half (backend-launcher stopAndWait) shipped in 1.0.52;
; this is the installer-side guarantee for manual-exe upgrades / hung shells.
;
; Defining customCheckAppRunning makes the template skip its own
; `!include getProcessInfo.nsh` + `Var pid` (allowOnlyOneInstallerInstance.nsh
; guards on !ifmacrondef), so we provide both here. $pid is referenced in BOTH
; compile units (installer + uninstaller insert CHECK_APP_RUNNING), so the
; top-level Var is safe under -WX (unlike the page Vars, see note above).
; -----------------------------------------------------------------------------
!include "getProcessInfo.nsh"
Var pid

!macro cxReapFamily
  ; Kill by PATH under the package dir. Backslash-bounded pattern so the
  ; sibling `<package>-updater` cache dir (which contains the RUNNING
  ; auto-update installer!) never matches; '*Uninstall*' excluded because the
  ; uninstaller runs in place under `_?=` (C4 lesson, would kill itself).
  nsExec::Exec `powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Process | Where-Object { $$_.Path -like '*\${APP_PACKAGE_NAME}\*' -and $$_.Path -notlike '*Uninstall*' -and $$_.Path -notlike '*-updater*' } | Stop-Process -Force -ErrorAction SilentlyContinue"`
!macroend

!macro customCheckAppRunning
  !insertmacro _CHECK_APP_RUNNING
  DetailPrint "$(cxStReap)"
  !insertmacro cxReapFamily
  Sleep 1200
  !insertmacro cxReapFamily
  Sleep 300
!macroend

; NOTE: our Var declarations live INSIDE customUnWelcomePage (below), not at
; file top level. The include is compiled into BOTH units (installer and
; uninstaller) but the macros only expand in the uninstaller one -- top-level
; Vars would be "not referenced" in the installer unit and makensis -WX turns
; that warning into a hard build failure (bitten on 2026-08-21, first build).

; -----------------------------------------------------------------------------
; LangStrings live in customHeader: our include is prepended BEFORE the language
; tables are loaded, but customHeader is inserted right AFTER addLangs -- the
; only hook where LangString compiles for both installer and uninstaller.
; -----------------------------------------------------------------------------
!macro customHeader
  ; Dialog font. SimpChinese.nlf hard-codes SimSun ("宋体") 9pt -- a 2010-vintage
  ; choice that makes every zh-CN NSIS wizard look like Windows XP -- while the
  ; RichEdit notice page and the app itself already use Microsoft YaHei UI.
  ; This macro is the only hook that runs AFTER MUI_LANGUAGE loaded the NLF (an
  ; earlier SetFont would be overwritten by the NLF font). The SAME face is set
  ; for English on purpose: dialog units scale with the font, and MUI stretches
  ; the sidebar/header bitmaps to the DU-sized controls -- one font for both
  ; languages means one fixed control aspect, so the artwork is rendered at
  ; exactly that aspect (191x410 / 175x74 px @96dpi, see build_installer_art.py)
  ; and is never squeezed. YaHei UI's Latin glyphs are Segoe UI shapes anyway.
  ; Ships with every Windows 8+ edition; GDI falls back gracefully if missing.
  SetFont /LANG=${LANG_SIMPCHINESE} "Microsoft YaHei UI" 9
  SetFont /LANG=${LANG_ENGLISH} "Microsoft YaHei UI" 9

  LangString cxUnDataTitle   ${LANG_ENGLISH} "Keep or erase your data?"
  LangString cxUnDataSub     ${LANG_ENGLISH} "Choose what happens to the data stored on this computer"
  LangString cxUnIntro       ${LANG_ENGLISH} "The program will be uninstalled. Your local data:"
  LangString cxKeepRadio     ${LANG_ENGLISH} "Keep my data (recommended)"
  LangString cxKeepDetail    ${LANG_ENGLISH} "Kept across upgrades and reinstalls: all account logins, chat history, customer profiles, knowledge base, personas, albums, cloned voices, settings."
  LangString cxKeepPath      ${LANG_ENGLISH} "Location: "
  LangString cxOpenDir       ${LANG_ENGLISH} "Open data folder"
  LangString cxWipeRadio     ${LANG_ENGLISH} "Erase ALL data permanently"
  LangString cxWipeWarn      ${LANG_ENGLISH} "Warning: all data above plus the update cache is deleted for good; accounts must log in again and the next install starts fresh. Diagnostic logs are erased too, so issues you reported can no longer be traced on this machine."
  LangString cxWipeConfirm   ${LANG_ENGLISH} "I understand this cannot be undone"
  LangString cxHelpLink      ${LANG_ENGLISH} "Need help or a fresh download? Visit bd2026.cc"
  LangString cxBtnKeep       ${LANG_ENGLISH} "Uninstall"
  LangString cxBtnWipe       ${LANG_ENGLISH} "Erase all"
  LangString cxNeedConfirm   ${LANG_ENGLISH} "Please tick $\"I understand this cannot be undone$\" first, or switch back to $\"Keep my data$\"."
  LangString cxWipeDone      ${LANG_ENGLISH} "All local data has been erased. The next install will start completely fresh."
  LangString cxWipeLeftover  ${LANG_ENGLISH} "Some files are currently in use and have been scheduled for automatic removal on the next reboot:"

  LangString cxUnDataTitle   ${LANG_SIMPCHINESE} "保留还是删除您的数据？"
  LangString cxUnDataSub     ${LANG_SIMPCHINESE} "请选择如何处理保存在本机的数据"
  LangString cxUnIntro       ${LANG_SIMPCHINESE} "即将卸载智聊程序。本机保存的数据："
  LangString cxKeepRadio     ${LANG_SIMPCHINESE} "保留我的数据（推荐）"
  LangString cxKeepDetail    ${LANG_SIMPCHINESE} "升级或重新安装后可继续使用：各平台账号登录状态、聊天记录与客户资料、知识库 / 人设 / 相册 / 克隆语音、软件设置。"
  LangString cxKeepPath      ${LANG_SIMPCHINESE} "数据位置："
  LangString cxOpenDir       ${LANG_SIMPCHINESE} "打开数据文件夹"
  LangString cxWipeRadio     ${LANG_SIMPCHINESE} "彻底删除所有数据"
  LangString cxWipeWarn      ${LANG_SIMPCHINESE} "注意：以上全部数据与更新缓存将被永久删除，无法恢复；所有账号需重新登录，下次安装将是全新初始状态。历史故障日志也将一并清除——已报障问题将无法再从本机取证排查。"
  LangString cxWipeConfirm   ${LANG_SIMPCHINESE} "我已了解此操作不可恢复"
  LangString cxHelpLink      ${LANG_SIMPCHINESE} "遇到问题或想重新下载？访问 bd2026.cc"
  LangString cxBtnKeep       ${LANG_SIMPCHINESE} "卸载"
  LangString cxBtnWipe       ${LANG_SIMPCHINESE} "删除并卸载"
  LangString cxNeedConfirm   ${LANG_SIMPCHINESE} "请先勾选「我已了解此操作不可恢复」，或改选「保留我的数据」。"
  LangString cxWipeDone      ${LANG_SIMPCHINESE} "所有本地数据已彻底清除。下次安装将是全新初始状态。"
  LangString cxWipeLeftover  ${LANG_SIMPCHINESE} "部分文件当前被占用，已安排在下次重启电脑时自动清除："

  ; ---- install-time data disposition page (C7) ------------------------------
  LangString cxInsDataTitle  ${LANG_ENGLISH} "Existing data found"
  LangString cxInsDataSub    ${LANG_ENGLISH} "This computer already has app data - choose how to continue"
  LangString cxInsIntro      ${LANG_ENGLISH} "Data from a previous installation was found:"
  LangString cxInsKeepRadio  ${LANG_ENGLISH} "Keep my data and continue (recommended)"
  LangString cxInsWipeRadio  ${LANG_ENGLISH} "Start fresh: erase ALL existing data before installing"
  LangString cxInsWipeWarn   ${LANG_ENGLISH} "Warning: everything above plus the update cache is erased for good before installing. All accounts must log in again."

  LangString cxInsDataTitle  ${LANG_SIMPCHINESE} "检测到本机已有数据"
  LangString cxInsDataSub    ${LANG_SIMPCHINESE} "本机保留着之前安装的数据，请选择如何继续"
  LangString cxInsIntro      ${LANG_SIMPCHINESE} "检测到之前安装留下的数据："
  LangString cxInsKeepRadio  ${LANG_SIMPCHINESE} "保留我的数据，继续安装（推荐）"
  LangString cxInsWipeRadio  ${LANG_SIMPCHINESE} "全新开始：先彻底清空旧数据再安装"
  LangString cxInsWipeWarn   ${LANG_SIMPCHINESE} "注意：以上全部数据与更新缓存将在安装前被永久删除，无法恢复；所有账号需重新登录。"

  ; ---- wizard chrome copy (2026-09-05): welcome / notice / directory / finish /
  ;      uninstall welcome+finish / progress status lines ------------------------
  LangString cxWelTitle      ${LANG_ENGLISH} "Welcome to ChatX Setup"
  LangString cxWelText       ${LANG_ENGLISH} "ChatX is the multi-platform AI chat and sales desktop app by BOUNDLESS Technology.$\r$\n$\r$\n-  About 1-3 minutes, roughly 1.3 GB of disk space$\r$\n-  Installs into your user profile, no admin rights$\r$\n-  Ready right away: QR login, no API key needed$\r$\n-  Your data stays on this computer; upgrades keep it$\r$\n$\r$\nClick Next to continue."
  LangString cxNoticeTitle   ${LANG_ENGLISH} "Before you install"
  LangString cxNoticeSub     ${LANG_ENGLISH} "Official download, your data and privacy - one minute"
  LangString cxNoticeTop     ${LANG_ENGLISH} "By continuing you agree to the Terms of Service and the Privacy Policy (bd2026.cc/terms  ·  bd2026.cc/privacy)."
  LangString cxNoticeBottom  ${LANG_ENGLISH} "That is everything you need to know. Click I Understand to continue."
  LangString cxNoticeBtn     ${LANG_ENGLISH} "&I Understand"
  LangString cxDirTop        ${LANG_ENGLISH} "ChatX will be installed into the folder below (your user profile by default; no administrator rights needed). Click Browse to choose another folder, then click Install."
  LangString cxFinTitle      ${LANG_ENGLISH} "ChatX is installed"
  LangString cxFinText       ${LANG_ENGLISH} "You can launch ChatX now.$\r$\n$\r$\nFirst run: follow the setup guide, add an account and log in by QR code. No API key is needed - AI runs through our secure gateway.$\r$\n$\r$\nGuides and support: bd2026.cc"
  LangString cxFinRun        ${LANG_ENGLISH} "&Launch ChatX now"
  LangString cxFinLink       ${LANG_ENGLISH} "Guides and support: bd2026.cc/download/chatx"
  LangString cxUnWelTitle    ${LANG_ENGLISH} "Uninstall ChatX"
  LangString cxUnWelText     ${LANG_ENGLISH} "This wizard removes the ChatX program from this computer.$\r$\n$\r$\n  -  If ChatX is still running you will be asked to close it$\r$\n  -  On the next page you choose whether to keep or erase your local data - it is KEPT by default, so a reinstall continues where you left off$\r$\n$\r$\nClick Next to continue."
  LangString cxUnFinTitle    ${LANG_ENGLISH} "ChatX has been uninstalled"
  LangString cxUnFinText     ${LANG_ENGLISH} "The program has been removed from this computer.$\r$\n$\r$\nIf you chose to keep your data, account logins, chat history and settings come back automatically after a reinstall.$\r$\n$\r$\nQuestions or a fresh download: bd2026.cc"
  LangString cxUnFinLink     ${LANG_ENGLISH} "Download again or contact support: bd2026.cc"
  LangString cxStInstalling  ${LANG_ENGLISH} "Installing ChatX components (about 1.3 GB) - this usually takes 1-3 minutes, please keep this window open..."
  LangString cxStReap        ${LANG_ENGLISH} "Stopping leftover ChatX background services..."
  LangString cxStWipe        ${LANG_ENGLISH} "Erasing user data..."
  LangString cxStWipeLeft    ${LANG_ENGLISH} "Some data is still in use and has been scheduled for removal:"
  LangString cxStWipeDone    ${LANG_ENGLISH} "User data erased."
  LangString cxBranding      ${LANG_ENGLISH} "BOUNDLESS Technology  ·  ChatX ${VERSION}"
  LangString cxCaption       ${LANG_ENGLISH} "ChatX Setup"
  LangString cxUnCaption     ${LANG_ENGLISH} "ChatX Uninstall"
  LangString cxFlavorInternal ${LANG_ENGLISH} "INTERNAL BUILD"
  ; finish-page variants for the wipe outcome (round 2: result on the page, not in a modal)
  LangString cxUnFinTitleWiped ${LANG_ENGLISH} "ChatX and all local data removed"
  LangString cxUnFinWipedTail  ${LANG_ENGLISH} "Fresh download or support: bd2026.cc"
  LangString cxUnFinTitleLeft  ${LANG_ENGLISH} "ChatX removed - some data pending"
  LangString cxUnFinLeftTail   ${LANG_ENGLISH} "They are removed automatically at your next sign-in (or restart). Nothing else to do."
  LangString cxInsFinWiped     ${LANG_ENGLISH} "Previous data was erased - this is a completely fresh start.$\r$\n$\r$\nFirst run: follow the setup guide, add an account and log in by QR code. No API key is needed."
  LangString cxInsFinLeft      ${LANG_ENGLISH} "Some previous data was still in use and is removed automatically at your next sign-in; this install works normally.$\r$\n$\r$\nFirst run: follow the setup guide, add an account and log in by QR code. No API key is needed."

  LangString cxWelTitle      ${LANG_SIMPCHINESE} "欢迎安装 智聊 ChatX"
  LangString cxWelText       ${LANG_SIMPCHINESE} "智聊是无界科技出品的多平台 AI 聊天与成交桌面端。$\r$\n$\r$\n·  安装约需 1–3 分钟，占用约 1.3 GB 磁盘空间$\r$\n·  安装到当前用户目录，不需要管理员权限$\r$\n·  装好即用：添加账号扫码登录，无需 API Key$\r$\n·  数据只保存在本机，升级不会丢失$\r$\n$\r$\n单击「下一步」继续。"
  LangString cxNoticeTitle   ${LANG_SIMPCHINESE} "安装前须知"
  LangString cxNoticeSub     ${LANG_SIMPCHINESE} "一分钟了解官方下载、数据与隐私"
  LangString cxNoticeTop     ${LANG_SIMPCHINESE} "继续安装即表示您已阅读并同意《服务条款》与《隐私政策》：bd2026.cc/terms  ·  bd2026.cc/privacy"
  LangString cxNoticeBottom  ${LANG_SIMPCHINESE} "以上就是安装前需要了解的全部内容。单击「我已了解」继续安装。"
  LangString cxNoticeBtn     ${LANG_SIMPCHINESE} "我已了解(&I)"
  LangString cxDirTop        ${LANG_SIMPCHINESE} "智聊将安装到下面的文件夹（默认在当前用户目录，不需要管理员权限）。如需更换位置，单击「浏览」；然后单击「安装」开始。"
  LangString cxFinTitle      ${LANG_SIMPCHINESE} "智聊已安装完成"
  LangString cxFinText       ${LANG_SIMPCHINESE} "现在可以启动智聊了。$\r$\n$\r$\n第一次使用：按首启向导添加账号、用手机扫码登录即可，无需填写任何 API Key——AI 能力经官网安全通道自动可用。$\r$\n$\r$\n使用指南与客服入口见 bd2026.cc。"
  LangString cxFinRun        ${LANG_SIMPCHINESE} "立即启动智聊(&R)"
  LangString cxFinLink       ${LANG_SIMPCHINESE} "使用指南与客服：bd2026.cc/download/chatx"
  LangString cxUnWelTitle    ${LANG_SIMPCHINESE} "卸载 智聊"
  LangString cxUnWelText     ${LANG_SIMPCHINESE} "此向导将从本机移除智聊程序。$\r$\n$\r$\n  ·  如智聊仍在运行，卸载时会提示您关闭$\r$\n  ·  下一页可选择保留或删除本机数据——默认保留，重新安装后可继续使用$\r$\n$\r$\n单击「下一步」继续。"
  LangString cxUnFinTitle    ${LANG_SIMPCHINESE} "智聊已卸载"
  LangString cxUnFinText     ${LANG_SIMPCHINESE} "程序已从本机移除。$\r$\n$\r$\n若您选择了保留数据，重新安装后账号登录、聊天记录与设置会自动恢复。$\r$\n$\r$\n遇到问题或想重新下载，欢迎访问 bd2026.cc。"
  LangString cxUnFinLink     ${LANG_SIMPCHINESE} "重新下载或联系客服：bd2026.cc"
  LangString cxStInstalling  ${LANG_SIMPCHINESE} "正在安装智聊组件（约 1.3 GB），通常需要 1–3 分钟，请勿关闭此窗口…"
  LangString cxStReap        ${LANG_SIMPCHINESE} "正在停止残留的智聊后台服务…"
  LangString cxStWipe        ${LANG_SIMPCHINESE} "正在清除用户数据…"
  LangString cxStWipeLeft    ${LANG_SIMPCHINESE} "部分数据仍被占用，已安排稍后自动清除："
  LangString cxStWipeDone    ${LANG_SIMPCHINESE} "用户数据已清除。"
  LangString cxBranding      ${LANG_SIMPCHINESE} "无界科技 BOUNDLESS  ·  智聊 ChatX ${VERSION}"
  LangString cxCaption       ${LANG_SIMPCHINESE} "智聊 ChatX 安装"
  LangString cxUnCaption     ${LANG_SIMPCHINESE} "智聊 ChatX 卸载"
  LangString cxFlavorInternal ${LANG_SIMPCHINESE} "内测版"
  LangString cxUnFinTitleWiped ${LANG_SIMPCHINESE} "智聊及全部本地数据已删除"
  LangString cxUnFinWipedTail  ${LANG_SIMPCHINESE} "重新下载或联系客服：bd2026.cc"
  LangString cxUnFinTitleLeft  ${LANG_SIMPCHINESE} "智聊已卸载，部分数据待清除"
  LangString cxUnFinLeftTail   ${LANG_SIMPCHINESE} "以上文件会在您下次登录（或重启）时自动清除，无需其他操作。"
  LangString cxInsFinWiped     ${LANG_SIMPCHINESE} "旧数据已彻底清空，本次为全新初始状态。$\r$\n$\r$\n第一次使用：按首启向导添加账号、用手机扫码登录即可，无需填写任何 API Key。"
  LangString cxInsFinLeft      ${LANG_SIMPCHINESE} "部分旧数据仍被占用，将在您下次登录时自动清除；本次安装可正常使用。$\r$\n$\r$\n第一次使用：按首启向导添加账号、用手机扫码登录即可，无需填写任何 API Key。"
!macroend

; -----------------------------------------------------------------------------
; Uninstall page flow: welcome -> data disposition (new) -> instfiles -> finish
; Defining customUnWelcomePage REPLACES the stock welcome, so we re-insert it
; and append our custom page right behind it.
; -----------------------------------------------------------------------------
!macro customUnWelcomePage
  Var cxWipe        ; "1" only when the user explicitly chose wipe on the page
  Var cxWipeGo      ; final wipe decision -- own Var, NEVER a register (see below)
  Var cxDlg
  Var cxRadioKeep
  Var cxRadioWipe
  Var cxConfirmChk
  Var cxFontBold    ; dialog font at weight 700 for the two option captions
  Var cxLeft        ; verify: newline list of paths that survived the wipe
  Var cxUnFinTitle  ; finish page title/text: seeded in customUnInit, rewritten
  Var cxUnFinMsg    ;   by the wipe result (replaces the old result MessageBoxes)

  ; stock welcome copy said "make sure ChatX is not running" -- the uninstaller
  ; closes it itself (CHECK_APP_RUNNING) and the real decision is on the next page
  !define MUI_WELCOMEPAGE_TITLE "$(cxUnWelTitle)"
  !define MUI_WELCOMEPAGE_TEXT "$(cxUnWelText)"
  !insertmacro MUI_UNPAGE_WELCOME

  UninstPage custom un.cxDataPageCreate un.cxDataPageLeave

  ; radio state -> confirm checkbox enabled + wizard button caption follows the
  ; consequence ("Uninstall" vs "Erase all") so the button itself tells the user
  ; what happens next.
  Function un.cxSyncUi
    Pop $0 ; NSD_OnClick pushes the control hwnd; unused
    GetDlgItem $1 $HWNDPARENT 1
    ${NSD_GetState} $cxRadioWipe $0
    ${If} $0 == ${BST_CHECKED}
      EnableWindow $cxConfirmChk 1
      SendMessage $1 ${WM_SETTEXT} 0 "STR:$(cxBtnWipe)"
    ${Else}
      ${NSD_SetState} $cxConfirmChk ${BST_UNCHECKED}
      EnableWindow $cxConfirmChk 0
      SendMessage $1 ${WM_SETTEXT} 0 "STR:$(cxBtnKeep)"
    ${EndIf}
  FunctionEnd

  Function un.cxOpenDataDir
    Pop $0
    IfFileExists "${CX_USERDATA_DIR}\*.*" 0 +2
      ExecShell "open" "${CX_USERDATA_DIR}"
  FunctionEnd

  Function un.cxOpenSite
    Pop $0
    ExecShell "open" "https://bd2026.cc"
  FunctionEnd

  Function un.cxDataPageCreate
    !insertmacro MUI_HEADER_TEXT "$(cxUnDataTitle)" "$(cxUnDataSub)"
    nsDialogs::Create 1018
    Pop $cxDlg
    ${If} $cxDlg == error
      Abort
    ${EndIf}

    ; Layout (dialog units; custom page floor ~140u). Same grid as the install-side
    ; page so both pages read as one design:
    ;   0 intro | 12 keep (bold) | 24 detail x2 lines | 44 path (ellipsis) | 54 open
    ;   66 rule | 72 wipe (bold) | 84 warning x3 lines | 114 confirm | 130 help
    ${If} $cxFontBold == ""
      CreateFont $cxFontBold "$(^Font)" "$(^FontSize)" "700"
    ${EndIf}

    ${NSD_CreateLabel} 0 0u 300u 8u "$(cxUnIntro)"
    Pop $0

    ${NSD_CreateRadioButton} 0 12u 292u 10u "$(cxKeepRadio)"
    Pop $cxRadioKeep
    SendMessage $cxRadioKeep ${WM_SETFONT} $cxFontBold 1
    ${NSD_OnClick} $cxRadioKeep un.cxSyncUi

    ${NSD_CreateLabel} 10u 24u 282u 18u "$(cxKeepDetail)"
    Pop $0
    SetCtlColors $0 ${CX_C_TEXT2} transparent

    ; long %APPDATA% paths: ellipsize the middle instead of clipping the tail
    ${NSD_CreateLabel} 10u 44u 282u 8u ""
    Pop $0
    ${NSD_AddStyle} $0 ${SS_PATHELLIPSIS}
    ${NSD_SetText} $0 "$(cxKeepPath)${CX_USERDATA_DIR}"
    SetCtlColors $0 ${CX_C_TEXT2} transparent

    ${NSD_CreateLink} 10u 54u 150u 8u "$(cxOpenDir)"
    Pop $0
    SetCtlColors $0 ${CX_C_LINK} transparent
    ${NSD_OnClick} $0 un.cxOpenDataDir

    ${NSD_CreateHLine} 0 66u 300u 1u ""
    Pop $0

    ${NSD_CreateRadioButton} 0 72u 292u 10u "$(cxWipeRadio)"
    Pop $cxRadioWipe
    SendMessage $cxRadioWipe ${WM_SETFONT} $cxFontBold 1
    ${NSD_OnClick} $cxRadioWipe un.cxSyncUi

    ${NSD_CreateLabel} 10u 84u 282u 28u "$(cxWipeWarn)"
    Pop $0
    SetCtlColors $0 ${CX_C_WARN} transparent

    ${NSD_CreateCheckbox} 10u 114u 282u 10u "$(cxWipeConfirm)"
    Pop $cxConfirmChk

    ${NSD_CreateLink} 0 130u 260u 8u "$(cxHelpLink)"
    Pop $0
    SetCtlColors $0 ${CX_C_LINK} transparent
    ${NSD_OnClick} $0 un.cxOpenSite

    ; defaults: keep-data selected, confirm disabled, button caption synced
    ${NSD_SetState} $cxRadioKeep ${BST_CHECKED}
    Push $cxRadioKeep
    Call un.cxSyncUi

    nsDialogs::Show
  FunctionEnd

  Function un.cxDataPageLeave
    ${NSD_GetState} $cxRadioWipe $0
    ${If} $0 == ${BST_CHECKED}
      ${NSD_GetState} $cxConfirmChk $1
      ${If} $1 != ${BST_CHECKED}
        MessageBox MB_ICONEXCLAMATION|MB_OK "$(cxNeedConfirm)"
        Abort
      ${EndIf}
      StrCpy $cxWipe "1"
    ${Else}
      StrCpy $cxWipe "0"
    ${EndIf}
  FunctionEnd
!macroend

; -----------------------------------------------------------------------------
; Wipe execution. Runs at the tail of the uninstall section (after the template
; removed $INSTDIR and, for the CLI channel, already RMDir'd the two $APPDATA
; dirs -- our pass is idempotent and ALSO fixes what the template misses:
; process reaping first, the electron-updater cache, and honest verification.
; -----------------------------------------------------------------------------

; Deletion with a retry budget, then a reboot-time fallback. Short-lived locks
; are the NORM here, not the exception (measured 2026-08-21: Defender scans the
; ~500MB installer.exe that the install step copies into the updater cache;
; a single immediate RMDir -- and even a 2s retry -- loses that race).
;   1. retry up to 6 times, 2.5s apart (~15s budget, covers AV scan windows);
;   2. whatever is STILL locked gets BOTH fallbacks:
;      - RMDir /r /REBOOTOK: PendingFileRenameOperations, deleted at boot --
;        but writing it needs elevation, so it only helps when the uninstall
;        runs elevated (e.g. ops scripts from an admin shell). Measured
;        2026-08-21: under a plain per-user run it silently no-ops.
;      - HKCU RunOnce `rd /s /q`: the PER-USER equivalent, runs at the user's
;        next logon, needs no elevation at all. This is the fallback that
;        actually fires for normal end users.
;      The user-facing report words it as "scheduled for automatic removal".
; $R7 is the retry counter (free in this section: $R0/$R1/$R8/$R9 are taken).
!macro cxRmDirRetry TARGET_DIR
  !define cxRmId ${__LINE__}
  StrCpy $R7 0
  cxRmTry_${cxRmId}:
    RMDir /r "${TARGET_DIR}"
    IfFileExists "${TARGET_DIR}\*.*" 0 cxRmDone_${cxRmId}
    IntOp $R7 $R7 + 1
    IntCmp $R7 6 cxRmGiveUp_${cxRmId} 0 cxRmGiveUp_${cxRmId}
    Sleep 2500
    Goto cxRmTry_${cxRmId}
  cxRmGiveUp_${cxRmId}:
    RMDir /r /REBOOTOK "${TARGET_DIR}"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\RunOnce" "ChatXWipe${cxRmId}" 'cmd /c rd /s /q "${TARGET_DIR}"'
    FileWrite $R6 "target=[${TARGET_DIR}] SURVIVED retries=$R7 -> rebootok+runonce$\r$\n"
    Goto cxRmEnd_${cxRmId}
  cxRmDone_${cxRmId}:
    FileWrite $R6 "target=[${TARGET_DIR}] gone (retries=$R7)$\r$\n"
  cxRmEnd_${cxRmId}:
  !undef cxRmId
!macroend
!macro customUnInstall
  ; DECISION VAR MUST NOT BE A REGISTER (2026-08-21 live-fire root cause):
  ; electron-builder's generated flag tests (isUpdated/isForceRun/...) expand to
  ;   ${StdUtils.TestParameter} $R9 "<flag>"  --  they CLOBBER $R9. The first
  ; version kept the wipe decision in $R9, so `${If} ${isUpdated}` overwrote it
  ; with "false" on every non-update uninstall and the whole wipe block below
  ; was dead code (template's built-in --delete-app-data pass hid it by erasing
  ; the two Roaming dirs; updater cache survived, no wipe log, no RunOnce).
  StrCpy $cxWipeGo "0"
  ${If} $cxWipe == "1"                          ; interactive page choice
    StrCpy $cxWipeGo "1"
  ${EndIf}
  ClearErrors
  ${GetParameters} $R0
  ${GetOptions} $R0 "--delete-app-data" $R1     ; explicit ops/CLI channel (C2)
  ${IfNot} ${Errors}
    StrCpy $cxWipeGo "1"
  ${EndIf}
  ${If} ${isUpdated}                            ; C1: an update NEVER wipes
    StrCpy $cxWipeGo "0"                        ; (this test clobbers $R9!)
  ${EndIf}

  ${If} $cxWipeGo == "1"
    DetailPrint "$(cxStWipe)"

    ; the template just RMDir'd $INSTDIR while our CWD sat inside it (un.onInit
    ; does SetOutPath $INSTDIR) -- move CWD out so child processes spawn with a
    ; valid working dir and the leftover root dir can actually be reclaimed
    SetOutPath $TEMP

    ; wipe battle log (paths + existence only, no user content): the wipe runs
    ; silent in ops channels, DetailPrint evaporates -- this file is the only
    ; first-scene record when a wipe misbehaves in the field
    FileOpen $R6 "$TEMP\chatx_wipe.log" a
    FileSeek $R6 0 END
    FileWrite $R6 "== chatx wipe == APPDATA=[$APPDATA] LOCALAPPDATA=[$LOCALAPPDATA] TEMP=[$TEMP]$\r$\n"

    ; C4: reap backend/sidecars by PATH, two rounds; never touch '*Uninstall*'
    nsExec::Exec `powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Process | Where-Object { $$_.Path -like '*${APP_PACKAGE_NAME}*' -and $$_.Path -notlike '*Uninstall*' } | Stop-Process -Force -ErrorAction SilentlyContinue"`
    Sleep 1500
    nsExec::Exec `powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Process | Where-Object { $$_.Path -like '*${APP_PACKAGE_NAME}*' -and $$_.Path -notlike '*Uninstall*' } | Stop-Process -Force -ErrorAction SilentlyContinue"`
    Sleep 500

    ; C3: macro-derived targets only. Recovery copies (*_bak20*) are sibling
    ; dirs of these and are deliberately NOT touched (ops discipline: recovery
    ; copies are only ever deleted by a human).
    !insertmacro cxRmDirRetry "$APPDATA\${APP_FILENAME}"
    !ifdef APP_PRODUCT_FILENAME
      !insertmacro cxRmDirRetry "$APPDATA\${APP_PRODUCT_FILENAME}"
    !endif
    !ifdef APP_PACKAGE_NAME
      !insertmacro cxRmDirRetry "$APPDATA\${APP_PACKAGE_NAME}"
      !insertmacro cxRmDirRetry "$LOCALAPPDATA\${APP_PACKAGE_NAME}-updater"
    !else
      !insertmacro cxRmDirRetry "$LOCALAPPDATA\${APP_FILENAME}-updater"
    !endif

    ; C5: verify + honest report. Product dir FIRST -- it is the MAIN data dir
    ; (see CX_USERDATA_DIR note); missing it here meant "all erased" could lie
    ; while %APPDATA%\<product> survived locked (found in 2026-08-21 review).
    StrCpy $cxLeft ""
    !ifdef APP_PRODUCT_FILENAME
      IfFileExists "$APPDATA\${APP_PRODUCT_FILENAME}\*.*" 0 +2
        StrCpy $cxLeft "$cxLeft$APPDATA\${APP_PRODUCT_FILENAME}$\r$\n"
    !endif
    IfFileExists "$APPDATA\${APP_FILENAME}\*.*" 0 +2
      StrCpy $cxLeft "$cxLeft$APPDATA\${APP_FILENAME}$\r$\n"
    !ifdef APP_PACKAGE_NAME
      IfFileExists "$APPDATA\${APP_PACKAGE_NAME}\*.*" 0 +2
        StrCpy $cxLeft "$cxLeft$APPDATA\${APP_PACKAGE_NAME}$\r$\n"
      IfFileExists "$LOCALAPPDATA\${APP_PACKAGE_NAME}-updater\*.*" 0 +2
        StrCpy $cxLeft "$cxLeft$LOCALAPPDATA\${APP_PACKAGE_NAME}-updater$\r$\n"
    !endif
    FileWrite $R6 "leftovers=[$cxLeft]$\r$\n"
    FileClose $R6

    ; result goes to the finish page (title + text), not a modal
    ${If} $cxLeft != ""
      DetailPrint "$(cxStWipeLeft)"
      DetailPrint "$cxLeft"
      StrCpy $cxUnFinTitle "$(cxUnFinTitleLeft)"
      StrCpy $cxUnFinMsg "$(cxWipeLeftover)$\r$\n$cxLeft$\r$\n$(cxUnFinLeftTail)"
    ${Else}
      DetailPrint "$(cxStWipeDone)"
      StrCpy $cxUnFinTitle "$(cxUnFinTitleWiped)"
      StrCpy $cxUnFinMsg "$(cxWipeDone)$\r$\n$\r$\n$(cxUnFinWipedTail)"
    ${EndIf}
  ${EndIf}
!macroend

; =============================================================================
; INSTALLER PAGE FLOW (installer unit only; the template wraps customWelcomePage
; in !ifndef BUILD_UNINSTALLER):
;   welcome (new 2026-09-05) -> data disposition (C7, only when data exists)
;   -> notice (per-language RTF, was "License Agreement") -> [install mode,
;   skipped via customInstallMode] -> directory -> instfiles -> finish
;
; Why the data page sits right after the welcome page and NOT after the
; directory page (its home since 2026-08-24): NSIS labels the Next button
; "Install" only on the page that is physically last before instfiles. With the
; data page after the directory page, a fresh install (page skipped at runtime)
; showed "Next" on the directory page and then started installing at once --
; no "Install" click anywhere. Now the directory page is always last, so the
; "Install" button is always real; and existing users answer the one question
; that matters to them first.
;
; INSTALL-TIME data disposition (C7, 2026-08-24) -- the mirror of the uninstall
; page for the OTHER direction: installing/upgrading onto a machine that already
; has data. Semantics:
;   [default] keep  -> logins/history/settings all keep working (today's
;                      behaviour, unchanged for silent + auto-update channels)
;   [opt-in ] wipe  -> factory-fresh install (same targets + retry/fallback
;                      machinery as the uninstall wipe; confirm checkbox gated)
;   auto-update (--updated) NEVER wipes and never shows the page (C1 spirit);
;   silent channel keeps data unless `--wipe-app-data` is passed explicitly
;   (mirror of the uninstaller's `--delete-app-data` ops channel).
; Page Vars live inside this macro: it expands only in the installer unit's
; page area -- top-level Vars would be "declared, not referenced" in the
; uninstaller unit and makensis -WX turns that into a hard failure.
; =============================================================================
!macro customWelcomePage
  Var cxInsWipe     ; "1" only when the user explicitly chose wipe on the page
  Var cxInsWipeGo   ; final decision -- own Var, NEVER a register ($R9 lesson)
  Var cxInsDlg
  Var cxInsRadioKeep
  Var cxInsRadioWipe
  Var cxInsConfirmChk
  Var cxInsFontBold ; dialog font at weight 700 for the two option captions
  Var cxInsLeft     ; verify: newline list of paths that survived the wipe
  Var cxInsFinMsg   ; finish page text: seeded in customInit, rewritten by the wipe result

  ; electron-builder's assisted installer has NO welcome page by default -- the
  ; first thing a user saw was the license page titled "License Agreement".
  !define MUI_WELCOMEPAGE_TITLE "$(cxWelTitle)"
  !define MUI_WELCOMEPAGE_TEXT "$(cxWelText)"
  !insertmacro skipPageIfUpdated
  !insertmacro MUI_PAGE_WELCOME

  Page custom cxInsDataPageCreate cxInsDataPageLeave

  ; Page settings for the pages the template inserts right after this macro
  ; (MUI consumes them on the next matching page insert; a plain `Page custom`
  ; does not touch them):
  ;   notice page = per-language build/license_*.rtf (write-installer-notice.js)
  !define MUI_PAGE_HEADER_TEXT "$(cxNoticeTitle)"
  !define MUI_PAGE_HEADER_SUBTEXT "$(cxNoticeSub)"
  ; the consent sentence lives in the top label (always visible, no scrolling)
  ; so the RTF body only has to carry the two informational sections
  !define MUI_LICENSEPAGE_TEXT_TOP "$(cxNoticeTop)"
  !define MUI_LICENSEPAGE_TEXT_BOTTOM "$(cxNoticeBottom)"
  !define MUI_LICENSEPAGE_BUTTON "$(cxNoticeBtn)"
  ;   directory page
  !define MUI_DIRECTORYPAGE_TEXT_TOP "$(cxDirTop)"

  Function cxInsSyncUi
    Pop $0 ; NSD_OnClick pushes the control hwnd; unused
    ${NSD_GetState} $cxInsRadioWipe $0
    ${If} $0 == ${BST_CHECKED}
      EnableWindow $cxInsConfirmChk 1
    ${Else}
      ${NSD_SetState} $cxInsConfirmChk ${BST_UNCHECKED}
      EnableWindow $cxInsConfirmChk 0
    ${EndIf}
  FunctionEnd

  Function cxInsOpenDataDir
    Pop $0
    IfFileExists "${CX_DATA_DIR_PKG}\*.*" 0 +3
      ExecShell "open" "${CX_DATA_DIR_PKG}"
      Goto +2
    ExecShell "open" "${CX_USERDATA_DIR}"
  FunctionEnd

  ; mirror of un.cxOpenSite -- the install-side help link shipped without an
  ; OnClick handler (dead link) until 2026-09-05
  Function cxInsOpenSite
    Pop $0
    ExecShell "open" "https://bd2026.cc"
  FunctionEnd

  Function cxInsDataPageCreate
    ; auto-update path (installer launched with --updated) never offers wipe --
    ; an update must feel like an update, not a decision tree (C1 spirit).
    ${If} ${isUpdated}
      Abort
    ${EndIf}
    ; fresh machine (no data anywhere) -> nothing to decide, skip the page.
    ; Check BOTH dirs: package dir is the real userData in the field, the CJK
    ; product dir is the belt (see CX_DATA_DIR_PKG note).
    IfFileExists "${CX_DATA_DIR_PKG}\*.*" cxInsHasData 0
    IfFileExists "${CX_USERDATA_DIR}\*.*" cxInsHasData 0
    Abort
    cxInsHasData:

    !insertmacro MUI_HEADER_TEXT "$(cxInsDataTitle)" "$(cxInsDataSub)"
    nsDialogs::Create 1018
    Pop $cxInsDlg
    ${If} $cxInsDlg == error
      Abort
    ${EndIf}

    ; same grid as un.cxDataPageCreate (see there)
    ${If} $cxInsFontBold == ""
      CreateFont $cxInsFontBold "$(^Font)" "$(^FontSize)" "700"
    ${EndIf}

    ${NSD_CreateLabel} 0 0u 300u 8u "$(cxInsIntro)"
    Pop $0

    ${NSD_CreateRadioButton} 0 12u 292u 10u "$(cxInsKeepRadio)"
    Pop $cxInsRadioKeep
    SendMessage $cxInsRadioKeep ${WM_SETFONT} $cxInsFontBold 1
    ${NSD_OnClick} $cxInsRadioKeep cxInsSyncUi

    ${NSD_CreateLabel} 10u 24u 282u 18u "$(cxKeepDetail)"
    Pop $0
    SetCtlColors $0 ${CX_C_TEXT2} transparent

    ${NSD_CreateLabel} 10u 44u 282u 8u ""
    Pop $0
    ${NSD_AddStyle} $0 ${SS_PATHELLIPSIS}
    ${NSD_SetText} $0 "$(cxKeepPath)${CX_DATA_DIR_PKG}"
    SetCtlColors $0 ${CX_C_TEXT2} transparent

    ${NSD_CreateLink} 10u 54u 150u 8u "$(cxOpenDir)"
    Pop $0
    SetCtlColors $0 ${CX_C_LINK} transparent
    ${NSD_OnClick} $0 cxInsOpenDataDir

    ${NSD_CreateHLine} 0 66u 300u 1u ""
    Pop $0

    ${NSD_CreateRadioButton} 0 72u 292u 10u "$(cxInsWipeRadio)"
    Pop $cxInsRadioWipe
    SendMessage $cxInsRadioWipe ${WM_SETFONT} $cxInsFontBold 1
    ${NSD_OnClick} $cxInsRadioWipe cxInsSyncUi

    ${NSD_CreateLabel} 10u 84u 282u 28u "$(cxInsWipeWarn)"
    Pop $0
    SetCtlColors $0 ${CX_C_WARN} transparent

    ${NSD_CreateCheckbox} 10u 114u 282u 10u "$(cxWipeConfirm)"
    Pop $cxInsConfirmChk

    ${NSD_CreateLink} 0 130u 260u 8u "$(cxHelpLink)"
    Pop $0
    SetCtlColors $0 ${CX_C_LINK} transparent
    ${NSD_OnClick} $0 cxInsOpenSite

    ; defaults: keep-data selected, confirm disabled
    ${NSD_SetState} $cxInsRadioKeep ${BST_CHECKED}
    Push $cxInsRadioKeep
    Call cxInsSyncUi

    nsDialogs::Show
  FunctionEnd

  Function cxInsDataPageLeave
    ${NSD_GetState} $cxInsRadioWipe $0
    ${If} $0 == ${BST_CHECKED}
      ${NSD_GetState} $cxInsConfirmChk $1
      ${If} $1 != ${BST_CHECKED}
        MessageBox MB_ICONEXCLAMATION|MB_OK "$(cxNeedConfirm)"
        Abort
      ${EndIf}
      StrCpy $cxInsWipe "1"
    ${Else}
      StrCpy $cxInsWipe "0"
    ${EndIf}
  FunctionEnd
!macroend

; customPageAfterChangeDir expands right after MUI_PAGE_DIRECTORY and right
; before the template's MUI_PAGE_INSTFILES -- the only hook that can attach a
; page function to the instfiles page. The data page used to live here; see the
; flow note above for why it moved.
!macro customPageAfterChangeDir
  ; The stock instfiles page is mute: the template runs `SetDetailsPrint none`
  ; and Nsis7z::Extract prints nothing, so during the whole ~1.5 GB extraction
  ; (plus the silent uninstall of the previous version before it) the user sees
  ; a bare progress bar. Put one honest status line into the status control
  ; (1006) when the page shows; nothing else writes it afterwards so it stays.
  ; _PRE is taken by the template's instFilesPre, _SHOW is free.
  !define MUI_PAGE_CUSTOMFUNCTION_SHOW cxInstFilesShow
  Function cxInstFilesShow
    FindWindow $0 "#32770" "" $HWNDPARENT
    GetDlgItem $1 $0 1006
    SendMessage $1 ${WM_SETTEXT} 0 "STR:$(cxStInstalling)"
  FunctionEnd
!macroend

; -----------------------------------------------------------------------------
; Install-section wipe execution (runs AFTER files are installed, BEFORE the
; app first launches). Process family is already dead here: stock
; _CHECK_APP_RUNNING closed the shell (graceful close lets the shell's
; before-quit stopAndWait take the backend down cleanly) and cxReapFamily
; swept any orphans -- so the RMDirs below do not race live handles.
; -----------------------------------------------------------------------------
!macro customInstall
  ; decision var, never a register: ${isUpdated} expands to a StdUtils test
  ; that clobbers $R9 (same 2026-08-21 live-fire lesson as the uninstaller).
  StrCpy $cxInsWipeGo "0"
  ${If} $cxInsWipe == "1"                       ; interactive page choice
    StrCpy $cxInsWipeGo "1"
  ${EndIf}
  ClearErrors
  ${GetParameters} $R0
  ${GetOptions} $R0 "--wipe-app-data" $R1       ; explicit silent/CLI channel
  ${IfNot} ${Errors}
    StrCpy $cxInsWipeGo "1"
  ${EndIf}
  ${If} ${isUpdated}                            ; an update NEVER wipes
    StrCpy $cxInsWipeGo "0"
  ${EndIf}

  ${If} $cxInsWipeGo == "1"
    DetailPrint "$(cxStWipe)"
    FileOpen $R6 "$TEMP\chatx_install_wipe.log" a
    FileSeek $R6 0 END
    FileWrite $R6 "== chatx install wipe == APPDATA=[$APPDATA] LOCALAPPDATA=[$LOCALAPPDATA]$\r$\n"

    !insertmacro cxRmDirRetry "$APPDATA\${APP_FILENAME}"
    !ifdef APP_PRODUCT_FILENAME
      !insertmacro cxRmDirRetry "$APPDATA\${APP_PRODUCT_FILENAME}"
    !endif
    !ifdef APP_PACKAGE_NAME
      !insertmacro cxRmDirRetry "$APPDATA\${APP_PACKAGE_NAME}"
      !insertmacro cxRmDirRetry "$LOCALAPPDATA\${APP_PACKAGE_NAME}-updater"
    !else
      !insertmacro cxRmDirRetry "$LOCALAPPDATA\${APP_FILENAME}-updater"
    !endif

    StrCpy $cxInsLeft ""
    !ifdef APP_PRODUCT_FILENAME
      IfFileExists "$APPDATA\${APP_PRODUCT_FILENAME}\*.*" 0 +2
        StrCpy $cxInsLeft "$cxInsLeft$APPDATA\${APP_PRODUCT_FILENAME}$\r$\n"
    !endif
    IfFileExists "$APPDATA\${APP_FILENAME}\*.*" 0 +2
      StrCpy $cxInsLeft "$cxInsLeft$APPDATA\${APP_FILENAME}$\r$\n"
    !ifdef APP_PACKAGE_NAME
      IfFileExists "$APPDATA\${APP_PACKAGE_NAME}\*.*" 0 +2
        StrCpy $cxInsLeft "$cxInsLeft$APPDATA\${APP_PACKAGE_NAME}$\r$\n"
    !endif
    FileWrite $R6 "leftovers=[$cxInsLeft]$\r$\n"
    FileClose $R6

    ; result goes to the finish page text. Leftovers additionally keep the modal:
    ; the user is about to launch onto half-wiped data, that deserves a stop.
    ${If} $cxInsLeft != ""
      DetailPrint "$(cxStWipeLeft)"
      DetailPrint "$cxInsLeft"
      StrCpy $cxInsFinMsg "$(cxInsFinLeft)"
      ${IfNot} ${Silent}
        MessageBox MB_ICONEXCLAMATION|MB_OK "$(cxWipeLeftover)$\r$\n$\r$\n$cxInsLeft"
      ${EndIf}
    ${Else}
      DetailPrint "$(cxStWipeDone)"
      StrCpy $cxInsFinMsg "$(cxInsFinWiped)"
    ${EndIf}
  ${EndIf}
!macroend
