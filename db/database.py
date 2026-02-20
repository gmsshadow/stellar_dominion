"""
Stellar Dominion - Database Layer
SQLite persistent universe database.
"""

import sqlite3
import os
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "game_data" / "stellar_dominion.db"


def get_faction(conn, faction_id):
    """Get faction details by ID. Returns dict with faction_id, abbreviation, name."""
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


def get_connection(db_path=None):
    """Get a database connection."""
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path=None):
    """Initialize the database schema."""
    conn = get_connection(db_path)
    cursor = conn.cursor()

    cursor.executescript("""
    -- Game metadata
    CREATE TABLE IF NOT EXISTS games (
        game_id TEXT PRIMARY KEY,
        game_name TEXT NOT NULL,
        current_year INTEGER NOT NULL DEFAULT 500,
        current_week INTEGER NOT NULL DEFAULT 1,
        schema_version INTEGER NOT NULL DEFAULT 3,
        rng_seed TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- Star systems (3-digit unique ID)
    CREATE TABLE IF NOT EXISTS star_systems (
        system_id INTEGER PRIMARY KEY,
        game_id TEXT NOT NULL,
        name TEXT NOT NULL,
        star_name TEXT NOT NULL DEFAULT 'Central Star',
        star_spectral_type TEXT DEFAULT 'G2V',
        star_grid_col TEXT NOT NULL DEFAULT 'M',
        star_grid_row INTEGER NOT NULL DEFAULT 13,
        FOREIGN KEY (game_id) REFERENCES games(game_id)
    );

    -- System links (for future interstellar travel)
    CREATE TABLE IF NOT EXISTS system_links (
        link_id INTEGER PRIMARY KEY AUTOINCREMENT,
        system_a INTEGER NOT NULL,
        system_b INTEGER NOT NULL,
        known_by_default INTEGER DEFAULT 0,
        FOREIGN KEY (system_a) REFERENCES star_systems(system_id),
        FOREIGN KEY (system_b) REFERENCES star_systems(system_id)
    );

    -- Celestial bodies (up to 6-digit ID): planets, moons, gas giants, asteroids
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
        FOREIGN KEY (system_id) REFERENCES star_systems(system_id),
        FOREIGN KEY (parent_body_id) REFERENCES celestial_bodies(body_id)
    );

    -- Planet surface grid (31x31 terrain tiles)
    CREATE TABLE IF NOT EXISTS planet_surface (
        body_id INTEGER NOT NULL,
        x INTEGER NOT NULL,
        y INTEGER NOT NULL,
        terrain_type TEXT NOT NULL,
        PRIMARY KEY (body_id, x, y),
        FOREIGN KEY (body_id) REFERENCES celestial_bodies(body_id)
    );

    -- Factions (2-digit code, 3-letter abbreviation, long name)
    CREATE TABLE IF NOT EXISTS factions (
        faction_id INTEGER PRIMARY KEY,
        abbreviation TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        description TEXT DEFAULT ''
    );

    -- Seed default faction: Stellar Training Academy
    INSERT OR IGNORE INTO factions (faction_id, abbreviation, name, description)
    VALUES (11, 'STA', 'Stellar Training Academy', 'Default starting faction for new players');

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

    -- Prefect positions (one per player, up to 8-digit ID)
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
        FOREIGN KEY (game_id) REFERENCES games(game_id),
        FOREIGN KEY (faction_id) REFERENCES factions(faction_id)
    );

    -- Ships (up to 8-digit unique ID)
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
        FOREIGN KEY (owner_prefect_id) REFERENCES prefects(prefect_id),
        FOREIGN KEY (system_id) REFERENCES star_systems(system_id)
    );

    -- Starbases (up to 8-digit unique ID)
    CREATE TABLE IF NOT EXISTS starbases (
        base_id INTEGER PRIMARY KEY,
        game_id TEXT NOT NULL,
        owner_prefect_id INTEGER,
        name TEXT NOT NULL,
        base_type TEXT DEFAULT 'Outpost',
        system_id INTEGER NOT NULL,
        grid_col TEXT NOT NULL,
        grid_row INTEGER NOT NULL,
        orbiting_body_id INTEGER,
        complexes INTEGER DEFAULT 0,
        workers INTEGER DEFAULT 0,
        troops INTEGER DEFAULT 0,
        has_market INTEGER DEFAULT 0,
        docking_capacity INTEGER DEFAULT 10,
        FOREIGN KEY (game_id) REFERENCES games(game_id),
        FOREIGN KEY (system_id) REFERENCES star_systems(system_id)
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

    -- Trade goods (game-wide item definitions)
    CREATE TABLE IF NOT EXISTS trade_goods (
        item_id INTEGER PRIMARY KEY,
        game_id TEXT NOT NULL,
        name TEXT NOT NULL,
        base_price INTEGER NOT NULL,
        mass_per_unit INTEGER NOT NULL,
        FOREIGN KEY (game_id) REFERENCES games(game_id)
    );

    -- Base trade configuration (what each base produces/demands)
    CREATE TABLE IF NOT EXISTS base_trade_config (
        config_id INTEGER PRIMARY KEY AUTOINCREMENT,
        base_id INTEGER NOT NULL,
        game_id TEXT NOT NULL,
        item_id INTEGER NOT NULL,
        trade_role TEXT NOT NULL,
        FOREIGN KEY (base_id) REFERENCES starbases(base_id),
        FOREIGN KEY (item_id) REFERENCES trade_goods(item_id)
    );

    -- Weekly market prices per base per item
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
        FOREIGN KEY (prefect_id) REFERENCES prefects(prefect_id)
    );

    -- Turn orders (stored)
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
    """)

    conn.commit()
    return conn


