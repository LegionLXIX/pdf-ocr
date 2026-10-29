// PDF OCR launcher: a tiny native exe so Windows file associations have a real program to run.
// Starts .venv\Scripts\pythonw.exe main.py <args> from the folder this exe lives in.
// Built by make_launcher.py with the C# compiler that ships with .NET Framework.
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;

[assembly: AssemblyTitle("PDF OCR")]
[assembly: AssemblyDescription("PDF viewer with GPU OCR and full-text search")]
[assembly: AssemblyProduct("PDF OCR")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

static class Launcher
{
    [DllImport("shell32.dll")]
    static extern int SetCurrentProcessExplicitAppUserModelID([MarshalAs(UnmanagedType.LPWStr)] string id);

    static int Main(string[] args)
    {
        SetCurrentProcessExplicitAppUserModelID("PDFOCR.App");
        // Find the app folder: the exe's own folder, or any parent of it (so a copy inside .venv\Scripts works too)
        string here = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        string python = null, main = null;
        for (string dir = here; dir != null; dir = Path.GetDirectoryName(dir))
        {
            string py = Path.Combine(dir, ".venv", "Scripts", "pythonw.exe");
            string mp = Path.Combine(dir, "main.py");
            if (File.Exists(py) && File.Exists(mp)) { python = py; main = mp; here = dir; break; }
        }
        if (python == null)
        {
            MessageBox.Show("PDF OCR is not set up in this folder:\n" + here +
                "\n\nExpected .venv\\Scripts\\pythonw.exe and main.py (see README.md).",
                "PDF OCR", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        var sb = new StringBuilder();
        sb.Append('"').Append(main).Append('"');
        foreach (string a in args)
            sb.Append(" \"").Append(a.Replace("\"", "\\\"")).Append('"');
        var psi = new ProcessStartInfo(python, sb.ToString())
        {
            UseShellExecute = false,
            WorkingDirectory = here,
        };
        try
        {
            Process.Start(psi);
        }
        catch (Exception e)
        {
            MessageBox.Show("Could not start PDF OCR:\n" + e.Message, "PDF OCR", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        return 0;
    }
}
