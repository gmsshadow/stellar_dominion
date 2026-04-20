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
    'SCANLOCATION': 1,  # per OC of scan duration; player specifies duration
    'SCANSYSTEM': 20,
    'ORBIT': 10,
    'DOCK': 30,
    'UNDOCK': 10,
    'LEAVEORBIT': 0,
    'LAND': 20,
    'TAKEOFF': 20,
    'SCANSURFACE': 20,
    'BUY': 0,          # trading while docked is free
    'SELL': 0,
    'LOAD': 1,         # load ammo from cargo to magazine
    'UNLOAD': 1,       # unload ammo from magazine to cargo
    'GETMARKET': 0,
    'JUMP': 60,        # priority estimate only — actual cost from installed drive
    'MESSAGE': 0,      # sending messages is free
    'MAKEOFFICER': 10, # promoting crew takes some time
    'INSTALL': 10,     # installing a component
    'UNINSTALL': 10,   # removing a component
    'SCRAP': 0,        # scrapping cargo
    'RENAMESHIP': 0,
    'RENAMEBASE': 0,
    'RENAMEPREFECT': 0,
    'RENAMEOFFICER': 0,
    'CHANGEFACTION': 0,
    'MODERATOR': 0,
    'TARGET': 0,
    'DEFEND': 0,
    'AVOID': 0,
    'DOCTRINE': 0,
}

