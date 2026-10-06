Drop the Windows Android SDK platform-tools build here before packaging a
phone-room agent. CI does not download these binaries, and they are gitignored.

Required files, from Google's platform-tools-latest-windows.zip:
  adb.exe
  AdbWinApi.dll
  AdbWinUsbApi.dll

Download on a trusted machine (do not commit the binaries):
  https://dl.google.com/android/repository/platform-tools-latest-windows.zip

Unzip and copy the three files into this directory, then rebuild:

  python fleet_agent/build_agent.py
  powershell -File fleet_agent/build_setup.ps1

The installer copies this folder next to chatx-agent.exe. The agent also
looks in:
  %ProgramData%\ChatX\platform-tools\adb.exe
  %ProgramFiles%\ChatX Agent\platform-tools\adb.exe

Phone-room install (opt in to bringing the bundled adb server up):
  powershell -File Install-ChatXAgent.ps1 -ManageAdbServer -PlatformToolsDir <folder>
  ChatXAgentSetup.exe /VERYSILENT /MANAGEADBSERVER=1

That writes agent.json "adb_manage_server": true. The default is off, so
existing installs are unchanged. Leave the switch off on the live-stream
machine. The agent will not bring an adb server up on a live-stream host
even if the flag is set.
