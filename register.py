"""Register (or remove) PDF OCR as an "Open with" application for .pdf in the current user's registry.

    python register.py            # register, using this folder's venv and icon
    python register.py --remove   # undo

No admin rights needed. Windows won't let a program make itself the *default* PDF app; after
registering, pick "PDF OCR" once via right-click > Open with > Always, or Settings > Default apps.
"""
import ctypes
import sys
import winreg
from pathlib import Path

HERE = Path(__file__).resolve().parent
# pdfocr.exe is a copy of the venv's pythonw.exe (made by this script). Windows' "Open with" list keys
# applications by exe name, so a dedicated name gets its own entry with our icon instead of "Python".
PYTHONW = HERE / ".venv" / "Scripts" / "pdfocr.exe"
PYTHONW_SRC = HERE / ".venv" / "Scripts" / "pythonw.exe"
MAIN = HERE / "main.py"
ICON = HERE / "icon.ico"
PROGID = "PDFOCR.Document"
APP_NAME = "PDF OCR"
APP_ID = "PDFOCR.App"   # AppUserModelID, keeps taskbar grouping/icon ours instead of Python's

HKCU = winreg.HKEY_CURRENT_USER


def _set(path: str, name: str | None, value: str):
    with winreg.CreateKey(HKCU, path) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)


def _delete_tree(path: str):
    try:
        with winreg.OpenKey(HKCU, path, 0, winreg.KEY_ALL_ACCESS) as k:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _delete_tree(f"{path}\\{sub}")
        winreg.DeleteKey(HKCU, path)
    except FileNotFoundError:
        pass


def register():
    # (re)build the icon if missing, then the launcher exe with the icon baked in; Explorer takes
    # an application's file-type icon from the exe itself, not from the registry
    if not ICON.exists():
        import make_icon
        make_icon.main()
    import make_launcher
    make_launcher.build()
    for p in (PYTHONW, MAIN, ICON):
        if not p.exists():
            sys.exit(f"missing: {p}")
    cmd = f'"{PYTHONW}" "{MAIN}" "%1"'
    icon = f'"{ICON}",0'   # quoted: the path has spaces and a '+'

    # Application entry: this is what Windows 11's "Open with" / Default apps chooser actually lists
    for exe in (PYTHONW.name, "PDF OCR.bat"):
        app = rf"Software\Classes\Applications\{exe}"
        _set(app, "FriendlyAppName", APP_NAME)
        _set(app, "AppUserModelID", APP_ID)
        _set(app + r"\DefaultIcon", None, icon)
        _set(app + r"\shell\open", None, f"Open with {APP_NAME}")
        _set(app + r"\shell\open", "FriendlyAppName", APP_NAME)
        _set(app + r"\shell\open", "Icon", icon)
        _set(app + r"\shell\open\command", None, cmd)
        _set(app + r"\SupportedTypes", ".pdf", "")
    prog = rf"Software\Classes\{PROGID}"
    _set(prog, None, "PDF Document")
    _set(prog, "FriendlyTypeName", "PDF Document")
    _set(prog, "AppUserModelID", APP_ID)
    _set(prog + r"\DefaultIcon", None, icon)
    _set(prog + r"\shell\open", None, f"Open with {APP_NAME}")
    _set(prog + r"\shell\open", "FriendlyAppName", APP_NAME)
    _set(prog + r"\shell\open", "Icon", icon)
    _set(prog + r"\shell\open\command", None, cmd)
    # offer it in the "Open with" list for .pdf
    _set(r"Software\Classes\.pdf\OpenWithProgids", PROGID, "")
    # and in Settings > Default apps
    caps = rf"Software\{APP_NAME}\Capabilities"
    _set(caps, "ApplicationName", APP_NAME)
    _set(caps, "ApplicationDescription", "PDF viewer with GPU OCR and full-text search")
    _set(caps, "ApplicationIcon", icon)
    _set(caps + r"\FileAssociations", ".pdf", PROGID)
    _set(r"Software\RegisteredApplications", APP_NAME, caps)
    _notify()
    print(f"Registered {APP_NAME}:\n  {cmd}\n  icon {ICON}")
    print("Now right-click a PDF > Open with > PDF OCR (tick Always), or Settings > Default apps > PDF OCR.")


def remove():
    _delete_tree(rf"Software\Classes\{PROGID}")
    _delete_tree(rf"Software\Classes\Applications\{PYTHONW.name}")
    _delete_tree(r"Software\Classes\Applications\PDF OCR.bat")
    try:
        with winreg.OpenKey(HKCU, r"Software\Classes\.pdf\OpenWithProgids", 0, winreg.KEY_ALL_ACCESS) as k:
            winreg.DeleteValue(k, PROGID)
    except FileNotFoundError:
        pass
    _delete_tree(rf"Software\{APP_NAME}")
    try:
        with winreg.OpenKey(HKCU, r"Software\RegisteredApplications", 0, winreg.KEY_ALL_ACCESS) as k:
            winreg.DeleteValue(k, APP_NAME)
    except FileNotFoundError:
        pass
    _notify()
    print(f"Removed {APP_NAME} registration.")


def _notify():
    # SHCNE_ASSOCCHANGED so Explorer picks up the new association without a sign-out,
    # then flush the shell icon cache so a changed pdfocr.ico shows up
    ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
    import subprocess
    try:
        subprocess.run(["ie4uinit.exe", "-show"], timeout=30)
    except Exception:
        pass


if __name__ == "__main__":
    remove() if "--remove" in sys.argv else register()