# Backwards-compatible command aliases (old -> new)
COMMAND_ALIASES = {
    'LOCATIONSCAN': 'SCANLOCATION',
    'SYSTEMSCAN': 'SCANSYSTEM',
    'SURFACESCAN': 'SCANSURFACE',
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
        cost = self._effective_move_step_cost(state)
        if cost is None:
            return {'moved': False, 'finished': True, 'blocked_no_engine': True}

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
                'move_efficiency': 100.0,       # movement-only efficiency (engines)
                'docked_at': ship['docked_at_base_id'],
                'orbiting': ship['orbiting_body_id'],
                'landed': ship['landed_body_id'] if 'landed_body_id' in ship.keys() else None,
                'landed_x': ship['landed_x'] if 'landed_x' in ship.keys() else 1,
                'landed_y': ship['landed_y'] if 'landed_y' in ship.keys() else 1,
                'start_col': ship['grid_col'],
                'start_row': ship['grid_row'],
                'start_tu': ship['tu_per_turn'],
                'efficiency': self._calc_efficiency(ship),
                'gravity_rating': ship['gravity_rating'] if 'gravity_rating' in ship.keys() else 1.0,
            }
            ship_size = ship['ship_size'] if 'ship_size' in ship.keys() else ship['hull_count']
            states[ship_id]['move_efficiency'] = self._calc_move_efficiency(ship_id, ship_size)
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
                                     d.get('ship_size', d.get('hull_count', '?')),
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
                            f"- {{{d.get('ship_size', d.get('hull_count', '?'))} {d.get('hull_type', '')}}}"
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
            cost = self._effective_move_step_cost(state)
            return cost or 0
        elif cmd == 'WAIT':
            if isinstance(params, (int, float)):
                return min(int(params), state['tu'])
            return state['tu']
        elif cmd == 'SCANLOCATION':
            # SCANLOCATION spends duration × per-OC cost; pull duration from params
            per_oc = self._effective_tu_cost(TU_COSTS['SCANLOCATION'], eff)
            duration = 1
            if isinstance(params, dict):
                duration = max(1, int(params.get('duration', 1)))
            elif isinstance(params, (int, float)):
                duration = max(1, int(params))
            return per_oc * duration
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
        effective_move_cost = self._effective_move_step_cost(state) or 0
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
        elif final_step.get('blocked_no_engine'):
            msg = "Cannot move: no functioning engines installed. Order dropped."
        elif steps_taken == 0 and final_step.get('out_of_tu'):
            eff_mc = self._effective_move_step_cost(state) or 0
            msg = (f"Insufficient OC for move ({state['tu']} < "
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

    def _effective_move_step_cost(self, state):
        """
        MOVE uses two factors:
        - Engines: inverse scaling (fewer engines => higher cost), capped at optimal.
        - Crew: standard linear OC penalty (shared with other actions).
        """
        base = state.get('move_cost', TU_COSTS['MOVE'])
        eng_pct = float(state.get('move_efficiency', 100.0) or 0.0)
        if eng_pct <= 0.0:
            return None  # blocked (no engines)

        engine_ratio = min(1.0, eng_pct / 100.0)
        engine_scaled = math.ceil(base / engine_ratio) if engine_ratio > 0 else None
        if engine_scaled is None:
            return None

        crew_eff = float(state.get('efficiency', 100.0) or 0.0)
        return self._effective_tu_cost(engine_scaled, crew_eff)

    def _calc_move_efficiency(self, ship_id, ship_size):
        """
        Movement efficiency from installed engines.
        Rule: optimal engines = 1 per size-10 (ship_size//10), minimum 1.
        Efficiency = min(engine_count / optimal, 1.0) * 100.
        If engine_count == 0, efficiency is 0 (MOVE blocked).
        """
        try:
            size = int(ship_size) if ship_size is not None else 50
        except (ValueError, TypeError):
            size = 50
        optimal = max(1, size // 10)

        row = self.conn.execute("""
            SELECT COALESCE(SUM(ii.quantity), 0) AS engine_count
            FROM installed_items ii
            JOIN ship_components sc ON ii.component_id = sc.component_id
            WHERE ii.ship_id = ? AND sc.category = 'engine'
        """, (ship_id,)).fetchone()
        engine_count = int(row['engine_count']) if row and row['engine_count'] is not None else 0

        if engine_count <= 0:
            return 0.0
        ratio = min(1.0, engine_count / float(optimal))
        return ratio * 100.0

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

    def _gravity_adjusted_cost(self, base_cost, ship_grav, body_grav, efficiency):
        """
        Apply gravity scaling to maneuvering OC costs (ORBIT, LAND, TAKEOFF).

        - body_grav=None: cost = base / ship_grav (orbit — fights ship's own gravity well entry)
        - body_grav set: cost = base * body_grav / ship_grav (land/takeoff — fights body gravity)

        Higher ship gravity rating (more thrust per hull) = lower cost.
        Heavier bodies = higher cost.
        Crew efficiency penalty applied last.
        Returns None if ship has zero thrust (no thrusters installed).
        """
        if ship_grav is None or ship_grav <= 0:
            return None  # blocked: no thrust at all
        if body_grav is None:
            scaled = base_cost / ship_grav
        else:
            scaled = base_cost * body_grav / ship_grav
        # Floor at 1 OC for any successful maneuver
        cost = max(1, math.ceil(scaled))
        return self._effective_tu_cost(cost, efficiency)

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
        if system and system['star_name']:
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
                'ship_size': s['ship_size'] if 'ship_size' in s.keys() else s['hull_count'], 'hull_count': s['hull_count'],
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
            'move_efficiency': 100.0,       # movement-only efficiency (engines)
            'docked_at': ship['docked_at_base_id'],
            'orbiting': ship['orbiting_body_id'],
            'landed': ship['landed_body_id'] if 'landed_body_id' in ship.keys() else None,
            'landed_x': ship['landed_x'] if 'landed_x' in ship.keys() else 1,
            'landed_y': ship['landed_y'] if 'landed_y' in ship.keys() else 1,
            'start_col': ship['grid_col'],
            'start_row': ship['grid_row'],
            'start_tu': ship['tu_per_turn'],
            'efficiency': self._calc_efficiency(ship),
            'gravity_rating': ship['gravity_rating'] if 'gravity_rating' in ship.keys() else 1.0,
        }
        ship_size = ship['ship_size'] if 'ship_size' in ship.keys() else ship['hull_count']
        state['move_efficiency'] = self._calc_move_efficiency(ship_id, ship_size)

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
        cmd = COMMAND_ALIASES.get(order['command'], order['command'])
        params = order['params']
        tu_before = state['tu']

        if cmd == 'WAIT':
            return self._cmd_wait(state, params)
        elif cmd == 'MOVE':
            return self._cmd_move(state, params)
        elif cmd == 'SCANLOCATION':
            return self._cmd_location_scan(state, params, rng)
        elif cmd == 'SCANSYSTEM':
            return self._cmd_system_scan(state)
        elif cmd == 'ORBIT':
            return self._cmd_orbit(state, params)
        elif cmd == 'DOCK':
            return self._cmd_dock(state, params)
        elif cmd == 'UNDOCK':
            return self._cmd_undock(state)
        elif cmd == 'LEAVEORBIT':
            return self._cmd_leaveorbit(state)
        elif cmd == 'LAND':
            return self._cmd_land(state, params)
        elif cmd == 'TAKEOFF':
            return self._cmd_takeoff(state)
        elif cmd == 'SCANSURFACE':
            return self._cmd_surfacescan(state)
        elif cmd == 'BUY':
            return self._cmd_buy(state, params)
        elif cmd == 'SELL':
            return self._cmd_sell(state, params)
        elif cmd == 'LOADMAGAZINE':
            return self._cmd_loadmagazine(state, params)
        elif cmd == 'UNLOADMAGAZINE':
            return self._cmd_unloadmagazine(state, params)
        elif cmd == 'GETMARKET':
            return self._cmd_getmarket(state, params)
        elif cmd == 'JUMP':
            return self._cmd_jump(state, params)
        elif cmd == 'MESSAGE':
            return self._cmd_message(state, params)
        elif cmd == 'MAKEOFFICER':
            return self._cmd_makeofficer(state, params)
        elif cmd == 'INSTALL':
            return self._cmd_install(state, params)
        elif cmd == 'UNINSTALL':
            return self._cmd_uninstall(state, params)
        elif cmd == 'SCRAP':
            return self._cmd_scrap(state, params)
        elif cmd == 'RENAMESHIP':
            return self._cmd_renameship(state, params)
        elif cmd == 'RENAMEBASE':
            return self._cmd_renamebase(state, params)
        elif cmd == 'RENAMEPREFECT':
            return self._cmd_renameprefect(state, params)
        elif cmd == 'RENAMEOFFICER':
            return self._cmd_renameofficer(state, params)
        elif cmd == 'CHANGEFACTION':
            # CHANGEFACTION is now prefect-scoped. If it reaches here it
            # means it was somehow filed against a ship — treat as error.
            return {
                'command': 'CHANGEFACTION', 'params': params,
                'tu_before': tu_before, 'tu_after': tu_before,
                'tu_cost': 0, 'success': False,
                'message': 'CHANGEFACTION is a prefect-scoped order and cannot be filed against a ship. File it in a PREFECT block.'
            }
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
        elif cmd in ('TARGET', 'DEFEND', 'AVOID'):
            return self._cmd_combat_list(state, cmd, params)
        elif cmd == 'DOCTRINE':
            return self._cmd_doctrine(state, params)
        elif cmd in ('LOAD', 'UNLOAD'):
            return self._cmd_magazine_transfer(state, cmd, params)
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
                'message': f"Waiting complete (partial: {cost} of {tu_amount} OC)."
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
        if float(state.get('move_efficiency', 100.0) or 0.0) <= 0.0:
            return {
                'command': 'MOVE', 'params': f"{target_col}{target_row:02d}",
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False,
                'message': "Cannot move: no functioning engines installed. Order dropped."
            }

        cost_per_step = self._effective_move_step_cost(state)
        if cost_per_step is None:
            return {
                'command': 'MOVE', 'params': f"{target_col}{target_row:02d}",
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False,
                'message': "Cannot move: no functioning engines installed. Order dropped."
            }

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
                'message': f"Insufficient OC for move ({state['tu']} < {cost_per_step}). Order carries forward."
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

    def _cmd_location_scan(self, state, params, rng):
        """
        SCANLOCATION [N] - active scan: spend N OCs staring at nearby cells.
        Each OC of duration is an independent detection roll against every
        target currently within scan range (2 cells). Uses the same quadratic
        formula as passive detection. Must stay stationary during the scan.

        The ship cannot SCANLOCATION while performing MOVE/ORBIT/DOCK etc —
        those consume their own OCs and SCANLOCATION is a separate stationary
        activity that blocks other actions for its duration.

        When combat is added, an immediate hostile detection should interrupt
        the remaining scan OCs and trigger combat. For now, all detections
        are recorded with their tick number so future combat can replay them.
        """
        from engine.detection import (PASSIVE_SCAN_RANGE, grid_distance,
                                       try_detect)

        tu_before = state['tu']
        duration = 1
        if isinstance(params, dict) and 'duration' in params:
            duration = max(1, int(params['duration']))

        per_oc_cost = self._effective_tu_cost(TU_COSTS['SCANLOCATION'],
                                                state['efficiency'])

        # Active ship context (need this before OC deduction to verify sensors)
        ship_id = state['ship_id']
        system_id = state['system_id']
        col = state['col']
        row = state['row']
        orbit_body = state.get('orbiting') or state.get('landed')

        active_ship = self.conn.execute(
            "SELECT * FROM ships WHERE ship_id = ?", (ship_id,)
        ).fetchone()
        if not active_ship:
            return {
                'command': 'SCANLOCATION', 'params': {'duration': duration},
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': 'Active scan failed: scanner ship not found.'
            }
        scanner_rating = active_ship['sensor_rating'] or 0
        prefect_id = active_ship['owner_prefect_id']

        if scanner_rating <= 0:
            return {
                'command': 'SCANLOCATION', 'params': {'duration': duration},
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': 'Active scan failed: no sensor components installed.'
            }

        # Determine how many OCs we can actually afford
        affordable_ticks = min(duration, state['tu'] // per_oc_cost) if per_oc_cost > 0 else duration
        if affordable_ticks <= 0:
            return {
                'command': 'SCANLOCATION', 'params': {'duration': duration},
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False, 'tu_exhausted': True,
                'message': f"Insufficient OC for scan ({state['tu']} < {per_oc_cost}). Order carries forward."
            }

        actual_cost = affordable_ticks * per_oc_cost
        state['tu'] -= actual_cost
        truncated = affordable_ticks < duration

        game = self.get_game()
        turn_year = game['current_year']
        turn_week = game['current_week']

        # Per-target accumulator: first tick where we rolled a hit + target info
        # Keyed by (type, id). A target only gets ONE recorded detection per
        # scan session (the first successful roll); subsequent rolls against
        # the same target in the same session are redundant for reporting.
        first_hits = {}  # {(type, id): {'tick': N, 'detail': dict}}
        rolls_by_target = {}  # {(type, id): int} — how many rolls we made
        attempted_targets = set()  # unique targets we tried to detect at all

        def _try_ship(target, tick):
            if target['ship_id'] == ship_id:
                return
            dist = grid_distance(col, row, target['grid_col'], target['grid_row'])
            if dist > PASSIVE_SCAN_RANGE:
                return
            key = ('ship', target['ship_id'])
            attempted_targets.add(key)
            rolls_by_target[key] = rolls_by_target.get(key, 0) + 1
            if key in first_hits:
                return  # already detected this session
            target_profile = target['sensor_profile'] or (target['ship_size'] / 100.0 if target['ship_size'] else 0.5)
            spotted, _chance = try_detect(scanner_rating, target_profile, dist)
            if spotted:
                faction = self._get_faction(target['faction_id']) if 'faction_id' in target.keys() and target['faction_id'] else {'abbreviation': 'IND', 'faction_id': None}
                display_name = f"{faction['abbreviation']} {target['name']}"
                first_hits[key] = {
                    'tick': tick,
                    'type': 'ship',
                    'id': target['ship_id'],
                    'name': display_name,
                    'col': target['grid_col'], 'row': target['grid_row'],
                    'range': dist,
                    'ship_size': target['ship_size'],
                    'hull_type': target['hull_type'],
                    'faction_id': target['faction_id'] if 'faction_id' in target.keys() else None,
                }

        def _try_base(target, kind, tick, forced_dist=None):
            if forced_dist is not None:
                dist = forced_dist
            else:
                dist = grid_distance(col, row, target['grid_col'], target['grid_row'])
                if dist > PASSIVE_SCAN_RANGE:
                    return
            key = (kind, target['base_id'])
            attempted_targets.add(key)
            rolls_by_target[key] = rolls_by_target.get(key, 0) + 1
            if key in first_hits:
                return
            target_profile = target['sensor_profile'] or 1.0
            spotted, _chance = try_detect(scanner_rating, target_profile, dist)
            if spotted:
                # Display location: for orbital-detected surface installations
                # we want the body's grid position
                if forced_dist == 0 and kind in ('port', 'outpost') and orbit_body:
                    body_loc = self.conn.execute(
                        "SELECT grid_col, grid_row FROM celestial_bodies WHERE body_id = ?",
                        (orbit_body,)
                    ).fetchone()
                    loc_col = body_loc['grid_col'] if body_loc else col
                    loc_row = body_loc['grid_row'] if body_loc else row
                else:
                    loc_col = target['grid_col']
                    loc_row = target['grid_row']
                first_hits[key] = {
                    'tick': tick,
                    'type': kind,
                    'id': target['base_id'],
                    'name': target['name'],
                    'col': loc_col, 'row': loc_row,
                    'range': dist,
                    'hull_type': kind.title(),
                    'ship_size': None,
                }

        # --- Run N independent ticks ---
        for tick in range(1, affordable_ticks + 1):
            # Fetch current world state for this tick (reflects any updates
            # the interleaver has made to other ships between ticks)
            candidate_ships = self.conn.execute(
                """SELECT s.*, pp.faction_id
                   FROM ships s
                   JOIN prefects pp ON s.owner_prefect_id = pp.prefect_id
                   JOIN players p ON pp.player_id = p.player_id
                   WHERE s.system_id = ? AND s.game_id = ?
                     AND p.status = 'active'
                     AND s.ship_id != ?""",
                (system_id, self.game_id, ship_id)
            ).fetchall()

            candidate_starbases = self.conn.execute(
                "SELECT *, 'starbase' AS kind, base_id FROM starbases WHERE system_id = ? AND game_id = ?",
                (system_id, self.game_id)
            ).fetchall()

            for sh in candidate_ships:
                _try_ship(sh, tick)
            for sb in candidate_starbases:
                _try_base(sb, 'starbase', tick)

            # Surface installations only if orbiting/landed
            if orbit_body:
                ports = self.conn.execute(
                    "SELECT *, 'port' AS kind, port_id AS base_id FROM surface_ports WHERE body_id = ? AND game_id = ?",
                    (orbit_body, self.game_id)
                ).fetchall()
                outposts = self.conn.execute(
                    "SELECT *, 'outpost' AS kind, outpost_id AS base_id FROM outposts WHERE body_id = ? AND game_id = ?",
                    (orbit_body, self.game_id)
                ).fetchall()
                for p in ports:
                    _try_base(p, 'port', tick, forced_dist=0)
                for o in outposts:
                    _try_base(o, 'outpost', tick, forced_dist=0)

        # --- Persist detections to known_contacts ---
        for key, det in first_hits.items():
            existing = self.conn.execute("""
                SELECT contact_id FROM known_contacts
                WHERE prefect_id = ? AND object_type = ? AND object_id = ?
            """, (prefect_id, det['type'], det['id'])).fetchone()
            if existing:
                self.conn.execute("""
                    UPDATE known_contacts SET
                        location_col = ?, location_row = ?,
                        location_system = ?,
                        discovered_turn_year = ?, discovered_turn_week = ?,
                        target_faction_id = ?,
                        target_hull_type = ?,
                        target_ship_size = ?,
                        detection_range = ?,
                        detected_on_tick = ?,
                        detection_source = 'active'
                    WHERE contact_id = ?
                """, (det['col'], det['row'], system_id, turn_year, turn_week,
                      det.get('faction_id'), det.get('hull_type'),
                      det.get('ship_size'), det.get('range'),
                      det['tick'], existing['contact_id']))
            else:
                self.conn.execute("""
                    INSERT INTO known_contacts
                    (prefect_id, object_type, object_id, object_name,
                     location_system, location_col, location_row,
                     discovered_turn_year, discovered_turn_week,
                     target_faction_id, target_hull_type,
                     target_ship_size, detection_range,
                     detected_on_tick, detection_source, scanner_ship_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                """, (prefect_id, det['type'], det['id'], det['name'],
                      system_id, det['col'], det['row'],
                      turn_year, turn_week,
                      det.get('faction_id'), det.get('hull_type'),
                      det.get('ship_size'), det.get('range'),
                      det['tick'], ship_id))
        self.conn.commit()

        # --- Build result message ---
        loc_str = f"{col}{row:02d}"
        scan_note = f"SCANLOCATION ({affordable_ticks} OC at {loc_str} system {system_id})"
        if truncated:
            scan_note += f" [requested {duration}, truncated to available OC]"

        if first_hits:
            detections_sorted = sorted(first_hits.values(), key=lambda d: d['tick'])
            lines = [scan_note, "  Contacts detected:"]
            for det in detections_sorted:
                if det['type'] == 'ship':
                    size = f"Size {det['ship_size']} " if det['ship_size'] else ""
                    hull = f"{det['hull_type']} Hull " if det['hull_type'] else ""
                    loc = f"{det['col']}{det['row']:02d}"
                    lines.append(
                        f"    - {det['name']} ({det['id']}) {size}{hull}"
                        f"at {loc} (range {det['range']}, first hit on OC {det['tick']})"
                    )
                else:
                    loc = f"{det['col']}{det['row']:02d}"
                    kind_str = det['type'].title()
                    lines.append(
                        f"    - {kind_str} {det['name']} ({det['id']}) "
                        f"at {loc} (range {det['range']}, first hit on OC {det['tick']})"
                    )
            missed = len(attempted_targets) - len(first_hits)
            if missed > 0:
                lines.append(f"  ({missed} other target(s) rolled and missed)")
            message = "\n".join(lines)
        else:
            if attempted_targets:
                message = f"{scan_note}\n  No contacts detected ({len(attempted_targets)} target(s) rolled and missed)."
            else:
                message = f"{scan_note}\n  No targets within scan range."

        return {
            'command': 'SCANLOCATION', 'params': {'duration': affordable_ticks},
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': actual_cost,
            'success': True,
            'message': message,
            'detected': list(first_hits.values()),
        }

    def _cmd_system_scan(self, state):
        """SCANSYSTEM - produce full system map."""
        tu_before = state['tu']
        cost = self._effective_tu_cost(TU_COSTS['SCANSYSTEM'], state['efficiency'])

        if state['tu'] < cost:
            return {
                'command': 'SCANSYSTEM', 'params': None,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False, 'tu_exhausted': True,
                'message': f"Insufficient OC for system scan ({state['tu']} < {cost}). Order carries forward."
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

        # Build legend of detected objects below the map
        legend_lines = []

        # Star
        if system['star_name'] and system['star_grid_col']:
            legend_lines.append(f"  *  {system['star_name']} ({system['star_spectral_type']}) at "
                                f"{system['star_grid_col']}{system['star_grid_row']:02d}")
        elif not system['star_name']:
            legend_lines.append(f"  (Starless nexus)")

        # Build parent/child relationships and base lookup
        bodies_by_id = {}
        children_by_parent = {}
        top_level = []
        bases_by_body = {}

        for obj in objects:
            if obj['type'] in ('planet', 'moon', 'gas_giant', 'asteroid'):
                bodies_by_id[obj['id']] = obj
                # Look up parent
                body_row = self.conn.execute(
                    "SELECT parent_body_id FROM celestial_bodies WHERE body_id = ?",
                    (obj['id'],)
                ).fetchone()
                parent = body_row['parent_body_id'] if body_row else None
                if parent:
                    children_by_parent.setdefault(parent, []).append(obj)
                else:
                    top_level.append(obj)
            elif obj['type'] == 'base':
                # Look up orbiting body
                base_row = self.conn.execute(
                    "SELECT orbiting_body_id, base_type, docking_capacity FROM starbases WHERE base_id = ?",
                    (obj['id'],)
                ).fetchone()
                if base_row and base_row['orbiting_body_id']:
                    bases_by_body.setdefault(base_row['orbiting_body_id'], []).append({
                        'name': obj['name'], 'id': obj['id'],
                        'col': obj['col'], 'row': obj['row'],
                        'base_type': base_row['base_type'],
                        'docking': base_row['docking_capacity'],
                    })

        def format_body(obj, indent="  "):
            loc = f"{obj['col']}{obj['row']:02d}"
            type_label = obj['type'].replace('_', ' ').title()
            symbol = obj.get('symbol', '?')
            lines = [f"{indent}{symbol}  {obj['name']} ({obj['id']}) at {loc} - {type_label}"]
            # Starbases orbiting this body
            for base in bases_by_body.get(obj['id'], []):
                base_loc = f"{base['col']}{base['row']:02d}"
                lines.append(f"{indent}     [{base['base_type']}] {base['name']} ({base['id']}) at {base_loc}"
                             f" - Docking: {base['docking']}")
            # Moons
            for child in children_by_parent.get(obj['id'], []):
                lines.extend(format_body(child, "      "))
            return lines

        for obj in top_level:
            legend_lines.extend(format_body(obj))

        legend = "\n".join(legend_lines)
        full_output = f"System scan complete.\n{ascii_map}\n\n{legend}"

        return {
            'command': 'SCANSYSTEM', 'params': None,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': cost,
            'success': True,
            'message': full_output,
            'map': ascii_map
        }

    def _cmd_orbit(self, state, body_id):
        """ORBIT {body_id} - enter orbit of a celestial body."""
        tu_before = state['tu']

        # Check body exists and is at ship's location (need body before computing cost)
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

        # Gravity-adjusted cost: base × body_grav / ship_grav
        body_grav = body['gravity'] or 1.0
        cost = self._gravity_adjusted_cost(
            TU_COSTS['ORBIT'], state.get('gravity_rating', 1.0), body_grav, state['efficiency']
        )
        if cost is None:
            return {
                'command': 'ORBIT', 'params': body_id,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False,
                'message': "Unable to orbit: ship has no thrusters installed."
            }

        if state['tu'] < cost:
            return {
                'command': 'ORBIT', 'params': body_id,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False, 'tu_exhausted': True,
                'message': f"Insufficient OC for orbit ({state['tu']} < {cost}). Order carries forward."
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
                'message': f"Insufficient OC for docking ({state['tu']} < {cost}). Order carries forward."
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
                    lines.append(f"        {c['name']} ({c['id']}) - {{{c.get('ship_size', c.get('hull_count', '?'))} {c.get('hull_type', 'Hulls')}}}")
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
                'message': f"Insufficient OC to undock ({state['tu']} < {cost}). Order carries forward."
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

    def _cmd_leaveorbit(self, state):
        """LEAVEORBIT - leave orbit and return to the grid square. 0 OC."""
        tu_before = state['tu']

        if not state['orbiting']:
            return {
                'command': 'LEAVEORBIT', 'params': None,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0,
                'success': False,
                'message': "Ship is not in orbit."
            }

        body_id = state['orbiting']
        body = self.conn.execute(
            "SELECT name FROM celestial_bodies WHERE body_id = ?", (body_id,)
        ).fetchone()
        body_name = body['name'] if body else str(body_id)

        state['orbiting'] = None
        self._commit_ship_position(state)

        loc = f"{state['col']}{state['row']:02d}"
        return {
            'command': 'LEAVEORBIT', 'params': None,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': 0,
            'success': True,
            'message': f"Left orbit of {body_name} ({body_id}). Now at {loc} in open space."
        }

    def _cmd_land(self, state, params):
        """LAND <body_id> <x> <y> - land on a planet or moon surface at coordinates."""
        from engine.maps.surface_gen import get_or_generate_surface, TERRAIN_SYMBOLS
        tu_before = state['tu']

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

        # Gravity-adjusted cost: base × body_grav / ship_grav
        body_grav = body['gravity'] or 1.0
        cost = self._gravity_adjusted_cost(
            TU_COSTS['LAND'], state.get('gravity_rating', 1.0), body_grav, state['efficiency']
        )
        if cost is None:
            return {
                'command': 'LAND', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': "Cannot land: ship has no thrusters installed."
            }

        # Check TU
        if state['tu'] < cost:
            return {
                'command': 'LAND', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False, 'tu_exhausted': True,
                'message': f"Insufficient OC to land ({state['tu']} < {cost}). Order carries forward."
            }

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

        if not state['landed']:
            return {
                'command': 'TAKEOFF', 'params': None,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': "Cannot take off: ship is not landed on any surface."
            }

        body_id = state['landed']
        body = self.conn.execute(
            "SELECT * FROM celestial_bodies WHERE body_id = ?", (body_id,)
        ).fetchone()
        body_name = body['name'] if body else str(body_id)
        body_grav = (body['gravity'] if body and body['gravity'] else 1.0)

        # Gravity-adjusted cost: base × body_grav / ship_grav
        cost = self._gravity_adjusted_cost(
            TU_COSTS['TAKEOFF'], state.get('gravity_rating', 1.0), body_grav, state['efficiency']
        )
        if cost is None:
            return {
                'command': 'TAKEOFF', 'params': None,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': "Cannot take off: ship has no thrusters installed."
            }

        if state['tu'] < cost:
            return {
                'command': 'TAKEOFF', 'params': None,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False, 'tu_exhausted': True,
                'message': f"Insufficient OC to take off ({state['tu']} < {cost}). Order carries forward."
            }

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
        """SCANSURFACE - produce a terrain map of the planet the ship is orbiting or landed on."""
        from engine.maps.surface_gen import get_or_generate_surface, render_surface_map
        tu_before = state['tu']
        cost = self._effective_tu_cost(TU_COSTS['SCANSURFACE'], state['efficiency'])

        # Determine which body to scan - landed takes priority, then orbiting
        body_id = state['landed'] or state['orbiting']
        if not body_id:
            return {
                'command': 'SCANSURFACE', 'params': None,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': "Cannot scan surface: ship must be orbiting or landed on a planet or moon."
            }

        if state['tu'] < cost:
            return {
                'command': 'SCANSURFACE', 'params': None,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False, 'tu_exhausted': True,
                'message': f"Insufficient OC for surface scan ({state['tu']} < {cost}). Order carries forward."
            }

        body = self.conn.execute(
            "SELECT * FROM celestial_bodies WHERE body_id = ?", (body_id,)
        ).fetchone()
        if not body:
            return {
                'command': 'SCANSURFACE', 'params': None,
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
            'command': 'SCANSURFACE', 'params': None,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': cost, 'success': True,
            'message': message,
        }

    def _cmd_buy(self, state, params):
        """BUY <base_id> <item_id> <quantity> [INSTALL|MAGAZINE] - buy items from base market."""
        tu_before = state['tu']
        base_id = params['base_id']
        item_id = params['item_id']
        quantity = params['quantity']
        install = params.get('install', False)
        magazine = params.get('magazine', False)
        install_str = " INSTALL" if install else (" MAGAZINE" if magazine else "")
        params_str = f"{base_id} {item_id} {quantity}{install_str}"

        # Must be docked at this base
        if state['docked_at'] != base_id:
            return {
                'command': 'BUY', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot buy: ship is not docked at base {base_id}."
            }

        # Check if item_id is a ship component
        component = self.conn.execute(
            "SELECT * FROM ship_components WHERE component_id = ?", (item_id,)
        ).fetchone()
        if component:
            return self._cmd_buy_component(state, params_str, base_id, component, quantity, install)

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

        # MAGAZINE flag: validate the item is missile/torpedo and the ship
        # has a matching magazine with free space. Rejects cleanly otherwise.
        MISSILE_ITEM_ID = 501
        TORPEDO_ITEM_ID = 502
        if magazine:
            if item_id not in (MISSILE_ITEM_ID, TORPEDO_ITEM_ID):
                return {
                    'command': 'BUY', 'params': params_str,
                    'tu_before': tu_before, 'tu_after': state['tu'],
                    'tu_cost': 0, 'success': False,
                    'message': (f"Cannot BUY MAGAZINE: {item['name']} is not missile/torpedo ammo.")
                }

        # Cap to available credits (skip for unlimited_credits prefects)
        ship = self.get_ship(state['ship_id'])
        prefect = self.conn.execute(
            "SELECT * FROM prefects WHERE prefect_id = ?",
            (ship['owner_prefect_id'],)
        ).fetchone()
        has_unlimited = prefect['unlimited_credits'] if 'unlimited_credits' in prefect.keys() else 0
        if buy_price > 0 and not has_unlimited:
            max_by_credits = int(prefect['credits'] // buy_price)
            actual_qty = min(actual_qty, max_by_credits)

        # Cap to available storage space. When magazine=True and item is
        # ammo, cap by magazine free space. Otherwise cap by cargo space.
        if magazine:
            if item_id == MISSILE_ITEM_ID:
                mag_free = (ship['max_missiles'] or 0) - (ship['missiles_loaded'] or 0)
            else:  # torpedo
                mag_free = (ship['max_torpedoes'] or 0) - (ship['torpedoes_loaded'] or 0)
            actual_qty = min(actual_qty, max(0, mag_free))
        else:
            # Cap to available cargo space (crew don't take cargo space)
            available_mu = ship['cargo_capacity'] - ship['cargo_used']
            if item['mass_per_unit'] > 0 and item_id != CREW_ITEM_ID:
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
            if magazine:
                if item_id == MISSILE_ITEM_ID:
                    extra_info += f", missile magazine={ship['missiles_loaded']}/{ship['max_missiles']}"
                else:
                    extra_info += f", torpedo magazine={ship['torpedoes_loaded']}/{ship['max_torpedoes']}"
                space_info = ""
            else:
                space_info = f", cargo space={ship['cargo_capacity'] - ship['cargo_used']} ST free"
            return {
                'command': 'BUY', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': (f"Cannot buy any {item['name']}: "
                            f"stock={available_stock}, credits={prefect['credits']:,.0f} cr"
                            f"{space_info}{extra_info}.")
            }

        # Build capped message
        cap_reasons = []
        if actual_qty < quantity:
            if available_stock < quantity:
                cap_reasons.append(f"stock={available_stock}")
            if buy_price > 0 and not has_unlimited and int(prefect['credits'] // buy_price) < quantity:
                cap_reasons.append(f"credits={prefect['credits']:,.0f} cr")
            if magazine:
                if item_id == MISSILE_ITEM_ID:
                    mag_free = (ship['max_missiles'] or 0) - (ship['missiles_loaded'] or 0)
                    if mag_free < quantity:
                        cap_reasons.append(f"missile magazine free={mag_free}")
                elif item_id == TORPEDO_ITEM_ID:
                    mag_free = (ship['max_torpedoes'] or 0) - (ship['torpedoes_loaded'] or 0)
                    if mag_free < quantity:
                        cap_reasons.append(f"torpedo magazine free={mag_free}")
            else:
                available_mu = ship['cargo_capacity'] - ship['cargo_used']
                if item['mass_per_unit'] > 0 and item_id != CREW_ITEM_ID and int(available_mu // item['mass_per_unit']) < quantity:
                    cap_reasons.append(f"cargo={available_mu} ST free")
            if item_id == CREW_ITEM_ID:
                current_crew = self._get_crew_count(state['ship_id'])
                life_support = ship['life_support_capacity'] if ship['life_support_capacity'] else 20
                if life_support - current_crew < quantity:
                    cap_reasons.append(f"life support={current_crew}/{life_support}")
            capped_msg = f" (capped from {quantity} to {actual_qty}: {', '.join(cap_reasons)})"
        else:
            capped_msg = ""

        total_cost_cr = buy_price * actual_qty
        # Crew don't occupy cargo space - they use life support capacity instead
        # Magazine ammo bypasses cargo space entirely (stored in magazine)
        if magazine or item_id == CREW_ITEM_ID:
            total_mass = 0
        else:
            total_mass = item['mass_per_unit'] * actual_qty

        # Execute purchase
        if not has_unlimited:
            self.conn.execute(
                "UPDATE prefects SET credits = credits - ? WHERE prefect_id = ?",
                (total_cost_cr, prefect['prefect_id'])
            )
        if total_mass > 0:
            self.conn.execute(
                "UPDATE ships SET cargo_used = cargo_used + ? WHERE ship_id = ?",
                (total_mass, state['ship_id'])
            )

        # Decrement base stock
        self.conn.execute("""
            UPDATE market_prices SET stock = stock - ?
            WHERE price_id = ?
        """, (actual_qty, price_row['price_id']))

        # Add to destination: magazine or cargo
        if magazine:
            if item_id == MISSILE_ITEM_ID:
                self.conn.execute(
                    "UPDATE ships SET missiles_loaded = missiles_loaded + ? "
                    "WHERE ship_id = ?",
                    (actual_qty, state['ship_id'])
                )
                dest_str = "missile magazine"
            else:  # torpedo
                self.conn.execute(
                    "UPDATE ships SET torpedoes_loaded = torpedoes_loaded + ? "
                    "WHERE ship_id = ?",
                    (actual_qty, state['ship_id'])
                )
                dest_str = "torpedo magazine"
        else:
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
                stored_mass = 0 if item_id == CREW_ITEM_ID else item['mass_per_unit']
                self.conn.execute("""
                    INSERT INTO cargo_items (ship_id, item_type_id, item_name, quantity, mass_per_unit)
                    VALUES (?, ?, ?, ?, ?)
                """, (state['ship_id'], item_id, item['name'], actual_qty, stored_mass))
            dest_str = "cargo"

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
                        f"from {base_name} ({base_id}) -> {dest_str}. "
                        f"[{total_mass} ST]{capped_msg}")
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
        # Crew don't occupy cargo space - use stored mass_per_unit (0 for crew)
        total_mass = cargo['mass_per_unit'] * actual_qty

        # Execute sale
        ship = self.get_ship(state['ship_id'])
        self.conn.execute(
            "UPDATE prefects SET credits = credits + ? WHERE prefect_id = ?",
            (total_income, ship['owner_prefect_id'])
        )
        if total_mass > 0:
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
                        f"to {base_name} ({base_id}). [{total_mass} ST freed]{capped_msg}{crew_warning}")
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

        role_labels = {'produces': 'Supply', 'average': 'Std', 'demands': 'Demand'}
        lines = [f"Market at {base['name']} ({base_id}):"]
        fmt = "  {:<26s} {:>6s} {:>5s} {:>5s} {:>5s} {:>5s} {}"
        lines.append(fmt.format('Item', 'ID', 'Buy', 'Sell', 'Stk', 'Dmd', 'Role'))
        lines.append(fmt.format('-'*26, '-'*6, '-'*5, '-'*5, '-'*5, '-'*5, '------'))
        for p in prices:
            role_str = role_labels.get(p['trade_role'], '')
            name = p['item_name'][:26]
            lines.append(fmt.format(
                name, str(p['item_id']),
                f"{p['buy_price']:.0f}", f"{p['sell_price']:.0f}",
                str(p['stock']), str(p['demand']), role_str
            ))
        lines.append(f"  {refresh_msg}")

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
        min_star_dist = JUMP_CONFIG['min_star_distance']
        params_str = str(target_system_id)

        # Check for installed jump drive
        drive = self.conn.execute("""
            SELECT sc.jump_range, sc.jump_oc_cost, sc.name, ii.quantity
            FROM installed_items ii
            JOIN ship_components sc ON ii.component_id = sc.component_id
            WHERE ii.ship_id = ? AND sc.category = 'jump_drive'
            ORDER BY sc.jump_range DESC
            LIMIT 1
        """, (state['ship_id'],)).fetchone()

        if not drive:
            return {
                'command': 'JUMP', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': "Cannot jump: no jump drive installed."
            }

        max_range = drive['jump_range']
        base_oc_cost = drive['jump_oc_cost']

        # Find shortest route (BFS unlimited — cost check handles affordability)
        jump_hops = self._find_jump_route(state['system_id'], target_system_id, max_hops=100)

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

        # Check distance from primary star (skip for starless nexus systems)
        current_system = self.conn.execute(
            "SELECT * FROM star_systems WHERE system_id = ?", (state['system_id'],)
        ).fetchone()
        if current_system and current_system['star_name']:
            star_col = current_system['star_grid_col']
            star_row = current_system['star_grid_row']
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
            return {
                'command': 'JUMP', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot jump: no known hyperspace route to {target['name']} ({target_system_id})."
            }

        # Cost = number of drive activations × drive OC cost
        # Each activation covers up to jump_range hops
        import math
        activations = math.ceil(jump_hops / max_range)
        base_total = base_oc_cost * activations
        total_cost = self._effective_tu_cost(base_total, state['efficiency'])

        # Check TU
        if state['tu'] < total_cost:
            return {
                'command': 'JUMP', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False, 'tu_exhausted': True,
                'message': (f"Insufficient OC for jump ({state['tu']} < {total_cost}). "
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
        hop_str = f" ({jump_hops} hops, {activations}x drive)" if jump_hops > 1 else ""
        return {
            'command': 'JUMP', 'params': params_str,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': total_cost, 'success': True,
            'message': (f"Hyperspace jump from {origin_name} ({origin_id}) "
                        f"to {target['name']} ({target_system_id}){hop_str}. "
                        f"Arrived at {arrival_loc}. [{total_cost} OC]")
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
                'message': f"Insufficient OC for officer promotion ({state['tu']} < {cost}). Order carries forward."
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

        # Crew don't occupy cargo space, so no cargo_used update needed

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
                        f"[{next_cn}] assigned. [{cost} OC]")
        }

    def _cmd_buy_component(self, state, params_str, base_id, component, quantity, install):
        """Buy a ship component from a starbase at catalogue base_price."""
        tu_before = state['tu']
        comp_id = component['component_id']
        comp_name = component['name']
        price_each = component['base_price']
        st_cost_each = component['st_cost']
        total_cost = price_each * quantity

        ship = self.get_ship(state['ship_id'])
        prefect = self.conn.execute(
            "SELECT * FROM prefects WHERE prefect_id = ?",
            (ship['owner_prefect_id'],)
        ).fetchone()

        # Check hull restriction
        if component['hull_restriction'] and ship['hull_type'].lower() != component['hull_restriction'].lower():
            return {
                'command': 'BUY', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot buy {comp_name}: requires {component['hull_restriction']} hull (ship is {ship['hull_type']})."
            }

        # Check credits (skip for unlimited_credits prefects)
        has_unlimited = prefect['unlimited_credits'] if 'unlimited_credits' in prefect.keys() else 0
        if not has_unlimited and prefect['credits'] < total_cost:
            max_afford = int(prefect['credits'] // price_each) if price_each > 0 else 0
            if max_afford <= 0:
                return {
                    'command': 'BUY', 'params': params_str,
                    'tu_before': tu_before, 'tu_after': state['tu'],
                    'tu_cost': 0, 'success': False,
                    'message': f"Cannot buy {comp_name}: costs {price_each} cr each, only {prefect['credits']:.0f} cr available."
                }
            quantity = max_afford
            total_cost = price_each * quantity

        if install:
            # Install directly — check ST capacity
            from db.database import get_ship_st_used, get_ship_st_capacity
            st_used = get_ship_st_used(self.conn, state['ship_id'])
            st_cap = get_ship_st_capacity(self.conn, state['ship_id'])
            st_needed = st_cost_each * quantity
            st_free = st_cap - st_used

            if st_needed > st_free:
                max_fit = st_free // st_cost_each if st_cost_each > 0 else 0
                if max_fit <= 0:
                    return {
                        'command': 'BUY', 'params': params_str,
                        'tu_before': tu_before, 'tu_after': state['tu'],
                        'tu_cost': 0, 'success': False,
                        'message': f"Cannot buy+install {comp_name}: {st_needed} ST needed, only {st_free} ST free."
                    }
                quantity = max_fit
                total_cost = price_each * quantity

            # Deduct credits (skip for unlimited)
            if not has_unlimited:
                self.conn.execute(
                    "UPDATE prefects SET credits = credits - ? WHERE prefect_id = ?",
                    (total_cost, prefect['prefect_id'])
                )

            # Install component
            existing = self.conn.execute(
                "SELECT * FROM installed_items WHERE ship_id = ? AND component_id = ?",
                (state['ship_id'], comp_id)
            ).fetchone()
            if existing:
                self.conn.execute(
                    "UPDATE installed_items SET quantity = quantity + ? WHERE item_install_id = ?",
                    (quantity, existing['item_install_id'])
                )
            else:
                self.conn.execute(
                    "INSERT INTO installed_items (ship_id, component_id, quantity) VALUES (?, ?, ?)",
                    (state['ship_id'], comp_id, quantity)
                )

            # Recalculate ship stats
            from db.database import recalculate_ship_stats
            recalculate_ship_stats(self.conn, state['ship_id'])
            self.conn.commit()

            return {
                'command': 'BUY', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': True,
                'message': (f"Bought and installed {quantity}x {comp_name} [{comp_id}] "
                            f"from base {base_id} for {total_cost} cr ({price_each} cr each). "
                            f"[{st_cost_each * quantity} ST installed]")
            }
        else:
            # Put in cargo — components take st_cost as cargo mass
            cargo_mass = st_cost_each
            total_mass = cargo_mass * quantity
            available = ship['cargo_capacity'] - ship['cargo_used']
            if total_mass > available:
                max_fit = available // cargo_mass if cargo_mass > 0 else 0
                if max_fit <= 0:
                    return {
                        'command': 'BUY', 'params': params_str,
                        'tu_before': tu_before, 'tu_after': state['tu'],
                        'tu_cost': 0, 'success': False,
                        'message': f"Cannot buy {comp_name}: {total_mass} ST cargo needed, only {available} ST free."
                    }
                quantity = max_fit
                total_cost = price_each * quantity
                total_mass = cargo_mass * quantity

            # Deduct credits (skip for unlimited)
            if not has_unlimited:
                self.conn.execute(
                    "UPDATE prefects SET credits = credits - ? WHERE prefect_id = ?",
                    (total_cost, prefect['prefect_id'])
                )

            # Add to cargo
            self.conn.execute(
                "UPDATE ships SET cargo_used = cargo_used + ? WHERE ship_id = ?",
                (total_mass, state['ship_id'])
            )
            existing = self.conn.execute(
                "SELECT * FROM cargo_items WHERE ship_id = ? AND item_type_id = ?",
                (state['ship_id'], comp_id)
            ).fetchone()
            if existing:
                self.conn.execute(
                    "UPDATE cargo_items SET quantity = quantity + ? WHERE cargo_id = ?",
                    (quantity, existing['cargo_id'])
                )
            else:
                self.conn.execute("""
                    INSERT INTO cargo_items (ship_id, item_type_id, item_name, quantity, mass_per_unit)
                    VALUES (?, ?, ?, ?, ?)
                """, (state['ship_id'], comp_id, comp_name, quantity, cargo_mass))

            self.conn.commit()
            return {
                'command': 'BUY', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': True,
                'message': (f"Bought {quantity}x {comp_name} [{comp_id}] "
                            f"from base {base_id} for {total_cost} cr ({price_each} cr each). "
                            f"[{total_mass} ST cargo]")
            }

    def _cmd_install(self, state, params):
        """INSTALL <component_id> [qty] - install a component from cargo."""
        tu_before = state['tu']
        comp_id = params['component_id']
        quantity = params.get('quantity', 1)
        cost = self._effective_tu_cost(TU_COSTS['INSTALL'], state['efficiency'])
        params_str = f"{comp_id} {quantity}"

        if state['tu'] < cost:
            return {
                'command': 'INSTALL', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Insufficient OC for install ({state['tu']} < {cost}). Order carries forward."
            }

        # Look up component
        component = self.conn.execute(
            "SELECT * FROM ship_components WHERE component_id = ?", (comp_id,)
        ).fetchone()
        if not component:
            return {
                'command': 'INSTALL', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Component {comp_id} not found in catalogue. Order dropped."
            }

        # Check hull restriction
        ship = self.get_ship(state['ship_id'])
        if component['hull_restriction'] and ship['hull_type'].lower() != component['hull_restriction'].lower():
            return {
                'command': 'INSTALL', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot install {component['name']}: requires {component['hull_restriction']} hull. Order dropped."
            }

        # Check cargo has the component
        cargo = self.conn.execute(
            "SELECT * FROM cargo_items WHERE ship_id = ? AND item_type_id = ?",
            (state['ship_id'], comp_id)
        ).fetchone()
        if not cargo or cargo['quantity'] < quantity:
            avail = cargo['quantity'] if cargo else 0
            return {
                'command': 'INSTALL', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot install: only {avail}x {component['name']} in cargo (need {quantity}). Order dropped."
            }

        # Check ST capacity
        from db.database import get_ship_st_used, get_ship_st_capacity
        st_used = get_ship_st_used(self.conn, state['ship_id'])
        st_cap = get_ship_st_capacity(self.conn, state['ship_id'])
        st_needed = component['st_cost'] * quantity
        st_free = st_cap - st_used
        if st_needed > st_free:
            return {
                'command': 'INSTALL', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': (f"Cannot install {quantity}x {component['name']}: "
                            f"{st_needed} ST needed, only {st_free} ST free. Order dropped.")
            }

        # Deduct TU
        state['tu'] -= cost

        # Remove from cargo
        cargo_mass = cargo['mass_per_unit'] * quantity
        if cargo['quantity'] == quantity:
            self.conn.execute("DELETE FROM cargo_items WHERE cargo_id = ?", (cargo['cargo_id'],))
        else:
            self.conn.execute(
                "UPDATE cargo_items SET quantity = quantity - ? WHERE cargo_id = ?",
                (quantity, cargo['cargo_id'])
            )
        self.conn.execute(
            "UPDATE ships SET cargo_used = MAX(0, cargo_used - ?) WHERE ship_id = ?",
            (cargo_mass, state['ship_id'])
        )

        # Add to installed
        existing = self.conn.execute(
            "SELECT * FROM installed_items WHERE ship_id = ? AND component_id = ?",
            (state['ship_id'], comp_id)
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE installed_items SET quantity = quantity + ? WHERE item_install_id = ?",
                (quantity, existing['item_install_id'])
            )
        else:
            self.conn.execute(
                "INSERT INTO installed_items (ship_id, component_id, quantity) VALUES (?, ?, ?)",
                (state['ship_id'], comp_id, quantity)
            )

        # Recalculate ship stats
        from db.database import recalculate_ship_stats
        recalculate_ship_stats(self.conn, state['ship_id'])
        self.conn.commit()

        return {
            'command': 'INSTALL', 'params': params_str,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': cost, 'success': True,
            'message': (f"Installed {quantity}x {component['name']} [{comp_id}]. "
                        f"[{st_needed} ST, {cost} OC]")
        }

    def _cmd_uninstall(self, state, params):
        """UNINSTALL <component_id> [qty] - uninstall a component to cargo."""
        tu_before = state['tu']
        comp_id = params['component_id']
        quantity = params.get('quantity', 1)
        cost = self._effective_tu_cost(TU_COSTS['UNINSTALL'], state['efficiency'])
        params_str = f"{comp_id} {quantity}"

        if state['tu'] < cost:
            return {
                'command': 'UNINSTALL', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Insufficient OC for uninstall ({state['tu']} < {cost}). Order carries forward."
            }

        # Look up component
        component = self.conn.execute(
            "SELECT * FROM ship_components WHERE component_id = ?", (comp_id,)
        ).fetchone()
        if not component:
            return {
                'command': 'UNINSTALL', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Component {comp_id} not found. Order dropped."
            }

        # Check component is installed
        installed = self.conn.execute(
            "SELECT * FROM installed_items WHERE ship_id = ? AND component_id = ?",
            (state['ship_id'], comp_id)
        ).fetchone()
        if not installed or installed['quantity'] < quantity:
            avail = installed['quantity'] if installed else 0
            return {
                'command': 'UNINSTALL', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot uninstall: only {avail}x {component['name']} installed (need {quantity}). Order dropped."
            }

        # Check cargo capacity for the uninstalled component
        ship = self.get_ship(state['ship_id'])
        cargo_mass = component['st_cost'] * quantity
        available_cargo = ship['cargo_capacity'] - ship['cargo_used']
        if cargo_mass > available_cargo:
            return {
                'command': 'UNINSTALL', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': (f"Cannot uninstall {quantity}x {component['name']}: "
                            f"{cargo_mass} ST cargo needed, only {available_cargo} ST free. Order dropped.")
            }

        # Deduct TU
        state['tu'] -= cost

        # Remove from installed
        if installed['quantity'] == quantity:
            self.conn.execute(
                "DELETE FROM installed_items WHERE item_install_id = ?",
                (installed['item_install_id'],)
            )
        else:
            self.conn.execute(
                "UPDATE installed_items SET quantity = quantity - ? WHERE item_install_id = ?",
                (quantity, installed['item_install_id'])
            )

        # Add to cargo
        self.conn.execute(
            "UPDATE ships SET cargo_used = cargo_used + ? WHERE ship_id = ?",
            (cargo_mass, state['ship_id'])
        )
        existing_cargo = self.conn.execute(
            "SELECT * FROM cargo_items WHERE ship_id = ? AND item_type_id = ?",
            (state['ship_id'], comp_id)
        ).fetchone()
        if existing_cargo:
            self.conn.execute(
                "UPDATE cargo_items SET quantity = quantity + ? WHERE cargo_id = ?",
                (quantity, existing_cargo['cargo_id'])
            )
        else:
            self.conn.execute("""
                INSERT INTO cargo_items (ship_id, item_type_id, item_name, quantity, mass_per_unit)
                VALUES (?, ?, ?, ?, ?)
            """, (state['ship_id'], comp_id, component['name'], quantity, component['st_cost']))

        # Recalculate ship stats (cargo_capacity may decrease!)
        from db.database import recalculate_ship_stats
        recalculate_ship_stats(self.conn, state['ship_id'])
        self.conn.commit()

        return {
            'command': 'UNINSTALL', 'params': params_str,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': cost, 'success': True,
            'message': (f"Uninstalled {quantity}x {component['name']} [{comp_id}] to cargo. "
                        f"[{cargo_mass} ST cargo, {cost} OC]")
        }

    def _cmd_scrap(self, state, params):
        """SCRAP <component_id> [qty] - scrap a component from cargo."""
        tu_before = state['tu']
        comp_id = params['component_id']
        quantity = params.get('quantity', 1)
        params_str = f"{comp_id} {quantity}"

        # Look up component
        component = self.conn.execute(
            "SELECT * FROM ship_components WHERE component_id = ?", (comp_id,)
        ).fetchone()
        if not component:
            return {
                'command': 'SCRAP', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Component {comp_id} not found. Order dropped."
            }

        # Check cargo
        cargo = self.conn.execute(
            "SELECT * FROM cargo_items WHERE ship_id = ? AND item_type_id = ?",
            (state['ship_id'], comp_id)
        ).fetchone()
        if not cargo or cargo['quantity'] < quantity:
            avail = cargo['quantity'] if cargo else 0
            return {
                'command': 'SCRAP', 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"Cannot scrap: only {avail}x {component['name']} in cargo (need {quantity}). Order dropped."
            }

        # Remove from cargo
        cargo_mass = cargo['mass_per_unit'] * quantity
        if cargo['quantity'] == quantity:
            self.conn.execute("DELETE FROM cargo_items WHERE cargo_id = ?", (cargo['cargo_id'],))
        else:
            self.conn.execute(
                "UPDATE cargo_items SET quantity = quantity - ? WHERE cargo_id = ?",
                (quantity, cargo['cargo_id'])
            )
        self.conn.execute(
            "UPDATE ships SET cargo_used = MAX(0, cargo_used - ?) WHERE ship_id = ?",
            (cargo_mass, state['ship_id'])
        )
        self.conn.commit()

        return {
            'command': 'SCRAP', 'params': params_str,
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': 0, 'success': True,
            'message': f"Scrapped {quantity}x {component['name']} [{comp_id}]. [{cargo_mass} ST freed]"
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
        """CHANGEFACTION in a ship context is no longer valid — redirect.

        CHANGEFACTION became a prefect-scoped order. If one ever reaches the
        ship dispatcher it was either mis-filed or is old data; drop it with
        a clear message rather than crashing.
        """
        tu_before = state['tu']
        return {
            'command': 'CHANGEFACTION',
            'params': str(params.get('faction_id', '')),
            'tu_before': tu_before, 'tu_after': state['tu'],
            'tu_cost': 0, 'success': False,
            'message': ("CHANGEFACTION is now a prefect-scoped order. "
                        "File it in a PREFECT block, not a SHIP block.")
        }

    def _process_changefaction(self, prefect_id, target_faction_id, reason):
        """Shared CHANGEFACTION logic. Returns (success, message) tuple.

        Writes a pending faction_requests row if the request is valid and
        not already duplicated. Does NOT auto-hold — that is done by the
        run-turn driver after this method returns.
        """
        prefect = self.conn.execute(
            "SELECT * FROM prefects WHERE prefect_id = ?",
            (prefect_id,)
        ).fetchone()
        if not prefect:
            return False, f"Prefect {prefect_id} not found."
        current_faction_id = prefect['faction_id']

        target_faction = self.conn.execute(
            "SELECT * FROM universe.factions WHERE faction_id = ?",
            (target_faction_id,)
        ).fetchone()
        if not target_faction:
            return False, (f"Cannot request faction change: faction "
                           f"{target_faction_id} not found. Order dropped.")

        if current_faction_id == target_faction_id:
            return False, (f"Already a member of {target_faction['name']} "
                           f"({target_faction['abbreviation']}). Order dropped.")

        # Don't duplicate a pending request
        existing = self.conn.execute(
            "SELECT request_id FROM faction_requests "
            "WHERE game_id = ? AND prefect_id = ? AND status = 'pending'",
            (self.game_id, prefect_id)
        ).fetchone()
        if existing:
            return False, (f"Request already pending (#{existing['request_id']}). "
                           f"Order dropped.")

        # Don't recreate if GM already actioned one for this prefect+target this turn
        game = self.get_game()
        already_actioned = self.conn.execute(
            "SELECT request_id, status FROM faction_requests "
            "WHERE game_id = ? AND prefect_id = ? AND target_faction_id = ? "
            "AND status IN ('approved', 'denied', 'completed') "
            "AND requested_turn_year = ? AND requested_turn_week = ?",
            (self.game_id, prefect_id, target_faction_id,
             game['current_year'], game['current_week'])
        ).fetchone()
        if already_actioned:
            return False, (f"Faction change for this turn was already "
                           f"{already_actioned['status']} "
                           f"(request #{already_actioned['request_id']}). Order dropped.")

        # File the request
        self.conn.execute("""
            INSERT INTO faction_requests
            (game_id, prefect_id, current_faction_id, target_faction_id,
             reason, status, requested_turn_year, requested_turn_week)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        """, (self.game_id, prefect_id, current_faction_id,
              target_faction_id, reason,
              game['current_year'], game['current_week']))
        self.conn.commit()

        current_faction = self.conn.execute(
            "SELECT abbreviation FROM universe.factions WHERE faction_id = ?",
            (current_faction_id,)
        ).fetchone()
        cur_abbr = current_faction['abbreviation'] if current_faction else '?'
        reason_str = f' Reason: "{reason}"' if reason else ''
        return True, (f"Faction change requested: {cur_abbr} -> "
                      f"{target_faction['abbreviation']} "
                      f"({target_faction['name']}).{reason_str} "
                      f"Awaiting GM approval.")

    def resolve_prefect_orders(self, prefect_orders_map):
        """Resolve all prefect-scoped orders (CHANGEFACTION, prefect MODERATOR).

        prefect_orders_map: dict of {prefect_id: [order_dicts]}
        Returns: dict of {prefect_id: [result_dicts]}
        """
        results = {}
        for prefect_id, orders in prefect_orders_map.items():
            prefect_results = []
            for order in orders:
                cmd = order['command']
                params = order.get('params') or {}

                if cmd == 'CHANGEFACTION':
                    target = params.get('faction_id')
                    reason = params.get('reason', '')
                    success, message = self._process_changefaction(
                        prefect_id, target, reason)
                    prefect_results.append({
                        'command': 'CHANGEFACTION',
                        'params': f"{target}" + (f" {reason}" if reason else ''),
                        'success': success,
                        'message': message,
                    })
                elif cmd == 'MODERATOR':
                    # Prefect-scoped MODERATOR: creates a moderator_action with
                    # ship_id = NULL-equivalent. Schema requires ship_id so we
                    # store 0 as a sentinel; the Phase 1.1 check uses prefect_id.
                    text = params.get('text', '')
                    prefect_results.append({
                        'command': 'MODERATOR',
                        'params': text,
                        'success': True,
                        'message': f"Moderator request filed: {text}",
                    })
                else:
                    prefect_results.append({
                        'command': cmd, 'params': str(params),
                        'success': False,
                        'message': f"Unknown or unsupported prefect order: {cmd}",
                    })
            results[prefect_id] = prefect_results
        return results

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

    def _cmd_combat_list(self, state, list_type, params):
        """
        TARGET/DEFEND/AVOID — manage combat lists (free, no OC cost).
        list_type is the command name (TARGET/DEFEND/AVOID), lowercased to
        match the table column.
        params: {'op': 'add'|'remove'|'clear', 'type': 'ship'|'base'|'faction', 'id': int}
        """
        tu_before = state['tu']
        ship_id = state['ship_id']
        list_kind = list_type.lower()  # 'target' | 'defend' | 'avoid'

        op = params.get('op')
        entry_type = params.get('type')
        entry_id = params.get('id')
        game = self.get_game()

        params_str = f"{op.upper()}"
        if entry_type and entry_id:
            params_str += f" {entry_type} {entry_id}"

        if op == 'clear':
            n = self.conn.execute(
                "SELECT COUNT(*) AS n FROM ship_combat_lists "
                "WHERE game_id = ? AND ship_id = ? AND list_type = ?",
                (self.game_id, ship_id, list_kind)
            ).fetchone()['n']
            self.conn.execute(
                "DELETE FROM ship_combat_lists "
                "WHERE game_id = ? AND ship_id = ? AND list_type = ?",
                (self.game_id, ship_id, list_kind)
            )
            self.conn.commit()
            return {
                'command': list_type, 'params': params_str,
                'tu_before': tu_before, 'tu_after': tu_before,
                'tu_cost': 0, 'success': True,
                'message': f"{list_type} list cleared ({n} entr{'y' if n == 1 else 'ies'} removed)."
            }

        # Validate entry exists where appropriate
        if entry_type == 'ship':
            row = self.conn.execute(
                "SELECT name FROM ships WHERE ship_id = ? AND game_id = ?",
                (entry_id, self.game_id)
            ).fetchone()
            if not row:
                return {
                    'command': list_type, 'params': params_str,
                    'tu_before': tu_before, 'tu_after': tu_before,
                    'tu_cost': 0, 'success': False,
                    'message': f"{list_type} {op}: ship {entry_id} not found."
                }
            entity_name = row['name']
        elif entry_type == 'faction':
            row = self.conn.execute(
                "SELECT name, abbreviation FROM universe.factions WHERE faction_id = ?",
                (entry_id,)
            ).fetchone()
            if not row:
                return {
                    'command': list_type, 'params': params_str,
                    'tu_before': tu_before, 'tu_after': tu_before,
                    'tu_cost': 0, 'success': False,
                    'message': f"{list_type} {op}: faction {entry_id} not found."
                }
            entity_name = f"{row['abbreviation']} ({row['name']})"
        elif entry_type == 'base':
            # Bases can be starbases, ports, or outposts; check all three
            base_row = None
            for tbl, idcol in (('starbases', 'base_id'), ('surface_ports', 'port_id'), ('outposts', 'outpost_id')):
                r = self.conn.execute(
                    f"SELECT name FROM {tbl} WHERE {idcol} = ? AND game_id = ?",
                    (entry_id, self.game_id)
                ).fetchone()
                if r:
                    base_row = r
                    break
            if not base_row:
                return {
                    'command': list_type, 'params': params_str,
                    'tu_before': tu_before, 'tu_after': tu_before,
                    'tu_cost': 0, 'success': False,
                    'message': f"{list_type} {op}: base {entry_id} not found."
                }
            entity_name = base_row['name']
        else:
            return {
                'command': list_type, 'params': params_str,
                'tu_before': tu_before, 'tu_after': tu_before,
                'tu_cost': 0, 'success': False,
                'message': f"{list_type} {op}: unknown entry type '{entry_type}'."
            }

        if op == 'remove':
            cur = self.conn.execute(
                "DELETE FROM ship_combat_lists "
                "WHERE game_id = ? AND ship_id = ? AND list_type = ? "
                "AND entry_type = ? AND entry_id = ?",
                (self.game_id, ship_id, list_kind, entry_type, entry_id)
            )
            self.conn.commit()
            if cur.rowcount > 0:
                return {
                    'command': list_type, 'params': params_str,
                    'tu_before': tu_before, 'tu_after': tu_before,
                    'tu_cost': 0, 'success': True,
                    'message': f"{list_type}: removed {entry_type} {entity_name} ({entry_id}) from {list_kind} list."
                }
            else:
                return {
                    'command': list_type, 'params': params_str,
                    'tu_before': tu_before, 'tu_after': tu_before,
                    'tu_cost': 0, 'success': True,
                    'message': f"{list_type}: {entry_type} {entry_id} was not on the {list_kind} list (no change)."
                }

        # ADD
        # Check for existing entry (UNIQUE constraint catches it but explicit is friendlier)
        existing = self.conn.execute(
            "SELECT 1 FROM ship_combat_lists WHERE game_id = ? AND ship_id = ? "
            "AND list_type = ? AND entry_type = ? AND entry_id = ?",
            (self.game_id, ship_id, list_kind, entry_type, entry_id)
        ).fetchone()
        if existing:
            return {
                'command': list_type, 'params': params_str,
                'tu_before': tu_before, 'tu_after': tu_before,
                'tu_cost': 0, 'success': True,
                'message': f"{list_type}: {entry_type} {entity_name} ({entry_id}) is already on the {list_kind} list."
            }
        self.conn.execute(
            "INSERT INTO ship_combat_lists "
            "(game_id, ship_id, list_type, entry_type, entry_id, "
            " added_turn_year, added_turn_week) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.game_id, ship_id, list_kind, entry_type, entry_id,
             game['current_year'], game['current_week'])
        )
        self.conn.commit()
        return {
            'command': list_type, 'params': params_str,
            'tu_before': tu_before, 'tu_after': tu_before,
            'tu_cost': 0, 'success': True,
            'message': f"{list_type}: added {entry_type} {entity_name} ({entry_id}) to {list_kind} list."
        }

    def _cmd_doctrine(self, state, params):
        """DOCTRINE <aggressive|defensive|evasive> — set this ship's combat doctrine."""
        tu_before = state['tu']
        ship_id = state['ship_id']
        doctrine = params.get('doctrine') if isinstance(params, dict) else str(params).lower()

        old_row = self.conn.execute(
            "SELECT combat_doctrine FROM ships WHERE ship_id = ?", (ship_id,)
        ).fetchone()
        old = old_row['combat_doctrine'] if old_row else 'defensive'

        self.conn.execute(
            "UPDATE ships SET combat_doctrine = ? WHERE ship_id = ?",
            (doctrine, ship_id)
        )
        self.conn.commit()
        return {
            'command': 'DOCTRINE', 'params': doctrine,
            'tu_before': tu_before, 'tu_after': tu_before,
            'tu_cost': 0, 'success': True,
            'message': f"Combat doctrine set to {doctrine.upper()} (was {old.upper() if old else 'DEFENSIVE'})."
        }

    def _cmd_magazine_transfer(self, state, cmd, params):
        """LOAD MAGAZINE <ammo> <qty> / UNLOAD MAGAZINE <ammo> <qty>.
        Moves missiles or torpedoes between cargo and magazine. 1 OC per order.
        Can be performed anywhere (docked or not).
        """
        tu_before = state['tu']
        ship_id = state['ship_id']
        ammo = params.get('ammo') if isinstance(params, dict) else None
        qty = params.get('qty') if isinstance(params, dict) else None
        params_str = f"MAGAZINE {ammo.upper() if ammo else '?'} {qty}"

        if ammo not in ('missile', 'torpedo') or not isinstance(qty, int) or qty <= 0:
            return {
                'command': cmd, 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"{cmd}: invalid parameters (ammo={ammo}, qty={qty})."
            }

        oc_cost = 1
        if state['tu'] < oc_cost:
            return {
                'command': cmd, 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': 0, 'success': False,
                'message': f"{cmd}: insufficient OC (need {oc_cost}, have {state['tu']})."
            }

        MISSILE_ITEM_ID = 501
        TORPEDO_ITEM_ID = 502
        item_id = MISSILE_ITEM_ID if ammo == 'missile' else TORPEDO_ITEM_ID
        ammo_label = 'missile' if ammo == 'missile' else 'torpedo'
        mag_col_cur = 'missiles_loaded' if ammo == 'missile' else 'torpedoes_loaded'
        mag_col_max = 'max_missiles' if ammo == 'missile' else 'max_torpedoes'

        ship = self.get_ship(ship_id)
        item = self.conn.execute(
            "SELECT name, mass_per_unit FROM trade_goods WHERE item_id = ?",
            (item_id,)
        ).fetchone()
        mass_per = item['mass_per_unit'] if item else (1 if ammo == 'missile' else 4)
        item_name = item['name'] if item else ammo_label.capitalize()

        cur_mag = ship[mag_col_cur] or 0
        max_mag = ship[mag_col_max] or 0

        cargo_row = self.conn.execute(
            "SELECT cargo_id, quantity FROM cargo_items "
            "WHERE ship_id = ? AND item_type_id = ?",
            (ship_id, item_id)
        ).fetchone()
        cargo_qty = cargo_row['quantity'] if cargo_row else 0

        if cmd == 'LOAD':
            # cargo -> magazine
            mag_free = max_mag - cur_mag
            if mag_free <= 0:
                return {
                    'command': cmd, 'params': params_str,
                    'tu_before': tu_before, 'tu_after': state['tu'],
                    'tu_cost': 0, 'success': False,
                    'message': f"LOAD: {ammo_label} magazine is full ({cur_mag}/{max_mag})."
                }
            if cargo_qty <= 0:
                return {
                    'command': cmd, 'params': params_str,
                    'tu_before': tu_before, 'tu_after': state['tu'],
                    'tu_cost': 0, 'success': False,
                    'message': f"LOAD: no {ammo_label}s in cargo to load."
                }
            actual = min(qty, mag_free, cargo_qty)
            cap_bits = []
            if mag_free < qty:
                cap_bits.append(f"magazine free={mag_free}")
            if cargo_qty < qty:
                cap_bits.append(f"cargo={cargo_qty}")
            cap_msg = f" (capped from {qty} to {actual}: {', '.join(cap_bits)})" if cap_bits else ""

            # Update magazine +actual, cargo -actual, cargo_used -actual*mass
            self.conn.execute(
                f"UPDATE ships SET {mag_col_cur} = {mag_col_cur} + ?, "
                f"cargo_used = MAX(0, cargo_used - ?) WHERE ship_id = ?",
                (actual, actual * mass_per, ship_id)
            )
            if cargo_qty == actual:
                self.conn.execute("DELETE FROM cargo_items WHERE cargo_id = ?",
                                    (cargo_row['cargo_id'],))
            else:
                self.conn.execute(
                    "UPDATE cargo_items SET quantity = quantity - ? WHERE cargo_id = ?",
                    (actual, cargo_row['cargo_id'])
                )
            state['tu'] -= oc_cost
            self.conn.commit()
            return {
                'command': cmd, 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': oc_cost, 'success': True,
                'message': (f"Loaded {actual} {item_name}(s) into magazine "
                             f"({cur_mag + actual}/{max_mag}){cap_msg}.")
            }

        else:  # UNLOAD
            # magazine -> cargo
            if cur_mag <= 0:
                return {
                    'command': cmd, 'params': params_str,
                    'tu_before': tu_before, 'tu_after': state['tu'],
                    'tu_cost': 0, 'success': False,
                    'message': f"UNLOAD: {ammo_label} magazine is empty."
                }
            # Cargo free space check
            cargo_free_mass = (ship['cargo_capacity'] or 0) - (ship['cargo_used'] or 0)
            max_by_cargo = cargo_free_mass // mass_per if mass_per > 0 else qty
            actual = min(qty, cur_mag, max_by_cargo)
            if actual <= 0:
                return {
                    'command': cmd, 'params': params_str,
                    'tu_before': tu_before, 'tu_after': state['tu'],
                    'tu_cost': 0, 'success': False,
                    'message': (f"UNLOAD: no cargo space for {ammo_label}s "
                                 f"(free={cargo_free_mass} ST, need {mass_per} ST each).")
                }
            cap_bits = []
            if cur_mag < qty:
                cap_bits.append(f"magazine has {cur_mag}")
            if max_by_cargo < qty:
                cap_bits.append(f"cargo free={cargo_free_mass} ST")
            cap_msg = f" (capped from {qty} to {actual}: {', '.join(cap_bits)})" if cap_bits else ""

            self.conn.execute(
                f"UPDATE ships SET {mag_col_cur} = MAX(0, {mag_col_cur} - ?), "
                f"cargo_used = cargo_used + ? WHERE ship_id = ?",
                (actual, actual * mass_per, ship_id)
            )
            if cargo_row:
                self.conn.execute(
                    "UPDATE cargo_items SET quantity = quantity + ? WHERE cargo_id = ?",
                    (actual, cargo_row['cargo_id'])
                )
            else:
                self.conn.execute(
                    """INSERT INTO cargo_items
                       (ship_id, item_type_id, item_name, quantity, mass_per_unit)
                       VALUES (?, ?, ?, ?, ?)""",
                    (ship_id, item_id, item_name, actual, mass_per)
                )
            state['tu'] -= oc_cost
            self.conn.commit()
            return {
                'command': cmd, 'params': params_str,
                'tu_before': tu_before, 'tu_after': state['tu'],
                'tu_cost': oc_cost, 'success': True,
                'message': (f"Unloaded {actual} {item_name}(s) to cargo "
                             f"(magazine: {cur_mag - actual}/{max_mag}){cap_msg}.")
            }

    def resolve_prefect_orders(self, prefect_orders_map):
        """
        Resolve prefect-scoped orders for multiple prefects.
        Input: {prefect_id: [order_dicts]}
        Returns: {prefect_id: [result_dicts]}
        Each result has keys: command, params, success, message.
        """
        all_results = {}
        for prefect_id, orders in prefect_orders_map.items():
            results = []
            for order in orders:
                cmd = order['command']
                params = order['params']
                if cmd == 'CHANGEFACTION':
                    results.append(self._resolve_prefect_changefaction(prefect_id, params))
                else:
                    results.append({
                        'command': cmd, 'params': params,
                        'success': False,
                        'message': f"Unknown or unsupported prefect command: {cmd}",
                    })
            all_results[prefect_id] = results
        return all_results

    def _resolve_prefect_changefaction(self, prefect_id, params):
        """CHANGEFACTION <faction_id> [reason] - prefect-scoped faction change request."""
        target_faction_id = params['faction_id']
        reason = params.get('reason', '')
        params_str = f"{target_faction_id}"
        if reason:
            params_str += f" {reason}"

        prefect = self.conn.execute(
            "SELECT * FROM prefects WHERE prefect_id = ?", (prefect_id,)
        ).fetchone()
        if not prefect:
            return {
                'command': 'CHANGEFACTION', 'params': params_str,
                'success': False,
                'message': f"Prefect {prefect_id} not found. Order dropped."
            }
        current_faction_id = prefect['faction_id']

        target_faction = self.conn.execute(
            "SELECT * FROM universe.factions WHERE faction_id = ?",
            (target_faction_id,)
        ).fetchone()
        if not target_faction:
            return {
                'command': 'CHANGEFACTION', 'params': params_str,
                'success': False,
                'message': f"Cannot request faction change: faction {target_faction_id} not found. Order dropped."
            }

        # Already in that faction (e.g. GM already approved this turn)
        if current_faction_id == target_faction_id:
            return {
                'command': 'CHANGEFACTION', 'params': params_str,
                'success': True,
                'message': (f"Already a member of {target_faction['name']} "
                            f"({target_faction['abbreviation']}). "
                            f"Faction change complete.")
            }

        # Check for existing pending request
        existing = self.conn.execute(
            "SELECT * FROM faction_requests WHERE game_id = ? AND prefect_id = ? AND status = 'pending'",
            (self.game_id, prefect_id)
        ).fetchone()
        if existing:
            existing_faction = self.conn.execute(
                "SELECT abbreviation FROM universe.factions WHERE faction_id = ?",
                (existing['target_faction_id'],)
            ).fetchone()
            ef_name = existing_faction['abbreviation'] if existing_faction else str(existing['target_faction_id'])
            return {
                'command': 'CHANGEFACTION', 'params': params_str,
                'success': False,
                'message': (f"Cannot request: you already have a pending faction change "
                            f"request to {ef_name} (request #{existing['request_id']}). Order dropped.")
            }

        # Submit request (this normally won't happen here because Phase 1.1 in
        # run-turn creates the request earlier, but handle it defensively).
        game = self.get_game()
        self.conn.execute("""
            INSERT INTO faction_requests
            (game_id, prefect_id, current_faction_id, target_faction_id,
             reason, status, requested_turn_year, requested_turn_week)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        """, (self.game_id, prefect_id, current_faction_id,
              target_faction_id, reason,
              game['current_year'], game['current_week']))
        self.conn.commit()

        from db.database import get_faction
        current_faction = get_faction(self.conn, current_faction_id)
        cur_abbr = current_faction['abbreviation'] if current_faction else '?'
        reason_str = f" Reason: \"{reason}\"" if reason else ""

        return {
            'command': 'CHANGEFACTION', 'params': params_str,
            'success': True,
            'message': (f"Faction change requested: {cur_abbr} -> "
                        f"{target_faction['abbreviation']} ({target_faction['name']}).{reason_str} "
                        f"Awaiting GM approval.")
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
        Passive scan sweep. When the active ship enters or rests on a cell,
        roll probabilistic detection against every ship and base within
        PASSIVE_SCAN_RANGE. Reciprocal: stationary ships and bases also roll
        to detect the active ship.

        Returns a list of contact dicts detected by the active ship (those
        it rolled and spotted), suitable for the encounters accumulator.
        Contacts detected by OTHER observers (patrol ships / bases) are
        written directly to known_contacts for their respective owners.
        """
        from engine.detection import (PASSIVE_SCAN_RANGE, grid_distance,
                                        try_detect)

        active_ship_id = state['ship_id']
        active_system = state['system_id']
        active_col = state['col']
        active_row = state['row']

        # Active ship's own stats
        active_ship = self.conn.execute(
            "SELECT * FROM ships WHERE ship_id = ?", (active_ship_id,)
        ).fetchone()
        if not active_ship:
            return []
        active_rating = active_ship['sensor_rating'] or 0
        active_profile = active_ship['sensor_profile'] or (active_ship['ship_size'] / 100.0 if active_ship['ship_size'] else 0.5)
        active_prefect_id = active_ship['owner_prefect_id']

        # --- 1. Candidate ships within range (same system, different ship) ---
        candidate_ships = self.conn.execute(
            """SELECT s.*, pp.faction_id, pp.prefect_id as owner_prefect_id
               FROM ships s
               JOIN prefects pp ON s.owner_prefect_id = pp.prefect_id
               JOIN players p ON pp.player_id = p.player_id
               WHERE s.system_id = ? AND s.game_id = ?
                 AND p.status = 'active'
                 AND s.ship_id != ?""",
            (active_system, self.game_id, active_ship_id)
        ).fetchall()

        # --- 2. Candidate bases within range (starbases only at system level;
        # surface ports and outposts are only visible from orbit due to
        # planetary/atmospheric interference) ---
        candidate_bases = []
        for kind, table, id_col in [
            ('starbase', 'starbases', 'base_id'),
        ]:
            cols = [r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if 'grid_col' not in cols or 'grid_row' not in cols:
                continue
            rows = self.conn.execute(
                f"SELECT *, '{kind}' AS kind, {id_col} AS base_id FROM {table} WHERE system_id = ? AND game_id = ?",
                (active_system, self.game_id)
            ).fetchall()
            for r in rows:
                candidate_bases.append(r)

        # --- 2b. Surface installations on the body this ship is orbiting
        # or landed on (treated as range 0; atmospheric interference means
        # they are ONLY detectable from close proximity to their body). ---
        orbit_body = state.get('orbiting') or state.get('landed')
        surface_installations = []
        if orbit_body:
            for kind, table, id_col in [
                ('port', 'surface_ports', 'port_id'),
                ('outpost', 'outposts', 'outpost_id'),
            ]:
                rows = self.conn.execute(
                    f"SELECT *, '{kind}' AS kind, {id_col} AS base_id FROM {table} WHERE body_id = ? AND game_id = ?",
                    (orbit_body, self.game_id)
                ).fetchall()
                for r in rows:
                    surface_installations.append(r)

        detected_by_active = []
        game = self.get_game()

        # --- 3. Active ship rolls to detect each candidate ---
        for s in candidate_ships:
            dist = grid_distance(active_col, active_row,
                                  s['grid_col'], s['grid_row'])
            if dist > PASSIVE_SCAN_RANGE:
                continue
            target_profile = s['sensor_profile'] or (s['ship_size'] / 100.0 if s['ship_size'] else 0.5)
            spotted, _chance = try_detect(active_rating, target_profile, dist)
            if spotted:
                faction = self._get_faction(s['faction_id'])
                display_name = f"{faction['abbreviation']} {s['name']}"
                contact = {
                    'type': 'ship', 'id': s['ship_id'],
                    'name': display_name,
                    'col': s['grid_col'], 'row': s['grid_row'],
                    'symbol': '^',
                    'ship_size': s['ship_size'],
                    'hull_count': s['hull_count'],
                    'hull_type': s['hull_type'],
                    'faction_id': s['faction_id'],
                    'range': dist,
                }
                detected_by_active.append(contact)
                if not any(c['type'] == 'ship' and c['id'] == s['ship_id']
                           for c in self.contacts):
                    self.contacts.append(contact)

        for b in candidate_bases:
            dist = grid_distance(active_col, active_row,
                                  b['grid_col'], b['grid_row'])
            if dist > PASSIVE_SCAN_RANGE:
                continue
            target_profile = b['sensor_profile'] or 1.0
            spotted, _chance = try_detect(active_rating, target_profile, dist)
            if spotted:
                base_name = b['name']
                contact = {
                    'type': b['kind'], 'id': b['base_id'],
                    'name': base_name,
                    'col': b['grid_col'], 'row': b['grid_row'],
                    'symbol': '#',
                    'ship_size': None,
                    'hull_type': b['kind'].title(),
                    'range': dist,
                }
                detected_by_active.append(contact)
                if not any(c['type'] == b['kind'] and c['id'] == b['base_id']
                           for c in self.contacts):
                    self.contacts.append(contact)

        # --- 3b. Active ship rolls to detect surface installations (range 0) ---
        for si in surface_installations:
            target_profile = si['sensor_profile'] or 1.0
            spotted, _chance = try_detect(active_rating, target_profile, 0)
            if spotted:
                # Use the body's grid position for reporting
                body_loc = self.conn.execute(
                    "SELECT grid_col, grid_row FROM celestial_bodies WHERE body_id = ?",
                    (orbit_body,)
                ).fetchone()
                loc_col = body_loc['grid_col'] if body_loc else active_col
                loc_row = body_loc['grid_row'] if body_loc else active_row
                contact = {
                    'type': si['kind'], 'id': si['base_id'],
                    'name': si['name'],
                    'col': loc_col, 'row': loc_row,
                    'symbol': '#',
                    'ship_size': None,
                    'hull_type': si['kind'].title(),
                    'range': 0,
                }
                detected_by_active.append(contact)
                if not any(c['type'] == si['kind'] and c['id'] == si['base_id']
                           for c in self.contacts):
                    self.contacts.append(contact)

        # --- 4. Reciprocal: stationary ships and bases roll to detect the active ship ---
        active_faction = None
        active_pr = self.conn.execute(
            "SELECT faction_id FROM prefects WHERE prefect_id = ?",
            (active_prefect_id,)
        ).fetchone()
        if active_pr:
            active_faction = active_pr['faction_id']
        active_display = f"{self._get_faction(active_faction)['abbreviation']} {active_ship['name']}" if active_faction else active_ship['name']

        def _record_reciprocal_contact(observer_prefect_id, scanner_ship_id, dist):
            """Write a known_contacts row for an observer that spotted the active ship."""
            if observer_prefect_id is None:
                return
            if observer_prefect_id == active_prefect_id:
                return  # don't log self-detection for the same owner
            # Avoid duplicate rows for this prefect/ship this turn
            existing = self.conn.execute("""
                SELECT contact_id FROM known_contacts
                WHERE prefect_id = ? AND object_type = 'ship' AND object_id = ?
                AND discovered_turn_year = ? AND discovered_turn_week = ?
            """, (observer_prefect_id, active_ship_id,
                  game['current_year'], game['current_week'])).fetchone()
            if existing:
                return
            self.conn.execute("""
                INSERT INTO known_contacts
                (prefect_id, object_type, object_id, object_name,
                 location_system, location_col, location_row,
                 discovered_turn_year, discovered_turn_week,
                 scanner_ship_id, target_faction_id, target_hull_type,
                 target_ship_size, detection_range)
                VALUES (?, 'ship', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (observer_prefect_id, active_ship_id, active_display,
                  active_system, active_col, active_row,
                  game['current_year'], game['current_week'],
                  scanner_ship_id, active_faction,
                  active_ship['hull_type'], active_ship['ship_size'], dist))
            self.conn.commit()

        # Stationary ships that aren't the active one roll too
        for s in candidate_ships:
            dist = grid_distance(active_col, active_row,
                                  s['grid_col'], s['grid_row'])
            if dist > PASSIVE_SCAN_RANGE:
                continue
            observer_rating = s['sensor_rating'] or 0
            if observer_rating <= 0:
                continue
            spotted, _chance = try_detect(observer_rating, active_profile, dist)
            if spotted:
                _record_reciprocal_contact(s['owner_prefect_id'],
                                            s['ship_id'], dist)

        # Bases with sensor_rating > 0 scan too
        for b in candidate_bases:
            dist = grid_distance(active_col, active_row,
                                  b['grid_col'], b['grid_row'])
            if dist > PASSIVE_SCAN_RANGE:
                continue
            observer_rating = b['sensor_rating'] or 0
            if observer_rating <= 0:
                continue
            spotted, _chance = try_detect(observer_rating, active_profile, dist)
            if spotted:
                _record_reciprocal_contact(b['owner_prefect_id'], None, dist)

        # Surface installations (ports, outposts) with sensors also scan the
        # active ship if it's orbiting or landed on their body (distance 0).
        for si in surface_installations:
            observer_rating = si['sensor_rating'] or 0
            if observer_rating <= 0:
                continue
            spotted, _chance = try_detect(observer_rating, active_profile, 0)
            if spotted:
                _record_reciprocal_contact(si['owner_prefect_id'], None, 0)

        # --- 5. Detection-triggered combat: check lists for any new hits ---
        self._check_combat_trigger_for_active_ship(
            active_ship, detected_by_active
        )

        return detected_by_active

    def _check_combat_trigger_for_active_ship(self, active_ship, detected_contacts):
        """
        After a passive sweep, check if any detected contacts match this
        ship's TARGET list (or AVOID — which suppresses targeting).
        If so, create or extend a combat engagement.

        For now this only checks the active ship's target list. Defend-list
        propagation is handled by the engagement runner each round.
        """
        from engine.combat import (get_ship_combat_lists, entity_matches_list,
                                     find_or_create_engagement, add_participant,
                                     log_combat_event)

        if not active_ship or not detected_contacts:
            return

        ship_id = active_ship['ship_id']
        lists = get_ship_combat_lists(self.conn, self.game_id, ship_id)
        target_list = lists['target']
        avoid_list = lists['avoid']
        if not target_list:
            return

        game = self.get_game()
        turn_year = game['current_year']
        turn_week = game['current_week']

        for c in detected_contacts:
            ckind = c.get('type')
            cid = c.get('id')
            cfaction = c.get('faction_id')

            # Avoid wins over target — skip
            if entity_matches_list(ckind, cid, cfaction, avoid_list):
                continue
            if not entity_matches_list(ckind, cid, cfaction, target_list):
                continue
            if ckind not in ('ship', 'starbase', 'port', 'outpost'):
                continue

            # Active ship initiates combat at its current location
            engagement_id = find_or_create_engagement(
                self.conn, self.game_id,
                active_ship['system_id'], active_ship['grid_col'],
                active_ship['grid_row'],
                turn_year, turn_week, started_on_round=0
            )
            # Add active ship as participant
            add_participant(
                self.conn, engagement_id, 'ship', ship_id,
                active_ship['owner_prefect_id'], turn_year, turn_week, 0,
                active_ship['integrity'] or 100.0
            )
            # Add the target as participant
            target_kind_for_db = ckind  # 'ship' | 'starbase' | 'port' | 'outpost'
            target_owner = None
            target_integrity = 100.0
            if ckind == 'ship':
                trow = self.conn.execute(
                    "SELECT owner_prefect_id, integrity FROM ships WHERE ship_id = ?",
                    (cid,)
                ).fetchone()
                if trow:
                    target_owner = trow['owner_prefect_id']
                    target_integrity = trow['integrity'] or 100.0
            else:
                tbl_map = {'starbase': ('starbases', 'base_id'),
                            'port': ('surface_ports', 'port_id'),
                            'outpost': ('outposts', 'outpost_id')}
                tbl, idcol = tbl_map.get(ckind, (None, None))
                if tbl:
                    trow = self.conn.execute(
                        f"SELECT owner_prefect_id FROM {tbl} WHERE {idcol} = ?",
                        (cid,)
                    ).fetchone()
                    if trow:
                        target_owner = trow['owner_prefect_id']
                    target_integrity = 100.0  # bases don't track integrity in v1
            add_participant(
                self.conn, engagement_id, target_kind_for_db, cid,
                target_owner, turn_year, turn_week, 0, target_integrity
            )
            log_combat_event(
                self.conn, engagement_id, turn_year, turn_week, 0,
                'system', None, 'engage',
                detail=f"{c.get('name', cid)} matched target list of ship {ship_id} — engagement opened"
            )

    def run_initial_passive_scan(self):
        """
        Run a one-shot passive scan sweep for every ship in the game.
        This catches static positions — ships that aren't moving this turn,
        and bases that haven't been triggered by reciprocal detection.

        Called once at the start of turn resolution, before movement.
        Writes detections directly to known_contacts.
        """
        ships = self.conn.execute(
            "SELECT * FROM ships WHERE game_id = ?", (self.game_id,)
        ).fetchall()

        for s in ships:
            state = {
                'ship_id': s['ship_id'],
                'system_id': s['system_id'],
                'col': s['grid_col'],
                'row': s['grid_row'],
                'orbiting': s['orbiting_body_id'],
                'landed': s['landed_body_id'],
            }
            prior_contacts = self.contacts
            self.contacts = []
            self._detect_ships_at_location(state)
            if self.contacts:
                self._update_contacts(s['owner_prefect_id'], s['system_id'])
            self.contacts = prior_contacts

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
                self.conn.execute("""
                    UPDATE known_contacts SET
                        location_col = ?, location_row = ?,
                        location_system = ?,
                        discovered_turn_year = ?, discovered_turn_week = ?,
                        target_faction_id = ?,
                        target_hull_type = ?,
                        target_ship_size = ?,
                        detection_range = ?
                    WHERE contact_id = ?
                """, (contact['col'], contact['row'], system_id,
                      game['current_year'], game['current_week'],
                      contact.get('faction_id'),
                      contact.get('hull_type'),
                      contact.get('ship_size'),
                      contact.get('range'),
                      existing['contact_id']))
            else:
                self.conn.execute("""
                    INSERT INTO known_contacts
                    (prefect_id, object_type, object_id, object_name,
                     location_system, location_col, location_row,
                     discovered_turn_year, discovered_turn_week,
                     target_faction_id, target_hull_type,
                     target_ship_size, detection_range)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    prefect_id, contact['type'], contact['id'], contact['name'],
                    system_id, contact['col'], contact['row'],
                    game['current_year'], game['current_week'],
                    contact.get('faction_id'),
                    contact.get('hull_type'),
                    contact.get('ship_size'),
                    contact.get('range'),
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
