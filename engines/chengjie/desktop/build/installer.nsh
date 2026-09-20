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
;   C10 UPGRADE PRE-CLEAN (installer unit, end of customCheckAppRunning): once
;       the family is reaped we empty $INSTDIR ourselves, non-atomically, keeping
;       only the old uninstaller exe -- so the OLD version's atomicRMDir (which
;       C9 cannot reach) renames a handful of entries instead of 9429. Template
;       call and `--updated` untouched; nothing queued for REBOOTOK/RunOnce;
;       %APPDATA% never looked at; fresh install returns without side effects.
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
; guards on !ifmacrondef). We used to insert _CHECK_APP_RUNNING ourselves and so
; needed both; since 1.0.93 we own the whole "close the app" step (see below) and
; never insert it, so neither is included -- an unreferenced `Var pid` is a
; warning and makensis runs with -WX.
; -----------------------------------------------------------------------------

; 2026-09-20 live-fire root cause (the reason this macro is what it is):
; the first version matched processes with `Get-Process | Where $_.Path -like`.
; `Process.Path` resolves through Process.MainModule, which needs
; PROCESS_VM_READ on the target -- from a per-user installer that is denied for
; nearly every process. Measured on 117 with a purpose-built makensis harness:
; 342 processes visible, only 18 with a readable .Path, predicate matched 0,
; squatter survived. So the whole C6 safety net was a **no-op in the field**
; from the day it was written, and nobody noticed while 智聊.exe (which the
; stock name-based check does cover) was the only thing living in $INSTDIR.
; Since 1.0.90 ships the frozen backend.exe + its WeChat PC driver child inside
; resources\backend\, there IS such a process now -- it survives the stock
; check, un.atomicRMDir cannot rename its image, the old uninstaller aborts
; with exit 2, and the user gets 「智聊 无法关闭」 + 「Failed to uninstall old
; application files: 2」 (field reports 2026-09-19/20, reproduced end-to-end in
; an isolated test user).
; Win32_Process.ExecutablePath comes from the kernel via WMI and needs no
; handle on the target, so it sees everything the user owns.
;
; 2026-09-20 round 2 (field reports on 1.0.92 -- the fix above was necessary but
; NOT sufficient): the 「智聊 无法关闭。请手动关闭它，然后单击重试以继续。」 dialog
; does not come from our code at all, it comes from the STOCK
; _CHECK_APP_RUNNING, which 1.0.92 still ran FIRST. Read its loop
; (node_modules/app-builder-lib/templates/nsis/include/allowOnlyOneInstallerInstance.nsh):
;   taskkill /im (graceful) -> Sleep 300 -> [find -> Sleep 1000 -> taskkill /f ->
;   find AGAIN WITH NO SETTLE TIME -> Sleep 2000] x2 -> MessageBox appCannotBeClosed
; i.e. ~6.3s of total budget, and the decisive re-check happens in the same
; instant the force-kill was issued. Our family cannot meet that: main.js's
; before-quit deliberately preventDefault()s and waits for
; shutdownBackendAndWait() = backendManager.stopAndWait(8000) + sidecars.stopAll()
; -- 8s+ by design (B57: a half-dead backend racing the new one costs every
; Telegram account a re-login). Since 1.0.90 the family also includes the frozen
; backend.exe and its WeChat PC driver child, so teardown got slower still. The
; app that behaves exactly as designed therefore loses a race the template never
; documented, on the machines where teardown is slowest -- which is why it looks
; random, why clicking 重试 eventually works (by then the family IS gone), and why
; rebooting "fixes" it. Under /S it is worse than a dialog: appCannotBeClosed
; carries /SD IDCANCEL, so an unattended upgrade just Quits.
;
; So we no longer insert the stock macro. We own the sequence, and the shape of
; it is the lesson: ASK NICELY, THEN WAIT FOR PROOF OF DEATH, THEN FORCE, THEN
; VERIFY -- never a fixed sleep, never a budget shorter than our own shutdown
; path, and never a dead end for the user.

