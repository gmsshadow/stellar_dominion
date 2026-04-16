"""
Stellar Dominion - Combat resolution engine.

Combat is detection-triggered. When a ship/base detects an entity that
matches its target list (or detects an attacker on something its defend
list protects), an engagement begins. Each turn provides 6 combat rounds.
Rounds run sequentially per engagement, with each combatant choosing one
action per round (move, fire, evade) based on its doctrine.

Key types/concepts:
- engagement: ongoing battle in a single grid cell area
- participant: ship or base in the engagement
- round: one "tick" of combat (6 per turn)
- doctrine: aggressive | defensive | evasive (per ship)

Combat ends when:
1. All hostiles destroyed
2. All hostiles out of detection range
3. All combatants on one side fled (broke contact + stayed broken 1 round)
4. Mutual breakoff (everyone retreating)
"""

import math
import random
from engine.detection import grid_distance, PASSIVE_SCAN_RANGE

ROUNDS_PER_TURN = 6
DEFEND_RESPONSE_RANGE = 5  # Chebyshev cells; defenders move to engage within this
SPEED_BONUS_GRAVITY_THRESHOLD = 3.0  # ships at or above this gravity_rating move 2 cells/round

DOCTRINE_RETREAT_THRESHOLDS = {
    'aggressive': 25,
    'defensive':  50,
    'evasive':    75,
}


def get_ship_weapons(conn, ship_id):
    """Return a list of (name, damage, range, shots_per_round, qty) for installed weapons."""
    rows = conn.execute(
        """SELECT sc.name, sc.weapon_damage, sc.weapon_range,
                  sc.weapon_shots_per_round, ii.quantity
           FROM installed_items ii
           JOIN ship_components sc ON ii.component_id = sc.component_id
           WHERE ii.ship_id = ? AND sc.category = 'weapon'""",
        (ship_id,)
    ).fetchall()
    return [(r['name'], r['weapon_damage'] or 0, r['weapon_range'] or 0,
             r['weapon_shots_per_round'] or 0, r['quantity']) for r in rows]


def get_base_weapons(conn, base_kind, base_id):
    """
    Return total damage-per-round and effective range for a base's weapons,
    derived from its installed defence modules. We treat each Defence Turret
    as: damage_per_round = defence_rating, range = 2.
    """
    if base_kind == 'starbase':
        where = "starbase_id = ?"
    elif base_kind == 'port':
        where = "port_id = ?"
    elif base_kind == 'outpost':
        where = "outpost_id = ?"
    else:
        return 0, 0
    rows = conn.execute(
        f"""SELECT bm.defence_rating, im.quantity
            FROM installed_modules im
            JOIN base_modules bm ON im.module_id = bm.module_id
            WHERE {where} AND bm.defence_rating > 0""",
        (base_id,)
    ).fetchall()
    total = sum((r['defence_rating'] or 0) * (r['quantity'] or 1) for r in rows)
    return total, 2  # range 2 always for base weapons in v1


def ship_movement_per_round(gravity_rating):
    """Faster ships move further per combat round."""
    if gravity_rating and gravity_rating >= SPEED_BONUS_GRAVITY_THRESHOLD:
        return 2
    return 1


def doctrine_retreat_threshold(doctrine):
    return DOCTRINE_RETREAT_THRESHOLDS.get(doctrine or 'defensive', 50)


def get_ship_combat_lists(conn, game_id, ship_id):
    """Return dict {list_type: [(entry_type, entry_id), ...]}."""
    rows = conn.execute(
        """SELECT list_type, entry_type, entry_id
           FROM ship_combat_lists
           WHERE game_id = ? AND ship_id = ?""",
        (game_id, ship_id)
    ).fetchall()
    out = {'target': [], 'defend': [], 'avoid': []}
    for r in rows:
        lst = r['list_type']
        if lst in out:
            out[lst].append((r['entry_type'], r['entry_id']))
    return out


def get_base_combat_lists(conn, game_id, base_kind, base_id):
    """Return dict {list_type: [(entry_type, entry_id), ...]} for a base."""
    rows = conn.execute(
        """SELECT list_type, entry_type, entry_id
           FROM base_combat_lists
           WHERE game_id = ? AND base_kind = ? AND base_id = ?""",
        (game_id, base_kind, base_id)
    ).fetchall()
    out = {'target': [], 'defend': []}
    for r in rows:
        lst = r['list_type']
        if lst in out:
            out[lst].append((r['entry_type'], r['entry_id']))
    return out


def entity_matches_list(entity_kind, entity_id, entity_faction_id, list_entries):
    """
    Check if a given entity (ship/base) with optional faction matches any
    entry in the list. list_entries is [(entry_type, entry_id), ...].
    """
    for entry_type, entry_id in list_entries:
        if entry_type == 'ship' and entity_kind == 'ship' and entry_id == entity_id:
            return True
        if entry_type == 'base' and entity_kind in ('starbase', 'port', 'outpost') and entry_id == entity_id:
            return True
        if entry_type == 'faction' and entity_faction_id == entry_id:
            return True
    return False


