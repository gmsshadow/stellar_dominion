"""
Stellar Dominion - Game Setup
Creates initial game state with star systems, celestial bodies, bases, and players.
"""

import random
from db.database import init_db, get_connection


def create_game(db_path=None, game_id="HANF231", game_name="Stellar Dominion - Hanf Campaign"):
    """Create a new game with the Hanf system."""
    conn = init_db(db_path)
    c = conn.cursor()

    # Check if game already exists
    existing = c.execute("SELECT game_id FROM games WHERE game_id = ?", (game_id,)).fetchone()
    if existing:
        print(f"Game {game_id} already exists. Use --force to recreate.")
        conn.close()
        return False

    # Create game
    c.execute("""
        INSERT INTO games (game_id, game_name, current_year, current_week, rng_seed)
        VALUES (?, ?, 500, 1, ?)
    """, (game_id, game_name, f"012-{game_id}-{random.randint(100000, 999999)}"))

    # =============================================
    # STAR SYSTEM: Hanf (231)
    # =============================================
    c.execute("""
        INSERT INTO star_systems (system_id, game_id, name, star_name, star_spectral_type)
        VALUES (231, ?, 'Hanf', 'Hanf Prime', 'G2V')
    """, (game_id,))

    # =============================================
    # CELESTIAL BODIES in Hanf System
    # =============================================

    # Planet: Orion at H04
    c.execute("""
        INSERT INTO celestial_bodies 
        (body_id, system_id, name, body_type, grid_col, grid_row, gravity, temperature, atmosphere, map_symbol)
        VALUES (247985, 231, 'Orion', 'planet', 'H', 4, 0.9, 295, 'Standard', 'O')
    """)

    # Planet: Tartarus at R08
    c.execute("""
        INSERT INTO celestial_bodies 
        (body_id, system_id, name, body_type, grid_col, grid_row, gravity, temperature, atmosphere, map_symbol)
        VALUES (301442, 231, 'Tartarus', 'planet', 'R', 8, 1.2, 340, 'Dense', 'O')
    """)

    # Gas Giant: Leviathan at E18
    c.execute("""
        INSERT INTO celestial_bodies 
        (body_id, system_id, name, body_type, grid_col, grid_row, gravity, temperature, atmosphere, map_symbol)
        VALUES (155230, 231, 'Leviathan', 'gas_giant', 'E', 18, 2.5, 120, 'Hydrogen', 'G')
    """)

    # Moon: Callyx at F19 (moon of Leviathan)
    c.execute("""
        INSERT INTO celestial_bodies 
        (body_id, system_id, name, body_type, parent_body_id, grid_col, grid_row, gravity, temperature, atmosphere, map_symbol)
        VALUES (88341, 231, 'Callyx', 'moon', 155230, 'F', 19, 0.3, 95, 'Thin', 'o')
    """)

    # Planet: Meridian at T20
    c.execute("""
        INSERT INTO celestial_bodies 
        (body_id, system_id, name, body_type, grid_col, grid_row, gravity, temperature, atmosphere, map_symbol)
        VALUES (412003, 231, 'Meridian', 'planet', 'T', 20, 0.7, 210, 'Thin', 'O')
    """)

    # =============================================
    # STARBASES (3 dockable bases)
    # =============================================

    # Citadel Station - orbiting Orion at H04
    c.execute("""
        INSERT INTO starbases 
        (base_id, game_id, name, base_type, system_id, grid_col, grid_row, orbiting_body_id,
         complexes, workers, troops, has_market, docking_capacity)
        VALUES (45687590, ?, 'Citadel Station', 'Starbase', 231, 'H', 4, 247985,
                25, 500, 100, 1, 5)
    """, (game_id,))

    # Tartarus Depot - orbiting Tartarus at R08
    c.execute("""
        INSERT INTO starbases 
        (base_id, game_id, name, base_type, system_id, grid_col, grid_row, orbiting_body_id,
         complexes, workers, troops, has_market, docking_capacity)
        VALUES (12340001, ?, 'Tartarus Depot', 'Outpost', 231, 'R', 8, 301442,
                10, 200, 50, 1, 3)
    """, (game_id,))

    # Meridian Waystation - orbiting Meridian at T20
    c.execute("""
        INSERT INTO starbases 
        (base_id, game_id, name, base_type, system_id, grid_col, grid_row, orbiting_body_id,
         complexes, workers, troops, has_market, docking_capacity)
        VALUES (78901234, ?, 'Meridian Waystation', 'Outpost', 231, 'T', 20, 412003,
                8, 150, 30, 1, 3)
    """, (game_id,))

    conn.commit()
    conn.close()
    print(f"Game '{game_name}' ({game_id}) created successfully.")
    print(f"  System: Hanf (231)")
    print(f"  Planets: Orion (H04), Tartarus (R08), Meridian (T20)")
    print(f"  Gas Giant: Leviathan (E18) with Moon Callyx (F19)")
    print(f"  Bases: Citadel Station (H04), Tartarus Depot (R08), Meridian Waystation (T20)")
    return True


