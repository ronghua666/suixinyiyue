; Suixin Yiyue - NSIS installer script
; Build variants: makensis /DARCH=x64 /DWIN_VER=win10 build_installers.nsi
; ARCH: x64 | x86
; WIN_VER: win8 | win10 | win11

Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"

!ifndef ARCH
  !define ARCH "x64"
!endif
!ifndef WIN_VER
  !define WIN_VER "win10"
!endif

!define PRODUCT_NAME "Suixin Yiyue"
!define PRODUCT_VERSION "2.0.0"
!define EXE_NAME "SuixinYiyue.exe"

!if ${ARCH} == "x64"
  !define ARCH_NAME "x64"
  !define SOURCE_EXE "SuixinYiyue_x64.exe"
!else
  !define ARCH_NAME "x86"
  !define SOURCE_EXE "SuixinYiyue_x86.exe"
!endif

Name "${PRODUCT_NAME} ${PRODUCT_VERSION} (${ARCH_NAME})"
OutFile "dist\suixinyiyue-v${PRODUCT_VERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\SuixinYiyue"
RequestExecutionLevel user
SetCompressor /SOLID lzma

!define MUI_ABORTWARNING
!define MUI_ICON "app_icon.ico"
!define MUI_UNICON "app_icon.ico"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

Function .onInit
!if ${ARCH} == "x64"
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "This is the x64 installer. Please use the x86 installer on 32-bit Windows."
    Abort
  ${EndIf}
!endif
FunctionEnd

Section "Install"
  SetOutPath "$INSTDIR"

  File "/oname=${EXE_NAME}" "dist\${SOURCE_EXE}"

  SetOutPath "$INSTDIR\frontend-dist"
  File /r "frontend-dist\*.*"

  SetOutPath "$INSTDIR\backend"
  File "backend\grader.py"
  File "backend\question_analyzer.py"
  File "backend\screen_selector.py"
  File "backend\token_pricing.py"

  CreateDirectory "$SMPROGRAMS\${PRODUCT_NAME}"
  CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk" "$INSTDIR\${EXE_NAME}"
  CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\Uninstall ${PRODUCT_NAME}.lnk" "$INSTDIR\uninst.exe"
  CreateShortCut "$DESKTOP\${PRODUCT_NAME}.lnk" "$INSTDIR\${EXE_NAME}"

  WriteUninstaller "$INSTDIR\uninst.exe"

  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" \
    "DisplayName" "${PRODUCT_NAME}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" \
    "UninstallString" "$INSTDIR\uninst.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" \
    "DisplayIcon" "$INSTDIR\${EXE_NAME}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" \
    "DisplayVersion" "${PRODUCT_VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" \
    "Publisher" "Suixin Yiyue"
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" \
    "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" \
    "NoRepair" 1
SectionEnd

Section "Uninstall"
  Delete "$INSTDIR\${EXE_NAME}"
  RMDir /r "$INSTDIR\frontend-dist"
  RMDir /r "$INSTDIR\backend"
  Delete "$INSTDIR\uninst.exe"
  RMDir "$INSTDIR"

  Delete "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk"
  Delete "$SMPROGRAMS\${PRODUCT_NAME}\Uninstall ${PRODUCT_NAME}.lnk"
  RMDir "$SMPROGRAMS\${PRODUCT_NAME}"
  Delete "$DESKTOP\${PRODUCT_NAME}.lnk"

  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}"
SectionEnd
