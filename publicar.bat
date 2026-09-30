@echo off
REM ==================================================================
REM  publicar.bat - publica migrations.json del fuente al runtime
REM
REM  FUENTE  : C:\Proyectos\migraciones  (repo git - UNICA fuente de verdad)
REM  DESTINO : C:\wsgc                   (junto a gestion.exe, lo lee
REM                                       AplicarMigraciones en funciones_mysql.prg)
REM
REM  CORRER SIEMPRE ANTES DE COMPILAR gestion.exe
REM  Nunca editar C:\wsgc\migrations.json a mano: se pisa desde aca.
REM ==================================================================
setlocal

set ORIGEN=C:\Proyectos\migraciones
set DESTINO=C:\wsgc
set UPD=C:\wsgc\Actualizador\suriupdate

if not exist "%ORIGEN%\migrations.json" (
    echo ERROR: no existe %ORIGEN%\migrations.json
    pause
    exit /b 1
)

echo.
echo Publicando migrations.json
echo   desde : %ORIGEN%
echo   hacia : %DESTINO%
echo.

copy /Y "%ORIGEN%\migrations.json" "%DESTINO%\migrations.json" >nul
if errorlevel 1 (
    echo ERROR: fallo la copia a %DESTINO%
    echo Puede estar abierto gestion.exe o el archivo en solo lectura.
    pause
    exit /b 1
)
for %%F in ("%DESTINO%\migrations.json") do echo   OK  %DESTINO%\migrations.json  (%%~zF bytes, %%~tF)

REM --- copia al repo del actualizador, para que viaje a los clientes ---
if exist "%UPD%" (
    copy /Y "%ORIGEN%\migrations.json" "%UPD%\migrations.json" >nul
    if errorlevel 1 (
        echo   AVISO: no se pudo copiar a %UPD%
    ) else (
        echo   OK  %UPD%\migrations.json
        echo.
        echo   RECORDAR: hacer commit/push en %UPD%
        echo   y verificar que comandos.txt contemple la descarga de migrations.json
    )
) else (
    echo   AVISO: no existe %UPD% - se omite la copia al actualizador
)

echo.
echo Listo. Ahora si, compilar gestion.exe
echo.
pause
