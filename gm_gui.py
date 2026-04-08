"""
Stellar Dominion - GM Console
A graphical wrapper around pbem.py for game master operations.

Run from the project root:
    python gm_gui.py

Two-panel layout:
  - Left: tabbed sections with action buttons (turn ops, players, universe,
    bases, moderator, previews, settings)
  - Right: live console showing CLI output

All actions invoke `python -u pbem.py <command>` as a subprocess and stream
stdout/stderr into the console widget.
"""

import json
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

# ============================================================================
# Config
# ============================================================================

PROJECT_DIR = Path(__file__).parent.resolve()
CONFIG_FILE = PROJECT_DIR / "gm_gui_config.json"
PBEM_SCRIPT = PROJECT_DIR / "pbem.py"


def load_config():
    defaults = {
        "game_id": "OMICRON101",
        "python_exe": sys.executable,
        "project_dir": str(PROJECT_DIR),
        "credentials_path": "",
    }
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
            defaults.update(data)
        except Exception:
            pass
    return defaults


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ============================================================================
# Main app
# ============================================================================

class StellarDominionGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Stellar Dominion - GM Console")
        self.root.geometry("1500x850")
        self.root.minsize(1100, 600)

        self.config_data = load_config()
        self.process = None
        self.output_queue = queue.Queue()
        self._current_callback = None

        # Wizard state
        self.wizard_active = False
        self.wizard_stage = 0
        self.wizard_auto = False

        self._build_ui()
        self._poll_output()

    # ========================================================================
    # Layout
    # ========================================================================

    def _build_ui(self):
        # ----- Top status bar -----
        topbar = ttk.Frame(self.root, padding=6)
        topbar.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(topbar, text="Game:", font=("", 10, "bold")).pack(side=tk.LEFT, padx=(0, 4))
        self.game_label = ttk.Label(topbar, text=self.config_data["game_id"],
                                     foreground="darkblue", font=("", 10, "bold"))
        self.game_label.pack(side=tk.LEFT, padx=(0, 16))

        ttk.Label(topbar, text="Status:").pack(side=tk.LEFT, padx=(0, 4))
        self.status_label = ttk.Label(topbar, text="Ready", foreground="darkgreen")
        self.status_label.pack(side=tk.LEFT)

        ttk.Button(topbar, text="Clear Console", command=self._clear_console).pack(side=tk.RIGHT, padx=2)
        ttk.Button(topbar, text="Cancel Process", command=self._cancel_process).pack(side=tk.RIGHT, padx=2)

        # ----- Main paned window -----
        paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Left: notebook with action sections
        self.notebook = ttk.Notebook(paned)
        paned.add(self.notebook, weight=2)

        self._build_wizard_tab()
        self._build_turn_tab()
        self._build_players_tab()
        self._build_universe_tab()
        self._build_bases_tab()
        self._build_moderator_tab()
        self._build_previews_tab()
        self._build_dbbrowser_tab()
        self._build_settings_tab()

        # Right: console output
        right = ttk.Frame(paned)
        paned.add(right, weight=3)

        ttk.Label(right, text="Output Console", font=("", 10, "bold")).pack(anchor=tk.W, padx=5, pady=(5, 0))
        self.console = scrolledtext.ScrolledText(
            right, wrap=tk.WORD, bg="#1e1e1e", fg="#d4d4d4",
            font=("Consolas", 9), insertbackground="white", state=tk.DISABLED,
        )
        self.console.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.console.tag_config("cmd", foreground="#9cdcfe", font=("Consolas", 9, "bold"))
        self.console.tag_config("ok", foreground="#4ec9b0")
        self.console.tag_config("err", foreground="#f48771")
        self.console.tag_config("info", foreground="#dcdcaa")

        # Welcome
        self._append("=== Stellar Dominion GM Console ===\n", "info")
        self._append(f"Game: {self.config_data['game_id']}\n", "info")
        self._append(f"Project: {PROJECT_DIR}\n", "info")
        self._append("Click any button on the left to run a CLI command.\n\n", "info")

    # ========================================================================
    # Helpers
    # ========================================================================

    def _scrollable_frame(self, parent):
        """Wrap a frame inside a scrollable canvas (for tabs with many actions)."""
        canvas = tk.Canvas(parent, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
        inner = ttk.Frame(canvas)

        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Bind mousewheel scrolling (Windows/Mac style)
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        return inner

    def _section_header(self, parent, text):
        lbl = ttk.Label(parent, text=text, font=("", 10, "bold"), foreground="#444")
        lbl.pack(anchor=tk.W, padx=5, pady=(10, 2))
        ttk.Separator(parent, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=5, pady=(0, 4))

    def _action_row(self, parent, label, base_args, fields=None, custom_builder=None,
                    button_width=22, validators=None):
        """
        Create a row with a button and optional input fields.

        base_args: list of args after `pbem.py` (e.g. ['fetch-mail'])
        fields: list of (key, label, default, width) tuples
                The default builder converts each non-empty entry to `--key value`.
                Use the form `key='some-flag'` for multi-word flags.
        custom_builder: optional callable(entries_dict) -> args list, overrides default
        validators: optional dict {field_key: validator_name} — validates IDs against DB
                    before running. Validators: ship, body, system, prefect, account,
                    starbase, port, outpost, base.
        """
        row = ttk.Frame(parent, padding=(5, 2))
        row.pack(fill=tk.X, padx=2, pady=1)

        entries = {}
        btn = ttk.Button(row, text=label, width=button_width)
        btn.pack(side=tk.LEFT, padx=(0, 8))

        if fields:
            for key, lbl, default, width in fields:
                ttk.Label(row, text=lbl).pack(side=tk.LEFT, padx=(4, 2))
                e = ttk.Entry(row, width=width)
                if default:
                    e.insert(0, str(default))
                e.pack(side=tk.LEFT)
                entries[key] = e

        def run():
            # Validate fields if validators specified
            if validators:
                for field_key, validator_name in validators.items():
                    e = entries.get(field_key)
                    if e is None:
                        continue
                    val = e.get().strip()
                    if not val:
                        continue  # skip empty fields
                    ok, err = self._validate_id(validator_name, val)
                    if not ok:
                        self._append(f"\n>>> {label}\n", "cmd")
                        self._append(f"[validation failed] {err}\n", "err")
                        messagebox.showerror("Validation failed", err)
                        return

            if custom_builder:
                try:
                    args = custom_builder(entries)
                except Exception as ex:
                    messagebox.showerror("Input error", str(ex))
                    return
            else:
                args = list(base_args)
                for key, e in entries.items():
                    val = e.get().strip()
                    if val:
                        args.extend([f"--{key}", val])
            self._run_pbem(args, label)

        btn.config(command=run)
        return row

    def _game_field(self):
        """Standard game ID field tuple."""
        return ('game', 'Game:', self.config_data['game_id'], 14)

    # ========================================================================
    # Database helpers (for validation)
    # ========================================================================

    def _db_path(self, which):
        """Return Path to universe.db or game_state.db under the project dir."""
        base = Path(self.config_data['project_dir']) / "game_data"
        return base / ("universe.db" if which == 'universe' else "game_state.db")

    def _validate_id(self, validator, value):
        """
        Check that an ID exists in the appropriate table.
        Returns (ok: bool, error_message: str).

        Validators:
          ship, body, system, prefect, account, starbase, port, outpost, base
        `base` matches any of starbase/port/outpost (for commands that accept all).
        """
        import sqlite3
        try:
            val_int = int(value)
        except (ValueError, TypeError):
            return False, f"'{value}' is not a valid integer ID"

        specs = {
            'ship':     ('game_state', 'ships',            'ship_id'),
            'prefect':  ('game_state', 'prefects',         'prefect_id'),
            'account':  ('game_state', 'players',          'account_number'),
            'starbase': ('game_state', 'starbases',        'base_id'),
            'port':     ('game_state', 'surface_ports',    'port_id'),
            'outpost':  ('game_state', 'outposts',         'outpost_id'),
            'body':     ('universe',   'celestial_bodies', 'body_id'),
            'system':   ('universe',   'star_systems',     'system_id'),
        }

        def _exists(db_key, table, col, v):
            path = self._db_path(db_key)
            if not path.exists():
                return None  # can't check — DB missing
            try:
                c = sqlite3.connect(str(path))
                row = c.execute(f"SELECT 1 FROM {table} WHERE {col} = ?", (v,)).fetchone()
                c.close()
                return row is not None
            except Exception as ex:
                return None  # skip validation on DB error

        if validator == 'base':
            # Try all three base types
            for key in ('starbase', 'port', 'outpost'):
                spec = specs[key]
                res = _exists(*spec, val_int)
                if res is True:
                    return True, ""
                if res is None:
                    return True, ""  # DB unavailable, skip
            return False, f"No starbase, port, or outpost found with ID {val_int}"

        spec = specs.get(validator)
        if not spec:
            return True, ""
        res = _exists(*spec, val_int)
        if res is None:
            return True, ""  # DB unavailable, skip validation rather than block
        if not res:
            return False, f"No {validator} found with ID {val_int}"
        return True, ""

    # ========================================================================
    # Gmail credentials test
    # ========================================================================

    def _test_gmail(self):
        """Verify gmail credentials file, token cache, and dependency imports."""
        self._append("\n=== Gmail Connection Test ===\n", "cmd")
        self.status_label.config(text="Testing Gmail...", foreground="orange")

        # 1. Dependency check
        import importlib.util
        missing = []
        for mod in ('google.oauth2.credentials', 'google_auth_oauthlib.flow',
                    'googleapiclient.discovery'):
            try:
                if importlib.util.find_spec(mod) is None:
                    missing.append(mod)
            except (ImportError, ValueError):
                missing.append(mod)

        if missing:
            self._append(f"[FAIL] Missing Python packages: {', '.join(missing)}\n", "err")
            self._append("      Install with: pip install google-auth google-auth-oauthlib google-api-python-client\n", "info")
            self.status_label.config(text="Gmail test failed", foreground="red")
            return
        self._append("[OK]   Google API libraries installed\n", "ok")

        # 2. Credentials file
        creds_path_str = self.config_data.get('credentials_path', '').strip()
        if not creds_path_str:
            self._append("[FAIL] No credentials path configured in Settings\n", "err")
            self.status_label.config(text="Gmail test failed", foreground="red")
            return
        creds_path = Path(creds_path_str)
        if not creds_path.exists():
            self._append(f"[FAIL] Credentials file not found: {creds_path}\n", "err")
            self.status_label.config(text="Gmail test failed", foreground="red")
            return
        self._append(f"[OK]   Credentials file exists: {creds_path.name}\n", "ok")

        # 3. Credentials file is a valid OAuth client secrets file
        try:
            data = json.loads(creds_path.read_text())
            if 'installed' in data or 'web' in data:
                section = data.get('installed') or data.get('web')
                if 'client_id' not in section:
                    self._append("[FAIL] Credentials file missing client_id\n", "err")
                    self.status_label.config(text="Gmail test failed", foreground="red")
                    return
                self._append(f"[OK]   Valid OAuth client secrets format\n", "ok")
                self._append(f"       client_id: ...{section['client_id'][-20:]}\n", "info")
            else:
                self._append("[WARN] Credentials JSON doesn't have 'installed' or 'web' key\n", "err")
                self._append("       This may not be an OAuth client secrets file\n", "info")
        except json.JSONDecodeError as ex:
            self._append(f"[FAIL] Credentials file is not valid JSON: {ex}\n", "err")
            self.status_label.config(text="Gmail test failed", foreground="red")
            return

        # 4. Check for cached token
        token_candidates = [
            Path(self.config_data['project_dir']) / "game_data" / "gmail_token.json",
            Path(self.config_data['project_dir']) / "token.json",
            creds_path.parent / "token.json",
        ]
        token_path = None
        for candidate in token_candidates:
            if candidate.exists():
                token_path = candidate
                break

        if not token_path:
            self._append("[INFO] No cached OAuth token found (will prompt browser on first fetch)\n", "info")
            self._append("       Checked: game_data/gmail_token.json, token.json, creds dir\n", "info")
            self.status_label.config(text="Gmail test passed (no token)", foreground="darkgreen")
            return

        self._append(f"[OK]   Token cache found: {token_path}\n", "ok")

        # 5. Try loading the token and check validity
        try:
            from google.oauth2.credentials import Credentials
            SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            if creds.valid:
                self._append("[OK]   Cached token is valid — Gmail should connect cleanly\n", "ok")
                self.status_label.config(text="Gmail test passed", foreground="darkgreen")
            elif creds.expired and creds.refresh_token:
                self._append("[INFO] Token expired but refreshable (will auto-refresh on next use)\n", "info")
                self.status_label.config(text="Gmail test passed", foreground="darkgreen")
            else:
                self._append("[WARN] Token exists but cannot be refreshed — re-auth will be required\n", "err")
                self.status_label.config(text="Gmail test warning", foreground="orange")
        except Exception as ex:
            self._append(f"[WARN] Could not load token: {ex}\n", "err")
            self.status_label.config(text="Gmail test warning", foreground="orange")

    # ========================================================================
    # Tab: Turn Wizard
    # ========================================================================

    WIZARD_STAGES = [
        ("Fetch Mail",     ['fetch-mail'],     "Download new player orders from Gmail inbox"),
        ("Process Inbox",  ['process-inbox'],  "Parse orders into the database"),
        ("Review Orders",  ['review-orders'],  "List all pending orders for this turn (pause here to inspect)"),
        ("Run Turn",       ['run-turn'],       "Resolve all orders and generate reports"),
        ("Send Turns",     ['send-turns'],     "Email ship/base/prefect reports to players"),
        ("Advance Turn",   ['advance-turn'],   "Move to next week, reset OCs"),
    ]

    def _build_wizard_tab(self):
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Turn Wizard")

        ttk.Label(tab, text="Turn Pipeline Wizard",
                  font=("", 14, "bold")).pack(anchor=tk.W, pady=(0, 4))
        ttk.Label(tab,
                  text="Runs each stage of the turn cycle in sequence. "
                       "Stop after any stage to review, or auto-run all.",
                  foreground="#555", wraplength=500, justify=tk.LEFT).pack(anchor=tk.W, pady=(0, 12))

        # Game ID entry
        game_row = ttk.Frame(tab)
        game_row.pack(fill=tk.X, pady=(0, 12))
        ttk.Label(game_row, text="Game:", width=8).pack(side=tk.LEFT)
        self.wizard_game = ttk.Entry(game_row, width=16)
        self.wizard_game.insert(0, self.config_data['game_id'])
        self.wizard_game.pack(side=tk.LEFT)

        # Stages list
        stages_frame = ttk.LabelFrame(tab, text="Stages", padding=8)
        stages_frame.pack(fill=tk.X, pady=(0, 12))

        self.wizard_labels = []
        self.wizard_status_labels = []
        for i, (name, _args, desc) in enumerate(self.WIZARD_STAGES):
            row = ttk.Frame(stages_frame)
            row.pack(fill=tk.X, pady=3)

            num_lbl = ttk.Label(row, text=f"{i+1}.", width=3, foreground="#666")
            num_lbl.pack(side=tk.LEFT)

            status_lbl = ttk.Label(row, text="●", width=2,
                                    foreground="#888", font=("", 12))
            status_lbl.pack(side=tk.LEFT, padx=(2, 6))
            self.wizard_status_labels.append(status_lbl)

            name_lbl = ttk.Label(row, text=name, width=16, font=("", 10, "bold"))
            name_lbl.pack(side=tk.LEFT)
            self.wizard_labels.append(name_lbl)

            desc_lbl = ttk.Label(row, text=desc, foreground="#666")
            desc_lbl.pack(side=tk.LEFT, padx=(6, 0))

        # Control buttons
        ctrl_frame = ttk.Frame(tab)
        ctrl_frame.pack(fill=tk.X, pady=(0, 12))

        self.wizard_start_btn = ttk.Button(ctrl_frame, text="▶ Start Wizard",
                                            width=18, command=self._wizard_start)
        self.wizard_start_btn.pack(side=tk.LEFT, padx=(0, 4))

        self.wizard_next_btn = ttk.Button(ctrl_frame, text="Next Stage →",
                                           width=16, command=self._wizard_next,
                                           state=tk.DISABLED)
        self.wizard_next_btn.pack(side=tk.LEFT, padx=4)

        self.wizard_auto_btn = ttk.Button(ctrl_frame, text="Auto-Run All",
                                           width=14, command=self._wizard_auto_run,
                                           state=tk.DISABLED)
        self.wizard_auto_btn.pack(side=tk.LEFT, padx=4)

        self.wizard_abort_btn = ttk.Button(ctrl_frame, text="Abort",
                                            width=10, command=self._wizard_abort,
                                            state=tk.DISABLED)
        self.wizard_abort_btn.pack(side=tk.LEFT, padx=4)

        # Current stage indicator
        self.wizard_status_text = ttk.Label(tab, text="Ready to start.",
                                             font=("", 10, "italic"), foreground="#444")
        self.wizard_status_text.pack(anchor=tk.W, pady=(4, 0))

    def _wizard_set_stage_status(self, idx, state):
        """state: 'pending', 'running', 'done', 'error'"""
        colors = {
            'pending': "#888",
            'running': "#ff9500",
            'done':    "#2ecc71",
            'error':   "#e74c3c",
        }
        symbols = {
            'pending': "●",
            'running': "◉",
            'done':    "✓",
            'error':   "✗",
        }
        if 0 <= idx < len(self.wizard_status_labels):
            self.wizard_status_labels[idx].config(
                text=symbols.get(state, "●"),
                foreground=colors.get(state, "#888"),
            )

    def _wizard_start(self):
        # Reset all stages
        for i in range(len(self.WIZARD_STAGES)):
            self._wizard_set_stage_status(i, 'pending')
        self.wizard_active = True
        self.wizard_stage = 0
        self.wizard_auto = False
        self.wizard_start_btn.config(state=tk.DISABLED)
        self.wizard_next_btn.config(state=tk.DISABLED)
        self.wizard_auto_btn.config(state=tk.NORMAL)
        self.wizard_abort_btn.config(state=tk.NORMAL)
        self._append("\n=== Turn Wizard Started ===\n", "cmd")
        self._wizard_run_current()

    def _wizard_run_current(self):
        if not self.wizard_active:
            return
        if self.wizard_stage >= len(self.WIZARD_STAGES):
            self._wizard_finish(success=True)
            return

        name, args, _desc = self.WIZARD_STAGES[self.wizard_stage]
        self._wizard_set_stage_status(self.wizard_stage, 'running')
        self.wizard_status_text.config(
            text=f"Stage {self.wizard_stage + 1}/{len(self.WIZARD_STAGES)}: {name}...",
            foreground="#ff9500",
        )
        self.wizard_next_btn.config(state=tk.DISABLED)

        # Build full args, injecting --game and stage-specific extras
        game = self.wizard_game.get().strip() or self.config_data['game_id']
        full_args = list(args) + ['--game', game]

        cmd = args[0]
        creds = self.config_data.get('credentials_path', '').strip()

        # Mail commands need credentials (if configured)
        if cmd in ('fetch-mail', 'send-turns') and creds:
            full_args.extend(['--credentials', creds])

        # Mail commands need an inbox directory
        if cmd in ('fetch-mail', 'process-inbox'):
            full_args.extend(['--inbox', './inbox'])

        self._run_pbem(full_args, f"Wizard: {name}",
                       on_complete=self._wizard_stage_complete)

    def _wizard_stage_complete(self, success):
        if not self.wizard_active:
            return  # aborted mid-run

        if success:
            self._wizard_set_stage_status(self.wizard_stage, 'done')
            self.wizard_stage += 1

            if self.wizard_stage >= len(self.WIZARD_STAGES):
                self._wizard_finish(success=True)
                return

            next_name = self.WIZARD_STAGES[self.wizard_stage][0]
            self.wizard_status_text.config(
                text=f"Stage {self.wizard_stage}/{len(self.WIZARD_STAGES)} complete. "
                     f"Next: {next_name}",
                foreground="#2ecc71",
            )

            if self.wizard_auto:
                self.root.after(300, self._wizard_run_current)
            else:
                self.wizard_next_btn.config(state=tk.NORMAL)
        else:
            self._wizard_set_stage_status(self.wizard_stage, 'error')
            self.wizard_status_text.config(
                text=f"Stage {self.wizard_stage + 1} failed. Fix the issue and click Next to retry, or Abort.",
                foreground="#e74c3c",
            )
            self.wizard_auto = False
            self.wizard_next_btn.config(state=tk.NORMAL)  # allow retry

    def _wizard_next(self):
        if not self.wizard_active:
            return
        self._wizard_run_current()

    def _wizard_auto_run(self):
        if not self.wizard_active:
            return
        if not messagebox.askyesno(
            "Auto-Run Confirmation",
            "Run all remaining wizard stages without stopping between them?\n\n"
            "This includes: Run Turn, Send Turns, Advance Turn.\n"
            "Abort is still available while stages are running.",
        ):
            return
        self.wizard_auto = True
        self.wizard_auto_btn.config(state=tk.DISABLED)
        self._append("[wizard: auto-run enabled]\n", "info")
        if self._current_callback is None and (self.process is None or self.process.poll() is not None):
            # Nothing currently running — kick off next stage
            self._wizard_run_current()

    def _wizard_abort(self):
        if not self.wizard_active:
            return
        if not messagebox.askyesno("Abort Wizard",
                                    "Abort the turn wizard? Any running stage will be cancelled."):
            return
        self.wizard_active = False
        self.wizard_auto = False
        self._current_callback = None
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except Exception:
                pass
        self._append("[wizard: aborted]\n", "err")
        self.wizard_status_text.config(text="Aborted.", foreground="#e74c3c")
        self._wizard_reset_buttons()

    def _wizard_finish(self, success):
        self.wizard_active = False
        self.wizard_auto = False
        if success:
            self._append("=== Turn Wizard Complete ===\n", "ok")
            self.wizard_status_text.config(text="All stages complete!", foreground="#2ecc71")
            messagebox.showinfo("Wizard Complete",
                                 "All turn pipeline stages completed successfully.")
        self._wizard_reset_buttons()

    def _wizard_reset_buttons(self):
        self.wizard_start_btn.config(state=tk.NORMAL)
        self.wizard_next_btn.config(state=tk.DISABLED)
        self.wizard_auto_btn.config(state=tk.DISABLED)
        self.wizard_abort_btn.config(state=tk.DISABLED)

    # ========================================================================
    # Tab: Turn Operations
    # ========================================================================

    def _build_turn_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Turn Ops")
        inner = self._scrollable_frame(tab)

        # Custom builders that auto-inject mail settings
        def fetch_mail_builder(entries):
            args = ['fetch-mail', '--game',
                    entries['game'].get().strip() or self.config_data['game_id']]
            creds = self.config_data.get('credentials_path', '').strip()
            if creds:
                args.extend(['--credentials', creds])
            inbox = entries.get('inbox')
            if inbox and inbox.get().strip():
                args.extend(['--inbox', inbox.get().strip()])
            return args

        def process_inbox_builder(entries):
            args = ['process-inbox', '--game',
                    entries['game'].get().strip() or self.config_data['game_id']]
            inbox = entries['inbox'].get().strip() or './inbox'
            args.extend(['--inbox', inbox])
            return args

        def send_turns_builder(entries):
            args = ['send-turns', '--game',
                    entries['game'].get().strip() or self.config_data['game_id']]
            creds = self.config_data.get('credentials_path', '').strip()
            if creds:
                args.extend(['--credentials', creds])
            return args

        self._section_header(inner, "Email Workflow")
        self._action_row(inner, "Fetch Mail", [], custom_builder=fetch_mail_builder,
                         fields=[self._game_field(),
                                 ('inbox', 'Inbox:', './inbox', 14)])
        self._action_row(inner, "Process Inbox", [], custom_builder=process_inbox_builder,
                         fields=[self._game_field(),
                                 ('inbox', 'Inbox:', './inbox', 14)])
        self._action_row(inner, "Review Orders", ['review-orders'], [self._game_field()])

        self._section_header(inner, "Turn Resolution")
        self._action_row(inner, "Run Turn", ['run-turn'], [self._game_field()])
        self._action_row(inner, "Send Turns (Email)", [], custom_builder=send_turns_builder,
                         fields=[self._game_field()])
        self._action_row(inner, "Advance Turn", ['advance-turn'], [self._game_field()])
        self._action_row(inner, "Turn Pipeline (full)", ['turn-pipeline'], [self._game_field()])

        self._section_header(inner, "Turn Control")
        self._action_row(inner, "Turn Status", ['turn-status'], [self._game_field()])
        self._action_row(inner, "Hold Turn", ['hold-turn'], [self._game_field()])
        self._action_row(inner, "Release Turn", ['release-turn'], [self._game_field()])

        self._section_header(inner, "Quick State")
        self._action_row(inner, "List Ships", ['list-ships'])
        self._action_row(inner, "List Players", ['list-players'])

    # ========================================================================
    # Tab: Players
    # ========================================================================

    def _build_players_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Players")
        inner = self._scrollable_frame(tab)

        self._section_header(inner, "Add / List Players")
        self._action_row(inner, "Add Player", ['add-player'],
                         [('name', 'Name:', '', 16),
                          ('email', 'Email:', '', 24)])
        self._action_row(inner, "List Players", ['list-players'])

        def register_builder(entries):
            form = entries['form'].get().strip()
            if not form:
                raise ValueError("form path is required")
            return ['register-player', form]
        self._action_row(inner, "Register Player", [], custom_builder=register_builder,
                         fields=[('form', 'Form file:', '', 36)])

        self._action_row(inner, "Join Game", ['join-game'],
                         [('game', 'Game:', self.config_data['game_id'], 14)])

        self._section_header(inner, "Faction Requests")
        self._action_row(inner, "List Faction Requests", ['faction-requests'],
                         [self._game_field()])
        self._action_row(inner, "Approve Faction", ['approve-faction'],
                         [self._game_field(),
                          ('request-id', 'Request ID:', '', 12),
                          ('note', 'Note:', '', 20)])
        self._action_row(inner, "Deny Faction", ['deny-faction'],
                         [self._game_field(),
                          ('request-id', 'Request ID:', '', 12),
                          ('note', 'Note:', '', 20)])

        self._section_header(inner, "Player Management")
        self._action_row(inner, "Suspend Player", ['suspend-player'],
                         [('account', 'Account:', '', 12)],
                         validators={'account': 'account'})
        self._action_row(inner, "Reinstate Player", ['reinstate-player'],
                         [('account', 'Account:', '', 12)],
                         validators={'account': 'account'})
        self._action_row(inner, "Edit Credits", ['edit-credits'],
                         [('prefect', 'Prefect:', '', 12),
                          ('amount', 'Amount:', '', 10)],
                         validators={'prefect': 'prefect'})
        self._action_row(inner, "Generate Order Form", ['generate-form'],
                         [self._game_field(),
                          ('output', 'Output dir:', '', 24)])

    # ========================================================================
    # Tab: Universe
    # ========================================================================

    def _build_universe_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Universe")
        inner = self._scrollable_frame(tab)

        self._section_header(inner, "View Catalogues")
        self._action_row(inner, "List Universe", ['list-universe'])
        self._action_row(inner, "List Factions", ['list-factions'])
        self._action_row(inner, "List Components", ['list-components'])
        self._action_row(inner, "List Modules", ['list-modules'])

        self._section_header(inner, "Add Star Systems & Bodies")
        self._action_row(inner, "Add System", ['add-system'],
                         [('name', 'Name:', '', 16),
                          ('star-col', 'Col:', 'M', 4),
                          ('star-row', 'Row:', '13', 4)])
        self._action_row(inner, "Add Body", ['add-body'],
                         [('name', 'Name:', '', 14),
                          ('system-id', 'Sys:', '101', 5),
                          ('col', 'Col:', 'M', 4),
                          ('row', 'Row:', '13', 4),
                          ('body-type', 'Type:', 'planet', 8),
                          ('gravity', 'Grav:', '1.0', 5)],
                         validators={'system-id': 'system'})

        def add_link_builder(entries):
            a = entries['system_a'].get().strip()
            b = entries['system_b'].get().strip()
            if not a or not b:
                raise ValueError("Both system IDs required")
            return ['add-link', a, b]
        self._action_row(inner, "Add Link", [], custom_builder=add_link_builder,
                         fields=[('system_a', 'Sys A:', '', 6),
                                 ('system_b', 'Sys B:', '', 6)],
                         validators={'system_a': 'system', 'system_b': 'system'})

        self._section_header(inner, "Surface Generation")
        self._action_row(inner, "Gen Missing Surfaces", ['gen-surfaces'])
        self._action_row(inner, "Regen Surface", ['regen-surface'],
                         [('body-id', 'Body ID:', '', 10)],
                         validators={'body-id': 'body'})

    # ========================================================================
    # Tab: Bases
    # ========================================================================

    def _build_bases_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Bases")
        inner = self._scrollable_frame(tab)

        self._section_header(inner, "Add Bases")

        # add-port uses positional args
        def add_port_builder(entries):
            args = ['add-port']
            for k in ('port_id', 'body_id', 'name', 'x', 'y'):
                e = entries.get(k)
                if e is None:
                    raise ValueError(f"Missing field: {k}")
                v = e.get().strip()
                if not v:
                    raise ValueError(f"{k} is required")
                args.append(v)
            return args

        self._action_row(inner, "Add Surface Port", [], custom_builder=add_port_builder,
                         fields=[('port_id', 'Port ID:', '', 10),
                                 ('body_id', 'Body ID:', '', 10),
                                 ('name', 'Name:', '', 14),
                                 ('x', 'X:', '15', 4),
                                 ('y', 'Y:', '15', 4)],
                         validators={'body_id': 'body'})

        def add_outpost_builder(entries):
            args = ['add-outpost']
            for k in ('outpost_id', 'body_id', 'name', 'x', 'y'):
                v = entries[k].get().strip()
                if not v:
                    raise ValueError(f"{k} is required")
                args.append(v)
            t = entries.get('type')
            if t and t.get().strip():
                args.extend(['--type', t.get().strip()])
            return args
        self._action_row(inner, "Add Outpost", [], custom_builder=add_outpost_builder,
                         fields=[('outpost_id', 'Outpost ID:', '', 10),
                                 ('body_id', 'Body ID:', '', 10),
                                 ('name', 'Name:', '', 14),
                                 ('x', 'X:', '5', 4),
                                 ('y', 'Y:', '5', 4),
                                 ('type', 'Type:', 'General', 10)],
                         validators={'body_id': 'body'})

        self._section_header(inner, "Base Information")
        self._action_row(inner, "Base Status", ['base-status'],
                         [('id', 'Base ID:', '', 12)],
                         validators={'id': 'base'})

        # Note about starbases
        note = ttk.Label(inner,
                         text="Note: starbases are typically created during game setup or via the database.",
                         foreground="#666", font=("", 9, "italic"))
        note.pack(anchor=tk.W, padx=10, pady=(8, 4))

    # ========================================================================
    # Tab: Moderator
    # ========================================================================

    def _build_moderator_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Moderator")
        inner = self._scrollable_frame(tab)

        self._section_header(inner, "GM Account & NPCs")
        self._action_row(inner, "Add GM Account", ['add-gm'], [self._game_field()])
        self._action_row(inner, "Add GM Prefect", ['add-gm-prefect'],
                         [('name', 'Name:', '', 16),
                          ('faction', 'Faction:', '', 4),
                          ('starbase', 'Starbase:', '', 10)],
                         validators={'starbase': 'starbase'})
        self._action_row(inner, "Add GM Ship", ['add-gm-ship'],
                         [('prefect', 'Prefect:', '', 10),
                          ('ship-name', 'Ship Name:', '', 14),
                          ('system', 'Sys:', '101', 5),
                          ('col', 'Col:', 'M', 4),
                          ('row', 'Row:', '13', 4),
                          ('hull-type', 'Hull:', 'Commercial', 10)],
                         validators={'prefect': 'prefect', 'system': 'system'})

        self._section_header(inner, "Action Requests")
        self._action_row(inner, "List Pending Actions", ['list-actions'])
        self._action_row(inner, "Respond to Action", ['respond-action'],
                         [('action-id', 'Action ID:', '', 10),
                          ('response', 'Response:', '', 30)])

        self._section_header(inner, "Order Manipulation")
        self._action_row(inner, "Inject Order", ['inject-order'],
                         [('ship', 'Ship:', '', 10),
                          ('command', 'Command:', '', 16)],
                         validators={'ship': 'ship'})
        self._action_row(inner, "Edit Order", ['edit-order'],
                         [('order-id', 'Order ID:', '', 10),
                          ('command', 'Command:', '', 16)])
        self._action_row(inner, "Delete Order", ['delete-order'],
                         [('order-id', 'Order ID:', '', 10)])

        def submit_builder(entries):
            email = entries['email'].get().strip()
            orders = entries['orders_file'].get().strip()
            if not email or not orders:
                raise ValueError("Both email and orders file are required")
            return ['submit-orders', '--email', email, orders]
        self._action_row(inner, "Submit Orders", [], custom_builder=submit_builder,
                         fields=[('email', 'Email:', '', 20),
                                 ('orders_file', 'Orders file:', '', 28)])

        self._section_header(inner, "DB Recalculation")
        self._action_row(inner, "Recalc All Ships", ['recalc-ships'])
        self._action_row(inner, "Recalc All Bases", ['recalc-bases'])

        self._section_header(inner, "Database Utilities")
        self._action_row(inner, "Setup Game", ['setup-game'])
        self._action_row(inner, "Split Legacy DB", ['split-db'])

    # ========================================================================
    # Tab: Previews
    # ========================================================================

    def _build_previews_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Previews & Maps")
        inner = self._scrollable_frame(tab)

        self._section_header(inner, "Turn Reports (Live State)")
        self._action_row(inner, "Preview Ship Report", ['preview-ship'],
                         [('ship', 'Ship ID:', '', 12)],
                         validators={'ship': 'ship'})
        self._action_row(inner, "Preview Base Report", ['preview-base'],
                         [('base', 'Base ID:', '', 12)],
                         validators={'base': 'base'})

        self._section_header(inner, "Maps")
        self._action_row(inner, "Show System Map", ['show-map'],
                         [('system', 'System:', '101', 6)],
                         validators={'system': 'system'})
        self._action_row(inner, "Show Surface Map", ['show-surface'],
                         [('body', 'Body ID:', '', 12)],
                         validators={'body': 'body'})

        self._section_header(inner, "Status & Lists")
        self._action_row(inner, "Show Ship Status", ['show-status'],
                         [('ship', 'Ship ID:', '', 12)],
                         validators={'ship': 'ship'})
        self._action_row(inner, "List Ships", ['list-ships'])

    # ========================================================================
    # Tab: DB Browser (read-only)
    # ========================================================================

    def _build_dbbrowser_tab(self):
        tab = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(tab, text="DB Browser")

        # --- Top row: DB selector + refresh ---
        top = ttk.Frame(tab)
        top.pack(fill=tk.X, pady=(0, 4))

        ttk.Label(top, text="Database:", font=("", 10, "bold")).pack(side=tk.LEFT, padx=(0, 6))
        self.db_var = tk.StringVar(value='game_state')
        ttk.Radiobutton(top, text="game_state.db", variable=self.db_var,
                        value='game_state',
                        command=self._dbbrowser_load_tables).pack(side=tk.LEFT)
        ttk.Radiobutton(top, text="universe.db", variable=self.db_var,
                        value='universe',
                        command=self._dbbrowser_load_tables).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(top, text="Refresh", width=10,
                   command=self._dbbrowser_load_tables).pack(side=tk.LEFT, padx=(12, 0))

        ttk.Label(top, text="(read-only access)",
                  foreground="#888", font=("", 8, "italic")).pack(side=tk.RIGHT)

        # --- Middle: tables list + query editor side by side ---
        middle = ttk.Frame(tab)
        middle.pack(fill=tk.X, pady=4)

        # Tables list (left)
        tables_frame = ttk.LabelFrame(middle, text="Tables (double-click to browse)", padding=4)
        tables_frame.pack(side=tk.LEFT, fill=tk.Y)

        lb_wrap = ttk.Frame(tables_frame)
        lb_wrap.pack(fill=tk.Y)
        self.tables_listbox = tk.Listbox(lb_wrap, width=22, height=8,
                                          font=("Consolas", 9),
                                          activestyle='none', exportselection=False)
        self.tables_listbox.pack(side=tk.LEFT, fill=tk.Y)
        self.tables_listbox.bind("<Double-Button-1>", self._dbbrowser_table_dblclick)
        tables_sb = ttk.Scrollbar(lb_wrap, orient=tk.VERTICAL,
                                   command=self.tables_listbox.yview)
        tables_sb.pack(side=tk.LEFT, fill=tk.Y)
        self.tables_listbox.config(yscrollcommand=tables_sb.set)

        schema_btn = ttk.Button(tables_frame, text="Show Schema", width=16,
                                 command=self._dbbrowser_show_schema)
        schema_btn.pack(fill=tk.X, pady=(4, 0))

        # Query editor (right)
        query_frame = ttk.LabelFrame(middle, text="SQL Query  (Ctrl+Enter to run)", padding=4)
        query_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0))

        self.query_text = scrolledtext.ScrolledText(
            query_frame, height=7, font=("Consolas", 9), wrap=tk.WORD,
        )
        self.query_text.pack(fill=tk.BOTH, expand=True)
        self.query_text.insert(
            "1.0",
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
        )
        self.query_text.bind("<Control-Return>", self._dbbrowser_run_on_ctrl_enter)

        q_btn_row = ttk.Frame(query_frame)
        q_btn_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(q_btn_row, text="▶ Run Query", width=16,
                   command=self._dbbrowser_run_query).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(q_btn_row, text="Clear",
                   command=lambda: self.query_text.delete("1.0", tk.END)
                   ).pack(side=tk.LEFT, padx=4)

        # --- Results ---
        results_frame = ttk.LabelFrame(tab, text="Results", padding=4)
        results_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        tree_wrap = ttk.Frame(results_frame)
        tree_wrap.pack(fill=tk.BOTH, expand=True)

        self.results_tree = ttk.Treeview(tree_wrap, show='headings', height=8)
        vsb = ttk.Scrollbar(tree_wrap, orient=tk.VERTICAL,
                             command=self.results_tree.yview)
        hsb = ttk.Scrollbar(tree_wrap, orient=tk.HORIZONTAL,
                             command=self.results_tree.xview)
        self.results_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.results_tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        tree_wrap.grid_rowconfigure(0, weight=1)
        tree_wrap.grid_columnconfigure(0, weight=1)

        self.result_status = ttk.Label(tab, text="No query run yet.",
                                        foreground="#666",
                                        font=("", 9, "italic"))
        self.result_status.pack(anchor=tk.W, pady=(4, 0))

        # Populate tables list on first show
        self._dbbrowser_load_tables()

    # ----- DB Browser helpers -----

    def _dbbrowser_get_readonly_conn(self):
        """Open the selected database in read-only URI mode."""
        import sqlite3
        which = self.db_var.get()
        path = self._db_path(which)
        if not path.exists():
            return None, f"Database not found: {path}"
        try:
            uri = f"file:{path.as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            return conn, None
        except Exception as ex:
            return None, str(ex)

    def _dbbrowser_load_tables(self):
        self.tables_listbox.delete(0, tk.END)
        conn, err = self._dbbrowser_get_readonly_conn()
        if not conn:
            self.tables_listbox.insert(tk.END, "(error)")
            if hasattr(self, 'result_status'):
                self.result_status.config(text=err, foreground="#e74c3c")
            return
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            ).fetchall()
            for r in rows:
                self.tables_listbox.insert(tk.END, r['name'])
            if hasattr(self, 'result_status'):
                self.result_status.config(
                    text=f"Connected to {self.db_var.get()}.db "
                         f"({len(rows)} tables)",
                    foreground="#666",
                )
        finally:
            conn.close()

    def _dbbrowser_table_dblclick(self, event):
        sel = self.tables_listbox.curselection()
        if not sel:
            return
        table = self.tables_listbox.get(sel[0])
        if table.startswith("("):
            return
        self.query_text.delete("1.0", tk.END)
        self.query_text.insert("1.0", f"SELECT * FROM {table} LIMIT 100;")
        self._dbbrowser_run_query()

    def _dbbrowser_show_schema(self):
        sel = self.tables_listbox.curselection()
        if not sel:
            messagebox.showinfo("Schema", "Select a table from the list first.")
            return
        table = self.tables_listbox.get(sel[0])
        if table.startswith("("):
            return
        self.query_text.delete("1.0", tk.END)
        self.query_text.insert("1.0", f"PRAGMA table_info({table});")
        self._dbbrowser_run_query()

    def _dbbrowser_run_on_ctrl_enter(self, event):
        self._dbbrowser_run_query()
        return 'break'  # suppress the newline insert

    def _dbbrowser_run_query(self):
        # Clear previous results
        for item in self.results_tree.get_children():
            self.results_tree.delete(item)
        self.results_tree['columns'] = ()

        sql = self.query_text.get("1.0", tk.END).strip()
        if not sql:
            self.result_status.config(text="Empty query.", foreground="#888")
            return

        sql_clean = sql.rstrip(';').strip()

        # Safety gate: reject write statements up-front. The read-only URI
        # connection would block them anyway, but this gives a clearer message.
        first_word = sql_clean.lower().lstrip().split(None, 1)[0] if sql_clean else ""
        forbidden = {'insert', 'update', 'delete', 'drop', 'alter', 'create',
                     'replace', 'truncate', 'attach', 'detach', 'vacuum', 'reindex'}
        if first_word in forbidden:
            self.result_status.config(
                text=f"Rejected '{first_word.upper()}': only SELECT / PRAGMA / EXPLAIN allowed.",
                foreground="#e74c3c",
            )
            return

        conn, err = self._dbbrowser_get_readonly_conn()
        if not conn:
            self.result_status.config(text=f"Error: {err}", foreground="#e74c3c")
            return

        try:
            cursor = conn.execute(sql_clean)
            desc = cursor.description
            if not desc:
                self.result_status.config(text="Query completed (no result set).",
                                           foreground="#666")
                return

            col_names = [d[0] for d in desc]
            self.results_tree['columns'] = col_names
            for col in col_names:
                self.results_tree.heading(col, text=col)
                # Width heuristic: use column name length, constrained
                width = max(70, min(250, len(col) * 10 + 20))
                self.results_tree.column(col, width=width, anchor=tk.W, stretch=False)

            # Cap at 1000 rows for UI responsiveness
            rows = cursor.fetchmany(1000)
            for row in rows:
                values = tuple(
                    (str(v) if v is not None else "NULL") for v in row
                )
                self.results_tree.insert('', tk.END, values=values)

            n = len(rows)
            cap_note = " (capped at 1000)" if n == 1000 else ""
            self.result_status.config(
                text=f"{n} row{'' if n == 1 else 's'} returned{cap_note}.",
                foreground="#2ecc71",
            )
        except Exception as ex:
            self.result_status.config(text=f"SQL error: {ex}", foreground="#e74c3c")
        finally:
            conn.close()

    # ========================================================================
    # Tab: Settings
    # ========================================================================

    def _build_settings_tab(self):
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Settings")

        ttk.Label(tab, text="GM Console Settings",
                  font=("", 13, "bold")).pack(anchor=tk.W, pady=(0, 12))

        def _setting_row(label_text, var, browse_kind=None):
            row = ttk.Frame(tab)
            row.pack(fill=tk.X, pady=4)
            ttk.Label(row, text=label_text, width=22).pack(side=tk.LEFT)
            ttk.Entry(row, textvariable=var, width=55).pack(side=tk.LEFT)
            if browse_kind == 'dir':
                ttk.Button(row, text="Browse",
                           command=lambda: self._browse_dir(var)).pack(side=tk.LEFT, padx=4)
            elif browse_kind == 'file':
                ttk.Button(row, text="Browse",
                           command=lambda: self._browse_file(var)).pack(side=tk.LEFT, padx=4)

        self.game_id_var = tk.StringVar(value=self.config_data['game_id'])
        _setting_row("Default Game ID:", self.game_id_var)

        self.project_var = tk.StringVar(value=self.config_data['project_dir'])
        _setting_row("Project Directory:", self.project_var, 'dir')

        self.python_var = tk.StringVar(value=self.config_data['python_exe'])
        _setting_row("Python Executable:", self.python_var, 'file')

        self.creds_var = tk.StringVar(value=self.config_data.get('credentials_path', ''))
        _setting_row("Gmail Credentials:", self.creds_var, 'file')

        btn_frame = ttk.Frame(tab)
        btn_frame.pack(pady=20)
        ttk.Button(btn_frame, text="Save Settings", width=18,
                   command=self._save_settings).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="Test Gmail Connection", width=22,
                   command=self._test_gmail).pack(side=tk.LEFT, padx=4)

        ttk.Label(tab, text=f"Config file: {CONFIG_FILE}",
                  foreground="#666", font=("", 8)).pack(anchor=tk.W, pady=(20, 0))

    def _browse_dir(self, var):
        path = filedialog.askdirectory(initialdir=var.get() or ".")
        if path:
            var.set(path)

    def _browse_file(self, var):
        path = filedialog.askopenfilename(initialdir=str(Path(var.get() or ".").parent))
        if path:
            var.set(path)

    def _save_settings(self):
        self.config_data['game_id'] = self.game_id_var.get().strip()
        self.config_data['project_dir'] = self.project_var.get().strip()
        self.config_data['python_exe'] = self.python_var.get().strip()
        self.config_data['credentials_path'] = self.creds_var.get().strip()
        save_config(self.config_data)
        self.game_label.config(text=self.config_data['game_id'])
        self._append("Settings saved.\n", "ok")
        messagebox.showinfo("Settings", "Settings saved successfully.")

    # ========================================================================
    # Subprocess execution
    # ========================================================================

    def _run_pbem(self, args, label, on_complete=None):
        if self.process is not None and self.process.poll() is None:
            messagebox.showwarning("Busy", "Another command is currently running.\nUse Cancel Process to stop it first.")
            return

        cmd = [self.config_data['python_exe'], '-u', str(PBEM_SCRIPT)] + args
        self._append(f"\n>>> {label}\n", "cmd")
        self._append(f"    {' '.join(cmd[2:])}\n", "info")
        self.status_label.config(text=f"Running: {label}", foreground="orange")
        self._current_callback = on_complete

        def worker():
            try:
                self.process = subprocess.Popen(
                    cmd,
                    cwd=self.config_data['project_dir'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                for line in self.process.stdout:
                    self.output_queue.put(('out', line))
                self.process.wait()
                rc = self.process.returncode
                if rc == 0:
                    self.output_queue.put(('done', "[done: exit 0]\n"))
                else:
                    self.output_queue.put(('err', f"[failed: exit {rc}]\n"))
            except FileNotFoundError as ex:
                self.output_queue.put(('err', f"[error: {ex}]\nCheck Settings - Python or Project paths may be wrong.\n"))
            except Exception as ex:
                self.output_queue.put(('err', f"[error: {ex}]\n"))
            finally:
                self.process = None

        threading.Thread(target=worker, daemon=True).start()

    def _poll_output(self):
        try:
            while True:
                kind, text = self.output_queue.get_nowait()
                if kind == 'out':
                    self._append(text)
                elif kind == 'done':
                    self._append(text, "ok")
                    self.status_label.config(text="Ready", foreground="darkgreen")
                    cb = self._current_callback
                    self._current_callback = None
                    if cb:
                        try:
                            cb(True)
                        except Exception as ex:
                            self._append(f"[callback error: {ex}]\n", "err")
                elif kind == 'err':
                    self._append(text, "err")
                    self.status_label.config(text="Error", foreground="red")
                    cb = self._current_callback
                    self._current_callback = None
                    if cb:
                        try:
                            cb(False)
                        except Exception as ex:
                            self._append(f"[callback error: {ex}]\n", "err")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_output)

    def _cancel_process(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self._append("\n[cancelled by user]\n", "err")
                self.status_label.config(text="Cancelled", foreground="red")
            except Exception as ex:
                self._append(f"\n[cancel failed: {ex}]\n", "err")
        else:
            self._append("[no process running]\n", "info")

    # ========================================================================
    # Console
    # ========================================================================

    def _append(self, text, tag=None):
        self.console.config(state=tk.NORMAL)
        if tag:
            self.console.insert(tk.END, text, tag)
        else:
            self.console.insert(tk.END, text)
        self.console.see(tk.END)
        self.console.config(state=tk.DISABLED)

    def _clear_console(self):
        self.console.config(state=tk.NORMAL)
        self.console.delete('1.0', tk.END)
        self.console.config(state=tk.DISABLED)


# ============================================================================
# Entry point
# ============================================================================

def main():
    if not PBEM_SCRIPT.exists():
        print(f"Error: pbem.py not found at {PBEM_SCRIPT}")
        print("Run gm_gui.py from the project root directory.")
        sys.exit(1)

    root = tk.Tk()
    try:
        style = ttk.Style()
        themes = style.theme_names()
        for preferred in ('vista', 'winnative', 'clam', 'alt'):
            if preferred in themes:
                style.theme_use(preferred)
                break
    except Exception:
        pass

    StellarDominionGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
