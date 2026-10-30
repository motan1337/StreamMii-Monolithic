@echo off
setlocal EnableDelayedExpansion

set "INSTALL_DIR=%LocalAppData%\StreamMii"
set "VENV_DIR=%INSTALL_DIR%\venv"
set "LAUNCHER=%INSTALL_DIR%\streammii.cmd"
set "REQ=%~dp0requirements.txt"

echo.
echo ============================================================
echo   StreamMii setup  (v4)
echo ============================================================
echo.
echo   Running as the current user no admin needed.
echo   Windows may still show a UAC prompt from winget itself
echo.

where winget >nul 2>&1
if errorlevel 1 goto NO_WINGET

python --version >nul 2>&1
if not errorlevel 1 (
    echo [!] Python already on path and working, skipping install.
) else (
    echo [!] Installing Python 3.12 via winget ^(hash-verified against the
    echo     signed Microsoft manifest^)...
    call :WINGET_INSTALL Python.Python.3.12 Python
)

:: ffmPEG_ME_PLEASE_MASTER_BOOT_RECORD
ffmpeg -version >nul 2>&1
if not errorlevel 1 (
    echo [!] FFmpeg already on path and working, skipping install.
) else (
    echo [!] Installing ffmPEG_ME_PLEASE_MASTER_BOOT_RECORD via winget...
    call :WINGET_INSTALL Gyan.FFmpeg FFmpeg
)

set "PATH=%PATH%;%LocalAppData%\Microsoft\WinGet\Links"
for %%V in (313 312 311) do (
    set "PATH=!PATH!;%LocalAppData%\Programs\Python\Python%%V;%LocalAppData%\Programs\Python\Python%%V\Scripts"
    set "PATH=!PATH!;%ProgramFiles%\Python%%V;%ProgramFiles%\Python%%V\Scripts"
)

set "PY_CMD="
set "PY_ARGS="

python --version >nul 2>&1
if not errorlevel 1 set "PY_CMD=python"
if defined PY_CMD goto GOT_PYTHON

py -3 --version >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=py"
    set "PY_ARGS=-3"
)
if defined PY_CMD goto GOT_PYTHON

for %%V in (313 312 311) do (
    if not defined PY_CMD if exist "%LocalAppData%\Programs\Python\Python%%V\python.exe" set "PY_CMD=%LocalAppData%\Programs\Python\Python%%V\python.exe"
    if not defined PY_CMD if exist "%ProgramFiles%\Python%%V\python.exe" set "PY_CMD=%ProgramFiles%\Python%%V\python.exe"
)
if defined PY_CMD goto GOT_PYTHON

if not defined PY_CMD (
    echo.
    echo [X] Could not find python in this session.
    echo     Close this window, open a NEW terminal, and run this script again
    echo     ^(a fresh terminal picks up the updated PATH^).
    goto FAIL
)

:GOT_PYTHON
echo [!] Using Python: "%PY_CMD%" %PY_ARGS%
"%PY_CMD%" %PY_ARGS% --version
if errorlevel 1 (
    echo [X] Python was found but will not run.
    goto FAIL
)

if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%" >nul 2>&1

if exist "%VENV_DIR%\pyvenv.cfg" (
    echo [!] Reusing existing venv: %VENV_DIR%
) else (
    if exist "%VENV_DIR%" (
        echo [X] %VENV_DIR% exists but is not a venv. Refusing to delete it.
        goto FAIL
    )
    echo [!] Creating virtual environment: %VENV_DIR%
    "%PY_CMD%" %PY_ARGS% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [X] Could not create the virtual environment.
        goto FAIL
    )
)

set "VPY=%VENV_DIR%\Scripts\python.exe"
if not exist "%VPY%" (
    echo [X] venv python missing: %VPY%
    goto FAIL
)

:: dependencies
echo [!] Installing Python packages...
if exist "%REQ%" (
    echo [!] Using hash-verified requirements: %REQ%
    "%VPY%" -m pip install --require-hashes --only-binary=:all: -r "%REQ%"
    if errorlevel 1 (
        echo [X] Hash verified install failed. Refusing to fall back silently.
        goto FAIL
    )
) else (
    echo [~] requirements.txt not found next to this script.
    echo [~] Falling back to pinned but NOT hash verified versions.
    "%VPY%" -m pip install "requests<2.32" "guessit<4" "colorama==0.4.6"
    if errorlevel 1 (
        echo [X] Package install failed.
        goto FAIL
    )
)

