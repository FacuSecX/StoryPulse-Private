@echo off
setlocal
title StoryPulse - Renovar una sesion
cd /d "%~dp0"
chcp 65001 >nul
echo Conexion compartida: vps_connection.local.json
echo Para configurarla por primera vez: CONFIGURAR_VPS.bat
echo.

if defined STORYPULSE_PYTHON (
    "%STORYPULSE_PYTHON%" -X utf8 -B "%~dp0renovar_session_vps.py" %*
    goto terminado
)

where python >nul 2>&1
if not errorlevel 1 (
    python -X utf8 -B "%~dp0renovar_session_vps.py" %*
    goto terminado
)

where py >nul 2>&1
if not errorlevel 1 (
    py -3 -X utf8 -B "%~dp0renovar_session_vps.py" %*
    goto terminado
)

echo No se encontro Python. Defini STORYPULSE_PYTHON con la ruta de tu Python.
set "SP_RESULTADO=1"
goto cerrar

:terminado
set "SP_RESULTADO=%ERRORLEVEL%"
:cerrar
echo.
if not "%SP_RESULTADO%"=="0" echo El proceso termino con un error o fue cancelado.
pause
exit /b %SP_RESULTADO%
