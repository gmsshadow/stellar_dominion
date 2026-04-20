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
    """Return a list of weapon dicts for installed weapons.
    Each dict: name, damage, range, shots_per_round, qty, accuracy,
    ammo_type (None for beams, 'missile'/'torpedo' for launchers),
    flight_rounds (0 for instant, 1/2 for projectiles).
    """
    rows = conn.execute(
        """SELECT sc.name, sc.weapon_damage, sc.weapon_range,
                  sc.weapon_shots_per_round, sc.weapon_accuracy,
                  sc.ammo_type, sc.flight_rounds, ii.quantity
           FROM installed_items ii
           JOIN ship_components sc ON ii.component_id = sc.component_id
           WHERE ii.ship_id = ? AND sc.category = 'weapon'""",
        (ship_id,)
    ).fetchall()
    return [{
        'name': r['name'],
        'damage': r['weapon_damage'] or 0,
        'range': r['weapon_range'] or 0,
        'shots_per_round': r['weapon_shots_per_round'] or 0,
        'qty': r['quantity'],
        'accuracy': r['weapon_accuracy'] if r['weapon_accuracy'] is not None else 1.0,
        'ammo_type': r['ammo_type'],
        'flight_rounds': r['flight_rounds'] or 0,
    } for r in rows]


def get_ship_pd(conn, ship_id):
    """Return installed Point Defence components for a ship.
    Each element: (name, shots_per_round, qty, accuracy).
    """
    rows = conn.execute(
        """SELECT sc.name, sc.weapon_shots_per_round, sc.weapon_accuracy,
                  ii.quantity
           FROM installed_items ii
           JOIN ship_components sc ON ii.component_id = sc.component_id
           WHERE ii.ship_id = ? AND sc.category = 'pd'""",
        (ship_id,)
    ).fetchall()
    return [(r['name'], r['weapon_shots_per_round'] or 0, r['quantity'],
             r['weapon_accuracy'] if r['weapon_accuracy'] is not None else 1.0)
            for r in rows]