def find_or_create_engagement(conn, game_id, system_id, col, row,
                                turn_year, turn_week, started_on_round):
    """
    Find an active engagement in the same grid cell, or create one.
    Engagements coalesce by location: all hostilities at the same grid
    cell join one engagement.
    """
    existing = conn.execute(
        """SELECT engagement_id FROM combat_engagements
           WHERE game_id = ? AND system_id = ? AND grid_col = ? AND grid_row = ?
             AND status = 'active'""",
        (game_id, system_id, col, row)
    ).fetchone()
    if existing:
        return existing['engagement_id']
    cur = conn.execute(
        """INSERT INTO combat_engagements
           (game_id, started_turn_year, started_turn_week, started_on_round,
            last_active_turn_year, last_active_turn_week,
            system_id, grid_col, grid_row, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
        (game_id, turn_year, turn_week, started_on_round,
         turn_year, turn_week, system_id, col, row)
    )
    conn.commit()
    return cur.lastrowid


def add_participant(conn, engagement_id, kind, entity_id, owner_prefect_id,
                     turn_year, turn_week, round_number, integrity):
    """Add a participant to an engagement (idempotent on UNIQUE)."""
    existing = conn.execute(
        """SELECT participant_id FROM combat_participants
           WHERE engagement_id = ? AND participant_kind = ? AND participant_id_value = ?""",
        (engagement_id, kind, entity_id)
    ).fetchone()
    if existing:
        return existing['participant_id']
    cur = conn.execute(
        """INSERT INTO combat_participants
           (engagement_id, participant_kind, participant_id_value, owner_prefect_id,
            joined_turn_year, joined_turn_week, joined_on_round,
            integrity_at_join, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
        (engagement_id, kind, entity_id, owner_prefect_id,
         turn_year, turn_week, round_number, integrity)
    )
    conn.commit()
    return cur.lastrowid


def log_combat_event(conn, engagement_id, turn_year, turn_week, round_number,
                      actor_kind, actor_id, action,
                      target_kind=None, target_id=None,
                      damage=None, integrity_after=None, detail=None):
    """Append an entry to the combat_log."""
    conn.execute(
        """INSERT INTO combat_log
           (engagement_id, turn_year, turn_week, round_number,
            actor_kind, actor_id, action, target_kind, target_id,
            damage, integrity_after, detail)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (engagement_id, turn_year, turn_week, round_number,
         actor_kind, actor_id, action, target_kind, target_id,
         damage, integrity_after, detail)
    )
    conn.commit()


def get_active_engagements(conn, game_id):
    """All active engagements in the game."""
    return conn.execute(
        """SELECT * FROM combat_engagements
           WHERE game_id = ? AND status = 'active'""",
        (game_id,)
    ).fetchall()


def get_engagement_participants(conn, engagement_id, active_only=True):
    """All participants in an engagement."""
    where = "engagement_id = ?"
    if active_only:
        where += " AND status = 'active'"
    return conn.execute(
        f"SELECT * FROM combat_participants WHERE {where}",
        (engagement_id,)
    ).fetchall()


def is_ship_in_combat(conn, game_id, ship_id):
    """True if this ship is an active participant in any active engagement."""
    r = conn.execute(
        """SELECT 1 FROM combat_participants p
           JOIN combat_engagements e ON p.engagement_id = e.engagement_id
           WHERE e.game_id = ? AND e.status = 'active'
             AND p.participant_kind = 'ship' AND p.participant_id_value = ?
             AND p.status = 'active'""",
        (game_id, ship_id)
    ).fetchone()
    return r is not None


# ============================================================
# COMBAT RESOLUTION
# ============================================================

def get_participant_state(conn, game_id, kind, entity_id):
    """
    Fetch the current world state of a combat participant — position,
    integrity, weapons, doctrine, faction, etc. Returns a dict.
    """
    if kind == 'ship':
        row = conn.execute(
            """SELECT s.*, pp.faction_id
               FROM ships s
               LEFT JOIN prefects pp ON s.owner_prefect_id = pp.prefect_id
               WHERE s.ship_id = ? AND s.game_id = ?""",
            (entity_id, game_id)
        ).fetchone()
        if not row:
            return None
        weapons = get_ship_weapons(conn, entity_id)
        integ = row['integrity'] if row['integrity'] is not None else 100.0
        return {
            'kind': 'ship', 'id': entity_id,
            'name': row['name'], 'faction_id': row['faction_id'],
            'col': row['grid_col'], 'row': row['grid_row'],
            'system_id': row['system_id'],
            'integrity': integ,
            'doctrine': row['combat_doctrine'] or 'defensive',
            'gravity_rating': row['gravity_rating'] or 1.0,
            'sensor_rating': row['sensor_rating'] or 0,
            'sensor_profile': row['sensor_profile'] or 0.5,
            'ship_size': row['ship_size'] or 50,
            'hull_type': row['hull_type'],
            'owner_prefect_id': row['owner_prefect_id'],
            'weapons': weapons,
            'movement': ship_movement_per_round(row['gravity_rating'] or 1.0),
        }
    else:  # 'starbase' | 'port' | 'outpost'
        tbl_map = {'starbase': ('starbases', 'base_id'),
                   'port': ('surface_ports', 'port_id'),
                   'outpost': ('outposts', 'outpost_id')}
        if kind not in tbl_map:
            return None
        tbl, idcol = tbl_map[kind]
        row = conn.execute(
            f"SELECT * FROM {tbl} WHERE {idcol} = ? AND game_id = ?",
            (entity_id, game_id)
        ).fetchone()
        if not row:
            return None
        owner_prefect_id = row['owner_prefect_id'] if 'owner_prefect_id' in row.keys() else None
        # Bases owner determines faction
        owner_faction = None
        if owner_prefect_id:
            f = conn.execute(
                "SELECT faction_id FROM prefects WHERE prefect_id = ?",
                (owner_prefect_id,)
            ).fetchone()
            if f:
                owner_faction = f['faction_id']
        wdmg, wrange = get_base_weapons(conn, kind, entity_id)
        return {
            'kind': kind, 'id': entity_id,
            'name': row['name'], 'faction_id': owner_faction,
            'col': row['grid_col'] if 'grid_col' in row.keys() else None,
            'row': row['grid_row'] if 'grid_row' in row.keys() else None,
            'system_id': row['system_id'] if 'system_id' in row.keys() else None,
            'integrity': 100.0,  # bases don't track integrity in v1
            'doctrine': None,
            'gravity_rating': 0,
            'sensor_rating': row['sensor_rating'] or 0,
            'sensor_profile': row['sensor_profile'] or 1.0,
            'ship_size': None,
            'hull_type': kind.title(),
            'owner_prefect_id': owner_prefect_id,
            'base_weapon_damage': wdmg,
            'base_weapon_range': wrange,
            'movement': 0,
            'weapons': [],
        }


def pick_combat_target(conn, game_id, actor, all_participants_state,
                         engagement_id=None):
    """
    From the actor's perspective, decide who to fire at this round.

    Strategy:
      1. Filter to other participants alive and in this engagement
      2. Match against actor's TARGET list (ship + base + faction entries)
      3. Filter against actor's AVOID list (skip those)
      4. Defenders: also target any participant who recently fired at the
         entities on this actor's defend list (those are "attackers of allies")
      5. Of valid targets, prefer the one with lowest integrity, then nearest
    """
    if actor['kind'] == 'ship':
        lists = get_ship_combat_lists(conn, game_id, actor['id'])
        targets = lists['target']
        defends = lists['defend']
        avoids = lists['avoid']
    else:
        lists = get_base_combat_lists(conn, game_id, actor['kind'], actor['id'])
        targets = lists['target']
        defends = lists['defend']
        avoids = []  # bases have no avoid

    # Build "attackers of my allies" set from combat_log: anyone who has
    # fired at a participant matching my defend list during this engagement
    defended_attackers = set()  # set of (kind, id)
    if engagement_id and defends:
        log_rows = conn.execute(
            """SELECT actor_kind, actor_id, target_kind, target_id
               FROM combat_log
               WHERE engagement_id = ? AND action = 'fire'""",
            (engagement_id,)
        ).fetchall()
        for lr in log_rows:
            tk, ti = lr['target_kind'], lr['target_id']
            if not tk or ti is None:
                continue
            # Look up target's faction to test against defend list
            tfaction = None
            if tk == 'ship':
                trow = conn.execute(
                    "SELECT pp.faction_id FROM ships s "
                    "LEFT JOIN prefects pp ON s.owner_prefect_id = pp.prefect_id "
                    "WHERE s.ship_id = ?", (ti,)
                ).fetchone()
                tfaction = trow['faction_id'] if trow else None
            if entity_matches_list(tk, ti, tfaction, defends):
                if lr['actor_kind'] in ('ship', 'starbase', 'port', 'outpost'):
                    defended_attackers.add((lr['actor_kind'], lr['actor_id']))

    candidates = []
    for p in all_participants_state:
        if p['id'] == actor['id'] and p['kind'] == actor['kind']:
            continue
        if p.get('integrity', 0) <= 0:
            continue
        if entity_matches_list(p['kind'], p['id'], p.get('faction_id'), avoids):
            continue
        # Eligible if on target list OR a known defended-target attacker
        if entity_matches_list(p['kind'], p['id'], p.get('faction_id'), targets):
            candidates.append(p)
        elif (p['kind'], p['id']) in defended_attackers:
            candidates.append(p)

    if not candidates:
        return None
    def sort_key(p):
        dist = grid_distance(actor['col'], actor['row'], p['col'], p['row'])
        return (p.get('integrity', 100), dist)
    candidates.sort(key=sort_key)
    return candidates[0]


def decide_action(conn, game_id, actor, target, all_participants_state):
    """
    Doctrine-driven action selection. Returns one of:
      ('fire', target_state)
      ('move', (dest_col, dest_row))
      ('evade', None)
      ('hold', None)
    """
    # No target found, no action
    if not target:
        return ('hold', None)

    integrity = actor.get('integrity', 100)
    doctrine = actor.get('doctrine', 'defensive') or 'defensive'
    retreat_threshold = doctrine_retreat_threshold(doctrine)

    # If integrity below retreat threshold, try to flee
    if integrity <= retreat_threshold and actor['kind'] == 'ship':
        # Move directly away from target
        dest = _step_away(actor, target)
        if dest:
            return ('move', dest)
        # Cornered (can't move, e.g. same cell as enemy or grid edge)
        # Fight back rather than just evade — at least take some with us.
        dist = grid_distance(actor['col'], actor['row'], target['col'], target['row'])
        max_wrange = max((w[2] for w in actor.get('weapons', [])), default=0)
        if max_wrange > 0 and dist <= max_wrange:
            return ('fire', target)
        return ('evade', None)

    # Calculate range to target
    dist = grid_distance(actor['col'], actor['row'], target['col'], target['row'])

    # Determine effective max weapon range
    if actor['kind'] == 'ship':
        max_wrange = max((w[2] for w in actor.get('weapons', [])), default=0)
    else:
        max_wrange = actor.get('base_weapon_range', 0)

    if max_wrange <= 0:
        # No weapons — can only move/evade
        if doctrine == 'evasive':
            return ('evade', None)
        if actor['kind'] == 'ship':
            dest = _step_away(actor, target)
            return ('move', dest) if dest else ('evade', None)
        return ('hold', None)

    # In range -> fire
    if dist <= max_wrange:
        # Doctrine adjustments
        if doctrine == 'evasive':
            # Only fire if target is closing; otherwise keep distance
            if dist < max_wrange:
                # Target may be too close; back off
                if actor['kind'] == 'ship':
                    dest = _step_away(actor, target)
                    if dest:
                        return ('move', dest)
            return ('fire', target)
        return ('fire', target)

    # Out of range
    # Special case: if this is a defender (target was added via defended_attackers)
    # — close to engage even on defensive doctrine. Defenders shouldn't sit still
    # while the ally they protect is being shot.
    is_defender_response = False
    if actor['kind'] == 'ship':
        lists = get_ship_combat_lists(conn, game_id, actor['id'])
        if lists['defend'] and not entity_matches_list(
                target['kind'], target['id'], target.get('faction_id'),
                lists['target']):
            is_defender_response = True

    if doctrine == 'aggressive' and actor['kind'] == 'ship':
        dest = _step_toward(actor, target)
        if dest:
            return ('move', dest)
    elif is_defender_response and actor['kind'] == 'ship':
        # Defenders close to engage regardless of base doctrine
        dest = _step_toward(actor, target)
        if dest:
            return ('move', dest)
    elif doctrine == 'defensive':
        # Hold position; fire only when target comes to us
        return ('hold', None)
    elif doctrine == 'evasive' and actor['kind'] == 'ship':
        # Move further away
        dest = _step_away(actor, target)
        if dest:
            return ('move', dest)
    return ('hold', None)


def _step_toward(actor, target):
    """Return (col, row) one step closer to target."""
    return _step(actor, target, +1)


def _step_away(actor, target):
    """Return (col, row) one step further from target."""
    return _step(actor, target, -1)


def _step(actor, target, sign):
    """Compute one Chebyshev step from actor toward (sign=+1) or away from (sign=-1) target."""
    if not actor['col'] or not target.get('col'):
        return None
    a_col_n = ord(actor['col'].upper())
    t_col_n = ord(target['col'].upper())
    a_row = int(actor['row'])
    t_row = int(target['row'])
    dc = t_col_n - a_col_n
    dr = t_row - a_row
    # Step direction
    step_c = (1 if dc > 0 else -1 if dc < 0 else 0) * sign
    step_r = (1 if dr > 0 else -1 if dr < 0 else 0) * sign
    new_col_n = a_col_n + step_c
    new_row = a_row + step_r
    # Clamp to grid 'A'-'Y' (1-25), rows 1-25
    new_col_n = max(ord('A'), min(ord('Y'), new_col_n))
    new_row = max(1, min(25, new_row))
    if new_col_n == a_col_n and new_row == a_row:
        return None
    return (chr(new_col_n), new_row)


def apply_damage_to_ship(conn, ship_id, damage, attacker_kind=None,
                          attacker_id=None, attacker_name=None,
                          attacker_faction_id=None,
                          attacker_hull_type=None, attacker_size=None,
                          attacker_col=None, attacker_row=None,
                          system_id=None, turn_year=None, turn_week=None,
                          tick=None):
    """
    Reduce ship integrity. Returns new integrity.

    Damage = perfect detection: when ship takes damage, the attacker is
    recorded as a known contact for the victim's owner (regardless of
    sensor odds). The contact is marked detection_source='damage'.
    """
    row = conn.execute(
        "SELECT integrity, owner_prefect_id FROM ships WHERE ship_id = ?",
        (ship_id,)
    ).fetchone()
    if not row:
        return 0
    new_integrity = max(0, (row['integrity'] or 100) - damage)
    conn.execute("UPDATE ships SET integrity = ? WHERE ship_id = ?",
                  (new_integrity, ship_id))
    conn.commit()

    # Auto-record attacker as a known contact for the victim's owner
    victim_prefect = row['owner_prefect_id']
    if (victim_prefect and attacker_kind and attacker_id
            and attacker_kind in ('ship', 'starbase', 'port', 'outpost')):
        existing = conn.execute(
            """SELECT contact_id FROM known_contacts
               WHERE prefect_id = ? AND object_type = ? AND object_id = ?""",
            (victim_prefect, attacker_kind, attacker_id)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE known_contacts SET
                    location_col = ?, location_row = ?, location_system = ?,
                    discovered_turn_year = ?, discovered_turn_week = ?,
                    target_faction_id = ?, target_hull_type = ?,
                    target_ship_size = ?, detection_range = 0,
                    detected_on_tick = ?, detection_source = 'damage'
                   WHERE contact_id = ?""",
                (attacker_col, attacker_row, system_id,
                 turn_year, turn_week,
                 attacker_faction_id, attacker_hull_type, attacker_size,
                 tick, existing['contact_id'])
            )
        else:
            conn.execute(
                """INSERT INTO known_contacts
                   (prefect_id, object_type, object_id, object_name,
                    location_system, location_col, location_row,
                    discovered_turn_year, discovered_turn_week,
                    target_faction_id, target_hull_type,
                    target_ship_size, detection_range,
                    detected_on_tick, detection_source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'damage')""",
                (victim_prefect, attacker_kind, attacker_id, attacker_name,
                 system_id, attacker_col, attacker_row,
                 turn_year, turn_week,
                 attacker_faction_id, attacker_hull_type, attacker_size,
                 tick)
            )
        conn.commit()
    return new_integrity


