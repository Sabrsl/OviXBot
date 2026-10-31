@echo off
rem Compile le plugin (nécessite MSBuild / VS Build Tools et WikiFunctions.dll à la racine du projet AWB)
msbuild src\DeadLinkFix.Awb\DeadLinkFix.Awb.csproj /p:Configuration=Release /restore:false
if errorlevel 1 exit /b 1
echo.
echo DLL : src\DeadLinkFix.Awb\bin\Release\DeadLinkFix.Awb.dll