def get_ship_ammo(conn, ship_id):
    """Return (missiles_loaded, torpedoes_loaded) for a ship."""
    row = conn.execute(
        "SELECT missiles_loaded, torpedoes_loaded FROM ships WHERE ship_id = ?",
        (ship_id,)
    ).fetchone()
    if not row:
        return (0, 0)
    return (row['missiles_loaded'] or 0, row['torpedoes_loaded'] or 0)


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
        pd_components = get_ship_pd(conn, entity_id)
        missiles_loaded, torpedoes_loaded = get_ship_ammo(conn, entity_id)
        integ = row['integrity'] if row['integrity'] is not None else 100.0
        # max_integrity scales with ship_size. Fall back to ship_size if the
        # column is somehow null (e.g. pre-migration rows).
        max_integ = row['max_integrity'] if 'max_integrity' in row.keys() and row['max_integrity'] else (row['ship_size'] or 50)
        return {
            'kind': 'ship', 'id': entity_id,
            'name': row['name'], 'faction_id': row['faction_id'],
            'col': row['grid_col'], 'row': row['grid_row'],
            'system_id': row['system_id'],
            'integrity': integ,
            'max_integrity': float(max_integ),
            'doctrine': row['combat_doctrine'] or 'defensive',
            'gravity_rating': row['gravity_rating'] or 1.0,
            'sensor_rating': row['sensor_rating'] or 0,
            'sensor_profile': row['sensor_profile'] or 0.5,
            'ship_size': row['ship_size'] or 50,
            'hull_type': row['hull_type'],
            'owner_prefect_id': row['owner_prefect_id'],
            'weapons': weapons,
            'pd': pd_components,
            'missiles_loaded': missiles_loaded,
            'torpedoes_loaded': torpedoes_loaded,
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
            'max_integrity': 100.0,
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
    max_integrity = actor.get('max_integrity') or 100
    if max_integrity <= 0:
        max_integrity = 100
    integrity_pct = (integrity / max_integrity) * 100.0
    doctrine = actor.get('doctrine', 'defensive') or 'defensive'
    retreat_threshold = doctrine_retreat_threshold(doctrine)

    # If integrity percentage below retreat threshold, try to flee
    if integrity_pct <= retreat_threshold and actor['kind'] == 'ship':
        # Move directly away from target
        dest = _step_away(actor, target)
        if dest:
            return ('move', dest)
        # Cornered (can't move, e.g. same cell as enemy or grid edge)
        # Fight back rather than just evade — at least take some with us.
        dist = grid_distance(actor['col'], actor['row'], target['col'], target['row'])
        max_wrange = max((w['range'] for w in actor.get('weapons', [])), default=0)
        if max_wrange > 0 and dist <= max_wrange:
            return ('fire', target)
        return ('evade', None)

    # Calculate range to target
    dist = grid_distance(actor['col'], actor['row'], target['col'], target['row'])

    # Determine effective max weapon range
    if actor['kind'] == 'ship':
        max_wrange = max((w['range'] for w in actor.get('weapons', [])), default=0)
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


def _shield_thickness(sp, ship_size):
    """Compute shield thickness from current SP and ship size, using global factor."""
    from db.database import SHIELD_THICKNESS_FACTOR
    if ship_size <= 0 or sp <= 0:
        return 0
    return (SHIELD_THICKNESS_FACTOR * sp) // ship_size


def apply_damage_to_ship(conn, ship_id, damage, attacker_kind=None,
                          attacker_id=None, attacker_name=None,
                          attacker_faction_id=None,
                          attacker_hull_type=None, attacker_size=None,
                          attacker_col=None, attacker_row=None,
                          system_id=None, turn_year=None, turn_week=None,
                          tick=None):
    """
    Apply damage to a ship. Damage resolves in this order:
      1. Shields absorb up to `thickness` damage. Absorbed damage depletes
         total SP by the same amount, and thickness is recomputed on every
         hit (floor(current_SP / ship_size), no minimum).
      2. Armour reduces the remaining damage by a flat amount (non-ablative).
      3. Whatever's left reduces integrity.

    Damage = perfect detection: taking any hit (even a fully-absorbed one)
    records the attacker as a known contact.

    Returns a dict with details:
      {'new_integrity': float, 'absorbed_by_shields': int,
       'absorbed_by_armour': int, 'damage_through': int,
       'shield_sp_after': int, 'shield_thickness_after': int}
    """
    row = conn.execute(
        """SELECT integrity, owner_prefect_id, ship_size,
                  armour, shield_sp, max_shield_sp
           FROM ships WHERE ship_id = ?""",
        (ship_id,)
    ).fetchone()
    if not row:
        return {
            'new_integrity': 0, 'absorbed_by_shields': 0,
            'absorbed_by_armour': 0, 'damage_through': 0,
            'shield_sp_after': 0, 'shield_thickness_after': 0,
        }

    raw = max(0, int(damage))
    remaining = raw
    ship_size = row['ship_size'] or 50
    current_sp = row['shield_sp'] or 0
    armour = row['armour'] or 0

    # --- 1. Shields (ablative, recompute thickness per hit) ---
    absorbed_by_shields = 0
    if current_sp > 0 and ship_size > 0:
        # Thickness = floor(FACTOR × current_SP / ship_size), no minimum
        thickness = _shield_thickness(current_sp, ship_size)
        if thickness > 0:
            absorbed_by_shields = min(thickness, remaining)
            remaining -= absorbed_by_shields
            current_sp = max(0, current_sp - absorbed_by_shields)

    # --- 2. Armour (flat reduction, non-ablative) ---
    absorbed_by_armour = 0
    if armour > 0 and remaining > 0:
        absorbed_by_armour = min(armour, remaining)
        remaining -= absorbed_by_armour

    # --- 3. Integrity takes the rest ---
    damage_through = remaining
    new_integrity = max(0, (row['integrity'] or 100) - damage_through)

    # Write back: integrity, shield_sp (armour is unchanged)
    conn.execute(
        "UPDATE ships SET integrity = ?, shield_sp = ? WHERE ship_id = ?",
        (new_integrity, current_sp, ship_id)
    )
    conn.commit()

    # Shield thickness AFTER this hit, for reporting
    new_thickness = _shield_thickness(current_sp, ship_size)

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
    return {
        'new_integrity': new_integrity,
        'absorbed_by_shields': absorbed_by_shields,
        'absorbed_by_armour': absorbed_by_armour,
        'damage_through': damage_through,
        'shield_sp_after': current_sp,
        'shield_thickness_after': new_thickness,
    }


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


def resolve_arriving_projectiles(conn, game_id, engagement_id, round_number,
                                    turn_year, turn_week, p_record_map):
    """
    Resolve all projectiles with arrives_on_round == round_number. For each:
      1. Find the target's PD; compute PD shots budget
      2. Sort projectiles by damage descending (torpedoes first)
      3. For each projectile: PD rolls to intercept (stops at first hit),
         then if not intercepted, projectile rolls accuracy,
         then applies damage through shield/armour/integrity pipeline
      4. Update projectile status; mark participant destroyed if hit kills

    Returns a list of summary dicts (one per target) for later logging.
    """
    import random
    proj_rows = conn.execute(
        """SELECT * FROM combat_projectiles
           WHERE engagement_id = ? AND status = 'in-flight'
                 AND arrives_on_round = ?
           ORDER BY damage DESC, projectile_id""",
        (engagement_id, round_number)
    ).fetchall()
    if not proj_rows:
        return

    # Group projectiles by target. For each target we'll:
    #   - Find PD on that target ship and compute a pool of PD shots
    #   - Sort that target's incoming projectiles by damage desc (torpedoes first)
    #   - Apply PD shots per projectile; each PD shot is an independent intercept roll
    #   - Any missile surviving PD rolls its own accuracy, then deals damage
    targets = {}
    for p in proj_rows:
        tgt_key = (p['target_kind'], p['target_id'])
        targets.setdefault(tgt_key, []).append(p)

    for (tgt_kind, tgt_id), incoming in targets.items():
        # PD only defends ships (bases don't take damage in v1 either way)
        if tgt_kind != 'ship':
            # Resolve directly: no PD, just accuracy check, but bases take no damage
            for p in incoming:
                status = 'hit' if random.random() <= p['accuracy'] else 'missed'
                conn.execute(
                    "UPDATE combat_projectiles SET status = ? WHERE projectile_id = ?",
                    (status, p['projectile_id'])
                )
                log_combat_event(
                    conn, engagement_id, turn_year, turn_week, round_number,
                    'projectile', None, 'impact',
                    target_kind=tgt_kind, target_id=tgt_id,
                    detail=(f"{p['ammo_type']} from {p['attacker_name']} "
                             f"{'struck' if status == 'hit' else 'missed'} "
                             f"{tgt_kind} (bases invulnerable)")
                )
            continue

        # Target is a ship — fetch its PD loadout and current state
        target_state = get_participant_state(conn, game_id, tgt_kind, tgt_id)
        if not target_state or target_state['integrity'] <= 0:
            # Target is already destroyed — remaining projectiles wasted
            for p in incoming:
                conn.execute(
                    "UPDATE combat_projectiles SET status = 'missed' "
                    "WHERE projectile_id = ?",
                    (p['projectile_id'],)
                )
                log_combat_event(
                    conn, engagement_id, turn_year, turn_week, round_number,
                    'projectile', None, 'impact',
                    target_kind=tgt_kind, target_id=tgt_id,
                    detail=(f"{p['ammo_type']} wasted — {target_state['name'] if target_state else 'target'} already destroyed")
                )
            continue

        pd_list = target_state.get('pd', [])
        # Build flat list of (accuracy, 1) for each PD shot the target can fire
        pd_shots_remaining = []
        for pd_name, pd_shots, pd_qty, pd_acc in pd_list:
            total_pd_shots = pd_shots * pd_qty
            pd_shots_remaining.extend([(pd_name, pd_acc)] * total_pd_shots)

        # Sort incoming by damage desc so torpedoes get prioritized
        incoming.sort(key=lambda p: (-p['damage'], p['projectile_id']))

        # Per-target summary tracking
        target_summary = {
            'target_name': target_state['name'],
            'max_integrity': int(target_state.get('max_integrity', 0) or 0),
            'incoming_count': len(incoming),
            'missiles_in': sum(1 for p in incoming if p['ammo_type'] == 'missile'),
            'torps_in': sum(1 for p in incoming if p['ammo_type'] == 'torpedo'),
            'intercepted': 0,
            'missiles_intercepted': 0,
            'torps_intercepted': 0,
            'pd_shots_total': len(pd_shots_remaining),
            'pd_shots_used': 0,
            'pd_hits': 0,
            'hits': 0,
            'misses': 0,
            'wasted': 0,
            'missiles_hit': 0,
            'torps_hit': 0,
            'total_shield_absorbed': 0,
            'total_armour_absorbed': 0,
            'total_hull_damage': 0,
            'final_integrity': target_state['integrity'],
            'final_sp': target_state.get('shield_sp', 0) or 0,
            'final_thk': 0,
        }
        tgt_size = target_state.get('ship_size') or 0
        target_destroyed = False

        for p in incoming:
            if target_destroyed:
                # Target dead — remaining projectiles wasted
                conn.execute(
                    "UPDATE combat_projectiles SET status = 'missed' "
                    "WHERE projectile_id = ?",
                    (p['projectile_id'],)
                )
                target_summary['wasted'] += 1
                continue

            # PD intercept: each PD shot is a sequential chance to kill the
            # projectile. Stops at first successful hit.
            intercepted = False
            for _ in range(len(pd_shots_remaining)):
                if not pd_shots_remaining:
                    break
                pd_name, pd_acc = pd_shots_remaining.pop(0)
                target_summary['pd_shots_used'] += 1
                if random.random() <= pd_acc:
                    # PD hits — projectile destroyed
                    target_summary['pd_hits'] += 1
                    intercepted = True
                    break

            if intercepted:
                conn.execute(
                    "UPDATE combat_projectiles SET status = 'intercepted' "
                    "WHERE projectile_id = ?",
                    (p['projectile_id'],)
                )
                target_summary['intercepted'] += 1
                if p['ammo_type'] == 'missile':
                    target_summary['missiles_intercepted'] += 1
                else:
                    target_summary['torps_intercepted'] += 1
                continue

            # Not intercepted — roll projectile accuracy
            if random.random() > p['accuracy']:
                conn.execute(
                    "UPDATE combat_projectiles SET status = 'missed' "
                    "WHERE projectile_id = ?",
                    (p['projectile_id'],)
                )
                target_summary['misses'] += 1
                continue

            # Hit! Apply damage through the normal pipeline
            dmg_result = apply_damage_to_ship(
                conn, tgt_id, p['damage'],
                attacker_kind=p['attacker_kind'],
                attacker_id=p['attacker_id'],
                attacker_name=p['attacker_name'],
                attacker_col=None, attacker_row=None,
                turn_year=turn_year, turn_week=turn_week,
                tick=round_number,
            )
            conn.execute(
                "UPDATE combat_projectiles SET status = 'hit' "
                "WHERE projectile_id = ?",
                (p['projectile_id'],)
            )
            target_summary['hits'] += 1
            if p['ammo_type'] == 'missile':
                target_summary['missiles_hit'] += 1
            else:
                target_summary['torps_hit'] += 1
            target_summary['total_shield_absorbed'] += dmg_result['absorbed_by_shields']
            target_summary['total_armour_absorbed'] += dmg_result['absorbed_by_armour']
            target_summary['total_hull_damage'] += dmg_result['damage_through']
            target_summary['final_integrity'] = dmg_result['new_integrity']
            target_summary['final_sp'] = dmg_result['shield_sp_after']
            target_summary['final_thk'] = dmg_result['shield_thickness_after']
            if dmg_result['new_integrity'] <= 0:
                target_destroyed = True
                log_combat_event(
                    conn, engagement_id, turn_year, turn_week, round_number,
                    tgt_kind, tgt_id, 'destroyed',
                    detail=f"{target_state['name']} destroyed by incoming {p['ammo_type']} from {p['attacker_name']}"
                )
                pid = p_record_map.get((tgt_kind, tgt_id))
                if pid:
                    mark_participant_left(conn, pid, turn_year, turn_week,
                                            round_number, 'destroyed', 0)

        # Build and log the impact summary
        s = target_summary
        bits = []
        # Incoming count
        in_bits = []
        if s['missiles_in']:
            in_bits.append(f"{s['missiles_in']} missile{'s' if s['missiles_in'] != 1 else ''}")
        if s['torps_in']:
            in_bits.append(f"{s['torps_in']} torpedo{'es' if s['torps_in'] != 1 else ''}")
        incoming_desc = " + ".join(in_bits)
        # PD summary
        pd_bits = []
        if s['pd_shots_total']:
            pd_bits.append(
                f"PD fired {s['pd_shots_used']}/{s['pd_shots_total']} shots, "
                f"intercepted {s['intercepted']}")
        else:
            if s['incoming_count'] > 0:
                pd_bits.append("no PD defence")
        # Hits/misses
        outcome_bits = [f"{s['hits']} hit{'s' if s['hits'] != 1 else ''}"]
        if s['misses']:
            outcome_bits.append(f"{s['misses']} miss{'es' if s['misses'] != 1 else ''}")
        if s['wasted']:
            outcome_bits.append(f"{s['wasted']} wasted")
        bits.append(f"{incoming_desc} arrived @ {s['target_name']}")
        if pd_bits:
            bits.append(" | ".join(pd_bits))
        bits.append(", ".join(outcome_bits))
        # Defence breakdown
        defence_bits = []
        if s['total_shield_absorbed'] > 0:
            defence_bits.append(
                f"shields absorbed {s['total_shield_absorbed']} "
                f"(SP now {s['final_sp']}, thk {s['final_thk']})")
        if s['total_armour_absorbed'] > 0:
            defence_bits.append(f"armour absorbed {s['total_armour_absorbed']}")
        if defence_bits:
            bits.append("[" + ", ".join(defence_bits) + "]")
        bits.append(f"hull -{s['total_hull_damage']}, "
                     f"hull {int(s['final_integrity'])}/{s['max_integrity']}")
        detail = " — ".join(bits)
        log_combat_event(
            conn, engagement_id, turn_year, turn_week, round_number,
            'projectile', None, 'impact',
            target_kind=tgt_kind, target_id=tgt_id,
            damage=s['total_hull_damage'],
            integrity_after=s['final_integrity'],
            detail=detail
        )
    conn.commit()


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

    # Resolve arriving projectiles BEFORE the actor loop. These were launched
    # in previous rounds and arrive this round. PD fires automatically,
    # destruction propagates into participant state for the actor loop below.
    resolve_arriving_projectiles(conn, game_id, engagement_id, round_number,
                                   turn_year, turn_week, p_record_map)

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
            dist = grid_distance(actor['col'], actor['row'], tgt['col'], tgt['row'])
            MAX_COMBAT_ROUNDS = 6  # must match the turn-round cap elsewhere

            # Separate weapons into beams (instant) and launchers (projectile).
            # Launchers create projectile records that resolve in a later round.
            beam_shots = []          # list of (weapon, damage, accuracy)
            launch_groups = []       # list of (weapon, damage, accuracy, ammo_type, flight_rounds, n_shots, wqty)
            weapon_label_bits = []
            ammo_depleted_bits = []  # track "wanted to fire but no ammo loaded"
            overflow_bits = []       # track "wanted to fire but too late in turn"
            if actor['kind'] == 'ship':
                missiles_left = actor.get('missiles_loaded', 0) or 0
                torpedoes_left = actor.get('torpedoes_loaded', 0) or 0

                for w in actor.get('weapons', []):
                    wname, wdmg, wrange = w['name'], w['damage'], w['range']
                    wshots, wqty, wacc = w['shots_per_round'], w['qty'], w['accuracy']
                    ammo_t = w.get('ammo_type')
                    flight = w.get('flight_rounds', 0) or 0
                    if dist > wrange or wdmg <= 0 or wshots <= 0 or wqty <= 0:
                        continue
                    n_shots = wshots * wqty

                    if ammo_t is None or flight == 0:
                        # Beam weapon — instant
                        weapon_label_bits.append(f"{wname}x{wqty}")
                        for _ in range(n_shots):
                            beam_shots.append((wname, wdmg, wacc))
                    else:
                        # Launcher — check late-turn prevention: projectile
                        # must arrive by round MAX_COMBAT_ROUNDS
                        arrives = round_number + flight
                        if arrives > MAX_COMBAT_ROUNDS:
                            overflow_bits.append(f"{wname}x{wqty} (arrives R{arrives})")
                            continue
                        # Check ammo availability
                        if ammo_t == 'missile':
                            available = min(n_shots, missiles_left)
                        elif ammo_t == 'torpedo':
                            available = min(n_shots, torpedoes_left)
                        else:
                            available = 0
                        if available <= 0:
                            ammo_depleted_bits.append(f"{wname}x{wqty} (no {ammo_t}s)")
                            continue
                        # Deplete ammo pool (for this round's decisions only;
                        # DB update happens after the launch loop)
                        if ammo_t == 'missile':
                            missiles_left -= available
                        elif ammo_t == 'torpedo':
                            torpedoes_left -= available
                        weapon_label_bits.append(f"{wname}x{wqty}")
                        launch_groups.append({
                            'name': wname, 'damage': wdmg, 'accuracy': wacc,
                            'ammo_type': ammo_t, 'flight_rounds': flight,
                            'n_shots': available, 'wqty': wqty,
                            'arrives_on_round': arrives,
                        })

                weapon_summary = ', '.join(weapon_label_bits) if weapon_label_bits else 'no weapons'
            else:
                # Base: one "shot" per round using base_weapon_damage.
                # Bases don't track per-weapon accuracy in v1; assume 1.0.
                bwd = actor.get('base_weapon_damage', 0)
                weapon_summary = 'turrets'
                if bwd > 0:
                    beam_shots.append(('turrets', bwd, 1.0))

            if not beam_shots and not launch_groups:
                # Nothing to fire. If it's because ammo is out, note that.
                notes = []
                if ammo_depleted_bits:
                    notes.append("ammo depleted: " + ", ".join(ammo_depleted_bits))
                if overflow_bits:
                    notes.append("skipped (late-turn): " + ", ".join(overflow_bits))
                detail = "no weapons in range" if not notes else "; ".join(notes)
                log_combat_event(conn, engagement_id, turn_year, turn_week,
                                  round_number, actor['kind'], actor['id'],
                                  'hold',
                                  detail=detail)
                continue

            # --- Launch projectiles (missiles/torpedoes) ---
            # These create combat_projectiles rows and deplete ammo. They do
            # NOT deal damage this round — they resolve on the arrival round.
            if launch_groups and tgt['kind'] == 'ship':
                # Summarize: total missiles and torpedoes launched
                total_missiles_launched = 0
                total_torpedoes_launched = 0
                arrival_rounds = set()
                for lg in launch_groups:
                    for _ in range(lg['n_shots']):
                        conn.execute(
                            """INSERT INTO combat_projectiles
                                (engagement_id, launched_turn_year, launched_turn_week,
                                 launched_on_round, arrives_on_round,
                                 attacker_kind, attacker_id, attacker_name,
                                 target_kind, target_id,
                                 damage, accuracy, ammo_type, status)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'in-flight')""",
                            (engagement_id, turn_year, turn_week,
                             round_number, lg['arrives_on_round'],
                             actor['kind'], actor['id'], actor.get('name'),
                             tgt['kind'], tgt['id'],
                             lg['damage'], lg['accuracy'], lg['ammo_type'])
                        )
                    if lg['ammo_type'] == 'missile':
                        total_missiles_launched += lg['n_shots']
                    else:
                        total_torpedoes_launched += lg['n_shots']
                    arrival_rounds.add(lg['arrives_on_round'])
                # Deplete ammo on the ship record
                if total_missiles_launched > 0:
                    conn.execute(
                        "UPDATE ships SET missiles_loaded = MAX(0, missiles_loaded - ?) "
                        "WHERE ship_id = ?",
                        (total_missiles_launched, actor['id'])
                    )
                if total_torpedoes_launched > 0:
                    conn.execute(
                        "UPDATE ships SET torpedoes_loaded = MAX(0, torpedoes_loaded - ?) "
                        "WHERE ship_id = ?",
                        (total_torpedoes_launched, actor['id'])
                    )
                conn.commit()
                # Log the launch as a single summary event
                launch_bits = []
                if total_missiles_launched:
                    launch_bits.append(f"{total_missiles_launched} missile{'s' if total_missiles_launched != 1 else ''}")
                if total_torpedoes_launched:
                    launch_bits.append(f"{total_torpedoes_launched} torpedo{'es' if total_torpedoes_launched != 1 else ''}")
                arrival_str = ", ".join(f"R{r}" for r in sorted(arrival_rounds))
                detail = (f"launched {' + '.join(launch_bits)} @ {tgt['name']} "
                           f"(arrive {arrival_str})")
                log_combat_event(conn, engagement_id, turn_year, turn_week,
                                  round_number, actor['kind'], actor['id'], 'launch',
                                  target_kind=tgt['kind'], target_id=tgt['id'],
                                  detail=detail)

            # --- Resolve beam shots (instant) ---
            if not beam_shots:
                continue

            # Now resolve per shot against the target
            if tgt['kind'] == 'ship':
                total_fired = len(beam_shots)
                hits = 0
                misses = 0
                total_shield_absorbed = 0
                total_armour_absorbed = 0
                total_hull_damage = 0
                final_integ = tgt['integrity']
                target_destroyed = False

                # Pull initial target SP and ship_size for the "started with" note
                tgt_size = tgt.get('ship_size') or 0
                tgt_max_int = int(tgt.get('max_integrity', 0) or 0)

                for wname, wdmg, wacc in beam_shots:
                    if target_destroyed:
                        # Remaining shots wasted — target already dead
                        break
                    # Accuracy roll
                    if random.random() > wacc:
                        misses += 1
                        continue
                    hits += 1
                    dmg_result = apply_damage_to_ship(
                        conn, tgt['id'], wdmg,
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
                    total_shield_absorbed += dmg_result['absorbed_by_shields']
                    total_armour_absorbed += dmg_result['absorbed_by_armour']
                    total_hull_damage += dmg_result['damage_through']
                    final_integ = dmg_result['new_integrity']
                    final_sp = dmg_result['shield_sp_after']
                    final_thk = dmg_result['shield_thickness_after']
                    if final_integ <= 0:
                        target_destroyed = True

                wasted = total_fired - hits - misses

                # Fetch final SP for the summary line (in case there were no hits at all)
                if hits == 0:
                    sp_row = conn.execute(
                        "SELECT shield_sp FROM ships WHERE ship_id = ?",
                        (tgt['id'],)
                    ).fetchone()
                    final_sp = sp_row['shield_sp'] if sp_row else 0
                    final_thk = _shield_thickness(final_sp, tgt_size) if tgt_size else 0

                # Build summary detail
                hit_bits = [f"{hits} hit{'s' if hits != 1 else ''}"]
                if misses:
                    hit_bits.append(f"{misses} miss{'es' if misses != 1 else ''}")
                if wasted:
                    hit_bits.append(f"{wasted} wasted")
                # Beam-only weapon list for the summary
                beam_label_bits = []
                for w in actor.get('weapons', []):
                    if (w.get('ammo_type') is None or (w.get('flight_rounds', 0) or 0) == 0) and w['damage'] > 0 and dist <= w['range']:
                        beam_label_bits.append(f"{w['name']}x{w['qty']}")
                beam_summary = ', '.join(beam_label_bits) if beam_label_bits else weapon_summary
                summary_parts = [
                    f"{total_fired} shot{'s' if total_fired != 1 else ''} "
                    f"[{beam_summary}] @ {tgt['name']} ({', '.join(hit_bits)})"
                ]
                defence_bits = []
                if total_shield_absorbed > 0:
                    defence_bits.append(
                        f"shields absorbed {total_shield_absorbed} "
                        f"(SP now {final_sp}, thk {final_thk})")
                if total_armour_absorbed > 0:
                    defence_bits.append(
                        f"armour absorbed {total_armour_absorbed}")
                if defence_bits:
                    summary_parts.append("[" + ", ".join(defence_bits) + "]")
                summary_parts.append(
                    f"hull -{total_hull_damage}, hull {int(final_integ)}/{tgt_max_int}"
                )
                detail_str = " — ".join(summary_parts)
                log_combat_event(conn, engagement_id, turn_year, turn_week,
                                  round_number, actor['kind'], actor['id'], 'fire',
                                  target_kind=tgt['kind'], target_id=tgt['id'],
                                  damage=total_hull_damage,
                                  integrity_after=final_integ,
                                  detail=detail_str)
                if target_destroyed:
                    log_combat_event(conn, engagement_id, turn_year, turn_week,
                                      round_number, tgt['kind'], tgt['id'],
                                      'destroyed',
                                      detail=f"{tgt['name']} destroyed by {actor['name']}")
                    pid = p_record_map.get((tgt['kind'], tgt['id']))
                    if pid:
                        mark_participant_left(conn, pid, turn_year, turn_week,
                                                round_number, 'destroyed', 0)
            else:
                # Bases don't take damage in v1
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

        # End-of-turn: any projectiles still 'in-flight' are expired.
        # Under Option D (late-turn launch prevention), this should be
        # an empty set unless a bug let one slip through.
        conn.execute(
            """UPDATE combat_projectiles SET status = 'expired'
               WHERE engagement_id = ? AND status = 'in-flight'""",
            (engagement_id,)
        )
        conn.commit()

        summaries.append({
            'engagement_id': engagement_id,
            'rounds_run': rounds_run,
            'system_id': eng['system_id'],
            'grid_col': eng['grid_col'],
            'grid_row': eng['grid_row'],
        })

    return summaries

