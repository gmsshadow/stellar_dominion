"""
Stellar Dominion - Database Layer
Two-database model:
  universe.db   — World definition (GM-editable): systems, bodies, links, factions, trade goods, planet surfaces
  game_state.db — Live game state (engine-managed): ships, players, markets, orders, etc.

The engine opens game_state.db as the main connection and ATTACHes universe.db.
Since table names are unique across both databases, all existing SQL queries
work transparently through a single connection object.
"""

import sqlite3
import shutil
from pathlib import Path
from datetime import datetime

GAME_DATA_DIR = Path(__file__).parent.parent / "game_data"
UNIVERSE_DB_PATH = GAME_DATA_DIR / "universe.db"
STATE_DB_PATH = GAME_DATA_DIR / "game_state.db"


# ======================================================================
# GAME CONSTANTS (tunable)
# ======================================================================

# Max integrity = ship_size × multiplier for that hull type. Military
# hulls are 1.5× tougher than Commercial. Armour modules (future) will
# add flat HP on top of this base. Unknown hull types fall back to 1.0.
HULL_HP_MULTIPLIER = {
    'Commercial': 1.0,
    'Military':   1.5,
}

# Shield thickness formula: thickness = floor(FACTOR × current_SP / ship_size).
# Higher FACTOR = more absorption per hit for a given SP pool, meaning SP
# depletes faster during combat. FACTOR=1 would mean 1 thickness per ship_size
# SP. FACTOR=2 means thickness drops 1 step for every size/2 SP depleted.
SHIELD_THICKNESS_FACTOR = 2

# Starbase tuning:
# - HP scales with installed module count
# - Shield thickness uses a fixed denominator (starbases don't have a size field)
BASE_HP_PER_MODULE = 50
BASE_SHIELD_SIZE   = 300

# Starbase combat constants
# - BASE_SIZE_STARBASE: the "ship_size equivalent" for shield thickness calc
# - BASE_HP_PER_MODULE: max_integrity = BASE_HP_PER_MODULE × module_count
STARBASE_SHIELD_BASE_SIZE = 300
STARBASE_HP_PER_MODULE = 50


# ======================================================================
# CONNECTION MANAGEMENT
# ======================================================================