CURRENT_SCHEMA_VERSION = 3


def migrate_db(db_path=None):
    """
    Apply any pending schema migrations to an existing database.
    
    Reads schema_version from the games table and applies migrations
    in sequence until the database is at CURRENT_SCHEMA_VERSION.
    """
    conn = get_connection(db_path)

    # Check if any game exists yet (skip migration if DB is brand new / empty)
    game = conn.execute("SELECT * FROM games LIMIT 1").fetchone() if \
        conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='games'").fetchone() \
        else None

    if not game:
        conn.close()
        return

    # Add schema_version column if missing (pre-versioning databases)
    game_columns = [row[1] for row in conn.execute("PRAGMA table_info(games)").fetchall()]
    if 'schema_version' not in game_columns:
        conn.execute("ALTER TABLE games ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE games SET schema_version = 0")
        conn.commit()

    version = conn.execute("SELECT schema_version FROM games LIMIT 1").fetchone()[0]

    if version < 1:
        _migrate_v0_to_v1(conn)
        conn.execute("UPDATE games SET schema_version = 1")
        conn.commit()
        print(f"  Migration: v0 -> v1 (added player status, factions, prefect faction_id)")
        version = 1

    if version < 2:
        _migrate_v1_to_v2(conn)
        conn.execute("UPDATE games SET schema_version = 2")
        conn.commit()
        print(f"  Migration: v1 -> v2 (added trade system: trade_goods, base_trade_config, market_prices)")
        version = 2

    if version < 3:
        _migrate_v2_to_v3(conn)
        conn.execute("UPDATE games SET schema_version = 3")
        conn.commit()
        print(f"  Migration: v2 -> v3 (added planet surfaces: tectonic/hydro/life, planet_surface table)")
        version = 3

    # Future migrations slot in here:
    # if version < 3:
    #     _migrate_v2_to_v3(conn)
    #     conn.execute("UPDATE games SET schema_version = 3")
    #     conn.commit()
    #     print(f"  Migration: v2 -> v3 (description)")
    #     version = 3

    conn.close()


