"""
Stellar Dominion - Database Layer
SQLite persistent universe database.
"""

import sqlite3
import os
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "game_data" / "stellar_dominion.db"


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
        map_symbol TEXT NOT NULL DEFAULT 'O',
        FOREIGN KEY (system_id) REFERENCES star_systems(system_id),
        FOREIGN KEY (parent_body_id) REFERENCES celestial_bodies(body_id)
    );

    -- Players
    CREATE TABLE IF NOT EXISTS players (
        player_id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id TEXT NOT NULL,
        player_name TEXT NOT NULL,
        email TEXT NOT NULL,
        account_number TEXT NOT NULL UNIQUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (game_id) REFERENCES games(game_id)
    );

    -- Political positions (one per player, up to 8-digit ID)
    CREATE TABLE IF NOT EXISTS political_positions (
        position_id INTEGER PRIMARY KEY,
        player_id INTEGER NOT NULL UNIQUE,
        game_id TEXT NOT NULL,
        name TEXT NOT NULL,
        affiliation TEXT DEFAULT 'Independent',
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

    -- Ships (up to 8-digit unique ID)
    CREATE TABLE IF NOT EXISTS ships (
        ship_id INTEGER PRIMARY KEY,
        game_id TEXT NOT NULL,
        owner_political_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        ship_class TEXT DEFAULT 'Scout',
        design TEXT DEFAULT 'Explorer',
        hull_type TEXT DEFAULT 'Normal Hull',
        hull_count INTEGER DEFAULT 50,
        hull_damage_pct REAL DEFAULT 0.0,
        grid_col TEXT NOT NULL,
        grid_row INTEGER NOT NULL,
        system_id INTEGER NOT NULL,
        docked_at_base_id INTEGER,
        orbiting_body_id INTEGER,
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
        FOREIGN KEY (owner_political_id) REFERENCES political_positions(position_id),
        FOREIGN KEY (system_id) REFERENCES star_systems(system_id)
    );

    -- Starbases (up to 8-digit unique ID)
    CREATE TABLE IF NOT EXISTS starbases (
        base_id INTEGER PRIMARY KEY,
        game_id TEXT NOT NULL,
        owner_political_id INTEGER,
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

    -- Ship officers
    CREATE TABLE IF NOT EXISTS officers (
        officer_id INTEGER PRIMARY KEY AUTOINCREMENT,
        ship_id INTEGER,
        base_id INTEGER,
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

    -- Known contacts per political position
    CREATE TABLE IF NOT EXISTS known_contacts (
        contact_id INTEGER PRIMARY KEY AUTOINCREMENT,
        political_id INTEGER NOT NULL,
        object_type TEXT NOT NULL,
        object_id INTEGER NOT NULL,
        object_name TEXT,
        location_system INTEGER,
        location_col TEXT,
        location_row INTEGER,
        discovered_turn_year INTEGER,
        discovered_turn_week INTEGER,
        FOREIGN KEY (political_id) REFERENCES political_positions(position_id)
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
