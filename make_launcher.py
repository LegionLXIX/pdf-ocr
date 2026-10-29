"""Build .venv\\Scripts\\pdfocr.exe: a copy of the venv's pythonw.exe whose icon resources are replaced
with icon.ico, so Explorer and the taskbar show the PDF OCR icon wherever they take it from the exe.
Run after make_icon.py; register.py calls this automatically."""
import ctypes
import shutil
import struct
import sys
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / ".venv" / "Scripts" / "pythonw.exe"
DST = HERE / ".venv" / "Scripts" / "pdfocr.exe"
ICON = HERE / "icon.ico"

RT_ICON, RT_GROUP_ICON = 3, 14
LOAD_LIBRARY_AS_DATAFILE = 0x02

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ENUMNAMEPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMODULE, wintypes.LPVOID, wintypes.LPVOID, wintypes.LPARAM)
ENUMLANGPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMODULE, wintypes.LPVOID, wintypes.LPVOID, wintypes.WORD, wintypes.LPARAM)
k32.LoadLibraryExW.restype = wintypes.HMODULE
k32.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
k32.FreeLibrary.argtypes = [wintypes.HMODULE]
k32.EnumResourceNamesW.restype = wintypes.BOOL
k32.EnumResourceNamesW.argtypes = [wintypes.HMODULE, wintypes.LPVOID, ENUMNAMEPROC, wintypes.LPARAM]
k32.EnumResourceLanguagesW.restype = wintypes.BOOL
k32.EnumResourceLanguagesW.argtypes = [wintypes.HMODULE, wintypes.LPVOID, wintypes.LPVOID, ENUMLANGPROC, wintypes.LPARAM]
k32.BeginUpdateResourceW.restype = wintypes.HANDLE
k32.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
k32.UpdateResourceW.restype = wintypes.BOOL
k32.UpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.LPVOID, wintypes.WORD, wintypes.LPVOID, wintypes.DWORD]
k32.EndUpdateResourceW.restype = wintypes.BOOL
k32.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]


def _icon_resources(path: Path) -> set[tuple[int, int, int]]:
    """(type, id, language) of every RT_ICON / RT_GROUP_ICON resource with an integer id."""
    found: set[tuple[int, int, int]] = set()
    h = k32.LoadLibraryExW(str(path), None, LOAD_LIBRARY_AS_DATAFILE)
    if not h:
        return found
    names: list[tuple[int, int]] = []

    def on_name(hmod, rtype, name, lparam):
        if name and not (name >> 16):   # integer id (IS_INTRESOURCE)
            names.append((lparam, name))
        return True

    def on_lang(hmod, rtype, name, lang, lparam):
        found.add((lparam >> 16, lparam & 0xFFFF, lang))
        return True

    for rtype in (RT_ICON, RT_GROUP_ICON):
        k32.EnumResourceNamesW(h, rtype, ENUMNAMEPROC(on_name), rtype)
    for rtype, rid in names:
        k32.EnumResourceLanguagesW(h, rtype, rid, ENUMLANGPROC(on_lang), (rtype << 16) | rid)
    k32.FreeLibrary(h)
    return found


def build():
    shutil.copy2(SRC, DST)
    data = ICON.read_bytes()
    _reserved, rtype, count = struct.unpack_from("<HHH", data, 0)
    if rtype != 1:
        sys.exit("icon.ico is not an ICO file")
    entries = [struct.unpack_from("<BBBBHHII", data, 6 + i * 16) for i in range(count)]

    existing = _icon_resources(SRC)
    # Write in the language the exe already uses so the new icons replace, rather than sit beside, the old
    langs = [l for t, i, l in existing if t == RT_GROUP_ICON]
    lang = langs[0] if langs else 0x0409

    h = k32.BeginUpdateResourceW(str(DST), False)
    if not h:
        sys.exit(f"BeginUpdateResource failed: {ctypes.get_last_error()}")
    written: set[tuple[int, int, int]] = set()
    grp = struct.pack("<HHH", 0, 1, count)
    for i, (w, hgt, colors, res, planes, bpp, size, off) in enumerate(entries, start=1):
        img = data[off:off + size]
        buf = ctypes.create_string_buffer(img, len(img))
        if not k32.UpdateResourceW(h, RT_ICON, i, lang, buf, len(img)):
            sys.exit(f"UpdateResource RT_ICON {i} failed: {ctypes.get_last_error()}")
        written.add((RT_ICON, i, lang))
        grp += struct.pack("<BBBBHHIH", w, hgt, colors, res, planes, bpp, size, i)
    gbuf = ctypes.create_string_buffer(grp, len(grp))
    if not k32.UpdateResourceW(h, RT_GROUP_ICON, 1, lang, gbuf, len(grp)):
        sys.exit(f"UpdateResource RT_GROUP_ICON failed: {ctypes.get_last_error()}")
    written.add((RT_GROUP_ICON, 1, lang))
    # Remove Python's remaining icon groups/images so ours is the only icon
    for t, i, l in existing - written:
        k32.UpdateResourceW(h, t, i, l, None, 0)
    if not k32.EndUpdateResourceW(h, False):
        sys.exit(f"EndUpdateResource failed: {ctypes.get_last_error()}")
    print(f"wrote {DST}: {count} icon images (lang 0x{lang:04x}), "
          f"removed {len(existing - written)} old icon resources")


if __name__ == "__main__":
    build()