def _migrate_v0_to_v1(conn):
    """Migration from pre-versioned schema to v1."""
    # Add status column to players
    columns = [row[1] for row in conn.execute("PRAGMA table_info(players)").fetchall()]
    if 'status' not in columns:
        conn.execute("ALTER TABLE players ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")

    # Create factions table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS factions (
            faction_id INTEGER PRIMARY KEY,
            abbreviation TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            description TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        INSERT OR IGNORE INTO factions (faction_id, abbreviation, name, description)
        VALUES (11, 'STA', 'Stellar Training Academy', 'Default starting faction for new players')
    """)

    # Add faction_id to prefects
    pp_columns = [row[1] for row in conn.execute("PRAGMA table_info(prefects)").fetchall()]
    if 'faction_id' not in pp_columns:
        conn.execute("ALTER TABLE prefects ADD COLUMN faction_id INTEGER DEFAULT 11")
        conn.execute("UPDATE prefects SET faction_id = 11 WHERE faction_id IS NULL")

    conn.commit()


def _migrate_v1_to_v2(conn):
    """Migration from v1 to v2: add trade system + landing support."""
    from engine.game_setup import generate_market_prices, get_market_cycle_start

    # Add landing and gravity columns to ships if missing
    ship_cols = [row[1] for row in conn.execute("PRAGMA table_info(ships)").fetchall()]
    if 'landed_body_id' not in ship_cols:
        conn.execute("ALTER TABLE ships ADD COLUMN landed_body_id INTEGER")
    if 'landed_x' not in ship_cols:
        conn.execute("ALTER TABLE ships ADD COLUMN landed_x INTEGER DEFAULT 1")
    if 'landed_y' not in ship_cols:
        conn.execute("ALTER TABLE ships ADD COLUMN landed_y INTEGER DEFAULT 1")
    if 'gravity_rating' not in ship_cols:
        conn.execute("ALTER TABLE ships ADD COLUMN gravity_rating REAL DEFAULT 1.5")
    conn.commit()

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trade_goods (
            item_id INTEGER PRIMARY KEY,
            game_id TEXT NOT NULL,
            name TEXT NOT NULL,
            base_price INTEGER NOT NULL,
            mass_per_unit INTEGER NOT NULL,
            FOREIGN KEY (game_id) REFERENCES games(game_id)
        );

        CREATE TABLE IF NOT EXISTS base_trade_config (
            config_id INTEGER PRIMARY KEY AUTOINCREMENT,
            base_id INTEGER NOT NULL,
            game_id TEXT NOT NULL,
            item_id INTEGER NOT NULL,
            trade_role TEXT NOT NULL,
            FOREIGN KEY (base_id) REFERENCES starbases(base_id),
            FOREIGN KEY (item_id) REFERENCES trade_goods(item_id)
        );

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
    """)

    # Get all games and populate trade data for each
    games = conn.execute("SELECT * FROM games").fetchall()
    for game in games:
        game_id = game['game_id']

        # Skip if trade data already exists (e.g. setup-game already created it)
        existing = conn.execute(
            "SELECT COUNT(*) FROM base_trade_config WHERE game_id = ?", (game_id,)
        ).fetchone()[0]
        if existing > 0:
            continue

        # Trade goods
        for item in [
            (101, game_id, 'Precious Metals', 20, 5),
            (102, game_id, 'Advanced Computer Cores', 50, 2),
            (103, game_id, 'Food Supplies', 30, 3),
        ]:
            conn.execute("""
                INSERT OR IGNORE INTO trade_goods (item_id, game_id, name, base_price, mass_per_unit)
                VALUES (?, ?, ?, ?, ?)
            """, item)

        # Base trade config (standard layout for existing bases)
        bases = conn.execute(
            "SELECT base_id FROM starbases WHERE game_id = ? ORDER BY base_id",
            (game_id,)
        ).fetchall()

        # Rotate roles across bases: each base produces one, demands one, averages one
        role_rotations = [
            [('produces', 101), ('average', 102), ('demands', 103)],
            [('demands', 101), ('produces', 102), ('average', 103)],
            [('average', 101), ('demands', 102), ('produces', 103)],
        ]
        for i, base in enumerate(bases):
            roles = role_rotations[i % 3]
            for role, item_id in roles:
                conn.execute("""
                    INSERT INTO base_trade_config (base_id, game_id, item_id, trade_role)
                    VALUES (?, ?, ?, ?)
                """, (base['base_id'], game_id, item_id, role))

        conn.commit()

        # Generate market prices for current cycle
        generate_market_prices(conn, game_id,
                               game['current_year'],
                               game['current_week'])


