# v2/app/ui/control_panel_view.py

import tkinter as tk
from tkinter.scrolledtext import ScrolledText
from tkinter import messagebox
import subprocess
import sys
import os
import time
import datetime
import pandas as pd
from tkcalendar import Calendar
from .. import config
from ..automation.task_manager import TaskManager
from ..ui.review_view import ReviewView
from core.processing.quilgo_parser import MASTER_TEST_CONFIG, ROLE_TO_TEST_MAPPING

class ControlPanelView(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller
        self.task_manager = TaskManager(self)
        self.configure(bg=config.THEME["bg_color"])

        self.timer_running = False
        self.start_time = 0

        if self.controller.banner_image: tk.Label(self, image=self.controller.banner_image).place(x=0, y=0, relwidth=1, relheight=1)
        if self.controller.logo_image: tk.Label(self, image=self.controller.logo_image, bg=config.THEME["bg_color"]).pack(pady=(20, 0))
        tk.Label(self, text="Automation Control Panel", font=config.TITLE_FONT, bg=config.THEME["bg_color"], fg=config.THEME["title_color"]).pack(side="top", fill="x", pady=10)

        self.auto_continue_var = tk.BooleanVar(value=True)
        tk.Checkbutton(self, text="Run Part 2 automatically after successful Part 1", variable=self.auto_continue_var, bg=config.THEME["bg_color"], anchor="w").pack(pady=5)

        # ── Filter Panel ──────────────────────────────────────────────────────────
        filter_frame = tk.LabelFrame(self, text="Filters (optional)", font=config.BODY_FONT,
                                     bg=config.THEME["bg_color"], fg=config.THEME["title_color"],
                                     padx=10, pady=8)
        filter_frame.pack(fill="x", padx=20, pady=(0, 5))

        # --- Date range row ---
        date_row = tk.Frame(filter_frame, bg=config.THEME["bg_color"])
        date_row.pack(fill="x", pady=(0, 6))

        # Start date
        tk.Label(date_row, text="Start date:", bg=config.THEME["bg_color"],
                 font=config.BODY_FONT).pack(side="left")
        self.start_date_var = tk.StringVar(value="")
        self.start_date_btn = tk.Button(
            date_row, textvariable=self.start_date_var,
            font=config.BODY_FONT, width=12,
            bg="white", fg=config.THEME["text_color"],
            relief="solid", bd=1,
            command=lambda: self._open_calendar("start")
        )
        self.start_date_btn.pack(side="left", padx=(4, 2))
        self.start_date_var.set("📅 Pick date")

        tk.Button(date_row, text="✕", font=("Helvetica", 8),
                  bg=config.THEME["bg_color"], fg="#999999",
                  relief="flat", bd=0,
                  command=lambda: self._clear_date("start")).pack(side="left", padx=(0, 16))

        # End date
        tk.Label(date_row, text="End date:", bg=config.THEME["bg_color"],
                 font=config.BODY_FONT).pack(side="left")
        self.end_date_var = tk.StringVar(value="")
        self.end_date_btn = tk.Button(
            date_row, textvariable=self.end_date_var,
            font=config.BODY_FONT, width=16,
            bg="white", fg=config.THEME["text_color"],
            relief="solid", bd=1,
            command=lambda: self._open_calendar("end")
        )
        self.end_date_btn.pack(side="left", padx=(4, 2))
        self.end_date_var.set("📅 Pick date  (blank = today)")

        tk.Button(date_row, text="✕", font=("Helvetica", 8),
                  bg=config.THEME["bg_color"], fg="#999999",
                  relief="flat", bd=0,
                  command=lambda: self._clear_date("end")).pack(side="left", padx=(0, 4))

        # Internal date state — actual datetime.date objects, None = not set
        self._start_date: datetime.date | None = None
        self._end_date:   datetime.date | None = None

        # --- Role / Quiz selector row ---
        role_row = tk.Frame(filter_frame, bg=config.THEME["bg_color"])
        role_row.pack(fill="x")

        tk.Button(role_row, text="🎯 Select Roles & Quizzes",
                  font=config.BODY_FONT,
                  bg=config.THEME["info_color"], fg="white",
                  command=self._open_selector_popup).pack(side="left", padx=(0, 8))

        self.clear_filter_button = tk.Button(role_row, text="✕ Clear",
                  font=config.BODY_FONT,
                  bg=config.THEME["danger_color"], fg="white",
                  command=self._clear_filter)
        # shown only when a filter is active — starts hidden
        self.clear_filter_button.pack_forget()

        self.quiz_status_label = tk.Label(role_row,
                                          text="No filter — all roles/quizzes will be downloaded.",
                                          bg=config.THEME["bg_color"],
                                          fg="#888888",
                                          font=("Helvetica", 9, "italic"),
                                          wraplength=500, justify="left")
        self.quiz_status_label.pack(side="left", padx=(6, 0))

        # State preserved across popup open/close
        self._selected_roles: list = []
        # ─────────────────────────────────────────────────────────────────────────

        controls_frame = tk.Frame(self, bg=config.THEME["bg_color"])
        controls_frame.pack(pady=10)

        self.part1_button = tk.Button(controls_frame, text="► Run Downloader (Part 1)", font=("Helvetica", 12, "bold"), bg=config.THEME["button_bg"], fg="white", pady=5, padx=15, command=self._on_start_part1)
        self.part1_button.grid(row=0, column=0, padx=10, pady=5)
        self.part1_stop_button = tk.Button(controls_frame, text="■ Stop", font=config.BODY_FONT, bg=config.THEME["danger_color"], fg="white", command=self.task_manager.stop_automation)
        self.part1_stop_and_continue_button = tk.Button(
            controls_frame, text="⏹ Stop & Continue to Part 2 →",
            font=config.BODY_FONT, bg=config.THEME["warning_color"], fg="black",
            command=self.task_manager.stop_and_continue
        )
        self.part2_button = tk.Button(controls_frame, text="► Run Processor (Part 2)", font=("Helvetica", 12, "bold"), bg=config.THEME["success_color"], fg="white", pady=5, padx=15, command=self._on_start_part2)
        self.part2_button.grid(row=1, column=0, padx=10, pady=5, sticky="ew")
        self.part2_rerun_button = tk.Button(controls_frame, text="► Re-run Processor (Cache)", font=("Helvetica", 10, "bold"), bg=config.THEME["warning_color"], fg="black", pady=5, padx=10, command=self._on_start_part2_rerun)
        self.part2_stop_button = tk.Button(controls_frame, text="■ Stop", font=config.BODY_FONT, bg=config.THEME["danger_color"], fg="white", command=self.task_manager.stop_automation)
        
        # --- NEW: Conditional button to go back to the QA page ---
        self.qa_page_button = tk.Button(controls_frame, text="Go to Final QA Page →", font=("Helvetica", 10, "bold"), command=lambda: self.show_final_review_page(self.task_manager.final_results))
        
        self.timer_label = tk.Label(controls_frame, text="00:00", font=("Helvetica", 10, "bold"), bg=config.THEME["bg_color"], fg=config.THEME["text_color"])

        self.status_log = ScrolledText(self, state="disabled", height=15, wrap=tk.WORD, font=config.LOG_FONT, bg=config.THEME["log_bg"], fg=config.THEME["log_fg"], relief=tk.SOLID, borderwidth=1)
        self.status_log.pack(padx=20, pady=10, fill="both", expand=True)

        self.footer_frame = tk.Frame(self, bg=config.THEME["bg_color"])
        self.footer_frame.pack(fill="x", padx=20, pady=10)
        tk.Button(self.footer_frame, text="← Back to Settings", command=lambda: self.controller.show_frame("SettingsView"), bg=config.THEME["secondary_button_bg"], fg=config.THEME["secondary_button_fg"]).pack(side="left")
        tk.Button(self.footer_frame, text="Clear Log", command=self.clear_log_with_confirmation, bg=config.THEME["secondary_button_bg"], fg=config.THEME["secondary_button_fg"]).pack(side="right")
        self.part1_folder_button = tk.Button(self.footer_frame, text="📂 Open Downloader Folder", command=lambda: self.open_folder(part=1), bg=config.THEME["info_color"], fg="white")
        self.part2_folder_button = tk.Button(self.footer_frame, text="📂 Open Processor Backups", command=lambda: self.open_folder(part=2), bg=config.THEME["info_color"], fg="white")
        
        self.set_initial_state()

    # ── Filter helpers ────────────────────────────────────────────────────────────

    def _open_calendar(self, which: str):
        """
        Open a small calendar popup for 'start' or 'end' date.
        The selected date is written back to the button label and stored
        as a datetime.date in self._start_date / self._end_date.
        """
        popup = tk.Toplevel(self)
        popup.title("Start date" if which == "start" else "End date")
        popup.grab_set()
        popup.resizable(False, False)
        popup.configure(bg=config.THEME["bg_color"])

        # Pre-select the previously chosen date, or today
        today = datetime.date.today()
        existing = self._start_date if which == "start" else self._end_date
        init = existing or today

        cal = Calendar(
            popup,
            selectmode="day",
            year=init.year, month=init.month, day=init.day,
            date_pattern="yyyy-mm-dd",
            background=config.THEME["button_bg"],
            foreground="white",
            headersbackground=config.THEME["title_color"],
            headersforeground="white",
            selectbackground=config.THEME["success_color"],
            normalbackground=config.THEME["bg_color"],
            weekendbackground=config.THEME["bg_color"],
            othermonthbackground="#e8e8e8",
            bordercolor=config.THEME["button_bg"],
            font=config.BODY_FONT,
        )
        cal.pack(padx=10, pady=10)

        def _confirm():
            date_str = cal.get_date()          # "YYYY-MM-DD"
            chosen   = datetime.date.fromisoformat(date_str)

            if which == "start":
                # Validate: start must not be after end
                if self._end_date and chosen > self._end_date:
                    messagebox.showerror(
                        "Invalid Range",
                        f"Start date ({chosen}) cannot be after end date ({self._end_date}).",
                        parent=popup
                    )
                    return
                self._start_date = chosen
                self.start_date_var.set(f"📅 {date_str}")
                self.start_date_btn.config(fg=config.THEME["title_color"], font=("Helvetica", 10, "bold"))
            else:
                # Validate: end must not be before start
                if self._start_date and chosen < self._start_date:
                    messagebox.showerror(
                        "Invalid Range",
                        f"End date ({chosen}) cannot be before start date ({self._start_date}).",
                        parent=popup
                    )
                    return
                self._end_date = chosen
                self.end_date_var.set(f"📅 {date_str}")
                self.end_date_btn.config(fg=config.THEME["title_color"], font=("Helvetica", 10, "bold"))

            popup.destroy()

        btn_row = tk.Frame(popup, bg=config.THEME["bg_color"])
        btn_row.pack(pady=(0, 10))
        tk.Button(btn_row, text="Cancel", font=config.BODY_FONT,
                  bg=config.THEME["secondary_button_bg"],
                  fg=config.THEME["secondary_button_fg"],
                  command=popup.destroy).pack(side="left", padx=8)
        tk.Button(btn_row, text="✓  Select", font=("Helvetica", 10, "bold"),
                  bg=config.THEME["button_bg"], fg="white",
                  command=_confirm).pack(side="left", padx=8)

    def _clear_date(self, which: str):
        """Clear a single date picker back to its placeholder."""
        if which == "start":
            self._start_date = None
            self.start_date_var.set("📅 Pick date")
            self.start_date_btn.config(fg=config.THEME["text_color"],
                                       font=config.BODY_FONT)
        else:
            self._end_date = None
            self.end_date_var.set("📅 Pick date  (blank = today)")
            self.end_date_btn.config(fg=config.THEME["text_color"],
                                     font=config.BODY_FONT)

    def _clear_filter(self):
        """Reset role + quiz selection back to 'download everything'."""
        self._selected_roles = []
        self.task_manager.selected_quizzes = []
        self.quiz_status_label.config(
            text="No filter — all roles/quizzes will be downloaded.",
            fg="#888888", font=("Helvetica", 9, "italic")
        )
        self.clear_filter_button.pack_forget()

    def _open_selector_popup(self):
        """
        Single popup with roles on the LEFT and quizzes on the RIGHT.

        • Ticking a role instantly ticks all its quizzes on the right.
        • Unticking a role unticks its quizzes (unless they are also
          covered by another still-ticked role).
        • Individual quizzes can always be ticked/unticked freely on
          the right panel regardless of role selection.
        • Confirm in one click — no second step needed.
        """
        popup = tk.Toplevel(self)
        popup.title("Select Roles & Quizzes to Download")
        popup.grab_set()
        popup.resizable(True, True)
        popup.configure(bg=config.THEME["bg_color"])
        popup.geometry("820x560")
        popup.minsize(700, 420)

        # ── Header ────────────────────────────────────────────────────────────────
        tk.Label(popup,
                 text="Tick roles on the left — their quizzes are highlighted on the right.\n"
                      "You can also tick/untick individual quizzes on the right to fine-tune.\n"
                      "Leave everything unchecked to download all quizzes.",
                 bg=config.THEME["bg_color"], font=config.BODY_FONT,
                 justify="left").pack(padx=15, pady=(10, 6), anchor="w")

        # ── Body: two panels ──────────────────────────────────────────────────────
        body = tk.Frame(popup, bg=config.THEME["bg_color"])
        body.pack(fill="both", expand=True, padx=15, pady=(0, 4))
        body.columnconfigure(0, weight=0, minsize=240)
        body.columnconfigure(1, weight=0, minsize=8)   # divider
        body.columnconfigure(2, weight=1)
        body.rowconfigure(0, weight=1)

        # ── LEFT: role panel ──────────────────────────────────────────────────────
        left_frame = tk.LabelFrame(body, text="Roles", font=("Helvetica", 9, "bold"),
                                   bg=config.THEME["bg_color"], fg=config.THEME["title_color"],
                                   padx=8, pady=6)
        left_frame.grid(row=0, column=0, sticky="nsew")

        prev_roles = set(self._selected_roles)
        role_vars: dict[str, tk.BooleanVar] = {}

        for role, tests in ROLE_TO_TEST_MAPPING.items():
            role_var = tk.BooleanVar(value=role in prev_roles)
            role_vars[role] = role_var

            role_frame = tk.Frame(left_frame, bg=config.THEME["bg_color"])
            role_frame.pack(fill="x", pady=3)

            tk.Checkbutton(role_frame, text=role, variable=role_var,
                           font=("Helvetica", 10, "bold"),
                           bg=config.THEME["bg_color"],
                           fg=config.THEME["title_color"],
                           anchor="w").pack(side="top", fill="x")

            required = [t for t in tests if MASTER_TEST_CONFIG[t]['type'] == 'Required']
            optional = [t for t in tests if MASTER_TEST_CONFIG[t]['type'] == 'Optional']

            if required:
                tk.Label(role_frame,
                         text="  ● " + ",  ".join(required),
                         bg=config.THEME["bg_color"],
                         fg=config.THEME["success_color"],
                         font=("Helvetica", 8),
                         justify="left", wraplength=210).pack(anchor="w", padx=(14, 0))
            if optional:
                tk.Label(role_frame,
                         text="  ○ " + ",  ".join(optional),
                         bg=config.THEME["bg_color"],
                         fg="#777777",
                         font=("Helvetica", 8),
                         justify="left", wraplength=210).pack(anchor="w", padx=(14, 0))

            tk.Frame(left_frame, bg="#dddddd", height=1).pack(fill="x", pady=(4, 0))

        # ── Thin divider ──────────────────────────────────────────────────────────
        tk.Frame(body, bg="#cccccc", width=2).grid(row=0, column=1, sticky="ns", padx=4)

        # ── RIGHT: quiz panel (scrollable) ────────────────────────────────────────
        right_frame = tk.LabelFrame(body, text="Quizzes  (● Required   ○ Optional)",
                                    font=("Helvetica", 9, "bold"),
                                    bg=config.THEME["bg_color"], fg=config.THEME["title_color"],
                                    padx=8, pady=6)
        right_frame.grid(row=0, column=2, sticky="nsew")

        canvas = tk.Canvas(right_frame, bg=config.THEME["bg_color"], highlightthickness=0)
        vsb = tk.Scrollbar(right_frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg=config.THEME["bg_color"])
        cw = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(cw, width=e.width))
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", lambda e, c=canvas: c.yview_scroll(int(-1*(e.delta/120)), "units")))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        # Build quiz checkboxes — each quiz appears exactly once, grouped by role
        # Cross-role quizzes show all their role tags on one row
        quiz_vars: dict[str, tk.BooleanVar] = {}

        # Build pre-selected set from saved state
        prev_quizzes = set(self.task_manager.selected_quizzes) if self.task_manager.selected_quizzes else set()
        if not prev_quizzes:
            # Derive from selected roles if no manual overrides exist
            for role in self._selected_roles:
                prev_quizzes.update(ROLE_TO_TEST_MAPPING[role])

        # Map quiz → all roles it belongs to (for the tag labels)
        quiz_to_roles: dict[str, list[str]] = {}
        for role, tests in ROLE_TO_TEST_MAPPING.items():
            for quiz in tests:
                quiz_to_roles.setdefault(quiz, []).append(role)

        rendered: set[str] = set()
        for role, tests in ROLE_TO_TEST_MAPPING.items():
            # Role section divider on the right panel
            tk.Label(inner, text=f" {role} ",
                     bg=config.THEME["info_color"], fg="white",
                     font=("Helvetica", 8, "bold")).pack(anchor="w", pady=(10, 3), padx=2)

            for quiz in tests:
                if quiz in rendered:
                    continue
                rendered.add(quiz)

                quiz_type = MASTER_TEST_CONFIG[quiz]['type']
                bullet = "●" if quiz_type == "Required" else "○"
                type_color = config.THEME["success_color"] if quiz_type == "Required" else "#777777"

                var = tk.BooleanVar(value=quiz in prev_quizzes)
                quiz_vars[quiz] = var

                row = tk.Frame(inner, bg=config.THEME["bg_color"])
                row.pack(fill="x", padx=6, pady=1)

                tk.Checkbutton(row, text=f"{bullet} {quiz}", variable=var,
                               bg=config.THEME["bg_color"],
                               fg=type_color,
                               font=config.BODY_FONT,
                               anchor="w").pack(side="left")

                # Show extra role tags if this quiz belongs to more than one role
                extra_roles = [r for r in quiz_to_roles.get(quiz, []) if r != role]
                if extra_roles:
                    tk.Label(row, text="  also: " + ", ".join(extra_roles),
                             bg=config.THEME["bg_color"],
                             fg="#aaaaaa",
                             font=("Helvetica", 8)).pack(side="left")

        # Wire role checkboxes to quiz checkboxes — live update on tick/untick
        def _on_role_toggle(*_):
            currently_selected_roles = {r for r, v in role_vars.items() if v.get()}
            quizzes_from_roles = set()
            for r in currently_selected_roles:
                quizzes_from_roles.update(ROLE_TO_TEST_MAPPING[r])
            # Tick quizzes that now belong to a selected role;
            # untick those whose roles are all deselected (but only if
            # the user hasn't manually ticked them independently — we
            # track "role-driven" ticks separately using a snapshot).
            for quiz, var in quiz_vars.items():
                in_selected_role = quiz in quizzes_from_roles
                in_deselected_role_only = quiz not in quizzes_from_roles
                # Simple rule: always sync — roles drive the right panel
                var.set(in_selected_role)

        for rv in role_vars.values():
            rv.trace_add("write", _on_role_toggle)

        # ── Bottom button row ─────────────────────────────────────────────────────
        btn_row = tk.Frame(popup, bg=config.THEME["bg_color"])
        btn_row.pack(fill="x", padx=15, pady=(4, 10))

        # Left-side helpers
        tk.Button(btn_row, text="Select All Roles", font=config.BODY_FONT,
                  bg=config.THEME["secondary_button_bg"],
                  fg=config.THEME["secondary_button_fg"],
                  command=lambda: [v.set(True) for v in role_vars.values()]).pack(side="left", padx=4)
        tk.Button(btn_row, text="Clear All Roles", font=config.BODY_FONT,
                  bg=config.THEME["secondary_button_bg"],
                  fg=config.THEME["secondary_button_fg"],
                  command=lambda: [v.set(False) for v in role_vars.values()]).pack(side="left", padx=4)
        tk.Button(btn_row, text="Select All Quizzes", font=config.BODY_FONT,
                  bg=config.THEME["secondary_button_bg"],
                  fg=config.THEME["secondary_button_fg"],
                  command=lambda: [v.set(True) for v in quiz_vars.values()]).pack(side="left", padx=4)
        tk.Button(btn_row, text="Clear All Quizzes", font=config.BODY_FONT,
                  bg=config.THEME["secondary_button_bg"],
                  fg=config.THEME["secondary_button_fg"],
                  command=lambda: [v.set(False) for v in quiz_vars.values()]).pack(side="left", padx=4)

        # Right-side confirm
        tk.Button(btn_row, text="✓  Confirm Selection", font=("Helvetica", 10, "bold"),
                  bg=config.THEME["button_bg"], fg="white", padx=12,
                  command=lambda: self._confirm_selection(popup, role_vars, quiz_vars)).pack(side="right", padx=6)

    def _confirm_selection(self, popup, role_vars: dict, quiz_vars: dict):
        """Save role + quiz selection, update status label, close popup."""
        self._selected_roles = [r for r, v in role_vars.items() if v.get()]
        selected_quizzes    = [q for q, v in quiz_vars.items() if v.get()]
        self.task_manager.selected_quizzes = selected_quizzes
        popup.destroy()

        if selected_quizzes:
            role_part  = f"{len(self._selected_roles)} role(s)" if self._selected_roles else "custom"
            quiz_names = ", ".join(selected_quizzes)
            self.quiz_status_label.config(
                text=f"Filter active — {role_part} → {len(selected_quizzes)} quiz(es): {quiz_names}",
                fg=config.THEME["title_color"],
                font=config.BODY_FONT
            )
            self.clear_filter_button.pack(side="left", padx=(0, 6))
        else:
            self._selected_roles = []
            self.quiz_status_label.config(
                text="No filter — all roles/quizzes will be downloaded.",
                fg="#888888", font=("Helvetica", 9, "italic")
            )
            self.clear_filter_button.pack_forget()

    def _apply_filter_params(self):
        """
        Push the current date selections into TaskManager before any run starts.
        Dates come from the calendar pickers (datetime.date objects), never from
        raw text — so no parsing errors are possible here.
        Returns True always (validation already happened inside the calendar popup).
        """
        if self._start_date:
            self.task_manager.start_date = pd.Timestamp(self._start_date, tz='UTC')
        else:
            self.task_manager.start_date = None

        if self._end_date:
            # End of the chosen day (23:59:59 UTC) so the full day is included
            self.task_manager.end_date = (
                pd.Timestamp(self._end_date, tz='UTC')
                + pd.Timedelta(days=1)
                - pd.Timedelta(seconds=1)
            )
        else:
            self.task_manager.end_date = None

        return True

    # ─────────────────────────────────────────────────────────────────────────────

    def show_final_review_page(self, results):
        self.controller.show_frame("FinalReviewView", data_to_pass=results)
        self.update_ui_for_task("Part 2", is_running=False)

    def prompt_for_manual_review(self, candidate_data, role_name, review_num, total_reviews):
        popup = ReviewView(self, candidate_data, role_name, review_num, total_reviews)
        self.task_manager.review_decision = (popup.decision, popup.justification)
        self.task_manager.review_event.set()

    def reset_ui_state(self):
        """Resets buttons to the ready state, and conditionally shows QA/re-run buttons."""
        self.part1_button.config(state="normal")
        run_folder_exists = config.QUILGO_RUNS_DIR.exists() and any(config.QUILGO_RUNS_DIR.iterdir())
        self.part2_button.config(state="normal" if run_folder_exists else "disabled")
        
        # --- NEW: Conditional Button Logic ---
        if self.task_manager.final_results: # If there's cached result data
            self.qa_page_button.grid(row=2, column=0, columnspan=2, padx=10, pady=10, sticky="ew")
        else:
            self.qa_page_button.grid_forget()

        if config.MANATAL_CACHE_FILE.exists():
            self.part2_rerun_button.grid(row=1, column=1, padx=10, pady=5, sticky="w")
        else:
            self.part2_rerun_button.grid_forget()
            
        self.part1_stop_button.grid_forget()
        self.part1_stop_and_continue_button.grid_forget()
        self.part2_stop_button.grid_forget()
        self.timer_label.grid_forget()

    def update_ui_for_task(self, part, is_running, is_continuation=False):
        if is_running:
            # When a new task starts, clear the previous final results.
            # Do NOT clear for API Push — that step needs the results to refresh its cache.
            if not is_continuation and part != "API Push":
                self.task_manager.final_results = []
                self._clear_log_content()
                self.timer_running = True
                self.start_time = time.time()
                self._update_timer()
            
            self.part1_button.config(state="disabled")
            self.part2_button.config(state="disabled")
            self.part1_folder_button.pack_forget()
            self.part2_folder_button.pack_forget()
            self.part2_rerun_button.grid_forget()
            self.qa_page_button.grid_forget()

            if part == "Part 1":
                self.part1_stop_button.grid(row=0, column=1, padx=10)
                self.part1_stop_and_continue_button.grid(row=0, column=2, padx=5)
                self.timer_label.grid(row=0, column=3, padx=5)
            elif part == "Part 2":
                self.part2_stop_button.grid(row=1, column=1, padx=10)
                self.timer_label.grid(row=1, column=2, padx=5)
        else:
            self.timer_running = False
            self.reset_ui_state()

    # The rest of the functions (_update_timer, log_message, etc.) are unchanged
    def _update_timer(self):
        if self.timer_running: self.timer_label.config(text=time.strftime('%M:%S', time.gmtime(time.time() - self.start_time))); self.after(1000, self._update_timer)
    def log_message(self, message): self.status_log.config(state="normal"); self.status_log.insert(tk.END, message + "\n"); self.status_log.config(state="disabled"); self.status_log.see(tk.END)
    def _clear_log_content(self): self.status_log.config(state="normal"); self.status_log.delete("1.0", tk.END); self.status_log.config(state="disabled")
    def clear_log_with_confirmation(self):
        if messagebox.askyesno("Confirm Clear", "Are you sure you want to clear the log?"): self._clear_log_content(); self.log_message("Log cleared by user. System ready.")
    def set_initial_state(self): self._clear_log_content(); self.log_message("System Ready. Please choose an action.")

    def _on_start_part1(self):
        if self._apply_filter_params():
            self.task_manager.start_part1_downloader()

    def _on_start_part2(self):
        if self._apply_filter_params():
            self.task_manager.start_part2_processor()

    def _on_start_part2_rerun(self):
        if self._apply_filter_params():
            self.task_manager.start_part2_reprocessor()
    def show_part1_folder_button(self): self.part1_folder_button.pack(side="right", padx=5)
    def show_part2_folder_button(self): self.part2_folder_button.pack(side="right", padx=5)
    def open_folder(self, part):
        try:
            if part == 1: folder_to_open = config.QUILGO_MASTER_DIR if config.QUILGO_MASTER_DIR.exists() else None
            else: folder_to_open = config.DOWNLOADS_DIR; folder_to_open.mkdir(exist_ok=True)
            if not folder_to_open: messagebox.showwarning("Not Found", "No folder found."); return
            if sys.platform == "win32": os.startfile(folder_to_open)
            else: subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", folder_to_open])
        except Exception as e: messagebox.showerror("Error", f"Could not open folder: {e}")