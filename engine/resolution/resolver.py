"""
Stellar Dominion - Turn Resolution Engine
Resolves orders for ships, deducting TU and updating game state.
"""

import random
import hashlib
import heapq
from datetime import datetime
from db.database import get_connection, get_faction
from engine.maps.system_map import (
    col_to_index, index_to_col, grid_distance, render_system_map, render_location_scan
)


# TU costs for v1
TU_COSTS = {
    'WAIT': 0,         # cost is the parameter value itself
    'MOVE': 2,         # per square moved (incremental)
    'LOCATIONSCAN': 20,
    'SYSTEMSCAN': 20,
    'ORBIT': 10,
    'DOCK': 30,
    'UNDOCK': 10,
    'LAND': 20,
    'TAKEOFF': 20,
    'SURFACESCAN': 20,
    'BUY': 0,          # trading while docked is free
    'SELL': 0,
    'GETMARKET': 0,
    'JUMP': 60,        # hyperspace jump between systems
}


class TurnResolver:
    """Resolves turns for ships, supporting interleaved multi-ship resolution."""

    def __init__(self, db_path=None, game_id="OMICRON101"):
        self.db_path = db_path
        self.game_id = game_id
        self.conn = get_connection(db_path)
        self.log = []  # Turn execution log
        self.pending = []  # Failed orders carried forward
        self.contacts = []  # Contacts discovered during turn

    def _commit_ship_position(self, state):
        """Lightweight mid-turn position update so scans see current positions."""
        self.conn.execute("""
            UPDATE ships SET
                system_id = ?,
                grid_col = ?, grid_row = ?,
                docked_at_base_id = ?, orbiting_body_id = ?,
                landed_body_id = ?, landed_x = ?, landed_y = ?
            WHERE ship_id = ? AND game_id = ?
        """, (
            state['system_id'],
            state['col'], state['row'],
            state['docked_at'], state['orbiting'],
            state['landed'], state['landed_x'], state['landed_y'],
            state['ship_id'], self.game_id
        ))
        self.conn.commit()

    def _cmd_move_step(self, state, target_col, target_row):
        """
        Move exactly one square toward target.
        Returns dict: moved (bool), finished (bool), step (col, row),
                      orbit_msg (str), error_msg (str or None).
        """
        cost = state['move_cost']

        # Already there
        if state['col'] == target_col and state['row'] == target_row:
            return {'moved': False, 'finished': True, 'already_there': True}

        # Can't afford
        if state['tu'] < cost:
            return {'moved': False, 'finished': True, 'out_of_tu': True}

        # Docked -- can't move
        if state['docked_at']:
            return {'moved': False, 'finished': True, 'blocked_docked': True}

        # Landed -- can't move
        if state['landed']:
            return {'moved': False, 'finished': True, 'blocked_landed': True}

        # Leave orbit on first step
        orbit_msg = ""
        if state['orbiting']:
            body = self.conn.execute(
                "SELECT name FROM celestial_bodies WHERE body_id = ?",
                (state['orbiting'],)
            ).fetchone()
            body_name = body['name'] if body else str(state['orbiting'])
            orbit_msg = f"Leaving orbit of {body_name}.\n    "
            state['orbiting'] = None

        # Take one step
        path = self._generate_path(
            state['col'], state['row'], target_col, target_row
        )
        if not path:
            return {'moved': False, 'finished': True, 'already_there': True}

        step_col, step_row = path[0]
        state['col'] = step_col
        state['row'] = step_row
        state['tu'] -= cost

        reached = (step_col == target_col and step_row == target_row)
        return {
            'moved': True,
            'finished': reached,
            'step': (step_col, step_row),
            'orbit_msg': orbit_msg,
        }

    def resolve_turn_interleaved(self, ship_orders_map):
        """
        Resolve all ships' orders interleaved by TU cost (priority queue).
        
        MOVE orders are broken into individual square steps (2 TU each)
        so ships can see each other's positions mid-move. After every
        action, the ship's position is committed to the database, making
        it visible to other ships' scans.
        
        ship_orders_map: dict of {ship_id: [order_dicts]}
        Returns: dict of {ship_id: turn_result_dict}
        """
        game = self.get_game()

        # Per-ship state tracking
        states = {}
        logs = {}
        pendings = {}
        contacts_map = {}
        rngs = {}
        queues = {}
        ship_rows = {}

        # Move accumulators: track multi-step moves for combined log entries
        # {ship_id: {target, tu_before, start_loc, waypoints, orbit_msg}}
        move_acc = {}

        for ship_id, orders in ship_orders_map.items():
            ship = self.get_ship(ship_id)
            if not ship:
                continue

            ship_rows[ship_id] = ship
            seed_str = (f"{self.game_id}-{game['current_year']}."
                        f"{game['current_week']}-{ship_id}")
            seed = int(hashlib.md5(seed_str.encode()).hexdigest()[:8], 16)

            states[ship_id] = {
                'ship_id': ship_id,
                'name': ship['name'],
                'system_id': ship['system_id'],
                'start_system_id': ship['system_id'],
                'col': ship['grid_col'],
                'row': ship['grid_row'],
                'tu': ship['tu_per_turn'],
                'move_cost': TU_COSTS['MOVE'],  # per-ship; will vary by engines
                'docked_at': ship['docked_at_base_id'],
                'orbiting': ship['orbiting_body_id'],
                'landed': ship['landed_body_id'] if 'landed_body_id' in ship.keys() else None,
                'landed_x': ship['landed_x'] if 'landed_x' in ship.keys() else 1,
                'landed_y': ship['landed_y'] if 'landed_y' in ship.keys() else 1,
                'start_col': ship['grid_col'],
                'start_row': ship['grid_row'],
                'start_tu': ship['tu_per_turn'],
            }
            logs[ship_id] = []
            pendings[ship_id] = []
            contacts_map[ship_id] = []
            rngs[ship_id] = random.Random(seed)
            queues[ship_id] = list(orders)

        # Build initial priority queue: (completion_time, tiebreaker, ship_id)
        heap = []
        counter = 0
        for ship_id in states:
            if queues[ship_id]:
                est = self._estimate_next_cost(states[ship_id], queues[ship_id][0])
                heapq.heappush(heap, (est, counter, ship_id))
                counter += 1

        # === Main interleaved loop ===
        while heap:
            _, _, ship_id = heapq.heappop(heap)

            if not queues[ship_id]:
                continue

            state = states[ship_id]
            order = queues[ship_id][0]  # peek, don't pop yet

            # Swap per-ship tracking into instance vars
            self.log = logs[ship_id]
            self.pending = pendings[ship_id]
            self.contacts = contacts_map[ship_id]

            if order['command'] == 'MOVE':
                params = order['params']
                target_col = params['col']
                target_row = params['row']
                target_str = f"{target_col}{target_row:02d}"

                # Start a new move accumulator if needed
                if ship_id not in move_acc:
                    move_acc[ship_id] = {
                        'target_col': target_col,
                        'target_row': target_row,
                        'tu_before': state['tu'],
                        'waypoints': [f"{state['col']}{state['row']:02d}"],
                        'orbit_msg': '',
                        'encounters': [],
                    }

                acc = move_acc[ship_id]
                step_result = self._cmd_move_step(state, target_col, target_row)

                if step_result.get('orbit_msg'):
                    acc['orbit_msg'] = step_result['orbit_msg']

                if step_result['moved']:
                    sc, sr = step_result['step']
                    acc['waypoints'].append(f"{sc}{sr:02d}")
                    self._commit_ship_position(state)

                    # Automatic detection: check for ships at new position
                    detected = self._detect_ships_at_location(state)
                    for d in detected:
                        loc = f"{d['col']}{d['row']:02d}"
                        enc_entry = (loc, d['name'], d['id'],
                                     d.get('hull_count', '?'),
                                     d.get('hull_type', ''))
                        if enc_entry not in acc['encounters']:
                            acc['encounters'].append(enc_entry)

                if step_result['finished']:
                    # Pop the order, flush accumulator to log
                    queues[ship_id].pop(0)
                    log_entry = self._build_move_log_entry(
                        acc, state, step_result, pendings[ship_id]
                    )
                    logs[ship_id].append(log_entry)
                    del move_acc[ship_id]
                # else: leave order in queue for next step

            else:
                # Non-MOVE: execute atomically, pop from queue
                queues[ship_id].pop(0)
                result = self._execute_order(state, order, rngs[ship_id])
                self._commit_ship_position(state)

                # Automatic detection after non-move actions too
                detected = self._detect_ships_at_location(state)
                if detected:
                    det_lines = []
                    for d in detected:
                        det_lines.append(
                            f"        {d['name']} ({d['id']}) "
                            f"- {{{d.get('hull_count', '?')} {d.get('hull_type', '')}}}"
                        )
                    result['message'] += (
                        "\n    Detected:\n" + "\n".join(det_lines)
                    )

                logs[ship_id].append(result)

            # Re-insert into heap if more work remains
            if queues[ship_id]:
                elapsed = state['start_tu'] - state['tu']
                next_est = self._estimate_next_cost(state, queues[ship_id][0])
                heapq.heappush(heap, (elapsed + next_est, counter, ship_id))
                counter += 1
            elif ship_id in move_acc:
                # MOVE still in progress (shouldn't happen since finished
                # pops, but guard against edge cases)
                elapsed = state['start_tu'] - state['tu']
                heapq.heappush(heap, (elapsed + state['move_cost'], counter, ship_id))
                counter += 1

        # Flush any interrupted moves (ran out of TU mid-move)
        for ship_id, acc in list(move_acc.items()):
            state = states[ship_id]
            self.pending = pendings[ship_id]
            log_entry = self._build_move_log_entry(
                acc, state, {'moved': False, 'finished': True, 'out_of_tu': True},
                pendings[ship_id]
            )
            logs[ship_id].append(log_entry)

        # === Commit final states and build results ===
        results = {}
        for ship_id in states:
            state = states[ship_id]
            ship = ship_rows[ship_id]

            self.log = logs[ship_id]
            self.pending = pendings[ship_id]
            self.contacts = contacts_map[ship_id]

            self._commit_ship_state(state)
            self._update_contacts(ship['owner_prefect_id'], state['system_id'])

            seed_str = (f"{self.game_id}-{game['current_year']}."
                        f"{game['current_week']}-{ship_id}")

            results[ship_id] = {
                'ship_id': ship_id,
                'ship_name': ship['name'],
                'system_id': state['start_system_id'],
                'final_system_id': state['system_id'],
                'start_col': state['start_col'],
                'start_row': state['start_row'],
                'start_tu': state['start_tu'],
                'start_orbiting': ship['orbiting_body_id'],
                'start_docked': ship['docked_at_base_id'],
                'start_landed': ship['landed_body_id'] if 'landed_body_id' in ship.keys() else None,
                'start_landed_x': ship['landed_x'] if 'landed_x' in ship.keys() else 1,
                'start_landed_y': ship['landed_y'] if 'landed_y' in ship.keys() else 1,
                'final_col': state['col'],
                'final_row': state['row'],
                'final_tu': state['tu'],
                'docked_at': state['docked_at'],
                'orbiting': state['orbiting'],
                'landed': state['landed'],
                'landed_x': state['landed_x'],
                'landed_y': state['landed_y'],
                'log': logs[ship_id],
                'pending': pendings[ship_id],
                'contacts': contacts_map[ship_id],
                'rng_seed': seed_str,
                'turn_year': game['current_year'],
                'turn_week': game['current_week'],
            }

        return results

    def _estimate_next_cost(self, state, order):
        """Estimate TU cost for priority ordering. MOVE = one step at ship's speed."""
        cmd = order['command']
        params = order['params']

        if cmd == 'MOVE':
            return state['move_cost']
        elif cmd == 'WAIT':
            if isinstance(params, (int, float)):
                return min(int(params), state['tu'])
            return state['tu']
        elif cmd in TU_COSTS:
            return TU_COSTS[cmd]
        return 0

    def _build_move_log_entry(self, acc, state, final_step, pendings_list):
        """Build a combined MOVE log entry from accumulated steps."""
        target_col = acc['target_col']
        target_row = acc['target_row']
        target_str = f"{target_col}{target_row:02d}"
        waypoints = acc['waypoints']
        tu_before = acc['tu_before']
        orbit_msg = acc['orbit_msg']
        steps_taken = len(waypoints) - 1  # first entry is start position
        total_cost = steps_taken * state['move_cost']

        reached = (state['col'] == target_col and state['row'] == target_row)
        final_loc = f"{state['col']}{state['row']:02d}"

        if final_step.get('already_there'):
            msg = f"Already at {target_str}."
        elif final_step.get('blocked_docked'):
            msg = "Cannot move while docked. UNDOCK first. Order queued as pending."
        elif final_step.get('blocked_landed'):
            msg = "Cannot move while landed. TAKEOFF first. Order queued as pending."
        elif steps_taken == 0 and final_step.get('out_of_tu'):
            msg = (f"Insufficient TU for move ({state['tu']} < "
                   f"{state['move_cost']}). Order queued as pending.")
            pendings_list.append({
                'command': 'MOVE', 'params': target_str,
                'reason': 'Insufficient TU'
            })
        elif reached:
            if steps_taken <= 4:
                path_str = " -> ".join(waypoints)
            else:
                path_str = f"{waypoints[0]} -> {waypoints[1]} -> ... -> {waypoints[-1]}"
            msg = f"{orbit_msg}Moved {steps_taken} squares to {final_loc}. ({path_str})"
        else:
            # Partial move
            if steps_taken <= 4:
                path_str = " -> ".join(waypoints)
            else:
                path_str = f"{waypoints[0]} -> {waypoints[1]} -> ... -> {waypoints[-1]}"
            remaining = grid_distance(state['col'], state['row'], target_col, target_row)
            msg = (f"{orbit_msg}Moved {steps_taken} squares toward {target_str}, "
                   f"stopped at {final_loc} ({remaining} squares remaining). ({path_str})")
            pendings_list.append({
                'command': 'MOVE', 'params': target_str,
                'reason': f"Ran out of TU at {final_loc}"
            })

        # Append encounter detections during movement
        encounters = acc.get('encounters', [])
        if encounters and steps_taken > 0:
            enc_lines = ["    Detected en route:"]
            for loc, name, eid, hc, ht in encounters:
                enc_lines.append(f"        {name} ({eid}) at {loc} - {{{hc} {ht}}}")
            msg += "\n" + "\n".join(enc_lines)

        return {
            'command': 'MOVE', 'params': target_str,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': total_cost,
            'success': reached,
            'message': msg,
            'steps': steps_taken,
            'waypoints': waypoints,
        }

    def get_game(self):
        """Get current game state."""
        return self.conn.execute(
            "SELECT * FROM games WHERE game_id = ?", (self.game_id,)
        ).fetchone()

    def _get_market_cycle_week(self, game=None):
        """Get the cycle start week for current market prices."""
        from engine.game_setup import get_market_cycle_start
        if game is None:
            game = self.get_game()
        return game['current_year'], get_market_cycle_start(game['current_week'])

    def get_ship(self, ship_id):
        """Get ship state."""
        return self.conn.execute(
            "SELECT * FROM ships WHERE ship_id = ? AND game_id = ?",
            (ship_id, self.game_id)
        ).fetchone()

    def _get_faction(self, faction_id):
        """Get faction details."""
        return get_faction(self.conn, faction_id)

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

        # Other ships (exclude suspended players' ships)
        ships = self.conn.execute(
            """SELECT s.*, pp.faction_id FROM ships s
               JOIN prefects pp ON s.owner_prefect_id = pp.prefect_id
               JOIN players p ON pp.player_id = p.player_id
               WHERE s.system_id = ? AND s.game_id = ? AND p.status = 'active'""",
            (system_id, self.game_id)
        ).fetchall()
        for s in ships:
            # Build faction-prefixed display name
            faction = self._get_faction(s['faction_id'])
            display_name = f"{faction['abbreviation']} {s['name']}"
            objects.append({
                'type': 'ship', 'id': s['ship_id'],
                'name': display_name,
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
            'move_cost': TU_COSTS['MOVE'],  # per-ship; will vary by engines
            'docked_at': ship['docked_at_base_id'],
            'orbiting': ship['orbiting_body_id'],
            'landed': ship['landed_body_id'] if 'landed_body_id' in ship.keys() else None,
            'landed_x': ship['landed_x'] if 'landed_x' in ship.keys() else 1,
            'landed_y': ship['landed_y'] if 'landed_y' in ship.keys() else 1,
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
        prefect_id = ship['owner_prefect_id']
        self._update_contacts(prefect_id, state['system_id'])

        return {
            'ship_id': ship_id,
            'ship_name': ship['name'],
            'system_id': system_id,
            'final_system_id': state['system_id'],
            'start_col': state['start_col'],
            'start_row': state['start_row'],
            'start_tu': state['start_tu'],
            'start_orbiting': ship['orbiting_body_id'],
            'start_docked': ship['docked_at_base_id'],
            'start_landed': ship['landed_body_id'] if 'landed_body_id' in ship.keys() else None,
            'start_landed_x': ship['landed_x'] if 'landed_x' in ship.keys() else 1,
            'start_landed_y': ship['landed_y'] if 'landed_y' in ship.keys() else 1,
            'final_col': state['col'],
            'final_row': state['row'],
            'final_tu': state['tu'],
            'docked_at': state['docked_at'],
            'orbiting': state['orbiting'],
            'landed': state['landed'],
            'landed_x': state['landed_x'],
            'landed_y': state['landed_y'],
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
        elif cmd == 'LAND':
            return self._cmd_land(state, params)
        elif cmd == 'TAKEOFF':
            return self._cmd_takeoff(state)
        elif cmd == 'SURFACESCAN':
            return self._cmd_surfacescan(state)
        elif cmd == 'BUY':
            return self._cmd_buy(state, params)
        elif cmd == 'SELL':
            return self._cmd_sell(state, params)
        elif cmd == 'GETMARKET':
            return self._cmd_getmarket(state, params)
        elif cmd == 'JUMP':
            return self._cmd_jump(state, params)
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
        """MOVE {coord} - move to grid coordinate, one square at a time."""
        tu_before = state['tu']
        target_col = params['col']
        target_row = params['row']
        cost_per_step = state['move_cost']

        # Already there?
        if state['col'] == target_col and state['row'] == target_row:
            return {
                'command': 'MOVE', 'params': f"{target_col}{target_row:02d}",
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': True,
                'message': f"Already at {target_col}{target_row:02d}."
            }

        if state['tu'] < cost_per_step:
            self.pending.append({
                'command': 'MOVE', 'params': f"{target_col}{target_row:02d}",
                'reason': 'Insufficient TU'
            })
            return {
                'command': 'MOVE', 'params': f"{target_col}{target_row:02d}",
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False,
                'message': f"Insufficient TU for move ({state['tu']} < {cost_per_step}). Order queued as pending."
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

        # Generate step-by-step path (Chebyshev: diagonal then straight)
        path = self._generate_path(state['col'], state['row'], target_col, target_row)

        # Walk the path one square at a time
        start_loc = f"{state['col']}{state['row']:02d}"
        steps_taken = 0
        waypoints = [start_loc]
        encounters = []

        for step_col, step_row in path:
            if state['tu'] < cost_per_step:
                # Out of TU -- queue remaining distance as pending
                self.pending.append({
                    'command': 'MOVE', 'params': f"{target_col}{target_row:02d}",
                    'reason': f"Ran out of TU at {state['col']}{state['row']:02d}"
                })
                break

            # Move to next square
            state['col'] = step_col
            state['row'] = step_row
            state['tu'] -= cost_per_step
            steps_taken += 1
            waypoints.append(f"{step_col}{step_row:02d}")

            # === ENCOUNTER CHECK (hook for future combat) ===
            # Check for other ships at this position
            # hostile_ships = self._check_encounters(state, rng)
            # if hostile_ships:
            #     encounters.append(...)
            #     break  # Combat halts movement

        total_cost = steps_taken * cost_per_step
        final_loc = f"{state['col']}{state['row']:02d}"
        reached_destination = (state['col'] == target_col and state['row'] == target_row)

        # Build movement message
        if steps_taken <= 4:
            path_str = " -> ".join(waypoints)
        else:
            path_str = f"{waypoints[0]} -> {waypoints[1]} -> ... -> {waypoints[-1]}"

        if reached_destination:
            msg = f"{orbit_msg}Moved {steps_taken} squares to {final_loc}. ({path_str})"
        else:
            remaining = grid_distance(state['col'], state['row'], target_col, target_row)
            msg = (f"{orbit_msg}Moved {steps_taken} squares toward {target_col}{target_row:02d}, "
                   f"stopped at {final_loc} ({remaining} squares remaining). ({path_str})")

        return {
            'command': 'MOVE', 'params': f"{target_col}{target_row:02d}",
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': total_cost,
            'success': reached_destination,
            'message': msg,
            'steps': steps_taken,
            'waypoints': waypoints,
        }

    def _generate_path(self, from_col, from_row, to_col, to_row):
        """
        Generate a step-by-step path from one grid position to another.
        Uses Chebyshev movement: diagonal steps when both axes need closing,
        then straight steps for the remaining axis.
        Returns list of (col_letter, row_int) for each step (excluding start).
        """
        path = []
        cur_c = col_to_index(from_col)
        cur_r = from_row
        dst_c = col_to_index(to_col)
        dst_r = to_row

        while cur_c != dst_c or cur_r != dst_r:
            # Step toward target on each axis
            if cur_c < dst_c:
                cur_c += 1
            elif cur_c > dst_c:
                cur_c -= 1

            if cur_r < dst_r:
                cur_r += 1
            elif cur_r > dst_r:
                cur_r -= 1

            path.append((index_to_col(cur_c), cur_r))

        return path

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

        # Add all detected objects as contacts (except own ship)
        for obj in objects:
            if obj['type'] != 'ship' or obj['id'] != state['ship_id']:
                self.contacts.append(obj)

        # Only celestial bodies go on the grid (no ships, no bases)
        map_objects = [o for o in objects
                       if o['type'] not in ('ship', 'base')]

        system_data = {
            'star_col': system['star_grid_col'],
            'star_row': system['star_grid_row']
        }

        ascii_map = render_system_map(system_data, map_objects)

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

        # If base is in orbit, ship must also be orbiting the same body
        if base['orbiting_body_id']:
            if state['orbiting'] != base['orbiting_body_id']:
                body = self.conn.execute(
                    "SELECT name FROM celestial_bodies WHERE body_id = ?",
                    (base['orbiting_body_id'],)
                ).fetchone()
                body_name = body['name'] if body else str(base['orbiting_body_id'])
                return {
                    'command': 'DOCK', 'params': base_id,
                    'tu_before': tu_before, 'tu_after': state['tu'],
                    'tu_cost': 0,
                    'success': False,
                    'message': (
                        f"Unable to dock: {base['name']} ({base_id}) is in orbit of "
                        f"{body_name} ({base['orbiting_body_id']}). "
                        f"You must ORBIT {base['orbiting_body_id']} first."
                    )
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

    def _cmd_land(self, state, params):
        """LAND <body_id> <x> <y> - land on a planet or moon surface at coordinates."""
        from engine.maps.surface_gen import get_or_generate_surface, TERRAIN_SYMBOLS
        tu_before = state['tu']
        cost = TU_COSTS['LAND']

        body_id = params['body_id']
        land_x = params['x']
        land_y = params['y']
        params_str = f"{body_id} {land_x} {land_y}"

        # Must be orbiting the target body
        if state['orbiting'] != body_id:
            if state['landed']:
                msg = "Cannot land: ship is already landed on a surface. TAKEOFF first."
            elif state['docked_at']:
                msg = f"Cannot land: ship is docked at a base. UNDOCK and ORBIT the body first."
            else:
                msg = f"Cannot land: ship must be orbiting body {body_id}. Use ORBIT first."
            return {
                'command': 'LAND', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': msg
            }

        # Look up the body
        body = self.conn.execute(
            "SELECT * FROM celestial_bodies WHERE body_id = ?", (body_id,)
        ).fetchone()
        if not body:
            return {
                'command': 'LAND', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot land: body {body_id} not found."
            }

        # Can't land on gas giants
        if body['body_type'] == 'gas_giant':
            return {
                'command': 'LAND', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot land: {body['name']} is a gas giant."
            }

        # Validate coordinates
        surface_size = body['surface_size'] if 'surface_size' in body.keys() else 31
        if not (1 <= land_x <= surface_size) or not (1 <= land_y <= surface_size):
            return {
                'command': 'LAND', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot land: coordinates ({land_x},{land_y}) out of range (1-{surface_size})."
            }

        # Check TU
        if state['tu'] < cost:
            self.pending.append({
                'command': 'LAND', 'params': params_str,
                'reason': 'Insufficient TU'
            })
            return {
                'command': 'LAND', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Insufficient TU to land ({state['tu']} < {cost}). Order queued as pending."
            }

        # Future: gravity check would go here
        # ship_gravity_rating = state.get('gravity_rating', 1.5)
        # if body['gravity'] > ship_gravity_rating:
        #     return error...

        # Generate/fetch surface to look up terrain at landing site
        tiles = get_or_generate_surface(self.conn, body)
        terrain_map = {(t[0], t[1]): t[2] for t in tiles}
        terrain = terrain_map.get((land_x, land_y), 'Unknown')

        # Execute landing
        state['orbiting'] = None
        state['landed'] = body_id
        state['landed_x'] = land_x
        state['landed_y'] = land_y
        state['tu'] -= cost

        gravity_str = f"{body['gravity']}g" if body['gravity'] else ""
        return {
            'command': 'LAND', 'params': params_str,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': cost, 'success': True,
            'message': (f"Landed on {body['body_type'].title()} {body['name']} ({body_id}) "
                        f"[{gravity_str}] at ({land_x},{land_y}) - {terrain}.")
        }

    def _cmd_takeoff(self, state):
        """TAKEOFF - lift off from planet surface, return to orbit."""
        tu_before = state['tu']
        cost = TU_COSTS['TAKEOFF']

        if not state['landed']:
            return {
                'command': 'TAKEOFF', 'params': None,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': "Cannot take off: ship is not landed on any surface."
            }

        if state['tu'] < cost:
            self.pending.append({
                'command': 'TAKEOFF', 'params': None,
                'reason': 'Insufficient TU'
            })
            return {
                'command': 'TAKEOFF', 'params': None,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Insufficient TU to take off ({state['tu']} < {cost}). Order queued as pending."
            }

        body_id = state['landed']
        body = self.conn.execute(
            "SELECT * FROM celestial_bodies WHERE body_id = ?", (body_id,)
        ).fetchone()
        body_name = body['name'] if body else str(body_id)

        # Return to orbit around the body we were landed on
        state['landed'] = None
        state['orbiting'] = body_id
        state['tu'] -= cost

        return {
            'command': 'TAKEOFF', 'params': None,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': cost, 'success': True,
            'message': f"Launched from {body_name} ({body_id}). Ship is now in orbit."
        }

    def _cmd_surfacescan(self, state):
        """SURFACESCAN - produce a terrain map of the planet the ship is orbiting or landed on."""
        from engine.maps.surface_gen import get_or_generate_surface, render_surface_map
        tu_before = state['tu']
        cost = TU_COSTS['SURFACESCAN']

        # Determine which body to scan - landed takes priority, then orbiting
        body_id = state['landed'] or state['orbiting']
        if not body_id:
            return {
                'command': 'SURFACESCAN', 'params': None,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': "Cannot scan surface: ship must be orbiting or landed on a planet or moon."
            }

        if state['tu'] < cost:
            self.pending.append({
                'command': 'SURFACESCAN', 'params': None,
                'reason': 'Insufficient TU'
            })
            return {
                'command': 'SURFACESCAN', 'params': None,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Insufficient TU for surface scan ({state['tu']} < {cost}). Order queued as pending."
            }

        body = self.conn.execute(
            "SELECT * FROM celestial_bodies WHERE body_id = ?", (body_id,)
        ).fetchone()
        if not body:
            return {
                'command': 'SURFACESCAN', 'params': None,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot scan surface: body {body_id} not found."
            }

        state['tu'] -= cost

        tiles = get_or_generate_surface(self.conn, body)
        planetary_data = {
            'gravity': body['gravity'],
            'temperature': body['temperature'],
            'atmosphere': body['atmosphere'],
            'tectonic_activity': body['tectonic_activity'],
            'hydrosphere': body['hydrosphere'],
            'life': body['life'],
        }

        # Show ship position on map if landed
        ship_pos = None
        if state['landed']:
            ship_pos = (state.get('landed_x', 1), state.get('landed_y', 1))

        map_lines = render_surface_map(
            tiles, body['name'], body_id, planetary_data, ship_pos=ship_pos
        )
        message = '\n'.join(f"    {line}" for line in map_lines)

        scan_type = "landed on" if state['landed'] else "orbiting"
        return {
            'command': 'SURFACESCAN', 'params': None,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': cost, 'success': True,
            'message': message,
        }

    def _cmd_buy(self, state, params):
        """BUY <base_id> <item_id> <quantity> - buy items from base market."""
        tu_before = state['tu']
        base_id = params['base_id']
        item_id = params['item_id']
        quantity = params['quantity']
        params_str = f"{base_id} {item_id} {quantity}"

        # Must be docked at this base
        if state['docked_at'] != base_id:
            return {
                'command': 'BUY', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot buy: ship is not docked at base {base_id}."
            }

        # Look up item
        item = self.conn.execute(
            "SELECT * FROM trade_goods WHERE item_id = ?",
            (item_id,)
        ).fetchone()
        if not item:
            return {
                'command': 'BUY', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot buy: item {item_id} not found."
            }

        # Look up current price (keyed to market cycle start)
        cycle_year, cycle_week = self._get_market_cycle_week()
        price_row = self.conn.execute("""
            SELECT * FROM market_prices
            WHERE game_id = ? AND base_id = ? AND item_id = ?
            AND turn_year = ? AND turn_week = ?
        """, (self.game_id, base_id, item_id,
              cycle_year, cycle_week)).fetchone()
        if not price_row:
            return {
                'command': 'BUY', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot buy: no market data at this base for {item['name']}."
            }

        buy_price = price_row['buy_price']
        available_stock = price_row['stock']

        # Check stock
        if available_stock <= 0:
            return {
                'command': 'BUY', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot buy: {item['name']} is out of stock at this base."
            }

        # Cap quantity to available stock
        actual_qty = min(quantity, available_stock)

        # Cap to available credits
        ship = self.get_ship(state['ship_id'])
        prefect = self.conn.execute(
            "SELECT * FROM prefects WHERE prefect_id = ?",
            (ship['owner_prefect_id'],)
        ).fetchone()
        if buy_price > 0:
            max_by_credits = int(prefect['credits'] // buy_price)
            actual_qty = min(actual_qty, max_by_credits)

        # Cap to available cargo space
        available_mu = ship['cargo_capacity'] - ship['cargo_used']
        if item['mass_per_unit'] > 0:
            max_by_cargo = int(available_mu // item['mass_per_unit'])
            actual_qty = min(actual_qty, max_by_cargo)

        if actual_qty <= 0:
            return {
                'command': 'BUY', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': (f"Cannot buy any {item['name']}: "
                            f"stock={available_stock}, credits={prefect['credits']:,.0f} cr, "
                            f"cargo space={available_mu} MU free.")
            }

        # Build capped message
        cap_reasons = []
        if actual_qty < quantity:
            if available_stock < quantity:
                cap_reasons.append(f"stock={available_stock}")
            if buy_price > 0 and int(prefect['credits'] // buy_price) < quantity:
                cap_reasons.append(f"credits={prefect['credits']:,.0f} cr")
            if item['mass_per_unit'] > 0 and int(available_mu // item['mass_per_unit']) < quantity:
                cap_reasons.append(f"cargo={available_mu} MU free")
            capped_msg = f" (capped from {quantity} to {actual_qty}: {', '.join(cap_reasons)})"
        else:
            capped_msg = ""

        total_cost_cr = buy_price * actual_qty
        total_mass = item['mass_per_unit'] * actual_qty

        # Execute purchase
        self.conn.execute(
            "UPDATE prefects SET credits = credits - ? WHERE prefect_id = ?",
            (total_cost_cr, prefect['prefect_id'])
        )
        self.conn.execute(
            "UPDATE ships SET cargo_used = cargo_used + ? WHERE ship_id = ?",
            (total_mass, state['ship_id'])
        )

        # Decrement base stock
        self.conn.execute("""
            UPDATE market_prices SET stock = stock - ?
            WHERE price_id = ?
        """, (actual_qty, price_row['price_id']))

        # Add to cargo (merge with existing if same item)
        existing = self.conn.execute(
            "SELECT * FROM cargo_items WHERE ship_id = ? AND item_type_id = ?",
            (state['ship_id'], item_id)
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE cargo_items SET quantity = quantity + ? WHERE cargo_id = ?",
                (actual_qty, existing['cargo_id'])
            )
        else:
            self.conn.execute("""
                INSERT INTO cargo_items (ship_id, item_type_id, item_name, quantity, mass_per_unit)
                VALUES (?, ?, ?, ?, ?)
            """, (state['ship_id'], item_id, item['name'], actual_qty, item['mass_per_unit']))

        self.conn.commit()

        base = self.conn.execute(
            "SELECT name FROM starbases WHERE base_id = ?", (base_id,)
        ).fetchone()
        base_name = base['name'] if base else str(base_id)

        return {
            'command': 'BUY', 'params': params_str,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': 0, 'success': True,
            'credits_spent': total_cost_cr,
            'item_name': item['name'], 'item_id': item_id,
            'quantity': actual_qty,
            'message': (f"Bought {actual_qty} {item['name']} ({item_id}) "
                        f"at {buy_price} cr each = {total_cost_cr:,} cr total "
                        f"from {base_name} ({base_id}). [{total_mass} MU]{capped_msg}")
        }

    def _cmd_sell(self, state, params):
        """SELL <base_id> <item_id> <quantity> - sell items to base market."""
        tu_before = state['tu']
        base_id = params['base_id']
        item_id = params['item_id']
        quantity = params['quantity']
        params_str = f"{base_id} {item_id} {quantity}"

        # Must be docked at this base
        if state['docked_at'] != base_id:
            return {
                'command': 'SELL', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot sell: ship is not docked at base {base_id}."
            }

        # Look up item
        item = self.conn.execute(
            "SELECT * FROM trade_goods WHERE item_id = ?",
            (item_id,)
        ).fetchone()
        if not item:
            return {
                'command': 'SELL', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot sell: item {item_id} not found."
            }

        # Check cargo
        cargo = self.conn.execute(
            "SELECT * FROM cargo_items WHERE ship_id = ? AND item_type_id = ?",
            (state['ship_id'], item_id)
        ).fetchone()
        if not cargo or cargo['quantity'] <= 0:
            return {
                'command': 'SELL', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot sell: you have no {item['name']} in cargo."
            }

        # Look up current price (keyed to market cycle start)
        cycle_year, cycle_week = self._get_market_cycle_week()
        price_row = self.conn.execute("""
            SELECT * FROM market_prices
            WHERE game_id = ? AND base_id = ? AND item_id = ?
            AND turn_year = ? AND turn_week = ?
        """, (self.game_id, base_id, item_id,
              cycle_year, cycle_week)).fetchone()
        if not price_row:
            return {
                'command': 'SELL', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot sell: no market data at this base for {item['name']}."
            }

        sell_price = price_row['sell_price']
        available_demand = price_row['demand']

        # Check demand
        if available_demand <= 0:
            return {
                'command': 'SELL', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot sell: no demand for {item['name']} at this base."
            }

        # Cap to what we actually have, then to demand
        actual_qty = min(quantity, cargo['quantity'], available_demand)

        if actual_qty <= 0:
            return {
                'command': 'SELL', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': (f"Cannot sell any {item['name']}: "
                            f"have={cargo['quantity']}, demand={available_demand}.")
            }

        # Build capped message
        cap_reasons = []
        if actual_qty < quantity:
            if cargo['quantity'] < quantity:
                cap_reasons.append(f"have {cargo['quantity']} in cargo")
            if available_demand < quantity:
                cap_reasons.append(f"demand={available_demand}")
            capped_msg = f" (capped from {quantity} to {actual_qty}: {', '.join(cap_reasons)})"
        else:
            capped_msg = ""

        total_income = sell_price * actual_qty
        total_mass = item['mass_per_unit'] * actual_qty

        # Execute sale
        ship = self.get_ship(state['ship_id'])
        self.conn.execute(
            "UPDATE prefects SET credits = credits + ? WHERE prefect_id = ?",
            (total_income, ship['owner_prefect_id'])
        )
        self.conn.execute(
            "UPDATE ships SET cargo_used = cargo_used - ? WHERE ship_id = ?",
            (total_mass, state['ship_id'])
        )

        # Decrement base demand
        self.conn.execute("""
            UPDATE market_prices SET demand = demand - ?
            WHERE price_id = ?
        """, (actual_qty, price_row['price_id']))

        # Update cargo
        new_qty = cargo['quantity'] - actual_qty
        if new_qty <= 0:
            self.conn.execute(
                "DELETE FROM cargo_items WHERE cargo_id = ?", (cargo['cargo_id'],)
            )
        else:
            self.conn.execute(
                "UPDATE cargo_items SET quantity = ? WHERE cargo_id = ?",
                (new_qty, cargo['cargo_id'])
            )

        self.conn.commit()

        base = self.conn.execute(
            "SELECT name FROM starbases WHERE base_id = ?", (base_id,)
        ).fetchone()
        base_name = base['name'] if base else str(base_id)

        return {
            'command': 'SELL', 'params': params_str,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': 0, 'success': True,
            'credits_earned': total_income,
            'item_name': item['name'], 'item_id': item_id,
            'quantity': actual_qty,
            'message': (f"Sold {actual_qty} {item['name']} ({item_id}) "
                        f"at {sell_price} cr each = {total_income:,} cr total "
                        f"to {base_name} ({base_id}). [{total_mass} MU freed]{capped_msg}")
        }

    def _cmd_getmarket(self, state, base_id):
        """GETMARKET <base_id> - view market prices at a base."""
        tu_before = state['tu']

        # Must be docked at or at same grid location as base
        base = self.conn.execute(
            "SELECT * FROM starbases WHERE base_id = ? AND game_id = ?",
            (base_id, self.game_id)
        ).fetchone()
        if not base:
            return {
                'command': 'GETMARKET', 'params': base_id,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot view market: base {base_id} not found."
            }

        at_base = (state['docked_at'] == base_id)
        at_location = (state['col'] == base['grid_col'] and
                       state['row'] == base['grid_row'])
        if not at_base and not at_location:
            return {
                'command': 'GETMARKET', 'params': base_id,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': (f"Cannot view market: must be docked at or "
                            f"in orbit near {base['name']} ({base_id}).")
            }

        # Get current prices (keyed to market cycle start)
        from engine.game_setup import get_market_weeks_remaining
        game = self.get_game()
        cycle_year, cycle_week = self._get_market_cycle_week(game)
        prices = self.conn.execute("""
            SELECT mp.*, tg.name as item_name, tg.mass_per_unit,
                   btc.trade_role
            FROM market_prices mp
            JOIN trade_goods tg ON mp.item_id = tg.item_id
            JOIN base_trade_config btc ON mp.base_id = btc.base_id
                AND mp.item_id = btc.item_id AND btc.game_id = mp.game_id
            WHERE mp.game_id = ? AND mp.base_id = ?
            AND mp.turn_year = ? AND mp.turn_week = ?
            ORDER BY mp.item_id
        """, (self.game_id, base_id,
              cycle_year, cycle_week)).fetchall()

        if not prices:
            return {
                'command': 'GETMARKET', 'params': base_id,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"No market data available at {base['name']} ({base_id})."
            }

        # Build market report with cycle countdown
        weeks_left = get_market_weeks_remaining(game['current_week'])
        if weeks_left <= 1:
            refresh_msg = "Market refreshes next week."
        else:
            refresh_msg = f"{weeks_left} weeks to market refresh."

        role_labels = {'produces': 'Produces', 'average': 'Standard', 'demands': 'In Demand'}
        lines = [f"Market at {base['name']} ({base_id}):"]
        # Format: Item(28) ID(4) Buy(7) Sell(7) Stock(6) Demand(7) Status
        lines.append(f"    {'Item':<28} {'ID':>3}  {'Buy':>7}  {'Sell':>7}  {'Stock':>5}  {'Demand':>6}  Status")
        lines.append(f"    {'-'*28} {'---':>3}  {'-------':>7}  {'-------':>7}  {'-----':>5}  {'------':>6}  ---------")
        for p in prices:
            role_str = role_labels.get(p['trade_role'], '')
            buy_str = f"{p['buy_price']} cr"
            sell_str = f"{p['sell_price']} cr"
            lines.append(
                f"    {p['item_name']:<28} {p['item_id']:>3}  "
                f"{buy_str:>7}  {sell_str:>7}  "
                f"{p['stock']:>5}  {p['demand']:>6}  "
                f"{role_str}"
            )
        lines.append(f"    {refresh_msg}")

        return {
            'command': 'GETMARKET', 'params': base_id,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': 0, 'success': True,
            'message': '\n'.join(lines),
        }

    def _cmd_jump(self, state, target_system_id):
        """JUMP <system_id> - hyperspace jump to a linked star system."""
        tu_before = state['tu']
        cost = TU_COSTS['JUMP']
        params_str = str(target_system_id)

        # Must not be docked
        if state['docked_at']:
            return {
                'command': 'JUMP', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': "Cannot jump: ship is docked. UNDOCK first."
            }

        # Must not be landed
        if state['landed']:
            return {
                'command': 'JUMP', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': "Cannot jump: ship is landed. TAKEOFF first."
            }

        # Must not be orbiting
        if state['orbiting']:
            return {
                'command': 'JUMP', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': "Cannot jump: ship is in orbit. Leave orbit first (MOVE to a square)."
            }

        # Can't jump to current system
        if target_system_id == state['system_id']:
            return {
                'command': 'JUMP', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': "Cannot jump: you are already in that system."
            }

        # Check distance from primary star (M13)
        # The star is always at M13 in a 25x25 grid
        star_col, star_row = 'M', 13
        dist_from_star = grid_distance(state['col'], state['row'], star_col, star_row)
        min_jump_distance = 10
        if dist_from_star < min_jump_distance:
            current_loc = f"{state['col']}{state['row']:02d}"
            return {
                'command': 'JUMP', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': (f"Cannot jump: too close to the star. "
                            f"Ship at {current_loc} is {dist_from_star} squares from the star "
                            f"(minimum {min_jump_distance} required).")
            }

        # Check target system exists
        target = self.conn.execute(
            "SELECT * FROM star_systems WHERE system_id = ?",
            (target_system_id,)
        ).fetchone()
        if not target:
            return {
                'command': 'JUMP', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot jump: system {target_system_id} not found."
            }

        # Check system link exists
        a, b = min(state['system_id'], target_system_id), max(state['system_id'], target_system_id)
        link = self.conn.execute(
            "SELECT * FROM system_links WHERE system_a = ? AND system_b = ?",
            (a, b)
        ).fetchone()
        if not link:
            return {
                'command': 'JUMP', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot jump: no known hyperspace link to {target['name']} ({target_system_id})."
            }

        # Check TU
        if state['tu'] < cost:
            self.pending.append({
                'command': 'JUMP', 'params': params_str,
                'reason': 'Insufficient TU'
            })
            return {
                'command': 'JUMP', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': (f"Insufficient TU for jump ({state['tu']} < {cost}). "
                            f"Order queued as pending.")
            }

        # Execute jump
        origin_system = self.conn.execute(
            "SELECT name FROM star_systems WHERE system_id = ?",
            (state['system_id'],)
        ).fetchone()
        origin_name = origin_system['name'] if origin_system else str(state['system_id'])

        state['system_id'] = target_system_id
        state['tu'] -= cost

        # Commit position immediately so subsequent commands see the new system
        self._commit_ship_position(state)

        arrival_loc = f"{state['col']}{state['row']:02d}"
        return {
            'command': 'JUMP', 'params': params_str,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': cost, 'success': True,
            'message': (f"Hyperspace jump from {origin_name} ({a if state['system_id'] == b else b}) "
                        f"to {target['name']} ({target_system_id}). "
                        f"Arrived at {arrival_loc}. [{cost} TU]")
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

    def _detect_ships_at_location(self, state):
        """
        Detect other ships at the same grid square (automatic, p=1).
        Returns list of detected ship dicts and adds them to contacts.
        Only checks ships -- planets/bases are found by scans.
        """
        ships = self.conn.execute(
            """SELECT s.*, pp.faction_id FROM ships s
               JOIN prefects pp ON s.owner_prefect_id = pp.prefect_id
               JOIN players p ON pp.player_id = p.player_id
               WHERE s.system_id = ? AND s.game_id = ? AND p.status = 'active'
               AND s.grid_col = ? AND s.grid_row = ?
               AND s.ship_id != ?""",
            (state['system_id'], self.game_id,
             state['col'], state['row'], state['ship_id'])
        ).fetchall()

        detected = []
        for s in ships:
            faction = self._get_faction(s['faction_id'])
            display_name = f"{faction['abbreviation']} {s['name']}"
            loc = f"{s['grid_col']}{s['grid_row']:02d}"
            contact = {
                'type': 'ship', 'id': s['ship_id'],
                'name': display_name,
                'col': s['grid_col'], 'row': s['grid_row'],
                'symbol': '^',
                'hull_count': s['hull_count'],
                'hull_type': s['hull_type'],
            }
            detected.append(contact)
            # Add to contacts if not already known this turn
            if not any(c['type'] == 'ship' and c['id'] == s['ship_id']
                       for c in self.contacts):
                self.contacts.append(contact)
        return detected

    def _commit_ship_state(self, state):
        """Write final ship state back to database."""
        self.conn.execute("""
            UPDATE ships SET
                system_id = ?,
                grid_col = ?, grid_row = ?, tu_remaining = ?,
                docked_at_base_id = ?, orbiting_body_id = ?,
                landed_body_id = ?, landed_x = ?, landed_y = ?
            WHERE ship_id = ? AND game_id = ?
        """, (
            state['system_id'],
            state['col'], state['row'], state['tu'],
            state['docked_at'], state['orbiting'],
            state['landed'], state['landed_x'], state['landed_y'],
            state['ship_id'], self.game_id
        ))
        self.conn.commit()

    def _update_contacts(self, prefect_id, system_id):
        """Update the known contacts list for the player."""
        game = self.get_game()
        for contact in self.contacts:
            # Check if already known
            existing = self.conn.execute("""
                SELECT contact_id FROM known_contacts
                WHERE prefect_id = ? AND object_type = ? AND object_id = ?
            """, (prefect_id, contact['type'], contact['id'])).fetchone()

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
                    (prefect_id, object_type, object_id, object_name,
                     location_system, location_col, location_row,
                     discovered_turn_year, discovered_turn_week)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    prefect_id, contact['type'], contact['id'], contact['name'],
                    system_id, contact['col'], contact['row'],
                    game['current_year'], game['current_week']
                ))
        self.conn.commit()

    def advance_turn(self):
        """Advance the game turn (year.week) and generate new market prices if cycle boundary."""
        from engine.game_setup import generate_market_prices, get_market_cycle_start, MARKET_CYCLE_WEEKS

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

        # Generate new market prices only at start of a new cycle
        old_cycle = get_market_cycle_start(week - 1 if week > 1 else 52)
        new_cycle = get_market_cycle_start(week)
        if new_cycle != old_cycle or week == 1:
            generate_market_prices(self.conn, self.game_id, year, week)

        return year, week

    def close(self):
        """Close database connection."""
        self.conn.close()
