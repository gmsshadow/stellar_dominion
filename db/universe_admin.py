"""
Stellar Dominion - Universe Administration
CLI helpers for adding/editing universe content (systems, bodies, links).
Works directly on universe.db — no game state interaction needed.
"""

from db.database import get_universe_connection, UNIVERSE_DB_PATH
from pathlib import Path


def add_system(universe_db_path=None, system_id=None, name=None,
               star_name=None, spectral_type='G2V',
               star_col='M', star_row=13, created_turn=None):
    """
    Add a new star system to the universe.
    
    If system_id is None, auto-assigns the next available ID.
    Returns the system_id.
    """
    conn = get_universe_connection(universe_db_path)

    if system_id is None:
        max_id = conn.execute("SELECT MAX(system_id) FROM star_systems").fetchone()[0]
        system_id = (max_id or 100) + 1

    if star_name is None:
        star_name = f"{name} Prime"

    conn.execute("""
        INSERT INTO star_systems
        (system_id, name, star_name, star_spectral_type,
         star_grid_col, star_grid_row, created_turn)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (system_id, name, star_name, spectral_type, star_col, star_row, created_turn))
    conn.commit()
    conn.close()

    print(f"  Added system: {name} ({system_id})")
    print(f"    Star: {star_name} [{spectral_type}] at {star_col}{star_row:02d}")
    return system_id


def add_body(universe_db_path=None, body_id=None, system_id=None, name=None,
             body_type='planet', parent_body_id=None,
             grid_col='M', grid_row=13,
             gravity=1.0, temperature=300, atmosphere='Standard',
             tectonic_activity=0, hydrosphere=0, life='None',
             map_symbol=None, surface_size=None, resource_id=None,
             created_turn=None):
    """
    Add a celestial body to a system in the universe.
    
    If body_id is None, auto-assigns a random 6-digit ID.
    map_symbol defaults based on body_type.
    surface_size defaults: planet=31, moon=15, gas_giant=50, asteroid=11.
    """
    import random

    conn = get_universe_connection(universe_db_path)

    # Verify system exists
    sys = conn.execute("SELECT name FROM star_systems WHERE system_id = ?", (system_id,)).fetchone()
    if not sys:
        print(f"Error: System {system_id} not found.")
        conn.close()
        return None

    if body_id is None:
        while True:
            body_id = random.randint(100000, 999999)
            if not conn.execute("SELECT 1 FROM celestial_bodies WHERE body_id = ?", (body_id,)).fetchone():
                break

    if map_symbol is None:
        map_symbol = {'planet': 'O', 'moon': 'o', 'gas_giant': 'G', 'asteroid': '*'}.get(body_type, '?')

    if surface_size is None:
        surface_size = {'planet': 31, 'moon': 15, 'gas_giant': 50, 'asteroid': 11}.get(body_type, 31)
    surface_size = max(5, min(50, surface_size))

    conn.execute("""
        INSERT INTO celestial_bodies
        (body_id, system_id, name, body_type, parent_body_id,
         grid_col, grid_row, gravity, temperature, atmosphere,
         tectonic_activity, hydrosphere, life, map_symbol, surface_size,
         resource_id, created_turn)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (body_id, system_id, name, body_type, parent_body_id,
          grid_col, grid_row, gravity, temperature, atmosphere,
          tectonic_activity, hydrosphere, life, map_symbol, surface_size,
          resource_id, created_turn))
    conn.commit()
    conn.close()

    loc = f"{grid_col}{grid_row:02d}"
    print(f"  Added {body_type}: {name} ({body_id}) at {loc} in {sys['name']}")
    print(f"    {gravity}g, {temperature}K, {atmosphere}, tec={tectonic_activity}, hydro={hydrosphere}%, life={life}")
    print(f"    Surface: {surface_size}x{surface_size}")
    if resource_id:
        print(f"    Resource: {resource_id}")
    return body_id


