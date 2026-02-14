"""
Stellar Dominion - Turn Folder Manager
Manages the folder structure for incoming orders and processed turn reports.

Folder layout:
    game_data/
      turns/
        incoming/                         ← raw orders arrive here
          500.1/
            alice@example.com/
              orders_57131458.yaml        ← valid orders for ship
              orders_57131458.yaml.receipt ← confirmation of acceptance
              rejected_99999999.yaml      ← orders that failed validation
              rejected_99999999.reason     ← explanation of rejection
            bob@example.com/
              orders_88234561.yaml

        processed/                        ← resolved turn output
          500.1/
            38291047/                     ← Alice's account number (secret)
              ship_57131458.txt           ← ship report
              prefect_42268153.txt      ← prefect summary
            71503928/                     ← Bob's account number (secret)
              ship_88234561.txt
              prefect_85545143.txt
"""

import shutil
import json
from pathlib import Path
from datetime import datetime

from db.database import get_connection


class TurnFolders:
    """Manages the turn file system layout."""

    def __init__(self, base_dir=None, db_path=None, game_id="OMICRON101"):
        self.game_id = game_id
        self.db_path = db_path
        if base_dir:
            self.base_dir = Path(base_dir)
        else:
            self.base_dir = Path(__file__).parent.parent / "game_data" / "turns"
        self.incoming_dir = self.base_dir / "incoming"
        self.processed_dir = self.base_dir / "processed"

    # ------------------------------------------------------------------
    # Turn string helpers
    # ------------------------------------------------------------------

    def get_current_turn_str(self):
        """Get the current turn as 'YEAR.WEEK' string from the database."""
        conn = get_connection(self.db_path)
        game = conn.execute(
            "SELECT current_year, current_week FROM games WHERE game_id = ?",
            (self.game_id,)
        ).fetchone()
        conn.close()
        if not game:
            raise ValueError(f"Game {self.game_id} not found")
        return f"{game['current_year']}.{game['current_week']}"

    # ------------------------------------------------------------------
    # Incoming orders
    # ------------------------------------------------------------------

    def get_incoming_dir(self, turn_str, email):
        """Get (and create) the incoming folder for a player's orders."""
        folder = self.incoming_dir / turn_str / email
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def store_incoming_orders(self, turn_str, email, ship_id, orders_content,
                              filename=None):
        """
        Store an incoming orders file in the correct folder.
        Returns the path to the stored file.
        """
        folder = self.get_incoming_dir(turn_str, email)
        if filename is None:
            filename = f"orders_{ship_id}.yaml"
        dest = folder / filename
        dest.write_text(orders_content)
        return dest

    def store_receipt(self, turn_str, email, ship_id, receipt_info):
        """
        Write a receipt file confirming order acceptance.
        receipt_info: dict with validation results.
        """
        folder = self.get_incoming_dir(turn_str, email)
        receipt_file = folder / f"orders_{ship_id}.yaml.receipt"
        lines = [
            f"Stellar Dominion - Order Receipt",
            f"================================",
            f"Game:      {self.game_id}",
            f"Turn:      {turn_str}",
            f"Ship:      {ship_id}",
            f"Email:     {email}",
            f"Received:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Status:    {receipt_info.get('status', 'accepted')}",
            f"Orders:    {receipt_info.get('order_count', 0)} valid",
        ]
        if receipt_info.get('warnings'):
            lines.append(f"Warnings:")
            for w in receipt_info['warnings']:
                lines.append(f"  - {w}")
        receipt_file.write_text("\n".join(lines))
        return receipt_file

    def store_rejected(self, turn_str, email, ship_id, orders_content, reasons):
        """
        Store a rejected orders file with explanation.
        """
        folder = self.get_incoming_dir(turn_str, email)
        # Save the original orders
        rejected_file = folder / f"rejected_{ship_id}.yaml"
        rejected_file.write_text(orders_content)
        # Save the reason
        reason_file = folder / f"rejected_{ship_id}.reason"
        lines = [
            f"Stellar Dominion - Order Rejection",
            f"===================================",
            f"Game:      {self.game_id}",
            f"Turn:      {turn_str}",
            f"Ship:      {ship_id}",
            f"Email:     {email}",
            f"Rejected:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"",
            f"Reasons:",
        ]
        for r in reasons:
            lines.append(f"  - {r}")
        reason_file.write_text("\n".join(lines))
        return rejected_file, reason_file

    def list_incoming(self, turn_str=None):
        """
        List all incoming orders for a turn.
        Returns list of dicts with email, ship_id, filepath, status.
        """
        if turn_str is None:
            turn_str = self.get_current_turn_str()

        turn_dir = self.incoming_dir / turn_str
        if not turn_dir.exists():
            return []

        results = []
        for email_dir in sorted(turn_dir.iterdir()):
            if not email_dir.is_dir():
                continue
            email = email_dir.name
            for f in sorted(email_dir.iterdir()):
                if f.name.startswith("orders_") and f.suffix == ".yaml":
                    ship_id = f.stem.replace("orders_", "")
                    has_receipt = (email_dir / f"{f.name}.receipt").exists()
                    results.append({
                        'email': email,
                        'ship_id': ship_id,
                        'filepath': f,
                        'status': 'received' if has_receipt else 'pending',
                    })
                elif f.name.startswith("rejected_") and f.suffix == ".yaml":
                    ship_id = f.stem.replace("rejected_", "")
                    results.append({
                        'email': email,
                        'ship_id': ship_id,
                        'filepath': f,
                        'status': 'rejected',
                    })
        return results

    # ------------------------------------------------------------------
    # Processed output (keyed by account number)
    # ------------------------------------------------------------------

    def get_processed_dir(self, turn_str, account_number):
        """Get (and create) the processed folder for a player's account."""
        folder = self.processed_dir / turn_str / str(account_number)
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def store_ship_report(self, turn_str, account_number, ship_id, report_text):
        """Store a ship turn report in the correct processed folder."""
        folder = self.get_processed_dir(turn_str, account_number)
        report_file = folder / f"ship_{ship_id}.txt"
        report_file.write_text(report_text)
        return report_file

    def store_prefect_report(self, turn_str, account_number, prefect_id, report_text):
        """Store a prefect turn report in the correct processed folder."""
        folder = self.get_processed_dir(turn_str, account_number)
        report_file = folder / f"prefect_{prefect_id}.txt"
        report_file.write_text(report_text)
        return report_file

    def list_processed(self, turn_str=None):
        """
        List all processed reports for a turn, grouped by account number.
        Returns dict of account_number -> list of report file paths.
        """
        if turn_str is None:
            turn_str = self.get_current_turn_str()

        turn_dir = self.processed_dir / turn_str
        if not turn_dir.exists():
            return {}

        results = {}
        for acct_dir in sorted(turn_dir.iterdir()):
            if not acct_dir.is_dir():
                continue
            account_number = acct_dir.name
            reports = []
            for f in sorted(acct_dir.iterdir()):
                if f.suffix == ".txt":
                    reports.append(f)
            if reports:
                results[account_number] = reports
        return results

    def get_player_reports(self, turn_str, account_number):
        """Get all report files for a specific player/turn."""
        folder = self.processed_dir / turn_str / str(account_number)
        if not folder.exists():
            return []
        return sorted(folder.glob("*.txt"))

    # ------------------------------------------------------------------
    # Routing lookups
    # ------------------------------------------------------------------

    def get_email_for_account(self, account_number):
        """Look up the email address for an account number."""
        conn = get_connection(self.db_path)
        result = conn.execute(
            "SELECT email FROM players WHERE account_number = ?",
            (str(account_number),)
        ).fetchone()
        conn.close()
        return result['email'] if result else None

    def get_account_for_email(self, email):
        """Look up the account number for an email address."""
        conn = get_connection(self.db_path)
        result = conn.execute(
            "SELECT account_number FROM players WHERE email = ? AND game_id = ?",
            (email, self.game_id)
        ).fetchone()
        conn.close()
        return result['account_number'] if result else None

    def get_account_for_prefect(self, prefect_id):
        """Look up the account number for a prefect."""
        conn = get_connection(self.db_path)
        result = conn.execute("""
            SELECT p.account_number FROM players p
            JOIN prefects pp ON p.player_id = pp.player_id
            WHERE pp.prefect_id = ?
        """, (prefect_id,)).fetchone()
        conn.close()
        return result['account_number'] if result else None

    def validate_ship_ownership(self, email, ship_id, account_number=None):
        """
        Validate that the given email owns the given ship.
        If account_number is provided, also verify it matches the player.
        Returns (valid, account_number, error_message).
        """
        conn = get_connection(self.db_path)

        # Find player by email
        player = conn.execute(
            "SELECT player_id, account_number, status FROM players WHERE email = ? AND game_id = ?",
            (email, self.game_id)
        ).fetchone()
        if not player:
            conn.close()
            return False, None, f"No player registered with email '{email}' in game {self.game_id}"

        # Verify account number if provided
        if account_number and str(account_number) != str(player['account_number']):
            conn.close()
            return False, None, (
                f"Account number does not match the player registered with '{email}'. "
                f"Check your account number and try again."
            )

        if player['status'] == 'suspended':
            conn.close()
            return False, player['account_number'], (
                f"Account for '{email}' is currently suspended. "
                f"Orders cannot be submitted while suspended."
            )

        account_number = player['account_number']

        # Find prefect for player
        prefect = conn.execute(
            "SELECT prefect_id FROM prefects WHERE player_id = ? AND game_id = ?",
            (player['player_id'], self.game_id)
        ).fetchone()
        if not prefect:
            conn.close()
            return False, account_number, f"No prefect found for player"

        # Check ship ownership
        ship = conn.execute(
            "SELECT ship_id, name, owner_prefect_id FROM ships WHERE ship_id = ? AND game_id = ?",
            (int(ship_id), self.game_id)
        ).fetchone()
        if not ship:
            conn.close()
            return False, account_number, f"Ship {ship_id} not found in game {self.game_id}"

        if ship['owner_prefect_id'] != prefect['prefect_id']:
            conn.close()
            return False, account_number, (
                f"Ship {ship['name']} ({ship_id}) is not owned by your prefect. "
                f"It belongs to prefect {ship['owner_prefect_id']}"
            )

        conn.close()
        return True, account_number, None

    # ------------------------------------------------------------------
    # Summary / status
    # ------------------------------------------------------------------

    def get_turn_summary(self, turn_str=None):
        """
        Get a full summary of a turn's status.
        Returns dict with incoming and processed info.
        """
        if turn_str is None:
            turn_str = self.get_current_turn_str()

        conn = get_connection(self.db_path)

        # Get all active players in the game (now including account_number)
        players = conn.execute("""
            SELECT p.email, p.player_name, p.account_number,
                   pp.prefect_id, pp.name as prefect_name
            FROM players p
            JOIN prefects pp ON p.player_id = pp.player_id
            WHERE p.game_id = ? AND p.status = 'active'
        """, (self.game_id,)).fetchall()

        # Get all ships (only active players)
        ships = conn.execute("""
            SELECT s.ship_id, s.name, s.owner_prefect_id
            FROM ships s
            JOIN prefects pp ON s.owner_prefect_id = pp.prefect_id
            JOIN players p ON pp.player_id = p.player_id
            WHERE s.game_id = ? AND p.status = 'active'
        """, (self.game_id,)).fetchall()

        conn.close()

        incoming = self.list_incoming(turn_str)
        processed = self.list_processed(turn_str)

        # Build per-email lookup: which ships have orders from which email?
        orders_by_email = {}
        rejected_by_email = {}
        for i in incoming:
            email = i['email']
            if i['status'] != 'rejected':
                orders_by_email.setdefault(email, set()).add(i['ship_id'])
            else:
                rejected_by_email.setdefault(email, set()).add(i['ship_id'])

        summary = {
            'turn': turn_str,
            'game_id': self.game_id,
            'players': [],
        }

        for player in players:
            email = player['email']
            account_number = player['account_number']
            player_ships = [s for s in ships if s['owner_prefect_id'] == player['prefect_id']]
            has_processed = account_number in processed
            player_orders = orders_by_email.get(email, set())
            player_rejected = rejected_by_email.get(email, set())

            player_info = {
                'email': email,
                'name': player['player_name'],
                'account_number': account_number,
                'prefect_id': player['prefect_id'],
                'prefect_name': player['prefect_name'],
                'processed': has_processed,
                'ships': [],
            }

            for s in player_ships:
                sid = str(s['ship_id'])
                ship_info = {
                    'ship_id': s['ship_id'],
                    'ship_name': s['name'],
                    'orders_received': sid in player_orders,
                    'orders_rejected': sid in player_rejected,
                }
                player_info['ships'].append(ship_info)

            summary['players'].append(player_info)

        return summary
