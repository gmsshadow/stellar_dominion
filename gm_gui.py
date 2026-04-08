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

        self._build_turn_tab()
        self._build_players_tab()
        self._build_universe_tab()
        self._build_bases_tab()
        self._build_moderator_tab()
        self._build_previews_tab()
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
                    button_width=22):
        """
        Create a row with a button and optional input fields.

        base_args: list of args after `pbem.py` (e.g. ['fetch-mail'])
        fields: list of (key, label, default, width) tuples
                The default builder converts each non-empty entry to `--key value`.
                Use the form `key='some-flag'` for multi-word flags.
        custom_builder: optional callable(entries_dict) -> args list, overrides default
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
    # Tab: Turn Operations
    # ========================================================================

    def _build_turn_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Turn Ops")
        inner = self._scrollable_frame(tab)

        self._section_header(inner, "Email Workflow")
        self._action_row(inner, "Fetch Mail", ['fetch-mail'], [self._game_field()])
        self._action_row(inner, "Process Inbox", ['process-inbox'], [self._game_field()])
        self._action_row(inner, "Review Orders", ['review-orders'], [self._game_field()])

        self._section_header(inner, "Turn Resolution")
        self._action_row(inner, "Run Turn", ['run-turn'], [self._game_field()])
        self._action_row(inner, "Send Turns (Email)", ['send-turns'], [self._game_field()])
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
        self._action_row(inner, "Register Player", ['register-player'],
                         [('name', 'Name:', '', 16),
                          ('email', 'Email:', '', 24)])
        self._action_row(inner, "Join Game", ['join-game'],
                         [('account', 'Account:', '', 12),
                          ('game', 'Game:', self.config_data['game_id'], 14)])

        self._section_header(inner, "Faction Requests")
        self._action_row(inner, "List Faction Requests", ['faction-requests'])
        self._action_row(inner, "Approve Faction", ['approve-faction'],
                         [('request', 'Request ID:', '', 12)])
        self._action_row(inner, "Deny Faction", ['deny-faction'],
                         [('request', 'Request ID:', '', 12),
                          ('reason', 'Reason:', '', 24)])

        self._section_header(inner, "Player Management")
        self._action_row(inner, "Suspend Player", ['suspend-player'],
                         [('account', 'Account:', '', 12)])
        self._action_row(inner, "Reinstate Player", ['reinstate-player'],
                         [('account', 'Account:', '', 12)])
        self._action_row(inner, "Edit Credits", ['edit-credits'],
                         [('prefect', 'Prefect:', '', 12),
                          ('amount', 'Amount:', '', 10)])
        self._action_row(inner, "Generate Order Form", ['generate-form'],
                         [('account', 'Account:', '', 12)])

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
                          ('col', 'Col:', 'M', 4),
                          ('row', 'Row:', '13', 4)])
        self._action_row(inner, "Add Body", ['add-body'],
                         [('name', 'Name:', '', 14),
                          ('system-id', 'Sys:', '101', 5),
                          ('col', 'Col:', 'M', 4),
                          ('row', 'Row:', '13', 4),
                          ('body-type', 'Type:', 'planet', 8),
                          ('gravity', 'Grav:', '1.0', 5)])
        self._action_row(inner, "Add Link", ['add-link'],
                         [('system-a', 'Sys A:', '', 6),
                          ('system-b', 'Sys B:', '', 6)])

        self._section_header(inner, "Surface Generation")
        self._action_row(inner, "Gen Missing Surfaces", ['gen-surfaces'])
        self._action_row(inner, "Regen Surface", ['regen-surface'],
                         [('body-id', 'Body ID:', '', 10)])

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
                                 ('y', 'Y:', '15', 4)])

        self._action_row(inner, "Add Outpost", ['add-outpost'],
                         [('name', 'Name:', '', 14),
                          ('body-id', 'Body:', '', 10),
                          ('x', 'X:', '5', 4),
                          ('y', 'Y:', '5', 4),
                          ('type', 'Type:', 'General', 10)])

        self._section_header(inner, "Base Information")
        self._action_row(inner, "Base Status", ['base-status'],
                         [('base', 'Base ID:', '', 12)])

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
                          ('starbase', 'Starbase:', '', 10)])
        self._action_row(inner, "Add GM Ship", ['add-gm-ship'],
                         [('prefect', 'Prefect:', '', 10),
                          ('ship-name', 'Ship Name:', '', 14),
                          ('hull-type', 'Hull:', 'Commercial', 10)])

        self._section_header(inner, "Action Requests")
        self._action_row(inner, "List Pending Actions", ['list-actions'])
        self._action_row(inner, "Respond to Action", ['respond-action'],
                         [('action', 'Action ID:', '', 10),
                          ('response', 'Response:', '', 30)])

        self._section_header(inner, "Order Manipulation")
        self._action_row(inner, "Inject Order", ['inject-order'],
                         [('ship', 'Ship:', '', 10),
                          ('command', 'Command:', '', 16)])
        self._action_row(inner, "Edit Order", ['edit-order'],
                         [('order-id', 'Order ID:', '', 10)])
        self._action_row(inner, "Delete Order", ['delete-order'],
                         [('order-id', 'Order ID:', '', 10)])
        self._action_row(inner, "Submit Orders", ['submit-orders'],
                         [('account', 'Account:', '', 12),
                          ('file', 'File:', '', 24)])

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
                         [('ship', 'Ship ID:', '', 12)])
        self._action_row(inner, "Preview Base Report", ['preview-base'],
                         [('base', 'Base ID:', '', 12)])

        self._section_header(inner, "Maps")
        self._action_row(inner, "Show System Map", ['show-map'],
                         [('system', 'System:', '101', 6)])
        self._action_row(inner, "Show Surface Map", ['show-surface'],
                         [('body', 'Body ID:', '', 12)])

        self._section_header(inner, "Status & Lists")
        self._action_row(inner, "Show Ship Status", ['show-status'],
                         [('ship', 'Ship ID:', '', 12)])
        self._action_row(inner, "List Ships", ['list-ships'])

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

        ttk.Button(tab, text="Save Settings",
                   command=self._save_settings).pack(pady=20)

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

    def _run_pbem(self, args, label):
        if self.process is not None and self.process.poll() is None:
            messagebox.showwarning("Busy", "Another command is currently running.\nUse Cancel Process to stop it first.")
            return

        cmd = [self.config_data['python_exe'], '-u', str(PBEM_SCRIPT)] + args
        self._append(f"\n>>> {label}\n", "cmd")
        self._append(f"    {' '.join(cmd[2:])}\n", "info")
        self.status_label.config(text=f"Running: {label}", foreground="orange")

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
                elif kind == 'err':
                    self._append(text, "err")
                    self.status_label.config(text="Error", foreground="red")
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
