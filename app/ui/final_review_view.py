# v2/app/ui/final_review_view.py

import tkinter as tk
from tkinter import messagebox
from .score_editor_view import ScoreEditorView
from .. import config
# Import the specific functions needed for re-evaluation
from core.processing.quilgo_parser import ROLE_TO_TEST_MAPPING, SLUG_MAPPING, ROLE_TO_DROPDOWN_OPTION_MAP, ROLE_TO_CATEGORY_MAPPING
from core.processing.candidate_evaluator import _generate_summary_notes
import json
import pandas as pd

class FinalReviewView(tk.Frame):
    """
    The final dashboard page for Quality Assurance.
    """
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller
        self.configure(bg=config.THEME["bg_color"])
        self.results_data = []
        self._create_widgets()

    def _on_mousewheel(self, event, canvas):
        if event.num == 5 or event.delta == -120: canvas.yview_scroll(2, "units")
        elif event.num == 4 or event.delta == 120: canvas.yview_scroll(-2, "units")

    def _create_widgets(self):
        header_frame = tk.Frame(self, bg=config.THEME["bg_color"])
        header_frame.pack(fill="x", pady=10, padx=20)
        tk.Label(header_frame, text="Final Review & QA Dashboard", font=config.TITLE_FONT, bg=config.THEME["bg_color"], fg=config.THEME["title_color"]).pack(side="left")
        self.api_push_button = tk.Button(header_frame, text="🚀 Push All to Manatal API", font=("Helvetica", 12, "bold"), bg=config.THEME["info_color"], fg="white", pady=5, padx=15, command=self._confirm_and_push)
        self.api_push_button.pack(side="right", padx=10)
        tk.Button(header_frame, text="← Back to Control Panel", command=lambda: self.controller.show_frame("ControlPanelView"), bg=config.THEME["secondary_button_bg"], fg=config.THEME["secondary_button_fg"]).pack(side="right")
        canvas_frame = tk.Frame(self, bg=config.THEME["bg_color"])
        canvas_frame.pack(fill="both", expand=True, padx=20, pady=(5, 20))
        self.canvas = tk.Canvas(canvas_frame, bg=config.THEME["bg_color"], highlightthickness=0)
        scrollbar = tk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = tk.Frame(self.canvas, bg=config.THEME["bg_color"])
        self.scrollable_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", lambda e, c=self.canvas: self._on_mousewheel(e, c)))
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<Button-4>",   lambda e, c=self.canvas: self._on_mousewheel(e, c)), add="+")
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<Button-5>",   lambda e, c=self.canvas: self._on_mousewheel(e, c)), add="+")
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<Button-4>"), add="+")
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<Button-5>"), add="+")
        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
    def set_results(self, results):
        self.results_data = results
        for widget in self.scrollable_frame.winfo_children(): widget.destroy()
        if not self.results_data:
            tk.Label(self.scrollable_frame, text="No candidate results to display.", font=("Helvetica", 14), bg=config.THEME["bg_color"]).pack(pady=20)
            return
        for i, candidate in enumerate(self.results_data):
            self._create_candidate_card(self.scrollable_frame, candidate, i)
            
    def _create_candidate_card(self, parent, candidate_data, index):
        final_status, qualified_roles = self._get_final_status(candidate_data)
        status_color = config.THEME["status_qualified"] if final_status == "APPROVED" else config.THEME["status_fail"]
        card = tk.Frame(parent, bg=config.THEME["secondary_button_bg"], bd=1, relief="solid", padx=10, pady=10)
        card.pack(fill="x", pady=5, padx=5)
        header = tk.Frame(card, bg=config.THEME["secondary_button_bg"])
        header.pack(fill="x")
        tk.Label(header, text=candidate_data.get('full_name', 'N/A'), font=("Helvetica", 14, "bold"), bg=config.THEME["secondary_button_bg"], fg=config.THEME["title_color"]).pack(side="left")
        tk.Label(header, text=f" {final_status} ", font=("Helvetica", 10, "bold"), bg=status_color, fg="white").pack(side="left", padx=10)
        tk.Button(header, text="Review / Edit Details", command=lambda idx=index: self._open_editor(idx)).pack(side="right")
        body = tk.Frame(card, bg=config.THEME["secondary_button_bg"])
        body.pack(fill="x", pady=(5,0))
        tk.Label(body, text=candidate_data.get('email', 'N/A'), font=config.BODY_FONT, bg=config.THEME["secondary_button_bg"]).pack(anchor="w")
        roles_text = "Qualified for: " + ", ".join(qualified_roles) if qualified_roles else "Reason: Did not meet minimum requirements for any role."
        tk.Label(body, text=roles_text, font=config.BODY_FONT, bg=config.THEME["secondary_button_bg"], justify="left", wraplength=700).pack(anchor="w", pady=(5,0))

        # Collect all unique tests across roles and display scores inline
        seen = set()
        score_parts = []
        for role_data in candidate_data.get('roles', {}).values():
            for test in role_data.get('tests', []):
                name = test.get('name')
                if name and name not in seen:
                    seen.add(name)
                    score = test.get('score', 'N/A')
                    score_str = f"{score}/10" if isinstance(score, (int, float)) else str(score)
                    score_parts.append(f"{name}: {score_str}")
        if score_parts:
            tk.Label(body, text="Scores: " + "  |  ".join(score_parts),
                     font=config.BODY_FONT, bg=config.THEME["secondary_button_bg"],
                     justify="left", wraplength=900, fg=config.THEME.get("muted_color", "#555555")).pack(anchor="w", pady=(3, 0))

    def _get_final_status(self, candidate_data):
        qualified_roles = {role for role, data in candidate_data['roles'].items() if "QUALIFIED" in data.get('status', 'FAIL')}
        manual_approvals = {d['role'] for d in candidate_data.get('manual_decisions', []) if d['decision'] == 'Approved'}
        all_qualified = sorted(list(qualified_roles.union(manual_approvals)))
        final_status = "APPROVED" if all_qualified else "REJECTED"
        return final_status, all_qualified

    def _open_editor(self, candidate_index):
        editor = ScoreEditorView(self, self.results_data[candidate_index])
        if editor.new_data:
            self.results_data[candidate_index] = editor.new_data
            self._reevaluate_candidate_data(candidate_index)
            self.set_results(self.results_data) # Re-render the entire dashboard
    
    def _reevaluate_candidate_data(self, candidate_index):
        """
        DEFINITIVE FIX: Re-runs the complete evaluation and note generation logic for a single
        candidate after their data has been edited.
        """
        candidate = self.results_data[candidate_index]
        integrity_df = self.controller.frames["ControlPanelView"].task_manager.integrity_df
        
        # --- Step 1: Re-evaluate the status of each role based on edited scores ---
        for role, tests_for_role in ROLE_TO_TEST_MAPPING.items():
            if role not in candidate['roles']: continue

            role_category = ROLE_TO_CATEGORY_MAPPING.get(role, 'tech')
            passing_scores_count = sum(1 for test in candidate['roles'][role].get('tests', []) if test.get('score', 0) >= 7)

            if role_category == 'tech' and passing_scores_count < 2:
                candidate['roles'][role]['status'] = 'FAIL'
                continue
            # Non-tech roles skip the score threshold — fall through to integrity check

            is_flagged_for_review = False
            if not integrity_df.empty:
                for test in candidate['roles'][role].get('tests', []):
                    integrity_issues = integrity_df[(integrity_df['email'] == candidate['email']) & (integrity_df['test_name'] == test['name'])]
                    if not integrity_issues.empty: is_flagged_for_review = True; break

            candidate['roles'][role]['status'] = 'MANUAL REVIEW' if is_flagged_for_review else 'QUALIFIED'

        # --- Step 2: Override statuses based on the (potentially edited) manual decisions ---
        for decision in candidate.get('manual_decisions', []):
            role_name = decision['role']
            final_decision = decision['decision']
            if role_name in candidate['roles']:
                new_status = f"QUALIFIED (Manually {final_decision})" if final_decision == "Approved" else f"FAIL (Manually {final_decision})"
                candidate['roles'][role_name]['status'] = new_status
        
        # --- Step 3: Regenerate summary notes and API payload with the updated data ---
        final_note_md, final_note_html = _generate_summary_notes(candidate, integrity_df)
        candidate['original_row']['summary_note_md'] = final_note_md
        candidate['original_row']['summary_note_html'] = final_note_html
        
        # Re-determine final qualified roles after all edits
        qualified_roles = [role for role, data in candidate['roles'].items() if "QUALIFIED" in data.get('status', 'FAIL')]
        
        scores_payload = {slug: candidate['original_row'].get(test) for test, slug in SLUG_MAPPING.items() if pd.notna(candidate['original_row'].get(test))}
        if qualified_roles:
            scores_payload['techtestspassed'] = [ROLE_TO_DROPDOWN_OPTION_MAP.get(r, r) for r in qualified_roles]
        else:
            scores_payload['techtestspassed'] = ["FAIL: Did not meet minimum requirements"]
        candidate['original_row']['scores_to_update'] = json.dumps(scores_payload)
        
        # Update the main data list with the fully re-evaluated candidate object
        self.results_data[candidate_index] = candidate

    def _confirm_and_push(self):
        """Shows a confirmation dialog and triggers the API push."""
        if messagebox.askyesno("Confirm Live API Push", "You are about to push all results to Manatal.\nThis action is final.\n\nAre you sure you want to proceed?"):
            control_panel = self.controller.frames["ControlPanelView"]
            control_panel.task_manager.final_results = self.results_data
            control_panel.task_manager.start_api_push()
            self.controller.show_frame("ControlPanelView")