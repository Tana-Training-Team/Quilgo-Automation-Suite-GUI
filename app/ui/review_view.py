# v2/app/ui/review_view.py

import tkinter as tk
from tkinter.scrolledtext import ScrolledText
from .. import config

class ReviewView(tk.Toplevel):
    """
    An intelligent, iterative pop-up window for making manual review decisions on a per-role basis.
    Includes "Copy Email" and "Skip All Reviews" functionality.
    """
    def __init__(self, parent, candidate_data, role_under_review, review_num, total_reviews):
        super().__init__(parent)

        self.decision = None
        self.justification = ""
        self.candidate_data = candidate_data
        self.role_under_review = role_under_review
        self.email_to_copy = candidate_data.get('email', '')

        self.title(f"Manual Review for {candidate_data.get('full_name', 'N/A')} (Review {review_num} of {total_reviews})")
        self.geometry("850x800")
        self.configure(bg=config.THEME["bg_color"])
        self.transient(parent)
        self.grab_set()

        self._create_widgets()
        self.wait_window()

    def _copy_to_clipboard(self):
        """Copies the candidate's email to the system clipboard and provides visual feedback."""
        self.clipboard_clear()
        self.clipboard_append(self.email_to_copy)
        # Change button text for feedback, then revert after 2 seconds
        original_text = self.copy_button.cget('text')
        self.copy_button.config(text="Copied!", state="disabled")
        self.after(2000, lambda: self.copy_button.config(text=original_text, state="normal"))

    def _on_mousewheel(self, event, canvas):
        """Cross-platform mouse wheel and trackpad scrolling handler."""
        if event.num == 5 or event.delta == -120: canvas.yview_scroll(1, "units")
        elif event.num == 4 or event.delta == 120: canvas.yview_scroll(-1, "units")

    def _create_widgets(self):
        # --- 1. Header Frame with "Copy Email" button ---
        header_frame = tk.Frame(self, bg=config.THEME["secondary_button_bg"], padx=10, pady=10)
        header_frame.pack(fill="x", side="top")
        
        tk.Label(header_frame, text=self.candidate_data.get('full_name', 'N/A'), font=("Helvetica", 18, "bold"),
                 bg=config.THEME["secondary_button_bg"], fg=config.THEME["title_color"]).pack()
        
        email_frame = tk.Frame(header_frame, bg=config.THEME["secondary_button_bg"])
        email_frame.pack()
        tk.Label(email_frame, text=self.email_to_copy, font=config.BODY_FONT,
                 bg=config.THEME["secondary_button_bg"], fg=config.THEME["text_color"]).pack(side="left")
        
        # --- NEW: "Copy Email" Button ---
        self.copy_button = tk.Button(email_frame, text="📋", command=self._copy_to_clipboard,
                                     font=("Helvetica", 10), relief="flat", bg=config.THEME["secondary_button_bg"])
        self.copy_button.pack(side="left", padx=(5,0))

        # ... (Key Findings and other sections remain the same) ...
        key_findings_frame = tk.Frame(self, bg=config.THEME["bg_color"], padx=20, pady=15)
        key_findings_frame.pack(fill="x", side="top")
        tk.Label(key_findings_frame, text="KEY FINDINGS FOR THIS REVIEW", font=("Helvetica", 12, "bold"),
                 bg=config.THEME["bg_color"], fg=config.THEME["title_color"]).pack(anchor="w")
        role_data = self.candidate_data.get('roles', {}).get(self.role_under_review, {})
        actionable_reasons = [r for r in role_data.get('manual_review_reasons', []) if "integrity" in r]
        tk.Label(key_findings_frame, text=f"Role Under Review: {self.role_under_review}", font=("Helvetica", 10, "bold"),
                 bg=config.THEME["bg_color"], fg=config.THEME["text_color"]).pack(anchor="w", pady=(5,0))
        reasons_text = "\n".join([f"{i}. {reason}" for i, reason in enumerate(actionable_reasons, 1)])
        tk.Label(key_findings_frame, text=f"Actionable Reason(s):\n{reasons_text}", font=config.BODY_FONT,
                 bg=config.THEME["bg_color"], fg=config.THEME["danger_color"], justify="left").pack(anchor="w")

        tk.Label(self, text="FULL CANDIDATE BREAKDOWN", font=("Helvetica", 12, "bold"),
                 bg=config.THEME["bg_color"], fg=config.THEME["title_color"], padx=20).pack(anchor="w", pady=(10,5))
        canvas_frame = tk.Frame(self, bg=config.THEME["bg_color"])
        canvas_frame.pack(fill="both", expand=True, padx=20)
        canvas = tk.Canvas(canvas_frame, bg=config.THEME["bg_color"], highlightthickness=0)
        scrollbar = tk.Scrollbar(canvas_frame, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg=config.THEME["bg_color"])
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
        self._populate_role_cards(scrollable_frame)

        # --- 4. Justification and Action Frame with "Skip All" button ---
        action_frame = tk.Frame(self, bg=config.THEME["bg_color"], padx=20, pady=15)
        action_frame.pack(fill="x", side="bottom")
        tk.Label(action_frame, text="Justification (Required for this role):", bg=config.THEME["bg_color"],
                 fg=config.THEME["title_color"]).pack(anchor="w")
        self.justification_box = ScrolledText(action_frame, height=4, wrap=tk.WORD, relief=tk.SOLID, borderwidth=1)
        self.justification_box.pack(pady=5, fill="x", expand=True)
        self.justification_box.focus_set()

        button_frame = tk.Frame(action_frame, bg=config.THEME["bg_color"])
        button_frame.pack(pady=(10,0))
        tk.Button(button_frame, text="Approve Role", bg=config.THEME["success_color"], fg="white",
                  command=self.approve, font=("Helvetica", 10, "bold"), padx=10, pady=5).pack(side="left", padx=10)
        tk.Button(button_frame, text="Reject Role", bg=config.THEME["danger_color"], fg="white",
                  command=self.reject, font=("Helvetica", 10, "bold"), padx=10, pady=5).pack(side="left", padx=10)
        tk.Button(button_frame, text="Skip Entire Candidate", command=self.skip, padx=10, pady=5).pack(side="left", padx=10)
        
        # --- NEW: "Skip All Reviews" Button ---
        tk.Button(button_frame, text="Skip All Remaining Reviews", command=self.skip_all, 
                  bg="black", fg="white", font=("Helvetica", 10, "bold"), padx=10, pady=5).pack(side="left", padx=20)

    def _populate_role_cards(self, parent_frame):
        for role, data in self.candidate_data.get('roles', {}).items():
            if not data.get('tests'): continue
            status = data.get('status', 'N/A')
            status_color = {"MANUAL REVIEW": config.THEME["status_manual_review"], "FAIL": config.THEME["status_fail"], "QUALIFIED": config.THEME["status_qualified"]}.get(status, "#CCCCCC")
            is_under_review = (role == self.role_under_review)
            card_relief, card_border_width = ("solid", 2) if is_under_review else ("groove", 1)
            card_bg = config.THEME["secondary_button_bg"] if is_under_review else config.THEME["bg_color"]
            card = tk.Frame(parent_frame, bg=card_bg, bd=card_border_width, relief=card_relief, padx=10, pady=10)
            card.pack(fill="x", pady=5)
            header = tk.Frame(card, bg=card_bg)
            header.pack(fill="x")
            tk.Label(header, text=role, font=("Helvetica", 11, "bold"), bg=card_bg, fg=config.THEME["title_color"]).pack(side="left")
            tk.Label(header, text=f" {status} ", font=("Helvetica", 9, "bold"), bg=status_color, fg="white").pack(side="left", padx=10)
            tk.Label(header, text=f"Tests Taken: {len(data.get('tests', []))}", font=("Helvetica", 9), bg=card_bg).pack(side="right")
            for test in data.get('tests', []):
                test_frame = tk.Frame(card, bg=card_bg)
                test_frame.pack(fill="x", padx=15, pady=2)
                test_status = test.get('status', '')
                tk.Label(test_frame, text=f"- {test.get('name')}:", bg=card_bg, anchor="w").grid(row=0, column=0, sticky="w")
                tk.Label(test_frame, text=f"Score {test.get('score', 'N/A')}", bg=card_bg).grid(row=0, column=1, padx=10)
                tk.Label(test_frame, text=f"Status: {test_status}", bg=card_bg, anchor="w").grid(row=0, column=2, sticky="w")
                test_frame.grid_columnconfigure(2, weight=1)

    def _get_justification(self):
        justification = self.justification_box.get("1.0", tk.END).strip()
        if not justification:
            messagebox.showwarning("Justification Required", "Please provide a justification to proceed.", parent=self)
            return None
        return justification

    def approve(self):
        justification = self._get_justification()
        if justification: self.decision, self.justification = "approve", justification; self.destroy()

    def reject(self):
        justification = self._get_justification()
        if justification: self.decision, self.justification = "reject", justification; self.destroy()

    def skip(self):
        self.decision = "skip"; self.destroy()
        
    def skip_all(self):
        """Sets the special 'skip_all' decision and closes the window."""
        self.decision = "skip_all"; self.destroy()