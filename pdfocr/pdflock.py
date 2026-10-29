"""One lock around every PyMuPDF call.

PyMuPDF (MuPDF) is not thread-safe: using it from the render threads, the search thread, the OCR
thread and the GUI thread at the same time crashes the process without a Python traceback. Every
piece of code that touches a pymupdf object takes this lock first, so calls are serialised.
"""
import threading

LOCK = threading.RLock()
