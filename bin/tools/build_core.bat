@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1

:: ============================================================
:: ETG PluginSystem - universal plugin builder (shared core)
:: Usage:  tools\build_core.bat <project_dir>
::
:: Reads  <project_dir>\versions.txt   (per-fork version list)
:: Uses   PluginSystem\temp\classes\<fork>\<version>\*.jar
::        + PluginSystem\data\android.jar
::        (falls back to <project_dir>\bin\ for standalone use)
::
:: 1. Compiles plugin sources against each (fork, version)
:: 2. Converts lowest-version classes to DEX (d8), base64+zlib
:: 3. Injects _DEX_PAYLOAD into <plugin_id>.plugin & validates AST
:: ============================================================

set "TOOLS_DIR=%~dp0"
set "PROJECT_DIR=%~1"
if "%PROJECT_DIR%"=="" (
    echo ERROR: project directory not specified.
    exit /b 1
)
if not exist "%PROJECT_DIR%" (
    echo ERROR: project directory not found: %PROJECT_DIR%
    exit /b 1
)
if not "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR%\"

:: ---------------- resolve config ----------------
call "%TOOLS_DIR%config.bat"

:: d8.bat needs a modern Java runtime on its JAVA_HOME
if defined EX_JBR set "JAVA_HOME=%EX_JBR%"

:: ---------------- paths ----------------
set "CODE_DIR=%PROJECT_DIR%code"
set "BIN_DIR=%PROJECT_DIR%bin"
set "JAVA_SRC=%CODE_DIR%\java.java"
set "PYTHON_SRC=%CODE_DIR%\python.py"
set "VERSIONS_FILE=%PROJECT_DIR%versions.txt"
set "TEMP_DIR=%PROJECT_DIR%temp_build"

:: Prefer shared PluginSystem root resources if inside PluginSystem\projects\<id>\
set "CLASSES_ROOT=%BIN_DIR%\classes"
set "ANDROID_JAR=%BIN_DIR%\android.jar"
for %%i in ("%PROJECT_DIR%..\..") do set "MAYBE_ROOT=%%~fi"
if exist "%MAYBE_ROOT%\temp\classes" set "CLASSES_ROOT=%MAYBE_ROOT%\temp\classes"
if exist "%MAYBE_ROOT%\data\android.jar" set "ANDROID_JAR=%MAYBE_ROOT%\data\android.jar"

:: ---------------- validate tools ----------------
if not defined EX_JAVAC (
    echo ERROR: javac not found. Install Android Studio or set JAVA_HOME.
    exit /b 1
)
if not defined EX_D8 (
    echo ERROR: d8 not found. Install Android build-tools.
    exit /b 1
)
if not exist "%ANDROID_JAR%" (
    echo ERROR: android.jar not found: %ANDROID_JAR%
    exit /b 1
)

set "JAVA_FOLDER=0"
for /r "%CODE_DIR%\java" %%f in (*.java) do set "JAVA_FOLDER=1"

if not exist "%JAVA_SRC%" if "!JAVA_FOLDER!"=="0" (
    echo ERROR: Java source not found: %JAVA_SRC% ^(or code\java\ folder^)
    exit /b 1
)
if not exist "%PYTHON_SRC%" (
    echo ERROR: Python source not found: %PYTHON_SRC%
    exit /b 1
)
if not exist "%VERSIONS_FILE%" (
    echo ERROR: versions.txt not found in project. Re-run create.bat.
    exit /b 1
)

:: ---------------- collect (fork, version) pairs from versions.txt ----------------
set "TARGET_LIST="
set "CUR_FORK="
for /f "usebackq delims=" %%l in ("%VERSIONS_FILE%") do (
    set "LINE=%%l"
    if not "!LINE!"=="" (
        set "CH=!LINE:~0,1!"
        if "!CH!"==" " (
            for %%v in (!LINE!) do (
                if defined CUR_FORK set "TARGET_LIST=!TARGET_LIST! !CUR_FORK!:%%v"
            )
        ) else (
            for %%f in (!LINE!) do set "CUR_FORK=%%f"
        )
    )
)
if not defined TARGET_LIST (
    echo ERROR: versions.txt is empty. Re-run create.bat.
    exit /b 1
)

echo.
echo ================================================
echo   ETG PluginSystem Plugin Builder
echo ================================================
echo   Project : %PROJECT_DIR%
echo   Classes : %CLASSES_ROOT%
echo   Targets :%TARGET_LIST%
echo.