def add_link(universe_db_path=None, system_a=None, system_b=None,
             known_by_default=0, created_turn=None):
    """
    Add a hyperspace link between two star systems.
    Normalises order so system_a < system_b to prevent duplicates.
    """
    conn = get_universe_connection(universe_db_path)

    # Normalise
    a, b = min(system_a, system_b), max(system_a, system_b)

    # Verify both systems exist
    sa = conn.execute("SELECT name FROM star_systems WHERE system_id = ?", (a,)).fetchone()
    sb = conn.execute("SELECT name FROM star_systems WHERE system_id = ?", (b,)).fetchone()
    if not sa or not sb:
        missing = a if not sa else b
        print(f"Error: System {missing} not found.")
        conn.close()
        return False

    # Check for duplicate
    existing = conn.execute(
        "SELECT 1 FROM system_links WHERE system_a = ? AND system_b = ?", (a, b)
    ).fetchone()
    if existing:
        print(f"  Link {sa['name']} ({a}) <-> {sb['name']} ({b}) already exists.")
        conn.close()
        return True

    conn.execute("""
        INSERT INTO system_links (system_a, system_b, known_by_default, created_turn)
        VALUES (?, ?, ?, ?)
    """, (a, b, known_by_default, created_turn))
    conn.commit()
    conn.close()

    vis = "known" if known_by_default else "hidden"
    print(f"  Added link: {sa['name']} ({a}) <-> {sb['name']} ({b}) [{vis}]")
    return True


def add_trade_good(universe_db_path=None, item_id=None, name=None,
                   base_price=10, mass_per_unit=1):
    """Add a trade good to the universe catalogue."""
    conn = get_universe_connection(universe_db_path)

    if item_id is None:
        max_id = conn.execute("SELECT MAX(item_id) FROM trade_goods").fetchone()[0]
        item_id = (max_id or 100) + 1

    conn.execute("""
        INSERT INTO trade_goods (item_id, name, base_price, mass_per_unit)
        VALUES (?, ?, ?, ?)
    """, (item_id, name, base_price, mass_per_unit))
    conn.commit()
    conn.close()

    print(f"  Added trade good: {name} ({item_id}) base={base_price}cr mass={mass_per_unit}MU")
    return item_id


