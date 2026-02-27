"""
Stellar Dominion - Game Setup
Creates initial game state with star systems, celestial bodies, bases, and players.
"""

import random
from pathlib import Path
from db.database import init_db, get_connection, get_faction, faction_display_name
from engine.resolution.resolver import TurnResolver
from engine.reports.report_gen import generate_ship_report, generate_prefect_report
from engine.turn_folders import TurnFolders


# Name pools for random crew generation
FIRST_NAMES = [
    "Marcus", "Elena", "Darius", "Katya", "Ravi", "Ingrid", "Tobias", "Yara",
    "Felix", "Maren", "Lukas", "Senna", "Orin", "Hana", "Cassius", "Petra",
    "Nikolai", "Ayla", "Dorian", "Freya", "Idris", "Zara", "Cyrus", "Lina",
    "Otto", "Jena", "Tariq", "Mila", "Soren", "Elsa", "Voss", "Anya",
    "Jalen", "Rhea", "Kiran", "Thea", "Henrik", "Nadia", "Brennan", "Lyra",
]

LAST_NAMES = [
    "Webb", "Kessler", "Okafor", "Strand", "Varga", "Reeves", "Navarro",
    "Lindqvist", "Holt", "Szabo", "Ortega", "Falk", "Mitra", "Graves",
    "Tanaka", "Eriksen", "Moreau", "Shah", "Voronova", "Cruz", "Decker",
    "Jansen", "Calloway", "Petrov", "Ashworth", "Torres", "Grimm", "Sato",
    "Brennan", "Larsson", "Osei", "Richter", "Vasquez", "Nolan", "Ivarsson",
    "Chen", "Kowalski", "Adeyemi", "Bergstrom", "Novak",
]


def generate_random_name():
    """Generate a random first + last name for crew."""
    return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"


# Trade role price modifiers
TRADE_ROLE_MODIFIERS = {
    'produces': 0.75,
    'average': 1.00,
    'demands': 1.50,
}

# Market cycle length in weeks (prices and stock reset every N weeks)
MARKET_CYCLE_WEEKS = 4

# Base stock/demand quantities by role per cycle (center value; fluctuates ±15%)
TRADE_ROLE_STOCK = {
    'produces': {'stock': 240, 'demand': 60},
    'average':  {'stock': 120, 'demand': 120},
    'demands':  {'stock': 60,  'demand': 240},
}

# Spread: buy costs more than sell (3% each side of effective price)
BUY_SPREAD = 1.03
SELL_SPREAD = 0.97