:: ---------------- extract plugin id from python (via AST) ----------------
set "PS_PY=%PYTHON_SRC%"
for /f "usebackq delims=" %%i in (`python -c "import ast,os;t=ast.parse(open(os.environ['PS_PY'],encoding='utf-8').read());v=[ast.literal_eval(n.value) for n in t.body if isinstance(n,ast.Assign) and any(isinstance(x,ast.Name) and x.id=='__id__' for x in n.targets)];print(v[0] if v else '')" 2^>nul`) do set "PLUGIN_ID=%%i"
if "%PLUGIN_ID%"=="" (
    echo ERROR: Could not extract __id__ from %PYTHON_SRC%
    exit /b 1
)
echo   Plugin ID: %PLUGIN_ID%
echo.

:: ---------------- analyze java sources ----------------
set "PACKAGE="
if "!JAVA_FOLDER!"=="1" (
    for /r "%CODE_DIR%\java" %%f in (*.java) do (
        if not defined PACKAGE (
            for /f "tokens=2 delims=; " %%p in ('findstr /r /c:"^package " "%%f" 2^>nul') do set "PACKAGE=%%p"
        )
    )
) else (
    for /f "tokens=2 delims=; " %%p in ('findstr /r /c:"^package " "%JAVA_SRC%" 2^>nul') do set "PACKAGE=%%p"
)
if "%PACKAGE%"=="" (
    echo ERROR: Could not find package declaration in the java sources
    exit /b 1
)

set "CLASS_NAME="
set "PS_CODE=%CODE_DIR%"
for /f "usebackq delims=" %%c in (`python -c "import os,re;cd=os.environ['PS_CODE'];p=os.path.join(cd,'java','Main.java');p=p if os.path.exists(p) else os.path.join(cd,'java.java');txt=open(p,encoding='utf-8',errors='ignore').read() if os.path.exists(p) else '';m=re.search(r'^\s*public\s+(?:final\s+|abstract\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)',txt,re.M);print(m.group(1) if m else 'Main')" 2^>nul`) do set "CLASS_NAME=%%c"
if not defined CLASS_NAME set "CLASS_NAME=Main"

set "PACKAGE_DIR=%PACKAGE:.=\%"
echo   Package : %PACKAGE%  (class %CLASS_NAME%)
echo.

:: ---------------- prepare build ----------------
if exist "%TEMP_DIR%" rmdir /s /q "%TEMP_DIR%"
mkdir "%TEMP_DIR%"
mkdir "%TEMP_DIR%\src" 2>nul
if "!JAVA_FOLDER!"=="1" (
    xcopy /e /y /q /i "%CODE_DIR%\java" "%TEMP_DIR%\src" >nul
) else (
    mkdir "%TEMP_DIR%\src\%PACKAGE_DIR%" 2>nul
    copy /y "%JAVA_SRC%" "%TEMP_DIR%\src\%PACKAGE_DIR%\%CLASS_NAME%.java" >nul
)

set "SRC_ARG="
for /r "%TEMP_DIR%\src" %%f in (*.java) do set "SRC_ARG=!SRC_ARG! "%%f""
if not defined SRC_ARG (
    echo ERROR: no java sources found in the project
    rmdir /s /q "%TEMP_DIR%" 2>nul
    exit /b 1
)

:: ============================================================
:: STEP 1: per-(fork, version) compatibility check
:: ============================================================
echo [1/5] Compatibility check (compiling against each target)...
echo.

set "COMPAT_OK=1"
set "DEX_SRC="

for %%t in (%TARGET_LIST%) do (
    for /f "tokens=1,2 delims=:" %%a in ("%%t") do call :check_target %%a %%b
)

echo.

if not "%COMPAT_OK%"=="1" (
    echo ================================================
    echo   BUILD FAILED: not compatible with all targets!
    echo ================================================
    for %%t in (%TARGET_LIST%) do (
        for /f "tokens=1,2 delims=:" %%a in ("%%t") do (
            if exist "%TEMP_DIR%\javac_err_%%a_%%b.txt" (
                for %%s in ("%TEMP_DIR%\javac_err_%%a_%%b.txt") do set "ERR_SIZE=%%~zs"
                if not "!ERR_SIZE!"=="0" (
                    echo.
                    echo   Errors for %%a %%b:
                    for /f "delims=" %%e in ('type "%TEMP_DIR%\javac_err_%%a_%%b.txt"') do echo     %%e
                )
            )
        )
    )
    rmdir /s /q "%TEMP_DIR%" 2>nul
    exit /b 1
)

:: ============================================================
:: STEP 2: convert compiled classes (lowest target) to DEX
:: ============================================================
echo [2/5] Converting to DEX (from %DEX_SRC_LABEL%)...

