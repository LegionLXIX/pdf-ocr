@echo off
rem Launch PDF OCR without a console window. Pass a PDF path to open it.
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0main.py" %*