; Family predicate, single-sourced (both commands below must agree, and drift
; between "what we kill" and "what we verify" is how round 1 stayed invisible):
;   - anything running out of $INSTDIR (the REAL target dir -- ${APP_PACKAGE_NAME}
;     alone misses a custom install folder, which the wizard lets the user pick)
;   - anything under the default package dir (catches a stale install elsewhere)
;   - anything with the app's image name in OUR session (what the stock check
;     covered; session-scoped so another user's copy is reported, never fought)
;   - minus '*Uninstall*' (the uninstaller runs in place under `_?=` and would
;     kill itself -- C4 lesson) and '*-updater*' (that cache dir holds the
;     RUNNING auto-update installer)
!define CX_FAM_FILTER `Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $$_.ExecutablePath -and $$_.ExecutablePath -notlike '*Uninstall*' -and $$_.ExecutablePath -notlike '*-updater*' -and (($$_.ExecutablePath -like '$INSTDIR\*') -or ($$_.ExecutablePath -like '*\${APP_PACKAGE_NAME}\*') -or ($$_.Name -eq '${APP_EXECUTABLE_FILENAME}' -and $$_.SessionId -eq $$sid)) }`
; Absolute interpreter path on purpose: powershell.exe lives in
; System32\WindowsPowerShell\v1.0, NOT System32, so a bare `powershell` only
; resolves through PATH -- a broken/trimmed PATH would silently disarm all of
; this. (makensis here is the large-strings build, NSIS_MAX_STRLEN=8192, so these
; long -Command strings are nowhere near truncation.)
!define CX_PS `"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command`

; Step 1 -- ask nicely, then wait for the family to actually leave.
; CloseMainWindow() = WM_CLOSE, which is what lets before-quit run and take the
; backend + sidecars down cleanly. Then POLL: exit 0 the moment the family is
; empty, so a closed app costs no extra install time. ${SECS} must stay > our
; own shutdown budget (8s stopAndWait + sidecars) or we recreate the stock bug.
; Pushes 0 = family gone, 1 = still there (caller escalates).
!macro cxCloseAppNicely SECS
  nsExec::Exec `${CX_PS} "$$sid=(Get-Process -Id $$PID).SessionId; $$f={ @(${CX_FAM_FILTER}) }; if ((&$$f).Count -eq 0) { exit 0 }; (&$$f) | ForEach-Object { try { $$q=Get-Process -Id $$_.ProcessId -ErrorAction Stop; if ($$q.MainWindowHandle -ne 0) { $$null=$$q.CloseMainWindow() } } catch {} }; $$d=(Get-Date).AddSeconds(${SECS}); while ((Get-Date) -lt $$d) { if ((&$$f).Count -eq 0) { exit 0 }; Start-Sleep -Milliseconds 300 }; exit 1"`
!macroend

; Step 2 -- force, and keep verifying until the family is really gone. A fixed
; Sleep is not evidence of death and the very next thing the template does is
; rename every file in $INSTDIR. Survivors are written to
; %TEMP%\chatx_install_reap.log with pid/name/path: when this net fails in the
; field again, that file is the evidence instead of another round of guessing.
; Pushes 0 = family clear, 1 = something outlived the budget.
!macro cxReapFamily SECS
  nsExec::Exec `${CX_PS} "$$sid=(Get-Process -Id $$PID).SessionId; $$f={ @(${CX_FAM_FILTER}) }; $$d=(Get-Date).AddSeconds(${SECS}); while ($$true) { $$p=&$$f; if ($$p.Count -eq 0) { exit 0 }; if ((Get-Date) -ge $$d) { break }; $$p | ForEach-Object { Stop-Process -Id $$_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Milliseconds 400 }; (&$$f) | ForEach-Object { '{0} survivor pid={1} {2} {3}' -f (Get-Date -Format s),$$_.ProcessId,$$_.Name,$$_.ExecutablePath } | Add-Content -Path (Join-Path $$env:TEMP 'chatx_install_reap.log') -ErrorAction SilentlyContinue; exit 1"`
!macroend