def get_market_cycle_start(turn_week):
    """Return the first week of the market cycle containing turn_week."""
    return ((turn_week - 1) // MARKET_CYCLE_WEEKS) * MARKET_CYCLE_WEEKS + 1


def get_market_weeks_remaining(turn_week):
    """Return how many weeks remain in the current market cycle (including this week)."""
    return MARKET_CYCLE_WEEKS - ((turn_week - 1) % MARKET_CYCLE_WEEKS)


def generate_market_prices(conn, game_id, turn_year, turn_week):
    """
    Generate market prices for a new cycle.
    
    Prices are keyed to the cycle start week and persist for MARKET_CYCLE_WEEKS.
    Stock and demand deplete over the cycle as players trade.
    
    Only call this at the start of a new cycle (or game setup).
    """
    import hashlib
    cycle_start = get_market_cycle_start(turn_week)
    seed_str = f"{game_id}-market-{turn_year}.{cycle_start}"
    seed = int(hashlib.md5(seed_str.encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)

    goods = conn.execute(
        "SELECT * FROM trade_goods"
    ).fetchall()

    week_averages = {}
    for g in goods:
        fluctuation = rng.uniform(0.95, 1.05)
        week_averages[g['item_id']] = g['base_price'] * fluctuation

    configs = conn.execute(
        "SELECT * FROM base_trade_config WHERE game_id = ?", (game_id,)
    ).fetchall()

    # Clear any existing prices for this cycle start (idempotent)
    conn.execute("""
        DELETE FROM market_prices
        WHERE game_id = ? AND turn_year = ? AND turn_week = ?
    """, (game_id, turn_year, cycle_start))

    for cfg in configs:
        avg = week_averages[cfg['item_id']]
        modifier = TRADE_ROLE_MODIFIERS.get(cfg['trade_role'], 1.0)
        effective = avg * modifier
        buy_price = max(1, round(effective * BUY_SPREAD))
        sell_price = max(1, round(effective * SELL_SPREAD))
        if sell_price >= buy_price:
            sell_price = buy_price - 1

        role_qty = TRADE_ROLE_STOCK.get(cfg['trade_role'], {'stock': 120, 'demand': 120})
        stock = max(1, round(role_qty['stock'] * rng.uniform(0.85, 1.15)))
        demand = max(1, round(role_qty['demand'] * rng.uniform(0.85, 1.15)))

        conn.execute("""
            INSERT INTO market_prices
            (game_id, base_id, item_id, turn_year, turn_week,
             buy_price, sell_price, stock, demand)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (game_id, cfg['base_id'], cfg['item_id'],
              turn_year, cycle_start, buy_price, sell_price, stock, demand))

    conn.commit()


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
        INSERT INTO star_systems (system_id, name, star_name, star_spectral_type, created_turn)
        VALUES (101, 'Omicron', 'Omicron Prime', 'G2V', '500.1')
    """)

    # =============================================
    # CELESTIAL BODIES in Omicron System
    # =============================================

    # Planet: Orion at H04 -- temperate, habitable, Earth-like
    c.execute("""
        INSERT INTO celestial_bodies 
        (body_id, system_id, name, body_type, grid_col, grid_row, gravity, temperature, atmosphere, 
         tectonic_activity, hydrosphere, life, map_symbol, surface_size)
        VALUES (247985, 101, 'Orion', 'planet', 'H', 4, 0.9, 295, 'Standard',
                4, 65, 'Sentient', 'O', 31)
    """)

    # Planet: Tartarus at R08 -- hot, volcanic, dense atmosphere
    c.execute("""
        INSERT INTO celestial_bodies 
        (body_id, system_id, name, body_type, grid_col, grid_row, gravity, temperature, atmosphere,
         tectonic_activity, hydrosphere, life, map_symbol, surface_size)
        VALUES (301442, 101, 'Tartarus', 'planet', 'R', 8, 1.2, 340, 'Dense',
                7, 15, 'Microbial', 'O', 25)
    """)

    # Gas Giant: Leviathan at E18
    c.execute("""
        INSERT INTO celestial_bodies 
        (body_id, system_id, name, body_type, grid_col, grid_row, gravity, temperature, atmosphere, map_symbol, surface_size)
        VALUES (155230, 101, 'Leviathan', 'gas_giant', 'E', 18, 2.5, 120, 'Hydrogen', 'G', 50)
    """)

    # Moon: Callyx at F19 (moon of Leviathan) -- cold, barren, icy
    c.execute("""
        INSERT INTO celestial_bodies 
        (body_id, system_id, name, body_type, parent_body_id, grid_col, grid_row, gravity, temperature, atmosphere,
         tectonic_activity, hydrosphere, life, map_symbol, surface_size)
        VALUES (88341, 101, 'Callyx', 'moon', 155230, 'F', 19, 0.3, 95, 'Thin',
                1, 40, 'None', 'o', 11)
    """)

    # Planet: Meridian at T20 -- cold, arid, thin atmosphere, sparse life
    c.execute("""
        INSERT INTO celestial_bodies 
        (body_id, system_id, name, body_type, grid_col, grid_row, gravity, temperature, atmosphere,
         tectonic_activity, hydrosphere, life, map_symbol, surface_size)
        VALUES (412003, 101, 'Meridian', 'planet', 'T', 20, 0.7, 210, 'Thin',
                2, 10, 'Plant', 'O', 21)
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

    # =============================================
    # TRADE GOODS & MARKET CONFIGURATION
    # =============================================
    trade_goods = [
        (101, 'Precious Metals', 20, 5),
        (102, 'Advanced Computer Cores', 50, 2),
        (103, 'Food Supplies', 30, 3),
    ]
    for item in trade_goods:
        c.execute("""
            INSERT OR IGNORE INTO trade_goods (item_id, name, base_price, mass_per_unit)
            VALUES (?, ?, ?, ?)
        """, item)

    # Base trade roles: (base_id, item_id, role)
    # Citadel:  produces Adv Computer Cores, average Food, demands Precious Metals
    # Tartarus: produces Precious Metals, average Adv Computer Cores, demands Food
    # Meridian: produces Food Supplies, average Precious Metals, demands Adv Computer Cores
    base_trade = [
        (45687590, 101, 'demands'),
        (45687590, 102, 'produces'),
        (45687590, 103, 'average'),
        (12340001, 101, 'produces'),
        (12340001, 102, 'average'),
        (12340001, 103, 'demands'),
        (78901234, 101, 'average'),
        (78901234, 102, 'demands'),
        (78901234, 103, 'produces'),
    ]
    for base_id, item_id, role in base_trade:
        c.execute("""
            INSERT INTO base_trade_config (base_id, game_id, item_id, trade_role)
            VALUES (?, ?, ?, ?)
        """, (base_id, game_id, item_id, role))

    conn.commit()

    # Generate initial week's market prices
    generate_market_prices(conn, game_id, 500, 1)

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


def generate_welcome_reports(db_path, game_id, account_number, prefect_id, ship_id):
    """
    Generate welcome reports for a newly added player.
    
    Runs a SYSTEMSCAN on their ship and produces both a ship report
    and a prefect report with an introductory welcome message.
    Reports are stored in the processed turn folder.
    """
    import textwrap

    conn = get_connection(db_path)
    game = conn.execute("SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()
    turn_str = f"{game['current_year']}.{game['current_week']}"
    conn.close()

    # Run SYSTEMSCAN as the welcome order
    resolver = TurnResolver(db_path, game_id=game_id)
    welcome_orders = [
        {'sequence': 1, 'command': 'SYSTEMSCAN', 'params': None},
    ]
    result = resolver.resolve_ship_turn(ship_id, welcome_orders)
    resolver.close()

    if result.get('error'):
        print(f"  Warning: could not generate welcome reports: {result['error']}")
        return

    # Welcome blurb for ship report
    ship_blurb = textwrap.dedent("""\
        WELCOME TO STELLAR DOMINION
        ===========================

        Boarding the ship the captain smiles at you warmly. "She's a fine ship
        to be sure," he states, as you watch a panel on the all behind him
        fall off, revealing freyed wiring. He looks back at you slighlty more
        nervous now. "Well, she might not be the newest ship on the market,
        but she'll get you where you want to go.

        This perhaps wasn't the state of the art vessel you had always dreamed
        of, but at least it was a start.                                                                                                                                                                               

        You have been assigned command of this vessel. Your ship has completed
        an initial system scan -- see the TURN REPORT below for what was found.

        Your Contacts section at the end of this report lists everything your
        ship has detected so far. Use this to plan your first set of orders.

        QUICK START - SAMPLE ORDERS
        ----------------------------
        Here are some example orders to get you moving. Copy the format below
        into a YAML file and submit it as your first turn:

            game: {game_id}
            account: YOUR_ACCOUNT_NUMBER
            ship: YOUR_SHIP_ID
            orders:
              - MOVE: M13          # Move to grid square M13
              - LOCATIONSCAN: {{}}   # Scan nearby area on arrival
              - ORBIT: 247985      # Enter orbit of a planet (use body ID)
              - DOCK: 45687590     # Dock at a starbase (use base ID)

        AVAILABLE COMMANDS
        -------------------
        MOVE <coord>       Move to a grid square (e.g. M13, H04). Costs 2 TU/square.
        LOCATIONSCAN       Scan nearby space. Costs 20 TU.
        SYSTEMSCAN         Produce a full system map. Costs 20 TU.
        ORBIT <body_id>    Enter orbit of a planet, moon, or gas giant. Costs 10 TU.
        DOCK <base_id>     Dock at a starbase (must be at same location). Costs 30 TU.
        UNDOCK             Leave a starbase. Costs 10 TU.
        LAND <body_id> <x> <y>  Land at surface coordinates (must be orbiting). Costs 20 TU.
        TAKEOFF            Take off from planet surface to orbit. Costs 20 TU.
        SURFACESCAN        Produce a terrain map (must be orbiting or landed). Costs 20 TU.
        WAIT <tu>          Wait and do nothing for a number of TU.

        TRADING (must be docked)
        -------------------------
        GETMARKET <base_id>                 View buy/sell prices at a base market.
        BUY <base_id> <item_id> <qty>       Buy items from a base market.
        SELL <base_id> <item_id> <qty>      Sell items to a base market.

        Trade items:  101 Precious Metals  |  102 Adv Computer Cores  |  103 Food Supplies
        YAML example: - BUY: "45687590 101 10"

        Your ship has 300 TU per turn. Unspent TU are lost at end of turn.
        Submit orders by email or file -- see your game moderator for details.

        Good luck, Commander.\
    """).format(game_id=game_id)
    ship_messages = ship_blurb.split('\n')

    # Welcome blurb for prefect report
    prefect_blurb = textwrap.dedent("""\
        WELCOME TO STELLAR DOMINION
        ===========================

        They warned you at the Stellar Academy that is was a big universe out
        there. Looks like it was time to find out. The Academy also offered                                                        
        for you to use their designation (STA) on your ship for now, as a mark
        of safety in your first steps. You know you'll have to join another 
        faction at some point, but for now you can get out own your own and
        explore.                                                                                    
                                    
        This is your Prefect report. It provides an overview of your position:
        your finances, your fleet, and everything you have discovered so far.

        You will receive this report each turn alongside your ship reports.
        Use it to track your credits, monitor your ships, and review contacts.

        Your account number is SECRET -- never share it with other players.
        Your prefect ID and ship IDs are public and may be discovered by
        other players through scanning.\
    """)
    prefect_messages = prefect_blurb.split('\n')

    # Generate reports
    ship_report = generate_ship_report(
        result, db_path, game_id,
        between_turn_messages=ship_messages
    )
    prefect_report = generate_prefect_report(
        prefect_id, db_path, game_id,
        between_turn_messages=prefect_messages
    )

    # Store in processed folder
    folders = TurnFolders(db_path=db_path, game_id=game_id)
    ship_file = folders.store_ship_report(turn_str, account_number, ship_id, ship_report)
    prefect_file = folders.store_prefect_report(turn_str, account_number, prefect_id, prefect_report)

    # Generate PDF versions if reportlab is available
    try:
        from engine.reports.pdf_export import report_file_to_pdf, is_available as pdf_available
        if pdf_available():
            report_file_to_pdf(ship_file)
            report_file_to_pdf(prefect_file)
    except Exception:
        pass  # PDF is optional

    print(f"  Welcome reports generated:")
    print(f"    Ship:    {ship_file}")
    print(f"    Prefect: {prefect_file}")


def add_player(db_path=None, game_id="OMICRON101", player_name="Player 1",
               email="player1@example.com", prefect_name="Erik Voss",
               ship_name="Boethius", ship_start_col="I", ship_start_row=6,
               start_orbit_body=None, dock_at_base=None):
    """
    Add a player with a prefect and starting ship.
    
    Starting location precedence:
      1) If dock_at_base is provided (base_id), the ship starts docked there
         and uses the base's grid position.
      2) Else if start_orbit_body is provided (body_id), the ship starts in
         orbit around that body and uses the body's grid position.
      3) Else uses ship_start_col/row.
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
    prefect_id = _generate_unique_id(conn, 'prefects', 'prefect_id')
    ship_id = _generate_unique_id(conn, 'ships', 'ship_id')

    ship_system_id = 101

    # If docking at a base, use the base's location and orbit
    docked_at = None
    orbiting_body = None
    if dock_at_base:
        base = c.execute(
            "SELECT * FROM starbases WHERE base_id = ? AND game_id = ?",
            (dock_at_base, game_id)
        ).fetchone()
        if base:
            ship_start_col = base['grid_col']
            ship_start_row = base['grid_row']
            ship_system_id = base['system_id']
            docked_at = dock_at_base
            orbiting_body = base['orbiting_body_id']  # May be None for space bases
        else:
            print(f"Warning: Base {dock_at_base} not found, using default position.")
    elif start_orbit_body:
        body = c.execute(
            "SELECT cb.* FROM celestial_bodies cb "
            "WHERE cb.body_id = ?",
            (start_orbit_body,)
        ).fetchone()
        if body:
            ship_start_col = body['grid_col']
            ship_start_row = body['grid_row']
            ship_system_id = body['system_id']
            docked_at = None
            orbiting_body = body['body_id']
        else:
            print(f"Warning: Body {start_orbit_body} not found, using default position.")

    # Create player
    c.execute("""
        INSERT INTO players (game_id, player_name, email, account_number)
        VALUES (?, ?, ?, ?)
    """, (game_id, player_name, email, account_number))
    player_id = c.lastrowid

    # Create prefect
    c.execute("""
        INSERT INTO prefects 
        (prefect_id, player_id, game_id, name, credits, location_type, location_id,
         created_turn_year, created_turn_week)
        VALUES (?, ?, ?, ?, 10000, 'ship', ?, ?, ?)
    """, (prefect_id, player_id, game_id, prefect_name, ship_id,
          game['current_year'], game['current_week']))

    # Create ship
    c.execute("""
        INSERT INTO ships 
        (ship_id, game_id, owner_prefect_id, name, ship_class, design, hull_type,
         hull_count, grid_col, grid_row, system_id, docked_at_base_id, orbiting_body_id,
         tu_per_turn, tu_remaining, sensor_rating, cargo_capacity, crew_count, crew_required)
        VALUES (?, ?, ?, ?, 'Trader', 'Light Trader MK I', 'Commercial', 50,
                ?, ?, ?, ?, ?, 300, 300, 20, 500, 15, 10)
    """, (ship_id, game_id, prefect_id, ship_name,
          ship_start_col, ship_start_row, ship_system_id, docked_at, orbiting_body))

    # Add a starting captain (randomly generated)
    captain_name = generate_random_name()
    c.execute("""
        INSERT INTO officers (ship_id, crew_number, name, rank, specialty, experience, crew_factors)
        VALUES (?, 1, ?, 'Captain', 'Navigation', 0, 8)
    """, (ship_id, captain_name))

    # Add starting installed items
    starting_items = [
        (ship_id, 100, 'Bridge', 1, 5),
        (ship_id, 103, 'Sensor', 1, 1),
        (ship_id, 155, 'Sublight Engines', 3, 1),
        (ship_id, 174, 'Jump Drive - Basic', 1, 5),
        (ship_id, 160, 'Thrust Engine', 2, 2),
        (ship_id, 131, 'Quarters', 2, 3),
        (ship_id, 180, 'Cargo Hold', 5, 10),
    ]
    for sid, item_type, item_name, qty, mass in starting_items:
        c.execute("""
            INSERT INTO installed_items (ship_id, item_type_id, item_name, quantity, mass_per_unit)
            VALUES (?, ?, ?, ?, ?)
        """, (sid, item_type, item_name, qty, mass))

    conn.commit()
    conn.close()

    dock_info = f" [Docked at {docked_at}]" if docked_at else ""
    orbit_info = f" [Orbiting {orbiting_body}]" if orbiting_body and not docked_at else ""
    print(f"Player '{player_name}' added to game {game_id}:")
    print(f"  Account Number: {account_number}  ** KEEP THIS SECRET **")
    print(f"  Prefect: {prefect_name} (ID: {prefect_id})")
    print(f"  Faction: Stellar Training Academy")
    print(f"  Ship: STA {ship_name} (ID: {ship_id}) at {ship_start_col}{ship_start_row:02d}{dock_info}{orbit_info}")
    print(f"  Starting Credits: 10,000")

    # Generate welcome reports (system scan + intro blurb)
    generate_welcome_reports(db_path, game_id, account_number, prefect_id, ship_id)

    return {
        'player_id': player_id,
        'account_number': account_number,
        'prefect_id': prefect_id,
        'ship_id': ship_id,
    }


