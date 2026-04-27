# v2/app/ui/score_editor_view.py

import tkinter as tk
from tkinter import messagebox
from tkinter.scrolledtext import ScrolledText
import copy
from .. import config

class ScoreEditorView(tk.Toplevel):
    """
    A comprehensive modal pop-up for editing scores, manual decisions, and justifications.
    This version has a clean layout and defaults all fields to be editable.
    """
    def __init__(self, parent, candidate_data):
        super().__init__(parent)
        
        self.candidate_data = copy.deepcopy(candidate_data)
        self.new_data = None

        self.title(f"Editing Details for {candidate_data.get('full_name', 'N/A')}")
        self.geometry("600x700")
        self.configure(bg=config.THEME["bg_color"])
        self.transient(parent)
        self.grab_set()
        
        self.score_entries = {}
        self.decision_widgets = {}
        self._create_widgets()
        
        self.wait_window()

    def _toggle_justification_edit(self, text_widget, button_widget):
        """Toggles the state of a justification text widget and updates the button text."""
        # This function is no longer used but is kept for reference.
        # All justification fields are now editable by default.
        pass

    def _on_mousewheel(self, event, canvas):
        """Handles mouse wheel scrolling for the canvas."""
        if event.num == 5 or event.delta == -120: canvas.yview_scroll(1, "units")
        elif event.num == 4 or event.delta == 120: canvas.yview_scroll(-1, "units")

    def _create_widgets(self):
        """Creates and lays out all widgets in the pop-up window."""
        tk.Label(self, text="Review / Edit Details", font=config.TITLE_FONT, 
                 bg=config.THEME["bg_color"], fg=config.THEME["title_color"]).pack(pady=10)
        
        canvas_frame = tk.Frame(self, bg=config.THEME["bg_color"])
        canvas_frame.pack(fill="both", expand=True, padx=20, pady=10)
        canvas = tk.Canvas(canvas_frame, bg=config.THEME["bg_color"], highlightthickness=0)
        scrollbar = tk.Scrollbar(canvas_frame, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg=config.THEME["bg_color"], padx=10)
        
        scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", lambda e, c=canvas: self._on_mousewheel(e, c)))
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<Button-4>",   lambda e, c=canvas: self._on_mousewheel(e, c)), add="+")
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<Button-5>",   lambda e, c=canvas: self._on_mousewheel(e, c)), add="+")
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<Button-4>"), add="+")
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<Button-5>"), add="+")

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        scores_frame = tk.LabelFrame(scrollable_frame, text="Test Scores", font=("Helvetica", 11, "bold"),
                                     bg=config.THEME["bg_color"], fg=config.THEME["title_color"], padx=10, pady=10)
        scores_frame.pack(fill="x", pady=(0, 15))
        scores_frame.grid_columnconfigure(1, weight=1)
        row_index = 0
        all_tests = [(role, test) for role, rd in self.candidate_data.get('roles', {}).items() for test in rd.get('tests', [])]
        
        if not all_tests:
            tk.Label(scores_frame, text="No test scores available to edit.", bg=config.THEME["bg_color"]).pack()
        else:
            for role, test in all_tests:
                test_name, score = test.get('name'), test.get('score', 'N/A')
                entry_key = f"{role}-{test_name}"
                if entry_key in self.score_entries: continue
                tk.Label(scores_frame, text=f"{test_name}:", anchor="w", bg=config.THEME["bg_color"]).grid(row=row_index, column=0, sticky="w", pady=2)
                entry_var = tk.StringVar(value=str(score))
                entry = tk.Entry(scores_frame, textvariable=entry_var, width=10)
                entry.grid(row=row_index, column=1, sticky="w", pady=2)
                self.score_entries[entry_key] = {'var': entry_var, 'test_name': test_name, 'role': role}
                row_index += 1

        manual_decisions = self.candidate_data.get('manual_decisions', [])
        if manual_decisions:
            just_frame = tk.LabelFrame(scrollable_frame, text="Manual Review Decisions", font=("Helvetica", 11, "bold"),
                                       bg=config.THEME["bg_color"], fg=config.THEME["title_color"], padx=10, pady=10)
            just_frame.pack(fill="x", expand=True)

            for i, decision in enumerate(manual_decisions):
                role_name, justification, current_decision = decision.get('role', 'N/A'), decision.get('justification', ''), decision.get('decision', 'Approved')
                
                decision_block = tk.Frame(just_frame, bg=config.THEME["bg_color"])
                decision_block.pack(fill="x", pady=5)
                decision_block.columnconfigure(1, weight=1)

                tk.Label(decision_block, text=f"Decision for '{role_name}':", anchor="w", bg=config.THEME["bg_color"]).grid(row=0, column=0, sticky="w", pady=(10, 2))
                decision_var = tk.StringVar(value=current_decision)
                options = ["Approved", "Rejected"]
                dropdown = tk.OptionMenu(decision_block, decision_var, *options)
                dropdown.grid(row=0, column=1, sticky="w")
                
                # --- DEFINITIVE FIX: Place Justification widgets directly below the decision ---
                tk.Label(decision_block, text="Justification:", anchor="w", bg=config.THEME["bg_color"]).grid(row=1, column=0, columnspan=2, sticky="w", pady=(5,0))
                
                text_widget = ScrolledText(decision_block, height=4, wrap=tk.WORD, relief=tk.SOLID, borderwidth=1, state='normal', bg='white')
                text_widget.insert("1.0", justification)
                text_widget.grid(row=2, column=0, columnspan=2, sticky="ew")
                
                self.decision_widgets[i] = {'var': decision_var, 'widget': text_widget}
        
        button_frame = tk.Frame(self, bg=config.THEME["bg_color"])
        button_frame.pack(pady=15)
        tk.Button(button_frame, text="Save Changes", command=self.save, bg=config.THEME["success_color"], fg="white").pack(side="left", padx=10)
        tk.Button(button_frame, text="Cancel", command=self.destroy).pack(side="left", padx=10)

    def save(self):
        """Updates the candidate_data dictionary with new scores, decisions, and justifications."""
        try:
            updated_data = self.candidate_data
            
            for key, data in self.score_entries.items():
                new_score_str = data['var'].get()
                if new_score_str.lower() == 'n/a': continue
                new_score = float(new_score_str)
                for test in updated_data['roles'][data['role']]['tests']:
                    if test['name'] == data['test_name']: test['score'] = new_score; break
            
            for index, data in self.decision_widgets.items():
                new_decision = data['var'].get()
                new_justification = data['widget'].get("1.0", tk.END).strip()
                updated_data['manual_decisions'][index]['decision'] = new_decision
                updated_data['manual_decisions'][index]['justification'] = new_justification

            self.new_data = updated_data
            messagebox.showinfo("Success", "Changes saved. The dashboard will now refresh.", parent=self)
            self.destroy()
        except ValueError:
            messagebox.showerror("Invalid Input", "All scores must be valid numbers.", parent=self)
        except Exception as e:
            messagebox.showerror("Error", f"An unexpected error occurred: {e}", parent=self)