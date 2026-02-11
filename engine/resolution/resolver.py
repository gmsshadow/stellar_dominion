"""
Stellar Dominion - Turn Resolution Engine
Resolves orders for ships, deducting TU and updating game state.
"""

import random
import hashlib
from datetime import datetime
from db.database import get_connection
from engine.maps.system_map import (
    col_to_index, grid_distance, render_system_map, render_location_scan
)


# TU costs for v1 (flat costs)
TU_COSTS = {
    'WAIT': 0,         # cost is the parameter value itself
    'MOVE': 20,        # per move action
    'LOCATIONSCAN': 20,
    'SYSTEMSCAN': 20,
    'ORBIT': 10,
    'DOCK': 30,
    'UNDOCK': 10,
}


class TurnResolver:
    """Resolves a turn for a single ship."""

    def __init__(self, db_path=None, game_id="OMICRON101"):
        self.db_path = db_path
        self.game_id = game_id
        self.conn = get_connection(db_path)
        self.log = []  # Turn execution log
        self.pending = []  # Failed orders carried forward
        self.contacts = []  # Contacts discovered during turn

    def get_game(self):
        """Get current game state."""
        return self.conn.execute(
            "SELECT * FROM games WHERE game_id = ?", (self.game_id,)
        ).fetchone()

    def get_ship(self, ship_id):
        """Get ship state."""
        return self.conn.execute(
            "SELECT * FROM ships WHERE ship_id = ? AND game_id = ?",
            (ship_id, self.game_id)
        ).fetchone()

    def get_system_objects(self, system_id):
        """Get all known objects in a star system."""
        objects = []

        # Star
        system = self.conn.execute(
            "SELECT * FROM star_systems WHERE system_id = ?", (system_id,)
        ).fetchone()
        if system:
            objects.append({
                'type': 'star', 'id': system_id,
                'name': system['star_name'],
                'col': system['star_grid_col'], 'row': system['star_grid_row'],
                'symbol': '*'
            })

        # Celestial bodies
        bodies = self.conn.execute(
            "SELECT * FROM celestial_bodies WHERE system_id = ?", (system_id,)
        ).fetchall()
        for b in bodies:
            objects.append({
                'type': b['body_type'], 'id': b['body_id'],
                'name': b['name'],
                'col': b['grid_col'], 'row': b['grid_row'],
                'symbol': b['map_symbol']
            })

        # Bases
        bases = self.conn.execute(
            "SELECT * FROM starbases WHERE system_id = ? AND game_id = ?",
            (system_id, self.game_id)
        ).fetchall()
        for base in bases:
            objects.append({
                'type': 'base', 'id': base['base_id'],
                'name': base['name'],
                'col': base['grid_col'], 'row': base['grid_row'],
                'symbol': 'B',
                'base_type': base['base_type']
            })

        # Other ships
        ships = self.conn.execute(
            "SELECT * FROM ships WHERE system_id = ? AND game_id = ?",
            (system_id, self.game_id)
        ).fetchall()
        for s in ships:
            objects.append({
                'type': 'ship', 'id': s['ship_id'],
                'name': s['name'],
                'col': s['grid_col'], 'row': s['grid_row'],
                'symbol': '@',
                'ship_class': s['ship_class'],
                'hull_count': s['hull_count'],
                'hull_type': s['hull_type']
            })

        return objects

    def resolve_ship_turn(self, ship_id, orders):
        """
        Resolve all orders for a ship in sequence.
        
        orders: list of dicts with {command, params, sequence}
        Returns: execution log and final state.
        """
        ship = self.get_ship(ship_id)
        if not ship:
            return {'error': f'Ship {ship_id} not found'}

        game = self.get_game()
        system_id = ship['system_id']

        # Generate deterministic RNG seed
        seed_str = f"{self.game_id}-{game['current_year']}.{game['current_week']}-{ship_id}"
        seed = int(hashlib.md5(seed_str.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)

        # Track ship state during resolution
        state = {
            'ship_id': ship_id,
            'name': ship['name'],
            'system_id': system_id,
            'col': ship['grid_col'],
            'row': ship['grid_row'],
            'tu': ship['tu_per_turn'],  # Reset TU at start of turn
            'docked_at': ship['docked_at_base_id'],
            'orbiting': ship['orbiting_body_id'],
            'start_col': ship['grid_col'],
            'start_row': ship['grid_row'],
            'start_tu': ship['tu_per_turn'],
        }

        self.log = []
        self.pending = []
        self.contacts = []

        # Execute each order in sequence
        for order in orders:
            result = self._execute_order(state, order, rng)
            self.log.append(result)

        # Commit final ship state to database
        self._commit_ship_state(state)

        # Update known contacts
        political_id = ship['owner_political_id']
        self._update_contacts(political_id, system_id)

        return {
            'ship_id': ship_id,
            'ship_name': ship['name'],
            'system_id': system_id,
            'start_col': state['start_col'],
            'start_row': state['start_row'],
            'start_tu': state['start_tu'],
            'start_orbiting': ship['orbiting_body_id'],
            'start_docked': ship['docked_at_base_id'],
            'final_col': state['col'],
            'final_row': state['row'],
            'final_tu': state['tu'],
            'docked_at': state['docked_at'],
            'orbiting': state['orbiting'],
            'log': self.log,
            'pending': self.pending,
            'contacts': self.contacts,
            'rng_seed': seed_str,
            'turn_year': game['current_year'],
            'turn_week': game['current_week'],
        }

    def _execute_order(self, state, order, rng):
        """Execute a single order, modifying state in place."""
        cmd = order['command']
        params = order['params']
        tu_before = state['tu']

        if cmd == 'WAIT':
            return self._cmd_wait(state, params)
        elif cmd == 'MOVE':
            return self._cmd_move(state, params)
        elif cmd == 'LOCATIONSCAN':
            return self._cmd_location_scan(state, rng)
        elif cmd == 'SYSTEMSCAN':
            return self._cmd_system_scan(state)
        elif cmd == 'ORBIT':
            return self._cmd_orbit(state, params)
        elif cmd == 'DOCK':
            return self._cmd_dock(state, params)
        elif cmd == 'UNDOCK':
            return self._cmd_undock(state)
        else:
            return {
                'command': cmd, 'params': params,
                'tu_before': tu_before, 'tu_after': tu_before,
                'success': False, 'message': f"Unknown command: {cmd}"
            }

    def _cmd_wait(self, state, tu_amount):
        """WAIT n - consume n TU."""
        tu_before = state['tu']
        cost = min(tu_amount, state['tu'])  # Can't wait more than you have

        if state['tu'] < tu_amount:
            # Partial wait
            state['tu'] = 0
            return {
                'command': 'WAIT', 'params': tu_amount,
                'tu_before': tu_before, 'tu_after': 0,
                'tu_cost': cost,
                'success': True,
                'message': f"Waiting complete (partial: {cost} of {tu_amount} TU)."
            }

        state['tu'] -= tu_amount
        return {
            'command': 'WAIT', 'params': tu_amount,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': tu_amount,
            'success': True,
            'message': "Waiting complete."
        }

    def _cmd_move(self, state, params):
        """MOVE {coord} - move to grid coordinate."""
        tu_before = state['tu']
        target_col = params['col']
        target_row = params['row']
        cost = TU_COSTS['MOVE']

        if state['tu'] < cost:
            self.pending.append({
                'command': 'MOVE', 'params': f"{target_col}{target_row:02d}",
                'reason': 'Insufficient TU'
            })
            return {
                'command': 'MOVE', 'params': f"{target_col}{target_row:02d}",
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False,
                'message': f"Insufficient TU for move ({state['tu']} < {cost}). Order queued as pending."
            }

        # If docked, must undock first
        if state['docked_at']:
            return {
                'command': 'MOVE', 'params': f"{target_col}{target_row:02d}",
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False,
                'message': "Cannot move while docked. UNDOCK first. Order queued as pending."
            }

        # Leave orbit if orbiting
        orbit_msg = ""
        if state['orbiting']:
            body = self.conn.execute(
                "SELECT name FROM celestial_bodies WHERE body_id = ?",
                (state['orbiting'],)
            ).fetchone()
            body_name = body['name'] if body else str(state['orbiting'])
            orbit_msg = f"Leaving orbit of {body_name}.\n    "
            state['orbiting'] = None

        old_loc = f"{state['col']}{state['row']:02d}"
        state['col'] = target_col
        state['row'] = target_row
        state['tu'] -= cost
        new_loc = f"{target_col}{target_row:02d}"

        return {
            'command': 'MOVE', 'params': new_loc,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': cost,
            'success': True,
            'message': f"{orbit_msg}Ship moved to {new_loc}."
        }

    def _cmd_location_scan(self, state, rng):
        """LOCATIONSCAN - scan nearby cells for objects."""
        tu_before = state['tu']
        cost = TU_COSTS['LOCATIONSCAN']

        if state['tu'] < cost:
            self.pending.append({
                'command': 'LOCATIONSCAN', 'params': None,
                'reason': 'Insufficient TU'
            })
            return {
                'command': 'LOCATIONSCAN', 'params': None,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False,
                'message': f"Insufficient TU for scan ({state['tu']} < {cost}). Order queued as pending."
            }

        state['tu'] -= cost
        objects = self.get_system_objects(state['system_id'])

        # Filter to nearby objects (scan radius based on sensor rating)
        scan_radius = 8  # Default for v1
        detected = []
        for obj in objects:
            if obj['type'] == 'ship' and obj['id'] == state['ship_id']:
                continue  # Don't detect self
            dist = grid_distance(state['col'], state['row'], obj['col'], obj['row'])
            if dist <= scan_radius:
                detected.append(obj)
                self.contacts.append(obj)

        if detected:
            scan_lines = ["Scan complete. Detected:"]
            for obj in detected:
                loc = f"{obj['col']}{obj['row']:02d}"
                scan_lines.append(f"    {obj['name']} ({obj['id']}) at {loc}")
        else:
            scan_lines = ["Scan complete. No contacts detected."]

        return {
            'command': 'LOCATIONSCAN', 'params': None,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': cost,
            'success': True,
            'message': "\n".join(scan_lines),
            'detected': detected
        }

    def _cmd_system_scan(self, state):
        """SYSTEMSCAN - produce full system map."""
        tu_before = state['tu']
        cost = TU_COSTS['SYSTEMSCAN']

        if state['tu'] < cost:
            self.pending.append({
                'command': 'SYSTEMSCAN', 'params': None,
                'reason': 'Insufficient TU'
            })
            return {
                'command': 'SYSTEMSCAN', 'params': None,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False,
                'message': f"Insufficient TU for system scan ({state['tu']} < {cost})."
            }

        state['tu'] -= cost
        system = self.conn.execute(
            "SELECT * FROM star_systems WHERE system_id = ?", (state['system_id'],)
        ).fetchone()

        objects = self.get_system_objects(state['system_id'])
        # Remove the current ship from objects so we can show it as @
        map_objects = [o for o in objects if not (o['type'] == 'ship' and o['id'] == state['ship_id'])]

        # Add all detected objects as contacts
        for obj in objects:
            if obj['type'] != 'ship' or obj['id'] != state['ship_id']:
                self.contacts.append(obj)

        system_data = {
            'star_col': system['star_grid_col'],
            'star_row': system['star_grid_row']
        }

        ascii_map = render_system_map(
            system_data, map_objects,
            ship_position=(state['col'], state['row'])
        )

        return {
            'command': 'SYSTEMSCAN', 'params': None,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': cost,
            'success': True,
            'message': f"System scan complete.\n{ascii_map}",
            'map': ascii_map
        }

    def _cmd_orbit(self, state, body_id):
        """ORBIT {body_id} - enter orbit of a celestial body."""
        tu_before = state['tu']
        cost = TU_COSTS['ORBIT']

        if state['tu'] < cost:
            self.pending.append({
                'command': 'ORBIT', 'params': body_id,
                'reason': 'Insufficient TU'
            })
            return {
                'command': 'ORBIT', 'params': body_id,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False,
                'message': f"Insufficient TU for orbit ({state['tu']} < {cost}). Order queued as pending."
            }

        # Check body exists and is at ship's location
        body = self.conn.execute(
            "SELECT * FROM celestial_bodies WHERE body_id = ? AND system_id = ?",
            (body_id, state['system_id'])
        ).fetchone()

        if not body:
            return {
                'command': 'ORBIT', 'params': body_id,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False,
                'message': f"Unable to orbit: celestial body {body_id} not found in this system."
            }

        if body['grid_col'] != state['col'] or body['grid_row'] != state['row']:
            loc = f"{body['grid_col']}{body['grid_row']:02d}"
            self.pending.append({
                'command': 'ORBIT', 'params': body_id,
                'reason': f'Not at body location ({loc})'
            })
            return {
                'command': 'ORBIT', 'params': body_id,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False,
                'message': f"Unable to orbit: ship is not at {body['name']} location ({loc}). Order queued as pending."
            }

        state['orbiting'] = body_id
        state['tu'] -= cost

        return {
            'command': 'ORBIT', 'params': body_id,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': cost,
            'success': True,
            'message': f"Ship entered orbit of {body['name']} ({body_id}) [{body['gravity']}g]"
        }

    def _cmd_dock(self, state, base_id):
        """DOCK {base_id} - dock at a starbase."""
        tu_before = state['tu']
        cost = TU_COSTS['DOCK']

        if state['tu'] < cost:
            self.pending.append({
                'command': 'DOCK', 'params': base_id,
                'reason': 'Insufficient TU'
            })
            return {
                'command': 'DOCK', 'params': base_id,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False,
                'message': f"Insufficient TU for docking ({state['tu']} < {cost}). Order queued as pending."
            }

        # Check base exists
        base = self.conn.execute(
            "SELECT * FROM starbases WHERE base_id = ? AND game_id = ?",
            (base_id, self.game_id)
        ).fetchone()

        if not base:
            return {
                'command': 'DOCK', 'params': base_id,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False,
                'message': f"Unable to dock: base {base_id} not found."
            }

        # Check ship is at base location
        if base['grid_col'] != state['col'] or base['grid_row'] != state['row']:
            loc = f"{base['grid_col']}{base['grid_row']:02d}"
            self.pending.append({
                'command': 'DOCK', 'params': base_id,
                'reason': f'Not at base location ({loc})'
            })
            return {
                'command': 'DOCK', 'params': base_id,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False,
                'message': f"Unable to dock: ship is not at base location ({loc}). Order queued as pending."
            }

        state['docked_at'] = base_id
        state['tu'] -= cost

        # Scan contacts at dock location
        dock_contacts = self._scan_at_location(state)
        contact_msg = ""
        if dock_contacts:
            lines = ["    Scanned:"]
            for c in dock_contacts:
                if c['type'] == 'ship':
                    lines.append(f"        {c['name']} ({c['id']}) - {{{c.get('hull_count', '?')} {c.get('hull_type', 'Hulls')}}}")
                elif c['type'] == 'base':
                    lines.append(f"        {c['name']} ({c['id']})")
            contact_msg = "\n" + "\n".join(lines)

        return {
            'command': 'DOCK', 'params': base_id,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': cost,
            'success': True,
            'message': f"Docking at {base['base_type']} {base['name']} ({base_id}).{contact_msg}"
        }

    def _cmd_undock(self, state):
        """UNDOCK - leave docked starbase."""
        tu_before = state['tu']
        cost = TU_COSTS['UNDOCK']

        if not state['docked_at']:
            return {
                'command': 'UNDOCK', 'params': None,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False,
                'message': "Unable to undock: ship is not docked at any base."
            }

        if state['tu'] < cost:
            self.pending.append({
                'command': 'UNDOCK', 'params': None,
                'reason': 'Insufficient TU'
            })
            return {
                'command': 'UNDOCK', 'params': None,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False,
                'message': f"Insufficient TU to undock ({state['tu']} < {cost}). Order queued as pending."
            }

        base_id = state['docked_at']
        base = self.conn.execute(
            "SELECT name FROM starbases WHERE base_id = ?", (base_id,)
        ).fetchone()
        base_name = base['name'] if base else str(base_id)

        state['docked_at'] = None
        state['tu'] -= cost

        return {
            'command': 'UNDOCK', 'params': None,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': cost,
            'success': True,
            'message': f"Undocked from {base_name} ({base_id})."
        }

    def _scan_at_location(self, state):
        """Scan for objects at the ship's current location."""
        objects = self.get_system_objects(state['system_id'])
        at_location = []
        for obj in objects:
            if obj['col'] == state['col'] and obj['row'] == state['row']:
                if obj['type'] == 'ship' and obj['id'] == state['ship_id']:
                    continue
                at_location.append(obj)
                self.contacts.append(obj)
        return at_location

    def _commit_ship_state(self, state):
        """Write final ship state back to database."""
        self.conn.execute("""
            UPDATE ships SET
                grid_col = ?, grid_row = ?, tu_remaining = ?,
                docked_at_base_id = ?, orbiting_body_id = ?
            WHERE ship_id = ? AND game_id = ?
        """, (
            state['col'], state['row'], state['tu'],
            state['docked_at'], state['orbiting'],
            state['ship_id'], self.game_id
        ))
        self.conn.commit()

    def _update_contacts(self, political_id, system_id):
        """Update the known contacts list for the player."""
        game = self.get_game()
        for contact in self.contacts:
            # Check if already known
            existing = self.conn.execute("""
                SELECT contact_id FROM known_contacts
                WHERE political_id = ? AND object_type = ? AND object_id = ?
            """, (political_id, contact['type'], contact['id'])).fetchone()

            if existing:
                # Update location
                self.conn.execute("""
                    UPDATE known_contacts SET 
                        location_col = ?, location_row = ?,
                        location_system = ?
                    WHERE contact_id = ?
                """, (contact['col'], contact['row'], system_id, existing['contact_id']))
            else:
                self.conn.execute("""
                    INSERT INTO known_contacts 
                    (political_id, object_type, object_id, object_name,
                     location_system, location_col, location_row,
                     discovered_turn_year, discovered_turn_week)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    political_id, contact['type'], contact['id'], contact['name'],
                    system_id, contact['col'], contact['row'],
                    game['current_year'], game['current_week']
                ))
        self.conn.commit()

    def advance_turn(self):
        """Advance the game turn (year.week)."""
        game = self.get_game()
        year = game['current_year']
        week = game['current_week']

        if week >= 52:
            year += 1
            week = 1
        else:
            week += 1

        self.conn.execute("""
            UPDATE games SET current_year = ?, current_week = ?
            WHERE game_id = ?
        """, (year, week, self.game_id))
        self.conn.commit()
        return year, week

    def close(self):
        """Close database connection."""
        self.conn.close()
