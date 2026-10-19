# PyInstaller build for GD-Claude.exe. Run from the gd-claude folder:
#   python app/make_icon.py
#   pyinstaller app/gd-claude.spec

from pathlib import Path

root = Path(SPECPATH).parent

a = Analysis(
    [str(root / "app" / "main.py")],
    pathex=[str(root)],
    datas=[(str(root / "app" / "static"), "app/static")],
    hiddenimports=["app", "app.server", "catalog", "deco", "deco_agent", "gdlevel", "save_file", "vault",
                   "gd_api", "physics", "level_builder", "study", "webview"],
    excludes=["tkinter", "pytest", "playwright"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="GD-Claude",
    icon=str(root / "app" / "icon.ico"),
    console=False,
    upx=False,
)