def list_universe(universe_db_path=None):
    """Print a summary of all universe content."""
    conn = get_universe_connection(universe_db_path)

    print("\n=== UNIVERSE CONTENTS ===\n")

    # Systems
    systems = conn.execute("SELECT * FROM star_systems ORDER BY system_id").fetchall()
    print(f"Star Systems ({len(systems)}):")
    for s in systems:
        loc = f"{s['star_grid_col']}{s['star_grid_row']:02d}"
        ct = f"  [added {s['created_turn']}]" if s['created_turn'] else ""
        print(f"  {s['system_id']:>4d}  {s['name']:<20s}  Star: {s['star_name']} [{s['star_spectral_type']}] at {loc}{ct}")

        # Bodies in this system
        bodies = conn.execute(
            "SELECT * FROM celestial_bodies WHERE system_id = ? ORDER BY grid_row, grid_col",
            (s['system_id'],)
        ).fetchall()
        for b in bodies:
            loc = f"{b['grid_col']}{b['grid_row']:02d}"
            parent = f" (moon of {b['parent_body_id']})" if b['parent_body_id'] else ""
            res_id = b['resource_id'] if 'resource_id' in b.keys() and b['resource_id'] else None
            res_str = ""
            if res_id:
                res = conn.execute("SELECT name FROM resources WHERE resource_id = ?", (res_id,)).fetchone()
                res_name = res['name'] if res else f"#{res_id}"
                res_str = f"  res={res_name} ({res_id})"
            print(f"         {b['body_id']:>6d}  {b['name']:<16s} {b['body_type']:<10s} at {loc}"
                  f"  {b['gravity']}g {b['temperature']}K {b['atmosphere']}  [{b['surface_size']}x{b['surface_size']}]{parent}{res_str}")

    # Links
    links = conn.execute("""
        SELECT sl.*, sa.name as name_a, sb.name as name_b
        FROM system_links sl
        JOIN star_systems sa ON sl.system_a = sa.system_id
        JOIN star_systems sb ON sl.system_b = sb.system_id
        ORDER BY sl.system_a, sl.system_b
    """).fetchall()
    if links:
        print(f"\nSystem Links ({len(links)}):")
        for l in links:
            vis = "known" if l['known_by_default'] else "hidden"
            print(f"  {l['name_a']} ({l['system_a']}) <-> {l['name_b']} ({l['system_b']}) [{vis}]")

    # Trade goods
    goods = conn.execute("SELECT * FROM trade_goods ORDER BY item_id").fetchall()
    if goods:
        print(f"\nTrade Goods ({len(goods)}):")
        for g in goods:
            origin = g['origin_system_id'] if 'origin_system_id' in g.keys() and g['origin_system_id'] else None
            origin_str = f"  origin={origin}" if origin else ""
            print(f"  {g['item_id']:>6d}  {g['name']:<30s}  base={g['base_price']}cr  mass={g['mass_per_unit']}MU{origin_str}")

    # Planetary resources (GM-only, hidden from players)
    has_res_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='resources'"
    ).fetchone()
    if has_res_table:
        res_list = conn.execute("SELECT * FROM resources ORDER BY resource_id").fetchall()
        if res_list:
            print(f"\nPlanetary Resources ({len(res_list)}):")
            print(f"  (GM-only -- not visible to players)")
            for r in res_list:
                produces = ""
                if r['produces_item_id']:
                    item = conn.execute("SELECT name FROM trade_goods WHERE item_id = ?",
                                        (r['produces_item_id'],)).fetchone()
                    produces = f"  -> {item['name']} ({r['produces_item_id']})" if item else f"  -> item {r['produces_item_id']}"
                print(f"  {r['resource_id']:>6d}  {r['name']:<30s}  {r['description']}{produces}")

    # Factions
    factions = conn.execute("SELECT * FROM factions ORDER BY faction_id").fetchall()
    if factions:
        print(f"\nFactions ({len(factions)}):")
        for f in factions:
            print(f"  {f['faction_id']:>3d}  [{f['abbreviation']}] {f['name']}")

    # Surface ports and outposts (in game_state.db — open combined connection if available)
    from db.database import STATE_DB_PATH
    uni_path = Path(universe_db_path) if universe_db_path else None
    state_path = uni_path.parent / "game_state.db" if uni_path else STATE_DB_PATH
    if state_path.exists():
        import sqlite3
        sc = sqlite3.connect(str(state_path))
        sc.row_factory = sqlite3.Row
        # Check if surface_ports table exists
        has_sp = sc.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='surface_ports'"
        ).fetchone()
        if has_sp:
            ports = sc.execute("SELECT * FROM surface_ports ORDER BY port_id").fetchall()
            if ports:
                print(f"\nSurface Ports ({len(ports)}):")
                for p in ports:
                    parent = ""
                    if p['parent_base_id']:
                        base = sc.execute(
                            "SELECT name FROM starbases WHERE base_id = ?",
                            (p['parent_base_id'],)
                        ).fetchone()
                        parent = f"  -> {base['name']} ({p['parent_base_id']})" if base else f"  -> base {p['parent_base_id']}"
                    print(f"  {p['port_id']:>8d}  {p['name']:<20s}  on body {p['body_id']}  "
                          f"at ({p['surface_x']},{p['surface_y']})  "
                          f"cx={p['complexes']} wk={p['workers']} tp={p['troops']}{parent}")
        # Outposts
        has_op = sc.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='outposts'"
        ).fetchone()
        if has_op:
            ops = sc.execute("SELECT * FROM outposts ORDER BY outpost_id").fetchall()
            if ops:
                print(f"\nOutposts ({len(ops)}):")
                for o in ops:
                    body = conn.execute(
                        "SELECT name FROM celestial_bodies WHERE body_id = ?",
                        (o['body_id'],)
                    ).fetchone()
                    body_name = body['name'] if body else f"#{o['body_id']}"
                    print(f"  {o['outpost_id']:>8d}  {o['name']:<24s}  on {body_name} ({o['body_id']})  "
                          f"at ({o['surface_x']},{o['surface_y']})  "
                          f"type={o['outpost_type']}  wk={o['workers']}")
        sc.close()

    conn.close()
