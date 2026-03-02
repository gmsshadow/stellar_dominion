"""
Stellar Dominion - Turn Resolution Engine
Resolves orders for ships, deducting TU and updating game state.
"""

import random
import hashlib
import heapq
import math
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
    'MESSAGE': 0,      # sending messages is free
    'MAKEOFFICER': 10, # promoting crew takes some time
    'RENAMESHIP': 0,
    'RENAMEBASE': 0,
    'RENAMEPREFECT': 0,
    'RENAMEOFFICER': 0,
    'CHANGEFACTION': 0,
    'MODERATOR': 0,
}

# Special item IDs
CREW_ITEM_ID = 401  # Human Crew
OFFICER_WAGE = 5    # Credits per officer per week
CREW_WAGE = 1       # Credits per regular crew per week

# Jump drive configuration (scope to make per-ship via installed items later)
JUMP_CONFIG = {
    'min_star_distance': 10,   # minimum squares from star to initiate jump
    'max_jump_range': 1,       # max systems away (1 = adjacent only, up to 4 planned)
    'tu_per_hop': 60,          # TU cost per system jumped (for multi-hop: cost * hops)
}


class TurnResolver:
    """Resolves turns for ships, supporting interleaved multi-ship resolution."""

    def __init__(self, db_path=None, game_id="OMICRON101"):
        self.db_path = db_path
        self.game_id = game_id
        self.conn = get_connection(db_path)
        self.log = []  # Turn execution log
        self.pending = []  # Legacy (unused) — overflow handled by caller
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
        cost = self._effective_tu_cost(state['move_cost'], state.get('efficiency', 100.0))

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
        overflows = {}  # Orders that carry forward to next turn (TU exhaustion)
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
                'efficiency': self._calc_efficiency(ship),
            }
            logs[ship_id] = []
            overflows[ship_id] = []
            contacts_map[ship_id] = []
            rngs[ship_id] = random.Random(seed)

            # Filter out CLEAR command — if present, caller already handled it
            queues[ship_id] = [o for o in orders if o['command'] != 'CLEAR']

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
                        acc, state, step_result
                    )
                    logs[ship_id].append(log_entry)
                    del move_acc[ship_id]

                    # If TU exhausted during MOVE, collect overflow
                    if log_entry.get('tu_exhausted'):
                        # The MOVE itself carries forward (remaining distance)
                        overflow_move = {
                            'command': 'MOVE',
                            'params': {'col': target_col, 'row': target_row},
                        }
                        overflows[ship_id].append(overflow_move)
                        # Plus all remaining orders in queue
                        for rem in queues[ship_id]:
                            overflows[ship_id].append({
                                'command': rem['command'],
                                'params': rem['params'],
                            })
                        queues[ship_id].clear()

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

                # If TU exhausted, collect this order + remaining as overflow
                if result.get('tu_exhausted'):
                    overflows[ship_id].append({
                        'command': order['command'],
                        'params': order['params'],
                    })
                    for rem in queues[ship_id]:
                        overflows[ship_id].append({
                            'command': rem['command'],
                            'params': rem['params'],
                        })
                    queues[ship_id].clear()

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
                eff_move = self._effective_tu_cost(state['move_cost'], state.get('efficiency', 100.0))
                heapq.heappush(heap, (elapsed + eff_move, counter, ship_id))
                counter += 1

        # Flush any interrupted moves (ran out of TU mid-move)
        for ship_id, acc in list(move_acc.items()):
            state = states[ship_id]
            log_entry = self._build_move_log_entry(
                acc, state, {'moved': False, 'finished': True, 'out_of_tu': True}
            )
            logs[ship_id].append(log_entry)

            # Collect overflow: remaining MOVE distance + any remaining orders
            if log_entry.get('tu_exhausted'):
                target_col = acc['target_col']
                target_row = acc['target_row']
                overflows[ship_id].append({
                    'command': 'MOVE',
                    'params': {'col': target_col, 'row': target_row},
                })
                for rem in queues[ship_id]:
                    overflows[ship_id].append({
                        'command': rem['command'],
                        'params': rem['params'],
                    })
                queues[ship_id].clear()

        # === Commit final states and build results ===
        results = {}
        for ship_id in states:
            state = states[ship_id]
            ship = ship_rows[ship_id]

            self.log = logs[ship_id]
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
                'overflow': overflows[ship_id],
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
        eff = state.get('efficiency', 100.0)

        if cmd == 'MOVE':
            return self._effective_tu_cost(state['move_cost'], eff)
        elif cmd == 'WAIT':
            if isinstance(params, (int, float)):
                return min(int(params), state['tu'])
            return state['tu']
        elif cmd in TU_COSTS:
            return self._effective_tu_cost(TU_COSTS[cmd], eff)
        return 0

    def _build_move_log_entry(self, acc, state, final_step, pendings_list=None):
        """Build a combined MOVE log entry from accumulated steps."""
        target_col = acc['target_col']
        target_row = acc['target_row']
        target_str = f"{target_col}{target_row:02d}"
        waypoints = acc['waypoints']
        tu_before = acc['tu_before']
        orbit_msg = acc['orbit_msg']
        steps_taken = len(waypoints) - 1  # first entry is start position
        effective_move_cost = self._effective_tu_cost(state['move_cost'], state.get('efficiency', 100.0))
        total_cost = steps_taken * effective_move_cost
        tu_exhausted = False

        reached = (state['col'] == target_col and state['row'] == target_row)
        final_loc = f"{state['col']}{state['row']:02d}"

        if final_step.get('already_there'):
            msg = f"Already at {target_str}."
        elif final_step.get('blocked_docked'):
            msg = "Cannot move while docked. UNDOCK first. Order dropped."
        elif final_step.get('blocked_landed'):
            msg = "Cannot move while landed. TAKEOFF first. Order dropped."
        elif steps_taken == 0 and final_step.get('out_of_tu'):
            eff_mc = self._effective_tu_cost(state['move_cost'], state.get('efficiency', 100.0))
            msg = (f"Insufficient TU for move ({state['tu']} < "
                   f"{eff_mc}). Order carries forward.")
            tu_exhausted = True
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
            tu_exhausted = True

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
            'success': reached, 'tu_exhausted': tu_exhausted,
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

    def _get_crew_count(self, ship_id):
        """Get total crew count: cargo crew + officers on ship."""
        row = self.conn.execute(
            "SELECT quantity FROM cargo_items WHERE ship_id = ? AND item_type_id = ?",
            (ship_id, CREW_ITEM_ID)
        ).fetchone()
        cargo_crew = row['quantity'] if row else 0
        off_row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM officers WHERE ship_id = ?",
            (ship_id,)
        ).fetchone()
        officer_count = off_row['cnt'] if off_row else 0
        return cargo_crew + officer_count

    def _sync_crew_count(self, ship_id):
        """Sync ships.crew_count with cargo crew + officers."""
        crew = self._get_crew_count(ship_id)
        self.conn.execute(
            "UPDATE ships SET crew_count = ? WHERE ship_id = ?",
            (crew, ship_id)
        )

    @staticmethod
    def _calc_efficiency(ship):
        """Calculate crew efficiency as percentage (0-100).
        Efficiency = min(100, crew_count / crew_required * 100).
        """
        required = ship['crew_required'] if ship['crew_required'] else 1
        if required <= 0:
            return 100.0
        return min(100.0, (ship['crew_count'] / required) * 100.0)

    @staticmethod
    def _effective_tu_cost(base_cost, efficiency):
        """Apply efficiency penalty to a TU cost.
        Penalty = (100 - efficiency)% extra cost, rounded up.
        E.g. 80% efficiency -> 20% penalty -> cost * 1.2, ceil'd.
        """
        if base_cost <= 0:
            return 0
        if efficiency >= 100.0:
            return base_cost
        penalty_pct = (100.0 - efficiency) / 100.0
        return math.ceil(base_cost * (1.0 + penalty_pct))

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
            'efficiency': self._calc_efficiency(ship),
        }

        self.log = []
        self.contacts = []
        overflow = []

        # Execute each order in sequence; stop on TU exhaustion
        remaining_orders = list(orders)
        for i, order in enumerate(remaining_orders):
            if order['command'] == 'CLEAR':
                continue  # Already handled by caller
            result = self._execute_order(state, order, rng)
            self.log.append(result)

            # If TU exhausted, collect this order + remaining as overflow
            if result.get('tu_exhausted'):
                overflow.append({
                    'command': order['command'],
                    'params': order['params'],
                })
                for rem in remaining_orders[i + 1:]:
                    if rem['command'] != 'CLEAR':
                        overflow.append({
                            'command': rem['command'],
                            'params': rem['params'],
                        })
                break

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
            'overflow': overflow,
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
        elif cmd == 'MESSAGE':
            return self._cmd_message(state, params)
        elif cmd == 'MAKEOFFICER':
            return self._cmd_makeofficer(state, params)
        elif cmd == 'RENAMESHIP':
            return self._cmd_renameship(state, params)
        elif cmd == 'RENAMEBASE':
            return self._cmd_renamebase(state, params)
        elif cmd == 'RENAMEPREFECT':
            return self._cmd_renameprefect(state, params)
        elif cmd == 'RENAMEOFFICER':
            return self._cmd_renameofficer(state, params)
        elif cmd == 'CHANGEFACTION':
            return self._cmd_changefaction(state, params)
        elif cmd == 'MODERATOR':
            return self._cmd_moderator(state, params)
        elif cmd == 'CLEAR':
            # CLEAR is handled by the caller before resolution starts.
            # If it reaches here, just log it as a no-op.
            return {
                'command': 'CLEAR', 'params': None,
                'tu_before': tu_before, 'tu_after': tu_before,
                'tu_cost': 0, 'success': True,
                'message': "Overflow orders from previous turn cleared."
            }
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
        cost_per_step = self._effective_tu_cost(state['move_cost'], state['efficiency'])

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
            return {
                'command': 'MOVE', 'params': f"{target_col}{target_row:02d}",
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False, 'tu_exhausted': True,
                'message': f"Insufficient TU for move ({state['tu']} < {cost_per_step}). Order carries forward."
            }

        # If docked, must undock first
        if state['docked_at']:
            return {
                'command': 'MOVE', 'params': f"{target_col}{target_row:02d}",
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False,
                'message': "Cannot move while docked. UNDOCK first. Order dropped."
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
                # Out of TU mid-move -- mark for overflow
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
        cost = self._effective_tu_cost(TU_COSTS['LOCATIONSCAN'], state['efficiency'])

        if state['tu'] < cost:
            return {
                'command': 'LOCATIONSCAN', 'params': None,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False, 'tu_exhausted': True,
                'message': f"Insufficient TU for scan ({state['tu']} < {cost}). Order carries forward."
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

                # For celestial bodies, also list surface ports and outposts
                if obj['type'] in ('planet', 'moon', 'gas_giant'):
                    body_id = obj['id']
                    ports = self.conn.execute(
                        "SELECT name, port_id, surface_x, surface_y FROM surface_ports WHERE body_id = ?",
                        (body_id,)
                    ).fetchall()
                    for p in ports:
                        scan_lines.append(
                            f"        Surface Port: {p['name']} ({p['port_id']}) at ({p['surface_x']},{p['surface_y']})")
                    outposts = self.conn.execute(
                        "SELECT name, outpost_id, surface_x, surface_y, outpost_type FROM outposts WHERE body_id = ?",
                        (body_id,)
                    ).fetchall()
                    for o in outposts:
                        scan_lines.append(
                            f"        Outpost: {o['name']} ({o['outpost_id']}) [{o['outpost_type']}] at ({o['surface_x']},{o['surface_y']})")
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
        cost = self._effective_tu_cost(TU_COSTS['SYSTEMSCAN'], state['efficiency'])

        if state['tu'] < cost:
            return {
                'command': 'SYSTEMSCAN', 'params': None,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False, 'tu_exhausted': True,
                'message': f"Insufficient TU for system scan ({state['tu']} < {cost}). Order carries forward."
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
        cost = self._effective_tu_cost(TU_COSTS['ORBIT'], state['efficiency'])

        if state['tu'] < cost:
            return {
                'command': 'ORBIT', 'params': body_id,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False, 'tu_exhausted': True,
                'message': f"Insufficient TU for orbit ({state['tu']} < {cost}). Order carries forward."
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
            return {
                'command': 'ORBIT', 'params': body_id,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False,
                'message': f"Unable to orbit: ship is not at {body['name']} location ({loc}). Order dropped."
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
        cost = self._effective_tu_cost(TU_COSTS['DOCK'], state['efficiency'])

        if state['tu'] < cost:
            return {
                'command': 'DOCK', 'params': base_id,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False, 'tu_exhausted': True,
                'message': f"Insufficient TU for docking ({state['tu']} < {cost}). Order carries forward."
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
            return {
                'command': 'DOCK', 'params': base_id,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False,
                'message': f"Unable to dock: ship is not at base location ({loc}). Order dropped."
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
        cost = self._effective_tu_cost(TU_COSTS['UNDOCK'], state['efficiency'])

        if not state['docked_at']:
            return {
                'command': 'UNDOCK', 'params': None,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False,
                'message': "Unable to undock: ship is not docked at any base."
            }

        if state['tu'] < cost:
            return {
                'command': 'UNDOCK', 'params': None,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False, 'tu_exhausted': True,
                'message': f"Insufficient TU to undock ({state['tu']} < {cost}). Order carries forward."
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
        cost = self._effective_tu_cost(TU_COSTS['LAND'], state['efficiency'])

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
            return {
                'command': 'LAND', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False, 'tu_exhausted': True,
                'message': f"Insufficient TU to land ({state['tu']} < {cost}). Order carries forward."
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
        cost = self._effective_tu_cost(TU_COSTS['TAKEOFF'], state['efficiency'])

        if not state['landed']:
            return {
                'command': 'TAKEOFF', 'params': None,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': "Cannot take off: ship is not landed on any surface."
            }

        if state['tu'] < cost:
            return {
                'command': 'TAKEOFF', 'params': None,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False, 'tu_exhausted': True,
                'message': f"Insufficient TU to take off ({state['tu']} < {cost}). Order carries forward."
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
        cost = self._effective_tu_cost(TU_COSTS['SURFACESCAN'], state['efficiency'])

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
            return {
                'command': 'SURFACESCAN', 'params': None,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False, 'tu_exhausted': True,
                'message': f"Insufficient TU for surface scan ({state['tu']} < {cost}). Order carries forward."
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

        # Query surface ports on this body
        port_positions = []
        ports = self.conn.execute(
            "SELECT port_id, name, surface_x, surface_y FROM surface_ports WHERE body_id = ?",
            (body_id,)
        ).fetchall()
        for p in ports:
            port_positions.append((p['surface_x'], p['surface_y'], p['name']))

        # Query outposts on this body (shown in data section, not on map)
        outpost_list = []
        outposts = self.conn.execute(
            "SELECT outpost_id, name, surface_x, surface_y, outpost_type FROM outposts WHERE body_id = ?",
            (body_id,)
        ).fetchall()
        for o in outposts:
            outpost_list.append((o['surface_x'], o['surface_y'], o['name'], o['outpost_type']))

        map_lines = render_surface_map(
            tiles, body['name'], body_id, planetary_data,
            ship_pos=ship_pos, port_positions=port_positions or None,
            outpost_positions=outpost_list or None
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

        # Cap crew purchases to life support capacity
        if item_id == CREW_ITEM_ID:
            current_crew = self._get_crew_count(state['ship_id'])
            life_support = ship['life_support_capacity'] if ship['life_support_capacity'] else 20
            max_by_ls = life_support - current_crew
            actual_qty = min(actual_qty, max(0, max_by_ls))

        if actual_qty <= 0:
            extra_info = ""
            if item_id == CREW_ITEM_ID:
                current_crew = self._get_crew_count(state['ship_id'])
                life_support = ship['life_support_capacity'] if ship['life_support_capacity'] else 20
                extra_info = f", life support={current_crew}/{life_support}"
            return {
                'command': 'BUY', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': (f"Cannot buy any {item['name']}: "
                            f"stock={available_stock}, credits={prefect['credits']:,.0f} cr, "
                            f"cargo space={available_mu} MU free{extra_info}.")
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
            if item_id == CREW_ITEM_ID:
                current_crew = self._get_crew_count(state['ship_id'])
                life_support = ship['life_support_capacity'] if ship['life_support_capacity'] else 20
                if life_support - current_crew < quantity:
                    cap_reasons.append(f"life support={current_crew}/{life_support}")
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

        # Sync crew_count and efficiency if buying crew
        if item_id == CREW_ITEM_ID:
            self._sync_crew_count(state['ship_id'])
            ship_now = self.get_ship(state['ship_id'])
            state['efficiency'] = self._calc_efficiency(ship_now)

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

        # Sync crew_count and efficiency if selling crew
        if item_id == CREW_ITEM_ID:
            self._sync_crew_count(state['ship_id'])
            ship_now = self.get_ship(state['ship_id'])
            state['efficiency'] = self._calc_efficiency(ship_now)

        self.conn.commit()

        base = self.conn.execute(
            "SELECT name FROM starbases WHERE base_id = ?", (base_id,)
        ).fetchone()
        base_name = base['name'] if base else str(base_id)

        # Warn if crew dropped below required
        crew_warning = ""
        if item_id == CREW_ITEM_ID:
            ship_now = self.get_ship(state['ship_id'])
            if ship_now['crew_count'] < ship_now['crew_required']:
                crew_warning = (f" WARNING: Crew now {ship_now['crew_count']}, "
                                f"required {ship_now['crew_required']}. Ship undermanned!")

        return {
            'command': 'SELL', 'params': params_str,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': 0, 'success': True,
            'credits_earned': total_income,
            'item_name': item['name'], 'item_id': item_id,
            'quantity': actual_qty,
            'message': (f"Sold {actual_qty} {item['name']} ({item_id}) "
                        f"at {sell_price} cr each = {total_income:,} cr total "
                        f"to {base_name} ({base_id}). [{total_mass} MU freed]{capped_msg}{crew_warning}")
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

    def _find_jump_route(self, from_system, to_system, max_hops):
        """
        BFS across system_links to find shortest route between two systems.
        Returns number of hops if reachable within max_hops, or None if not.
        For max_hops=1 this is a simple adjacency check.
        """
        if from_system == to_system:
            return 0

        # Load all links into adjacency map
        links = self.conn.execute("SELECT system_a, system_b FROM system_links").fetchall()
        adj = {}
        for link in links:
            a, b = link['system_a'], link['system_b']
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)

        # BFS
        visited = {from_system}
        frontier = [from_system]
        for depth in range(1, max_hops + 1):
            next_frontier = []
            for sys_id in frontier:
                for neighbor in adj.get(sys_id, set()):
                    if neighbor == to_system:
                        return depth
                    if neighbor not in visited:
                        visited.add(neighbor)
                        next_frontier.append(neighbor)
            frontier = next_frontier

        return None  # Not reachable within max_hops

    def _cmd_jump(self, state, target_system_id):
        """JUMP <system_id> - hyperspace jump to a linked star system."""
        tu_before = state['tu']
        cost = self._effective_tu_cost(JUMP_CONFIG['tu_per_hop'], state['efficiency'])
        min_star_dist = JUMP_CONFIG['min_star_distance']
        max_range = JUMP_CONFIG['max_jump_range']
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
        star_col, star_row = 'M', 13
        dist_from_star = grid_distance(state['col'], state['row'], star_col, star_row)
        if dist_from_star < min_star_dist:
            current_loc = f"{state['col']}{state['row']:02d}"
            return {
                'command': 'JUMP', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': (f"Cannot jump: too close to the star. "
                            f"Ship at {current_loc} is {dist_from_star} squares from the star "
                            f"(minimum {min_star_dist} required).")
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

        # Find route: BFS across system_links up to max_jump_range hops
        jump_hops = self._find_jump_route(state['system_id'], target_system_id, max_range)
        if jump_hops is None:
            if max_range == 1:
                msg = f"Cannot jump: no known hyperspace link to {target['name']} ({target_system_id})."
            else:
                msg = (f"Cannot jump: {target['name']} ({target_system_id}) is beyond "
                       f"jump range ({max_range} system{'s' if max_range > 1 else ''} max).")
            return {
                'command': 'JUMP', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': msg
            }

        # Total cost scales with hops
        total_cost = cost * jump_hops

        # Check TU
        if state['tu'] < total_cost:
            return {
                'command': 'JUMP', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False, 'tu_exhausted': True,
                'message': (f"Insufficient TU for jump ({state['tu']} < {total_cost}). "
                            f"Order carries forward.")
            }

        # Execute jump
        origin_system = self.conn.execute(
            "SELECT name FROM star_systems WHERE system_id = ?",
            (state['system_id'],)
        ).fetchone()
        origin_name = origin_system['name'] if origin_system else str(state['system_id'])
        origin_id = state['system_id']

        state['system_id'] = target_system_id
        state['tu'] -= total_cost

        # Commit position immediately so subsequent commands see the new system
        self._commit_ship_position(state)

        arrival_loc = f"{state['col']}{state['row']:02d}"
        hop_str = f" ({jump_hops} hops)" if jump_hops > 1 else ""
        return {
            'command': 'JUMP', 'params': params_str,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': total_cost, 'success': True,
            'message': (f"Hyperspace jump from {origin_name} ({origin_id}) "
                        f"to {target['name']} ({target_system_id}){hop_str}. "
                        f"Arrived at {arrival_loc}. [{total_cost} TU]")
        }

    def _cmd_message(self, state, params):
        """MESSAGE <target_id> <text> - send a free-text message to another position."""
        tu_before = state['tu']
        target_id = params['target_id']
        text = params['text']
        params_str = f"{target_id} {text}"

        # Determine recipient type by looking up the target_id
        recipient_type = None
        recipient_name = None

        ship = self.conn.execute(
            "SELECT ship_id, name FROM ships WHERE ship_id = ? AND game_id = ?",
            (target_id, self.game_id)
        ).fetchone()
        if ship:
            recipient_type = 'ship'
            recipient_name = ship['name']

        if not recipient_type:
            base = self.conn.execute(
                "SELECT base_id, name FROM starbases WHERE base_id = ? AND game_id = ?",
                (target_id, self.game_id)
            ).fetchone()
            if base:
                recipient_type = 'base'
                recipient_name = base['name']

        if not recipient_type:
            prefect = self.conn.execute(
                "SELECT prefect_id, name FROM prefects WHERE prefect_id = ? AND game_id = ?",
                (target_id, self.game_id)
            ).fetchone()
            if prefect:
                recipient_type = 'prefect'
                recipient_name = prefect['name']

        if not recipient_type:
            return {
                'command': 'MESSAGE', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Message failed: unknown position {target_id}. Order dropped."
            }

        # Store the message
        game = self.get_game()
        self.conn.execute("""
            INSERT INTO messages
            (game_id, sender_type, sender_id, sender_name,
             recipient_type, recipient_id, message_text,
             sent_turn_year, sent_turn_week)
            VALUES (?, 'ship', ?, ?, ?, ?, ?, ?, ?)
        """, (self.game_id, state['ship_id'], state['name'],
              recipient_type, target_id, text,
              game['current_year'], game['current_week']))
        self.conn.commit()

        return {
            'command': 'MESSAGE', 'params': params_str,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': 0, 'success': True,
            'message': f"Message sent to {recipient_name} ({target_id})."
        }

    def _cmd_makeofficer(self, state, params):
        """MAKEOFFICER <ship_id> <crew_type_id> - promote a crew member to officer."""
        from engine.game_setup import generate_random_name

        tu_before = state['tu']
        target_ship_id = params['ship_id']
        crew_type_id = params['crew_type_id']
        cost = self._effective_tu_cost(TU_COSTS['MAKEOFFICER'], state['efficiency'])
        params_str = f"{target_ship_id} {crew_type_id}"

        # Validate ship_id matches executing ship
        if target_ship_id != state['ship_id']:
            return {
                'command': 'MAKEOFFICER', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': (f"Cannot promote officer: ship {target_ship_id} does not match "
                            f"executing ship {state['ship_id']}. Order dropped.")
            }

        # Check TU
        if state['tu'] < cost:
            return {
                'command': 'MAKEOFFICER', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False, 'tu_exhausted': True,
                'message': f"Insufficient TU for officer promotion ({state['tu']} < {cost}). Order carries forward."
            }

        # Check crew of that type in cargo
        cargo = self.conn.execute(
            "SELECT * FROM cargo_items WHERE ship_id = ? AND item_type_id = ?",
            (state['ship_id'], crew_type_id)
        ).fetchone()
        if not cargo or cargo['quantity'] <= 0:
            # Look up the item name for a better message
            item = self.conn.execute(
                "SELECT name FROM trade_goods WHERE item_id = ?", (crew_type_id,)
            ).fetchone()
            item_name = item['name'] if item else f"item {crew_type_id}"
            return {
                'command': 'MAKEOFFICER', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot promote officer: no {item_name} ({crew_type_id}) in cargo. Order dropped."
            }

        state['tu'] -= cost

        # Remove 1 crew from cargo
        new_qty = cargo['quantity'] - 1
        if new_qty <= 0:
            self.conn.execute("DELETE FROM cargo_items WHERE cargo_id = ?", (cargo['cargo_id'],))
        else:
            self.conn.execute(
                "UPDATE cargo_items SET quantity = ? WHERE cargo_id = ?",
                (new_qty, cargo['cargo_id'])
            )

        # Update cargo_used (1 MU freed since crew weighs 1 MU)
        self.conn.execute(
            "UPDATE ships SET cargo_used = cargo_used - ? WHERE ship_id = ?",
            (cargo['mass_per_unit'], state['ship_id'])
        )

        # Determine next crew_number
        max_cn = self.conn.execute(
            "SELECT MAX(crew_number) as mx FROM officers WHERE ship_id = ?",
            (state['ship_id'],)
        ).fetchone()
        next_cn = (max_cn['mx'] or 0) + 1

        # Generate officer
        name = params.get('name') or generate_random_name()
        self.conn.execute("""
            INSERT INTO officers
            (ship_id, crew_number, name, rank, specialty, experience,
             crew_factors, crew_type_id, wages)
            VALUES (?, ?, ?, 'Ensign', 'General', 0, 5, ?, ?)
        """, (state['ship_id'], next_cn, name, crew_type_id, OFFICER_WAGE))

        # Sync crew_count (net zero: -1 cargo +1 officer)
        self._sync_crew_count(state['ship_id'])
        ship_now = self.get_ship(state['ship_id'])
        state['efficiency'] = self._calc_efficiency(ship_now)

        self.conn.commit()

        item = self.conn.execute(
            "SELECT name FROM trade_goods WHERE item_id = ?", (crew_type_id,)
        ).fetchone()
        item_name = item['name'] if item else f"item {crew_type_id}"

        return {
            'command': 'MAKEOFFICER', 'params': params_str,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': cost, 'success': True,
            'message': (f"Promoted {item_name} to officer: Ensign {name} "
                        f"[{next_cn}] assigned. [{cost} TU]")
        }

    def _cmd_renameship(self, state, params):
        """RENAMESHIP <ship_id> <new_name> - rename a ship."""
        tu_before = state['tu']
        target_id = params['id']
        new_name = params['name']
        params_str = f"{target_id} {new_name}"

        if target_id != state['ship_id']:
            return {
                'command': 'RENAMESHIP', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': (f"Cannot rename: ship {target_id} does not match "
                            f"executing ship {state['ship_id']}. Order dropped.")
            }

        old_name = state['name']
        self.conn.execute(
            "UPDATE ships SET name = ? WHERE ship_id = ?",
            (new_name, target_id)
        )
        state['name'] = new_name
        self.conn.commit()

        return {
            'command': 'RENAMESHIP', 'params': params_str,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': 0, 'success': True,
            'message': f"Ship renamed from '{old_name}' to '{new_name}'."
        }

    def _cmd_renamebase(self, state, params):
        """RENAMEBASE <base_id> <new_name> - rename a starbase."""
        tu_before = state['tu']
        target_id = params['id']
        new_name = params['name']
        params_str = f"{target_id} {new_name}"

        # Verify base exists and is owned by this ship's prefect
        ship = self.get_ship(state['ship_id'])
        base = self.conn.execute(
            "SELECT * FROM starbases WHERE base_id = ?", (target_id,)
        ).fetchone()
        if not base:
            return {
                'command': 'RENAMEBASE', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot rename: base {target_id} not found. Order dropped."
            }
        if base['owner_prefect_id'] != ship['owner_prefect_id']:
            return {
                'command': 'RENAMEBASE', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot rename: base {target_id} is not owned by your prefect. Order dropped."
            }

        old_name = base['name']
        self.conn.execute(
            "UPDATE starbases SET name = ? WHERE base_id = ?",
            (new_name, target_id)
        )
        self.conn.commit()

        return {
            'command': 'RENAMEBASE', 'params': params_str,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': 0, 'success': True,
            'message': f"Base renamed from '{old_name}' to '{new_name}'."
        }

    def _cmd_renameprefect(self, state, params):
        """RENAMEPREFECT <prefect_id> <new_name> - rename a prefect."""
        tu_before = state['tu']
        target_id = params['id']
        new_name = params['name']
        params_str = f"{target_id} {new_name}"

        # Verify prefect is the one owning this ship
        ship = self.get_ship(state['ship_id'])
        if target_id != ship['owner_prefect_id']:
            return {
                'command': 'RENAMEPREFECT', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot rename: prefect {target_id} is not your prefect. Order dropped."
            }

        prefect = self.conn.execute(
            "SELECT name FROM prefects WHERE prefect_id = ?", (target_id,)
        ).fetchone()
        old_name = prefect['name'] if prefect else str(target_id)

        self.conn.execute(
            "UPDATE prefects SET name = ? WHERE prefect_id = ?",
            (new_name, target_id)
        )
        self.conn.commit()

        return {
            'command': 'RENAMEPREFECT', 'params': params_str,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': 0, 'success': True,
            'message': f"Prefect renamed from '{old_name}' to '{new_name}'."
        }

    def _cmd_renameofficer(self, state, params):
        """RENAMEOFFICER <ship_id> <crew_number> <new_name> - rename an officer."""
        tu_before = state['tu']
        target_ship = params['ship_id']
        crew_number = params['crew_number']
        new_name = params['name']
        params_str = f"{target_ship} {crew_number} {new_name}"

        if target_ship != state['ship_id']:
            return {
                'command': 'RENAMEOFFICER', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': (f"Cannot rename: ship {target_ship} does not match "
                            f"executing ship {state['ship_id']}. Order dropped.")
            }

        officer = self.conn.execute(
            "SELECT * FROM officers WHERE ship_id = ? AND crew_number = ?",
            (state['ship_id'], crew_number)
        ).fetchone()
        if not officer:
            return {
                'command': 'RENAMEOFFICER', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot rename: no officer with crew number {crew_number}. Order dropped."
            }

        old_name = officer['name']
        self.conn.execute(
            "UPDATE officers SET name = ? WHERE officer_id = ?",
            (new_name, officer['officer_id'])
        )
        self.conn.commit()

        return {
            'command': 'RENAMEOFFICER', 'params': params_str,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': 0, 'success': True,
            'message': (f"Officer [{crew_number}] {officer['rank']} renamed "
                        f"from '{old_name}' to '{new_name}'.")
        }

    def _cmd_changefaction(self, state, params):
        """CHANGEFACTION <faction_id> [reason] - request faction change (GM-moderated)."""
        tu_before = state['tu']
        target_faction_id = params['faction_id']
        reason = params.get('reason', '')
        params_str = f"{target_faction_id}"
        if reason:
            params_str += f" {reason}"

        ship = self.get_ship(state['ship_id'])
        prefect = self.conn.execute(
            "SELECT * FROM prefects WHERE prefect_id = ?",
            (ship['owner_prefect_id'],)
        ).fetchone()
        current_faction_id = prefect['faction_id']

        # Check target faction exists
        target_faction = self.conn.execute(
            "SELECT * FROM factions WHERE faction_id = ?",
            (target_faction_id,)
        ).fetchone()
        if not target_faction:
            return {
                'command': 'CHANGEFACTION', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot request faction change: faction {target_faction_id} not found. Order dropped."
            }

        # Already in that faction
        if current_faction_id == target_faction_id:
            return {
                'command': 'CHANGEFACTION', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': (f"Already a member of {target_faction['name']} "
                            f"({target_faction['abbreviation']}). Order dropped.")
            }

        # Check for existing pending request
        existing = self.conn.execute(
            "SELECT * FROM faction_requests WHERE game_id = ? AND prefect_id = ? AND status = 'pending'",
            (self.game_id, prefect['prefect_id'])
        ).fetchone()
        if existing:
            existing_faction = self.conn.execute(
                "SELECT abbreviation FROM factions WHERE faction_id = ?",
                (existing['target_faction_id'],)
            ).fetchone()
            ef_name = existing_faction['abbreviation'] if existing_faction else str(existing['target_faction_id'])
            return {
                'command': 'CHANGEFACTION', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': (f"Cannot request: you already have a pending faction change "
                            f"request to {ef_name} (request #{existing['request_id']}). Order dropped.")
            }

        # Submit request
        game = self.get_game()
        self.conn.execute("""
            INSERT INTO faction_requests
            (game_id, prefect_id, current_faction_id, target_faction_id,
             reason, status, requested_turn_year, requested_turn_week)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        """, (self.game_id, prefect['prefect_id'], current_faction_id,
              target_faction_id, reason,
              game['current_year'], game['current_week']))
        self.conn.commit()

        from db.database import get_faction
        current_faction = get_faction(self.conn, current_faction_id)
        reason_str = f" Reason: \"{reason}\"" if reason else ""

        return {
            'command': 'CHANGEFACTION', 'params': params_str,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': 0, 'success': True,
            'message': (f"Faction change requested: {current_faction['abbreviation']} -> "
                        f"{target_faction['abbreviation']} ({target_faction['name']}).{reason_str} "
                        f"Awaiting GM approval.")
        }

    def _cmd_moderator(self, state, params):
        """MODERATOR <text> - submit a free-text request to the GM."""
        tu_before = state['tu']
        request_text = params['text']
        params_str = request_text

        ship = self.get_ship(state['ship_id'])
        game = self.get_game()

        # Look up the GM response (created during Phase 1.1 auto-hold)
        action = self.conn.execute("""
            SELECT * FROM moderator_actions
            WHERE game_id = ? AND ship_id = ? AND request_text = ?
            AND requested_turn_year = ? AND requested_turn_week = ?
        """, (self.game_id, state['ship_id'], request_text,
              game['current_year'], game['current_week'])).fetchone()

        if action and action['status'] == 'responded':
            # Mark as resolved
            self.conn.execute("""
                UPDATE moderator_actions
                SET status = 'resolved', resolved_turn_year = ?, resolved_turn_week = ?
                WHERE action_id = ?
            """, (game['current_year'], game['current_week'], action['action_id']))
            self.conn.commit()

            response = action['gm_response']
            return {
                'command': 'MODERATOR', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': True,
                'message': (f"MODERATOR REQUEST: \"{request_text}\"\n"
                            f"  GM RESPONSE: \"{response}\"")
            }
        else:
            # No response yet (shouldn't happen if auto-hold worked, but handle gracefully)
            return {
                'command': 'MODERATOR', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': True,
                'message': (f"MODERATOR REQUEST: \"{request_text}\"\n"
                            f"  GM RESPONSE: (no response — request noted)")
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
