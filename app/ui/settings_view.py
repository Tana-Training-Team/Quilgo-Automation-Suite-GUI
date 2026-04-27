# v2/app/ui/settings_view.py

import tkinter as tk
from tkinter import messagebox
import configparser
import os
import sys
import subprocess
from .. import config

class SettingsView(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller
        self.configure(bg=config.THEME["bg_color"])

        # --- Check if this is a first-time setup ---
        is_first_time = not os.path.exists(config.CONFIG_FILE)

        # --- Banner Image ---
        if self.controller.banner_image:
            tk.Label(self, image=self.controller.banner_image).place(x=0, y=0, relwidth=1, relheight=1)

        # --- Logo and Title ---
        if self.controller.logo_image:
            tk.Label(self, image=self.controller.logo_image, bg=config.THEME["bg_color"]).pack(pady=(20, 10))

        tk.Label(self, text="Settings & System Setup", font=config.TITLE_FONT,
                 bg=config.THEME["bg_color"], fg=config.THEME["title_color"]).pack(side="top", fill="x", pady=10)

        # --- Form Frame for Credentials ---
        form_frame = tk.Frame(self, bg=config.THEME["bg_color"])
        form_frame.pack(pady=10, padx=50, fill="x")

        self.entries = {}
        fields = ["Quilgo Email", "Quilgo Password", "Manatal API Key"]

        for i, field in enumerate(fields):
            tk.Label(form_frame, text=f"{field}:", anchor="w",
                     bg=config.THEME["bg_color"], fg=config.THEME["text_color"]).grid(row=i, column=0, sticky="ew", pady=5, padx=5)
            entry = tk.Entry(form_frame, show="*" if "Password" in field else "",
                             relief=tk.SOLID, borderwidth=1)
            entry.grid(row=i, column=1, sticky="ew", pady=5, padx=5)
            self.entries[field] = entry
        form_frame.grid_columnconfigure(1, weight=1)

        # --- Button Frame for Actions ---
        button_frame = tk.Frame(self, bg=config.THEME["bg_color"])
        button_frame.pack(pady=20)

        # --- Dynamic button text ---
        save_button_text = "Save and Continue" if is_first_time else "Update Credentials"
        
        save_button = tk.Button(button_frame, text=save_button_text,
                                command=self.save_settings, bg=config.THEME["button_bg"],
                                fg=config.THEME["button_fg"], activebackground=config.THEME["button_hover_bg"],
                                activeforeground=config.THEME["button_fg"], font=config.BODY_FONT)
        save_button.pack(side="left", padx=10)
        
        # --- Conditional "Go to Automation" button ---
        if not is_first_time:
            go_to_automation_button = tk.Button(button_frame, text="Go to Automation Page →",
                                                command=lambda: self.controller.show_frame("ControlPanelView"),
                                                bg=config.THEME["secondary_button_bg"], fg=config.THEME["secondary_button_fg"])
            go_to_automation_button.pack(side="left", padx=10)

        # --- System Setup Section ---
        setup_frame = tk.Frame(self, bg=config.THEME["bg_color"])
        setup_frame.pack(pady=(10, 20), padx=50, fill="x")

        setup_button = tk.Button(setup_frame, text="Run System Setup",
                                 command=self.run_system_setup, bg=config.THEME["info_color"],
                                 fg="white", font=config.BODY_FONT)
        setup_button.pack()
        
        setup_label = tk.Label(setup_frame, text="Run this once on a new computer (or to update packages) to install project dependencies and browser drivers.",
                               bg=config.THEME["bg_color"], fg=config.THEME["text_color"], wraplength=400, justify="center")
        setup_label.pack(pady=(5, 10))

        self.load_settings()

    def run_system_setup(self):
        """Opens a new terminal to run the full, non-interactive setup."""
        messagebox.showinfo("System Setup",
                            "A new terminal window will now open to install project dependencies and browser drivers.\n\nThis may take several minutes. Please wait for the process to complete, then you can close the new window.")
        
        # This chained command first runs 'npm install' and, if successful, runs 'npx playwright install'.
        command = "npm install && npx playwright install"
        
        try:
            if sys.platform == "win32":
                full_command = f'start cmd /k "{command}"'
                subprocess.Popen(full_command, shell=True, cwd=config.PROJECT_ROOT)
            elif sys.platform == "darwin":
                script = f'tell app "Terminal" to do script "cd {config.PROJECT_ROOT} && {command}"'
                subprocess.Popen(['osascript', '-e', script])
            else: # Linux
                full_command = f'gnome-terminal --working-directory={config.PROJECT_ROOT} -- /bin/sh -c "{command}; echo; echo Press Enter to close.; read"'
                subprocess.Popen(full_command, shell=True)
        except Exception as e:
            messagebox.showerror("Setup Error", f"Could not open terminal to run setup.\nPlease run this command manually in the project folder:\n\n{command}\n\nError: {e}")

    def load_settings(self):
        if not os.path.exists(config.CONFIG_FILE): return
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(config.CONFIG_FILE)
        if 'credentials' in parser:
            self.entries["Quilgo Email"].insert(0, parser['credentials'].get('quilgo_email', ''))
            self.entries["Quilgo Password"].insert(0, parser['credentials'].get('quilgo_password', ''))
            self.entries["Manatal API Key"].insert(0, parser['credentials'].get('manatal_api_key', ''))

    def save_settings(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser['credentials'] = {
            'quilgo_email': self.entries["Quilgo Email"].get(),
            'quilgo_password': self.entries["Quilgo Password"].get(),
            'manatal_api_key': self.entries["Manatal API Key"].get()
        }
        try:
            with open(config.CONFIG_FILE, 'w') as configfile:
                parser.write(configfile)
            messagebox.showinfo("Success", "Credentials saved successfully!")
            self.controller.show_frame("ControlPanelView")
        except Exception as e:
            messagebox.showerror("Error", f"Could not save settings file.\n{e}")