def get_connection(state_db_path=None, universe_db_path=None):
    """
    Open game_state.db and ATTACH universe.db — returns one connection
    that can query tables from both databases seamlessly.
    """
    state_path = Path(state_db_path) if state_db_path else STATE_DB_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(state_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Determine universe.db path: explicit > alongside state DB > default
    if universe_db_path:
        uni_path = Path(universe_db_path)
    else:
        uni_path = state_path.parent / "universe.db"
        if not uni_path.exists():
            uni_path = UNIVERSE_DB_PATH

    # ATTACH universe.db if it exists and is a separate file
    if uni_path.exists() and uni_path.resolve() != state_path.resolve():
        conn.execute("ATTACH DATABASE ? AS universe", (str(uni_path),))
        # Migrate universe.db: add origin_system_id to trade_goods if missing
        tg_cols = [r[1] for r in conn.execute("PRAGMA universe.table_info(trade_goods)").fetchall()]
        if 'origin_system_id' not in tg_cols:
            conn.execute("ALTER TABLE universe.trade_goods ADD COLUMN origin_system_id INTEGER DEFAULT NULL")
            conn.commit()
        # Migrate universe.db: add resource_id to celestial_bodies if missing
        cb_cols = [r[1] for r in conn.execute("PRAGMA universe.table_info(celestial_bodies)").fetchall()]
        if 'resource_id' not in cb_cols:
            conn.execute("ALTER TABLE universe.celestial_bodies ADD COLUMN resource_id INTEGER DEFAULT NULL")
            conn.commit()
        # Migrate universe.db: create resources table if missing
        has_resources = conn.execute(
            "SELECT name FROM universe.sqlite_master WHERE type='table' AND name='resources'"
        ).fetchone()
        if not has_resources:
            conn.execute("""CREATE TABLE IF NOT EXISTS universe.resources (
                resource_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                produces_item_id INTEGER DEFAULT NULL
            )""")
            conn.commit()

        # Cleanup: if ship_components was incorrectly created in game_state.db (main),
        # drop it — it belongs in universe.db only
        main_has_components = conn.execute(
            "SELECT name FROM main.sqlite_master WHERE type='table' AND name='ship_components'"
        ).fetchone()
        if main_has_components:
            conn.execute("DROP TABLE IF EXISTS main.ship_components")
            conn.commit()

        # Cleanup: same for resources table
        main_has_resources = conn.execute(
            "SELECT name FROM main.sqlite_master WHERE type='table' AND name='resources'"
        ).fetchone()
        if main_has_resources:
            conn.execute("DROP TABLE IF EXISTS main.resources")
            conn.commit()

        # Migrate: fix star_systems with star_name but NULL grid positions (default to M13)
        conn.execute("""
            UPDATE universe.star_systems
            SET star_grid_col = 'M', star_grid_row = 13
            WHERE star_name IS NOT NULL AND (star_grid_col IS NULL OR star_grid_row IS NULL)
        """)
        conn.commit()

        # Migrate universe.db: create ship_components table if missing
        has_components = conn.execute(
            "SELECT name FROM universe.sqlite_master WHERE type='table' AND name='ship_components'"
        ).fetchone()
        if not has_components:
            conn.execute("""CREATE TABLE IF NOT EXISTS universe.ship_components (
                component_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                st_cost INTEGER NOT NULL,
                cargo_capacity INTEGER DEFAULT 0,
                crew_capacity INTEGER DEFAULT 0,
                life_capacity INTEGER DEFAULT 0,
                thrust INTEGER DEFAULT 0,
                engine_efficiency REAL DEFAULT 0,
                sensor_rating INTEGER DEFAULT 0,
                jump_range INTEGER DEFAULT 0,
                jump_oc_cost INTEGER DEFAULT 0,
                hull_restriction TEXT DEFAULT NULL,
                base_price INTEGER DEFAULT 0,
                description TEXT DEFAULT ''
            )""")
            # Seed default components
            seed_components = [
                (100, 'Standard Bridge', 'bridge', 50, 0, 0, 0, 0, 0, 0, 0, 0, None, 500, 'Basic command centre.'),
                (110, 'Thruster Array', 'thruster', 20, 0, 0, 0, 5, 0, 0, 0, 0, None, 800, 'Standard thruster pack.'),
                (111, 'Heavy Thruster Pack', 'thruster', 30, 0, 0, 0, 10, 0, 0, 0, 0, None, 1500, 'High-output thrusters.'),
                (120, 'Commercial Sublight Engine', 'engine', 10, 0, 0, 0, 0, 1.0, 0, 0, 0, None, 1200, 'Standard propulsion.'),
                (121, 'Military Sublight Engine', 'engine', 10, 0, 0, 0, 0, 1.5, 0, 0, 0, 'military', 2500, 'High-performance drive.'),
                (130, 'Cargo Bay', 'cargo', 25, 20, 0, 0, 0, 0, 0, 0, 0, None, 600, 'Standard cargo bay.'),
                (131, 'Reinforced Cargo Bay', 'cargo', 30, 20, 0, 0, 0, 0, 0, 0, 0, None, 900, 'Armoured cargo storage.'),
                (140, 'Crew Quarters', 'quarters', 30, 0, 20, 20, 0, 0, 0, 0, 0, None, 400, 'Standard crew accommodation.'),
                (141, 'Military Bunks', 'quarters', 30, 0, 40, 25, 0, 0, 0, 0, 0, 'military', 500, 'Compact military berths.'),
                (142, 'Luxury Cabins', 'quarters', 30, 0, 10, 15, 0, 0, 0, 0, 0, None, 700, 'Comfortable passenger cabins.'),
                (150, 'Basic Sensor Array', 'sensor', 10, 0, 0, 0, 0, 0, 5, 0, 0, None, 300, 'Standard detection suite.'),
                (151, 'Military Sensor Suite', 'sensor', 15, 0, 0, 0, 0, 0, 10, 0, 0, 'military', 1000, 'Advanced military sensors.'),
                (152, 'Deep Space Scanner', 'sensor', 20, 0, 0, 0, 0, 0, 15, 0, 0, None, 1800, 'Long-range detection.'),
                (160, 'Jump Drive Mk1', 'jump_drive', 50, 0, 0, 0, 0, 0, 0, 5, 50, None, 5000, 'Basic hyperspace drive.'),
                (161, 'Jump Drive Mk2', 'jump_drive', 60, 0, 0, 0, 0, 0, 0, 6, 40, None, 12000, 'Advanced jump drive.'),
            ]
            for c in seed_components:
                conn.execute("""INSERT OR IGNORE INTO universe.ship_components VALUES
                    (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", c)
            conn.commit()

        # Migrate: update jump drive OC costs (was 150/100, now 50/40)
        conn.execute("UPDATE universe.ship_components SET jump_oc_cost = 50 WHERE component_id = 160 AND jump_oc_cost > 50")
        conn.execute("UPDATE universe.ship_components SET jump_oc_cost = 40 WHERE component_id = 161 AND jump_oc_cost > 40")
        # Migrate: rebalance thruster thrust values (was 20/40, now 5/10)
        conn.execute("UPDATE universe.ship_components SET thrust = 5 WHERE component_id = 110 AND thrust = 20")
        conn.execute("UPDATE universe.ship_components SET thrust = 10 WHERE component_id = 111 AND thrust = 40")
        conn.commit()

        # Migrate: add sensor_rating column to base_modules if missing,
        # and seed the Sensor Suite / Deep Scan Array entries.
        bm_cols = [r[1] for r in conn.execute("PRAGMA universe.table_info(base_modules)").fetchall()]
        if bm_cols and 'sensor_rating' not in bm_cols:
            conn.execute("ALTER TABLE universe.base_modules ADD COLUMN sensor_rating INTEGER DEFAULT 0")
            conn.commit()
            bm_cols.append('sensor_rating')
        if bm_cols:
            # If prior seed runs inserted sensor modules with wrong column
            # ordering (before this fix), clean them up so we can re-insert.
            bad = conn.execute(
                "SELECT module_id FROM universe.base_modules WHERE module_id IN (590, 591) "
                "AND (category != 'sensor' OR sensor_rating NOT IN (15, 35) OR base_price NOT IN (4000, 9000))"
            ).fetchall()
            if bad:
                conn.execute("DELETE FROM universe.base_modules WHERE module_id IN (590, 591)")
                conn.commit()
            # Seed new sensor modules using explicit column names so the
            # row survives any past or future ALTER TABLE column additions.
            sensor_modules = [
                {'module_id': 590, 'name': 'Sensor Suite', 'category': 'sensor',
                 'employees_required': 5, 'location_restriction': None,
                 'docking_slots': 0, 'mining_capacity': 0, 'factory_capacity': 0,
                 'repair_capacity': 0, 'market_income': 0, 'storage_capacity': 0,
                 'habitat_capacity': 0, 'defence_rating': 0,
                 'sensor_rating': 15, 'base_price': 4000,
                 'description': 'Passive sensor array. Detects nearby ships and objects. Multiple suites stack with diminishing returns.'},
                {'module_id': 591, 'name': 'Deep Scan Array', 'category': 'sensor',
                 'employees_required': 10, 'location_restriction': None,
                 'docking_slots': 0, 'mining_capacity': 0, 'factory_capacity': 0,
                 'repair_capacity': 0, 'market_income': 0, 'storage_capacity': 0,
                 'habitat_capacity': 0, 'defence_rating': 0,
                 'sensor_rating': 35, 'base_price': 9000,
                 'description': 'High-power sensor array with greater range and accuracy.'},
            ]
            for m in sensor_modules:
                cols = ', '.join(m.keys())
                placeholders = ', '.join(['?'] * len(m))
                conn.execute(
                    f"INSERT OR IGNORE INTO universe.base_modules ({cols}) VALUES ({placeholders})",
                    tuple(m.values())
                )
            conn.commit()

        # Migrate universe.db: create base_modules table if missing
        has_base_modules = conn.execute(
            "SELECT name FROM universe.sqlite_master WHERE type='table' AND name='base_modules'"
        ).fetchone()
        if not has_base_modules:
            conn.execute("""CREATE TABLE IF NOT EXISTS universe.base_modules (
                module_id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
                employees_required INTEGER DEFAULT 10, location_restriction TEXT DEFAULT NULL,
                docking_slots INTEGER DEFAULT 0, mining_capacity INTEGER DEFAULT 0,
                factory_capacity INTEGER DEFAULT 0, repair_capacity INTEGER DEFAULT 0,
                market_income INTEGER DEFAULT 0, storage_capacity INTEGER DEFAULT 0,
                habitat_capacity INTEGER DEFAULT 0, defence_rating INTEGER DEFAULT 0,
                base_price INTEGER DEFAULT 0, description TEXT DEFAULT ''
            )""")
            seed_modules = [
                (500, 'Command Module', 'command', 10, None, 0, 0, 0, 0, 0, 0, 0, 0, 2000, '1 per 100 modules for 100% command efficiency.'),
                (510, 'Docking Bay', 'dock', 20, 'starbase', 1, 0, 0, 0, 0, 0, 0, 0, 5000, 'Allows one ship to dock. Starbase only.'),
                (511, 'Heavy Docking Bay', 'dock', 30, 'starbase', 1, 0, 0, 0, 0, 0, 0, 0, 8000, 'Reinforced bay. Starbase only.'),
                (520, 'Mining Rig', 'mining', 15, 'surface', 0, 10, 0, 0, 0, 0, 0, 0, 3000, 'Extracts resources. Surface only.'),
                (521, 'Deep Core Drill', 'mining', 25, 'surface', 0, 25, 0, 0, 0, 0, 0, 0, 7000, 'Heavy mining. Surface only.'),
                (530, 'Assembly Plant', 'factory', 25, None, 0, 0, 10, 0, 0, 0, 0, 0, 6000, 'Constructs items.'),
                (531, 'Advanced Fabricator', 'factory', 40, None, 0, 0, 25, 0, 0, 0, 0, 0, 12000, 'High-tech manufacturing.'),
                (540, 'Repair Bay', 'maintenance', 15, 'starbase', 0, 0, 0, 5, 0, 0, 0, 0, 4000, 'Repairs ships. Starbase only.'),
                (541, 'Shipyard', 'maintenance', 30, 'starbase', 0, 0, 0, 15, 0, 0, 0, 0, 10000, 'Full shipyard. Starbase only.'),
                (550, 'Trade Market', 'market', 10, 'surface', 0, 0, 0, 0, 100, 0, 0, 0, 3000, 'Trade with population. Surface only.'),
                (551, 'Commerce Hub', 'market', 20, 'surface', 0, 0, 0, 0, 250, 0, 0, 0, 8000, 'Large trade hub. Surface only.'),
                (560, 'Storage Warehouse', 'storage', 5, None, 0, 0, 0, 0, 0, 500, 0, 0, 1500, 'Bulk storage. 500 ST.'),
                (561, 'Secure Vault', 'storage', 8, None, 0, 0, 0, 0, 0, 200, 0, 0, 3000, 'Armoured storage. 200 ST.'),
                (570, 'Habitat Block', 'habitat', 2, None, 0, 0, 0, 0, 0, 0, 50, 0, 2000, 'Housing for 50.'),
                (571, 'Life Dome', 'habitat', 3, 'surface', 0, 0, 0, 0, 0, 0, 100, 0, 4000, 'Dome for 100. Surface only.'),
                (580, 'Defence Turret', 'defence', 10, None, 0, 0, 0, 0, 0, 0, 0, 5, 3500, 'Defensive weapon.'),
                (581, 'Shield Generator', 'defence', 15, None, 0, 0, 0, 0, 0, 0, 0, 10, 6000, 'Energy shield.'),
            ]
            for m in seed_modules:
                conn.execute("""INSERT OR IGNORE INTO universe.base_modules VALUES
                    (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", m)
            conn.commit()

        # Migrate planet_surface: create in universe.db if missing
        has_planet_surface = conn.execute(
            "SELECT name FROM universe.sqlite_master WHERE type='table' AND name='planet_surface'"
        ).fetchone()
        if not has_planet_surface:
            conn.execute("""CREATE TABLE IF NOT EXISTS universe.planet_surface (
                body_id INTEGER NOT NULL,
                x INTEGER NOT NULL,
                y INTEGER NOT NULL,
                terrain_type TEXT NOT NULL,
                PRIMARY KEY (body_id, x, y),
                FOREIGN KEY (body_id) REFERENCES celestial_bodies(body_id)
            )""")
            conn.commit()

        # Migrate planet_surface: if it exists in game_state.db, copy to universe and drop from main
        main_has_planet_surface = conn.execute(
            "SELECT name FROM main.sqlite_master WHERE type='table' AND name='planet_surface'"
        ).fetchone()
        if main_has_planet_surface:
            for row in conn.execute("SELECT body_id, x, y, terrain_type FROM main.planet_surface").fetchall():
                conn.execute(
                    "INSERT OR REPLACE INTO universe.planet_surface (body_id, x, y, terrain_type) VALUES (?, ?, ?, ?)",
                    (row['body_id'], row['x'], row['y'], row['terrain_type'])
                )
            conn.execute("DROP TABLE main.planet_surface")
            conn.commit()

    # Migrate game_state.db: add life_support_capacity to ships if missing
    ship_cols = [r[1] for r in conn.execute("PRAGMA table_info(ships)").fetchall()]
    if 'life_support_capacity' not in ship_cols:
        conn.execute("ALTER TABLE ships ADD COLUMN life_support_capacity INTEGER DEFAULT 20")
        conn.commit()

    # Migrate game_state.db: add crew_type_id and wages to officers if missing
    off_cols = [r[1] for r in conn.execute("PRAGMA table_info(officers)").fetchall()]
    if 'crew_type_id' not in off_cols:
        conn.execute("ALTER TABLE officers ADD COLUMN crew_type_id INTEGER DEFAULT 401")
        conn.execute("ALTER TABLE officers ADD COLUMN wages INTEGER DEFAULT 5")
        conn.commit()

    # Migrate game_state.db: add turn_status to games if missing
    game_cols = [r[1] for r in conn.execute("PRAGMA table_info(games)").fetchall()]
    if 'turn_status' not in game_cols:
        conn.execute("ALTER TABLE games ADD COLUMN turn_status TEXT NOT NULL DEFAULT 'open'")
        conn.commit()

    # Migrate: crew (item 401) should not occupy cargo space - fix existing cargo_items and ships
    crew_cargo = conn.execute(
        "SELECT cargo_id, ship_id, quantity, mass_per_unit FROM cargo_items WHERE item_type_id = 401 AND mass_per_unit > 0"
    ).fetchall()
    if crew_cargo:
        for row in crew_cargo:
            freed_mass = row['quantity'] * row['mass_per_unit']
            conn.execute("UPDATE cargo_items SET mass_per_unit = 0 WHERE cargo_id = ?", (row['cargo_id'],))
            conn.execute(
                "UPDATE ships SET cargo_used = MAX(0, cargo_used - ?) WHERE ship_id = ?",
                (freed_mass, row['ship_id'])
            )
        conn.commit()

    # Migrate game_state.db: add ship_size to ships if missing
    ship_cols = [r[1] for r in conn.execute("PRAGMA table_info(ships)").fetchall()]
    if 'ship_size' not in ship_cols:
        conn.execute("ALTER TABLE ships ADD COLUMN ship_size INTEGER DEFAULT 50")
        conn.execute("UPDATE ships SET ship_size = hull_count WHERE ship_size IS NULL OR ship_size = 0")
        conn.commit()
    else:
        # Repair: previous migration incorrectly set ship_size=10 for all ships
        # Fix any ship where ship_size=10 but hull_count differs
        conn.execute("UPDATE ships SET ship_size = hull_count WHERE ship_size = 10 AND hull_count != 10")
        conn.commit()

    # Migrate surface_port <-> starbase relationship: invert so starbase references surface_port
    base_cols = [r[1] for r in conn.execute("PRAGMA table_info(starbases)").fetchall()]
    sp_cols = [r[1] for r in conn.execute("PRAGMA table_info(surface_ports)").fetchall()]
    if 'surface_port_id' not in base_cols:
        conn.execute("ALTER TABLE starbases ADD COLUMN surface_port_id INTEGER")
        conn.commit()
    if 'parent_base_id' in sp_cols:
        # Populate starbases.surface_port_id from surface_ports.parent_base_id
        for row in conn.execute(
            "SELECT port_id, parent_base_id FROM surface_ports WHERE parent_base_id IS NOT NULL"
        ).fetchall():
            conn.execute(
                "UPDATE starbases SET surface_port_id = ? WHERE base_id = ?",
                (row['port_id'], row['parent_base_id'])
            )
        conn.commit()
        # Recreate surface_ports without parent_base_id
        conn.execute("""
            CREATE TABLE surface_ports_new (
                port_id INTEGER PRIMARY KEY,
                game_id TEXT NOT NULL,
                name TEXT NOT NULL,
                body_id INTEGER NOT NULL,
                surface_x INTEGER NOT NULL,
                surface_y INTEGER NOT NULL,
                owner_prefect_id INTEGER,
                complexes INTEGER DEFAULT 0,
                workers INTEGER DEFAULT 0,
                troops INTEGER DEFAULT 0,
                FOREIGN KEY (game_id) REFERENCES games(game_id)
            )
        """)
        conn.execute("""
            INSERT INTO surface_ports_new
            (port_id, game_id, name, body_id, surface_x, surface_y,
             owner_prefect_id, complexes, workers, troops)
            SELECT port_id, game_id, name, body_id, surface_x, surface_y,
                   owner_prefect_id, complexes, workers, troops
            FROM surface_ports
        """)
        conn.execute("DROP TABLE surface_ports")
        conn.execute("ALTER TABLE surface_ports_new RENAME TO surface_ports")
        conn.commit()

    # Migrate installed_items: if old schema (has item_type_id), rework to component_id
    ii_cols = [r[1] for r in conn.execute("PRAGMA table_info(installed_items)").fetchall()]
    if 'item_type_id' in ii_cols and 'component_id' not in ii_cols:
        # Old schema → new schema migration
        # Map old item_type_ids to new component_ids
        old_to_new = {
            100: 100,   # Bridge → Standard Bridge
            103: 150,   # Sensor → Basic Sensor Array
            155: 120,   # Sublight Engines → Commercial Sublight Engine
            174: 160,   # Jump Drive - Basic → Jump Drive Mk1
            160: 110,   # Thrust Engine → Thruster Array
            131: 140,   # Quarters → Crew Quarters
            180: 130,   # Cargo Hold → Cargo Bay
        }
        old_items = conn.execute("SELECT * FROM installed_items").fetchall()
        conn.execute("DROP TABLE installed_items")
        conn.execute("""CREATE TABLE IF NOT EXISTS installed_items (
            item_install_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ship_id INTEGER, base_id INTEGER,
            component_id INTEGER NOT NULL, quantity INTEGER NOT NULL DEFAULT 1
        )""")
        for item in old_items:
            new_id = old_to_new.get(item['item_type_id'], item['item_type_id'])
            conn.execute(
                "INSERT INTO installed_items (ship_id, base_id, component_id, quantity) VALUES (?, ?, ?, ?)",
                (item['ship_id'], item['base_id'], new_id, item['quantity'])
            )
        conn.commit()
        # Recalculate stats for all ships
        for ship in conn.execute("SELECT ship_id FROM ships").fetchall():
            recalculate_ship_stats(conn, ship['ship_id'])

    # Migrate: add employees and employee_capacity to base tables if missing
    for table, id_col in [('starbases', 'base_id'), ('surface_ports', 'port_id'), ('outposts', 'outpost_id')]:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if 'employees' not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN employees INTEGER DEFAULT 0")
            conn.commit()
        if 'employee_capacity' not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN employee_capacity INTEGER DEFAULT 0")
            conn.commit()

    # Migrate: create installed_modules table if missing
    has_im = conn.execute(
        "SELECT name FROM main.sqlite_master WHERE type='table' AND name='installed_modules'"
    ).fetchone()
    if not has_im:
        conn.execute("""CREATE TABLE IF NOT EXISTS installed_modules (
            install_id INTEGER PRIMARY KEY AUTOINCREMENT,
            starbase_id INTEGER, port_id INTEGER, outpost_id INTEGER,
            module_id INTEGER NOT NULL, quantity INTEGER NOT NULL DEFAULT 1
        )""")
        conn.commit()

    # Migrate: create base_inventory table if missing
    has_bi = conn.execute(
        "SELECT name FROM main.sqlite_master WHERE type='table' AND name='base_inventory'"
    ).fetchone()
    if not has_bi:
        conn.execute("""CREATE TABLE IF NOT EXISTS base_inventory (
            inventory_id INTEGER PRIMARY KEY AUTOINCREMENT,
            starbase_id INTEGER, port_id INTEGER, outpost_id INTEGER,
            item_type_id INTEGER NOT NULL, item_name TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 0, mass_per_unit INTEGER DEFAULT 1
        )""")
        conn.commit()

    # Migrate: fix crew_required to 1 per 2 hulls
    # Always recalculate to catch any incorrect values
    conn.execute("""
        UPDATE ships SET crew_required = MAX(1, (ship_size + 1) / 2)
        WHERE crew_required != MAX(1, (ship_size + 1) / 2) AND ship_size > 0
    """)

    # Migrate: add sensor_profile column to ships if missing
    ship_cols = [r[1] for r in conn.execute("PRAGMA table_info(ships)").fetchall()]
    if 'sensor_profile' not in ship_cols:
        conn.execute("ALTER TABLE ships ADD COLUMN sensor_profile REAL DEFAULT 0.5")
        conn.execute("UPDATE ships SET sensor_profile = ship_size / 100.0 WHERE ship_size > 0")
    conn.commit()

    # Migrate: add sensor_profile column to bases if missing
    for table in ('starbases', 'surface_ports', 'outposts'):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if 'sensor_profile' not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN sensor_profile REAL DEFAULT 1.0")
    conn.commit()

    # Migrate: add sensor_rating column to bases if missing
    for table in ('starbases', 'surface_ports', 'outposts'):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if 'sensor_rating' not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN sensor_rating INTEGER DEFAULT 0")
    conn.commit()

    # Migrate: add combat_doctrine to ships
    ship_cols = [r[1] for r in conn.execute("PRAGMA table_info(ships)").fetchall()]
    if 'combat_doctrine' not in ship_cols:
        conn.execute("ALTER TABLE ships ADD COLUMN combat_doctrine TEXT DEFAULT 'defensive'")
        conn.commit()

    # Migrate: add max_integrity to ships (scales with ship_size).
    # Integrity is no longer 0-100; it's an absolute HP pool equal to ship_size.
    # For existing ships, set max_integrity = ship_size and scale current
    # integrity proportionally so a ship at 100% health stays at 100% health,
    # but with the new absolute value.
    ship_cols = [r[1] for r in conn.execute("PRAGMA table_info(ships)").fetchall()]
    if 'max_integrity' not in ship_cols:
        conn.execute("ALTER TABLE ships ADD COLUMN max_integrity REAL DEFAULT 50.0")
        # For each existing ship, set max_integrity = ship_size and scale
        # current integrity from the old 0-100 scale to 0-ship_size scale.
        for r in conn.execute(
            "SELECT ship_id, ship_size, integrity FROM ships"
        ).fetchall():
            size = r['ship_size'] or 50
            old_integrity = r['integrity'] if r['integrity'] is not None else 100.0
            # If old integrity was 0-100, convert to absolute. A fresh ship
            # at integrity=100 becomes integrity=ship_size; a damaged ship
            # at integrity=60 becomes integrity=ship_size*0.6.
            new_integrity = size * (old_integrity / 100.0)
            conn.execute(
                "UPDATE ships SET max_integrity = ?, integrity = ? WHERE ship_id = ?",
                (float(size), float(new_integrity), r['ship_id'])
            )
        conn.commit()

    # Migrate: apply hull-type HP multiplier. If any ship's max_integrity
    # doesn't match (ship_size × hull_multiplier), recompute — preserving
    # current health as a percentage so a ship at 60% stays at 60%.
    needs_fix = conn.execute(
        """SELECT ship_id, ship_size, hull_type, integrity, max_integrity
           FROM ships"""
    ).fetchall()
    for r in needs_fix:
        size = r['ship_size'] or 50
        hull = (r['hull_type'] or 'Commercial').strip()
        mult = HULL_HP_MULTIPLIER.get(hull, 1.0)
        expected_max = float(size) * mult
        cur_max = r['max_integrity'] or 0
        if abs(cur_max - expected_max) < 0.01:
            continue  # already correct
        cur_integrity = r['integrity'] if r['integrity'] is not None else cur_max
        # Preserve health percentage
        pct = (cur_integrity / cur_max) if cur_max > 0 else 1.0
        new_integrity = expected_max * pct
        conn.execute(
            "UPDATE ships SET max_integrity = ?, integrity = ? WHERE ship_id = ?",
            (expected_max, new_integrity, r['ship_id'])
        )
    conn.commit()

    # Migrate: add armour and shield columns to ships
    ship_cols_now = [r[1] for r in conn.execute("PRAGMA table_info(ships)").fetchall()]
    for col, typ_default in [
        ('armour',           'INTEGER DEFAULT 0'),
        ('shield_sp',        'INTEGER DEFAULT 0'),
        ('max_shield_sp',    'INTEGER DEFAULT 0'),
        ('missiles_loaded',  'INTEGER DEFAULT 0'),
        ('torpedoes_loaded', 'INTEGER DEFAULT 0'),
        ('max_missiles',     'INTEGER DEFAULT 0'),
        ('max_torpedoes',    'INTEGER DEFAULT 0'),
    ]:
        if col not in ship_cols_now:
            conn.execute(f"ALTER TABLE ships ADD COLUMN {col} {typ_default}")
    conn.commit()

    # Migrate: add shield-generator columns to ship_components (in universe.db).
    # shield_sp_capacity is the SP each unit of this component provides.
    sc_cols_pre = [r[1] for r in conn.execute("PRAGMA universe.table_info(ship_components)").fetchall()]
    if 'shield_sp_capacity' not in sc_cols_pre:
        conn.execute("ALTER TABLE universe.ship_components ADD COLUMN shield_sp_capacity INTEGER DEFAULT 0")
    conn.commit()

    # Seed Shield Generator Mk1 (idempotent). If it already exists but has
    # stale values (e.g. older 30-SP spec), update to the current 15 SP.
    shield_gen = conn.execute(
        "SELECT component_id, shield_sp_capacity FROM universe.ship_components "
        "WHERE component_id = 210"
    ).fetchone()
    if not shield_gen:
        conn.execute(
            """INSERT INTO universe.ship_components
               (component_id, name, category, st_cost, cargo_capacity,
                crew_capacity, life_capacity, thrust, engine_efficiency,
                sensor_rating, jump_range, jump_oc_cost, hull_restriction,
                base_price, weapon_damage, weapon_range, weapon_shots_per_round,
                weapon_subcategory, weapon_requires_ammo, shield_sp_capacity,
                description)
               VALUES (210, 'Shield Generator Mk1', 'shield', 20, 0, 0, 0, 0,
                       0, 0, 0, 0, NULL, 3000, 0, 0, 0, NULL, 0, 15,
                       'Provides 15 SP per unit. Thickness = floor(2 × total_SP / ship_size).')"""
        )
        conn.commit()
    elif shield_gen['shield_sp_capacity'] != 15:
        # Migration: previous spec gave 30 SP per generator. Halve it so existing
        # DBs pick up the 15-SP balance change.
        conn.execute(
            "UPDATE universe.ship_components SET shield_sp_capacity = 15, "
            "description = 'Provides 15 SP per unit. Thickness = floor(2 × total_SP / ship_size).' "
            "WHERE component_id = 210"
        )
        conn.commit()
        # Recompute max_shield_sp for every ship so the new value propagates.
        # Preserve current health percentage (damaged shields stay damaged proportionally).
        from_ships = conn.execute(
            "SELECT ship_id, shield_sp, max_shield_sp FROM ships WHERE max_shield_sp > 0"
        ).fetchall()
        for r in from_ships:
            # Count this ship's shield generators now
            gens = conn.execute(
                "SELECT COALESCE(SUM(ii.quantity * sc.shield_sp_capacity), 0) AS sp "
                "FROM installed_items ii JOIN universe.ship_components sc "
                "  ON ii.component_id = sc.component_id "
                "WHERE ii.ship_id = ? AND sc.category = 'shield'",
                (r['ship_id'],)
            ).fetchone()
            new_max = int(gens['sp'] or 0)
            old_max = r['max_shield_sp'] or 0
            if old_max > 0:
                pct = (r['shield_sp'] or 0) / old_max
            else:
                pct = 1.0
            new_sp = min(new_max, int(new_max * pct))
            conn.execute(
                "UPDATE ships SET max_shield_sp = ?, shield_sp = ? WHERE ship_id = ?",
                (new_max, new_sp, r['ship_id'])
            )
        conn.commit()

    # Migrate: add weapon columns to ship_components (in universe.db)
    sc_cols = [r[1] for r in conn.execute("PRAGMA universe.table_info(ship_components)").fetchall()]
    weapon_col_defs = [
        ('weapon_damage',               'INTEGER DEFAULT 0'),
        ('weapon_range',                'INTEGER DEFAULT 0'),
        ('weapon_shots_per_round',      'INTEGER DEFAULT 0'),
        ('weapon_subcategory',          'TEXT DEFAULT NULL'),
        ('weapon_requires_ammo',        'INTEGER DEFAULT 0'),
        ('weapon_accuracy',             'REAL DEFAULT 1.0'),
        ('ammo_type',                   'TEXT DEFAULT NULL'),
        ('flight_rounds',               'INTEGER DEFAULT 0'),
        ('magazine_missile_capacity',   'INTEGER DEFAULT 0'),
        ('magazine_torpedo_capacity',   'INTEGER DEFAULT 0'),
    ]
    for col, typ in weapon_col_defs:
        if col not in sc_cols:
            conn.execute(f"ALTER TABLE universe.ship_components ADD COLUMN {col} {typ}")
    conn.commit()

    # Seed/update Beam Cannon Mk1 (idempotent). If the row already exists but
    # has stale values, set the canonical current ones.
    sc_cols_now = [r[1] for r in conn.execute(
        "PRAGMA universe.table_info(ship_components)").fetchall()]
    if sc_cols_now:
        beam_cannon = {
            'component_id': 200, 'name': 'Beam Cannon Mk1', 'category': 'weapon',
            'st_cost': 15, 'cargo_capacity': 0, 'crew_capacity': 0, 'life_capacity': 0,
            'thrust': 0, 'engine_efficiency': 0, 'sensor_rating': 0,
            'jump_range': 0, 'jump_oc_cost': 0, 'hull_restriction': None,
            'base_price': 2500,
            'weapon_damage': 10, 'weapon_range': 2, 'weapon_shots_per_round': 1,
            'weapon_subcategory': 'beam', 'weapon_requires_ammo': 0,
            'weapon_accuracy': 0.8,
            'description': 'Standard energy beam weapon. Damage 10, range 2, accuracy 0.8, 1 shot per combat round. No ammunition required.',
        }
        cols = ', '.join(beam_cannon.keys())
        placeholders = ', '.join(['?'] * len(beam_cannon))
        conn.execute(
            f"INSERT OR IGNORE INTO universe.ship_components ({cols}) VALUES ({placeholders})",
            tuple(beam_cannon.values())
        )
        # Idempotently refresh accuracy for existing Beam Cannon rows so DBs
        # migrated before this change get the 0.8 value.
        conn.execute(
            "UPDATE universe.ship_components SET weapon_accuracy = 0.8 "
            "WHERE component_id = 200 AND (weapon_accuracy IS NULL OR weapon_accuracy = 0 OR weapon_accuracy = 1.0)"
        )
        conn.commit()

    # Seed projectile launchers, Laser PD, and magazines (idempotent).
    # All use named columns so they survive future ALTER TABLE reordering.
    projectile_components = [
        {
            'component_id': 220, 'name': 'Missile Launcher Mk1', 'category': 'weapon',
            'st_cost': 20, 'cargo_capacity': 0, 'crew_capacity': 0, 'life_capacity': 0,
            'thrust': 0, 'engine_efficiency': 0, 'sensor_rating': 0,
            'jump_range': 0, 'jump_oc_cost': 0, 'hull_restriction': None,
            'base_price': 4000,
            'weapon_damage': 30, 'weapon_range': 2, 'weapon_shots_per_round': 1,
            'weapon_subcategory': 'missile', 'weapon_requires_ammo': 1,
            'weapon_accuracy': 0.9,
            'ammo_type': 'missile', 'flight_rounds': 1,
            'magazine_missile_capacity': 0, 'magazine_torpedo_capacity': 0,
            'shield_sp_capacity': 0,
            'description': 'Fires guided missiles. Damage 30, range 2, accuracy 0.9, 1 missile per shot, 1-round flight. Consumes 1 missile per shot from the ship\'s magazine. Can be intercepted by Point Defence.',
        },
        {
            'component_id': 230, 'name': 'Torpedo Launcher Mk1', 'category': 'weapon',
            'st_cost': 40, 'cargo_capacity': 0, 'crew_capacity': 0, 'life_capacity': 0,
            'thrust': 0, 'engine_efficiency': 0, 'sensor_rating': 0,
            'jump_range': 0, 'jump_oc_cost': 0, 'hull_restriction': None,
            'base_price': 8000,
            'weapon_damage': 80, 'weapon_range': 2, 'weapon_shots_per_round': 1,
            'weapon_subcategory': 'torpedo', 'weapon_requires_ammo': 1,
            'weapon_accuracy': 0.95,
            'ammo_type': 'torpedo', 'flight_rounds': 2,
            'magazine_missile_capacity': 0, 'magazine_torpedo_capacity': 0,
            'shield_sp_capacity': 0,
            'description': 'Fires heavy torpedoes. Damage 80, range 2, accuracy 0.95, 1 torpedo per shot, 2-round flight. Consumes 1 torpedo per shot from the ship\'s magazine. More vulnerable to Point Defence due to longer flight time.',
        },
        {
            'component_id': 240, 'name': 'Laser Point Defence Mk1', 'category': 'pd',
            'st_cost': 10, 'cargo_capacity': 0, 'crew_capacity': 0, 'life_capacity': 0,
            'thrust': 0, 'engine_efficiency': 0, 'sensor_rating': 0,
            'jump_range': 0, 'jump_oc_cost': 0, 'hull_restriction': None,
            'base_price': 2000,
            'weapon_damage': 0, 'weapon_range': 0, 'weapon_shots_per_round': 4,
            'weapon_subcategory': 'pd', 'weapon_requires_ammo': 0,
            'weapon_accuracy': 0.6,
            'ammo_type': None, 'flight_rounds': 0,
            'magazine_missile_capacity': 0, 'magazine_torpedo_capacity': 0,
            'shield_sp_capacity': 0,
            'description': 'Rapid-fire laser turret that auto-intercepts incoming missiles and torpedoes. 4 shots per round at 0.6 accuracy. Prioritises torpedoes (higher damage) first. No anti-ship capability.',
        },
        {
            'component_id': 250, 'name': 'Missile Magazine Mk1', 'category': 'magazine',
            'st_cost': 20, 'cargo_capacity': 0, 'crew_capacity': 0, 'life_capacity': 0,
            'thrust': 0, 'engine_efficiency': 0, 'sensor_rating': 0,
            'jump_range': 0, 'jump_oc_cost': 0, 'hull_restriction': None,
            'base_price': 500,
            'weapon_damage': 0, 'weapon_range': 0, 'weapon_shots_per_round': 0,
            'weapon_subcategory': None, 'weapon_requires_ammo': 0,
            'weapon_accuracy': 1.0,
            'ammo_type': None, 'flight_rounds': 0,
            'magazine_missile_capacity': 20, 'magazine_torpedo_capacity': 0,
            'shield_sp_capacity': 0,
            'description': 'Holds up to 20 missiles in ready-to-fire state. Load/unload via LOAD MAGAZINE / UNLOAD MAGAZINE orders (1 OC each).',
        },
        {
            'component_id': 260, 'name': 'Torpedo Magazine Mk1', 'category': 'magazine',
            'st_cost': 30, 'cargo_capacity': 0, 'crew_capacity': 0, 'life_capacity': 0,
            'thrust': 0, 'engine_efficiency': 0, 'sensor_rating': 0,
            'jump_range': 0, 'jump_oc_cost': 0, 'hull_restriction': None,
            'base_price': 800,
            'weapon_damage': 0, 'weapon_range': 0, 'weapon_shots_per_round': 0,
            'weapon_subcategory': None, 'weapon_requires_ammo': 0,
            'weapon_accuracy': 1.0,
            'ammo_type': None, 'flight_rounds': 0,
            'magazine_missile_capacity': 0, 'magazine_torpedo_capacity': 5,
            'shield_sp_capacity': 0,
            'description': 'Holds up to 5 torpedoes in ready-to-fire state. Load/unload via LOAD MAGAZINE / UNLOAD MAGAZINE orders (1 OC each).',
        },
    ]
    for comp in projectile_components:
        cols = ', '.join(comp.keys())
        placeholders = ', '.join(['?'] * len(comp))
        conn.execute(
            f"INSERT OR IGNORE INTO universe.ship_components ({cols}) "
            f"VALUES ({placeholders})",
            tuple(comp.values())
        )
    conn.commit()

    # Migrate: create combat tables if missing (idempotent)
    combat_tables_ddl = [
        """CREATE TABLE IF NOT EXISTS ship_combat_lists (
            list_entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id TEXT NOT NULL,
            ship_id INTEGER NOT NULL,
            list_type TEXT NOT NULL,
            entry_type TEXT NOT NULL,
            entry_id INTEGER NOT NULL,
            added_turn_year INTEGER,
            added_turn_week INTEGER,
            UNIQUE(game_id, ship_id, list_type, entry_type, entry_id)
        )""",
        """CREATE TABLE IF NOT EXISTS base_combat_lists (
            list_entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id TEXT NOT NULL,
            base_kind TEXT NOT NULL,
            base_id INTEGER NOT NULL,
            list_type TEXT NOT NULL,
            entry_type TEXT NOT NULL,
            entry_id INTEGER NOT NULL,
            added_turn_year INTEGER,
            added_turn_week INTEGER,
            UNIQUE(game_id, base_kind, base_id, list_type, entry_type, entry_id)
        )""",
        """CREATE TABLE IF NOT EXISTS combat_engagements (
            engagement_id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id TEXT NOT NULL,
            started_turn_year INTEGER NOT NULL,
            started_turn_week INTEGER NOT NULL,
            started_on_round INTEGER NOT NULL,
            last_active_turn_year INTEGER,
            last_active_turn_week INTEGER,
            system_id INTEGER NOT NULL,
            grid_col TEXT,
            grid_row INTEGER,
            status TEXT DEFAULT 'active',
            resolution TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS combat_participants (
            participant_id INTEGER PRIMARY KEY AUTOINCREMENT,
            engagement_id INTEGER NOT NULL,
            participant_kind TEXT NOT NULL,
            participant_id_value INTEGER NOT NULL,
            owner_prefect_id INTEGER,
            joined_turn_year INTEGER,
            joined_turn_week INTEGER,
            joined_on_round INTEGER,
            left_turn_year INTEGER,
            left_turn_week INTEGER,
            left_on_round INTEGER,
            integrity_at_join REAL,
            integrity_at_end REAL,
            status TEXT DEFAULT 'active',
            UNIQUE(engagement_id, participant_kind, participant_id_value)
        )""",
        """CREATE TABLE IF NOT EXISTS combat_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            engagement_id INTEGER NOT NULL,
            turn_year INTEGER NOT NULL,
            turn_week INTEGER NOT NULL,
            round_number INTEGER NOT NULL,
            actor_kind TEXT NOT NULL,
            actor_id INTEGER,
            action TEXT NOT NULL,
            target_kind TEXT,
            target_id INTEGER,
            damage REAL,
            integrity_after REAL,
            detail TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS combat_projectiles (
            projectile_id INTEGER PRIMARY KEY AUTOINCREMENT,
            engagement_id INTEGER NOT NULL,
            launched_turn_year INTEGER,
            launched_turn_week INTEGER,
            launched_on_round INTEGER,
            arrives_on_round INTEGER,
            attacker_kind TEXT,
            attacker_id INTEGER,
            attacker_name TEXT,
            target_kind TEXT,
            target_id INTEGER,
            damage INTEGER,
            accuracy REAL,
            ammo_type TEXT,
            status TEXT DEFAULT 'in-flight'
        )""",
    ]
    for ddl in combat_tables_ddl:
        conn.execute(ddl)
    for idx in [
        "CREATE INDEX IF NOT EXISTS idx_combat_lists_ship ON ship_combat_lists(game_id, ship_id)",
        "CREATE INDEX IF NOT EXISTS idx_combat_lists_base ON base_combat_lists(game_id, base_kind, base_id)",
        "CREATE INDEX IF NOT EXISTS idx_engagement_active ON combat_engagements(game_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_participants_engagement ON combat_participants(engagement_id)",
        "CREATE INDEX IF NOT EXISTS idx_combat_log_engagement ON combat_log(engagement_id, turn_year, turn_week, round_number)",
        "CREATE INDEX IF NOT EXISTS idx_projectiles_engagement ON combat_projectiles(engagement_id, status, arrives_on_round)",
    ]:
        conn.execute(idx)
    conn.commit()

    # Migrate: seed Missile and Torpedo as trade goods (ammo), available at
    # all bases. Prices are fixed (not fluctuating). Cargo mass: missile 1,
    # torpedo 4. Buy prices as spec'd, sell prices lower (60%).
    for item_id, name, price, mass in [
        (501, 'Missile', 100, 1),
        (502, 'Torpedo', 500, 4),
    ]:
        existing = conn.execute(
            "SELECT item_id FROM universe.trade_goods WHERE item_id = ?", (item_id,)
        ).fetchone()
        if not existing:
            conn.execute(
                """INSERT INTO universe.trade_goods
                   (item_id, name, base_price, mass_per_unit, origin_system_id)
                   VALUES (?, ?, ?, ?, NULL)""",
                (item_id, name, price, mass)
            )
    conn.commit()

    # Ensure every starbase has base_trade_config rows for Missile and Torpedo.
    # Idempotent: only inserts if missing. 'average' trade_role so they're
    # available everywhere at standard rates.
    bases = conn.execute(
        "SELECT DISTINCT base_id, game_id FROM base_trade_config"
    ).fetchall()
    # Include starbases that have no trade config yet
    all_starbases = conn.execute(
        "SELECT base_id, game_id FROM starbases "
        "WHERE (status IS NULL OR status = 'active')"
    ).fetchall()
    configs_to_add = set()
    for b in bases:
        configs_to_add.add((b['base_id'], b['game_id']))
    for b in all_starbases:
        configs_to_add.add((b['base_id'], b['game_id']))
    for base_id, game_id in configs_to_add:
        for ammo_id in (501, 502):
            existing = conn.execute(
                "SELECT config_id FROM base_trade_config "
                "WHERE base_id = ? AND game_id = ? AND item_id = ?",
                (base_id, game_id, ammo_id)
            ).fetchone()
            if not existing:
                conn.execute(
                    """INSERT INTO base_trade_config
                       (base_id, game_id, item_id, trade_role)
                       VALUES (?, ?, ?, 'average')""",
                    (base_id, game_id, ammo_id)
                )
    conn.commit()

    # Populate market_prices for missile/torpedo at each base's current cycle
    # so they're immediately available. Idempotent: only inserts if missing.
    # Uses fixed-price values (100/60 for missiles, 500/300 for torpedoes).
    fixed_ammo = {
        501: {'buy': 100, 'sell': 60, 'stock': 500, 'demand': 100},
        502: {'buy': 500, 'sell': 300, 'stock': 100, 'demand': 50},
    }
    existing_cycles = conn.execute(
        "SELECT DISTINCT game_id, base_id, turn_year, turn_week FROM market_prices"
    ).fetchall()
    for c_row in existing_cycles:
        for ammo_id, fp in fixed_ammo.items():
            already = conn.execute(
                """SELECT price_id FROM market_prices
                   WHERE game_id = ? AND base_id = ? AND item_id = ?
                         AND turn_year = ? AND turn_week = ?""",
                (c_row['game_id'], c_row['base_id'], ammo_id,
                 c_row['turn_year'], c_row['turn_week'])
            ).fetchone()
            if not already:
                conn.execute(
                    """INSERT INTO market_prices
                       (game_id, base_id, item_id, turn_year, turn_week,
                        buy_price, sell_price, stock, demand)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (c_row['game_id'], c_row['base_id'], ammo_id,
                     c_row['turn_year'], c_row['turn_week'],
                     fp['buy'], fp['sell'], fp['stock'], fp['demand'])
                )
    conn.commit()

    # ======================================================================
    # STARBASE COMBAT MIGRATIONS (Phase 1)
    # ======================================================================

    # Add combat-related columns to starbases
    sb_cols = [r[1] for r in conn.execute("PRAGMA table_info(starbases)").fetchall()]
    for col, typ_default in [
        ('integrity',       'REAL DEFAULT 0'),
        ('max_integrity',   'REAL DEFAULT 0'),
        ('shield_sp',       'INTEGER DEFAULT 0'),
        ('max_shield_sp',   'INTEGER DEFAULT 0'),
        ('armour',          'INTEGER DEFAULT 0'),
        ('status',          "TEXT DEFAULT 'active'"),
    ]:
        if col not in sb_cols:
            conn.execute(f"ALTER TABLE starbases ADD COLUMN {col} {typ_default}")
    conn.commit()

    # Add combat-related columns to base_modules (weapon stats + shield/armour
    # capacity, for modules that provide defensive or offensive capability).
    bm_cols = [r[1] for r in conn.execute("PRAGMA universe.table_info(base_modules)").fetchall()]
    for col, typ_default in [
        ('weapon_damage',           'INTEGER DEFAULT 0'),
        ('weapon_range',            'INTEGER DEFAULT 0'),
        ('weapon_shots_per_round',  'INTEGER DEFAULT 0'),
        ('weapon_accuracy',         'REAL DEFAULT 1.0'),
        ('shield_sp_capacity',      'INTEGER DEFAULT 0'),
        ('armour_value',            'INTEGER DEFAULT 0'),
    ]:
        if col not in bm_cols:
            conn.execute(f"ALTER TABLE universe.base_modules ADD COLUMN {col} {typ_default}")
    conn.commit()

    # Upgrade Defence Turret (#580): now a proper weapon. Damage 10, range 2,
    # 2 shots/round, accuracy 0.9. Old defence_rating column retained for
    # backward compatibility (unused in combat engine going forward).
    conn.execute(
        """UPDATE universe.base_modules SET
            weapon_damage = 10, weapon_range = 2,
            weapon_shots_per_round = 2, weapon_accuracy = 0.9,
            category = 'weapon',
            description = 'Rapid-fire defensive turret. Damage 10, range 2, 2 shots/round, accuracy 0.9.'
           WHERE module_id = 580
                 AND (weapon_damage IS NULL OR weapon_damage = 0)"""
    )
    # Upgrade Shield Generator (#581): now provides 60 SP per unit.
    conn.execute(
        """UPDATE universe.base_modules SET
            shield_sp_capacity = 60,
            category = 'shield',
            description = 'Provides 60 SP per unit. Thickness = floor(2 × total_SP / 300).'
           WHERE module_id = 581
                 AND (shield_sp_capacity IS NULL OR shield_sp_capacity = 0)"""
    )
    conn.commit()

    # Seed Armour Plating (#582) and Base Point Defence (#583) if missing.
    new_modules = [
        {
            'module_id': 582, 'name': 'Armour Plating', 'category': 'armour',
            'employees_required': 0, 'location_restriction': None,
            'docking_slots': 0, 'mining_capacity': 0, 'factory_capacity': 0,
            'repair_capacity': 0, 'market_income': 0, 'storage_capacity': 0,
            'habitat_capacity': 0, 'defence_rating': 0, 'base_price': 4000,
            'sensor_rating': 0,
            'weapon_damage': 0, 'weapon_range': 0, 'weapon_shots_per_round': 0,
            'weapon_accuracy': 1.0, 'shield_sp_capacity': 0, 'armour_value': 2,
            'description': 'Heavy armour plating. +2 armour per unit (non-ablative, stacks).',
        },
        {
            'module_id': 583, 'name': 'Base Point Defence', 'category': 'pd',
            'employees_required': 1, 'location_restriction': None,
            'docking_slots': 0, 'mining_capacity': 0, 'factory_capacity': 0,
            'repair_capacity': 0, 'market_income': 0, 'storage_capacity': 0,
            'habitat_capacity': 0, 'defence_rating': 0, 'base_price': 2500,
            'sensor_rating': 0,
            'weapon_damage': 0, 'weapon_range': 0, 'weapon_shots_per_round': 6,
            'weapon_accuracy': 0.7, 'shield_sp_capacity': 0, 'armour_value': 0,
            'description': 'Rapid-fire laser turret for intercepting incoming missiles/torpedoes. 6 shots/round at 0.7 accuracy. Prioritises torpedoes first.',
        },
    ]
    for m in new_modules:
        cols = ', '.join(m.keys())
        placeholders = ', '.join(['?'] * len(m))
        conn.execute(
            f"INSERT OR IGNORE INTO universe.base_modules ({cols}) "
            f"VALUES ({placeholders})",
            tuple(m.values())
        )
    conn.commit()

    # Recompute max_integrity, max_shield_sp, armour for all existing starbases.
    # Preserves current integrity percentage if set. Sets shield_sp = max
    # for ports that had no prior shield tracking.
    for sb in conn.execute("SELECT base_id FROM starbases").fetchall():
        # Sum installed modules: count, shield_sp, armour
        mods = conn.execute(
            """SELECT COALESCE(SUM(im.quantity), 0) AS total_count,
                      COALESCE(SUM(im.quantity * bm.shield_sp_capacity), 0) AS sp_cap,
                      COALESCE(SUM(im.quantity * bm.armour_value), 0) AS armour_tot
               FROM installed_modules im
               JOIN universe.base_modules bm ON im.module_id = bm.module_id
               WHERE im.starbase_id = ?""",
            (sb['base_id'],)
        ).fetchone()
        module_count = mods['total_count'] or 0
        max_sp = int(mods['sp_cap'] or 0)
        armour_tot = int(mods['armour_tot'] or 0)
        max_hp = BASE_HP_PER_MODULE * module_count
        # If this starbase has no combat state yet, initialise to full
        cur = conn.execute(
            "SELECT integrity, max_integrity, shield_sp, max_shield_sp, armour FROM starbases WHERE base_id = ?",
            (sb['base_id'],)
        ).fetchone()
        old_max = cur['max_integrity'] or 0
        # Determine new integrity: preserve percentage if there was one, else full
        if old_max > 0 and cur['integrity'] is not None:
            pct = (cur['integrity'] / old_max) if old_max else 1.0
            new_integ = max_hp * pct
        else:
            new_integ = max_hp  # fresh base: full HP
        # Shield SP: preserve ratio to old max if available, else full
        old_sp_max = cur['max_shield_sp'] or 0
        if old_sp_max > 0 and cur['shield_sp'] is not None:
            sp_pct = (cur['shield_sp'] / old_sp_max) if old_sp_max else 1.0
            new_sp = int(max_sp * sp_pct)
        else:
            new_sp = max_sp
        conn.execute(
            """UPDATE starbases SET
                max_integrity = ?, integrity = ?,
                max_shield_sp = ?, shield_sp = ?,
                armour = ?
               WHERE base_id = ?""",
            (max_hp, new_integ, max_sp, new_sp, armour_tot, sb['base_id'])
        )
    conn.commit()

    # ======================================================================
    # KNOWLEDGE SYSTEM MIGRATIONS (Phase 1)
    # ======================================================================
    # is_public flags on gated object types
    for tbl, default in [
        ('star_systems',      0),
        ('starbases',         0),
        ('celestial_bodies',  0),
    ]:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()]
        if 'is_public' not in cols:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN is_public INTEGER DEFAULT {default}")

    # trade_goods lives in universe DB and defaults public (1)
    cols = [r[1] for r in conn.execute("PRAGMA universe.table_info(trade_goods)").fetchall()]
    if 'is_public' not in cols:
        conn.execute("ALTER TABLE universe.trade_goods ADD COLUMN is_public INTEGER DEFAULT 1")

    # Also add is_public to surface_ports and outposts (both private by default —
    # you have to discover them). They don't need any combat columns but the
    # knowledge flag applies.
    for tbl in ('surface_ports', 'outposts'):
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()]
            if cols and 'is_public' not in cols:
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN is_public INTEGER DEFAULT 0")
        except Exception:
            pass
    conn.commit()

    # Generic prefect-knowledge table. One row per (prefect, object_type,
    # object_id). Existence knowledge is recorded here; transient sightings
    # (current location etc) remain in known_contacts. surface_scanned is
    # meaningful only for celestial_body entries.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prefect_knowledge (
            knowledge_id INTEGER PRIMARY KEY AUTOINCREMENT,
            prefect_id   INTEGER NOT NULL,
            game_id      TEXT NOT NULL,
            object_type  TEXT NOT NULL,
            object_id    INTEGER NOT NULL,
            discovered_turn_year INTEGER,
            discovered_turn_week INTEGER,
            surface_scanned INTEGER DEFAULT 0,
            UNIQUE (prefect_id, game_id, object_type, object_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prefect_knowledge_lookup "
        "ON prefect_knowledge (prefect_id, object_type)"
    )
    conn.commit()

    # Seed public flags: mark the three starter systems (existing as of this
    # migration) and all their contents as public. Newly-generated systems
    # are private by default.
    starter_ids = [row['system_id'] for row in conn.execute(
        "SELECT system_id FROM star_systems ORDER BY system_id LIMIT 3"
    ).fetchall()]
    for sid in starter_ids:
        conn.execute(
            "UPDATE star_systems SET is_public = 1 WHERE system_id = ?",
            (sid,)
        )
        conn.execute(
            "UPDATE celestial_bodies SET is_public = 1 WHERE system_id = ?",
            (sid,)
        )
        conn.execute(
            "UPDATE starbases SET is_public = 1 WHERE system_id = ?",
            (sid,)
        )
    conn.commit()

    # Migration backfill (Option B with one-time grace): grant all existing
    # prefects prefect_knowledge for:
    #  (a) every system any of their ships is currently in
    #  (b) every system referenced in their known_contacts
    #  (c) every system where they own a starbase / port / outpost
    #  (d) every system connected by a jump route to any of (a)(b)(c) — the
    #      one-time SURVEY grace so existing saves don't break
    #  (e) all celestial bodies in those systems (existence only)
    #  (f) all starbases/ports/outposts in those systems
    # RUNS ONCE PER GAME: guarded by a schema_migrations row. Otherwise
    # moving a ship for testing would auto-re-grant neighbours via step (d).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            migration_name TEXT PRIMARY KEY,
            applied_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    already_applied = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE migration_name = ?",
        ('knowledge_backfill_v1',)
    ).fetchone()

    prefects = conn.execute("SELECT prefect_id, game_id FROM prefects").fetchall()
    if prefects and not already_applied:
        # Build system links adjacency once
        links = conn.execute("SELECT system_a, system_b FROM system_links").fetchall()
        adj = {}
        for link in links:
            a, b = link['system_a'], link['system_b']
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)

        for p in prefects:
            pid = p['prefect_id']
            gid = p['game_id']
            visited = set()
            # (a) ship positions
            for r in conn.execute(
                "SELECT DISTINCT system_id FROM ships "
                "WHERE owner_prefect_id = ? AND system_id IS NOT NULL",
                (pid,)
            ).fetchall():
                if r['system_id']:
                    visited.add(r['system_id'])
            # (b) known_contacts (systems referenced there)
            for r in conn.execute(
                "SELECT DISTINCT location_system FROM known_contacts "
                "WHERE prefect_id = ? AND location_system IS NOT NULL",
                (pid,)
            ).fetchall():
                if r['location_system']:
                    visited.add(r['location_system'])
            # (c) systems where prefect owns any base
            for tbl, idcol in (('starbases', 'base_id'),
                                 ('surface_ports', 'port_id'),
                                 ('outposts', 'outpost_id')):
                try:
                    for r in conn.execute(
                        f"SELECT DISTINCT system_id FROM {tbl} "
                        f"WHERE owner_prefect_id = ? AND system_id IS NOT NULL",
                        (pid,)
                    ).fetchall():
                        if r['system_id']:
                            visited.add(r['system_id'])
                except Exception:
                    pass

            # (d) one-hop neighbours of anything in `visited` (survey grace)
            surveyed = set(visited)
            for sid in list(visited):
                for nbr in adj.get(sid, set()):
                    surveyed.add(nbr)

            # Write knowledge rows for genuinely private systems/bodies/bases
            # only. Public ones are known by default — duplicating into
            # prefect_knowledge would just be noise.
            for sid in surveyed:
                # Skip if system is already public
                pub = conn.execute(
                    "SELECT is_public FROM star_systems WHERE system_id = ?",
                    (sid,)
                ).fetchone()
                if pub and pub['is_public']:
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO prefect_knowledge
                       (prefect_id, game_id, object_type, object_id,
                        discovered_turn_year, discovered_turn_week)
                       VALUES (?, ?, 'star_system', ?, NULL, NULL)""",
                    (pid, gid, sid)
                )

            # Bodies and bases in fully-visited systems only
            for sid in visited:
                # Skip entire visited system if it's public — everything
                # inside is public by extension
                sys_pub = conn.execute(
                    "SELECT is_public FROM star_systems WHERE system_id = ?",
                    (sid,)
                ).fetchone()
                system_is_public = bool(sys_pub['is_public']) if sys_pub else False
                for r in conn.execute(
                    "SELECT body_id, is_public FROM celestial_bodies WHERE system_id = ?",
                    (sid,)
                ).fetchall():
                    if (r['is_public'] or 0) and system_is_public:
                        continue
                    conn.execute(
                        """INSERT OR IGNORE INTO prefect_knowledge
                           (prefect_id, game_id, object_type, object_id,
                            discovered_turn_year, discovered_turn_week)
                           VALUES (?, ?, 'celestial_body', ?, NULL, NULL)""",
                        (pid, gid, r['body_id'])
                    )
                for r in conn.execute(
                    "SELECT base_id, is_public FROM starbases "
                    "WHERE system_id = ? AND game_id = ?",
                    (sid, gid)
                ).fetchall():
                    if r['is_public'] or 0:
                        continue
                    conn.execute(
                        """INSERT OR IGNORE INTO prefect_knowledge
                           (prefect_id, game_id, object_type, object_id,
                            discovered_turn_year, discovered_turn_week)
                           VALUES (?, ?, 'starbase', ?, NULL, NULL)""",
                        (pid, gid, r['base_id'])
                    )
                try:
                    for r in conn.execute(
                        "SELECT port_id, is_public FROM surface_ports "
                        "WHERE system_id = ? AND game_id = ?",
                        (sid, gid)
                    ).fetchall():
                        if r['is_public'] or 0:
                            continue
                        conn.execute(
                            """INSERT OR IGNORE INTO prefect_knowledge
                               (prefect_id, game_id, object_type, object_id,
                                discovered_turn_year, discovered_turn_week)
                               VALUES (?, ?, 'surface_port', ?, NULL, NULL)""",
                            (pid, gid, r['port_id'])
                        )
                except Exception:
                    pass
                try:
                    for r in conn.execute(
                        "SELECT outpost_id, is_public FROM outposts "
                        "WHERE system_id = ? AND game_id = ?",
                        (sid, gid)
                    ).fetchall():
                        if r['is_public'] or 0:
                            continue
                        conn.execute(
                            """INSERT OR IGNORE INTO prefect_knowledge
                               (prefect_id, game_id, object_type, object_id,
                                discovered_turn_year, discovered_turn_week)
                               VALUES (?, ?, 'outpost', ?, NULL, NULL)""",
                            (pid, gid, r['outpost_id'])
                        )
                except Exception:
                    pass
    conn.commit()

    # End of knowledge migrations: stamp the backfill as applied so it
    # doesn't re-grant every time the DB reopens.
    if prefects and not already_applied:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (migration_name) VALUES (?)",
            ('knowledge_backfill_v1',)
        )
        conn.commit()


    # Migrate: add detection detail columns to known_contacts
    kc_cols = [r[1] for r in conn.execute("PRAGMA table_info(known_contacts)").fetchall()]
    for col, ddl in [
        ('scanner_ship_id',   'ALTER TABLE known_contacts ADD COLUMN scanner_ship_id INTEGER'),
        ('target_faction_id', 'ALTER TABLE known_contacts ADD COLUMN target_faction_id INTEGER'),
        ('target_hull_type',  'ALTER TABLE known_contacts ADD COLUMN target_hull_type TEXT'),
        ('target_ship_size',  'ALTER TABLE known_contacts ADD COLUMN target_ship_size INTEGER'),
        ('detection_range',   'ALTER TABLE known_contacts ADD COLUMN detection_range INTEGER'),
        ('detected_on_tick',  'ALTER TABLE known_contacts ADD COLUMN detected_on_tick INTEGER'),
        ('detection_source',  "ALTER TABLE known_contacts ADD COLUMN detection_source TEXT DEFAULT 'passive'"),
    ]:
        if col not in kc_cols:
            conn.execute(ddl)
    conn.commit()

    # Migrate: add is_gm to players if missing
    player_cols = [r[1] for r in conn.execute("PRAGMA table_info(players)").fetchall()]
    if 'is_gm' not in player_cols:
        conn.execute("ALTER TABLE players ADD COLUMN is_gm INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    # Migrate: add unlimited_credits to prefects if missing
    prefect_cols = [r[1] for r in conn.execute("PRAGMA table_info(prefects)").fetchall()]
    if 'unlimited_credits' not in prefect_cols:
        conn.execute("ALTER TABLE prefects ADD COLUMN unlimited_credits INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    # Cleanup: if prefects_old exists from a previous failed migration, drop it
    stale_old = conn.execute(
        "SELECT name FROM main.sqlite_master WHERE type='table' AND name='prefects_old'"
    ).fetchone()
    if stale_old:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TABLE IF EXISTS prefects_old")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")

    # Migrate: remove UNIQUE constraint on prefects.player_id (allows GM multiple prefects)
    create_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='prefects'"
    ).fetchone()
    if create_sql and 'UNIQUE' in create_sql['sql'] and 'player_id' in create_sql['sql']:
        # Temporarily disable foreign keys for table rebuild
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("ALTER TABLE prefects RENAME TO prefects_old")
        conn.execute("""CREATE TABLE prefects (
            prefect_id INTEGER PRIMARY KEY,
            player_id INTEGER NOT NULL,
            game_id TEXT NOT NULL,
            name TEXT NOT NULL,
            faction_id INTEGER DEFAULT 11,
            rank TEXT DEFAULT 'Citizen',
            credits REAL NOT NULL DEFAULT 10000,
            unlimited_credits INTEGER NOT NULL DEFAULT 0,
            influence INTEGER DEFAULT 0,
            location_type TEXT DEFAULT 'ship',
            location_id INTEGER,
            created_turn_year INTEGER,
            created_turn_week INTEGER,
            FOREIGN KEY (player_id) REFERENCES players(player_id),
            FOREIGN KEY (game_id) REFERENCES games(game_id)
        )""")
        old_cols = [r[1] for r in conn.execute("PRAGMA table_info(prefects_old)").fetchall()]
        new_cols = [r[1] for r in conn.execute("PRAGMA table_info(prefects)").fetchall()]
        shared = [c for c in new_cols if c in old_cols]
        cols_str = ', '.join(shared)
        conn.execute(f"INSERT INTO prefects ({cols_str}) SELECT {cols_str} FROM prefects_old")
        conn.execute("DROP TABLE prefects_old")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")

    return conn


def get_universe_connection(universe_db_path=None):
    """Direct connection to universe.db for admin/editing. No ATTACH."""
    path = Path(universe_db_path) if universe_db_path else UNIVERSE_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ======================================================================
# UNIVERSE DATABASE SCHEMA (GM-editable world definition)
# ======================================================================

UNIVERSE_SCHEMA = """
-- Universe schema version tracking
CREATE TABLE IF NOT EXISTS universe_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Star systems
CREATE TABLE IF NOT EXISTS star_systems (
    system_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    star_name TEXT DEFAULT NULL,
    star_spectral_type TEXT DEFAULT NULL,
    star_grid_col TEXT DEFAULT NULL,
    star_grid_row INTEGER DEFAULT NULL,
    created_turn TEXT
);

-- System links (interstellar connections)
CREATE TABLE IF NOT EXISTS system_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    system_a INTEGER NOT NULL,
    system_b INTEGER NOT NULL,
    known_by_default INTEGER DEFAULT 0,
    created_turn TEXT,
    FOREIGN KEY (system_a) REFERENCES star_systems(system_id),
    FOREIGN KEY (system_b) REFERENCES star_systems(system_id),
    UNIQUE(system_a, system_b)
);

-- Celestial bodies: planets, moons, gas giants, asteroids
CREATE TABLE IF NOT EXISTS celestial_bodies (
    body_id INTEGER PRIMARY KEY,
    system_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    body_type TEXT NOT NULL CHECK(body_type IN ('planet', 'moon', 'gas_giant', 'asteroid')),
    parent_body_id INTEGER,
    grid_col TEXT NOT NULL,
    grid_row INTEGER NOT NULL,
    gravity REAL DEFAULT 1.0,
    temperature INTEGER DEFAULT 300,
    atmosphere TEXT DEFAULT 'Standard',
    tectonic_activity INTEGER DEFAULT 0,
    hydrosphere INTEGER DEFAULT 0,
    life TEXT DEFAULT 'None',
    map_symbol TEXT NOT NULL DEFAULT 'O',
    surface_size INTEGER NOT NULL DEFAULT 31,
    resource_id INTEGER DEFAULT NULL,
    created_turn TEXT,
    FOREIGN KEY (system_id) REFERENCES star_systems(system_id),
    FOREIGN KEY (parent_body_id) REFERENCES celestial_bodies(body_id)
);

-- Factions
CREATE TABLE IF NOT EXISTS factions (
    faction_id INTEGER PRIMARY KEY,
    abbreviation TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT DEFAULT ''
);

-- Trade goods catalogue (what items exist in the universe)
CREATE TABLE IF NOT EXISTS trade_goods (
    item_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    base_price INTEGER NOT NULL,
    mass_per_unit INTEGER NOT NULL,
    origin_system_id INTEGER DEFAULT NULL
);

-- Planetary resources (GM-only, not visible to players)
-- Resources are separate from trade goods. When mined (future), a resource
-- produces the linked trade good. Discovery mechanics TBD.
CREATE TABLE IF NOT EXISTS resources (
    resource_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    produces_item_id INTEGER DEFAULT NULL,
    FOREIGN KEY (produces_item_id) REFERENCES trade_goods(item_id)
);

-- Seed factions
INSERT OR IGNORE INTO factions (faction_id, abbreviation, name, description)
VALUES (11, 'STA', 'Stellar Training Academy', 'Default starting faction for new players');
INSERT OR IGNORE INTO factions (faction_id, abbreviation, name, description)
VALUES (12, 'MTG', 'Merchant Trade Guild', 'A coalition of traders and commerce-focused captains');
INSERT OR IGNORE INTO factions (faction_id, abbreviation, name, description)
VALUES (13, 'IMP', 'Imperial Navy', 'Military arm of the Terran Empire');
INSERT OR IGNORE INTO factions (faction_id, abbreviation, name, description)
VALUES (14, 'FRN', 'Frontier Coalition', 'Independent settlers and explorers of the outer systems');
INSERT OR IGNORE INTO factions (faction_id, abbreviation, name, description)
VALUES (15, 'SYN', 'Syndicate', 'A shadowy network of smugglers, pirates, and opportunists');
INSERT OR IGNORE INTO factions (faction_id, abbreviation, name, description)
VALUES (0, 'IND', 'Independent', 'No faction affiliation');

-- Ship component catalogue (what components can be installed on ships)
-- 3-digit IDs for future trading. Category groups:
--   100-109: Bridge/Command   110-119: Thrusters    120-129: Sublight Engines
--   130-139: Cargo Systems    140-149: Crew/Life    150-159: Sensors
--   160-169: Jump Drives      170-179: Reserved (weapons/shields)
CREATE TABLE IF NOT EXISTS ship_components (
    component_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    st_cost INTEGER NOT NULL,
    cargo_capacity INTEGER DEFAULT 0,
    crew_capacity INTEGER DEFAULT 0,
    life_capacity INTEGER DEFAULT 0,
    thrust INTEGER DEFAULT 0,
    engine_efficiency REAL DEFAULT 0,
    sensor_rating INTEGER DEFAULT 0,
    jump_range INTEGER DEFAULT 0,
    jump_oc_cost INTEGER DEFAULT 0,
    hull_restriction TEXT DEFAULT NULL,
    base_price INTEGER DEFAULT 0,
    weapon_damage INTEGER DEFAULT 0,
    weapon_range INTEGER DEFAULT 0,
    weapon_shots_per_round INTEGER DEFAULT 0,
    weapon_subcategory TEXT DEFAULT NULL,
    weapon_requires_ammo INTEGER DEFAULT 0,
    description TEXT DEFAULT ''
);

-- Seed ship components
INSERT OR IGNORE INTO ship_components VALUES
    (100, 'Standard Bridge', 'bridge', 50, 0, 0, 0, 0, 0, 0, 0, 0, NULL, 500, 0, 0, 0, NULL, 0, 'Basic command centre. Required for ship operation.');
INSERT OR IGNORE INTO ship_components VALUES
    (110, 'Thruster Array', 'thruster', 20, 0, 0, 0, 5, 0, 0, 0, 0, NULL, 800, 0, 0, 0, NULL, 0, 'Standard thruster pack. Provides thrust for gravity rating.');
INSERT OR IGNORE INTO ship_components VALUES
    (111, 'Heavy Thruster Pack', 'thruster', 30, 0, 0, 0, 10, 0, 0, 0, 0, NULL, 1500, 0, 0, 0, NULL, 0, 'High-output thrusters for larger vessels or heavy landing.');
INSERT OR IGNORE INTO ship_components VALUES
    (120, 'Commercial Sublight Engine', 'engine', 10, 0, 0, 0, 0, 1.0, 0, 0, 0, NULL, 1200, 0, 0, 0, NULL, 0, 'Standard propulsion. 1.0 efficiency.');
INSERT OR IGNORE INTO ship_components VALUES
    (121, 'Military Sublight Engine', 'engine', 10, 0, 0, 0, 0, 1.5, 0, 0, 0, 'military', 2500, 0, 0, 0, NULL, 0, 'High-performance drive. 1.5 efficiency. Military hulls only.');
INSERT OR IGNORE INTO ship_components VALUES
    (130, 'Cargo Bay', 'cargo', 25, 20, 0, 0, 0, 0, 0, 0, 0, NULL, 600, 0, 0, 0, NULL, 0, 'Standard modular cargo bay. 20 ST capacity.');
INSERT OR IGNORE INTO ship_components VALUES
    (131, 'Reinforced Cargo Bay', 'cargo', 30, 20, 0, 0, 0, 0, 0, 0, 0, NULL, 900, 0, 0, 0, NULL, 0, 'Armoured cargo storage. 20 ST capacity.');
INSERT OR IGNORE INTO ship_components VALUES
    (140, 'Crew Quarters', 'quarters', 30, 0, 20, 20, 0, 0, 0, 0, 0, NULL, 400, 0, 0, 0, NULL, 0, 'Standard crew accommodation with life support.');
INSERT OR IGNORE INTO ship_components VALUES
    (141, 'Military Bunks', 'quarters', 30, 0, 40, 25, 0, 0, 0, 0, 0, 'military', 500, 0, 0, 0, NULL, 0, 'Compact military berths. High crew capacity.');
INSERT OR IGNORE INTO ship_components VALUES
    (142, 'Luxury Cabins', 'quarters', 30, 0, 10, 15, 0, 0, 0, 0, 0, NULL, 700, 0, 0, 0, NULL, 0, 'Comfortable passenger cabins. Low density.');
INSERT OR IGNORE INTO ship_components VALUES
    (150, 'Basic Sensor Array', 'sensor', 10, 0, 0, 0, 0, 0, 5, 0, 0, NULL, 300, 0, 0, 0, NULL, 0, 'Standard detection and scanning suite.');
INSERT OR IGNORE INTO ship_components VALUES
    (151, 'Military Sensor Suite', 'sensor', 15, 0, 0, 0, 0, 0, 10, 0, 0, 'military', 1000, 0, 0, 0, NULL, 0, 'Advanced military-grade sensors.');
INSERT OR IGNORE INTO ship_components VALUES
    (152, 'Deep Space Scanner', 'sensor', 20, 0, 0, 0, 0, 0, 15, 0, 0, NULL, 1800, 0, 0, 0, NULL, 0, 'Long-range deep space detection system.');
INSERT OR IGNORE INTO ship_components VALUES
    (160, 'Jump Drive Mk1', 'jump_drive', 50, 0, 0, 0, 0, 0, 0, 5, 50, NULL, 5000, 0, 0, 0, NULL, 0, 'Basic hyperspace jump drive. Range 5 systems, 50 OC per activation.');
INSERT OR IGNORE INTO ship_components VALUES
    (161, 'Jump Drive Mk2', 'jump_drive', 60, 0, 0, 0, 0, 0, 0, 6, 40, NULL, 12000, 0, 0, 0, NULL, 0, 'Advanced jump drive. Range 6 systems, 40 OC per activation.');
INSERT OR IGNORE INTO ship_components VALUES
    (200, 'Beam Cannon Mk1', 'weapon', 15, 0, 0, 0, 0, 0, 0, 0, 0, NULL, 2500,
     10, 2, 1, 'beam', 0,
     'Standard energy beam weapon. Damage 10, range 2, 1 shot per combat round. No ammunition required.');

-- Base module catalogue (what modules can be installed on starbases/ports/outposts)
-- 3-digit IDs in 500-599 range. Category groups:
--   500-509: Command       510-519: Docking      520-529: Mining
--   530-539: Factory       540-549: Maintenance   550-559: Market
--   560-569: Storage       570-579: Habitat       580-589: Defence
CREATE TABLE IF NOT EXISTS base_modules (
    module_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    employees_required INTEGER DEFAULT 10,
    location_restriction TEXT DEFAULT NULL,
    docking_slots INTEGER DEFAULT 0,
    mining_capacity INTEGER DEFAULT 0,
    factory_capacity INTEGER DEFAULT 0,
    repair_capacity INTEGER DEFAULT 0,
    market_income INTEGER DEFAULT 0,
    storage_capacity INTEGER DEFAULT 0,
    habitat_capacity INTEGER DEFAULT 0,
    defence_rating INTEGER DEFAULT 0,
    sensor_rating INTEGER DEFAULT 0,
    base_price INTEGER DEFAULT 0,
    description TEXT DEFAULT ''
);

-- Seed base modules
INSERT OR IGNORE INTO base_modules VALUES
    (500, 'Command Module', 'command', 10, NULL, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2000,
     '1 required per 100 modules for 100% command efficiency.');
INSERT OR IGNORE INTO base_modules VALUES
    (510, 'Docking Bay', 'dock', 20, 'starbase', 1, 0, 0, 0, 0, 0, 0, 0, 0, 5000,
     'Allows one ship to dock. Starbase only.');
INSERT OR IGNORE INTO base_modules VALUES
    (511, 'Heavy Docking Bay', 'dock', 30, 'starbase', 1, 0, 0, 0, 0, 0, 0, 0, 0, 8000,
     'Reinforced bay for larger vessels. Starbase only.');
INSERT OR IGNORE INTO base_modules VALUES
    (520, 'Mining Rig', 'mining', 15, 'surface', 0, 10, 0, 0, 0, 0, 0, 0, 0, 3000,
     'Extracts planetary resources. Surface port or outpost only.');
INSERT OR IGNORE INTO base_modules VALUES
    (521, 'Deep Core Drill', 'mining', 25, 'surface', 0, 25, 0, 0, 0, 0, 0, 0, 0, 7000,
     'Heavy mining for deep deposits. Surface port or outpost only.');
INSERT OR IGNORE INTO base_modules VALUES
    (530, 'Assembly Plant', 'factory', 25, NULL, 0, 0, 10, 0, 0, 0, 0, 0, 0, 6000,
     'Constructs items from raw materials.');
INSERT OR IGNORE INTO base_modules VALUES
    (531, 'Advanced Fabricator', 'factory', 40, NULL, 0, 0, 25, 0, 0, 0, 0, 0, 0, 12000,
     'High-tech manufacturing facility.');
INSERT OR IGNORE INTO base_modules VALUES
    (540, 'Repair Bay', 'maintenance', 15, 'starbase', 0, 0, 0, 5, 0, 0, 0, 0, 0, 4000,
     'Repairs ship integrity. Starbase only.');
INSERT OR IGNORE INTO base_modules VALUES
    (541, 'Shipyard', 'maintenance', 30, 'starbase', 0, 0, 0, 15, 0, 0, 0, 0, 0, 10000,
     'Full shipyard for major repairs and refits. Starbase only.');
INSERT OR IGNORE INTO base_modules VALUES
    (550, 'Trade Market', 'market', 10, 'surface', 0, 0, 0, 0, 100, 0, 0, 0, 0, 3000,
     'Enables trade with planetary population. Surface port only. Generates background income.');
INSERT OR IGNORE INTO base_modules VALUES
    (551, 'Commerce Hub', 'market', 20, 'surface', 0, 0, 0, 0, 250, 0, 0, 0, 0, 8000,
     'Large-scale trading hub. Surface port only. Higher income.');
INSERT OR IGNORE INTO base_modules VALUES
    (560, 'Storage Warehouse', 'storage', 5, NULL, 0, 0, 0, 0, 0, 500, 0, 0, 0, 1500,
     'Bulk storage for goods and materials. 500 ST capacity.');
INSERT OR IGNORE INTO base_modules VALUES
    (561, 'Secure Vault', 'storage', 8, NULL, 0, 0, 0, 0, 0, 200, 0, 0, 0, 3000,
     'Armoured storage for valuables. 200 ST capacity.');
INSERT OR IGNORE INTO base_modules VALUES
    (570, 'Habitat Block', 'habitat', 2, NULL, 0, 0, 0, 0, 0, 0, 50, 0, 0, 2000,
     'Housing for 50 employees.');
INSERT OR IGNORE INTO base_modules VALUES
    (571, 'Life Dome', 'habitat', 3, 'surface', 0, 0, 0, 0, 0, 0, 100, 0, 0, 4000,
     'Pressurised dome housing 100 employees. Surface only.');
INSERT OR IGNORE INTO base_modules VALUES
    (580, 'Defence Turret', 'defence', 10, NULL, 0, 0, 0, 0, 0, 0, 0, 5, 0, 3500,
     'Automated defensive weapon emplacement.');
INSERT OR IGNORE INTO base_modules VALUES
    (581, 'Shield Generator', 'defence', 15, NULL, 0, 0, 0, 0, 0, 0, 0, 10, 0, 6000,
     'Energy shield protecting the installation.');
INSERT OR IGNORE INTO base_modules VALUES
    (590, 'Sensor Suite', 'sensor', 5, NULL, 0, 0, 0, 0, 0, 0, 0, 0, 15, 4000,
     'Passive sensor array. Detects nearby ships and objects. Multiple suites stack with diminishing returns.');
INSERT OR IGNORE INTO base_modules VALUES
    (591, 'Deep Scan Array', 'sensor', 10, NULL, 0, 0, 0, 0, 0, 0, 0, 0, 35, 9000,
     'High-power sensor array with greater range and accuracy.');

-- Planet surface grid (31x31 terrain tiles per body, generated lazily)
-- Intrinsic to the universe: terrain is world definition, not game state.
CREATE TABLE IF NOT EXISTS planet_surface (
    body_id INTEGER NOT NULL,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    terrain_type TEXT NOT NULL,
    PRIMARY KEY (body_id, x, y),
    FOREIGN KEY (body_id) REFERENCES celestial_bodies(body_id)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_bodies_system ON celestial_bodies(system_id);
CREATE INDEX IF NOT EXISTS idx_links_a ON system_links(system_a);
CREATE INDEX IF NOT EXISTS idx_links_b ON system_links(system_b);
"""


def init_universe_db(db_path=None):
    """Create/initialise universe.db with world definition tables."""
    path = Path(db_path) if db_path else UNIVERSE_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(UNIVERSE_SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO universe_meta (key, value) VALUES ('schema_version', '1')"
    )
    conn.commit()
    return conn


# ======================================================================
# GAME STATE DATABASE SCHEMA (engine-managed, backed up per turn)
# ======================================================================

STATE_SCHEMA = """
-- Game metadata
CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,
    game_name TEXT NOT NULL,
    current_year INTEGER NOT NULL DEFAULT 500,
    current_week INTEGER NOT NULL DEFAULT 1,
    turn_status TEXT NOT NULL DEFAULT 'open',
    schema_version INTEGER NOT NULL DEFAULT 1,
    rng_seed TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Players
CREATE TABLE IF NOT EXISTS players (
    player_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    player_name TEXT NOT NULL,
    email TEXT NOT NULL,
    account_number TEXT NOT NULL UNIQUE,
    is_gm INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

-- Prefect positions (one per regular player; GM can have many)
CREATE TABLE IF NOT EXISTS prefects (
    prefect_id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL,
    game_id TEXT NOT NULL,
    name TEXT NOT NULL,
    faction_id INTEGER DEFAULT 11,
    rank TEXT DEFAULT 'Citizen',
    credits REAL NOT NULL DEFAULT 10000,
    unlimited_credits INTEGER NOT NULL DEFAULT 0,
    influence INTEGER DEFAULT 0,
    location_type TEXT DEFAULT 'ship',
    location_id INTEGER,
    created_turn_year INTEGER,
    created_turn_week INTEGER,
    FOREIGN KEY (player_id) REFERENCES players(player_id),
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

-- Ships
CREATE TABLE IF NOT EXISTS ships (
    ship_id INTEGER PRIMARY KEY,
    game_id TEXT NOT NULL,
    owner_prefect_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    ship_class TEXT DEFAULT 'Trader',
    design TEXT DEFAULT 'Light Trader MK I',
    hull_type TEXT DEFAULT 'Commercial',
    ship_size INTEGER DEFAULT 50,
    hull_count INTEGER DEFAULT 50,
    hull_damage_pct REAL DEFAULT 0.0,
    grid_col TEXT NOT NULL,
    grid_row INTEGER NOT NULL,
    system_id INTEGER NOT NULL,
    docked_at_base_id INTEGER,
    orbiting_body_id INTEGER,
    landed_body_id INTEGER,
    landed_x INTEGER DEFAULT 1,
    landed_y INTEGER DEFAULT 1,
    gravity_rating REAL DEFAULT 1.5,
    tu_per_turn INTEGER NOT NULL DEFAULT 300,
    tu_remaining INTEGER NOT NULL DEFAULT 300,
    sensor_rating INTEGER DEFAULT 20,
    sensor_profile REAL DEFAULT 0.5,
    cargo_capacity INTEGER DEFAULT 500,
    cargo_used INTEGER DEFAULT 0,
    crew_count INTEGER DEFAULT 10,
    crew_required INTEGER DEFAULT 10,
    life_support_capacity INTEGER DEFAULT 20,
    efficiency REAL DEFAULT 100.0,
    integrity REAL DEFAULT 50.0,
    max_integrity REAL DEFAULT 50.0,
    combat_doctrine TEXT DEFAULT 'defensive',
    armour INTEGER DEFAULT 0,
    shield_sp INTEGER DEFAULT 0,
    max_shield_sp INTEGER DEFAULT 0,
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (owner_prefect_id) REFERENCES prefects(prefect_id)
);

-- Surface ports (ground facilities; built first, starbase constructed above)
CREATE TABLE IF NOT EXISTS surface_ports (
    port_id INTEGER PRIMARY KEY,
    game_id TEXT NOT NULL,
    name TEXT NOT NULL,
    body_id INTEGER NOT NULL,
    surface_x INTEGER NOT NULL,
    surface_y INTEGER NOT NULL,
    owner_prefect_id INTEGER,
    complexes INTEGER DEFAULT 0,
    workers INTEGER DEFAULT 0,
    troops INTEGER DEFAULT 0,
    employees INTEGER DEFAULT 0,
    employee_capacity INTEGER DEFAULT 0,
    sensor_profile REAL DEFAULT 1.0,
    sensor_rating INTEGER DEFAULT 0,
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

-- Starbases (orbital facilities; may be built above a surface port)
CREATE TABLE IF NOT EXISTS starbases (
    base_id INTEGER PRIMARY KEY,
    game_id TEXT NOT NULL,
    owner_prefect_id INTEGER,
    name TEXT NOT NULL,
    base_type TEXT DEFAULT 'Starbase',
    system_id INTEGER NOT NULL,
    grid_col TEXT NOT NULL,
    grid_row INTEGER NOT NULL,
    orbiting_body_id INTEGER,
    surface_port_id INTEGER,
    complexes INTEGER DEFAULT 0,
    workers INTEGER DEFAULT 0,
    troops INTEGER DEFAULT 0,
    has_market INTEGER DEFAULT 0,
    docking_capacity INTEGER DEFAULT 0,
    employees INTEGER DEFAULT 0,
    employee_capacity INTEGER DEFAULT 0,
    sensor_profile REAL DEFAULT 1.0,
    sensor_rating INTEGER DEFAULT 0,
    integrity INTEGER DEFAULT 0,
    max_integrity INTEGER DEFAULT 0,
    shield_sp INTEGER DEFAULT 0,
    max_shield_sp INTEGER DEFAULT 0,
    armour INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (surface_port_id) REFERENCES surface_ports(port_id)
);

-- Outposts (lightweight surface installations, smaller than surface ports)
CREATE TABLE IF NOT EXISTS outposts (
    outpost_id INTEGER PRIMARY KEY,
    game_id TEXT NOT NULL,
    name TEXT NOT NULL,
    body_id INTEGER NOT NULL,
    surface_x INTEGER NOT NULL,
    surface_y INTEGER NOT NULL,
    owner_prefect_id INTEGER,
    outpost_type TEXT DEFAULT 'General',
    workers INTEGER DEFAULT 0,
    employees INTEGER DEFAULT 0,
    employee_capacity INTEGER DEFAULT 0,
    sensor_profile REAL DEFAULT 1.0,
    sensor_rating INTEGER DEFAULT 0,
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

-- Installed modules on bases/ports/outposts
CREATE TABLE IF NOT EXISTS installed_modules (
    install_id INTEGER PRIMARY KEY AUTOINCREMENT,
    starbase_id INTEGER,
    port_id INTEGER,
    outpost_id INTEGER,
    module_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1
);

-- Base/port/outpost inventory (stored goods and materials)
CREATE TABLE IF NOT EXISTS base_inventory (
    inventory_id INTEGER PRIMARY KEY AUTOINCREMENT,
    starbase_id INTEGER,
    port_id INTEGER,
    outpost_id INTEGER,
    item_type_id INTEGER NOT NULL,
    item_name TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 0,
    mass_per_unit INTEGER DEFAULT 1
);

-- Ship officers / crew
CREATE TABLE IF NOT EXISTS officers (
    officer_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ship_id INTEGER,
    base_id INTEGER,
    crew_number INTEGER DEFAULT 1,
    name TEXT NOT NULL,
    rank TEXT DEFAULT 'Ensign',
    specialty TEXT DEFAULT 'General',
    experience INTEGER DEFAULT 0,
    crew_factors INTEGER DEFAULT 5,
    crew_type_id INTEGER DEFAULT 401,
    wages INTEGER DEFAULT 5
);

-- Ship installed items
CREATE TABLE IF NOT EXISTS installed_items (
    item_install_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ship_id INTEGER,
    base_id INTEGER,
    component_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1
);

-- Cargo items
CREATE TABLE IF NOT EXISTS cargo_items (
    cargo_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ship_id INTEGER,
    base_id INTEGER,
    item_type_id INTEGER NOT NULL,
    item_name TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 0,
    mass_per_unit INTEGER DEFAULT 1
);

-- Base trade configuration (runtime trade roles per base)
CREATE TABLE IF NOT EXISTS base_trade_config (
    config_id INTEGER PRIMARY KEY AUTOINCREMENT,
    base_id INTEGER NOT NULL,
    game_id TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    trade_role TEXT NOT NULL,
    FOREIGN KEY (base_id) REFERENCES starbases(base_id)
);

-- Market prices (current cycle, depleting)
CREATE TABLE IF NOT EXISTS market_prices (
    price_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    base_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    turn_year INTEGER NOT NULL,
    turn_week INTEGER NOT NULL,
    buy_price INTEGER NOT NULL,
    sell_price INTEGER NOT NULL,
    stock INTEGER NOT NULL DEFAULT 0,
    demand INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

-- Known contacts per prefect
CREATE TABLE IF NOT EXISTS known_contacts (
    contact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    prefect_id INTEGER NOT NULL,
    object_type TEXT NOT NULL,
    object_id INTEGER NOT NULL,
    object_name TEXT,
    location_system INTEGER,
    location_col TEXT,
    location_row INTEGER,
    discovered_turn_year INTEGER,
    discovered_turn_week INTEGER,
    scanner_ship_id INTEGER,
    target_faction_id INTEGER,
    target_hull_type TEXT,
    target_ship_size INTEGER,
    detection_range INTEGER,
    detected_on_tick INTEGER,
    detection_source TEXT DEFAULT 'passive',
    FOREIGN KEY (prefect_id) REFERENCES prefects(prefect_id)
);

-- ============================================================
-- COMBAT
-- ============================================================

-- Per-ship combat target/defend/avoid lists.
-- Entries can refer to specific ships, bases, or factions (entry_type
-- determines which). NULL list_type means the row is invalid.
CREATE TABLE IF NOT EXISTS ship_combat_lists (
    list_entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    ship_id INTEGER NOT NULL,
    list_type TEXT NOT NULL,            -- 'target' | 'defend' | 'avoid'
    entry_type TEXT NOT NULL,           -- 'ship' | 'base' | 'faction'
    entry_id INTEGER NOT NULL,
    added_turn_year INTEGER,
    added_turn_week INTEGER,
    UNIQUE(game_id, ship_id, list_type, entry_type, entry_id),
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (ship_id) REFERENCES ships(ship_id)
);

-- Per-base combat lists (target + defend only — bases cannot avoid).
CREATE TABLE IF NOT EXISTS base_combat_lists (
    list_entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    base_kind TEXT NOT NULL,            -- 'starbase' | 'port' | 'outpost'
    base_id INTEGER NOT NULL,
    list_type TEXT NOT NULL,            -- 'target' | 'defend'
    entry_type TEXT NOT NULL,           -- 'ship' | 'base' | 'faction'
    entry_id INTEGER NOT NULL,
    added_turn_year INTEGER,
    added_turn_week INTEGER,
    UNIQUE(game_id, base_kind, base_id, list_type, entry_type, entry_id),
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

-- A combat engagement is one ongoing battle. It persists across turns
-- until an end condition is met. status: 'active' | 'resolved' | 'fled'
CREATE TABLE IF NOT EXISTS combat_engagements (
    engagement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    started_turn_year INTEGER NOT NULL,
    started_turn_week INTEGER NOT NULL,
    started_on_round INTEGER NOT NULL,
    last_active_turn_year INTEGER,
    last_active_turn_week INTEGER,
    system_id INTEGER NOT NULL,
    grid_col TEXT,
    grid_row INTEGER,
    status TEXT DEFAULT 'active',
    resolution TEXT,
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

-- Each ship/base in an engagement. participant_kind = 'ship' | 'starbase' | 'port' | 'outpost'.
CREATE TABLE IF NOT EXISTS combat_participants (
    participant_id INTEGER PRIMARY KEY AUTOINCREMENT,
    engagement_id INTEGER NOT NULL,
    participant_kind TEXT NOT NULL,
    participant_id_value INTEGER NOT NULL,
    owner_prefect_id INTEGER,
    joined_turn_year INTEGER,
    joined_turn_week INTEGER,
    joined_on_round INTEGER,
    left_turn_year INTEGER,
    left_turn_week INTEGER,
    left_on_round INTEGER,
    integrity_at_join REAL,
    integrity_at_end REAL,
    status TEXT DEFAULT 'active',       -- 'active' | 'destroyed' | 'fled'
    UNIQUE(engagement_id, participant_kind, participant_id_value),
    FOREIGN KEY (engagement_id) REFERENCES combat_engagements(engagement_id)
);

-- Combat events log: per-engagement, per-round, what each participant did.
CREATE TABLE IF NOT EXISTS combat_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    engagement_id INTEGER NOT NULL,
    turn_year INTEGER NOT NULL,
    turn_week INTEGER NOT NULL,
    round_number INTEGER NOT NULL,
    actor_kind TEXT NOT NULL,           -- ship/starbase/port/outpost/system
    actor_id INTEGER,
    action TEXT NOT NULL,               -- 'fire' | 'move' | 'evade' | 'destroyed' | 'flee' | 'engage' | 'note'
    target_kind TEXT,
    target_id INTEGER,
    damage REAL,
    integrity_after REAL,
    detail TEXT,
    FOREIGN KEY (engagement_id) REFERENCES combat_engagements(engagement_id)
);

-- Indexes for combat tables
CREATE INDEX IF NOT EXISTS idx_combat_lists_ship ON ship_combat_lists(game_id, ship_id);
CREATE INDEX IF NOT EXISTS idx_combat_lists_base ON base_combat_lists(game_id, base_kind, base_id);
CREATE INDEX IF NOT EXISTS idx_engagement_active ON combat_engagements(game_id, status);
CREATE INDEX IF NOT EXISTS idx_participants_engagement ON combat_participants(engagement_id);
CREATE INDEX IF NOT EXISTS idx_combat_log_engagement ON combat_log(engagement_id, turn_year, turn_week, round_number);

-- Turn orders (stored for audit)
CREATE TABLE IF NOT EXISTS turn_orders (
    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    turn_year INTEGER NOT NULL,
    turn_week INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id INTEGER NOT NULL,
    order_sequence INTEGER NOT NULL,
    command TEXT NOT NULL,
    parameters TEXT,
    status TEXT DEFAULT 'pending',
    result_message TEXT,
    tu_cost INTEGER DEFAULT 0,
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id)
);

-- Pending orders (carried forward)
CREATE TABLE IF NOT EXISTS pending_orders (
    pending_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id INTEGER NOT NULL,
    order_sequence INTEGER NOT NULL,
    command TEXT NOT NULL,
    parameters TEXT,
    reason TEXT
);

-- Inter-position messages
CREATE TABLE IF NOT EXISTS messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    sender_type TEXT NOT NULL,
    sender_id INTEGER NOT NULL,
    sender_name TEXT NOT NULL,
    recipient_type TEXT NOT NULL,
    recipient_id INTEGER NOT NULL,
    message_text TEXT NOT NULL,
    sent_turn_year INTEGER NOT NULL,
    sent_turn_week INTEGER NOT NULL,
    delivered INTEGER DEFAULT 0,
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

-- Faction change requests (GM-moderated)
CREATE TABLE IF NOT EXISTS faction_requests (
    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    prefect_id INTEGER NOT NULL,
    current_faction_id INTEGER,
    target_faction_id INTEGER NOT NULL,
    reason TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    requested_turn_year INTEGER NOT NULL,
    requested_turn_week INTEGER NOT NULL,
    processed_turn_year INTEGER,
    processed_turn_week INTEGER,
    gm_note TEXT DEFAULT '',
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (prefect_id) REFERENCES prefects(prefect_id)
);

-- Moderator action requests (player -> GM free-text, GM responds)
CREATE TABLE IF NOT EXISTS moderator_actions (
    action_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    ship_id INTEGER NOT NULL,
    prefect_id INTEGER NOT NULL,
    request_text TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    gm_response TEXT DEFAULT '',
    requested_turn_year INTEGER NOT NULL,
    requested_turn_week INTEGER NOT NULL,
    resolved_turn_year INTEGER,
    resolved_turn_week INTEGER,
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (ship_id) REFERENCES ships(ship_id),
    FOREIGN KEY (prefect_id) REFERENCES prefects(prefect_id)
);

-- Turn audit log
CREATE TABLE IF NOT EXISTS turn_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    turn_year INTEGER NOT NULL,
    turn_week INTEGER NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id INTEGER NOT NULL,
    tu_before INTEGER,
    tu_after INTEGER,
    action TEXT NOT NULL,
    result TEXT,
    rng_seed TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_ships_system ON ships(system_id);
CREATE INDEX IF NOT EXISTS idx_ships_game ON ships(game_id);
CREATE INDEX IF NOT EXISTS idx_bases_system ON starbases(system_id);
CREATE INDEX IF NOT EXISTS idx_orders_turn ON turn_orders(game_id, turn_year, turn_week);
CREATE INDEX IF NOT EXISTS idx_contacts_prefect ON known_contacts(prefect_id);
"""


def init_state_db(db_path=None):
    """Create/initialise game_state.db with game state tables."""
    path = Path(db_path) if db_path else STATE_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(STATE_SCHEMA)
    conn.commit()
    return conn


# ======================================================================
# COMBINED INIT (for setup-game)
# ======================================================================

def init_db(state_db_path=None, universe_db_path=None):
    """Initialise both databases and return a combined connection."""
    state_path = Path(state_db_path) if state_db_path else STATE_DB_PATH

    # If universe path not specified, put it alongside the state DB
    if universe_db_path:
        uni_path = Path(universe_db_path)
    else:
        uni_path = state_path.parent / "universe.db"

    init_universe_db(uni_path)
    init_state_db(state_path)

    return get_connection(state_path, uni_path)


# ======================================================================
# TURN BACKUPS
# ======================================================================

def backup_state(turn_label=None, state_db_path=None):
    """
    Copy game_state.db to saves/ with a turn-stamped name.
    Call after each successful run-turn.
    Returns the backup path or None on failure.
    """
    state_path = Path(state_db_path) if state_db_path else STATE_DB_PATH
    if not state_path.exists():
        return None

    saves_dir = state_path.parent / "saves"
    saves_dir.mkdir(parents=True, exist_ok=True)

    if turn_label:
        backup_name = f"game_state_{turn_label}.db"
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"game_state_{timestamp}.db"

    backup_path = saves_dir / backup_name
    shutil.copy2(str(state_path), str(backup_path))
    return backup_path


# ======================================================================
# SHIP COMPONENT HELPERS
# ======================================================================

def get_component(conn, component_id):
    """Get component details from the catalogue."""
    result = conn.execute(
        "SELECT * FROM ship_components WHERE component_id = ?", (component_id,)
    ).fetchone()
    return dict(result) if result else None


def get_ship_components(conn, ship_id):
    """Get all installed components for a ship, joined with catalogue data."""
    return conn.execute("""
        SELECT ii.item_install_id, ii.component_id, ii.quantity,
               sc.name, sc.category, sc.st_cost,
               sc.cargo_capacity, sc.crew_capacity, sc.life_capacity,
               sc.thrust, sc.engine_efficiency, sc.sensor_rating,
               sc.jump_range, sc.jump_oc_cost, sc.hull_restriction,
               sc.shield_sp_capacity,
               sc.magazine_missile_capacity, sc.magazine_torpedo_capacity
        FROM installed_items ii
        JOIN ship_components sc ON ii.component_id = sc.component_id
        WHERE ii.ship_id = ?
        ORDER BY sc.category, sc.component_id
    """, (ship_id,)).fetchall()


def get_ship_st_capacity(conn, ship_id):
    """Get a ship's total ST capacity from ship_size."""
    ship = conn.execute(
        "SELECT ship_size FROM ships WHERE ship_id = ?", (ship_id,)
    ).fetchone()
    if not ship:
        return 0
    return (ship['ship_size'] or 50) * 50


def get_ship_st_used(conn, ship_id):
    """Get total ST used by installed components."""
    result = conn.execute("""
        SELECT COALESCE(SUM(ii.quantity * sc.st_cost), 0) as total
        FROM installed_items ii
        JOIN ship_components sc ON ii.component_id = sc.component_id
        WHERE ii.ship_id = ?
    """, (ship_id,)).fetchone()
    return result['total']


def recalculate_ship_stats(conn, ship_id):
    """
    Recalculate all ship stats from installed components.
    
    Updates: cargo_capacity, life_support_capacity, sensor_rating,
             gravity_rating, crew_required.
    """
    ship = conn.execute(
        "SELECT ship_size, hull_type FROM ships WHERE ship_id = ?", (ship_id,)
    ).fetchone()
    if not ship:
        return

    ship_size = ship['ship_size'] or 50
    components = get_ship_components(conn, ship_id)

    # Sum up component contributions
    total_cargo = 0
    total_crew_cap = 0
    total_life_cap = 0
    total_thrust = 0
    total_engine_eff = 0.0
    total_installed_st = 0
    engine_count = 0
    best_jump_range = 0
    best_jump_oc = 0

    # Sensor arrays aggregate with diminishing returns: best × sqrt(count)
    sensor_components = []  # list of (rating_per_unit, qty)
    total_shield_sp = 0
    total_missile_capacity = 0
    total_torpedo_capacity = 0
    for c in components:
        qty = c['quantity']
        total_cargo += c['cargo_capacity'] * qty
        total_crew_cap += c['crew_capacity'] * qty
        total_life_cap += c['life_capacity'] * qty
        total_thrust += c['thrust'] * qty
        total_installed_st += c['st_cost'] * qty
        if c['sensor_rating'] and c['sensor_rating'] > 0:
            sensor_components.append((c['sensor_rating'], qty))
        if c['category'] == 'engine':
            engine_count += qty
            total_engine_eff += c['engine_efficiency'] * qty
        if c['category'] == 'jump_drive':
            for _ in range(qty):
                if c['jump_range'] > best_jump_range:
                    best_jump_range = c['jump_range']
                    best_jump_oc = c['jump_oc_cost']
        # Shield generators contribute SP
        if (c['category'] == 'shield' and c['shield_sp_capacity']
                and c['shield_sp_capacity'] > 0):
            total_shield_sp += c['shield_sp_capacity'] * qty
        # Magazines contribute missile/torpedo capacity
        if c['category'] == 'magazine':
            if c['magazine_missile_capacity']:
                total_missile_capacity += c['magazine_missile_capacity'] * qty
            if c['magazine_torpedo_capacity']:
                total_torpedo_capacity += c['magazine_torpedo_capacity'] * qty

    # Compute total sensor rating with sqrt diminishing returns.
    # Formula: best_per_unit_rating × sqrt(total_unit_count)
    # This means 5 identical rating-10 sensors give 10*sqrt(5) ≈ 22, not 50.
    import math
    if sensor_components:
        best_per_unit = max(r for r, _q in sensor_components)
        total_count = sum(q for _r, q in sensor_components)
        total_sensor = int(round(best_per_unit * math.sqrt(total_count)))
    else:
        total_sensor = 0

    # Engine efficiency caps at 1 engine per 10 ship_size
    max_engines = max(1, ship_size // 10)
    if engine_count > max_engines:
        # Scale efficiency back to max_engines worth
        total_engine_eff = total_engine_eff * max_engines / engine_count

    # Gravity rating = thrust / effective_mass
    # effective_mass = ship_size (hull) + installed_st / 100 (component mass)
    # An empty hull is light; a packed hull is heavy.
    effective_mass = ship_size + (total_installed_st / 100.0)
    gravity_rating = total_thrust / effective_mass if effective_mass > 0 else 0

    # Crew required = 1 per 2 hull points (rounded up)
    crew_required = max(1, -(-ship_size // 2))  # ceiling division

    # Sensor profile = ship_size / 100 (detection signature strength)
    sensor_profile = ship_size / 100.0 if ship_size > 0 else 0.5

    # Max integrity = ship_size × hull type multiplier. Military hulls
    # are 1.5× tougher than Commercial. Armour modules (future) will
    # layer additional HP on top of this base.
    hull_type = (ship['hull_type'] or 'Commercial').strip()
    max_integrity = float(ship_size) * HULL_HP_MULTIPLIER.get(hull_type, 1.0)

    conn.execute("""
        UPDATE ships SET
            cargo_capacity = ?,
            life_support_capacity = ?,
            sensor_rating = ?,
            sensor_profile = ?,
            gravity_rating = ?,
            crew_required = ?,
            max_integrity = ?,
            integrity = MIN(integrity, ?),
            max_shield_sp = ?,
            shield_sp = MIN(shield_sp, ?),
            max_missiles = ?,
            missiles_loaded = MIN(missiles_loaded, ?),
            max_torpedoes = ?,
            torpedoes_loaded = MIN(torpedoes_loaded, ?)
        WHERE ship_id = ?
    """, (total_cargo, total_life_cap, total_sensor, round(sensor_profile, 2),
          round(gravity_rating, 2), crew_required,
          max_integrity, max_integrity,
          total_shield_sp, total_shield_sp,
          total_missile_capacity, total_missile_capacity,
          total_torpedo_capacity, total_torpedo_capacity,
          ship_id))
    conn.commit()

    return {
        'cargo_capacity': total_cargo,
        'life_support_capacity': total_life_cap,
        'crew_capacity': total_crew_cap,
        'sensor_rating': total_sensor,
        'sensor_profile': round(sensor_profile, 2),
        'gravity_rating': round(gravity_rating, 2),
        'thrust': total_thrust,
        'engine_efficiency': round(total_engine_eff, 2),
        'engine_count': engine_count,
        'max_engines': max_engines,
        'jump_range': best_jump_range,
        'jump_oc_cost': best_jump_oc,
        'crew_required': crew_required,
        'max_shield_sp': total_shield_sp,
        'max_missiles': total_missile_capacity,
        'max_torpedoes': total_torpedo_capacity,
        'st_used': get_ship_st_used(conn, ship_id),
        'st_capacity': ship_size * 50,
    }


# ======================================================================
# BASE MODULE HELPERS
# ======================================================================

def get_base_module(conn, module_id):
    """Get module details from the catalogue."""
    result = conn.execute(
        "SELECT * FROM base_modules WHERE module_id = ?", (module_id,)
    ).fetchone()
    return dict(result) if result else None


def get_installed_modules(conn, starbase_id=None, port_id=None, outpost_id=None):
    """Get all installed modules for a base/port/outpost, joined with catalogue."""
    if starbase_id:
        where = "im.starbase_id = ?"
        param = starbase_id
    elif port_id:
        where = "im.port_id = ?"
        param = port_id
    elif outpost_id:
        where = "im.outpost_id = ?"
        param = outpost_id
    else:
        return []
    return conn.execute(f"""
        SELECT im.install_id, im.module_id, im.quantity,
               bm.name, bm.category, bm.employees_required,
               bm.location_restriction, bm.docking_slots, bm.mining_capacity,
               bm.factory_capacity, bm.repair_capacity, bm.market_income,
               bm.storage_capacity, bm.habitat_capacity, bm.defence_rating,
               bm.sensor_rating,
               bm.weapon_damage, bm.weapon_range, bm.weapon_shots_per_round,
               bm.weapon_accuracy, bm.shield_sp_capacity, bm.armour_value
        FROM installed_modules im
        JOIN base_modules bm ON im.module_id = bm.module_id
        WHERE {where}
        ORDER BY bm.category, bm.module_id
    """, (param,)).fetchall()


def check_module_location(module, location_type):
    """Check if a module can be installed at a given location type.
    location_type: 'starbase', 'surface_port', 'outpost'
    Returns (ok, error_msg)."""
    restriction = module['location_restriction']
    if restriction is None:
        return True, None
    if restriction == 'starbase' and location_type == 'starbase':
        return True, None
    if restriction == 'surface' and location_type in ('surface_port', 'outpost'):
        return True, None
    return False, f"{module['name']} requires {restriction} (this is a {location_type})."


# ==========================================================================
# KNOWLEDGE SYSTEM HELPERS (Phase 1)
# ==========================================================================

# Object types tracked by the knowledge system. Each has a corresponding
# lookup table for "is_public" and an FK-like object_id in prefect_knowledge.
_KNOWLEDGE_TABLES = {
    'star_system':    ('star_systems',    'system_id',  True),   # global (no game_id)
    'celestial_body': ('celestial_bodies', 'body_id',   True),   # global
    'starbase':       ('starbases',       'base_id',    False),  # per-game
    'surface_port':   ('surface_ports',   'port_id',    False),  # per-game
    'outpost':        ('outposts',        'outpost_id', False),  # per-game
    'trade_good':     ('trade_goods',     'item_id',    True),   # universe-wide
    # Ships/prefects/factions have no is_public row (ships private; prefects
    # semi-public via combat contacts; factions always public).
}


def is_object_public(conn, object_type, object_id):
    """Return True if an object is marked is_public=1 in its source table.
    For object types without an is_public column, return a sensible default:
    ships/prefects = False; factions = True.
    """
    if object_type in _KNOWLEDGE_TABLES:
        tbl, idcol, _ = _KNOWLEDGE_TABLES[object_type]
        # trade_goods is in universe DB; all others are main
        schema = "universe." if object_type == 'trade_good' else ""
        try:
            r = conn.execute(
                f"SELECT is_public FROM {schema}{tbl} WHERE {idcol} = ?",
                (object_id,)
            ).fetchone()
            return bool(r['is_public']) if r else False
        except Exception:
            return False
    if object_type == 'faction':
        return True
    return False


def prefect_knows(conn, prefect_id, game_id, object_type, object_id):
    """Check whether a prefect has knowledge (public OR personal OR — later —
    via their faction) of the given object. Returns True/False.

    Current implementation: public ∨ personal. Faction layer lands in Phase 3.
    """
    if prefect_id is None or object_id is None:
        return False
    # Public knowledge always accessible
    if is_object_public(conn, object_type, object_id):
        return True
    # Personal prefect_knowledge row
    r = conn.execute(
        """SELECT 1 FROM prefect_knowledge
           WHERE prefect_id = ? AND game_id = ?
                 AND object_type = ? AND object_id = ?""",
        (prefect_id, game_id, object_type, object_id)
    ).fetchone()
    return r is not None


def grant_knowledge(conn, prefect_id, game_id, object_type, object_id,
                      turn_year=None, turn_week=None):
    """Write a prefect_knowledge row if not already present. Idempotent.
    Does nothing if the object is already publicly known.

    Returns True if a new row was inserted, False if it already existed or
    the object was public.
    """
    if prefect_id is None or object_id is None:
        return False
    # No-op for public objects — they don't need personal knowledge rows
    if is_object_public(conn, object_type, object_id):
        return False
    cur = conn.execute(
        """INSERT OR IGNORE INTO prefect_knowledge
           (prefect_id, game_id, object_type, object_id,
            discovered_turn_year, discovered_turn_week)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (prefect_id, game_id, object_type, object_id, turn_year, turn_week)
    )
    return cur.rowcount > 0


def grant_system_knowledge(conn, prefect_id, game_id, system_id,
                             turn_year=None, turn_week=None):
    """Convenience: arriving in a system grants knowledge of the system and
    all celestial bodies in it (existence-only). Bases and surface ports
    within the system are NOT automatically revealed — those require
    scan orders to detect.
    """
    grant_knowledge(conn, prefect_id, game_id, 'star_system', system_id,
                     turn_year, turn_week)
    for r in conn.execute(
        "SELECT body_id FROM celestial_bodies WHERE system_id = ?",
        (system_id,)
    ).fetchall():
        grant_knowledge(conn, prefect_id, game_id, 'celestial_body',
                         r['body_id'], turn_year, turn_week)


def prefect_knowledge_set(conn, prefect_id, game_id, object_type):
    """Return a set of object_ids of the given type known to the prefect
    (either publicly OR personally). Useful for filtering query results."""
    ids = set()
    # Public
    if object_type in _KNOWLEDGE_TABLES:
        tbl, idcol, is_global = _KNOWLEDGE_TABLES[object_type]
        schema = "universe." if object_type == 'trade_good' else ""
        where = "is_public = 1"
        params = ()
        if not is_global:
            where += " AND game_id = ?"
            params = (game_id,)
        try:
            for r in conn.execute(
                f"SELECT {idcol} FROM {schema}{tbl} WHERE {where}", params
            ).fetchall():
                ids.add(r[idcol])
        except Exception:
            pass
    # Personal
    for r in conn.execute(
        """SELECT object_id FROM prefect_knowledge
           WHERE prefect_id = ? AND game_id = ? AND object_type = ?""",
        (prefect_id, game_id, object_type)
    ).fetchall():
        ids.add(r['object_id'])
    return ids


def recalculate_base_stats(conn, starbase_id=None, port_id=None, outpost_id=None):
    """
    Recalculate base stats from installed modules.
    Updates: docking_capacity, employee_capacity, employees_required (computed).
    Returns a stats dict.
    """
    modules = get_installed_modules(conn, starbase_id=starbase_id,
                                     port_id=port_id, outpost_id=outpost_id)

    total_modules = sum(m['quantity'] for m in modules)
    total_employees_required = sum(m['employees_required'] * m['quantity'] for m in modules)
    total_docking = sum(m['docking_slots'] * m['quantity'] for m in modules)
    total_mining = sum(m['mining_capacity'] * m['quantity'] for m in modules)
    total_factory = sum(m['factory_capacity'] * m['quantity'] for m in modules)
    total_repair = sum(m['repair_capacity'] * m['quantity'] for m in modules)
    total_market_income = sum(m['market_income'] * m['quantity'] for m in modules)
    total_storage = sum(m['storage_capacity'] * m['quantity'] for m in modules)
    total_habitat = sum(m['habitat_capacity'] * m['quantity'] for m in modules)
    total_defence = sum(m['defence_rating'] * m['quantity'] for m in modules)

    # Sensor rating: sqrt-diminishing returns on sensor modules
    # (parallels the ship sensor aggregation).
    sensor_modules_present = [(m['sensor_rating'], m['quantity'])
                               for m in modules
                               if m['sensor_rating'] and m['sensor_rating'] > 0]
    import math
    if sensor_modules_present:
        best_per_unit = max(r for r, _q in sensor_modules_present)
        total_count = sum(q for _r, q in sensor_modules_present)
        total_sensor_rating = int(round(best_per_unit * math.sqrt(total_count)))
    else:
        total_sensor_rating = 0

    # Command efficiency: 1 command module per 100 total modules
    command_count = sum(m['quantity'] for m in modules if m['category'] == 'command')
    command_required = math.ceil(total_modules / 100) if total_modules > 0 else 0
    command_pct = min(100, (command_count / command_required * 100)) if command_required > 0 else 100

    # Employee efficiency
    if starbase_id:
        base = conn.execute("SELECT employees FROM starbases WHERE base_id = ?", (starbase_id,)).fetchone()
        employees = base['employees'] if base else 0
    elif port_id:
        base = conn.execute("SELECT employees FROM surface_ports WHERE port_id = ?", (port_id,)).fetchone()
        employees = base['employees'] if base else 0
    elif outpost_id:
        base = conn.execute("SELECT employees FROM outposts WHERE outpost_id = ?", (outpost_id,)).fetchone()
        employees = base['employees'] if base else 0
    else:
        employees = 0

    employee_pct = min(100, (employees / total_employees_required * 100)) if total_employees_required > 0 else 100
    overall_efficiency = min(command_pct, employee_pct)

    # Sensor profile = max(1, total_modules) + inventory_mass/100
    # Bases throw a strong signature; more modules + cargo = more visible
    if starbase_id:
        inv_col = 'starbase_id'; inv_id = starbase_id
    elif port_id:
        inv_col = 'port_id'; inv_id = port_id
    else:
        inv_col = 'outpost_id'; inv_id = outpost_id
    inv_mass_row = conn.execute(
        f"SELECT COALESCE(SUM(quantity * mass_per_unit), 0) AS m FROM base_inventory WHERE {inv_col} = ?",
        (inv_id,)
    ).fetchone()
    inv_mass = inv_mass_row['m'] if inv_mass_row else 0
    sensor_profile = round(max(1, total_modules) + (inv_mass / 100.0), 2)

    # Update the base record
    if starbase_id:
        conn.execute("""
            UPDATE starbases SET docking_capacity = ?, employee_capacity = ?,
                   sensor_profile = ?, sensor_rating = ?
            WHERE base_id = ?
        """, (total_docking, total_habitat, sensor_profile, total_sensor_rating, starbase_id))

        # Combat stat recalculation for starbases (v1: starbases only, not
        # ports/outposts). Preserves current integrity/SP percentage where
        # possible when max values change due to module additions/removals.
        def _row_get(row, key, default=0):
            """Safe accessor for sqlite3.Row (which has no .get())."""
            try:
                return row[key] if key in row.keys() else default
            except (KeyError, IndexError):
                return default
        total_shield_sp = sum(
            (_row_get(m, 'shield_sp_capacity', 0) or 0) * m['quantity']
            for m in modules
        )
        total_armour = sum(
            (_row_get(m, 'armour_value', 0) or 0) * m['quantity']
            for m in modules
        )
        max_hp = BASE_HP_PER_MODULE * total_modules
        cur = conn.execute(
            """SELECT integrity, max_integrity, shield_sp, max_shield_sp
               FROM starbases WHERE base_id = ?""",
            (starbase_id,)
        ).fetchone()
        # Preserve integrity as a percentage of old max (clamped to new max).
        old_max_hp = cur['max_integrity'] or 0
        if old_max_hp > 0 and cur['integrity'] is not None:
            pct = (cur['integrity'] / old_max_hp) if old_max_hp > 0 else 1.0
            new_integ = min(max_hp, max_hp * pct)
        else:
            new_integ = max_hp
        # Preserve shield SP as a ratio to old max (clamped).
        old_max_sp = cur['max_shield_sp'] or 0
        if old_max_sp > 0 and cur['shield_sp'] is not None:
            sp_pct = (cur['shield_sp'] / old_max_sp) if old_max_sp > 0 else 1.0
            new_sp = min(total_shield_sp, int(total_shield_sp * sp_pct))
        else:
            new_sp = total_shield_sp
        conn.execute(
            """UPDATE starbases SET
                max_integrity = ?, integrity = ?,
                max_shield_sp = ?, shield_sp = ?,
                armour = ?
               WHERE base_id = ?""",
            (max_hp, new_integ, total_shield_sp, new_sp, total_armour, starbase_id)
        )
    elif port_id:
        conn.execute("""
            UPDATE surface_ports SET employee_capacity = ?,
                   sensor_profile = ?, sensor_rating = ?
            WHERE port_id = ?
        """, (total_habitat, sensor_profile, total_sensor_rating, port_id))
    elif outpost_id:
        conn.execute("""
            UPDATE outposts SET employee_capacity = ?,
                   sensor_profile = ?, sensor_rating = ?
            WHERE outpost_id = ?
        """, (total_habitat, sensor_profile, total_sensor_rating, outpost_id))
    conn.commit()

    return {
        'total_modules': total_modules,
        'employees': employees,
        'employees_required': total_employees_required,
        'employee_capacity': total_habitat,
        'employee_pct': round(employee_pct, 1),
        'command_count': command_count,
        'command_required': command_required,
        'command_pct': round(command_pct, 1),
        'overall_efficiency': round(overall_efficiency, 1),
        'sensor_profile': sensor_profile,
        'sensor_rating': total_sensor_rating,
        'docking_capacity': total_docking,
        'mining_capacity': total_mining,
        'factory_capacity': total_factory,
        'repair_capacity': total_repair,
        'market_income': total_market_income,
        'storage_capacity': total_storage,
        'defence_rating': total_defence,
    }


# ======================================================================
# FACTION HELPERS
# ======================================================================

def get_faction(conn, faction_id):
    """Get faction details by ID."""
    if faction_id is None:
        return {'faction_id': None, 'abbreviation': 'IND', 'name': 'Independent'}
    result = conn.execute(
        "SELECT * FROM factions WHERE faction_id = ?", (faction_id,)
    ).fetchone()
    if result:
        return dict(result)
    return {'faction_id': faction_id, 'abbreviation': '???', 'name': 'Unknown'}


def faction_display_name(conn, name, faction_id):
    """Return a name with faction prefix, e.g. 'STA Vengeance'."""
    faction = get_faction(conn, faction_id)
    return f"{faction['abbreviation']} {name}"


def get_faction_for_prefect(conn, prefect_id):
    """Look up the faction for a prefect."""
    result = conn.execute(
        "SELECT faction_id FROM prefects WHERE prefect_id = ?",
        (prefect_id,)
    ).fetchone()
    if result and result['faction_id']:
        return get_faction(conn, result['faction_id'])
    return {'faction_id': None, 'abbreviation': 'IND', 'name': 'Independent'}


# ======================================================================
# LEGACY DATABASE SPLIT (one-time migration from single DB)
# ======================================================================

def split_legacy_db(legacy_path):
    """
    Split an old stellar_dominion.db into universe.db + game_state.db.
    Safe to run multiple times — skips if targets exist.
    """
    legacy_path = Path(legacy_path)
    if not legacy_path.exists():
        print(f"Error: {legacy_path} not found.")
        return False

    target_dir = legacy_path.parent
    uni_path = target_dir / "universe.db"
    state_path = target_dir / "game_state.db"

    if uni_path.exists() and state_path.exists():
        print(f"Both {uni_path.name} and {state_path.name} already exist. Skipping split.")
        return True

    print(f"Splitting {legacy_path.name} into universe.db + game_state.db ...")

    # First: apply any pending legacy migrations
    _apply_legacy_migrations(legacy_path)

    legacy = sqlite3.connect(str(legacy_path))
    legacy.row_factory = sqlite3.Row

    # ---- Create universe.db ----
    uni_conn = init_universe_db(uni_path)

    # Disable FK constraints during bulk import (parent_body_id ordering)
    uni_conn.execute("PRAGMA foreign_keys = OFF")

    # Get current turn for created_turn stamps
    game = legacy.execute("SELECT * FROM games LIMIT 1").fetchone()
    created_turn = f"{game['current_year']}.{game['current_week']}" if game else None

    # Copy star_systems (drop game_id column, add created_turn)
    for row in legacy.execute("SELECT * FROM star_systems").fetchall():
        uni_conn.execute("""
            INSERT OR IGNORE INTO star_systems
            (system_id, name, star_name, star_spectral_type,
             star_grid_col, star_grid_row, created_turn)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (row['system_id'], row['name'], row['star_name'],
              row['star_spectral_type'], row['star_grid_col'],
              row['star_grid_row'], created_turn))

    # Copy celestial_bodies
    cb_cols = [r[1] for r in legacy.execute("PRAGMA table_info(celestial_bodies)").fetchall()]
    for row in legacy.execute("SELECT * FROM celestial_bodies").fetchall():
        # Determine surface_size: from column if present, else defaults by type
        if 'surface_size' in cb_cols:
            ssize = row['surface_size']
        else:
            bt = row['body_type']
            ssize = 50 if bt == 'gas_giant' else 15 if bt == 'moon' else 11 if bt == 'asteroid' else 31
        uni_conn.execute("""
            INSERT OR IGNORE INTO celestial_bodies
            (body_id, system_id, name, body_type, parent_body_id,
             grid_col, grid_row, gravity, temperature, atmosphere,
             tectonic_activity, hydrosphere, life, map_symbol, surface_size, created_turn)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (row['body_id'], row['system_id'], row['name'], row['body_type'],
              row['parent_body_id'], row['grid_col'], row['grid_row'],
              row['gravity'], row['temperature'], row['atmosphere'],
              row['tectonic_activity'] if 'tectonic_activity' in cb_cols else 0,
              row['hydrosphere'] if 'hydrosphere' in cb_cols else 0,
              row['life'] if 'life' in cb_cols else 'None',
              row['map_symbol'], ssize, created_turn))

    # Copy system_links
    if legacy.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='system_links'").fetchone():
        for row in legacy.execute("SELECT * FROM system_links").fetchall():
            a, b = min(row['system_a'], row['system_b']), max(row['system_a'], row['system_b'])
            uni_conn.execute("""
                INSERT OR IGNORE INTO system_links (system_a, system_b, known_by_default, created_turn)
                VALUES (?, ?, ?, ?)
            """, (a, b, row['known_by_default'], created_turn))

    # Copy factions
    if legacy.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='factions'").fetchone():
        for row in legacy.execute("SELECT * FROM factions").fetchall():
            uni_conn.execute("""
                INSERT OR REPLACE INTO factions (faction_id, abbreviation, name, description)
                VALUES (?, ?, ?, ?)
            """, (row['faction_id'], row['abbreviation'], row['name'], row['description']))

    # Copy trade_goods (drop game_id)
    if legacy.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trade_goods'").fetchone():
        for row in legacy.execute("SELECT * FROM trade_goods").fetchall():
            uni_conn.execute("""
                INSERT OR IGNORE INTO trade_goods (item_id, name, base_price, mass_per_unit)
                VALUES (?, ?, ?, ?)
            """, (row['item_id'], row['name'], row['base_price'], row['mass_per_unit']))

    # Copy planet_surface (terrain is universe data, not game state)
    if legacy.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='planet_surface'").fetchone():
        for row in legacy.execute("SELECT body_id, x, y, terrain_type FROM planet_surface").fetchall():
            uni_conn.execute(
                "INSERT OR REPLACE INTO planet_surface (body_id, x, y, terrain_type) VALUES (?, ?, ?, ?)",
                (row['body_id'], row['x'], row['y'], row['terrain_type'])
            )

    uni_conn.commit()
    uni_conn.close()
    print(f"  Created {uni_path.name}")

    # ---- Create game_state.db ----
    state_conn = init_state_db(state_path)

    # Disable FK constraints during bulk import
    state_conn.execute("PRAGMA foreign_keys = OFF")

    # Build port_id -> base_id mapping from legacy (for starbase.surface_port_id)
    port_to_base = {}
    if legacy.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='surface_ports'").fetchone():
        sp_cols = [r[1] for r in legacy.execute("PRAGMA table_info(surface_ports)").fetchall()]
        if 'parent_base_id' in sp_cols:
            for row in legacy.execute(
                "SELECT port_id, parent_base_id FROM surface_ports WHERE parent_base_id IS NOT NULL"
            ).fetchall():
                port_to_base[row['parent_base_id']] = row['port_id']

    state_tables = [
        'games', 'players', 'prefects', 'ships', 'surface_ports', 'starbases', 'outposts',
        'officers', 'installed_items', 'installed_modules', 'base_inventory', 'cargo_items',
        'base_trade_config', 'market_prices',
        'known_contacts', 'turn_orders', 'pending_orders', 'messages', 'faction_requests', 'moderator_actions', 'turn_log',
    ]

    for table in state_tables:
        if not legacy.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            continue

        legacy_cols = [r[1] for r in legacy.execute(f"PRAGMA table_info({table})").fetchall()]
        state_cols = [r[1] for r in state_conn.execute(f"PRAGMA table_info({table})").fetchall()]
        common_cols = [c for c in state_cols if c in legacy_cols]
        if not common_cols:
            continue

        col_list = ', '.join(common_cols)
        placeholders = ', '.join(['?'] * len(common_cols))

        for row in legacy.execute(f"SELECT {col_list} FROM {table}").fetchall():
            try:
                state_conn.execute(
                    f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})",
                    tuple(row)
                )
            except sqlite3.IntegrityError:
                pass

    # Set starbases.surface_port_id from legacy port->base mapping
    for base_id, port_id in port_to_base.items():
        state_conn.execute(
            "UPDATE starbases SET surface_port_id = ? WHERE base_id = ?",
            (port_id, base_id)
        )

    state_conn.commit()
    state_conn.close()
    legacy.close()

    print(f"  Created {state_path.name}")
    print(f"  Split complete. Original {legacy_path.name} preserved.")
    return True


def _apply_legacy_migrations(db_path):
    """Apply schema migrations to a legacy single-file database before splitting."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    has_games = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='games'"
    ).fetchone()
    if not has_games:
        conn.close()
        return

    game = conn.execute("SELECT * FROM games LIMIT 1").fetchone()
    if not game:
        conn.close()
        return

    game_columns = [row[1] for row in conn.execute("PRAGMA table_info(games)").fetchall()]
    if 'schema_version' not in game_columns:
        conn.execute("ALTER TABLE games ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE games SET schema_version = 0")
        conn.commit()

    version = conn.execute("SELECT schema_version FROM games LIMIT 1").fetchone()[0]

    if version < 1:
        # v0 -> v1: factions, player status
        columns = [row[1] for row in conn.execute("PRAGMA table_info(players)").fetchall()]
        if 'status' not in columns:
            conn.execute("ALTER TABLE players ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        conn.execute("""CREATE TABLE IF NOT EXISTS factions (
            faction_id INTEGER PRIMARY KEY, abbreviation TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL, description TEXT DEFAULT '')""")
        conn.execute("""INSERT OR IGNORE INTO factions VALUES
            (11, 'STA', 'Stellar Training Academy', 'Default starting faction for new players')""")
        pp_cols = [row[1] for row in conn.execute("PRAGMA table_info(prefects)").fetchall()]
        if 'faction_id' not in pp_cols:
            conn.execute("ALTER TABLE prefects ADD COLUMN faction_id INTEGER DEFAULT 11")
            conn.execute("UPDATE prefects SET faction_id = 11 WHERE faction_id IS NULL")
        conn.execute("UPDATE games SET schema_version = 1")
        conn.commit()
        print(f"  Legacy migration: v0 -> v1")
        version = 1

    if version < 2:
        # v1 -> v2: trade system + landing
        from engine.game_setup import generate_market_prices
        ship_cols = [row[1] for row in conn.execute("PRAGMA table_info(ships)").fetchall()]
        for col, typ in [('landed_body_id', 'INTEGER'), ('landed_x', 'INTEGER DEFAULT 1'),
                         ('landed_y', 'INTEGER DEFAULT 1'), ('gravity_rating', 'REAL DEFAULT 1.5')]:
            if col not in ship_cols:
                conn.execute(f"ALTER TABLE ships ADD COLUMN {col} {typ}")
        conn.commit()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trade_goods (
                item_id INTEGER PRIMARY KEY, game_id TEXT, name TEXT NOT NULL,
                base_price INTEGER NOT NULL, mass_per_unit INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS base_trade_config (
                config_id INTEGER PRIMARY KEY AUTOINCREMENT, base_id INTEGER NOT NULL,
                game_id TEXT NOT NULL, item_id INTEGER NOT NULL, trade_role TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS market_prices (
                price_id INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT NOT NULL,
                base_id INTEGER NOT NULL, item_id INTEGER NOT NULL,
                turn_year INTEGER NOT NULL, turn_week INTEGER NOT NULL,
                buy_price INTEGER NOT NULL, sell_price INTEGER NOT NULL,
                stock INTEGER NOT NULL DEFAULT 0, demand INTEGER NOT NULL DEFAULT 0);""")
        for g in conn.execute("SELECT * FROM games").fetchall():
            gid = g['game_id']
            if conn.execute("SELECT COUNT(*) FROM base_trade_config WHERE game_id = ?", (gid,)).fetchone()[0] > 0:
                continue
            for item in [(101, gid, 'Precious Metals', 20, 5), (102, gid, 'Advanced Computer Cores', 50, 2),
                         (103, gid, 'Food Supplies', 30, 3)]:
                conn.execute("INSERT OR IGNORE INTO trade_goods VALUES (?, ?, ?, ?, ?)", item)
            bases = conn.execute("SELECT base_id FROM starbases WHERE game_id = ? ORDER BY base_id", (gid,)).fetchall()
            roles = [[('produces', 101), ('average', 102), ('demands', 103)],
                     [('demands', 101), ('produces', 102), ('average', 103)],
                     [('average', 101), ('demands', 102), ('produces', 103)]]
            for i, base in enumerate(bases):
                for role, iid in roles[i % 3]:
                    conn.execute("INSERT INTO base_trade_config (base_id, game_id, item_id, trade_role) VALUES (?,?,?,?)",
                                 (base['base_id'], gid, iid, role))
            conn.commit()
            generate_market_prices(conn, gid, g['current_year'], g['current_week'])
        conn.execute("UPDATE games SET schema_version = 2")
        conn.commit()
        print(f"  Legacy migration: v1 -> v2")
        version = 2

    if version < 3:
        # v2 -> v3: planet surfaces
        ship_cols = [row[1] for row in conn.execute("PRAGMA table_info(ships)").fetchall()]
        for col, typ in [('landed_x', 'INTEGER DEFAULT 1'), ('landed_y', 'INTEGER DEFAULT 1')]:
            if col not in ship_cols:
                conn.execute(f"ALTER TABLE ships ADD COLUMN {col} {typ}")
        cb_cols = [row[1] for row in conn.execute("PRAGMA table_info(celestial_bodies)").fetchall()]
        for col, typ in [('tectonic_activity', 'INTEGER DEFAULT 0'),
                         ('hydrosphere', 'INTEGER DEFAULT 0'), ('life', "TEXT DEFAULT 'None'")]:
            if col not in cb_cols:
                conn.execute(f"ALTER TABLE celestial_bodies ADD COLUMN {col} {typ}")
        conn.execute("""CREATE TABLE IF NOT EXISTS planet_surface (
            body_id INTEGER NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL,
            terrain_type TEXT NOT NULL, PRIMARY KEY (body_id, x, y))""")
        for body in conn.execute("SELECT * FROM celestial_bodies").fetchall():
            if body['body_type'] == 'gas_giant':
                continue
            temp, atmo = body['temperature'] or 300, (body['atmosphere'] or 'None').lower()
            if atmo == 'standard' and 230 <= temp <= 310:     t, h, l = 4, 60, 'Sentient'
            elif atmo == 'dense' and temp > 310:              t, h, l = 7, 15, 'Microbial'
            elif atmo == 'thin' and temp < 230:
                t = 1 if body['body_type'] == 'moon' else 2
                h = 40 if temp < 150 else 10
                l = 'None' if body['body_type'] == 'moon' else 'Plant'
            elif atmo == 'thin':                              t, h, l = 2, 10, 'Plant'
            else:                                             t, h, l = 1, 0, 'None'
            conn.execute("UPDATE celestial_bodies SET tectonic_activity=?, hydrosphere=?, life=? WHERE body_id=? AND tectonic_activity=0 AND hydrosphere=0",
                         (t, h, l, body['body_id']))
        conn.execute("UPDATE games SET schema_version = 3")
        conn.commit()
        print(f"  Legacy migration: v2 -> v3")

    # v3 -> v4: surface_size on celestial_bodies
    cb_cols = [row[1] for row in conn.execute("PRAGMA table_info(celestial_bodies)").fetchall()]
    if 'surface_size' not in cb_cols:
        conn.execute("ALTER TABLE celestial_bodies ADD COLUMN surface_size INTEGER NOT NULL DEFAULT 31")
        # Set sensible defaults by body type
        conn.execute("UPDATE celestial_bodies SET surface_size = 50 WHERE body_type = 'gas_giant'")
        conn.execute("UPDATE celestial_bodies SET surface_size = 15 WHERE body_type = 'moon'")
        conn.execute("UPDATE celestial_bodies SET surface_size = 11 WHERE body_type = 'asteroid'")
        # Planets stay at 31 (the default)
        conn.commit()
        print(f"  Legacy migration: v3 -> v4 (surface_size)")

    conn.close()


# Legacy alias for old code that called migrate_db()
def migrate_db(db_path=None):
    """Legacy entry point — applies migrations then splits."""
    path = Path(db_path) if db_path else GAME_DATA_DIR / "stellar_dominion.db"
    if path.exists():
        split_legacy_db(path)
