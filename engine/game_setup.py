"""
Stellar Dominion - Game Setup
Creates initial game state with star systems, celestial bodies, bases, and players.
"""

import random
from pathlib import Path
from db.database import init_db, get_connection


def _generate_unique_id(conn, table, column, min_val=10000000, max_val=99999999):
    """Generate a unique random integer ID for the given table/column."""
    while True:
        candidate = random.randint(min_val, max_val)
        exists = conn.execute(
            f"SELECT 1 FROM {table} WHERE {column} = ?", (candidate,)
        ).fetchone()
        if not exists:
            return candidate


def _generate_account_number(conn):
    """Generate a unique 8-digit account number (stored as text for leading zeros)."""
    while True:
        candidate = f"{random.randint(10000000, 99999999)}"
        exists = conn.execute(
            "SELECT 1 FROM players WHERE account_number = ?", (candidate,)
        ).fetchone()
        if not exists:
            return candidate


def create_game(db_path=None, game_id="OMICRON101", game_name="Stellar Dominion - Omicron Campaign"):
    """Create a new game with the Omicron system."""
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
    # STAR SYSTEM: Omicron (101)
    # =============================================
    c.execute("""
        INSERT INTO star_systems (system_id, game_id, name, star_name, star_spectral_type)
        VALUES (101, ?, 'Omicron', 'Omicron Prime', 'G2V')
    """, (game_id,))

    # =============================================
    # CELESTIAL BODIES in Omicron System
    # =============================================

    # Planet: Orion at H04
    c.execute("""
        INSERT INTO celestial_bodies 
        (body_id, system_id, name, body_type, grid_col, grid_row, gravity, temperature, atmosphere, map_symbol)
        VALUES (247985, 101, 'Orion', 'planet', 'H', 4, 0.9, 295, 'Standard', 'O')
    """)

    # Planet: Tartarus at R08
    c.execute("""
        INSERT INTO celestial_bodies 
        (body_id, system_id, name, body_type, grid_col, grid_row, gravity, temperature, atmosphere, map_symbol)
        VALUES (301442, 101, 'Tartarus', 'planet', 'R', 8, 1.2, 340, 'Dense', 'O')
    """)

    # Gas Giant: Leviathan at E18
    c.execute("""
        INSERT INTO celestial_bodies 
        (body_id, system_id, name, body_type, grid_col, grid_row, gravity, temperature, atmosphere, map_symbol)
        VALUES (155230, 101, 'Leviathan', 'gas_giant', 'E', 18, 2.5, 120, 'Hydrogen', 'G')
    """)

    # Moon: Callyx at F19 (moon of Leviathan)
    c.execute("""
        INSERT INTO celestial_bodies 
        (body_id, system_id, name, body_type, parent_body_id, grid_col, grid_row, gravity, temperature, atmosphere, map_symbol)
        VALUES (88341, 101, 'Callyx', 'moon', 155230, 'F', 19, 0.3, 95, 'Thin', 'o')
    """)

    # Planet: Meridian at T20
    c.execute("""
        INSERT INTO celestial_bodies 
        (body_id, system_id, name, body_type, grid_col, grid_row, gravity, temperature, atmosphere, map_symbol)
        VALUES (412003, 101, 'Meridian', 'planet', 'T', 20, 0.7, 210, 'Thin', 'O')
    """)

    # =============================================
    # STARBASES (3 dockable bases)
    # =============================================

    # Citadel Station - orbiting Orion at H04
    c.execute("""
        INSERT INTO starbases 
        (base_id, game_id, name, base_type, system_id, grid_col, grid_row, orbiting_body_id,
         complexes, workers, troops, has_market, docking_capacity)
        VALUES (45687590, ?, 'Citadel Station', 'Starbase', 101, 'H', 4, 247985,
                25, 500, 100, 1, 5)
    """, (game_id,))

    # Tartarus Depot - orbiting Tartarus at R08
    c.execute("""
        INSERT INTO starbases 
        (base_id, game_id, name, base_type, system_id, grid_col, grid_row, orbiting_body_id,
         complexes, workers, troops, has_market, docking_capacity)
        VALUES (12340001, ?, 'Tartarus Depot', 'Outpost', 101, 'R', 8, 301442,
                10, 200, 50, 1, 3)
    """, (game_id,))

    # Meridian Waystation - orbiting Meridian at T20
    c.execute("""
        INSERT INTO starbases 
        (base_id, game_id, name, base_type, system_id, grid_col, grid_row, orbiting_body_id,
         complexes, workers, troops, has_market, docking_capacity)
        VALUES (78901234, ?, 'Meridian Waystation', 'Outpost', 101, 'T', 20, 412003,
                8, 150, 30, 1, 3)
    """, (game_id,))

    conn.commit()
    conn.close()

    # Create turn folder skeleton
    db_dir = db_path.parent if db_path else Path(__file__).parent.parent / "game_data"
    turns_dir = db_dir / "turns"
    incoming = turns_dir / "incoming"
    processed = turns_dir / "processed"
    incoming.mkdir(parents=True, exist_ok=True)
    processed.mkdir(parents=True, exist_ok=True)

    print(f"Game '{game_name}' ({game_id}) created successfully.")
    print(f"  System: Omicron (101)")
    print(f"  Planets: Orion (H04), Tartarus (R08), Meridian (T20)")
    print(f"  Gas Giant: Leviathan (E18) with Moon Callyx (F19)")
    print(f"  Bases: Citadel Station (H04), Tartarus Depot (R08), Meridian Waystation (T20)")
    print(f"  Turn folders: {turns_dir}/")
    print(f"    incoming/   <- player orders arrive here (by email)")
    print(f"    processed/  <- resolved reports go here (by account number)")
    return True


