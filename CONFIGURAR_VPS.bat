@echo off
setlocal
chcp 65001 >nul
title StoryPulse - Configurar conexion compartida al VPS
cd /d "%~dp0"
echo Esta configuracion se comparte entre CREAR, RENOVAR y ELIMINAR_SESSION.
echo Completa servidor, usuario, huella SSH, ruta del proyecto y nombre del servicio.
echo auth_mode: key para llave PPK, o password para contrasena SSH.
echo remote_dir: carpeta exacta que contiene bot.py en el VPS.
echo service_name: nombre elegido al instalar, incluyendo .service.
echo Las contrasenas vacias se pediran ocultas al usar las utilidades.
echo No subas vps_connection.local.json a GitHub.
echo.
if not exist "%~dp0vps_connection.local.json" (
    if not exist "%~dp0vps_connection.example.json" (
        echo Falta la plantilla vps_connection.example.json.
        pause
        exit /b 1
    )
    copy /b "%~dp0vps_connection.example.json" "%~dp0vps_connection.local.json" >nul
    if errorlevel 1 (
        echo No se pudo crear la configuracion privada.
        pause
        exit /b 1
    )
)
start "" /wait notepad.exe "%~dp0vps_connection.local.json"
exit /b %ERRORLEVEL%
