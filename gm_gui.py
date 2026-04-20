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

# Ensure stellar_dominion modules are importable for the DB editors
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


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
        self._build_combat_tab()
        self._build_previews_tab()
        self._build_dbbrowser_tab()
        self._build_ship_editor_tab()
        self._build_base_editor_tab()
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

        self.wizard_reply_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(game_row, text="Send reply on Fetch Mail",
                        variable=self.wizard_reply_var
                        ).pack(side=tk.LEFT, padx=(16, 0))

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

        # Fetch Mail can optionally send an acknowledgement reply
        if cmd == 'fetch-mail' and getattr(self, 'wizard_reply_var', None) and self.wizard_reply_var.get():
            full_args.append('--reply')

        self._run_pbem(full_args, f"Wizard: {name}",
                       on_complete=self._wizard_stage_complete)

    def _wizard_stage_complete(self, success):
        if not self.wizard_active:
            return  # aborted mid-run

        # After Run Turn, verify the turn actually resolved (not auto-held).
        # run-turn exits 0 even on auto-hold, so we check the DB directly.
        if success:
            stage_name = self.WIZARD_STAGES[self.wizard_stage][0]
            if stage_name == "Run Turn":
                held_reason = self._wizard_check_turn_held()
                if held_reason:
                    self._append(f"[wizard: {held_reason}]\n", "err")
                    self._wizard_set_stage_status(self.wizard_stage, 'error')
                    self.wizard_status_text.config(
                        text=f"Turn held — {held_reason}. Resolve pending GM items, "
                             "then click Next to retry Run Turn.",
                        foreground="#e74c3c",
                    )
                    self.wizard_auto = False
                    self.wizard_next_btn.config(state=tk.NORMAL)
                    return

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

    def _wizard_check_turn_held(self):
        """
        Query game_state.db to see if the current turn is held.
        Returns a reason string if held, or None if safe to proceed.
        """
        import sqlite3
        try:
            path = self._db_path('game_state')
            if not path.exists():
                return None  # can't check — proceed rather than block
            uri = f"file:{path.as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row

            game_id = (self.wizard_game.get().strip()
                       or self.config_data['game_id'])
            row = conn.execute(
                "SELECT turn_status FROM games WHERE game_id = ?",
                (game_id,)
            ).fetchone()

            if not row:
                conn.close()
                return None

            status = row['turn_status']
            if status != 'held':
                conn.close()
                return None

            # Turn is held — find out why
            pending_actions = conn.execute("""
                SELECT COUNT(*) FROM moderator_actions
                WHERE game_id = ? AND status = 'pending'
            """, (game_id,)).fetchone()[0]

            pending_factions = conn.execute("""
                SELECT COUNT(*) FROM faction_requests
                WHERE game_id = ? AND status = 'pending'
            """, (game_id,)).fetchone()[0]

            conn.close()

            parts = []
            if pending_actions:
                parts.append(f"{pending_actions} moderator action(s)")
            if pending_factions:
                parts.append(f"{pending_factions} faction request(s)")
            if parts:
                return f"turn auto-held with {' and '.join(parts)} pending"
            return "turn auto-held (reason unclear — check Turn Ops tab)"
        except Exception as ex:
            # On any error, err on the side of letting the wizard continue
            # rather than blocking a legitimate run.
            self._append(f"[wizard: hold check failed: {ex}]\n", "info")
            return None

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
            reply_var = entries.get('reply_var')
            if reply_var and reply_var.get():
                args.append('--reply')
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

        # Fetch Mail — custom row with a Reply checkbox
        fm_row = ttk.Frame(inner, padding=(5, 2))
        fm_row.pack(fill=tk.X, padx=2, pady=1)

        fm_entries = {}
        fm_btn = ttk.Button(fm_row, text="Fetch Mail", width=22)
        fm_btn.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Label(fm_row, text="Game:").pack(side=tk.LEFT, padx=(4, 2))
        e = ttk.Entry(fm_row, width=14)
        e.insert(0, self.config_data['game_id'])
        e.pack(side=tk.LEFT)
        fm_entries['game'] = e

        ttk.Label(fm_row, text="Inbox:").pack(side=tk.LEFT, padx=(4, 2))
        e = ttk.Entry(fm_row, width=14)
        e.insert(0, './inbox')
        e.pack(side=tk.LEFT)
        fm_entries['inbox'] = e

        fm_entries['reply_var'] = tk.BooleanVar(value=False)
        ttk.Checkbutton(fm_row, text="Send reply",
                        variable=fm_entries['reply_var']
                        ).pack(side=tk.LEFT, padx=(6, 0))

        def run_fetch_mail():
            args = fetch_mail_builder(fm_entries)
            self._run_pbem(args, "Fetch Mail")
        fm_btn.config(command=run_fetch_mail)

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

        def add_starbase_builder(entries):
            args = ['add-starbase']
            for k in ('base_id', 'surface_port_id', 'name'):
                v = entries[k].get().strip()
                if not v:
                    raise ValueError(f"{k} is required")
                args.append(v)
            complexes = entries['complexes'].get().strip()
            if complexes:
                args.extend(['--complexes', complexes])
            if entries['market_var'].get():
                args.append('--market')
            return args

        # Custom row for starbase — needs a checkbox for --market
        sb_row = ttk.Frame(inner, padding=(5, 2))
        sb_row.pack(fill=tk.X, padx=2, pady=1)

        sb_entries = {}
        sb_btn = ttk.Button(sb_row, text="Add Starbase", width=22)
        sb_btn.pack(side=tk.LEFT, padx=(0, 8))

        for key, lbl, default, width in [
            ('base_id', 'Base ID:', '', 10),
            ('surface_port_id', 'Port ID:', '', 10),
            ('name', 'Name:', '', 14),
            ('complexes', 'Cx:', '0', 4),
        ]:
            ttk.Label(sb_row, text=lbl).pack(side=tk.LEFT, padx=(4, 2))
            e = ttk.Entry(sb_row, width=width)
            if default:
                e.insert(0, default)
            e.pack(side=tk.LEFT)
            sb_entries[key] = e

        sb_entries['market_var'] = tk.BooleanVar(value=True)
        ttk.Checkbutton(sb_row, text="Market", variable=sb_entries['market_var']
                        ).pack(side=tk.LEFT, padx=(6, 0))

        def run_starbase():
            # Validate surface_port_id exists (as a port)
            pid = sb_entries['surface_port_id'].get().strip()
            if pid:
                ok, err = self._validate_id('port', pid)
                if not ok:
                    self._append(f"\n>>> Add Starbase\n", "cmd")
                    self._append(f"[validation failed] {err}\n", "err")
                    messagebox.showerror("Validation failed", err)
                    return
            try:
                args = add_starbase_builder(sb_entries)
            except Exception as ex:
                messagebox.showerror("Input error", str(ex))
                return
            self._run_pbem(args, "Add Starbase")

        sb_btn.config(command=run_starbase)

        self._section_header(inner, "Base Information")
        self._action_row(inner, "Base Status", ['base-status'],
                         [('id', 'Base ID:', '', 12)],
                         validators={'id': 'base'})

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

        # Inject Order — custom row with Ship/Prefect radio
        io_row = ttk.Frame(inner, padding=(5, 2))
        io_row.pack(fill=tk.X, padx=2, pady=1)

        io_btn = ttk.Button(io_row, text="Inject Order", width=22)
        io_btn.pack(side=tk.LEFT, padx=(0, 8))

        io_subject_var = tk.StringVar(value='ship')
        ttk.Radiobutton(io_row, text="Ship", variable=io_subject_var,
                        value='ship').pack(side=tk.LEFT, padx=(4, 2))
        ttk.Radiobutton(io_row, text="Prefect", variable=io_subject_var,
                        value='prefect').pack(side=tk.LEFT, padx=(0, 6))

        ttk.Label(io_row, text="ID:").pack(side=tk.LEFT, padx=(4, 2))
        io_id_entry = ttk.Entry(io_row, width=10)
        io_id_entry.pack(side=tk.LEFT)

        ttk.Label(io_row, text="Command:").pack(side=tk.LEFT, padx=(6, 2))
        io_cmd_entry = ttk.Entry(io_row, width=24)
        io_cmd_entry.pack(side=tk.LEFT)

        def run_inject_order():
            subject = io_subject_var.get()
            subject_id = io_id_entry.get().strip()
            cmd_text = io_cmd_entry.get().strip()
            if not subject_id or not cmd_text:
                messagebox.showerror("Input error",
                                      "Both ID and Command are required.")
                return
            # Validate the subject ID against the DB
            ok, err = self._validate_id(subject, subject_id)
            if not ok:
                self._append("\n>>> Inject Order\n", "cmd")
                self._append(f"[validation failed] {err}\n", "err")
                messagebox.showerror("Validation failed", err)
                return
            args = ['inject-order', f'--{subject}', subject_id,
                    '--command', cmd_text]
            self._run_pbem(args, "Inject Order")

        io_btn.config(command=run_inject_order)

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

    def _build_combat_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Combat")
        inner = self._scrollable_frame(tab)

        # ----- Engagement viewer -----
        self._section_header(inner, "Active Engagements")

        viewer_frame = ttk.Frame(inner, padding=5)
        viewer_frame.pack(fill=tk.X, padx=2, pady=1)

        btn_row = ttk.Frame(viewer_frame)
        btn_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(btn_row, text="Refresh", width=10,
                   command=self._combat_refresh_engagements).pack(side=tk.LEFT, padx=2)
        self.combat_show_all_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(btn_row, text="Show resolved/fled too",
                        variable=self.combat_show_all_var,
                        command=self._combat_refresh_engagements).pack(side=tk.LEFT, padx=6)

        tv_wrap = ttk.Frame(viewer_frame)
        tv_wrap.pack(fill=tk.X)
        self.combat_eng_tree = ttk.Treeview(
            tv_wrap, show='headings',
            columns=('id', 'loc', 'sys', 'status', 'started', 'parts'), height=7)
        for col, hd, w in [('id', 'Eng#', 55), ('loc', 'Loc', 55),
                            ('sys', 'Sys', 55), ('status', 'Status', 80),
                            ('started', 'Started', 80),
                            ('parts', 'Participants', 320)]:
            self.combat_eng_tree.heading(col, text=hd)
            self.combat_eng_tree.column(col, width=w, anchor=tk.W)
        eng_sb = ttk.Scrollbar(tv_wrap, orient=tk.VERTICAL,
                                command=self.combat_eng_tree.yview)
        self.combat_eng_tree.configure(yscrollcommand=eng_sb.set)
        self.combat_eng_tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        eng_sb.pack(side=tk.LEFT, fill=tk.Y)
        # Double-click or "View Log" button pops up the full round-by-round log
        self.combat_eng_tree.bind("<Double-1>", lambda e: self._combat_show_log_popup())

        # Buttons below tree: view log + end engagement
        btn_row = ttk.Frame(viewer_frame)
        btn_row.pack(fill=tk.X, pady=(6, 2))
        ttk.Button(btn_row, text="View Log for Selected", width=22,
                   command=self._combat_show_log_popup).pack(side=tk.LEFT, padx=2)
        ttk.Label(btn_row, text="(or double-click a row)",
                   foreground="#888", font=("", 8, "italic")).pack(side=tk.LEFT, padx=(4, 0))

        end_row = ttk.Frame(viewer_frame)
        end_row.pack(fill=tk.X, pady=(6, 2))
        ttk.Button(end_row, text="End Selected Engagement", width=26,
                   command=self._combat_end_selected).pack(side=tk.LEFT, padx=2)
        ttk.Label(end_row, text="Note:").pack(side=tk.LEFT, padx=(8, 2))
        self.combat_end_note = ttk.Entry(end_row, width=40)
        self.combat_end_note.pack(side=tk.LEFT)

        # ----- Combat GM commands -----
        self._section_header(inner, "GM Commands")

        self._action_row(inner, "List Engagements (active only)",
                         ['list-engagements'], [self._game_field()])

        self._action_row(inner, "List All Engagements",
                         ['list-engagements', '--all'], [self._game_field()])

        self._action_row(inner, "End Engagement",
                         ['end-engagement'],
                         [('engagement-id', 'Engagement ID:', '', 10),
                          ('note', 'Note:', '', 24)])

        self._action_row(inner, "Inject Attack (force combat)",
                         ['inject-attack'],
                         [('attacker', 'Attacker Ship:', '', 12),
                          ('target', 'Target Ship:', '', 12)],
                         validators={'attacker': 'ship', 'target': 'ship'})

        # Set Doctrine — custom row with choice dropdown
        dr_row = ttk.Frame(inner, padding=(5, 2))
        dr_row.pack(fill=tk.X, padx=2, pady=1)
        dr_btn = ttk.Button(dr_row, text="Set Doctrine", width=22)
        dr_btn.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(dr_row, text="Ship:").pack(side=tk.LEFT, padx=(4, 2))
        dr_ship_entry = ttk.Entry(dr_row, width=12)
        dr_ship_entry.pack(side=tk.LEFT)
        ttk.Label(dr_row, text="Doctrine:").pack(side=tk.LEFT, padx=(6, 2))
        dr_doctrine_var = tk.StringVar(value='defensive')
        dr_combo = ttk.Combobox(dr_row, textvariable=dr_doctrine_var,
                                 state='readonly', width=12,
                                 values=['aggressive', 'defensive', 'evasive'])
        dr_combo.pack(side=tk.LEFT)

        def run_set_doctrine():
            sid = dr_ship_entry.get().strip()
            doc = dr_doctrine_var.get()
            if not sid:
                messagebox.showerror("Input error", "Ship ID required.")
                return
            ok, err = self._validate_id('ship', sid)
            if not ok:
                self._append("\n>>> Set Doctrine\n", "cmd")
                self._append(f"[validation failed] {err}\n", "err")
                messagebox.showerror("Validation failed", err)
                return
            self._run_pbem(['set-doctrine', '--ship', sid, '--doctrine', doc],
                            "Set Doctrine")
        dr_btn.config(command=run_set_doctrine)

        # Show Lists — ship or base
        sl_row = ttk.Frame(inner, padding=(5, 2))
        sl_row.pack(fill=tk.X, padx=2, pady=1)
        sl_btn = ttk.Button(sl_row, text="Show Lists", width=22)
        sl_btn.pack(side=tk.LEFT, padx=(0, 8))
        sl_kind_var = tk.StringVar(value='ship')
        ttk.Radiobutton(sl_row, text="Ship", variable=sl_kind_var,
                        value='ship').pack(side=tk.LEFT, padx=(4, 2))
        ttk.Radiobutton(sl_row, text="Base", variable=sl_kind_var,
                        value='base').pack(side=tk.LEFT, padx=(0, 6))
        ttk.Label(sl_row, text="ID:").pack(side=tk.LEFT, padx=(4, 2))
        sl_id_entry = ttk.Entry(sl_row, width=12)
        sl_id_entry.pack(side=tk.LEFT)

        def run_show_lists():
            kind = sl_kind_var.get()
            sid = sl_id_entry.get().strip()
            if not sid:
                messagebox.showerror("Input error", "ID required.")
                return
            self._run_pbem(['show-lists', f'--{kind}', sid], "Show Lists")
        sl_btn.config(command=run_show_lists)

        # Initial load
        self._combat_refresh_engagements()

    # ----- Combat tab helpers -----

    def _combat_refresh_engagements(self):
        """Populate the engagements treeview from DB."""
        try:
            from db.database import get_connection
        except Exception as e:
            self._append(f"[error opening DB] {e}\n", "err")
            return
        # Clear existing
        for row_id in self.combat_eng_tree.get_children():
            self.combat_eng_tree.delete(row_id)
        show_all = self.combat_show_all_var.get()
        try:
            conn = get_connection()
            if show_all:
                engs = conn.execute(
                    "SELECT * FROM combat_engagements "
                    "WHERE game_id = ? ORDER BY engagement_id DESC",
                    (self.config_data['game_id'],)
                ).fetchall()
            else:
                engs = conn.execute(
                    "SELECT * FROM combat_engagements "
                    "WHERE game_id = ? AND status = 'active' "
                    "ORDER BY engagement_id DESC",
                    (self.config_data['game_id'],)
                ).fetchall()
            for e in engs:
                loc = f"{e['grid_col']}{e['grid_row']:02d}" if e['grid_col'] else '?'
                started = f"{e['started_turn_year']}.{e['started_turn_week']}"
                # Build participants summary
                parts = conn.execute(
                    "SELECT * FROM combat_participants WHERE engagement_id = ?",
                    (e['engagement_id'],)
                ).fetchall()
                part_bits = []
                for p in parts:
                    if p['participant_kind'] == 'ship':
                        nrow = conn.execute(
                            "SELECT name, integrity, max_integrity FROM ships WHERE ship_id = ?",
                            (p['participant_id_value'],)
                        ).fetchone()
                        nm = nrow['name'] if nrow else f"#{p['participant_id_value']}"
                        hp_str = ''
                        if nrow and nrow['max_integrity']:
                            pct = nrow['integrity'] / nrow['max_integrity'] * 100
                            hp_str = f" {nrow['integrity']:.0f}/{nrow['max_integrity']:.0f}"
                    else:
                        tbl_map = {'starbase': ('starbases', 'base_id'),
                                    'port': ('surface_ports', 'port_id'),
                                    'outpost': ('outposts', 'outpost_id')}
                        tbl, idcol = tbl_map.get(p['participant_kind'], (None, None))
                        nrow = conn.execute(
                            f"SELECT name FROM {tbl} WHERE {idcol} = ?",
                            (p['participant_id_value'],)
                        ).fetchone() if tbl else None
                        nm = nrow['name'] if nrow else f"{p['participant_kind']}#{p['participant_id_value']}"
                        hp_str = ''
                    stat = p['status'][:1].upper() if p['status'] else '?'
                    part_bits.append(f"{nm}[{stat}]{hp_str}")
                self.combat_eng_tree.insert(
                    '', tk.END,
                    values=(e['engagement_id'], loc, e['system_id'],
                             (e['status'] or '').upper(),
                             started, ', '.join(part_bits))
                )
            conn.close()
        except Exception as e:
            self._append(f"[error loading engagements] {e}\n", "err")

    def _combat_end_selected(self):
        sel = self.combat_eng_tree.selection()
        if not sel:
            messagebox.showerror("No selection", "Pick an engagement first.")
            return
        values = self.combat_eng_tree.item(sel[0])['values']
        eng_id = str(values[0])
        status = str(values[3]).lower()
        if status != 'active':
            messagebox.showinfo("Not active",
                                  f"Engagement #{eng_id} is already {status}.")
            return
        note = self.combat_end_note.get().strip() or "GM ended via GUI"
        if not messagebox.askyesno("Confirm",
                                      f"Force-end engagement #{eng_id}?"):
            return
        self._run_pbem(['end-engagement', '--engagement-id', eng_id,
                         '--note', note],
                        "End Engagement")
        self.root.after(600, self._combat_refresh_engagements)

    def _combat_show_log_popup(self):
        """Open a modal showing the full round-by-round log for the selected engagement."""
        sel = self.combat_eng_tree.selection()
        if not sel:
            messagebox.showerror("No selection", "Pick an engagement first.")
            return
        values = self.combat_eng_tree.item(sel[0])['values']
        eng_id = int(values[0])
        try:
            from db.database import get_connection
            conn = get_connection()
            eng = conn.execute(
                "SELECT * FROM combat_engagements WHERE engagement_id = ?",
                (eng_id,)
            ).fetchone()
            if not eng:
                conn.close()
                messagebox.showerror("Not found", f"Engagement #{eng_id} not found.")
                return
            parts = conn.execute(
                "SELECT * FROM combat_participants WHERE engagement_id = ? "
                "ORDER BY participant_id",
                (eng_id,)
            ).fetchall()
            log = conn.execute(
                "SELECT * FROM combat_log WHERE engagement_id = ? "
                "ORDER BY turn_year, turn_week, round_number, log_id",
                (eng_id,)
            ).fetchall()

            # Build name lookup for participants (so the log reads nicely)
            name_cache = {}
            for p in parts:
                pk, pv = p['participant_kind'], p['participant_id_value']
                if pk == 'ship':
                    r = conn.execute(
                        "SELECT name FROM ships WHERE ship_id = ?", (pv,)
                    ).fetchone()
                else:
                    tbl_map = {'starbase': ('starbases', 'base_id'),
                                'port': ('surface_ports', 'port_id'),
                                'outpost': ('outposts', 'outpost_id')}
                    tbl, idcol = tbl_map.get(pk, (None, None))
                    r = conn.execute(
                        f"SELECT name FROM {tbl} WHERE {idcol} = ?", (pv,)
                    ).fetchone() if tbl else None
                name_cache[(pk, pv)] = r['name'] if r else f"{pk}#{pv}"
            conn.close()
        except Exception as ex:
            messagebox.showerror("Load error",
                                  f"Could not load engagement #{eng_id}:\n{ex}")
            return

        # Build the popup window
        popup = tk.Toplevel(self.root)
        popup.title(f"Combat Log — Engagement #{eng_id}")
        popup.geometry("720x520")

        header = ttk.Frame(popup, padding=8)
        header.pack(fill=tk.X)
        ttk.Label(header,
                   text=f"Engagement #{eng_id} — {(eng['status'] or '?').upper()}",
                   font=("", 11, "bold")).pack(anchor=tk.W)
        loc_txt = f"{eng['grid_col']}{eng['grid_row']:02d}" if eng['grid_col'] else '?'
        ttk.Label(header,
                   text=(f"Location: {loc_txt} system {eng['system_id']}   "
                         f"Started: {eng['started_turn_year']}.{eng['started_turn_week']} "
                         f"R{eng['started_on_round']}   "
                         f"Last active: {eng['last_active_turn_year']}."
                         f"{eng['last_active_turn_week']}"),
                   foreground="#555").pack(anchor=tk.W)
        if eng['resolution']:
            ttk.Label(header, text=f"Resolution: {eng['resolution']}",
                       foreground="#333").pack(anchor=tk.W)

        # Participants summary
        parts_frame = ttk.LabelFrame(popup, text="Participants", padding=6)
        parts_frame.pack(fill=tk.X, padx=8, pady=(0, 4))
        for p in parts:
            pname = name_cache.get((p['participant_kind'], p['participant_id_value']),
                                     f"#{p['participant_id_value']}")
            joined = (f"{p['joined_turn_year']}.{p['joined_turn_week']} "
                       f"R{p['joined_on_round']}" if p['joined_turn_year'] else '?')
            left = ""
            if p['left_turn_year']:
                left = (f"  left {p['left_turn_year']}.{p['left_turn_week']} "
                         f"R{p['left_on_round']}")
            int_join = f"{p['integrity_at_join']:.0f}" if p['integrity_at_join'] is not None else '?'
            int_end = f"{p['integrity_at_end']:.0f}" if p['integrity_at_end'] is not None else '-'
            ttk.Label(parts_frame,
                       text=(f"  {p['participant_kind']:8s} {pname} "
                             f"({p['participant_id_value']})  "
                             f"status: {(p['status'] or '?').upper():10s}  "
                             f"HP: {int_join} -> {int_end}  "
                             f"joined: {joined}{left}"),
                       font=("Consolas", 9)).pack(anchor=tk.W)

        # Scrollable log
        log_frame = ttk.LabelFrame(popup, text="Round-by-Round Log", padding=4)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        txt = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD,
                                          bg="#1e1e1e", fg="#d4d4d4",
                                          font=("Consolas", 9),
                                          insertbackground="white")
        txt.pack(fill=tk.BOTH, expand=True)
        txt.tag_config("round", foreground="#9cdcfe", font=("Consolas", 9, "bold"))
        txt.tag_config("engage", foreground="#dcdcaa")
        txt.tag_config("fire", foreground="#ffffff")
        txt.tag_config("destroyed", foreground="#f48771", font=("Consolas", 9, "bold"))
        txt.tag_config("flee", foreground="#c586c0")
        txt.tag_config("move", foreground="#808080")

        cur_turn = None
        for le in log:
            tag = le['action'] if le['action'] in (
                'engage', 'fire', 'destroyed', 'flee', 'move') else None
            tk_tuple = (le['turn_year'], le['turn_week'])
            if tk_tuple != cur_turn:
                txt.insert(tk.END,
                             f"\n--- Turn {tk_tuple[0]}.{tk_tuple[1]} ---\n",
                             "round")
                cur_turn = tk_tuple
            actor = ''
            if le['actor_kind'] and le['actor_id']:
                actor = name_cache.get(
                    (le['actor_kind'], le['actor_id']),
                    f"{le['actor_kind']}#{le['actor_id']}")
            elif le['actor_kind']:
                actor = le['actor_kind']
            line = (f"R{le['round_number']}  {actor:15s}  "
                     f"{le['action']:10s}  {le['detail'] or ''}\n")
            txt.insert(tk.END, line, tag)
        txt.config(state=tk.DISABLED)

        ttk.Button(popup, text="Close",
                    command=popup.destroy).pack(pady=(0, 8))

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
    # Tab: Ship Editor
    # ========================================================================

    def _build_ship_editor_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Ship Editor")
        inner = self._scrollable_frame(tab)

        ttk.Label(inner,
                  text="⚠ Direct database editor. Changes are written immediately. "
                       "Stats auto-recalculate after every edit.",
                  foreground="#c87000", font=("", 9, "italic")).pack(
            anchor=tk.W, padx=5, pady=(5, 8))

        # ----- Ship picker -----
        pick = ttk.Frame(inner)
        pick.pack(fill=tk.X, padx=5, pady=(0, 8))
        ttk.Label(pick, text="Ship:", font=("", 10, "bold")).pack(side=tk.LEFT, padx=(0, 4))
        self.ship_ed_combo = ttk.Combobox(pick, width=34, state='readonly')
        self.ship_ed_combo.pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(pick, text="Load", width=8,
                   command=self._ship_ed_load_selected).pack(side=tk.LEFT, padx=2)
        ttk.Button(pick, text="Refresh List", width=14,
                   command=self._ship_ed_load_list).pack(side=tk.LEFT, padx=2)

        self.ship_ed_current_id = None

        # ----- Basic Info -----
        basic = ttk.LabelFrame(inner, text="Basic Info", padding=6)
        basic.pack(fill=tk.X, padx=5, pady=4)

        self.ship_ed_fields = {}
        BASIC_FIELDS = [
            ('name', 'Name:', 30),
            ('ship_size', 'Hull Size:', 10),
            ('hull_type', 'Hull Type:', 20),
            ('ship_class', 'Class:', 20),
            ('design', 'Design:', 20),
            ('system_id', 'System ID:', 10),
            ('grid_col', 'Grid Col:', 6),
            ('grid_row', 'Grid Row:', 6),
            ('tu_remaining', 'OC Remaining:', 10),
            ('armour', 'Armour:', 6),
        ]
        for key, label, width in BASIC_FIELDS:
            row = ttk.Frame(basic)
            row.pack(fill=tk.X, pady=2)
            ttk.Label(row, text=label, width=14).pack(side=tk.LEFT)
            e = ttk.Entry(row, width=width)
            e.pack(side=tk.LEFT, padx=(2, 10))
            self.ship_ed_fields[key] = e

        ttk.Button(basic, text="Save Basic Info", width=18,
                   command=self._ship_ed_save_basic).pack(anchor=tk.W, pady=(4, 0))

        # ----- Combat State (integrity + shield SP) -----
        cstate = ttk.LabelFrame(inner, text="Combat State", padding=6)
        cstate.pack(fill=tk.X, padx=5, pady=4)

        # Integrity row
        int_row = ttk.Frame(cstate)
        int_row.pack(fill=tk.X, pady=2)
        ttk.Label(int_row, text="Integrity:", width=12).pack(side=tk.LEFT)
        self.ship_ed_integrity_entry = ttk.Entry(int_row, width=8)
        self.ship_ed_integrity_entry.pack(side=tk.LEFT, padx=(2, 4))
        ttk.Label(int_row, text="/").pack(side=tk.LEFT)
        self.ship_ed_max_integrity_label = ttk.Label(int_row, text="-", width=10,
                                                       foreground="#555")
        self.ship_ed_max_integrity_label.pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(int_row, text="(max auto-computed from ship_size × hull multiplier)",
                   foreground="#888", font=("", 8, "italic")).pack(side=tk.LEFT)

        # Shield SP row
        sp_row = ttk.Frame(cstate)
        sp_row.pack(fill=tk.X, pady=2)
        ttk.Label(sp_row, text="Shield SP:", width=12).pack(side=tk.LEFT)
        self.ship_ed_shield_entry = ttk.Entry(sp_row, width=8)
        self.ship_ed_shield_entry.pack(side=tk.LEFT, padx=(2, 4))
        ttk.Label(sp_row, text="/").pack(side=tk.LEFT)
        self.ship_ed_max_shield_label = ttk.Label(sp_row, text="-", width=10,
                                                    foreground="#555")
        self.ship_ed_max_shield_label.pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(sp_row, text="(max auto-computed from installed shield generators)",
                   foreground="#888", font=("", 8, "italic")).pack(side=tk.LEFT)

        # Missiles row
        m_row = ttk.Frame(cstate)
        m_row.pack(fill=tk.X, pady=2)
        ttk.Label(m_row, text="Missiles:", width=12).pack(side=tk.LEFT)
        self.ship_ed_missiles_entry = ttk.Entry(m_row, width=8)
        self.ship_ed_missiles_entry.pack(side=tk.LEFT, padx=(2, 4))
        ttk.Label(m_row, text="/").pack(side=tk.LEFT)
        self.ship_ed_max_missiles_label = ttk.Label(m_row, text="-", width=10,
                                                      foreground="#555")
        self.ship_ed_max_missiles_label.pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(m_row, text="(max auto-computed from installed missile magazines)",
                   foreground="#888", font=("", 8, "italic")).pack(side=tk.LEFT)

        # Torpedoes row
        t_row = ttk.Frame(cstate)
        t_row.pack(fill=tk.X, pady=2)
        ttk.Label(t_row, text="Torpedoes:", width=12).pack(side=tk.LEFT)
        self.ship_ed_torpedoes_entry = ttk.Entry(t_row, width=8)
        self.ship_ed_torpedoes_entry.pack(side=tk.LEFT, padx=(2, 4))
        ttk.Label(t_row, text="/").pack(side=tk.LEFT)
        self.ship_ed_max_torpedoes_label = ttk.Label(t_row, text="-", width=10,
                                                       foreground="#555")
        self.ship_ed_max_torpedoes_label.pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(t_row, text="(max auto-computed from installed torpedo magazines)",
                   foreground="#888", font=("", 8, "italic")).pack(side=tk.LEFT)

        btn_row = ttk.Frame(cstate)
        btn_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(btn_row, text="Save Combat State", width=20,
                   command=self._ship_ed_save_combat_state).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="Repair to Full", width=14,
                   command=self._ship_ed_repair_full).pack(side=tk.LEFT, padx=2)

        # ----- Installed Components -----
        comp = ttk.LabelFrame(inner, text="Installed Components", padding=6)
        comp.pack(fill=tk.X, padx=5, pady=4)

        tv_wrap = ttk.Frame(comp)
        tv_wrap.pack(fill=tk.X)
        self.ship_comp_tree = ttk.Treeview(
            tv_wrap, show='headings',
            columns=('id', 'name', 'qty', 'st_each', 'st_total'), height=8)
        for col, hd, w in [('id', 'ID', 55), ('name', 'Name', 220),
                            ('qty', 'Qty', 50), ('st_each', 'ST Each', 70),
                            ('st_total', 'ST Total', 70)]:
            self.ship_comp_tree.heading(col, text=hd)
            self.ship_comp_tree.column(col, width=w, anchor=tk.W)
        comp_sb = ttk.Scrollbar(tv_wrap, orient=tk.VERTICAL,
                                 command=self.ship_comp_tree.yview)
        self.ship_comp_tree.configure(yscrollcommand=comp_sb.set)
        self.ship_comp_tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        comp_sb.pack(side=tk.LEFT, fill=tk.Y)

        ccr = ttk.Frame(comp)
        ccr.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(ccr, text="Component:").pack(side=tk.LEFT)
        self.ship_comp_combo = ttk.Combobox(ccr, width=30, state='readonly')
        self.ship_comp_combo.pack(side=tk.LEFT, padx=2)
        ttk.Label(ccr, text="Qty:").pack(side=tk.LEFT, padx=(6, 2))
        self.ship_comp_qty = ttk.Entry(ccr, width=5)
        self.ship_comp_qty.insert(0, "1")
        self.ship_comp_qty.pack(side=tk.LEFT)
        ttk.Button(ccr, text="Add", width=6,
                   command=self._ship_ed_add_component).pack(side=tk.LEFT, padx=(4, 2))
        ttk.Button(ccr, text="Set Qty", width=8,
                   command=self._ship_ed_set_component_qty).pack(side=tk.LEFT, padx=2)
        ttk.Button(ccr, text="Remove Selected", width=16,
                   command=self._ship_ed_remove_component).pack(side=tk.LEFT, padx=2)

        # ----- Cargo -----
        cargo = ttk.LabelFrame(inner, text="Cargo", padding=6)
        cargo.pack(fill=tk.X, padx=5, pady=4)

        cg_wrap = ttk.Frame(cargo)
        cg_wrap.pack(fill=tk.X)
        self.ship_cargo_tree = ttk.Treeview(
            cg_wrap, show='headings',
            columns=('id', 'name', 'qty', 'mass', 'total'), height=6)
        for col, hd, w in [('id', 'ID', 55), ('name', 'Name', 190),
                            ('qty', 'Qty', 60), ('mass', 'Mass/Unit', 80),
                            ('total', 'Total ST', 80)]:
            self.ship_cargo_tree.heading(col, text=hd)
            self.ship_cargo_tree.column(col, width=w, anchor=tk.W)
        cg_sb = ttk.Scrollbar(cg_wrap, orient=tk.VERTICAL,
                               command=self.ship_cargo_tree.yview)
        self.ship_cargo_tree.configure(yscrollcommand=cg_sb.set)
        self.ship_cargo_tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        cg_sb.pack(side=tk.LEFT, fill=tk.Y)

        cgr = ttk.Frame(cargo)
        cgr.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(cgr, text="Item:").pack(side=tk.LEFT)
        self.ship_cargo_combo = ttk.Combobox(cgr, width=30, state='readonly')
        self.ship_cargo_combo.pack(side=tk.LEFT, padx=2)
        ttk.Label(cgr, text="Qty:").pack(side=tk.LEFT, padx=(6, 2))
        self.ship_cargo_qty = ttk.Entry(cgr, width=6)
        self.ship_cargo_qty.insert(0, "1")
        self.ship_cargo_qty.pack(side=tk.LEFT)
        ttk.Button(cgr, text="Add", width=6,
                   command=self._ship_ed_add_cargo).pack(side=tk.LEFT, padx=(4, 2))
        ttk.Button(cgr, text="Set Qty", width=8,
                   command=self._ship_ed_set_cargo_qty).pack(side=tk.LEFT, padx=2)
        ttk.Button(cgr, text="Remove Selected", width=16,
                   command=self._ship_ed_remove_cargo).pack(side=tk.LEFT, padx=2)

        # ----- Combat -----
        combat_frame = ttk.LabelFrame(inner, text="Combat", padding=6)
        combat_frame.pack(fill=tk.X, padx=5, pady=4)

        # Doctrine selector
        doct_row = ttk.Frame(combat_frame)
        doct_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(doct_row, text="Doctrine:", width=12).pack(side=tk.LEFT)
        self.ship_ed_doctrine = ttk.Combobox(
            doct_row, state='readonly', width=14,
            values=['aggressive', 'defensive', 'evasive'])
        self.ship_ed_doctrine.set('defensive')
        self.ship_ed_doctrine.pack(side=tk.LEFT, padx=(2, 6))
        ttk.Button(doct_row, text="Save Doctrine", width=14,
                   command=self._ship_ed_save_doctrine).pack(side=tk.LEFT, padx=2)

        # Three lists side by side
        lists_row = ttk.Frame(combat_frame)
        lists_row.pack(fill=tk.X, pady=(4, 0))

        self.ship_ed_list_trees = {}
        for list_type in ('target', 'defend', 'avoid'):
            col = ttk.LabelFrame(lists_row, text=list_type.upper(), padding=4)
            col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=2)
            tv = ttk.Treeview(col, show='headings',
                                columns=('type', 'id'), height=5)
            tv.heading('type', text='Type')
            tv.heading('id', text='ID')
            tv.column('type', width=70, anchor=tk.W)
            tv.column('id', width=80, anchor=tk.W)
            tv.pack(fill=tk.X)
            self.ship_ed_list_trees[list_type] = tv

        # Add/remove controls
        ctrl_row = ttk.Frame(combat_frame)
        ctrl_row.pack(fill=tk.X, pady=(6, 2))
        ttk.Label(ctrl_row, text="List:").pack(side=tk.LEFT)
        self.ship_ed_list_list = ttk.Combobox(
            ctrl_row, state='readonly', width=10,
            values=['target', 'defend', 'avoid'])
        self.ship_ed_list_list.set('target')
        self.ship_ed_list_list.pack(side=tk.LEFT, padx=(2, 6))
        ttk.Label(ctrl_row, text="Type:").pack(side=tk.LEFT)
        self.ship_ed_list_entrytype = ttk.Combobox(
            ctrl_row, state='readonly', width=9,
            values=['ship', 'base', 'faction'])
        self.ship_ed_list_entrytype.set('ship')
        self.ship_ed_list_entrytype.pack(side=tk.LEFT, padx=(2, 6))
        ttk.Label(ctrl_row, text="ID:").pack(side=tk.LEFT)
        self.ship_ed_list_entryid = ttk.Entry(ctrl_row, width=12)
        self.ship_ed_list_entryid.pack(side=tk.LEFT, padx=(2, 6))
        ttk.Button(ctrl_row, text="Add", width=8,
                   command=self._ship_ed_list_add).pack(side=tk.LEFT, padx=2)
        ttk.Button(ctrl_row, text="Remove Selected", width=18,
                   command=self._ship_ed_list_remove).pack(side=tk.LEFT, padx=2)
        ttk.Button(ctrl_row, text="Clear List", width=12,
                   command=self._ship_ed_list_clear).pack(side=tk.LEFT, padx=2)

        self.ship_ed_status = ttk.Label(inner, text="No ship loaded.",
                                         foreground="#666", font=("", 9, "italic"))
        self.ship_ed_status.pack(anchor=tk.W, padx=5, pady=(4, 0))

        self._ship_ed_load_list()
        self._ship_ed_load_catalogs()

    # ----- Ship editor helpers -----

    def _ship_ed_msg(self, msg, ok=True):
        self.ship_ed_status.config(text=msg,
                                    foreground="#2ecc71" if ok else "#e74c3c")

    def _ship_ed_rw_conn(self):
        """Open a read-write connection. Caller must close()."""
        from db.database import get_connection
        return get_connection()

    def _ship_ed_load_list(self):
        try:
            conn = self._ship_ed_rw_conn()
            ships = conn.execute(
                "SELECT ship_id, name FROM ships ORDER BY name"
            ).fetchall()
            conn.close()
            self.ship_ed_combo['values'] = [
                f"{s['name']} ({s['ship_id']})" for s in ships
            ]
            self._ship_ed_msg(f"Loaded {len(ships)} ships.")
        except Exception as ex:
            self._ship_ed_msg(f"Error loading ships: {ex}", ok=False)

    def _ship_ed_load_catalogs(self):
        """Load component and cargo item dropdowns."""
        try:
            conn = self._ship_ed_rw_conn()
            comps = conn.execute(
                "SELECT component_id, name, st_cost "
                "FROM ship_components ORDER BY component_id"
            ).fetchall()
            goods = conn.execute(
                "SELECT item_id, name, mass_per_unit "
                "FROM trade_goods ORDER BY item_id"
            ).fetchall()
            conn.close()

            self._ship_comp_map = {}
            labels = []
            for c in comps:
                lbl = f"{c['component_id']}: {c['name']} ({c['st_cost']} ST)"
                labels.append(lbl)
                self._ship_comp_map[lbl] = dict(c)
            self.ship_comp_combo['values'] = labels

            self._ship_cargo_map = {}
            labels = []
            for g in goods:
                lbl = f"{g['item_id']}: {g['name']}"
                labels.append(lbl)
                self._ship_cargo_map[lbl] = dict(g)
            self.ship_cargo_combo['values'] = labels
        except Exception as ex:
            self._ship_ed_msg(f"Catalog load failed: {ex}", ok=False)

    def _ship_ed_load_selected(self):
        val = self.ship_ed_combo.get().strip()
        if not val:
            messagebox.showinfo("Select ship", "Pick a ship from the dropdown first.")
            return
        import re
        m = re.search(r'\((\d+)\)\s*$', val)
        if not m:
            return
        self._ship_ed_load(int(m.group(1)))

    def _ship_ed_load(self, ship_id):
        try:
            conn = self._ship_ed_rw_conn()
            ship = conn.execute("SELECT * FROM ships WHERE ship_id = ?",
                                 (ship_id,)).fetchone()
            conn.close()
            if not ship:
                self._ship_ed_msg(f"Ship {ship_id} not found.", ok=False)
                return
            self.ship_ed_current_id = ship_id
            for key, _lbl, _w in [
                ('name', '', 0), ('ship_size', '', 0), ('hull_type', '', 0),
                ('ship_class', '', 0), ('design', '', 0),
                ('system_id', '', 0), ('grid_col', '', 0), ('grid_row', '', 0),
                ('tu_remaining', '', 0), ('armour', '', 0),
            ]:
                self.ship_ed_fields[key].delete(0, tk.END)
                v = ship[key] if ship[key] is not None else ''
                self.ship_ed_fields[key].insert(0, str(v))
            self._ship_ed_refresh_components()
            self._ship_ed_refresh_cargo()
            self._ship_ed_refresh_combat()
            self._ship_ed_msg(f"Loaded: {ship['name']} ({ship_id})")
        except Exception as ex:
            self._ship_ed_msg(f"Load failed: {ex}", ok=False)

    def _ship_ed_refresh_components(self):
        for r in self.ship_comp_tree.get_children():
            self.ship_comp_tree.delete(r)
        if self.ship_ed_current_id is None:
            return
        try:
            conn = self._ship_ed_rw_conn()
            rows = conn.execute("""
                SELECT ii.component_id, sc.name, ii.quantity, sc.st_cost
                FROM installed_items ii
                JOIN ship_components sc ON ii.component_id = sc.component_id
                WHERE ii.ship_id = ?
                ORDER BY sc.component_id
            """, (self.ship_ed_current_id,)).fetchall()
            conn.close()
            for r in rows:
                self.ship_comp_tree.insert('', tk.END, values=(
                    r['component_id'], r['name'], r['quantity'],
                    r['st_cost'], r['st_cost'] * r['quantity']))
        except Exception as ex:
            self._ship_ed_msg(f"Refresh components failed: {ex}", ok=False)

    def _ship_ed_refresh_cargo(self):
        for r in self.ship_cargo_tree.get_children():
            self.ship_cargo_tree.delete(r)
        if self.ship_ed_current_id is None:
            return
        try:
            conn = self._ship_ed_rw_conn()
            rows = conn.execute("""
                SELECT item_type_id, item_name, quantity, mass_per_unit
                FROM cargo_items
                WHERE ship_id = ?
                ORDER BY item_type_id
            """, (self.ship_ed_current_id,)).fetchall()
            conn.close()
            for r in rows:
                self.ship_cargo_tree.insert('', tk.END, values=(
                    r['item_type_id'], r['item_name'], r['quantity'],
                    r['mass_per_unit'], r['mass_per_unit'] * r['quantity']))
        except Exception as ex:
            self._ship_ed_msg(f"Refresh cargo failed: {ex}", ok=False)

    def _ship_ed_require_loaded(self):
        if self.ship_ed_current_id is None:
            messagebox.showinfo("No ship", "Load a ship first.")
            return False
        return True

    def _ship_ed_save_basic(self):
        if not self._ship_ed_require_loaded():
            return
        try:
            from db.database import recalculate_ship_stats
            f = self.ship_ed_fields
            conn = self._ship_ed_rw_conn()
            conn.execute("""
                UPDATE ships SET
                    name = ?, ship_size = ?, hull_type = ?,
                    ship_class = ?, design = ?,
                    system_id = ?, grid_col = ?, grid_row = ?,
                    tu_remaining = ?, armour = ?
                WHERE ship_id = ?
            """, (
                f['name'].get().strip(),
                int(f['ship_size'].get().strip() or '50'),
                f['hull_type'].get().strip() or 'Commercial',
                f['ship_class'].get().strip() or None,
                f['design'].get().strip() or None,
                int(f['system_id'].get().strip() or '101'),
                f['grid_col'].get().strip() or 'M',
                int(f['grid_row'].get().strip() or '13'),
                int(f['tu_remaining'].get().strip() or '0'),
                int(f['armour'].get().strip() or '0'),
                self.ship_ed_current_id,
            ))
            conn.commit()
            recalculate_ship_stats(conn, self.ship_ed_current_id)
            conn.commit()
            conn.close()
            self._ship_ed_msg("Basic info saved, stats recalculated.")
            self._ship_ed_load_list()
        except Exception as ex:
            self._ship_ed_msg(f"Save failed: {ex}", ok=False)

    def _ship_ed_sync_crew(self, conn):
        """Re-derive ships.crew_count from cargo_items (401) + officers."""
        sid = self.ship_ed_current_id
        cc = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM cargo_items "
            "WHERE ship_id = ? AND item_type_id = 401", (sid,)
        ).fetchone()[0]
        off = conn.execute(
            "SELECT COUNT(*) FROM officers WHERE ship_id = ?", (sid,)
        ).fetchone()[0]
        conn.execute("UPDATE ships SET crew_count = ? WHERE ship_id = ?",
                     (cc + off, sid))

    def _ship_ed_sel_row(self, tree):
        sel = tree.selection()
        return tree.item(sel[0], 'values') if sel else None

    def _ship_ed_add_component(self):
        if not self._ship_ed_require_loaded():
            return
        label = self.ship_comp_combo.get()
        if not label or label not in self._ship_comp_map:
            self._ship_ed_msg("Select a component from the dropdown.", ok=False)
            return
        comp = self._ship_comp_map[label]
        try:
            qty = int(self.ship_comp_qty.get())
            if qty <= 0:
                raise ValueError
        except ValueError:
            self._ship_ed_msg("Invalid quantity.", ok=False)
            return
        try:
            from db.database import recalculate_ship_stats
            conn = self._ship_ed_rw_conn()
            existing = conn.execute(
                "SELECT item_install_id, quantity FROM installed_items "
                "WHERE ship_id = ? AND component_id = ?",
                (self.ship_ed_current_id, comp['component_id'])
            ).fetchone()
            if existing:
                new_q = existing['quantity'] + qty
                conn.execute(
                    "UPDATE installed_items SET quantity = ? WHERE item_install_id = ?",
                    (new_q, existing['item_install_id']))
                msg = f"{comp['name']}: qty now {new_q}"
            else:
                conn.execute(
                    "INSERT INTO installed_items (ship_id, component_id, quantity) "
                    "VALUES (?, ?, ?)",
                    (self.ship_ed_current_id, comp['component_id'], qty))
                msg = f"Added {qty}× {comp['name']}"
            conn.commit()
            recalculate_ship_stats(conn, self.ship_ed_current_id)
            conn.commit()
            conn.close()
            self._ship_ed_refresh_components()
            self._ship_ed_msg(msg)
        except Exception as ex:
            self._ship_ed_msg(f"Add failed: {ex}", ok=False)

    def _ship_ed_set_component_qty(self):
        if not self._ship_ed_require_loaded():
            return
        row = self._ship_ed_sel_row(self.ship_comp_tree)
        if not row:
            messagebox.showinfo("Select row", "Select a component row first.")
            return
        comp_id = int(row[0])
        try:
            qty = int(self.ship_comp_qty.get())
            if qty < 0:
                raise ValueError
        except ValueError:
            self._ship_ed_msg("Invalid quantity.", ok=False)
            return
        try:
            from db.database import recalculate_ship_stats
            conn = self._ship_ed_rw_conn()
            if qty == 0:
                conn.execute(
                    "DELETE FROM installed_items WHERE ship_id = ? AND component_id = ?",
                    (self.ship_ed_current_id, comp_id))
            else:
                conn.execute(
                    "UPDATE installed_items SET quantity = ? "
                    "WHERE ship_id = ? AND component_id = ?",
                    (qty, self.ship_ed_current_id, comp_id))
            conn.commit()
            recalculate_ship_stats(conn, self.ship_ed_current_id)
            conn.commit()
            conn.close()
            self._ship_ed_refresh_components()
            self._ship_ed_msg(f"Component {comp_id}: qty = {qty}")
        except Exception as ex:
            self._ship_ed_msg(f"Set failed: {ex}", ok=False)

    def _ship_ed_remove_component(self):
        if not self._ship_ed_require_loaded():
            return
        row = self._ship_ed_sel_row(self.ship_comp_tree)
        if not row:
            messagebox.showinfo("Select row", "Select a component row first.")
            return
        comp_id = int(row[0])
        if not messagebox.askyesno(
            "Confirm", f"Remove '{row[1]}' completely from this ship?"
        ):
            return
        try:
            from db.database import recalculate_ship_stats
            conn = self._ship_ed_rw_conn()
            conn.execute(
                "DELETE FROM installed_items WHERE ship_id = ? AND component_id = ?",
                (self.ship_ed_current_id, comp_id))
            conn.commit()
            recalculate_ship_stats(conn, self.ship_ed_current_id)
            conn.commit()
            conn.close()
            self._ship_ed_refresh_components()
            self._ship_ed_msg(f"Removed component {comp_id}")
        except Exception as ex:
            self._ship_ed_msg(f"Remove failed: {ex}", ok=False)

    def _ship_ed_add_cargo(self):
        if not self._ship_ed_require_loaded():
            return
        label = self.ship_cargo_combo.get()
        if not label or label not in self._ship_cargo_map:
            self._ship_ed_msg("Select a cargo item from the dropdown.", ok=False)
            return
        item = self._ship_cargo_map[label]
        try:
            qty = int(self.ship_cargo_qty.get())
            if qty <= 0:
                raise ValueError
        except ValueError:
            self._ship_ed_msg("Invalid quantity.", ok=False)
            return
        try:
            from db.database import recalculate_ship_stats
            conn = self._ship_ed_rw_conn()
            existing = conn.execute(
                "SELECT cargo_id, quantity FROM cargo_items "
                "WHERE ship_id = ? AND item_type_id = ?",
                (self.ship_ed_current_id, item['item_id'])
            ).fetchone()
            if existing:
                new_q = existing['quantity'] + qty
                conn.execute(
                    "UPDATE cargo_items SET quantity = ? WHERE cargo_id = ?",
                    (new_q, existing['cargo_id']))
                msg = f"{item['name']}: qty now {new_q}"
            else:
                conn.execute(
                    "INSERT INTO cargo_items "
                    "(ship_id, item_type_id, item_name, quantity, mass_per_unit) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (self.ship_ed_current_id, item['item_id'],
                     item['name'], qty, item['mass_per_unit']))
                msg = f"Added {qty}× {item['name']}"
            conn.commit()
            if item['item_id'] == 401:
                self._ship_ed_sync_crew(conn)
            recalculate_ship_stats(conn, self.ship_ed_current_id)
            conn.commit()
            conn.close()
            self._ship_ed_refresh_cargo()
            self._ship_ed_msg(msg)
        except Exception as ex:
            self._ship_ed_msg(f"Add cargo failed: {ex}", ok=False)

    def _ship_ed_set_cargo_qty(self):
        if not self._ship_ed_require_loaded():
            return
        row = self._ship_ed_sel_row(self.ship_cargo_tree)
        if not row:
            messagebox.showinfo("Select row", "Select a cargo row first.")
            return
        item_id = int(row[0])
        try:
            qty = int(self.ship_cargo_qty.get())
            if qty < 0:
                raise ValueError
        except ValueError:
            self._ship_ed_msg("Invalid quantity.", ok=False)
            return
        try:
            from db.database import recalculate_ship_stats
            conn = self._ship_ed_rw_conn()
            if qty == 0:
                conn.execute(
                    "DELETE FROM cargo_items WHERE ship_id = ? AND item_type_id = ?",
                    (self.ship_ed_current_id, item_id))
            else:
                conn.execute(
                    "UPDATE cargo_items SET quantity = ? "
                    "WHERE ship_id = ? AND item_type_id = ?",
                    (qty, self.ship_ed_current_id, item_id))
            conn.commit()
            if item_id == 401:
                self._ship_ed_sync_crew(conn)
            recalculate_ship_stats(conn, self.ship_ed_current_id)
            conn.commit()
            conn.close()
            self._ship_ed_refresh_cargo()
            self._ship_ed_msg(f"Cargo {item_id}: qty = {qty}")
        except Exception as ex:
            self._ship_ed_msg(f"Set cargo failed: {ex}", ok=False)

    def _ship_ed_remove_cargo(self):
        if not self._ship_ed_require_loaded():
            return
        row = self._ship_ed_sel_row(self.ship_cargo_tree)
        if not row:
            messagebox.showinfo("Select row", "Select a cargo row first.")
            return
        item_id = int(row[0])
        if not messagebox.askyesno("Confirm",
                                     f"Remove '{row[1]}' completely from this ship's cargo?"):
            return
        try:
            from db.database import recalculate_ship_stats
            conn = self._ship_ed_rw_conn()
            conn.execute(
                "DELETE FROM cargo_items WHERE ship_id = ? AND item_type_id = ?",
                (self.ship_ed_current_id, item_id))
            conn.commit()
            if item_id == 401:
                self._ship_ed_sync_crew(conn)
            recalculate_ship_stats(conn, self.ship_ed_current_id)
            conn.commit()
            conn.close()
            self._ship_ed_refresh_cargo()
            self._ship_ed_msg(f"Removed cargo {item_id}")
        except Exception as ex:
            self._ship_ed_msg(f"Remove cargo failed: {ex}", ok=False)

    # ----- Ship editor: combat -----

    def _ship_ed_refresh_combat(self):
        """Load doctrine, combat lists, and combat state for the currently loaded ship."""
        for lt in ('target', 'defend', 'avoid'):
            tv = self.ship_ed_list_trees[lt]
            for row_id in tv.get_children():
                tv.delete(row_id)
        # Reset combat state fields even if no ship loaded
        self.ship_ed_integrity_entry.delete(0, tk.END)
        self.ship_ed_max_integrity_label.config(text="-")
        self.ship_ed_shield_entry.delete(0, tk.END)
        self.ship_ed_max_shield_label.config(text="-")
        self.ship_ed_missiles_entry.delete(0, tk.END)
        self.ship_ed_max_missiles_label.config(text="-")
        self.ship_ed_torpedoes_entry.delete(0, tk.END)
        self.ship_ed_max_torpedoes_label.config(text="-")
        if self.ship_ed_current_id is None:
            return
        try:
            conn = self._ship_ed_rw_conn()
            row = conn.execute(
                """SELECT combat_doctrine, integrity, max_integrity,
                          shield_sp, max_shield_sp,
                          missiles_loaded, max_missiles,
                          torpedoes_loaded, max_torpedoes
                   FROM ships WHERE ship_id = ?""",
                (self.ship_ed_current_id,)
            ).fetchone()
            doctrine = (row['combat_doctrine'] if row else 'defensive') or 'defensive'
            self.ship_ed_doctrine.set(doctrine)
            # Combat state fields
            if row:
                integ = row['integrity'] if row['integrity'] is not None else 0
                max_integ = row['max_integrity'] if row['max_integrity'] is not None else 0
                sp = row['shield_sp'] if row['shield_sp'] is not None else 0
                max_sp = row['max_shield_sp'] if row['max_shield_sp'] is not None else 0
                miss = row['missiles_loaded'] if row['missiles_loaded'] is not None else 0
                max_miss = row['max_missiles'] if row['max_missiles'] is not None else 0
                torp = row['torpedoes_loaded'] if row['torpedoes_loaded'] is not None else 0
                max_torp = row['max_torpedoes'] if row['max_torpedoes'] is not None else 0
                self.ship_ed_integrity_entry.insert(0, f"{int(integ)}")
                self.ship_ed_max_integrity_label.config(text=f"{int(max_integ)}")
                self.ship_ed_shield_entry.insert(0, f"{int(sp)}")
                self.ship_ed_max_shield_label.config(text=f"{int(max_sp)}")
                self.ship_ed_missiles_entry.insert(0, f"{int(miss)}")
                self.ship_ed_max_missiles_label.config(text=f"{int(max_miss)}")
                self.ship_ed_torpedoes_entry.insert(0, f"{int(torp)}")
                self.ship_ed_max_torpedoes_label.config(text=f"{int(max_torp)}")
            lists = conn.execute(
                "SELECT list_type, entry_type, entry_id FROM ship_combat_lists "
                "WHERE game_id = ? AND ship_id = ? "
                "ORDER BY list_type, entry_type, entry_id",
                (self.config_data['game_id'], self.ship_ed_current_id)
            ).fetchall()
            conn.close()
            for r in lists:
                tv = self.ship_ed_list_trees.get(r['list_type'])
                if tv is not None:
                    tv.insert('', tk.END,
                               values=(r['entry_type'], r['entry_id']))
        except Exception as ex:
            self._ship_ed_msg(f"Refresh combat failed: {ex}", ok=False)

    def _ship_ed_save_combat_state(self):
        """Save integrity, shield_sp, missiles, torpedoes. All clamped to max."""
        if self.ship_ed_current_id is None:
            self._ship_ed_msg("Load a ship first.", ok=False)
            return
        try:
            integ_str = self.ship_ed_integrity_entry.get().strip()
            sp_str = self.ship_ed_shield_entry.get().strip()
            miss_str = self.ship_ed_missiles_entry.get().strip()
            torp_str = self.ship_ed_torpedoes_entry.get().strip()
            for label, v in [('integrity', integ_str), ('shield SP', sp_str),
                              ('missiles', miss_str), ('torpedoes', torp_str)]:
                if not v.isdigit():
                    messagebox.showerror("Input error",
                                          f"{label} must be a non-negative integer (got '{v}').")
                    return
            integ = int(integ_str)
            sp = int(sp_str)
            miss = int(miss_str)
            torp = int(torp_str)
            conn = self._ship_ed_rw_conn()
            row = conn.execute(
                """SELECT max_integrity, max_shield_sp, max_missiles, max_torpedoes
                   FROM ships WHERE ship_id = ?""",
                (self.ship_ed_current_id,)
            ).fetchone()
            max_integ = int(row['max_integrity'] or 0)
            max_sp = int(row['max_shield_sp'] or 0)
            max_miss = int(row['max_missiles'] or 0)
            max_torp = int(row['max_torpedoes'] or 0)
            # Clamp to maxes
            clamped_integ = min(integ, max_integ) if max_integ > 0 else integ
            clamped_sp = min(sp, max_sp)
            clamped_miss = min(miss, max_miss)
            clamped_torp = min(torp, max_torp)
            conn.execute(
                """UPDATE ships SET integrity = ?, shield_sp = ?,
                          missiles_loaded = ?, torpedoes_loaded = ?
                   WHERE ship_id = ?""",
                (clamped_integ, clamped_sp, clamped_miss, clamped_torp,
                 self.ship_ed_current_id)
            )
            conn.commit()
            conn.close()
            msg_bits = []
            if clamped_integ != integ:
                msg_bits.append(f"integrity clamped to {clamped_integ} (max {max_integ})")
            if clamped_sp != sp:
                msg_bits.append(f"shield SP clamped to {clamped_sp} (max {max_sp})")
            if clamped_miss != miss:
                msg_bits.append(f"missiles clamped to {clamped_miss} (max {max_miss})")
            if clamped_torp != torp:
                msg_bits.append(f"torpedoes clamped to {clamped_torp} (max {max_torp})")
            note = " — " + "; ".join(msg_bits) if msg_bits else ""
            self._ship_ed_msg(f"Combat state saved{note}")
            self._ship_ed_refresh_combat()
        except Exception as ex:
            self._ship_ed_msg(f"Save combat state failed: {ex}", ok=False)

    def _ship_ed_repair_full(self):
        """Set integrity, shield_sp, missiles, torpedoes to their maxes."""
        if self.ship_ed_current_id is None:
            self._ship_ed_msg("Load a ship first.", ok=False)
            return
        if not messagebox.askyesno("Confirm",
                                      "Restore integrity, shields, and magazines to full?"):
            return
        try:
            conn = self._ship_ed_rw_conn()
            conn.execute(
                """UPDATE ships SET
                       integrity = max_integrity,
                       shield_sp = max_shield_sp,
                       missiles_loaded = max_missiles,
                       torpedoes_loaded = max_torpedoes
                   WHERE ship_id = ?""",
                (self.ship_ed_current_id,)
            )
            conn.commit()
            conn.close()
            self._ship_ed_msg("Integrity and shields restored to full.")
            self._ship_ed_refresh_combat()
        except Exception as ex:
            self._ship_ed_msg(f"Repair failed: {ex}", ok=False)

    def _ship_ed_save_doctrine(self):
        if self.ship_ed_current_id is None:
            self._ship_ed_msg("Load a ship first.", ok=False)
            return
        doc = self.ship_ed_doctrine.get()
        self._run_pbem(
            ['set-doctrine', '--ship', str(self.ship_ed_current_id),
             '--doctrine', doc],
            "Save Doctrine"
        )
        self.root.after(500, self._ship_ed_refresh_combat)

    def _ship_ed_list_add(self):
        if self.ship_ed_current_id is None:
            self._ship_ed_msg("Load a ship first.", ok=False)
            return
        lt = self.ship_ed_list_list.get()
        et = self.ship_ed_list_entrytype.get()
        eid = self.ship_ed_list_entryid.get().strip()
        if not eid.isdigit():
            messagebox.showerror("Input error", "Entry ID must be a positive integer.")
            return
        cmd_text = f"{lt.upper()} ADD {et} {eid}"
        self._run_pbem(
            ['inject-order', '--ship', str(self.ship_ed_current_id),
             '--command', cmd_text],
            f"{lt.upper()} ADD"
        )
        self.root.after(500, self._ship_ed_refresh_combat)

    def _ship_ed_list_remove(self):
        if self.ship_ed_current_id is None:
            self._ship_ed_msg("Load a ship first.", ok=False)
            return
        lt = self.ship_ed_list_list.get()
        tv = self.ship_ed_list_trees[lt]
        sel = tv.selection()
        if not sel:
            messagebox.showerror("No selection",
                                  f"Select a row in the {lt.upper()} list first.")
            return
        vals = tv.item(sel[0])['values']
        et, eid = str(vals[0]), str(vals[1])
        cmd_text = f"{lt.upper()} REMOVE {et} {eid}"
        self._run_pbem(
            ['inject-order', '--ship', str(self.ship_ed_current_id),
             '--command', cmd_text],
            f"{lt.upper()} REMOVE"
        )
        self.root.after(500, self._ship_ed_refresh_combat)

    def _ship_ed_list_clear(self):
        if self.ship_ed_current_id is None:
            self._ship_ed_msg("Load a ship first.", ok=False)
            return
        lt = self.ship_ed_list_list.get()
        if not messagebox.askyesno("Confirm",
                                      f"Clear the entire {lt.upper()} list?"):
            return
        cmd_text = f"{lt.upper()} CLEAR"
        self._run_pbem(
            ['inject-order', '--ship', str(self.ship_ed_current_id),
             '--command', cmd_text],
            f"{lt.upper()} CLEAR"
        )
        self.root.after(500, self._ship_ed_refresh_combat)

    # ========================================================================
    # Tab: Base Editor
    # ========================================================================

    def _build_base_editor_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Base Editor")
        inner = self._scrollable_frame(tab)

        ttk.Label(inner,
                  text="⚠ Direct database editor. Changes are written immediately. "
                       "Stats auto-recalculate after every edit.",
                  foreground="#c87000", font=("", 9, "italic")).pack(
            anchor=tk.W, padx=5, pady=(5, 8))

        # ----- Base picker -----
        pick = ttk.Frame(inner)
        pick.pack(fill=tk.X, padx=5, pady=(0, 8))
        ttk.Label(pick, text="Base:", font=("", 10, "bold")).pack(side=tk.LEFT, padx=(0, 4))
        self.base_ed_combo = ttk.Combobox(pick, width=40, state='readonly')
        self.base_ed_combo.pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(pick, text="Load", width=8,
                   command=self._base_ed_load_selected).pack(side=tk.LEFT, padx=2)
        ttk.Button(pick, text="Refresh List", width=14,
                   command=self._base_ed_load_list).pack(side=tk.LEFT, padx=2)

        # Current base state: (kind, id) where kind in {'starbase','port','outpost'}
        self.base_ed_current = None

        # ----- Basic Info -----
        basic = ttk.LabelFrame(inner, text="Basic Info", padding=6)
        basic.pack(fill=tk.X, padx=5, pady=4)

        self.base_ed_fields = {}
        BASE_BASIC = [
            ('name', 'Name:', 30),
            ('employees', 'Employees:', 10),
            ('workers', 'Workers:', 10),
            ('troops', 'Troops:', 10),
            ('complexes', 'Complexes:', 10),
        ]
        for key, label, width in BASE_BASIC:
            row = ttk.Frame(basic)
            row.pack(fill=tk.X, pady=2)
            ttk.Label(row, text=label, width=14).pack(side=tk.LEFT)
            e = ttk.Entry(row, width=width)
            e.pack(side=tk.LEFT, padx=(2, 10))
            self.base_ed_fields[key] = e

        # has_market checkbox (starbase only)
        self.base_ed_market_var = tk.BooleanVar(value=False)
        self.base_ed_market_chk = ttk.Checkbutton(
            basic, text="Has Market (starbase only)",
            variable=self.base_ed_market_var)
        self.base_ed_market_chk.pack(anchor=tk.W, pady=(2, 4))

        ttk.Button(basic, text="Save Basic Info", width=18,
                   command=self._base_ed_save_basic).pack(anchor=tk.W, pady=(4, 0))

        # ----- Installed Modules -----
        mods = ttk.LabelFrame(inner, text="Installed Modules", padding=6)
        mods.pack(fill=tk.X, padx=5, pady=4)

        mw = ttk.Frame(mods)
        mw.pack(fill=tk.X)
        self.base_mod_tree = ttk.Treeview(
            mw, show='headings',
            columns=('id', 'name', 'cat', 'qty', 'emp'), height=8)
        for col, hd, w in [('id', 'ID', 50), ('name', 'Name', 200),
                            ('cat', 'Category', 90),
                            ('qty', 'Qty', 50), ('emp', 'Emp/Each', 70)]:
            self.base_mod_tree.heading(col, text=hd)
            self.base_mod_tree.column(col, width=w, anchor=tk.W)
        mod_sb = ttk.Scrollbar(mw, orient=tk.VERTICAL,
                                command=self.base_mod_tree.yview)
        self.base_mod_tree.configure(yscrollcommand=mod_sb.set)
        self.base_mod_tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        mod_sb.pack(side=tk.LEFT, fill=tk.Y)

        mcr = ttk.Frame(mods)
        mcr.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(mcr, text="Module:").pack(side=tk.LEFT)
        self.base_mod_combo = ttk.Combobox(mcr, width=32, state='readonly')
        self.base_mod_combo.pack(side=tk.LEFT, padx=2)
        ttk.Label(mcr, text="Qty:").pack(side=tk.LEFT, padx=(6, 2))
        self.base_mod_qty = ttk.Entry(mcr, width=5)
        self.base_mod_qty.insert(0, "1")
        self.base_mod_qty.pack(side=tk.LEFT)
        ttk.Button(mcr, text="Add", width=6,
                   command=self._base_ed_add_module).pack(side=tk.LEFT, padx=(4, 2))
        ttk.Button(mcr, text="Set Qty", width=8,
                   command=self._base_ed_set_module_qty).pack(side=tk.LEFT, padx=2)
        ttk.Button(mcr, text="Remove Selected", width=16,
                   command=self._base_ed_remove_module).pack(side=tk.LEFT, padx=2)

        # ----- Inventory -----
        inv = ttk.LabelFrame(inner, text="Inventory", padding=6)
        inv.pack(fill=tk.X, padx=5, pady=4)

        iw = ttk.Frame(inv)
        iw.pack(fill=tk.X)
        self.base_inv_tree = ttk.Treeview(
            iw, show='headings',
            columns=('id', 'name', 'qty', 'mass', 'total'), height=6)
        for col, hd, w in [('id', 'ID', 55), ('name', 'Name', 190),
                            ('qty', 'Qty', 60), ('mass', 'Mass/Unit', 80),
                            ('total', 'Total ST', 80)]:
            self.base_inv_tree.heading(col, text=hd)
            self.base_inv_tree.column(col, width=w, anchor=tk.W)
        inv_sb = ttk.Scrollbar(iw, orient=tk.VERTICAL,
                                command=self.base_inv_tree.yview)
        self.base_inv_tree.configure(yscrollcommand=inv_sb.set)
        self.base_inv_tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        inv_sb.pack(side=tk.LEFT, fill=tk.Y)

        icr = ttk.Frame(inv)
        icr.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(icr, text="Item:").pack(side=tk.LEFT)
        self.base_inv_combo = ttk.Combobox(icr, width=30, state='readonly')
        self.base_inv_combo.pack(side=tk.LEFT, padx=2)
        ttk.Label(icr, text="Qty:").pack(side=tk.LEFT, padx=(6, 2))
        self.base_inv_qty = ttk.Entry(icr, width=6)
        self.base_inv_qty.insert(0, "1")
        self.base_inv_qty.pack(side=tk.LEFT)
        ttk.Button(icr, text="Add", width=6,
                   command=self._base_ed_add_inv).pack(side=tk.LEFT, padx=(4, 2))
        ttk.Button(icr, text="Set Qty", width=8,
                   command=self._base_ed_set_inv_qty).pack(side=tk.LEFT, padx=2)
        ttk.Button(icr, text="Remove Selected", width=16,
                   command=self._base_ed_remove_inv).pack(side=tk.LEFT, padx=2)

        # ----- Combat -----
        b_combat = ttk.LabelFrame(inner, text="Combat (Bases: target + defend, no avoid)",
                                    padding=6)
        b_combat.pack(fill=tk.X, padx=5, pady=4)

        b_lists_row = ttk.Frame(b_combat)
        b_lists_row.pack(fill=tk.X)
        self.base_ed_list_trees = {}
        for list_type in ('target', 'defend'):
            col_f = ttk.LabelFrame(b_lists_row, text=list_type.upper(), padding=4)
            col_f.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=2)
            tv = ttk.Treeview(col_f, show='headings',
                                columns=('type', 'id'), height=5)
            tv.heading('type', text='Type')
            tv.heading('id', text='ID')
            tv.column('type', width=70, anchor=tk.W)
            tv.column('id', width=100, anchor=tk.W)
            tv.pack(fill=tk.X)
            self.base_ed_list_trees[list_type] = tv

        b_ctrl = ttk.Frame(b_combat)
        b_ctrl.pack(fill=tk.X, pady=(6, 2))
        ttk.Label(b_ctrl, text="List:").pack(side=tk.LEFT)
        self.base_ed_list_list = ttk.Combobox(
            b_ctrl, state='readonly', width=10,
            values=['target', 'defend'])
        self.base_ed_list_list.set('target')
        self.base_ed_list_list.pack(side=tk.LEFT, padx=(2, 6))
        ttk.Label(b_ctrl, text="Type:").pack(side=tk.LEFT)
        self.base_ed_list_entrytype = ttk.Combobox(
            b_ctrl, state='readonly', width=9,
            values=['ship', 'base', 'faction'])
        self.base_ed_list_entrytype.set('ship')
        self.base_ed_list_entrytype.pack(side=tk.LEFT, padx=(2, 6))
        ttk.Label(b_ctrl, text="ID:").pack(side=tk.LEFT)
        self.base_ed_list_entryid = ttk.Entry(b_ctrl, width=12)
        self.base_ed_list_entryid.pack(side=tk.LEFT, padx=(2, 6))
        ttk.Button(b_ctrl, text="Add", width=8,
                   command=self._base_ed_list_add).pack(side=tk.LEFT, padx=2)
        ttk.Button(b_ctrl, text="Remove Selected", width=18,
                   command=self._base_ed_list_remove).pack(side=tk.LEFT, padx=2)
        ttk.Button(b_ctrl, text="Clear List", width=12,
                   command=self._base_ed_list_clear).pack(side=tk.LEFT, padx=2)

        self.base_ed_status = ttk.Label(inner, text="No base loaded.",
                                         foreground="#666", font=("", 9, "italic"))
        self.base_ed_status.pack(anchor=tk.W, padx=5, pady=(4, 0))

        self._base_ed_load_list()
        self._base_ed_load_catalogs()

    # ----- Base editor helpers -----

    def _base_ed_msg(self, msg, ok=True):
        self.base_ed_status.config(text=msg,
                                    foreground="#2ecc71" if ok else "#e74c3c")

    def _base_ed_rw_conn(self):
        from db.database import get_connection
        return get_connection()

    def _base_ed_load_list(self):
        try:
            conn = self._base_ed_rw_conn()
            items = []
            for row in conn.execute(
                "SELECT base_id AS id, name FROM starbases ORDER BY name"
            ).fetchall():
                items.append(('starbase', row['id'], f"[Starbase] {row['name']} ({row['id']})"))
            for row in conn.execute(
                "SELECT port_id AS id, name FROM surface_ports ORDER BY name"
            ).fetchall():
                items.append(('port', row['id'], f"[Port] {row['name']} ({row['id']})"))
            for row in conn.execute(
                "SELECT outpost_id AS id, name FROM outposts ORDER BY name"
            ).fetchall():
                items.append(('outpost', row['id'], f"[Outpost] {row['name']} ({row['id']})"))
            conn.close()
            self._base_ed_items = items
            self.base_ed_combo['values'] = [i[2] for i in items]
            self._base_ed_msg(f"Loaded {len(items)} bases.")
        except Exception as ex:
            self._base_ed_msg(f"Error loading bases: {ex}", ok=False)

    def _base_ed_load_catalogs(self):
        try:
            conn = self._base_ed_rw_conn()
            mods = conn.execute(
                "SELECT module_id, name, category, employees_required "
                "FROM base_modules ORDER BY module_id"
            ).fetchall()
            goods = conn.execute(
                "SELECT item_id, name, mass_per_unit "
                "FROM trade_goods ORDER BY item_id"
            ).fetchall()
            conn.close()

            self._base_mod_map = {}
            labels = []
            for m in mods:
                lbl = f"{m['module_id']}: {m['name']} ({m['category']})"
                labels.append(lbl)
                self._base_mod_map[lbl] = dict(m)
            self.base_mod_combo['values'] = labels

            self._base_inv_map = {}
            labels = []
            for g in goods:
                lbl = f"{g['item_id']}: {g['name']}"
                labels.append(lbl)
                self._base_inv_map[lbl] = dict(g)
            self.base_inv_combo['values'] = labels
        except Exception as ex:
            self._base_ed_msg(f"Catalog load failed: {ex}", ok=False)

    def _base_ed_load_selected(self):
        val = self.base_ed_combo.get().strip()
        if not val:
            messagebox.showinfo("Select base", "Pick a base from the dropdown first.")
            return
        # Find item in our list by display string
        for kind, bid, label in getattr(self, '_base_ed_items', []):
            if label == val:
                self._base_ed_load(kind, bid)
                return

    def _base_ed_load(self, kind, bid):
        """Load base data. kind in {'starbase','port','outpost'}"""
        table = {'starbase': 'starbases', 'port': 'surface_ports',
                 'outpost': 'outposts'}[kind]
        id_col = {'starbase': 'base_id', 'port': 'port_id',
                  'outpost': 'outpost_id'}[kind]
        try:
            conn = self._base_ed_rw_conn()
            base = conn.execute(
                f"SELECT * FROM {table} WHERE {id_col} = ?", (bid,)
            ).fetchone()
            conn.close()
            if not base:
                self._base_ed_msg(f"{kind} {bid} not found.", ok=False)
                return
            self.base_ed_current = (kind, bid)

            f = self.base_ed_fields
            for key in ('name', 'employees', 'workers', 'troops', 'complexes'):
                f[key].delete(0, tk.END)
                # outposts have no complexes/troops column
                try:
                    v = base[key]
                except (IndexError, KeyError):
                    v = 0
                if v is None:
                    v = 0
                f[key].insert(0, str(v))

            # Market flag: only starbases
            if kind == 'starbase':
                try:
                    self.base_ed_market_var.set(bool(base['has_market']))
                except (IndexError, KeyError):
                    self.base_ed_market_var.set(False)
                self.base_ed_market_chk.state(['!disabled'])
            else:
                self.base_ed_market_var.set(False)
                self.base_ed_market_chk.state(['disabled'])

            self._base_ed_refresh_modules()
            self._base_ed_refresh_inventory()
            self._base_ed_refresh_combat()
            self._base_ed_msg(f"Loaded {kind}: {base['name']} ({bid})")
        except Exception as ex:
            self._base_ed_msg(f"Load failed: {ex}", ok=False)

    def _base_ed_where_clause(self):
        """Return (column_name, id_value) for the current base's FK in modules/inventory."""
        if not self.base_ed_current:
            return None, None
        kind, bid = self.base_ed_current
        col = {'starbase': 'starbase_id', 'port': 'port_id',
               'outpost': 'outpost_id'}[kind]
        return col, bid

    def _base_ed_refresh_modules(self):
        for r in self.base_mod_tree.get_children():
            self.base_mod_tree.delete(r)
        col, bid = self._base_ed_where_clause()
        if not col:
            return
        try:
            conn = self._base_ed_rw_conn()
            rows = conn.execute(f"""
                SELECT im.module_id, bm.name, bm.category, im.quantity, bm.employees_required
                FROM installed_modules im
                JOIN base_modules bm ON im.module_id = bm.module_id
                WHERE im.{col} = ?
                ORDER BY bm.module_id
            """, (bid,)).fetchall()
            conn.close()
            for r in rows:
                self.base_mod_tree.insert('', tk.END, values=(
                    r['module_id'], r['name'], r['category'],
                    r['quantity'], r['employees_required']))
        except Exception as ex:
            self._base_ed_msg(f"Refresh modules failed: {ex}", ok=False)

    def _base_ed_refresh_inventory(self):
        for r in self.base_inv_tree.get_children():
            self.base_inv_tree.delete(r)
        col, bid = self._base_ed_where_clause()
        if not col:
            return
        try:
            conn = self._base_ed_rw_conn()
            rows = conn.execute(f"""
                SELECT item_type_id, item_name, quantity, mass_per_unit
                FROM base_inventory
                WHERE {col} = ?
                ORDER BY item_type_id
            """, (bid,)).fetchall()
            conn.close()
            for r in rows:
                self.base_inv_tree.insert('', tk.END, values=(
                    r['item_type_id'], r['item_name'], r['quantity'],
                    r['mass_per_unit'], r['mass_per_unit'] * r['quantity']))
        except Exception as ex:
            self._base_ed_msg(f"Refresh inventory failed: {ex}", ok=False)

    def _base_ed_require_loaded(self):
        if not self.base_ed_current:
            messagebox.showinfo("No base", "Load a base first.")
            return False
        return True

    def _base_ed_save_basic(self):
        if not self._base_ed_require_loaded():
            return
        kind, bid = self.base_ed_current
        f = self.base_ed_fields
        try:
            from db.database import recalculate_base_stats
            conn = self._base_ed_rw_conn()
            name = f['name'].get().strip() or 'Unnamed'
            employees = int(f['employees'].get().strip() or '0')
            workers = int(f['workers'].get().strip() or '0')
            troops = int(f['troops'].get().strip() or '0')
            complexes = int(f['complexes'].get().strip() or '0')

            if kind == 'starbase':
                conn.execute("""
                    UPDATE starbases SET name = ?, employees = ?, workers = ?,
                    troops = ?, complexes = ?, has_market = ?
                    WHERE base_id = ?
                """, (name, employees, workers, troops, complexes,
                      1 if self.base_ed_market_var.get() else 0, bid))
                recalculate_base_stats(conn, starbase_id=bid)
            elif kind == 'port':
                conn.execute("""
                    UPDATE surface_ports SET name = ?, employees = ?, workers = ?,
                    troops = ?, complexes = ?
                    WHERE port_id = ?
                """, (name, employees, workers, troops, complexes, bid))
                recalculate_base_stats(conn, port_id=bid)
            else:  # outpost - no complexes/troops
                conn.execute("""
                    UPDATE outposts SET name = ?, employees = ?, workers = ?
                    WHERE outpost_id = ?
                """, (name, employees, workers, bid))
                recalculate_base_stats(conn, outpost_id=bid)

            conn.commit()
            conn.close()
            self._base_ed_msg("Basic info saved, stats recalculated.")
            self._base_ed_load_list()
        except Exception as ex:
            self._base_ed_msg(f"Save failed: {ex}", ok=False)

    def _base_ed_recalc(self, conn):
        from db.database import recalculate_base_stats
        kind, bid = self.base_ed_current
        if kind == 'starbase':
            recalculate_base_stats(conn, starbase_id=bid)
        elif kind == 'port':
            recalculate_base_stats(conn, port_id=bid)
        else:
            recalculate_base_stats(conn, outpost_id=bid)

    def _base_ed_sel_row(self, tree):
        sel = tree.selection()
        return tree.item(sel[0], 'values') if sel else None

    def _base_ed_add_module(self):
        if not self._base_ed_require_loaded():
            return
        label = self.base_mod_combo.get()
        if not label or label not in self._base_mod_map:
            self._base_ed_msg("Select a module from the dropdown.", ok=False)
            return
        mod = self._base_mod_map[label]
        try:
            qty = int(self.base_mod_qty.get())
            if qty <= 0:
                raise ValueError
        except ValueError:
            self._base_ed_msg("Invalid quantity.", ok=False)
            return
        col, bid = self._base_ed_where_clause()
        try:
            conn = self._base_ed_rw_conn()
            existing = conn.execute(
                f"SELECT install_id, quantity FROM installed_modules "
                f"WHERE {col} = ? AND module_id = ?",
                (bid, mod['module_id'])
            ).fetchone()
            if existing:
                new_q = existing['quantity'] + qty
                conn.execute(
                    "UPDATE installed_modules SET quantity = ? WHERE install_id = ?",
                    (new_q, existing['install_id']))
                msg = f"{mod['name']}: qty now {new_q}"
            else:
                conn.execute(
                    f"INSERT INTO installed_modules ({col}, module_id, quantity) "
                    f"VALUES (?, ?, ?)",
                    (bid, mod['module_id'], qty))
                msg = f"Added {qty}× {mod['name']}"
            conn.commit()
            self._base_ed_recalc(conn)
            conn.commit()
            conn.close()
            self._base_ed_refresh_modules()
            self._base_ed_msg(msg)
        except Exception as ex:
            self._base_ed_msg(f"Add module failed: {ex}", ok=False)

    def _base_ed_set_module_qty(self):
        if not self._base_ed_require_loaded():
            return
        row = self._base_ed_sel_row(self.base_mod_tree)
        if not row:
            messagebox.showinfo("Select row", "Select a module row first.")
            return
        mod_id = int(row[0])
        try:
            qty = int(self.base_mod_qty.get())
            if qty < 0:
                raise ValueError
        except ValueError:
            self._base_ed_msg("Invalid quantity.", ok=False)
            return
        col, bid = self._base_ed_where_clause()
        try:
            conn = self._base_ed_rw_conn()
            if qty == 0:
                conn.execute(
                    f"DELETE FROM installed_modules WHERE {col} = ? AND module_id = ?",
                    (bid, mod_id))
            else:
                conn.execute(
                    f"UPDATE installed_modules SET quantity = ? "
                    f"WHERE {col} = ? AND module_id = ?",
                    (qty, bid, mod_id))
            conn.commit()
            self._base_ed_recalc(conn)
            conn.commit()
            conn.close()
            self._base_ed_refresh_modules()
            self._base_ed_msg(f"Module {mod_id}: qty = {qty}")
        except Exception as ex:
            self._base_ed_msg(f"Set failed: {ex}", ok=False)

    def _base_ed_remove_module(self):
        if not self._base_ed_require_loaded():
            return
        row = self._base_ed_sel_row(self.base_mod_tree)
        if not row:
            messagebox.showinfo("Select row", "Select a module row first.")
            return
        mod_id = int(row[0])
        if not messagebox.askyesno("Confirm",
                                     f"Remove '{row[1]}' completely from this base?"):
            return
        col, bid = self._base_ed_where_clause()
        try:
            conn = self._base_ed_rw_conn()
            conn.execute(
                f"DELETE FROM installed_modules WHERE {col} = ? AND module_id = ?",
                (bid, mod_id))
            conn.commit()
            self._base_ed_recalc(conn)
            conn.commit()
            conn.close()
            self._base_ed_refresh_modules()
            self._base_ed_msg(f"Removed module {mod_id}")
        except Exception as ex:
            self._base_ed_msg(f"Remove failed: {ex}", ok=False)

    def _base_ed_add_inv(self):
        if not self._base_ed_require_loaded():
            return
        label = self.base_inv_combo.get()
        if not label or label not in self._base_inv_map:
            self._base_ed_msg("Select an item from the dropdown.", ok=False)
            return
        item = self._base_inv_map[label]
        try:
            qty = int(self.base_inv_qty.get())
            if qty <= 0:
                raise ValueError
        except ValueError:
            self._base_ed_msg("Invalid quantity.", ok=False)
            return
        col, bid = self._base_ed_where_clause()
        try:
            conn = self._base_ed_rw_conn()
            existing = conn.execute(
                f"SELECT inventory_id, quantity FROM base_inventory "
                f"WHERE {col} = ? AND item_type_id = ?",
                (bid, item['item_id'])
            ).fetchone()
            if existing:
                new_q = existing['quantity'] + qty
                conn.execute(
                    "UPDATE base_inventory SET quantity = ? WHERE inventory_id = ?",
                    (new_q, existing['inventory_id']))
                msg = f"{item['name']}: qty now {new_q}"
            else:
                conn.execute(
                    f"INSERT INTO base_inventory "
                    f"({col}, item_type_id, item_name, quantity, mass_per_unit) "
                    f"VALUES (?, ?, ?, ?, ?)",
                    (bid, item['item_id'], item['name'],
                     qty, item['mass_per_unit']))
                msg = f"Added {qty}× {item['name']}"
            conn.commit()
            self._base_ed_recalc(conn)
            conn.commit()
            conn.close()
            self._base_ed_refresh_inventory()
            self._base_ed_msg(msg)
        except Exception as ex:
            self._base_ed_msg(f"Add inv failed: {ex}", ok=False)

    def _base_ed_set_inv_qty(self):
        if not self._base_ed_require_loaded():
            return
        row = self._base_ed_sel_row(self.base_inv_tree)
        if not row:
            messagebox.showinfo("Select row", "Select an inventory row first.")
            return
        item_id = int(row[0])
        try:
            qty = int(self.base_inv_qty.get())
            if qty < 0:
                raise ValueError
        except ValueError:
            self._base_ed_msg("Invalid quantity.", ok=False)
            return
        col, bid = self._base_ed_where_clause()
        try:
            conn = self._base_ed_rw_conn()
            if qty == 0:
                conn.execute(
                    f"DELETE FROM base_inventory WHERE {col} = ? AND item_type_id = ?",
                    (bid, item_id))
            else:
                conn.execute(
                    f"UPDATE base_inventory SET quantity = ? "
                    f"WHERE {col} = ? AND item_type_id = ?",
                    (qty, bid, item_id))
            conn.commit()
            self._base_ed_recalc(conn)
            conn.commit()
            conn.close()
            self._base_ed_refresh_inventory()
            self._base_ed_msg(f"Inv {item_id}: qty = {qty}")
        except Exception as ex:
            self._base_ed_msg(f"Set inv failed: {ex}", ok=False)

    def _base_ed_remove_inv(self):
        if not self._base_ed_require_loaded():
            return
        row = self._base_ed_sel_row(self.base_inv_tree)
        if not row:
            messagebox.showinfo("Select row", "Select an inventory row first.")
            return
        item_id = int(row[0])
        if not messagebox.askyesno("Confirm",
                                     f"Remove '{row[1]}' completely from this base's inventory?"):
            return
        col, bid = self._base_ed_where_clause()
        try:
            conn = self._base_ed_rw_conn()
            conn.execute(
                f"DELETE FROM base_inventory WHERE {col} = ? AND item_type_id = ?",
                (bid, item_id))
            conn.commit()
            self._base_ed_recalc(conn)
            conn.commit()
            conn.close()
            self._base_ed_refresh_inventory()
            self._base_ed_msg(f"Removed inv item {item_id}")
        except Exception as ex:
            self._base_ed_msg(f"Remove inv failed: {ex}", ok=False)

    # ----- Base editor: combat -----

    def _base_ed_refresh_combat(self):
        """Load combat lists for the currently loaded base."""
        for lt in ('target', 'defend'):
            tv = self.base_ed_list_trees[lt]
            for row_id in tv.get_children():
                tv.delete(row_id)
        if not self.base_ed_current:
            return
        kind, bid = self.base_ed_current
        # Translate kind to canonical base_kind used by base_combat_lists
        try:
            conn = self._base_ed_rw_conn()
            lists = conn.execute(
                "SELECT list_type, entry_type, entry_id FROM base_combat_lists "
                "WHERE game_id = ? AND base_kind = ? AND base_id = ? "
                "ORDER BY list_type, entry_type, entry_id",
                (self.config_data['game_id'], kind, bid)
            ).fetchall()
            conn.close()
            for r in lists:
                tv = self.base_ed_list_trees.get(r['list_type'])
                if tv is not None:
                    tv.insert('', tk.END,
                               values=(r['entry_type'], r['entry_id']))
        except Exception as ex:
            self._base_ed_msg(f"Refresh combat failed: {ex}", ok=False)

    def _base_ed_list_add(self):
        if not self.base_ed_current:
            self._base_ed_msg("Load a base first.", ok=False)
            return
        kind, bid = self.base_ed_current
        lt = self.base_ed_list_list.get()
        et = self.base_ed_list_entrytype.get()
        eid_str = self.base_ed_list_entryid.get().strip()
        if not eid_str.isdigit():
            messagebox.showerror("Input error", "Entry ID must be a positive integer.")
            return
        eid = int(eid_str)
        # Validate referenced entity exists
        try:
            conn = self._base_ed_rw_conn()
            found_name = None
            if et == 'ship':
                r = conn.execute("SELECT name FROM ships WHERE ship_id = ?",
                                   (eid,)).fetchone()
                if r:
                    found_name = r['name']
            elif et == 'faction':
                r = conn.execute(
                    "SELECT name FROM universe.factions WHERE faction_id = ?",
                    (eid,)
                ).fetchone()
                if r:
                    found_name = r['name']
            elif et == 'base':
                for tbl, idcol in (('starbases', 'base_id'),
                                     ('surface_ports', 'port_id'),
                                     ('outposts', 'outpost_id')):
                    r = conn.execute(
                        f"SELECT name FROM {tbl} WHERE {idcol} = ?", (eid,)
                    ).fetchone()
                    if r:
                        found_name = r['name']
                        break
            if not found_name:
                conn.close()
                messagebox.showerror("Not found",
                                      f"{et} {eid} does not exist.")
                return
            # Check duplicate
            dup = conn.execute(
                "SELECT 1 FROM base_combat_lists "
                "WHERE game_id = ? AND base_kind = ? AND base_id = ? "
                "AND list_type = ? AND entry_type = ? AND entry_id = ?",
                (self.config_data['game_id'], kind, bid, lt, et, eid)
            ).fetchone()
            if dup:
                conn.close()
                self._base_ed_msg(f"Already on {lt.upper()}: {et} {found_name} ({eid})",
                                    ok=False)
                return
            conn.execute(
                "INSERT INTO base_combat_lists "
                "(game_id, base_kind, base_id, list_type, entry_type, entry_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (self.config_data['game_id'], kind, bid, lt, et, eid)
            )
            conn.commit()
            conn.close()
            self._base_ed_refresh_combat()
            self._base_ed_msg(f"Added to {lt.upper()}: {et} {found_name} ({eid})")
        except Exception as ex:
            self._base_ed_msg(f"Add failed: {ex}", ok=False)

    def _base_ed_list_remove(self):
        if not self.base_ed_current:
            self._base_ed_msg("Load a base first.", ok=False)
            return
        kind, bid = self.base_ed_current
        lt = self.base_ed_list_list.get()
        tv = self.base_ed_list_trees[lt]
        sel = tv.selection()
        if not sel:
            messagebox.showerror("No selection",
                                  f"Select a row in the {lt.upper()} list first.")
            return
        vals = tv.item(sel[0])['values']
        et, eid = str(vals[0]), int(vals[1])
        try:
            conn = self._base_ed_rw_conn()
            conn.execute(
                "DELETE FROM base_combat_lists "
                "WHERE game_id = ? AND base_kind = ? AND base_id = ? "
                "AND list_type = ? AND entry_type = ? AND entry_id = ?",
                (self.config_data['game_id'], kind, bid, lt, et, eid)
            )
            conn.commit()
            conn.close()
            self._base_ed_refresh_combat()
            self._base_ed_msg(f"Removed from {lt.upper()}: {et} {eid}")
        except Exception as ex:
            self._base_ed_msg(f"Remove failed: {ex}", ok=False)

    def _base_ed_list_clear(self):
        if not self.base_ed_current:
            self._base_ed_msg("Load a base first.", ok=False)
            return
        kind, bid = self.base_ed_current
        lt = self.base_ed_list_list.get()
        if not messagebox.askyesno("Confirm",
                                      f"Clear the entire {lt.upper()} list?"):
            return
        try:
            conn = self._base_ed_rw_conn()
            conn.execute(
                "DELETE FROM base_combat_lists "
                "WHERE game_id = ? AND base_kind = ? AND base_id = ? "
                "AND list_type = ?",
                (self.config_data['game_id'], kind, bid, lt)
            )
            conn.commit()
            conn.close()
            self._base_ed_refresh_combat()
            self._base_ed_msg(f"{lt.upper()} list cleared.")
        except Exception as ex:
            self._base_ed_msg(f"Clear failed: {ex}", ok=False)

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
