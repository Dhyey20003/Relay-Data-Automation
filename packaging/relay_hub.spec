# PyInstaller spec for the Windows build (one-folder, no console window).
# Build with:  scripts\windows\build_exe.bat   (or: pyinstaller packaging\relay_hub.spec)
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
datas = [(str(ROOT / "relay_hub" / "static"), "relay_hub/static")]
binaries = []
hiddenimports = collect_submodules("uvicorn") + collect_submodules("relay_hub")
for pkg in ("pypdfium2", "pypdfium2_raw"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass
datas += collect_data_files("tzdata")
datas += collect_data_files("openpyxl")
if sys.platform == "win32":
    hiddenimports += ["win32com", "win32com.client", "win32com.client.dynamic", "pythoncom", "pywintypes",
                      "win32timezone", "winreg"]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "numpy", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RelayEmployeeHub",
    console=False,
    icon=str(ROOT / "relay_hub" / "static" / "img" / "app.ico"),
    version=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="RelayEmployeeHub")
