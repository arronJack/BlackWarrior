; BlackWarrior NSIS custom installer logic (electron-builder include)
; Fixes:
;  1. "无法关闭" error during install/upgrade: silently force-close previous
;     instance (tree-kill also takes down the spawned python backend child).
;  2. 360 realtime protection deleting installed files: warn user to add a
;     trust entry BEFORE files hit disk.

!macro customCheckAppRunning
  DetailPrint "正在关闭黑武士残留进程..."
  nsExec::ExecToLog 'taskkill /IM "BlackWarrior.exe" /F /T'
  Pop $0
  Sleep 1200
!macroend

!macro customInit
  ; Detect 360 realtime protection (exit 1 = found, 0 = not found)
  nsExec::ExecToStack 'powershell -NoProfile -Command "if (Get-Process 360tray,360Safe,360rp,360sd -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }"'
  Pop $R0
  Pop $R1
  ${If} $R0 == "1"
    MessageBox MB_OKCANCEL|MB_ICONEXCLAMATION \
      "检测到 360 安全卫士正在运行。$\r$\n$\r$\n未签名的开源程序可能被 360 实时防护误删，导致安装后目录被清空（启动后黑屏无内容）。$\r$\n$\r$\n建议先做一步：$\r$\n  打开 360 → 木马查杀 → 信任区 → 添加目录$\r$\n  将本安装包与安装目录加入信任$\r$\n$\r$\n「确定」= 已加信任，继续安装    「取消」= 退出，先去加信任" \
      IDOK +2
    Quit
  ${EndIf}
!macroend
