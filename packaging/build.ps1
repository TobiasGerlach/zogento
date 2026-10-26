# Baut den Windows-Installer: dist\Bibliothek-<version>-windows-setup.exe
#
# Voraussetzungen: uv, Tesseract (UB Mannheim, z. B. `choco install tesseract`),
# Inno Setup 6 (wird bei Bedarf per choco nachinstalliert).
#   $env:SKIP_SYNC = "1"             Umgebung nicht neu synchronisieren
#   $env:BIBLIOTHEK_VERSION = "1.2.3" Version vorgeben (sonst aus pyproject.toml)
#   $env:TESSERACT_DIR = "C:\..."     Tesseract-Ordner, falls nicht am Standardort
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$Root = (Get-Location).Path

if ($env:SKIP_SYNC -ne "1") { uv sync --extra build; if ($LASTEXITCODE) { exit $LASTEXITCODE } }
$Py = Join-Path $Root ".venv\Scripts\python.exe"

if (-not $env:BIBLIOTHEK_VERSION) {
    $env:BIBLIOTHEK_VERSION = & $Py -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])'
}
$Version = $env:BIBLIOTHEK_VERSION
Write-Host "== Bibliothek $Version für Windows =="

& $Py packaging\make_icon.py
if ($LASTEXITCODE) { exit $LASTEXITCODE }

# --- Tesseract ins Paket holen -------------------------------------------------
$TessSrc = if ($env:TESSERACT_DIR) { $env:TESSERACT_DIR } else { Join-Path $env:ProgramFiles "Tesseract-OCR" }
if (-not (Test-Path (Join-Path $TessSrc "tesseract.exe"))) {
    throw "Tesseract nicht gefunden unter $TessSrc (choco install tesseract oder TESSERACT_DIR setzen)."
}
$TessDst = Join-Path $Root "packaging\windows\tesseract"
if (Test-Path $TessDst) { Remove-Item -Recurse -Force $TessDst }
Copy-Item -Recurse $TessSrc $TessDst
# Deinstallationsprogramm und Doku des Tesseract-Installers nicht mitnehmen
Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $TessDst "tesseract-uninstall.exe")
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $TessDst "doc")

# Hochgenaue Modelle (tessdata_best) für Englisch und Deutsch, im Format des
# eingebauten Buch-OCR (Modell + .sha256), damit beim ersten Lauf nichts lädt.
$Best = Join-Path $TessDst "tessdata_best"
New-Item -ItemType Directory -Force $Best | Out-Null
foreach ($code in @("eng", "deu")) {
    $target = Join-Path $Best "$code.traineddata"
    Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/main/$code.traineddata" -OutFile $target
    (Get-FileHash -Algorithm SHA256 $target).Hash.ToLower() + "`n" | Set-Content -NoNewline -Encoding ascii (Join-Path $Best "$code.sha256")
}
$Osd = Join-Path $TessDst "tessdata\osd.traineddata"
if (Test-Path $Osd) { Copy-Item $Osd $Best }

# --- PyInstaller -----------------------------------------------------------------
foreach ($d in @("build\pyinstaller", "dist\Bibliothek")) { if (Test-Path $d) { Remove-Item -Recurse -Force $d } }
& $Py -m PyInstaller --noconfirm --clean --distpath dist --workpath build\pyinstaller packaging\bibliothek.spec
if ($LASTEXITCODE) { exit $LASTEXITCODE }

Write-Host "== Selbsttest =="
$env:BIBLIOTHEK_SMOKE = "1"
& "dist\Bibliothek\bib.exe"
$code = $LASTEXITCODE
Remove-Item Env:\BIBLIOTHEK_SMOKE
if ($code) { exit $code }

# --- Installer -------------------------------------------------------------------
$Iscc = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
if (-not (Test-Path $Iscc)) {
    choco install innosetup -y --no-progress
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
}
& $Iscc "/DMyAppVersion=$Version" packaging\windows\bibliothek.iss
if ($LASTEXITCODE) { exit $LASTEXITCODE }
Get-ChildItem dist\*-setup.exe | ForEach-Object { "{0}  {1:N0} MB" -f $_.Name, ($_.Length / 1MB) }