; Step 3 -- the black box. `${APP_EXECUTABLE_FILENAME} 无法关闭` is NOT one
; failure: app-builder-lib prints that same LangString (appCannotBeClosed) from
; THREE different places, and a screenshot cannot tell them apart --
;   allowOnlyOneInstallerInstance.nsh : could not kill the app (we no longer run it)
;   installUtil.nsh UninstallLoop     : old uninstaller failed 5x (customUnInstallCheck)
;   extractAppPackage.nsh             : CopyFiles into $INSTDIR hit in-use files,
;                                       5 retries x Sleep 1000 = ~5s of tolerance,
;                                       and clicking 重试 is what falls through to
;                                       the non-atomic overwrite that finally works
;                                       (= the boss's 「再点一次继续才装完」).
; The last two are inside macros we cannot override (customFiles_* is only a
; POST-decompression hook), and the template runs `SetDetailsPrint none` in the
; assisted wizard, so the field has zero evidence to send us. So: write our own.
; One line per install with the stage, the process family, and which files under
; $INSTDIR are actually locked right before the template takes over -- next field
; report is read off this file instead of guessed at for a day.
!macro cxLogState TAG
  nsExec::Exec `${CX_PS} "$$sid=(Get-Process -Id $$PID).SessionId; $$l=Join-Path $$env:TEMP 'chatx_install_reap.log'; $$o=@('[{0}] ${TAG} v${VERSION} INSTDIR=$INSTDIR' -f (Get-Date -Format s)); (${CX_FAM_FILTER}) | ForEach-Object { $$o+=('  proc pid={0} {1} {2}' -f $$_.ProcessId,$$_.Name,$$_.ExecutablePath) }; @('${APP_EXECUTABLE_FILENAME}','resources\app.asar','resources\backend\backend.exe') | ForEach-Object { $$p=Join-Path '$INSTDIR' $$_; if (Test-Path $$p) { try { $$h=[IO.File]::Open($$p,'Open','ReadWrite','None'); $$h.Close() } catch { $$o+=('  LOCKED {0}' -f $$p) } } }; $$o | Add-Content -Path $$l -ErrorAction SilentlyContinue"`
  Pop $0
!macroend

!macro customCheckAppRunning
  ; Graceful first (B57), force second, and whatever survives BOTH does not get
  ; to fail the upgrade: extractUsing7za already retries and finally overwrites
  ; in place, and customUnInstallCheck (below) recovers the old-version uninstall
  ; the same way. A truthful note beats「点重试」 with nothing to retry, and the
  ; /SD keeps unattended installs moving instead of Quitting like the stock path.
  DetailPrint "$(cxStClose)"
  !insertmacro cxCloseAppNicely 15
  Pop $0
  ${If} $0 != 0
    DetailPrint "$(cxStReap)"
    !insertmacro cxReapFamily 20
    Pop $0
    ${If} $0 != 0
      DetailPrint "$(cxStReapLeft)"
      MessageBox MB_OK|MB_ICONEXCLAMATION "$(cxAppBusy)" /SD IDOK
    ${EndIf}
  ${EndIf}
  ; Always, not only on failure: "family empty and nothing locked" is exactly the
  ; line that proves the next dialog came from the extract stage instead of here.
  !insertmacro cxLogState "pre-uninstall"
  ; C11 -- installer unit only: the uninstaller expands this macro too (via
  ; un.checkAppRunning) and there is no "old version" for it to remove.
  !ifndef BUILD_UNINSTALLER
    !insertmacro cxTakeOverOldUninstall
  !endif
!macroend

; ---- C11: WE remove the old version; the old uninstaller never runs ----------
; Round 4, and the first one whose result does not depend on luck.
;
; C9 (customRemoveFiles) lives in the uninstaller we SHIP, so it only protects the
; NEXT upgrade -- the hop that installs it still runs the OLD uninstaller with
; `--updated`, i.e. un.atomicRMDir over 9429 files, and any ONE busy file there is
; rollback + Abort(2), five times, then MessageBox appCannotBeClosed from inside
; installUtil.nsh's UninstallLoop -- a place customUnInstallCheck cannot reach
; (handleUninstallResult runs only AFTER that loop gave up).
;
; C10 (Devin, 2026-09-20) tried to shrink the exposure: empty $INSTDIR ourselves
; first so the atomic pass has a handful of entries instead of 9429. MEASURED, and
; it does not work -- the file that is locked is exactly the file we also cannot
; delete, so it stays as a leftover and vetoes the atomic pass just the same:
;   A 1.0.92 -> 1.0.93 (no C10): old-uninstall-failed=1
;   B 1.0.92 -> 1.0.94 (C10)   : preclean 9429 -> leftovers=2, old-uninstall-failed=1
; Shrinking the candidate set only helps a RANDOM transient lock; it cannot help
; the case the field actually reports, where one file is held across the upgrade.
;
; So stop negotiating with that step and take it over. installUtil.nsh's
; uninstallOldVersion returns EARLY, with $R0 = 0 and no errors, when the old
; UninstallString is not in the registry:
;   ${if} $uninstallString == "" ... ClearErrors / Return
; (handleUninstallResult then treats it as success -- no loop, no dialog, and the
; ops path already relies on this: the 173 robocopy-bypass machines have no
; uninstaller and upgrade by plain overwrite, see chatx-desktop-install.mdc.)
; Removing the uninstall entry is also EXACTLY what a successful old uninstall
; would have done (uninstaller.nsh: DeleteRegKey UNINSTALL_REGISTRY_KEY), and the
; new install rewrites it at the end, so no dangling "Apps & features" row.
;
; Therefore, per root key, in the installer unit, after the family is reaped:
;   1. sweep the old install dir NON-atomically (locked files stay as leftovers --
;      harmless, extractAppPackage overwrites that tree next);
;   2. delete the uninstall entry so the template skips running the old
;      uninstaller at all -> no atomicRMDir, no Abort, no retry loop, no dialog,
;      and the outcome no longer depends on whether anything is locked.
; `--updated` is never touched -- we do not run the old uninstaller, so the
; "upgrade never wipes data" question does not arise; %APPDATA% is never looked
; at; nothing is queued for REBOOTOK/RunOnce ($INSTDIR is where the new version
; lands, a queued delete would erase the fresh install).
; Trade-off, stated plainly: an install that fails after this point leaves the old
; program dir empty (old version unusable, user data intact). The template's own
; atomicRMDir success path has the same consequence -- it moves the whole tree
; away -- so this is not a new failure mode, but it IS no longer rollback-able.
!macro cxTakeOverOldUninstall
  DetailPrint "$(cxStPreClean)"
  ; before-state in the same black box the rest of the upgrade writes to
  nsExec::Exec `${CX_PS} "$$l=Join-Path $$env:TEMP 'chatx_install_reap.log'; $$n=@(Get-ChildItem -LiteralPath '$INSTDIR' -Recurse -Force -File -ErrorAction SilentlyContinue).Count; ('[{0}] oldrm start INSTDIR=$INSTDIR files={1}' -f (Get-Date -Format s),$$n) | Add-Content -Path $$l -ErrorAction SilentlyContinue"`
  Pop $R3
  ; Both root keys, because the template itself runs uninstallOldVersion for
  ; SHELL_CONTEXT and again for HKEY_CURRENT_USER when installMode == "all".
  !insertmacro cxOldRemoveForRoot SHELL_CONTEXT
  !insertmacro cxOldRemoveForRoot HKEY_CURRENT_USER
  ; No registry entry (manual/bypass installs, see the 173 notes) but an old tree
  ; sitting in our target dir: sweep it anyway, the template will skip regardless.
  ${If} ${FileExists} "$INSTDIR\${UNINSTALL_FILENAME}"
    !insertmacro cxSweepDirNonAtomic $INSTDIR
  ${EndIf}
  ; after-state: what survived and whether it is actually locked. When this net
  ; fails in the field again, THIS is the evidence instead of another round of
  ; guessing -- exactly how C10 was disproved.
  nsExec::Exec `${CX_PS} "$$l=Join-Path $$env:TEMP 'chatx_install_reap.log'; $$r=@(Get-ChildItem -LiteralPath '$INSTDIR' -Recurse -Force -File -ErrorAction SilentlyContinue); $$o=@('[{0}] oldrm done leftovers={1}' -f (Get-Date -Format s),$$r.Count); $$r | Select-Object -First 20 | ForEach-Object { $$s='?'; try { $$h=[IO.File]::Open($$_.FullName,'Open','ReadWrite','None'); $$h.Close(); $$s='free' } catch { $$s='LOCKED' }; $$o+=('  left {0} {1}' -f $$s,$$_.FullName) }; $$o | Add-Content -Path $$l -ErrorAction SilentlyContinue"`
  Pop $R3
!macroend

; Per root key: sweep the install dir it points at, then drop its uninstall entry.
; $R4 = UninstallString (proof an old entry exists), $R5 = that install dir.
!macro cxOldRemoveForRoot ROOT_KEY
  ReadRegStr $R4 ${ROOT_KEY} "${UNINSTALL_REGISTRY_KEY}" "UninstallString"
  !ifdef UNINSTALL_REGISTRY_KEY_2
    ${If} $R4 == ""
      ReadRegStr $R4 ${ROOT_KEY} "${UNINSTALL_REGISTRY_KEY_2}" "UninstallString"
    ${EndIf}
  !endif
  ${If} $R4 != ""
    ReadRegStr $R5 ${ROOT_KEY} "${INSTALL_REGISTRY_KEY}" "InstallLocation"
    ${If} $R5 == ""
      StrCpy $R5 $INSTDIR
    ${EndIf}
    ; A directory only gets swept if it PROVES it is one of ours by holding our
    ; uninstaller. A corrupt/foreign InstallLocation (think "C:\") must never
    ; reach RMDir /r -- this predicate is the entire safety story of this macro.
    ${If} ${FileExists} "$R5\${UNINSTALL_FILENAME}"
      !insertmacro cxLogLine "[oldrm] ${ROOT_KEY}: sweeping $R5"
      !insertmacro cxSweepDirNonAtomic $R5
      ; $R3 = survivors. Dropping the uninstall entry for a tree we could NOT empty
      ; and are NOT about to install over would orphan it with no way to uninstall,
      ; so in that one case leave the registry alone and let the template try its
      ; own way (status quo: C9 recovery + overwrite).
      ${If} $R3 == 0
      ${OrIf} $R5 == $INSTDIR
        DeleteRegKey ${ROOT_KEY} "${UNINSTALL_REGISTRY_KEY}"
        !ifdef UNINSTALL_REGISTRY_KEY_2
          DeleteRegKey ${ROOT_KEY} "${UNINSTALL_REGISTRY_KEY_2}"
        !endif
        !insertmacro cxLogLine "[oldrm] ${ROOT_KEY}: uninstall entry removed, template will skip uninstallOldVersion"
      ${Else}
        !insertmacro cxLogLine "[oldrm] ${ROOT_KEY}: $R3 left in $R5 which is not our target dir, registry kept"
      ${EndIf}
    ${EndIf}
  ${EndIf}
!macroend

; One non-atomic pass over the top level of TARGET_DIR: directories via RMDir /r,
; files via Delete, errors ignored, stragglers left where they are. Sets $R3 =
; number of top-level entries that survived (0 = clean). Enumerating while
; deleting is fine for FindFirst/FindNext -- a vanished entry just fails its own
; Delete. The uninstaller exe is NOT spared: nobody is going to run it.
!macro cxSweepDirNonAtomic TARGET_DIR
  StrCpy $R3 0
  ClearErrors
  FindFirst $R1 $R2 "${TARGET_DIR}\*.*"
  ${IfNot} ${Errors}
    ${Do}
      ${If} $R2 != "."
      ${AndIf} $R2 != ".."
        ${If} ${FileExists} "${TARGET_DIR}\$R2\*.*"
          RMDir /r "${TARGET_DIR}\$R2"
        ${Else}
          Delete "${TARGET_DIR}\$R2"
        ${EndIf}
        ${If} ${FileExists} "${TARGET_DIR}\$R2"
          IntOp $R3 $R3 + 1
        ${EndIf}
      ${EndIf}
      ClearErrors
      FindNext $R1 $R2
    ${LoopUntil} ${Errors}
    FindClose $R1
  ${EndIf}
  ClearErrors
  !insertmacro cxLogLine "[oldrm] swept ${TARGET_DIR}, top-level entries left: $R3"
!macroend

; ---- old-version uninstall: recover instead of dying (2026-09-20) ------------
; handleUninstallResult's stock tail is `MessageBox (no /SD) + SetErrorLevel 2 +
; Quit`. Two separate failures come out of that:
;   interactive -- the user is told the upgrade failed and is left to retry by
;     hand (the 「Failed to uninstall old application files ... : 2」 screenshot);
;   silent (/S) -- a MessageBox WITHOUT /SD blocks forever, so an unattended
;     install just hangs. Measured 2026-09-20: 18 minutes on an invisible modal.
; The old uninstaller only reaches a non-zero exit code because un.atomicRMDir
; could not rename a file that is still in use, so: sweep the family again (it
; actually works now), give it one more round, and if it STILL will not go,
; carry on with an in-place overwrite -- extractUsing7za overwrites the tree
; anyway, and a refreshed-in-place install beats a dead upgrade.
; ---- C9: one busy file must not kill the upgrade (2026-09-20, round 3) --------
; This is the stage the field screenshots actually show. Chain, measured on 117:
;   installUtil.nsh::uninstallOldVersion runs the OLD uninstaller as
;   `/S /KEEP_APP_DATA /currentuser --updated _?=<dir>`, and retries it FIVE times
;   before giving up with MessageBox appCannotBeClosed (MB_RETRYCANCEL) -- the
;   「智聊 无法关闭。请手动关闭它，然后单击重试以继续。」 dialog, appearing mid-progress,
;   which is exactly where the screenshots show it (the app-running check runs
;   much earlier, and 1.0.93 owns that step anyway). Clicking 重试 = one more
;   uninstall attempt, which is why 「再点一次就装完了」 works.
;   `--updated` makes uninstaller.nsh take the un.atomicRMDir path: rename EVERY
;   file in $INSTDIR into $PLUGINSDIR, and if even ONE rename fails, restore the
;   whole tree and `Abort` -> uninstaller exit code 2 -> that is also the source
;   of 「Failed to uninstall old application files ... : 2」.
; So a single transiently-held file out of the 9429 we ship fails the entire
; upgrade. It does not take a bug to hold one: an antivirus mid-scan opens files
; without FILE_SHARE_DELETE, which blocks Rename. Reproduced 2026-09-20 in an
; isolated test user -- 5 aborts over 132s with NO process of ours alive and
; nothing locked among the files we probe; the same uninstaller run without
; `--updated` (= no atomic path) exits 0 and clears 9428/9429 files.
; Defining customRemoveFiles replaces that abort-or-nothing block. We keep the
; atomic fast path (it IS the clean way when it works), but a busy file now costs
; a retry and a log line instead of the upgrade.
!macro cxLogLine TEXT
  ; NSIS-side logging on purpose: the uninstaller must not depend on PowerShell,
  ; and this lands in the same %TEMP%\chatx_install_reap.log timeline the
  ; installer writes, so one file tells the whole upgrade story.
  ; $R9/$R8 are free this early in un.install (only setLinkVars ran, named vars).
  ClearErrors
  FileOpen $R9 "$TEMP\chatx_install_reap.log" a
  ${IfNot} ${Errors}
    FileSeek $R9 0 END
    FileWrite $R9 "${TEXT}$\r$\n"
    FileClose $R9
  ${EndIf}
!macroend

!macro customRemoveFiles
  ; Silent = the upgrade path and ops pushes, and uninstaller.nsh only calls
  ; un.checkAppRunning when NOT silent -- so in exactly the case that matters
  ; nothing has closed the app yet unless the installer did it for us. Cheap when
  ; there is nothing to close (one WMI query), decisive when there is.
  ${If} ${Silent}
    !insertmacro cxCloseAppNicely 15
    Pop $R8
    !insertmacro cxReapFamily 20
    Pop $R8
  ${EndIf}

  ${If} ${isUpdated}
    CreateDirectory "$PLUGINSDIR\old-install"
    Push ""
    Call un.atomicRMDir
    Pop $R0
    ${If} $R0 != 0
      ; atomicRMDir hands back the exact file it could not rename -- the single
      ; most useful line in a field report, and until now nobody ever saw it.
      DetailPrint "$(cxStUnBusyFile)"
      !insertmacro cxLogLine "[uninst] busy file blocked atomic removal: $R0"
      Sleep 2000
      Push ""
      Call un.atomicRMDir
      Pop $R0
      ${If} $R0 != 0
        !insertmacro cxLogLine "[uninst] still busy after retry: $R0 -- falling back to non-atomic removal"
      ${EndIf}
    ${EndIf}
    ; deliberately NOT calling un.restoreFiles: whatever was already moved into
    ; $PLUGINSDIR is exactly what we wanted gone, and it dies with $PLUGINSDIR.
  ${EndIf}

  ; The template's own last resort, reached without Abort: RMDir /r ignores
  ; errors, so a held file becomes one stale file instead of a failed upgrade,
  ; and uninstallOldVersion returns 0 -> no retry loop, no dialog.
  ; NEVER schedule leftovers for deletion at next reboot/logon here (unlike
  ; cxRmDirRetry for data dirs): $INSTDIR is where the NEW version is about to
  ; be installed, and a queued `rd /s /q` would erase the fresh install.
  RMDir /r $INSTDIR
!macroend

!macro cxUnInstallCheckBody ROOT_KEY
  IfErrors 0 +3
  DetailPrint `Uninstall was not successful. Not able to launch uninstaller!`
  Return

  ${If} $R0 != 0
    DetailPrint "$(cxStUnRetry)"
    !insertmacro cxReapFamily 20
    Pop $0
    Sleep 1500
    ; forward Call: Function uninstallOldVersion is defined later in installUtil.nsh
    Push "${ROOT_KEY}"
    Call uninstallOldVersion
    ${If} $R0 != 0
      DetailPrint "$(cxStUnBusy)"
      !insertmacro cxLogState "old-uninstall-failed"
      MessageBox MB_OK|MB_ICONEXCLAMATION "$(cxUnOldBusy)" /SD IDOK
    ${EndIf}
  ${EndIf}
!macroend
!macro customUnInstallCheck
  !insertmacro cxUnInstallCheckBody SHELL_CONTEXT
!macroend
!macro customUnInstallCheckCurrentUser
  !insertmacro cxUnInstallCheckBody HKEY_CURRENT_USER
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
  LangString cxFinText       ${LANG_ENGLISH} "You can launch ChatX now.$\r$\n$\r$\nFirst run: follow the setup guide, add an account and log in by QR code. No API key is needed - AI runs through our secure gateway."
  LangString cxFinRun        ${LANG_ENGLISH} "&Launch ChatX now"
  LangString cxFinLink       ${LANG_ENGLISH} "Guides and support: bd2026.cc/download/chatx"
  LangString cxUnWelTitle    ${LANG_ENGLISH} "Uninstall ChatX"
  LangString cxUnWelText     ${LANG_ENGLISH} "This wizard removes the ChatX program from this computer.$\r$\n$\r$\n-  If ChatX is still running you will be asked to close it$\r$\n-  Next page: keep or erase your local data (kept by default)$\r$\n$\r$\nClick Next to continue."
  LangString cxUnFinTitle    ${LANG_ENGLISH} "ChatX has been uninstalled"
  LangString cxUnFinText     ${LANG_ENGLISH} "The program has been removed from this computer.$\r$\n$\r$\nIf you chose to keep your data, account logins, chat history and settings come back automatically after a reinstall."
  LangString cxUnFinLink     ${LANG_ENGLISH} "Download again or contact support: bd2026.cc"
  LangString cxStInstalling  ${LANG_ENGLISH} "Installing ChatX components (about 1.3 GB) - this usually takes 1-3 minutes, please keep this window open..."
  LangString cxStClose       ${LANG_ENGLISH} "Closing ChatX and waiting for it to shut down..."
  LangString cxStReap        ${LANG_ENGLISH} "Stopping leftover ChatX background services..."
  LangString cxStReapLeft    ${LANG_ENGLISH} "A background service would not stop; installing over it..."
  LangString cxAppBusy       ${LANG_ENGLISH} "Some ChatX background processes are still running and could not be closed automatically (usually because ChatX was started as administrator, or antivirus is holding a file).$\r$\n$\r$\nSetup will carry on and install over them. Your data and account logins are untouched. If ChatX misbehaves afterwards, restart the computer and run this installer once more.$\r$\n$\r$\nDetails were written to %TEMP%\chatx_install_reap.log"
  LangString cxStUnBusyFile  ${LANG_ENGLISH} "A file of the previous version is busy - retrying, then removing it the direct way..."
  LangString cxStUnRetry     ${LANG_ENGLISH} "Previous version files are still in use - clearing them and retrying..."
  LangString cxStPreClean    ${LANG_ENGLISH} "Clearing the previous version's files..."
  LangString cxStUnBusy      ${LANG_ENGLISH} "Previous version could not be removed cleanly; installing over it."
  LangString cxUnOldBusy     ${LANG_ENGLISH} "Some files of the previous version are still in use, so they could not be removed first. Setup will install over them - this is safe, and your data is untouched. If ChatX misbehaves afterwards, restart the computer and run this installer once more."
  LangString cxStWipe        ${LANG_ENGLISH} "Erasing user data..."
  LangString cxStWipeLeft    ${LANG_ENGLISH} "Some data is still in use and has been scheduled for removal:"
  LangString cxStWipeDone    ${LANG_ENGLISH} "User data erased."
  LangString cxBranding      ${LANG_ENGLISH} "BOUNDLESS Technology  ·  ChatX ${VERSION}"
  LangString cxCaption       ${LANG_ENGLISH} "ChatX Setup"
  LangString cxUnCaption     ${LANG_ENGLISH} "ChatX Uninstall"
  LangString cxFlavorInternal ${LANG_ENGLISH} "INTERNAL BUILD"
  ; finish-page variants for the wipe outcome (round 2: result on the page, not in a modal)
  LangString cxUnFinTitleWiped ${LANG_ENGLISH} "ChatX and all local data removed"
  LangString cxUnFinWipedTail  ${LANG_ENGLISH} "Thank you for using ChatX."
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
  LangString cxFinText       ${LANG_SIMPCHINESE} "现在可以启动智聊了。$\r$\n$\r$\n第一次使用：按首启向导添加账号、用手机扫码登录即可，无需填写任何 API Key——AI 能力经官网安全通道自动可用。"
  LangString cxFinRun        ${LANG_SIMPCHINESE} "立即启动智聊(&R)"
  LangString cxFinLink       ${LANG_SIMPCHINESE} "使用指南与客服：bd2026.cc/download/chatx"
  LangString cxUnWelTitle    ${LANG_SIMPCHINESE} "卸载 智聊"
  LangString cxUnWelText     ${LANG_SIMPCHINESE} "此向导将从本机移除智聊程序。$\r$\n$\r$\n·  如智聊仍在运行，卸载时会提示您关闭$\r$\n·  下一页可选择保留或删除本机数据（默认保留）$\r$\n$\r$\n单击「下一步」继续。"
  LangString cxUnFinTitle    ${LANG_SIMPCHINESE} "智聊已卸载"
  LangString cxUnFinText     ${LANG_SIMPCHINESE} "程序已从本机移除。$\r$\n$\r$\n若您选择了保留数据，重新安装后账号登录、聊天记录与设置会自动恢复。"
  LangString cxUnFinLink     ${LANG_SIMPCHINESE} "重新下载或联系客服：bd2026.cc"
  LangString cxStInstalling  ${LANG_SIMPCHINESE} "正在安装智聊组件（约 1.3 GB），通常需要 1–3 分钟，请勿关闭此窗口…"
  LangString cxStClose       ${LANG_SIMPCHINESE} "正在关闭智聊并等待其退出…"
  LangString cxStReap        ${LANG_SIMPCHINESE} "正在停止残留的智聊后台服务…"
  LangString cxStReapLeft    ${LANG_SIMPCHINESE} "有后台服务未能结束，将直接覆盖安装…"
  LangString cxAppBusy       ${LANG_SIMPCHINESE} "智聊仍有后台进程在运行，自动关闭未成功（常见原因：智聊是以管理员身份启动的，或杀毒软件正占用文件）。$\r$\n$\r$\n安装将继续，直接覆盖安装——您的数据与各平台登录状态不受影响。若安装后使用异常，请重启电脑再运行一次本安装包。$\r$\n$\r$\n诊断信息已记录在 %TEMP%\chatx_install_reap.log"
  LangString cxStUnBusyFile  ${LANG_SIMPCHINESE} "旧版本有文件被占用，正在重试，随后改用直接删除…"
  LangString cxStUnRetry     ${LANG_SIMPCHINESE} "旧版本文件仍被占用，正在清理并重试…"
  LangString cxStPreClean    ${LANG_SIMPCHINESE} "正在清理旧版本文件…"
  LangString cxStUnBusy      ${LANG_SIMPCHINESE} "旧版本未能完全移除，将直接覆盖安装。"
  LangString cxUnOldBusy     ${LANG_SIMPCHINESE} "旧版本仍有文件被占用，无法先行移除。安装程序将直接覆盖安装——这是安全的，您的数据不受影响。若安装后使用异常，请重启电脑再运行一次本安装包。"
  LangString cxStWipe        ${LANG_SIMPCHINESE} "正在清除用户数据…"
  LangString cxStWipeLeft    ${LANG_SIMPCHINESE} "部分数据仍被占用，已安排稍后自动清除："
  LangString cxStWipeDone    ${LANG_SIMPCHINESE} "用户数据已清除。"
  LangString cxBranding      ${LANG_SIMPCHINESE} "无界科技 BOUNDLESS  ·  智聊 ChatX ${VERSION}"
  LangString cxCaption       ${LANG_SIMPCHINESE} "智聊 ChatX 安装"
  LangString cxUnCaption     ${LANG_SIMPCHINESE} "智聊 ChatX 卸载"
  LangString cxFlavorInternal ${LANG_SIMPCHINESE} "内测版"
  LangString cxUnFinTitleWiped ${LANG_SIMPCHINESE} "智聊及全部本地数据已删除"
  LangString cxUnFinWipedTail  ${LANG_SIMPCHINESE} "感谢您使用智聊。"
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

    ; C4: reap backend/sidecars by PATH and wait for them to actually die; never
    ; touch '*Uninstall*'. Same Process.Path -> Win32_Process.ExecutablePath fix
    ; as cxReapFamily (see the note there): the old predicate matched nothing
    ; from an installer context, so every RMDir below was racing live handles.
    !insertmacro cxReapFamily 20
    Pop $0
    FileWrite $R6 "reap exit=[$0] (0=family clear)$\r$\n"

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
