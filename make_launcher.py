"""Build pdfocr.exe next to this file: a small native launcher (launcher.cs) compiled with the C#
compiler that ships with .NET Framework, carrying icon.ico and a "PDF OCR" file description.

Windows file associations need a real program: Explorer runs the registered exe as `pdfocr.exe "<file>"`
and ignores any extra command-line arguments in the registry, so a renamed pythonw.exe can't be used.
register.py calls this automatically."""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "launcher.cs"
DST = HERE / "pdfocr.exe"
ICON = HERE / "icon.ico"


def find_csc() -> Path:
    roots = [Path(r"C:\Windows\Microsoft.NET\Framework64"), Path(r"C:\Windows\Microsoft.NET\Framework")]
    for root in roots:
        if root.exists():
            for d in sorted(root.glob("v4*"), reverse=True):
                csc = d / "csc.exe"
                if csc.exists():
                    return csc
    sys.exit("csc.exe (the .NET Framework C# compiler) was not found under C:\\Windows\\Microsoft.NET")


def build():
    if not ICON.exists():
        sys.exit("icon.ico is missing; run make_icon.py first")
    csc = find_csc()
    cmd = [str(csc), "/nologo", "/target:winexe", "/optimize+", f"/out:{DST}",
           f"/win32icon:{ICON}", "/reference:System.Windows.Forms.dll", str(SRC)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"csc failed:\n{r.stdout}\n{r.stderr}")
    print(f"built {DST} ({DST.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    build()
