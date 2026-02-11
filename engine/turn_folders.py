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
            42268153/                     ← Alice's political ID
              ship_57131458.txt           ← ship report
              political_42268153.txt      ← political summary
            85545143/                     ← Bob's political ID
              ship_88234561.txt
              political_85545143.txt
"""

import shutil
import json
from pathlib import Path
from datetime import datetime

from db.database import get_connection


class TurnFolders:
    """Manages the turn file system layout."""

    def __init__(self, base_dir=None, db_path=None, game_id="HANF231"):
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
    # Processed output
    # ------------------------------------------------------------------

    def get_processed_dir(self, turn_str, political_id):
        """Get (and create) the processed folder for a political position."""
        folder = self.processed_dir / turn_str / str(political_id)
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def store_ship_report(self, turn_str, political_id, ship_id, report_text):
        """Store a ship turn report in the correct processed folder."""
        folder = self.get_processed_dir(turn_str, political_id)
        report_file = folder / f"ship_{ship_id}.txt"
        report_file.write_text(report_text)
        return report_file

    def store_political_report(self, turn_str, political_id, report_text):
        """Store a political turn report in the correct processed folder."""
        folder = self.get_processed_dir(turn_str, political_id)
        report_file = folder / f"political_{political_id}.txt"
        report_file.write_text(report_text)
        return report_file

    def list_processed(self, turn_str=None):
        """
        List all processed reports for a turn, grouped by political ID.
        Returns dict of political_id -> list of report file paths.
        """
        if turn_str is None:
            turn_str = self.get_current_turn_str()

        turn_dir = self.processed_dir / turn_str
        if not turn_dir.exists():
            return {}

        results = {}
        for pol_dir in sorted(turn_dir.iterdir()):
            if not pol_dir.is_dir():
                continue
            political_id = pol_dir.name
            reports = []
            for f in sorted(pol_dir.iterdir()):
                if f.suffix == ".txt":
                    reports.append(f)
            if reports:
                results[political_id] = reports
        return results

    def get_player_reports(self, turn_str, political_id):
        """Get all report files for a specific player/turn."""
        folder = self.processed_dir / turn_str / str(political_id)
        if not folder.exists():
            return []
        return sorted(folder.glob("*.txt"))

    # ------------------------------------------------------------------
    # Email routing lookup
    # ------------------------------------------------------------------

    def get_email_for_political(self, political_id):
        """Look up the email address for a political position."""
        conn = get_connection(self.db_path)
        result = conn.execute("""
            SELECT p.email FROM players p
            JOIN political_positions pp ON p.player_id = pp.player_id
            WHERE pp.position_id = ?
        """, (political_id,)).fetchone()
        conn.close()
        return result['email'] if result else None

    def get_political_for_email(self, email):
        """Look up the political position ID for an email address."""
        conn = get_connection(self.db_path)
        result = conn.execute("""
            SELECT pp.position_id FROM political_positions pp
            JOIN players p ON pp.player_id = p.player_id
            WHERE p.email = ? AND pp.game_id = ?
        """, (email, self.game_id)).fetchone()
        conn.close()
        return result['position_id'] if result else None

    def validate_ship_ownership(self, email, ship_id):
        """
        Validate that the given email owns the given ship.
        Returns (valid, political_id, error_message).
        """
        conn = get_connection(self.db_path)

        # Find player by email
        player = conn.execute(
            "SELECT player_id FROM players WHERE email = ? AND game_id = ?",
            (email, self.game_id)
        ).fetchone()
        if not player:
            conn.close()
            return False, None, f"No player registered with email '{email}' in game {self.game_id}"

        # Find political position for player
        political = conn.execute(
            "SELECT position_id FROM political_positions WHERE player_id = ? AND game_id = ?",
            (player['player_id'], self.game_id)
        ).fetchone()
        if not political:
            conn.close()
            return False, None, f"No political position found for player"

        # Check ship ownership
        ship = conn.execute(
            "SELECT ship_id, name, owner_political_id FROM ships WHERE ship_id = ? AND game_id = ?",
            (int(ship_id), self.game_id)
        ).fetchone()
        if not ship:
            conn.close()
            return False, political['position_id'], f"Ship {ship_id} not found in game {self.game_id}"

        if ship['owner_political_id'] != political['position_id']:
            conn.close()
            return False, political['position_id'], (
                f"Ship {ship['name']} ({ship_id}) is not owned by your political position. "
                f"It belongs to political {ship['owner_political_id']}"
            )

        conn.close()
        return True, political['position_id'], None

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

        # Get all players in the game
        players = conn.execute("""
            SELECT p.email, p.player_name, pp.position_id, pp.name as political_name
            FROM players p
            JOIN political_positions pp ON p.player_id = pp.player_id
            WHERE p.game_id = ?
        """, (self.game_id,)).fetchall()

        # Get all ships
        ships = conn.execute("""
            SELECT s.ship_id, s.name, s.owner_political_id
            FROM ships s WHERE s.game_id = ?
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
            player_ships = [s for s in ships if s['owner_political_id'] == player['position_id']]
            has_processed = str(player['position_id']) in processed
            player_orders = orders_by_email.get(email, set())
            player_rejected = rejected_by_email.get(email, set())

            player_info = {
                'email': email,
                'name': player['player_name'],
                'political_id': player['position_id'],
                'political_name': player['political_name'],
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
