# v2/app/ui/main_app_view.py

import tkinter as tk
import os
from .settings_view import SettingsView
from .control_panel_view import ControlPanelView
from .final_review_view import FinalReviewView # --- NEW: Import the Final QA Page ---
from .. import utils
from .. import config

class MainAppView(tk.Tk):
    """
    The main application window (the root Tk instance).
    This class is responsible for creating and managing the different pages (frames)
    of the application and controlling which one is visible.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.title("Quilgo Automation Suite v2")
        self.geometry("900x700")
        self.configure(bg=config.THEME["bg_color"])

        # --- Load branding assets ---
        # These are loaded once here and passed to the child frames as needed.
        self.logo_image = utils.load_image(config.ASSETS_DIR / "logo.png", (150, 150))
        self.banner_image = utils.load_image(config.ASSETS_DIR / "banner.webp")

        # --- Container for frames ---
        # This single container holds all the different pages of the app.
        # We raise one frame to the top to show it.
        container = tk.Frame(self)
        container.pack(side="top", fill="both", expand=True)
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)

        self.frames = {}

        # --- Initialize all pages ---
        # --- NEW: Add FinalReviewView to the tuple of pages ---
        for F in (SettingsView, ControlPanelView, FinalReviewView):
            page_name = F.__name__
            frame = F(parent=container, controller=self)
            self.frames[page_name] = frame
            # Place all frames in the same grid cell; tkraise() will control visibility.
            frame.grid(row=0, column=0, sticky="nsew")

        # --- Decide which page to show on startup ---
        if os.path.exists(config.CONFIG_FILE):
            self.show_frame("ControlPanelView")
        else:
            self.show_frame("SettingsView")

    def show_frame(self, page_name, data_to_pass=None):
        """
        Raises the specified frame to the top and optionally passes data to it.
        
        Args:
            page_name (str): The class name of the frame to show.
            data_to_pass:   Optional data (e.g., final results) to pass to the
                            target frame's `set_results` method if it exists.
        """
        frame = self.frames[page_name]
        
        # If we are showing the final review page, pass the results to it.
        if page_name == "FinalReviewView" and hasattr(frame, 'set_results'):
            frame.set_results(data_to_pass)
            
        frame.tkraise()