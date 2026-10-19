@echo off
rem ---------------------------------------------------------------------------
rem  Builds loot-editor.exe, in this folder, from loot_editor.py
rem  (PyInstaller: python -m pip install pyinstaller; Pillow: python -m pip install pillow).
rem
rem  Run again after any change to loot_editor.py, loot_editor_window.py, loot_data.py,
rem  loot_model.py, loot_strings.py, or to the installer modules they use (core.py,
rem  window.py, mpq_archive.py, wow_exe.py).
rem
rem  This file must stay pure ASCII with Windows line endings.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"
python -m PyInstaller --version >/dev/null 2>&1
if errorlevel 1 (
    echo PyInstaller not found: python -m pip install pyinstaller
    exit /b 1
)
python -c "import PIL" >/dev/null 2>&1
if errorlevel 1 (
    echo Pillow not found: python -m pip install pillow
    exit /b 1
)
rem Work folder on the same drive as the sources (PyInstaller requires it).
set WORK=%~dp0build-editor
python -m PyInstaller --noconfirm --onefile --windowed --name loot-editor --distpath . --workpath "%WORK%" --specpath "%WORK%" loot_editor.py
set CODE=%ERRORLEVEL%
rmdir /s /q "%WORK%" 2>nul
rmdir /s /q __pycache__ 2>nul
exit /b %CODE%
