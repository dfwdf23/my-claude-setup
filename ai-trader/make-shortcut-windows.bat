@echo off
chcp 65001 >nul
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\AI 자동매매.lnk'); $s.TargetPath='%~dp0start-windows.bat'; $s.WorkingDirectory='%~dp0'; $s.Save()"
echo 바탕화면에 "AI 자동매매" 바로가기를 만들었습니다.
pause