def update_ship_position(conn, ship_id, col, row):
    conn.execute(
        "UPDATE ships SET grid_col = ?, grid_row = ? WHERE ship_id = ?",
        (col, row, ship_id)
    )
    conn.commit()


def end_engagement(conn, engagement_id, resolution_text, status='resolved'):
    """End an engagement and update all still-active participants' end integrity."""
    # First, snapshot end-of-engagement integrity for any still-active participants
    active = conn.execute(
        """SELECT p.participant_id, p.participant_kind, p.participant_id_value
           FROM combat_participants p
           WHERE p.engagement_id = ? AND p.status = 'active'""",
        (engagement_id,)
    ).fetchall()
    for p in active:
        # Look up current integrity
        if p['participant_kind'] == 'ship':
            row = conn.execute("SELECT integrity FROM ships WHERE ship_id = ?",
                                (p['participant_id_value'],)).fetchone()
            integ = row['integrity'] if row else None
        else:
            integ = 100.0  # bases don't track integrity in v1
        conn.execute(
            "UPDATE combat_participants SET integrity_at_end = ? WHERE participant_id = ?",
            (integ, p['participant_id'])
        )
    conn.execute(
        "UPDATE combat_engagements SET status = ?, resolution = ? WHERE engagement_id = ?",
        (status, resolution_text, engagement_id)
    )
    conn.commit()


