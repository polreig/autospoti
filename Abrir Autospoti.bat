@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo No se encontro Python en PATH.
    echo Instala Python y vuelve a intentarlo.
    pause
    exit /b 1
)

echo Iniciando Autospoti en el navegador...
python -m streamlit run autoplaylist.py

if errorlevel 1 (
    echo.
    echo Autospoti no pudo iniciarse. Comprueba que las dependencias esten instaladas:
    echo python -m pip install -r requirements.txt
    pause
)