def join_game(db_path=None, game_id="OMICRON101"):
    """
    Interactive player registration form.
    
    Prompts for name, email, prefect name, and ship name.
    Prompts the player to choose a starting planet to begin in orbit around.
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

    prefect_name = input("  Name for your prefect character: ").strip()
    if not prefect_name:
        prefect_name = f"Commander {player_name.split()[0]}"
        print(f"  (Defaulting to: {prefect_name})")

    ship_name = input("  Name for your starting ship: ").strip()
    if not ship_name:
        ship_name = f"{player_name.split()[0]}"
        print(f"  (Defaulting to: {ship_name})")

    # Present a list of planets to start in orbit around
    planets = conn.execute(
        "SELECT cb.body_id, cb.name, cb.grid_col, cb.grid_row, cb.system_id, ss.name as system_name "
        "FROM celestial_bodies cb "
        "JOIN star_systems ss ON cb.system_id = ss.system_id "
        "WHERE cb.body_type = 'planet' "
        "ORDER BY cb.name"
    ).fetchall()
    conn.close()

    if not planets:
        print("Error: No planets available in this game.")
        return None

    print("")
    print("  Available starting planets (enter body ID):")
    for p in planets:
        loc = f"{p['grid_col']}{p['grid_row']:02d}"
        print(f"    - {p['name']} ({p['body_id']}) at {loc} - {p['system_name']} ({p['system_id']})")

    planet_by_id = {int(p['body_id']): p for p in planets}
    chosen_planet = None
    while not chosen_planet:
        raw = input("  Starting planet ID (blank = random): ").strip()
        if not raw:
            chosen_planet = random.choice(planets)
            break
        try:
            pid = int(raw)
        except ValueError:
            print("  Please enter a numeric planet/body ID from the list.")
            continue
        chosen_planet = planet_by_id.get(pid)
        if not chosen_planet:
            print("  Unknown planet ID. Choose one from the list above.")

    print(f"")
    print(f"  Confirming registration:")
    print(f"    Player:    {player_name}")
    print(f"    Email:     {email}")
    print(f"    Prefect: {prefect_name}")
    print(f"    Ship:      STA {ship_name}")
    print(f"    Faction:   STA - Stellar Training Academy")
    start_loc = f"{chosen_planet['grid_col']}{chosen_planet['grid_row']:02d}"
    print(f"    Starting:  In orbit of {chosen_planet['name']} ({start_loc})")
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
        prefect_name=prefect_name,
        ship_name=ship_name,
        start_orbit_body=int(chosen_planet['body_id']),
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
        print(f"  Your prefect ID ({result['prefect_id']}) and")
        print(f"  ship ID ({result['ship_id']}) are public -- other")
        print(f"  players may discover these through scanning.")
        print(f"")

    return result


def setup_demo_game(db_path=None):
    """Create a complete demo game with 2 players."""
    if create_game(db_path):
        p1 = add_player(db_path, player_name="Alice", email="alice@example.com",
                         prefect_name="Li Chen", ship_name="Boethius",
                         ship_start_col="I", ship_start_row=6)
        p2 = add_player(db_path, player_name="Bob", email="bob@example.com",
                         prefect_name="Erik Voss", ship_name="Resolute",
                         ship_start_col="P", ship_start_row=15)
        return p1, p2
    return None


def suspend_player(db_path=None, game_id="OMICRON101", account_number=None, email=None):
    """
    Suspend a player account and all associated positions/assets.

    Suspended players:
    - Cannot submit orders
    - Ships are invisible to other players (scans, maps)
    - Ships do not participate in turn resolution
    - Do not appear in turn-status
    - Prefect position and credits are preserved
    - Can be reinstated later with full state restored

    Identify player by account_number or email (one required).
    """
    conn = get_connection(db_path)

    # Find the player
    if account_number:
        player = conn.execute(
            "SELECT * FROM players WHERE account_number = ? AND game_id = ?",
            (account_number, game_id)
        ).fetchone()
    elif email:
        player = conn.execute(
            "SELECT * FROM players WHERE email = ? AND game_id = ?",
            (email, game_id)
        ).fetchone()
    else:
        print("Error: must provide --account or --email to identify the player.")
        conn.close()
        return False

    if not player:
        print(f"Error: player not found in game {game_id}.")
        conn.close()
        return False

    if player['status'] == 'suspended':
        print(f"Player '{player['player_name']}' is already suspended.")
        conn.close()
        return False

    # Get their prefect and ships for the summary
    prefect = conn.execute(
        "SELECT * FROM prefects WHERE player_id = ? AND game_id = ?",
        (player['player_id'], game_id)
    ).fetchone()

    ships = []
    if prefect:
        ships = conn.execute(
            "SELECT ship_id, name FROM ships WHERE owner_prefect_id = ? AND game_id = ?",
            (prefect['prefect_id'], game_id)
        ).fetchall()

    # Set player status to suspended
    conn.execute(
        "UPDATE players SET status = 'suspended' WHERE player_id = ?",
        (player['player_id'],)
    )
    conn.commit()

    # Get faction info for display
    faction_id = prefect["faction_id"] if prefect else None
    faction = get_faction(conn, faction_id)

    conn.close()

    print(f"Player SUSPENDED: {player['player_name']} ({player['email']})")
    print(f"  Account: {player['account_number']}")
    if prefect:
        print(f"  Prefect: {prefect['name']} ({prefect['prefect_id']})")
        print(f"  Faction: {faction['abbreviation']} - {faction['name']}")
    if ships:
        print(f"  Ships archived ({len(ships)}):")
        for s in ships:
            print(f"    {faction['abbreviation']} {s['name']} ({s['ship_id']})")
    print(f"")
    print(f"  All assets are preserved and invisible to other players.")
    print(f"  Use 'reinstate-player' to restore this account.")
    return True


def reinstate_player(db_path=None, game_id="OMICRON101", account_number=None, email=None):
    """
    Reinstate a previously suspended player account.

    All positions, ships, and assets become active and visible again.
    """
    conn = get_connection(db_path)

    # Find the player
    if account_number:
        player = conn.execute(
            "SELECT * FROM players WHERE account_number = ? AND game_id = ?",
            (account_number, game_id)
        ).fetchone()
    elif email:
        player = conn.execute(
            "SELECT * FROM players WHERE email = ? AND game_id = ?",
            (email, game_id)
        ).fetchone()
    else:
        print("Error: must provide --account or --email to identify the player.")
        conn.close()
        return False

    if not player:
        print(f"Error: player not found in game {game_id}.")
        conn.close()
        return False

    if player['status'] == 'active':
        print(f"Player '{player['player_name']}' is already active.")
        conn.close()
        return False

    # Get their prefect and ships for the summary
    prefect = conn.execute(
        "SELECT * FROM prefects WHERE player_id = ? AND game_id = ?",
        (player['player_id'], game_id)
    ).fetchone()

    ships = []
    if prefect:
        ships = conn.execute(
            "SELECT ship_id, name, grid_col, grid_row FROM ships WHERE owner_prefect_id = ? AND game_id = ?",
            (prefect['prefect_id'], game_id)
        ).fetchall()

    # Set player status back to active
    conn.execute(
        "UPDATE players SET status = 'active' WHERE player_id = ?",
        (player['player_id'],)
    )
    conn.commit()

    # Get faction info for display
    faction_id = prefect["faction_id"] if prefect else None
    faction = get_faction(conn, faction_id)

    conn.close()

    print(f"Player REINSTATED: {player['player_name']} ({player['email']})")
    print(f"  Account: {player['account_number']}")
    if prefect:
        print(f"  Prefect: {prefect['name']} ({prefect['prefect_id']})")
        print(f"  Faction: {faction['abbreviation']} - {faction['name']}")
        print(f"  Credits: {prefect['credits']:,.0f}")
    if ships:
        print(f"  Ships restored ({len(ships)}):")
        for s in ships:
            loc = f"{s['grid_col']}{s['grid_row']:02d}"
            print(f"    {faction['abbreviation']} {s['name']} ({s['ship_id']}) at {loc}")
    print(f"")
    print(f"  All assets are now visible and active again.")
    print(f"  Player can submit orders for the current turn.")
    return True


def list_players(db_path=None, game_id="OMICRON101", include_suspended=False):
    """List all players in a game with their status."""
    conn = get_connection(db_path)

    query = """
        SELECT p.*, pp.prefect_id, pp.name as prefect_name, pp.credits, pp.faction_id,
               f.abbreviation as faction_abbr
        FROM players p
        LEFT JOIN prefects pp ON p.player_id = pp.player_id AND pp.game_id = p.game_id
        LEFT JOIN factions f ON pp.faction_id = f.faction_id
        WHERE p.game_id = ?
    """
    if not include_suspended:
        query += " AND p.status = 'active'"
    query += " ORDER BY p.player_name"

    players = conn.execute(query, (game_id,)).fetchall()

    if not players:
        print(f"No players in game {game_id}.")
        conn.close()
        return

    print(f"\nPlayers in game {game_id}:")
    print(f"{'Name':<16} {'Email':<28} {'Account':<12} {'Faction':<8} {'Prefect':<20} {'Status':<10}")
    print("-" * 94)
    for p in players:
        status = p['status'] if p['status'] != 'active' else ''
        pol_name = p['prefect_name'] or '--'
        faction = p['faction_abbr'] or '--'
        print(f"{p['player_name']:<16} {p['email']:<28} {p['account_number']:<12} {faction:<8} {pol_name:<20} {status:<10}")

    conn.close()
