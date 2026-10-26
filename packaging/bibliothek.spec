# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller-Bauplan für die Bibliothek.

Eine Analyse, zwei Programme im selben Ordner:
  Bibliothek  App (Server + Fenster), ohne Konsole
  bib         Kommandozeile
macOS bekommt daraus Bibliothek.app (beide Programme in Contents/MacOS/).

Das Embedding-Modell (Qwen3-Embedding-0.6B) wird NICHT gebündelt; es landet beim
ersten semantischen Suchlauf im Hugging-Face-Cache des Benutzers.

Aufruf (aus dem Projektordner):  pyinstaller --noconfirm packaging/bibliothek.spec
"""
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

HERE = Path(SPECPATH)
ROOT = HERE.parent
SRC = ROOT / "src"
ASSETS = HERE / "assets"
VERSION = os.environ.get("BIBLIOTHEK_VERSION", "0.1.0")

datas = []
datas += collect_data_files("bibliothek")          # templates/, static/, KI-ANLEITUNG.md
datas += collect_data_files("immich_book_ocr")     # templates/, static/
datas += collect_data_files("sentence_transformers")
datas += collect_data_files("docx")                # Word-Vorlage für python-docx

# transformers/sentence-transformers prüfen beim Import installierte Versionen
for dist in (
    "transformers", "sentence-transformers", "tokenizers", "huggingface-hub", "safetensors",
    "torch", "tqdm", "regex", "requests", "packaging", "filelock", "numpy", "PyYAML",
    "Pillow", "scipy", "scikit-learn", "pypdfium2",
    "keyring",   # findet seine Backends über Entry-Points in den Metadaten
):
    try:
        datas += copy_metadata(dist)
    except Exception:  # nicht installiert — dann prüft es auch niemand
        pass

hiddenimports = []
hiddenimports += collect_submodules("bibliothek")
hiddenimports += collect_submodules("immich_book_ocr")
hiddenimports += collect_submodules("sentence_transformers")
hiddenimports += collect_submodules("keyring.backends")   # Schlüsselbund: macOS, Windows, Secret Service
hiddenimports += [
    "smoke",
    "striprtf.striprtf",
    "transformers.models.qwen3",
    "transformers.models.qwen3.configuration_qwen3",
    "transformers.models.qwen3.modeling_qwen3",
    "pillow_heif",
]
if sys.platform != "linux":   # natives Fenster; unter Linux öffnet die App den Browser
    hiddenimports += ["webview"]
if sys.platform == "darwin":
    hiddenimports += ["objc", "Foundation", "AppKit", "Vision", "Quartz", "WebKit", "webview.platforms.cocoa"]
if sys.platform == "win32":
    hiddenimports += ["webview.platforms.edgechromium", "webview.platforms.winforms", "clr"]

# Windows: Tesseract liegt nach packaging/build.ps1 in packaging/windows/tesseract/
# und landet im Paket unter _internal/tesseract/ (= sys._MEIPASS/tesseract).
tess = HERE / "windows" / "tesseract"
if sys.platform == "win32" and tess.is_dir():
    datas.append((str(tess), "tesseract"))

a = Analysis(
    [str(HERE / "entry_gui.py"), str(HERE / "entry_cli.py")],
    pathex=[str(SRC), str(HERE)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "IPython", "jupyter_client", "notebook", "pytest", "PyInstaller",
              "tensorboard", "torch.utils.tensorboard"],
    noarchive=False,
)
pyz = PYZ(a.pure)

# Jedes Programm bekommt nur sein eigenes Hauptskript (plus die Laufzeit-Hooks).
gui_scripts = [s for s in a.scripts if s[0] != "entry_cli"]
cli_scripts = [s for s in a.scripts if s[0] != "entry_gui"]
assert len(gui_scripts) == len(cli_scripts) == len(a.scripts) - 1, [s[0] for s in a.scripts]

icon = str(ASSETS / ("icon.icns" if sys.platform == "darwin" else "icon.ico"))

exe_gui = EXE(
    pyz, gui_scripts, [],
    exclude_binaries=True,
    name="Bibliothek",
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon=icon,
    upx=False,
)
exe_cli = EXE(
    pyz, cli_scripts, [],
    exclude_binaries=True,
    name="bib",
    console=True,
    icon=icon,
    upx=False,
)
coll = COLLECT(
    exe_gui, exe_cli,
    a.binaries, a.datas,
    strip=False,
    upx=False,
    name="Bibliothek",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Bibliothek.app",
        icon=icon,
        bundle_identifier="de.tobiasgerlach.bibliothek",
        version=VERSION,
        info_plist={
            "CFBundleName": "Bibliothek",
            "CFBundleDisplayName": "Bibliothek",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "12.0",
            "LSApplicationCategoryType": "public.app-category.education",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            # Das Fenster lädt die Oberfläche vom lokalen Server (http://127.0.0.1)
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
            "NSHumanReadableCopyright": "Tobias Gerlach",
        },
    )