def mark_participant_left(conn, participant_id, turn_year, turn_week,
                            round_number, status, integrity):
    conn.execute(
        """UPDATE combat_participants
           SET status = ?, left_turn_year = ?, left_turn_week = ?,
               left_on_round = ?, integrity_at_end = ?
           WHERE participant_id = ?""",
        (status, turn_year, turn_week, round_number, integrity, participant_id)
    )
    conn.commit()


def propagate_defend_responses(conn, game_id, engagement, turn_year, turn_week,
                                round_number):
    """
    For an active engagement, find any nearby (within DEFEND_RESPONSE_RANGE)
    ships and bases that have a defend list entry matching one of the
    current participants under attack. Add them as participants.

    Defenders join the engagement automatically. They will then act based
    on their own target list (and the now-known attacker as a hostile via
    damage-detection rule).
    """
    engagement_id = engagement['engagement_id']
    sys_id = engagement['system_id']
    eng_col = engagement['grid_col']
    eng_row = engagement['grid_row']

    # Get current participants in this engagement
    parts = conn.execute(
        """SELECT participant_kind, participant_id_value, owner_prefect_id, status
           FROM combat_participants WHERE engagement_id = ?""",
        (engagement_id,)
    ).fetchall()
    participant_keys = {(p['participant_kind'], p['participant_id_value']) for p in parts}

    # Build a list of "victims being attacked": participants who took damage
    # this turn in this engagement. (Anyone in the engagement counts as a
    # potential defendable victim — defenders trigger on attack on allies.)
    victim_set = set(participant_keys)

    # Find ships in same system within DEFEND_RESPONSE_RANGE of engagement loc
    candidate_ships = conn.execute(
        """SELECT s.*, pp.faction_id
           FROM ships s
           LEFT JOIN prefects pp ON s.owner_prefect_id = pp.prefect_id
           WHERE s.game_id = ? AND s.system_id = ?
             AND s.integrity > 0""",
        (game_id, sys_id)
    ).fetchall()

    for sh in candidate_ships:
        key = ('ship', sh['ship_id'])
        if key in participant_keys:
            continue  # already in
        d = grid_distance(eng_col, eng_row, sh['grid_col'], sh['grid_row'])
        if d > DEFEND_RESPONSE_RANGE:
            continue
        # Check if this ship's defend list matches any victim
        defend = get_ship_combat_lists(conn, game_id, sh['ship_id'])['defend']
        if not defend:
            continue
        match = False
        for vkind, vid in victim_set:
            if vkind == 'ship':
                vrow = conn.execute(
                    "SELECT s.ship_id, pp.faction_id FROM ships s "
                    "LEFT JOIN prefects pp ON s.owner_prefect_id = pp.prefect_id "
                    "WHERE s.ship_id = ?", (vid,)
                ).fetchone()
                vfaction = vrow['faction_id'] if vrow else None
            else:
                vfaction = None
                tbl_map = {'starbase': ('starbases', 'base_id'),
                           'port': ('surface_ports', 'port_id'),
                           'outpost': ('outposts', 'outpost_id')}
                tbl, idcol = tbl_map.get(vkind, (None, None))
                if tbl:
                    vrow = conn.execute(
                        f"SELECT owner_prefect_id FROM {tbl} WHERE {idcol} = ?",
                        (vid,)
                    ).fetchone()
                    if vrow and vrow['owner_prefect_id']:
                        f = conn.execute(
                            "SELECT faction_id FROM prefects WHERE prefect_id = ?",
                            (vrow['owner_prefect_id'],)
                        ).fetchone()
                        vfaction = f['faction_id'] if f else None
            if entity_matches_list(vkind, vid, vfaction, defend):
                match = True
                break
        if match:
            add_participant(
                conn, engagement_id, 'ship', sh['ship_id'],
                sh['owner_prefect_id'], turn_year, turn_week, round_number,
                sh['integrity'] or 100.0
            )
            log_combat_event(
                conn, engagement_id, turn_year, turn_week, round_number,
                'system', None, 'engage',
                detail=(f"{sh['name']} ({sh['ship_id']}) entered to defend ally "
                        f"(distance {d} cells)")
            )

    # Bases (starbases) — same logic but no movement
    candidate_bases = conn.execute(
        """SELECT * FROM starbases WHERE game_id = ? AND system_id = ?""",
        (game_id, sys_id)
    ).fetchall()
    for b in candidate_bases:
        key = ('starbase', b['base_id'])
        if key in participant_keys:
            continue
        d = grid_distance(eng_col, eng_row, b['grid_col'], b['grid_row'])
        if d > DEFEND_RESPONSE_RANGE:
            continue
        defend = get_base_combat_lists(conn, game_id, 'starbase', b['base_id'])['defend']
        if not defend:
            continue
        match = False
        for vkind, vid in victim_set:
            if vkind == 'ship':
                vrow = conn.execute(
                    "SELECT pp.faction_id FROM ships s "
                    "LEFT JOIN prefects pp ON s.owner_prefect_id = pp.prefect_id "
                    "WHERE s.ship_id = ?", (vid,)
                ).fetchone()
                vfaction = vrow['faction_id'] if vrow else None
            else:
                vfaction = None
            if entity_matches_list(vkind, vid, vfaction, defend):
                match = True
                break
        if match:
            add_participant(
                conn, engagement_id, 'starbase', b['base_id'],
                b['owner_prefect_id'], turn_year, turn_week, round_number,
                100.0
            )
            log_combat_event(
                conn, engagement_id, turn_year, turn_week, round_number,
                'system', None, 'engage',
                detail=(f"Starbase {b['name']} ({b['base_id']}) responded to defend "
                        f"ally (distance {d} cells)")
            )