:: install the script + a launcher
set "SM="
if exist "%~dp0streammii-monolithic-v4.py" set "SM=%~dp0streammii-monolithic-v4.py"
if not defined SM (
    for /f "delims=" %%F in ('dir /b /a-d "%~dp0streammii-monolithic-v*.py" 2^>nul') do set "SM=%~dp0%%F"
    if defined SM echo [~] v4 not found; falling back to: !SM!
)

if defined SM (
    copy /y "!SM!" "%INSTALL_DIR%\streammii.py" >nul
    > "%LAUNCHER%" echo @echo off
    >>"%LAUNCHER%" echo setlocal
    >>"%LAUNCHER%" echo set "STREAMMII_LAUNCHER=1"
    >>"%LAUNCHER%" echo "%VPY%" "%INSTALL_DIR%\streammii.py" %%*
    >>"%LAUNCHER%" echo endlocal ^& exit /b %%errorlevel%%
    echo [!] Installed launcher: %LAUNCHER%
) else (
    echo [~] No streammii-monolithic-v*.py found next to this installer.
    echo [~] Run it manually with: "%VPY%" path\to\streammii-monolithic-v4.py
)

echo.
echo ================ VERIFICATION ================
echo.
echo Python ^(venv^):
"%VPY%" --version
echo.
echo Pip packages:
for %%P in (requests guessit colorama) do (
    "%VPY%" -m pip show %%P 2>nul | findstr /B /C:"Name" /C:"Version" || echo   [X] %%P - NOT FOUND
)
echo.
echo FFmpeg:
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo   [X] ffmpeg is not on PATH in this session.
    echo       Open a NEW terminal and run: ffmpeg -version
) else (
    ffmpeg -version 2>&1 | findstr /B /C:"ffmpeg version"
    ffmpeg -hide_banner -encoders 2>nul | findstr /C:"libx264" >nul
    if errorlevel 1 (
        echo   [~] WARNING: this ffmpeg has no libx264 encoder.
        echo   [~] StreamMii would fall back to libxvid, which produces MPEG-4
        echo   [~] files that are NOT Wii compliant and get re encoded every run.
    ) else (
        echo   libx264: yes
    )
)

if exist "%LAUNCHER%" (
    echo.
    echo Launcher:
    "%VPY%" "%INSTALL_DIR%\streammii.py" --version
)

echo.
echo ================ ALL DONE! ================
echo.
echo Run it with:
echo   cd /d D:\your\media
echo   "%LAUNCHER%" --dry-run
echo.
echo Tip: make a desktop shortcut to that .cmd file.
echo.
pause
endlocal
exit /b 0


:: subroutine
:WINGET_INSTALL
winget install --id %~1 -e --source winget --accept-package-agreements --accept-source-agreements
set "WGRC=%errorlevel%"
if "%WGRC%"=="0" goto :eof
if "%WGRC%"=="-1978335189" (echo   [i] %~2 is already installed. & goto :eof)
if "%WGRC%"=="-1978335135" (echo   [i] %~2 is already installed. & goto :eof)
if "%WGRC%"=="-1978334963" (echo   [i] %~2 is already installed. & goto :eof)
if "%WGRC%"=="-1978335153" (echo   [i] %~2 is already installed. & goto :eof)
if "%WGRC%"=="-1978334969" echo   [~] %~2 needs a RESTART to finish installing.
if "%WGRC%"=="-1978334965" echo   [~] %~2 triggered a restart.
echo   [~] winget returned %WGRC% for %~2.
echo   [~] Continuing the verification step below decides pass/fail.
goto :eof


:NO_WINGET
echo [X] winget ^(App Installer^) was not found on this system.
echo.
echo     This installer will NOT download and run unverified executables,
echo     so please install the two dependencies manually and rerun:
echo.
echo       1^) Python 3.12 : https://www.python.org/downloads/
echo                        ^(tick "Add python.exe to PATH"^)
echo       2^) FFmpeg      : https://ffmpeg.org/download.html
echo                        ^(use a full build - it must include libx264^)
echo.
echo     Tip: install "App Installer" from the Microsoft Store to get winget,
echo     then rerun this script for the automatic path.
echo.
pause
endlocal
exit /b 1

:FAIL
echo.
echo [X] Setup did not complete. See the messages above.
pause
endlocal
exit /b 1
