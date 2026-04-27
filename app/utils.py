# v2/app/utils.py

import tkinter as tk
from tkinter import messagebox
from PIL import Image, ImageTk
from . import config

# --- Image Loading Utility ---
def load_image(path, resize=None):
    """Loads an image from the given path and optionally resizes it."""
    try:
        img = Image.open(path)
        if resize:
            img = img.resize(resize, Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(img)
    except FileNotFoundError:
        print(f"Warning: Branding image not found at {path}. The app will run without it.")
        return None
    except Exception as e:
        print(f"Error loading image {path}: {e}")
        return None

# --- Text Redirector for Logging ---
class TextRedirector:
    """A class to redirect stdout to a tkinter Text widget."""
    def __init__(self, widget):
        self.widget = widget

    def write(self, s):
        """Writes a string to the widget."""
        self.widget.log_message(s.strip())

    def flush(self):
        """Flush method is required for compatibility."""
        pass