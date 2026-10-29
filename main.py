"""Entry point. Run with the venv:  .venv\\Scripts\\pythonw.exe main.py [file.pdf]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pdfocr.app import run

if __name__ == "__main__":
    sys.exit(run())
