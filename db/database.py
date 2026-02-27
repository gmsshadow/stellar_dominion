"""
Stellar Dominion - Database Layer
Two-database model:
  universe.db   — World definition (GM-editable): systems, bodies, links, factions, trade goods
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
            conn.execute("ALTER TABLE trade_goods ADD COLUMN origin_system_id INTEGER DEFAULT NULL")
            conn.commit()
        # Migrate universe.db: add resource_id to celestial_bodies if missing
        cb_cols = [r[1] for r in conn.execute("PRAGMA universe.table_info(celestial_bodies)").fetchall()]
        if 'resource_id' not in cb_cols:
            conn.execute("ALTER TABLE celestial_bodies ADD COLUMN resource_id INTEGER DEFAULT NULL")
            conn.commit()

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
    star_name TEXT NOT NULL DEFAULT 'Central Star',
    star_spectral_type TEXT DEFAULT 'G2V',
    star_grid_col TEXT NOT NULL DEFAULT 'M',
    star_grid_row INTEGER NOT NULL DEFAULT 13,
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

-- Seed default faction
INSERT OR IGNORE INTO factions (faction_id, abbreviation, name, description)
VALUES (11, 'STA', 'Stellar Training Academy', 'Default starting faction for new players');

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
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

-- Prefect positions (one per player)
CREATE TABLE IF NOT EXISTS prefects (
    prefect_id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL UNIQUE,
    game_id TEXT NOT NULL,
    name TEXT NOT NULL,
    faction_id INTEGER DEFAULT 11,
    rank TEXT DEFAULT 'Citizen',
    credits REAL NOT NULL DEFAULT 10000,
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
    cargo_capacity INTEGER DEFAULT 500,
    cargo_used INTEGER DEFAULT 0,
    crew_count INTEGER DEFAULT 10,
    crew_required INTEGER DEFAULT 10,
    efficiency REAL DEFAULT 100.0,
    integrity REAL DEFAULT 100.0,
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (owner_prefect_id) REFERENCES prefects(prefect_id)
);

-- Starbases
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
    complexes INTEGER DEFAULT 0,
    workers INTEGER DEFAULT 0,
    troops INTEGER DEFAULT 0,
    has_market INTEGER DEFAULT 0,
    docking_capacity INTEGER DEFAULT 10,
    FOREIGN KEY (game_id) REFERENCES games(game_id)
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
    crew_factors INTEGER DEFAULT 5
);

-- Ship installed items
CREATE TABLE IF NOT EXISTS installed_items (
    item_install_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ship_id INTEGER,
    base_id INTEGER,
    item_type_id INTEGER NOT NULL,
    item_name TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    mass_per_unit INTEGER DEFAULT 10
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

-- Planet surface grid (31x31 terrain tiles, generated lazily)
CREATE TABLE IF NOT EXISTS planet_surface (
    body_id INTEGER NOT NULL,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    terrain_type TEXT NOT NULL,
    PRIMARY KEY (body_id, x, y)
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
    FOREIGN KEY (prefect_id) REFERENCES prefects(prefect_id)
);

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

    uni_conn.commit()
    uni_conn.close()
    print(f"  Created {uni_path.name}")

    # ---- Create game_state.db ----
    state_conn = init_state_db(state_path)

    # Disable FK constraints during bulk import
    state_conn.execute("PRAGMA foreign_keys = OFF")

    state_tables = [
        'games', 'players', 'prefects', 'ships', 'starbases',
        'officers', 'installed_items', 'cargo_items',
        'base_trade_config', 'market_prices', 'planet_surface',
        'known_contacts', 'turn_orders', 'pending_orders', 'turn_log',
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