def add_player(db_path=None, game_id="OMICRON101", player_name="Player 1",
               email="player1@example.com", political_name="Commander Voss",
               ship_name="VFS Boethius", ship_start_col="I", ship_start_row=6,
               dock_at_base=None):
    """
    Add a player with a political position and starting ship.
    
    If dock_at_base is provided (base_id), the ship starts docked there
    and uses the base's grid position. Otherwise uses ship_start_col/row.
    """
    conn = get_connection(db_path)
    c = conn.cursor()

    # Verify game exists
    game = c.execute("SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()
    if not game:
        print(f"Error: Game {game_id} not found.")
        conn.close()
        return None

    # Generate unique IDs
    account_number = _generate_account_number(conn)
    political_id = _generate_unique_id(conn, 'political_positions', 'position_id')
    ship_id = _generate_unique_id(conn, 'ships', 'ship_id')

    # If docking at a base, use the base's location
    docked_at = None
    if dock_at_base:
        base = c.execute(
            "SELECT * FROM starbases WHERE base_id = ? AND game_id = ?",
            (dock_at_base, game_id)
        ).fetchone()
        if base:
            ship_start_col = base['grid_col']
            ship_start_row = base['grid_row']
            docked_at = dock_at_base
        else:
            print(f"Warning: Base {dock_at_base} not found, using default position.")

    # Create player
    c.execute("""
        INSERT INTO players (game_id, player_name, email, account_number)
        VALUES (?, ?, ?, ?)
    """, (game_id, player_name, email, account_number))
    player_id = c.lastrowid

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
         hull_count, grid_col, grid_row, system_id, docked_at_base_id,
         tu_per_turn, tu_remaining, sensor_rating, cargo_capacity, crew_count, crew_required)
        VALUES (?, ?, ?, ?, 'Scout', 'Explorer Mk I', 'Light Hull', 50,
                ?, ?, 101, ?, 300, 300, 20, 500, 15, 10)
    """, (ship_id, game_id, political_id, ship_name,
          ship_start_col, ship_start_row, docked_at))

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

    dock_info = f" [Docked at {docked_at}]" if docked_at else ""
    print(f"Player '{player_name}' added to game {game_id}:")
    print(f"  Account Number: {account_number}  ** KEEP THIS SECRET **")
    print(f"  Political: {political_name} (ID: {political_id})")
    print(f"  Ship: {ship_name} (ID: {ship_id}) at {ship_start_col}{ship_start_row:02d}{dock_info}")
    print(f"  Starting Credits: 10,000")
    return {
        'player_id': player_id,
        'account_number': account_number,
        'political_id': political_id,
        'ship_id': ship_id,
    }


def join_game(db_path=None, game_id="OMICRON101"):
    """
    Interactive player registration form.
    
    Prompts for name, email, political name, and ship name.
    Assigns the new ship to a random starbase (docked).
    Returns the new player's details including their secret account number.
    """
    conn = get_connection(db_path)

    # Verify game exists
    game = conn.execute("SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()
    if not game:
        print(f"Error: Game {game_id} not found.")
        conn.close()
        return None

    turn_str = f"{game['current_year']}.{game['current_week']}"

    print(f"")
    print(f"======================================================")
    print(f"  STELLAR DOMINION - New Player Registration")
    print(f"  Game: {game['game_name']} ({game_id})")
    print(f"  Current Turn: {turn_str}")
    print(f"======================================================")
    print(f"")

    # Get player details
    player_name = input("  Your real name: ").strip()
    if not player_name:
        print("Error: Name is required.")
        conn.close()
        return None

    email = input("  Your email address: ").strip()
    if not email or '@' not in email:
        print("Error: Valid email is required.")
        conn.close()
        return None

    # Check email not already registered
    existing = conn.execute(
        "SELECT player_name FROM players WHERE email = ? AND game_id = ?",
        (email, game_id)
    ).fetchone()
    if existing:
        print(f"Error: Email '{email}' is already registered to {existing['player_name']}.")
        conn.close()
        return None

    political_name = input("  Name for your political character: ").strip()
    if not political_name:
        political_name = f"Commander {player_name.split()[0]}"
        print(f"  (Defaulting to: {political_name})")

    ship_name = input("  Name for your starting ship: ").strip()
    if not ship_name:
        ship_name = f"SS {player_name.split()[0]}"
        print(f"  (Defaulting to: {ship_name})")

    # Pick a random starbase to dock at
    bases = conn.execute(
        "SELECT base_id, name, grid_col, grid_row FROM starbases WHERE game_id = ?",
        (game_id,)
    ).fetchall()
    conn.close()

    if not bases:
        print("Error: No starbases available in this game.")
        return None

    dock_base = random.choice(bases)

    print(f"")
    print(f"  Confirming registration:")
    print(f"    Player:    {player_name}")
    print(f"    Email:     {email}")
    print(f"    Political: {political_name}")
    print(f"    Ship:      {ship_name}")
    print(f"    Starting:  Docked at {dock_base['name']} ({dock_base['grid_col']}{dock_base['grid_row']:02d})")
    print(f"")

    confirm = input("  Proceed? (y/n): ").strip().lower()
    if confirm not in ('y', 'yes'):
        print("  Registration cancelled.")
        return None

    # Create the player
    result = add_player(
        db_path=db_path,
        game_id=game_id,
        player_name=player_name,
        email=email,
        political_name=political_name,
        ship_name=ship_name,
        dock_at_base=dock_base['base_id'],
    )

    if result:
        print(f"")
        print(f"======================================================")
        print(f"  REGISTRATION COMPLETE")
        print(f"======================================================")
        print(f"")
        print(f"  Your Account Number: {result['account_number']}")
        print(f"")
        print(f"  ** KEEP YOUR ACCOUNT NUMBER SECRET **")
        print(f"  You will need it (along with your email) to")
        print(f"  submit orders each turn. Do not share it with")
        print(f"  other players.")
        print(f"")
        print(f"  Your political ID ({result['political_id']}) and")
        print(f"  ship ID ({result['ship_id']}) are public — other")
        print(f"  players may discover these through scanning.")
        print(f"")

    return result


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
