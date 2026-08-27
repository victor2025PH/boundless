; =============================================================================
; ChatX custom NSIS include -- uninstaller "data disposition" page + wipe logic
; =============================================================================
; WHY THIS FILE EXISTS
;   The stock electron-builder uninstaller removes the PROGRAM only; every bit
;   of user data survives under %APPDATA% (that is the upgrade-self-heal
;   contract and stays the DEFAULT). This include adds an explicit, honest
;   choice at uninstall time:
;     [default] keep data  -> upgrade/reinstall continues where the user left
;     [opt-in ] wipe data  -> next install is factory fresh (databases, logins,
;                             kb/personas/albums/voices, settings, update cache)
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
  DetailPrint "ChatX: reaping leftover background services..."
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
  LangString cxUnDataTitle   ${LANG_ENGLISH} "Your data"
  LangString cxUnDataSub     ${LANG_ENGLISH} "Choose what happens to the data stored on this computer"
  LangString cxUnIntro       ${LANG_ENGLISH} "The program will be uninstalled. Your local data:"
  LangString cxKeepRadio     ${LANG_ENGLISH} "Keep my data (recommended)"
  LangString cxKeepDetail    ${LANG_ENGLISH} "Everything keeps working after an upgrade or reinstall:$\r$\n   - account logins on all platforms$\r$\n   - chat history and customer profiles$\r$\n   - knowledge base, personas, albums, cloned voices$\r$\n   - app settings"
  LangString cxKeepPath      ${LANG_ENGLISH} "Location: "
  LangString cxOpenDir       ${LANG_ENGLISH} "Open data folder"
  LangString cxWipeRadio     ${LANG_ENGLISH} "Erase ALL data permanently"
  LangString cxWipeWarn      ${LANG_ENGLISH} "Everything listed above plus the update cache is deleted for good. Accounts must log in again; the next install starts completely fresh."
  LangString cxWipeConfirm   ${LANG_ENGLISH} "I understand this cannot be undone"
  LangString cxHelpLink      ${LANG_ENGLISH} "Need help or a fresh download? Visit bd2026.cc"
  LangString cxBtnKeep       ${LANG_ENGLISH} "Uninstall"
  LangString cxBtnWipe       ${LANG_ENGLISH} "Erase all"
  LangString cxNeedConfirm   ${LANG_ENGLISH} "Please tick $\"I understand this cannot be undone$\" first, or switch back to $\"Keep my data$\"."
  LangString cxWipeDone      ${LANG_ENGLISH} "All local data has been erased. The next install will start completely fresh."
  LangString cxWipeLeftover  ${LANG_ENGLISH} "Some files are currently in use and have been scheduled for automatic removal on the next reboot:"

  LangString cxUnDataTitle   ${LANG_SIMPCHINESE} "您的数据"
  LangString cxUnDataSub     ${LANG_SIMPCHINESE} "请选择如何处理保存在本机的数据"
  LangString cxUnIntro       ${LANG_SIMPCHINESE} "即将卸载智聊程序。本机保存的数据："
  LangString cxKeepRadio     ${LANG_SIMPCHINESE} "保留我的数据（推荐）"
  LangString cxKeepDetail    ${LANG_SIMPCHINESE} "升级或重新安装后可继续使用，包括：$\r$\n   · 各平台账号登录状态$\r$\n   · 聊天记录与客户资料$\r$\n   · 知识库、人设、相册与克隆语音$\r$\n   · 软件设置"
  LangString cxKeepPath      ${LANG_SIMPCHINESE} "数据位置："
  LangString cxOpenDir       ${LANG_SIMPCHINESE} "打开数据文件夹"
  LangString cxWipeRadio     ${LANG_SIMPCHINESE} "彻底删除所有数据"
  LangString cxWipeWarn      ${LANG_SIMPCHINESE} "以上全部数据与更新缓存将被永久删除，无法恢复；所有账号需重新登录，下次安装将是全新初始状态。"
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
  LangString cxInsWipeWarn   ${LANG_ENGLISH} "Everything listed above plus the update cache is erased for good before installing. All accounts must log in again."

  LangString cxInsDataTitle  ${LANG_SIMPCHINESE} "检测到本机已有数据"
  LangString cxInsDataSub    ${LANG_SIMPCHINESE} "本机保留着之前安装的数据，请选择如何继续"
  LangString cxInsIntro      ${LANG_SIMPCHINESE} "检测到之前安装留下的数据："
  LangString cxInsKeepRadio  ${LANG_SIMPCHINESE} "保留我的数据，继续安装（推荐）"
  LangString cxInsWipeRadio  ${LANG_SIMPCHINESE} "全新开始：先彻底清空旧数据再安装"
  LangString cxInsWipeWarn   ${LANG_SIMPCHINESE} "以上全部数据与更新缓存将在安装前被永久删除，无法恢复；所有账号需重新登录。"
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
  Var cxLeft        ; verify: newline list of paths that survived the wipe

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

    ${NSD_CreateLabel} 0 0u 300u 8u "$(cxUnIntro)"
    Pop $0

    ${NSD_CreateRadioButton} 0 11u 292u 10u "$(cxKeepRadio)"
    Pop $cxRadioKeep
    ${NSD_OnClick} $cxRadioKeep un.cxSyncUi

    ${NSD_CreateLabel} 10u 23u 282u 40u "$(cxKeepDetail)"
    Pop $0
    SetCtlColors $0 0x666666 transparent

    ${NSD_CreateLabel} 10u 64u 282u 8u "$(cxKeepPath)${CX_USERDATA_DIR}"
    Pop $0
    SetCtlColors $0 0x666666 transparent

    ${NSD_CreateLink} 10u 73u 150u 8u "$(cxOpenDir)"
    Pop $0
    ${NSD_OnClick} $0 un.cxOpenDataDir

    ${NSD_CreateRadioButton} 0 85u 292u 10u "$(cxWipeRadio)"
    Pop $cxRadioWipe
    ${NSD_OnClick} $cxRadioWipe un.cxSyncUi

    ${NSD_CreateLabel} 10u 97u 282u 18u "$(cxWipeWarn)"
    Pop $0
    SetCtlColors $0 0x1F1FBF transparent

    ${NSD_CreateCheckbox} 10u 117u 282u 10u "$(cxWipeConfirm)"
    Pop $cxConfirmChk

    ${NSD_CreateLink} 0 131u 260u 8u "$(cxHelpLink)"
    Pop $0
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
    DetailPrint "ChatX: erasing user data..."

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

    ${If} $cxLeft != ""
      DetailPrint "ChatX: some data survived (locked):"
      DetailPrint "$cxLeft"
      ${IfNot} ${Silent}
        MessageBox MB_ICONEXCLAMATION|MB_OK "$(cxWipeLeftover)$\r$\n$\r$\n$cxLeft"
      ${EndIf}
    ${Else}
      DetailPrint "ChatX: user data erased."
      ${IfNot} ${Silent}
        MessageBox MB_ICONINFORMATION|MB_OK "$(cxWipeDone)"
      ${EndIf}
    ${EndIf}
  ${EndIf}