def _migrate_v2_to_v3(conn):
    """Migration from v2 to v3: add planet surface system."""
    # Add landed_x/landed_y to ships if missing (in case v1->v2 was run before these existed)
    ship_cols = [row[1] for row in conn.execute("PRAGMA table_info(ships)").fetchall()]
    if 'landed_x' not in ship_cols:
        conn.execute("ALTER TABLE ships ADD COLUMN landed_x INTEGER DEFAULT 1")
    if 'landed_y' not in ship_cols:
        conn.execute("ALTER TABLE ships ADD COLUMN landed_y INTEGER DEFAULT 1")

    # Add new columns to celestial_bodies if missing
    cb_cols = [row[1] for row in conn.execute("PRAGMA table_info(celestial_bodies)").fetchall()]
    if 'tectonic_activity' not in cb_cols:
        conn.execute("ALTER TABLE celestial_bodies ADD COLUMN tectonic_activity INTEGER DEFAULT 0")
    if 'hydrosphere' not in cb_cols:
        conn.execute("ALTER TABLE celestial_bodies ADD COLUMN hydrosphere INTEGER DEFAULT 0")
    if 'life' not in cb_cols:
        conn.execute("ALTER TABLE celestial_bodies ADD COLUMN life TEXT DEFAULT 'None'")

    # Create planet_surface table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS planet_surface (
            body_id INTEGER NOT NULL,
            x INTEGER NOT NULL,
            y INTEGER NOT NULL,
            terrain_type TEXT NOT NULL,
            PRIMARY KEY (body_id, x, y),
            FOREIGN KEY (body_id) REFERENCES celestial_bodies(body_id)
        )
    """)

    # Set reasonable defaults for existing bodies based on their properties
    bodies = conn.execute("""
        SELECT body_id, name, body_type, temperature, atmosphere, gravity
        FROM celestial_bodies
    """).fetchall()

    for body in bodies:
        temp = body['temperature'] or 300
        atmo = (body['atmosphere'] or 'None').lower()
        btype = body['body_type']

        # Skip gas giants
        if btype == 'gas_giant':
            continue

        # Estimate tectonic, hydrosphere, life from existing properties
        if atmo in ('standard',) and 230 <= temp <= 310:
            tectonic = 4
            hydro = 60
            life = 'Sentient'
        elif atmo in ('dense',) and temp > 310:
            tectonic = 7
            hydro = 15
            life = 'Microbial'
        elif atmo in ('thin',) and temp < 230:
            tectonic = 1 if btype == 'moon' else 2
            hydro = 40 if temp < 150 else 10
            life = 'None' if btype == 'moon' else 'Plant'
        elif atmo in ('thin',):
            tectonic = 2
            hydro = 10
            life = 'Plant'
        else:
            tectonic = 1
            hydro = 0
            life = 'None'

        conn.execute("""
            UPDATE celestial_bodies
            SET tectonic_activity = ?, hydrosphere = ?, life = ?
            WHERE body_id = ? AND tectonic_activity = 0 AND hydrosphere = 0
        """, (tectonic, hydro, life, body['body_id']))

    conn.commit()
