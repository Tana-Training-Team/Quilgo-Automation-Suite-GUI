# v2/main.py

import sys
from tkinter import messagebox
from app.ui.main_app_view import MainAppView

# --- Dependency Check ---
# Ensure Pillow is installed before running the app.
try:
    from PIL import Image, ImageTk
except ImportError:
    messagebox.showerror(
        "Missing Dependency",
        "Pillow library not found. Please run 'pip install -r requirements.txt'."
    )
    sys.exit(1)


if __name__ == "__main__":
    app = MainAppView()
    app.mainloop()