def resolve_engagement_round(conn, game_id, engagement, round_number):
    """
    Run one round of combat for an engagement.
    Returns True if engagement is still active afterwards, False if ended.
    """
    engagement_id = engagement['engagement_id']
    turn_year = engagement['last_active_turn_year']
    turn_week = engagement['last_active_turn_week']

    # Defend list propagation: nearby allies with defend list matches
    # join the engagement before action this round
    propagate_defend_responses(conn, game_id, engagement, turn_year, turn_week,
                                round_number)

    participants_db = get_engagement_participants(conn, engagement_id, active_only=True)
    if len(participants_db) < 2:
        end_engagement(conn, engagement_id,
                        f"Ended round {round_number}: fewer than 2 active participants.",
                        status='resolved')
        return False

    # Build state for all active participants
    state_map = {}  # (kind, id) -> state
    p_record_map = {}  # (kind, id) -> participant_id (DB row id)
    for p in participants_db:
        s = get_participant_state(conn, game_id, p['participant_kind'],
                                    p['participant_id_value'])
        if s is None:
            continue
        state_map[(s['kind'], s['id'])] = s
        p_record_map[(s['kind'], s['id'])] = p['participant_id']

    if len(state_map) < 2:
        end_engagement(conn, engagement_id,
                        f"Ended round {round_number}: insufficient participants.",
                        status='resolved')
        return False

    actor_states = list(state_map.values())

    # Each actor decides and acts in sequence
    # (Order: by ship_id ascending for determinism — doesn't matter much)
    for actor in sorted(actor_states, key=lambda a: (a['kind'], a['id'])):
        actor_key = (actor['kind'], actor['id'])
        # Re-fetch actor state (may have been damaged earlier in round)
        fresh = get_participant_state(conn, game_id, actor['kind'], actor['id'])
        if fresh is None or fresh['integrity'] <= 0:
            continue
        actor.update(fresh)

        # Pick a target
        # Refresh other participants too (positions may have changed)
        others_state = []
        for k, _ in state_map.items():
            if k == actor_key:
                continue
            s2 = get_participant_state(conn, game_id, k[0], k[1])
            if s2 and s2['integrity'] > 0:
                others_state.append(s2)
        target = pick_combat_target(conn, game_id, actor, others_state,
                                       engagement_id=engagement_id)
        if not target:
            log_combat_event(conn, engagement_id, turn_year, turn_week,
                              round_number, actor['kind'], actor['id'], 'hold',
                              detail='no valid target on lists')
            continue

        action, payload = decide_action(conn, game_id, actor, target, others_state)

        if action == 'fire':
            tgt = payload
            # Sum total damage from all weapons, scaled by shots_per_round
            if actor['kind'] == 'ship':
                total_dmg = 0
                weapon_names = []
                for wname, wdmg, wrange, wshots, wqty in actor.get('weapons', []):
                    dist = grid_distance(actor['col'], actor['row'], tgt['col'], tgt['row'])
                    if dist > wrange or wdmg <= 0 or wshots <= 0:
                        continue
                    weapon_total = wdmg * wshots * wqty
                    total_dmg += weapon_total
                    weapon_names.append(f"{wname}x{wqty}")
                if total_dmg <= 0:
                    log_combat_event(conn, engagement_id, turn_year, turn_week,
                                      round_number, actor['kind'], actor['id'],
                                      'hold',
                                      detail='no weapons in range')
                    continue
                weapon_summary = ', '.join(weapon_names)
            else:
                # Base
                total_dmg = actor.get('base_weapon_damage', 0)
                weapon_summary = 'turrets'
                if total_dmg <= 0:
                    log_combat_event(conn, engagement_id, turn_year, turn_week,
                                      round_number, actor['kind'], actor['id'],
                                      'hold', detail='no weapons')
                    continue

            # Apply damage to target
            if tgt['kind'] == 'ship':
                new_integ = apply_damage_to_ship(
                    conn, tgt['id'], total_dmg,
                    attacker_kind=actor['kind'],
                    attacker_id=actor['id'],
                    attacker_name=actor.get('name'),
                    attacker_faction_id=actor.get('faction_id'),
                    attacker_hull_type=actor.get('hull_type'),
                    attacker_size=actor.get('ship_size'),
                    attacker_col=actor['col'],
                    attacker_row=actor['row'],
                    system_id=actor.get('system_id'),
                    turn_year=turn_year,
                    turn_week=turn_week,
                    tick=round_number,
                )
                log_combat_event(conn, engagement_id, turn_year, turn_week,
                                  round_number, actor['kind'], actor['id'], 'fire',
                                  target_kind=tgt['kind'], target_id=tgt['id'],
                                  damage=total_dmg, integrity_after=new_integ,
                                  detail=f"fired {weapon_summary} ({int(total_dmg)} dmg) at {tgt['name']}")
                # Check for destruction
                if new_integ <= 0:
                    log_combat_event(conn, engagement_id, turn_year, turn_week,
                                      round_number, tgt['kind'], tgt['id'],
                                      'destroyed',
                                      detail=f"{tgt['name']} destroyed by {actor['name']}")
                    pid = p_record_map.get((tgt['kind'], tgt['id']))
                    if pid:
                        mark_participant_left(conn, pid, turn_year, turn_week,
                                                round_number, 'destroyed', 0)
            else:
                # Bases don't take damage in v1 — log the attempt but no effect
                log_combat_event(conn, engagement_id, turn_year, turn_week,
                                  round_number, actor['kind'], actor['id'], 'fire',
                                  target_kind=tgt['kind'], target_id=tgt['id'],
                                  damage=0, integrity_after=100,
                                  detail=f"fired at {tgt['name']} (bases invulnerable in v1)")

        elif action == 'move':
            dest_col, dest_row = payload
            if actor['kind'] == 'ship':
                # Move 1 cell (or 2 if speed bonus)
                steps = actor.get('movement', 1)
                cur_col, cur_row = actor['col'], actor['row']
                for _ in range(steps):
                    nxt = _step(
                        {'col': cur_col, 'row': cur_row},
                        {'col': dest_col, 'row': dest_row},
                        +1
                    )
                    if not nxt:
                        break
                    cur_col, cur_row = nxt
                update_ship_position(conn, actor['id'], cur_col, cur_row)
                log_combat_event(conn, engagement_id, turn_year, turn_week,
                                  round_number, actor['kind'], actor['id'], 'move',
                                  detail=f"moved to {cur_col}{cur_row:02d}")

        elif action == 'evade':
            log_combat_event(conn, engagement_id, turn_year, turn_week,
                              round_number, actor['kind'], actor['id'], 'evade',
                              detail='evading')

        elif action == 'hold':
            log_combat_event(conn, engagement_id, turn_year, turn_week,
                              round_number, actor['kind'], actor['id'], 'hold',
                              detail='holding position')

    # Check end conditions: anyone still in detection range of anyone hostile?
    remaining = get_engagement_participants(conn, engagement_id, active_only=True)
    if len(remaining) < 2:
        end_engagement(conn, engagement_id,
                        f"Ended round {round_number}: fewer than 2 active participants.",
                        status='resolved')
        return False

    # Check if any hostile pair is still within detection range (PASSIVE_SCAN_RANGE)
    states_now = []
    for p in remaining:
        s = get_participant_state(conn, game_id, p['participant_kind'],
                                    p['participant_id_value'])
        if s:
            states_now.append((s, p['participant_id']))

    in_contact = False
    for i, (s1, _) in enumerate(states_now):
        for s2, _ in states_now[i+1:]:
            if s1['col'] is None or s2['col'] is None:
                continue
            d = grid_distance(s1['col'], s1['row'], s2['col'], s2['row'])
            if d <= PASSIVE_SCAN_RANGE:
                in_contact = True
                break
        if in_contact:
            break

    if not in_contact:
        # Mark fled participants
        for s, pid in states_now:
            mark_participant_left(conn, pid,
                                    turn_year, turn_week, round_number,
                                    'fled', s['integrity'])
        end_engagement(conn, engagement_id,
                        f"Ended round {round_number}: all participants out of contact.",
                        status='resolved')
        return False

    return True


