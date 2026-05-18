@echo off
rem Launch Aidds from the current folder.
pushd "%~dp0"
if not exist "Aidds.py" (
    echo ERROR: Aidds.py not found in %CD%
    pause
    popd
    exit /b 1
)
where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py Aidds.py %*
) else (
    python Aidds.py %*
)
popd