def add_player(db_path=None, game_id="HANF231", player_name="Player 1",
               email="player1@example.com", political_name="Commander Voss",
               ship_name="VFS Boethius", ship_start_col="I", ship_start_row=6):
    """Add a player with a political position and starting ship."""
    conn = get_connection(db_path)
    c = conn.cursor()

    # Verify game exists
    game = c.execute("SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()
    if not game:
        print(f"Error: Game {game_id} not found.")
        conn.close()
        return None

    # Create player
    c.execute("""
        INSERT INTO players (game_id, player_name, email)
        VALUES (?, ?, ?)
    """, (game_id, player_name, email))
    player_id = c.lastrowid

    # Generate unique IDs
    political_id = random.randint(10000, 99999999)
    ship_id = random.randint(10000, 99999999)

    # Create political position
    c.execute("""
        INSERT INTO political_positions 
        (position_id, player_id, game_id, name, credits, location_type, location_id,
         created_turn_year, created_turn_week)
        VALUES (?, ?, ?, ?, 10000, 'ship', ?, ?, ?)
    """, (political_id, player_id, game_id, political_name, ship_id,
          game['current_year'], game['current_week']))

    # Create ship
    c.execute("""
        INSERT INTO ships 
        (ship_id, game_id, owner_political_id, name, ship_class, design, hull_type,
         hull_count, grid_col, grid_row, system_id, tu_per_turn, tu_remaining,
         sensor_rating, cargo_capacity, crew_count, crew_required)
        VALUES (?, ?, ?, ?, 'Scout', 'Explorer Mk I', 'Light Hull', 50,
                ?, ?, 231, 300, 300, 20, 500, 15, 10)
    """, (ship_id, game_id, political_id, ship_name, ship_start_col, ship_start_row))

    # Add a starting officer
    c.execute("""
        INSERT INTO officers (ship_id, name, rank, specialty, experience, crew_factors)
        VALUES (?, ?, 'Captain', 'Navigation', 0, 8)
    """, (ship_id, political_name))

    # Add starting installed items
    starting_items = [
        (ship_id, 100, 'Bridge', 1, 50),
        (ship_id, 103, 'Sensor', 1, 10),
        (ship_id, 155, 'ISR Type 2 Engines', 3, 10),
        (ship_id, 174, 'Jump Drive - Basic', 1, 50),
        (ship_id, 160, 'Thrust Engine', 2, 20),
        (ship_id, 131, 'Quarters', 2, 25),
        (ship_id, 180, 'Cargo Hold', 5, 100),
    ]
    for sid, item_type, item_name, qty, mass in starting_items:
        c.execute("""
            INSERT INTO installed_items (ship_id, item_type_id, item_name, quantity, mass_per_unit)
            VALUES (?, ?, ?, ?, ?)
        """, (sid, item_type, item_name, qty, mass))

    conn.commit()
    conn.close()

    print(f"Player '{player_name}' added to game {game_id}:")
    print(f"  Political: {political_name} (ID: {political_id})")
    print(f"  Ship: {ship_name} (ID: {ship_id}) at {ship_start_col}{ship_start_row:02d}")
    print(f"  Starting Credits: 10,000")
    return {
        'player_id': player_id,
        'political_id': political_id,
        'ship_id': ship_id
    }


def setup_demo_game(db_path=None):
    """Create a complete demo game with 2 players."""
    if create_game(db_path):
        p1 = add_player(db_path, player_name="Alice", email="alice@example.com",
                         political_name="Admiral Chen", ship_name="VFS Boethius",
                         ship_start_col="I", ship_start_row=6)
        p2 = add_player(db_path, player_name="Bob", email="bob@example.com",
                         political_name="Commander Voss", ship_name="HMS Resolute",
                         ship_start_col="P", ship_start_row=15)
        return p1, p2
    return None