def resolve_active_engagements(conn, game_id, turn_year, turn_week):
    """
    Top-level: run up to ROUNDS_PER_TURN rounds for every active engagement.
    Called once per turn from the run-turn pipeline before normal resolution.
    Returns a list of summary dicts for reporting.
    """
    summaries = []
    engagements = get_active_engagements(conn, game_id)
    for eng in engagements:
        engagement_id = eng['engagement_id']
        # Update last-active to current turn before running rounds
        conn.execute(
            "UPDATE combat_engagements SET last_active_turn_year = ?, last_active_turn_week = ? "
            "WHERE engagement_id = ?",
            (turn_year, turn_week, engagement_id)
        )
        conn.commit()
        eng_now = conn.execute(
            "SELECT * FROM combat_engagements WHERE engagement_id = ?",
            (engagement_id,)
        ).fetchone()

        rounds_run = 0
        for r in range(1, ROUNDS_PER_TURN + 1):
            still_active = resolve_engagement_round(conn, game_id, eng_now, r)
            rounds_run += 1
            if not still_active:
                break

        summaries.append({
            'engagement_id': engagement_id,
            'rounds_run': rounds_run,
            'system_id': eng['system_id'],
            'grid_col': eng['grid_col'],
            'grid_row': eng['grid_row'],
        })

    return summaries