set "JAR_EXE=%EX_JBR%\bin\jar.exe"
if not exist "%JAR_EXE%" set "JAR_EXE=jar"
"%JAR_EXE%" cf "%TEMP_DIR%\classes.jar" -C "%DEX_SRC%" .
call "%EX_D8%" --lib "%ANDROID_JAR%" --output "%TEMP_DIR%" "%TEMP_DIR%\classes.jar"
if errorlevel 1 (
    echo ERROR: d8 conversion failed
    rmdir /s /q "%TEMP_DIR%" 2>nul
    exit /b 1
)
if not exist "%TEMP_DIR%\classes.dex" (
    echo ERROR: classes.dex was not created
    rmdir /s /q "%TEMP_DIR%" 2>nul
    exit /b 1
)
echo        DEX created successfully

:: ============================================================
:: STEP 3: encode DEX to base64+zlib Python variable
:: ============================================================
echo [3/5] Encoding DEX to base64...
set "PS_TEMP=%TEMP_DIR%"
set "PS_OUT=%PROJECT_DIR%%PLUGIN_ID%.plugin"
python -c "import base64,zlib,os;td=os.environ['PS_TEMP'];dex=open(os.path.join(td,'classes.dex'),'rb').read();enc=base64.b64encode(zlib.compress(dex,9)).decode();lines=['    \"'+enc[i:i+72]+'\"' for i in range(0,len(enc),72)];body='_DEX_PAYLOAD = (\n'+'\n'.join(lines)+'\n)' if lines else '_DEX_PAYLOAD = \"\"';open(os.path.join(td,'dex_payload.txt'),'w',encoding='utf-8').write(body)"
if errorlevel 1 (
    echo ERROR: base64 encoding failed - is python installed?
    rmdir /s /q "%TEMP_DIR%" 2>nul
    exit /b 1
)
echo        Encoding successful

:: ============================================================
:: STEP 4: assemble & validate plugin file
:: ============================================================
echo [4/5] Assembling and validating plugin file...
python -c "import ast,os;td=os.environ['PS_TEMP'];py=open(os.environ['PS_PY'],'r',encoding='utf-8').read();b='# __DEX_BEGIN__';e='# __DEX_END__';assert b in py and e in py,'Missing DEX markers in python.py';before,rest=py.split(b,1);_,after=rest.split(e,1);payload=open(os.path.join(td,'dex_payload.txt'),'r',encoding='utf-8').read();out_text=before+b+'\n'+payload+'\n'+e+after;ast.parse(out_text);open(os.environ['PS_OUT'],'w',encoding='utf-8').write(out_text)"
if errorlevel 1 (
    echo ERROR: plugin assembly or AST validation failed
    rmdir /s /q "%TEMP_DIR%" 2>nul
    exit /b 1
)
echo        Assembly and AST check passed

:: ============================================================
:: STEP 5: cleanup
:: ============================================================
echo [5/5] Cleaning up...
rmdir /s /q "%TEMP_DIR%" 2>nul

echo.
echo ================================================
echo   BUILD SUCCESS: %PLUGIN_ID%.plugin
echo ================================================
echo.
for %%f in ("%PROJECT_DIR%%PLUGIN_ID%.plugin") do (
    echo   Size: %%~zf bytes
    echo   Path: %%~ff
)
echo.

endlocal
exit /b 0

:: ============================================================
:: :check_target <fork> <version>
:: Compiles plugin source against one fork+version's classes.
:: ============================================================
:check_target
set "FORK=%~1"
set "VER=%~2"
set "VER_CP=%ANDROID_JAR%"

if exist "%CLASSES_ROOT%\%FORK%\%VER%\" (
    for %%j in ("%CLASSES_ROOT%\%FORK%\%VER%\*.jar") do (
        set "VER_CP=!VER_CP!;%%j"
    )
)

if "!VER_CP!"=="%ANDROID_JAR%" (
    echo   [%FORK% %VER%] SKIP - no classes found in %CLASSES_ROOT%\%FORK%\%VER%
    set "COMPAT_OK=0"
    exit /b 0
)

set "OUT_DIR=%TEMP_DIR%\compile_%FORK%_%VER%"
mkdir "!OUT_DIR!" 2>nul

"%EX_JAVAC%" -source 1.8 -target 1.8 -Xlint:-options -Xlint:deprecation -d "!OUT_DIR!" -cp "!VER_CP!" %SRC_ARG% 2>"%TEMP_DIR%\javac_err_%FORK%_%VER%.txt"
if errorlevel 1 (
    echo   [%FORK% %VER%] FAILED - incompatible or missing classes
    set "COMPAT_OK=0"
) else (
    echo   [%FORK% %VER%] OK
    if not defined DEX_SRC (
        set "DEX_SRC=!OUT_DIR!"
        set "DEX_SRC_LABEL=%FORK% %VER%"
    )
)
exit /b 0