!macroend

; =============================================================================
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
!macro customPageAfterChangeDir
  Var cxInsWipe     ; "1" only when the user explicitly chose wipe on the page
  Var cxInsWipeGo   ; final decision -- own Var, NEVER a register ($R9 lesson)
  Var cxInsDlg
  Var cxInsRadioKeep
  Var cxInsRadioWipe
  Var cxInsConfirmChk
  Var cxInsLeft     ; verify: newline list of paths that survived the wipe

  Page custom cxInsDataPageCreate cxInsDataPageLeave

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

    ${NSD_CreateLabel} 0 0u 300u 8u "$(cxInsIntro)"
    Pop $0

    ${NSD_CreateRadioButton} 0 11u 292u 10u "$(cxInsKeepRadio)"
    Pop $cxInsRadioKeep
    ${NSD_OnClick} $cxInsRadioKeep cxInsSyncUi

    ${NSD_CreateLabel} 10u 23u 282u 40u "$(cxKeepDetail)"
    Pop $0
    SetCtlColors $0 0x666666 transparent

    ${NSD_CreateLabel} 10u 64u 282u 8u "$(cxKeepPath)${CX_DATA_DIR_PKG}"
    Pop $0
    SetCtlColors $0 0x666666 transparent

    ${NSD_CreateLink} 10u 73u 150u 8u "$(cxOpenDir)"
    Pop $0
    ${NSD_OnClick} $0 cxInsOpenDataDir

    ${NSD_CreateRadioButton} 0 85u 292u 10u "$(cxInsWipeRadio)"
    Pop $cxInsRadioWipe
    ${NSD_OnClick} $cxInsRadioWipe cxInsSyncUi

    ${NSD_CreateLabel} 10u 97u 282u 18u "$(cxInsWipeWarn)"
    Pop $0
    SetCtlColors $0 0x1F1FBF transparent

    ${NSD_CreateCheckbox} 10u 117u 282u 10u "$(cxWipeConfirm)"
    Pop $cxInsConfirmChk

    ${NSD_CreateLink} 0 131u 260u 8u "$(cxHelpLink)"
    Pop $0

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
    DetailPrint "ChatX: erasing previous user data (fresh start)..."
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

    ${If} $cxInsLeft != ""
      DetailPrint "ChatX: some old data survived (locked):"
      DetailPrint "$cxInsLeft"
      ${IfNot} ${Silent}
        MessageBox MB_ICONEXCLAMATION|MB_OK "$(cxWipeLeftover)$\r$\n$\r$\n$cxInsLeft"
      ${EndIf}
    ${Else}
      DetailPrint "ChatX: previous user data erased (fresh start)."
    ${EndIf}
  ${EndIf}
!macroend
