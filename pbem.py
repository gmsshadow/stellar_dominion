#!/usr/bin/env python3
"""
Stellar Dominion - PBEM Strategy Game Engine
Main CLI entry point.

Usage:
    python pbem.py setup-game --demo                    # Create the demo game
    python pbem.py add-player --name "Alice" --email ... # Add a player
    python pbem.py submit-orders <file> --email ...     # Submit ship orders
    python pbem.py run-turn --game OMICRON101              # Resolve all pending orders
    python pbem.py show-map --game OMICRON101              # Display system map
    python pbem.py show-status --ship <id>              # Show ship status
    python pbem.py advance-turn --game OMICRON101          # Advance to next turn
    python pbem.py list-ships --game OMICRON101            # List all ships
    python pbem.py turn-status --game OMICRON101           # Show incoming/processed status
"""

import argparse
import sys
import json
import shutil
from pathlib import Path
from datetime import datetime

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from db.database import init_db, get_connection, migrate_db
from engine.game_setup import create_game, add_player, setup_demo_game, join_game, suspend_player, reinstate_player, list_players
from engine.orders.parser import parse_orders_file, parse_yaml_orders, parse_text_orders
from engine.resolution.resolver import TurnResolver
from engine.reports.report_gen import generate_ship_report, generate_political_report
from engine.maps.system_map import render_system_map
from engine.turn_folders import TurnFolders


# ======================================================================
# SETUP COMMANDS
# ======================================================================

def cmd_setup_game(args):
    """Create the demo game with Omicron system."""
    db_path = Path(args.db) if args.db else None
    if args.demo:
        setup_demo_game(db_path)
    else:
        create_game(db_path, game_id=args.game, game_name=args.name or "Stellar Dominion")


def cmd_add_player(args):
    """Add a player to the game."""
    db_path = Path(args.db) if args.db else None
    result = add_player(
        db_path, game_id=args.game,
        player_name=args.name, email=args.email,
        political_name=args.political or f"Commander {args.name}",
        ship_name=args.ship_name or f"SS {args.name}",
        ship_start_col=args.start_col or 'I',
        ship_start_row=args.start_row or 6
    )
    if result:
        print(f"\nShip ID: {result['ship_id']} (use this for orders)")


def cmd_join_game(args):
    """Interactive player registration form."""
    db_path = Path(args.db) if args.db else None
    join_game(db_path=db_path, game_id=args.game)


# ======================================================================
# ORDER SUBMISSION
# ======================================================================

def cmd_submit_orders(args):
    """
    Parse, validate, and file incoming orders.
    
    Flow:
    1. Parse the orders file (YAML or text)
    2. Validate sender email owns the ship
    3. Store valid orders in incoming/{turn}/{email}/
    4. Write a receipt or rejection file
    5. Store orders in database for resolution
    """
    db_path = Path(args.db) if args.db else None
    folders = TurnFolders(db_path=db_path, game_id=args.game)
    turn_str = folders.get_current_turn_str()

    # Determine sender email
    email = args.email
    if not email:
        print("Error: --email is required to identify the submitting player.")
        print("  (In future, this will be extracted from the IMAP envelope.)")
        return

    # Parse the orders file
    filepath = Path(args.orders_file)
    orders = parse_orders_file(filepath)

    if orders.get('error'):
        print(f"Parse error: {orders['error']}")
        return

    ship_id = orders.get('ship', '')
    if not ship_id:
        print("Error: orders file must specify a ship ID.")
        return

    raw_content = filepath.read_text()

    # Validate ownership: does this email own this ship?
    valid, account_number, error_msg = folders.validate_ship_ownership(email, ship_id)

    if not valid:
        # Store as rejected
        rejected_file, reason_file = folders.store_rejected(
            turn_str, email, ship_id, raw_content,
            reasons=[error_msg] + orders.get('errors', [])
        )
        print(f"REJECTED: {error_msg}")
        print(f"  Rejection stored: {rejected_file}")
        print(f"  Reason file:      {reason_file}")
        return

    # Check for parse errors on individual orders
    warnings = orders.get('errors', [])

    if not orders.get('orders'):
        folders.store_rejected(
            turn_str, email, ship_id, raw_content,
            reasons=["No valid orders found"] + warnings
        )
        print(f"REJECTED: No valid orders in file.")
        if warnings:
            for w in warnings:
                print(f"  - {w}")
        return

    # Store the incoming orders file
    orders_file = folders.store_incoming_orders(turn_str, email, ship_id, raw_content)

    # Write receipt
    receipt_file = folders.store_receipt(turn_str, email, ship_id, {
        'status': 'accepted',
        'order_count': len(orders['orders']),
        'warnings': warnings,
    })

    # Store orders in database for turn resolution
    if not args.dry_run:
        conn = get_connection(db_path)
        game = conn.execute("SELECT * FROM games WHERE game_id = ?", (args.game,)).fetchone()

        # Find player
        player = conn.execute(
            "SELECT player_id FROM players WHERE email = ? AND game_id = ?",
            (email, args.game)
        ).fetchone()

        # Clear any existing orders for this ship/turn (resubmission replaces)
        conn.execute("""
            DELETE FROM turn_orders
            WHERE game_id = ? AND turn_year = ? AND turn_week = ?
            AND subject_type = 'ship' AND subject_id = ? AND status = 'pending'
        """, (args.game, game['current_year'], game['current_week'], int(ship_id)))

        for o in orders['orders']:
            params_json = json.dumps(o['params']) if o['params'] is not None else None
            conn.execute("""
                INSERT INTO turn_orders 
                (game_id, turn_year, turn_week, player_id, subject_type, subject_id,
                 order_sequence, command, parameters, status)
                VALUES (?, ?, ?, ?, 'ship', ?, ?, ?, ?, 'pending')
            """, (
                args.game, game['current_year'], game['current_week'],
                player['player_id'], int(ship_id),
                o['sequence'], o['command'], params_json
            ))

        conn.commit()
        conn.close()

    # Summary
    print(f"Orders ACCEPTED for turn {turn_str}")
    print(f"  Game:     {args.game}")
    print(f"  Email:    {email}")
    print(f"  Ship:     {ship_id}")
    print(f"  Orders:   {len(orders['orders'])} valid")
    if warnings:
        print(f"  Warnings: {len(warnings)}")
        for w in warnings:
            print(f"    - {w}")
    print(f"  Filed:    {orders_file}")
    print(f"  Receipt:  {receipt_file}")

    # Print order list
    for o in orders['orders']:
        params_str = f" {o['params']}" if o['params'] else ""
        print(f"    {o['sequence']:>2}. {o['command']}{params_str}")

    if args.dry_run:
        print("\n  (Dry run - orders filed but not stored in database)")


# ======================================================================
# TURN RESOLUTION
# ======================================================================

def cmd_run_turn(args):
    """
    Resolve turn for all ships (or a specific ship).
    
    Flow:
    1. Gather stored orders from database
    2. Resolve each ship's orders sequentially
    3. Generate ship and political reports
    4. Store reports in processed/{turn}/{political_id}/
    """
    db_path = Path(args.db) if args.db else None
    resolver = TurnResolver(db_path, game_id=args.game)
    folders = TurnFolders(db_path=db_path, game_id=args.game)

    conn = get_connection(db_path)
    game = conn.execute("SELECT * FROM games WHERE game_id = ?", (args.game,)).fetchone()
    if not game:
        print(f"Error: Game {args.game} not found.")
        conn.close()
        return

    turn_str = f"{game['current_year']}.{game['current_week']}"

    # Determine which ships to resolve
    if args.ship:
        ships = [conn.execute(
            "SELECT * FROM ships WHERE ship_id = ? AND game_id = ?",
            (args.ship, args.game)
        ).fetchone()]
        if not ships[0]:
            print(f"Ship {args.ship} not found.")
            conn.close()
            return
    else:
        ships = conn.execute(
            """SELECT s.* FROM ships s
               JOIN political_positions pp ON s.owner_political_id = pp.position_id
               JOIN players p ON pp.player_id = p.player_id
               WHERE s.game_id = ? AND p.status = 'active'""",
            (args.game,)
        ).fetchall()

    print(f"=== Resolving Turn {turn_str} for game {args.game} ===\n")

    for ship in ships:
        ship_id = ship['ship_id']
        political_id = ship['owner_political_id']

        # Look up account number for this ship's owner
        account_number = folders.get_account_for_political(political_id)

        # Get stored orders for this ship/turn
        stored_orders = conn.execute("""
            SELECT * FROM turn_orders 
            WHERE game_id = ? AND turn_year = ? AND turn_week = ?
            AND subject_type = 'ship' AND subject_id = ? AND status = 'pending'
            ORDER BY order_sequence
        """, (args.game, game['current_year'], game['current_week'], ship_id)).fetchall()

        if not stored_orders:
            print(f"  {ship['name']} ({ship_id}): No orders this turn.")
            continue

        # Build orders list from stored orders
        order_list = []
        for so in stored_orders:
            params = json.loads(so['parameters']) if so['parameters'] else None
            order_list.append({
                'sequence': so['order_sequence'],
                'command': so['command'],
                'params': params,
            })

        print(f"  Resolving {ship['name']} ({ship_id}) - {len(order_list)} orders...")
        result = resolver.resolve_ship_turn(ship_id, order_list)

        if result.get('error'):
            print(f"    Error: {result['error']}")
            continue

        # Mark stored orders as resolved
        conn.execute("""
            UPDATE turn_orders SET status = 'resolved'
            WHERE game_id = ? AND turn_year = ? AND turn_week = ?
            AND subject_type = 'ship' AND subject_id = ? AND status = 'pending'
        """, (args.game, game['current_year'], game['current_week'], ship_id))
        conn.commit()

        # Generate ship report
        report = generate_ship_report(result, db_path, args.game)

        # Store in processed/{turn}/{account_number}/
        report_file = folders.store_ship_report(turn_str, account_number, ship_id, report)
        print(f"    Ship report:      {report_file}")

        # Print to console if verbose
        if args.verbose:
            print()
            print(report)
            print()

    # Generate one political report per active political position
    all_politicals = conn.execute("""
        SELECT DISTINCT pp.position_id, p.email, p.account_number
        FROM political_positions pp
        JOIN players p ON pp.player_id = p.player_id
        WHERE pp.game_id = ? AND p.status = 'active'
    """, (args.game,)).fetchall()

    print()
    for pol in all_politicals:
        political_id = pol['position_id']
        account_number = pol['account_number']
        political_report = generate_political_report(political_id, db_path, args.game)
        pol_file = folders.store_political_report(turn_str, account_number, political_id, political_report)
        email = pol['email']
        print(f"  Account {account_number} -> {email}")
        print(f"    Political report: {pol_file}")

        # Show total files to send
        player_reports = folders.get_player_reports(turn_str, account_number)
        if len(player_reports) > 1:
            print(f"    Total files to email: {len(player_reports)}")

    resolver.close()
    conn.close()
    print(f"\n=== Turn {turn_str} resolution complete ===")

    # Show processed folder structure
    processed = folders.list_processed(turn_str)
    if processed:
        print(f"\nProcessed folder: {folders.processed_dir / turn_str}/")
        for acct_num, files in processed.items():
            email = folders.get_email_for_account(acct_num)
            email_str = f" -> {email}" if email else ""
            print(f"  {acct_num}/{email_str}")
            for f in files:
                print(f"    {f.name}")


# ======================================================================
# TURN STATUS
# ======================================================================

def cmd_turn_status(args):
    """
    Show the status of incoming orders and processed reports for a turn.
    """
    db_path = Path(args.db) if args.db else None
    folders = TurnFolders(db_path=db_path, game_id=args.game)

    turn_str = args.turn or folders.get_current_turn_str()
    summary = folders.get_turn_summary(turn_str)

    print(f"\n=== Turn {turn_str} Status - Game {args.game} ===\n")

    for player in summary['players']:
        status_icon = "[done]" if player['processed'] else "[    ]"
        print(f"{status_icon} {player['name']} ({player['email']})")
        print(f"        Account:   {player['account_number']}")
        print(f"        Political: {player['political_name']} ({player['political_id']})")
        print(f"        Reports generated: {'Yes' if player['processed'] else 'No'}")

        for ship in player['ships']:
            if ship['orders_rejected']:
                icon = " [FAIL]"
                status = "REJECTED"
            elif ship['orders_received']:
                icon = " [ ok ]"
                status = "received"
            else:
                icon = " [    ]"
                status = "awaiting orders"

            print(f"       {icon} {ship['ship_name']} ({ship['ship_id']}): {status}")
        print()

    # Show folder structure
    incoming = folders.list_incoming(turn_str)
    if incoming:
        print(f"Incoming: {folders.incoming_dir / turn_str}/")
        current_email = None
        for entry in incoming:
            if entry['email'] != current_email:
                current_email = entry['email']
                print(f"  {current_email}/")
            status_label = {
                'received': '  [ok]  ',
                'pending': '  [  ]  ',
                'rejected': '  [FAIL]',
            }.get(entry['status'], '  [??]  ')
            print(f"   {status_label} {entry['filepath'].name}")

    processed = folders.list_processed(turn_str)
    if processed:
        print(f"\nProcessed: {folders.processed_dir / turn_str}/")
        for acct_num, files in processed.items():
            email = folders.get_email_for_account(acct_num)
            email_str = f" -> {email}" if email else ""
            print(f"  {acct_num}/{email_str}")
            for f in files:
                print(f"    {f.name}")


# ======================================================================
# MAP & STATUS COMMANDS
# ======================================================================

def cmd_show_map(args):
    """Display the system map."""
    db_path = Path(args.db) if args.db else None
    conn = get_connection(db_path)

    system = conn.execute(
        "SELECT * FROM star_systems WHERE game_id = ? AND system_id = ?",
        (args.game, args.system or 101)
    ).fetchone()

    if not system:
        print("System not found.")
        conn.close()
        return

    system_id = system['system_id']
    objects = []

    bodies = conn.execute(
        "SELECT * FROM celestial_bodies WHERE system_id = ?", (system_id,)
    ).fetchall()
    for b in bodies:
        objects.append({'type': b['body_type'], 'col': b['grid_col'], 'row': b['grid_row'],
                        'symbol': b['map_symbol'], 'name': b['name']})

    bases = conn.execute(
        "SELECT * FROM starbases WHERE system_id = ? AND game_id = ?",
        (system_id, args.game)
    ).fetchall()
    for b in bases:
        objects.append({'type': 'base', 'col': b['grid_col'], 'row': b['grid_row'],
                        'symbol': 'B', 'name': b['name']})

    ships = conn.execute(
        """SELECT s.* FROM ships s
           JOIN political_positions pp ON s.owner_political_id = pp.position_id
           JOIN players p ON pp.player_id = p.player_id
           WHERE s.system_id = ? AND s.game_id = ? AND p.status = 'active'""",
        (system_id, args.game)
    ).fetchall()
    for s in ships:
        objects.append({'type': 'ship', 'col': s['grid_col'], 'row': s['grid_row'],
                        'symbol': '@', 'name': s['name']})

    system_data = {
        'star_col': system['star_grid_col'],
        'star_row': system['star_grid_row']
    }

    title = f"{system['name']} System ({system_id})"
    print(f"\n{title}")
    print("=" * len(title))
    print(render_system_map(system_data, objects))

    print(f"\nLegend:")
    print(f"  *  Star ({system['star_name']})")
    for b in bodies:
        loc = f"{b['grid_col']}{b['grid_row']:02d}"
        print(f"  {b['map_symbol']}  {b['name']} ({b['body_id']}) at {loc} - {b['body_type']}")
    for b in bases:
        loc = f"{b['grid_col']}{b['grid_row']:02d}"
        print(f"  B  {b['name']} ({b['base_id']}) at {loc}")
    for s in ships:
        loc = f"{s['grid_col']}{s['grid_row']:02d}"
        dock_info = f" [Docked at {s['docked_at_base_id']}]" if s['docked_at_base_id'] else ""
        print(f"  @  {s['name']} ({s['ship_id']}) at {loc}{dock_info}")

    conn.close()


def cmd_show_status(args):
    """Show status of a ship."""
    db_path = Path(args.db) if args.db else None
    conn = get_connection(db_path)

    if args.ship:
        ship = conn.execute(
            "SELECT s.*, ss.name as system_name FROM ships s "
            "JOIN star_systems ss ON s.system_id = ss.system_id "
            "WHERE s.ship_id = ?", (args.ship,)
        ).fetchone()
        if ship:
            loc = f"{ship['grid_col']}{ship['grid_row']:02d}"
            dock_info = f" [Docked at {ship['docked_at_base_id']}]" if ship['docked_at_base_id'] else ""
            orbit_info = f" [Orbiting {ship['orbiting_body_id']}]" if ship['orbiting_body_id'] else ""
            print(f"Ship: {ship['name']} ({ship['ship_id']})")
            print(f"  Location: {loc} - {ship['system_name']} ({ship['system_id']}){dock_info}{orbit_info}")
            print(f"  Class: {ship['design']} {ship['ship_class']}")
            print(f"  Hull: {ship['hull_count']} {ship['hull_type']} ({ship['hull_damage_pct']:.0f}% damage)")
            print(f"  TU: {ship['tu_remaining']}/{ship['tu_per_turn']}")
            print(f"  Cargo: {ship['cargo_used']}/{ship['cargo_capacity']}")
            print(f"  Crew: {ship['crew_count']}/{ship['crew_required']}")
        else:
            print(f"Ship {args.ship} not found.")

    conn.close()


def cmd_list_ships(args):
    """List all ships in a game."""
    db_path = Path(args.db) if args.db else None
    conn = get_connection(db_path)

    query = """
        SELECT s.*, ss.name as system_name, pp.name as owner_name,
               p.email, p.account_number, p.status as player_status
        FROM ships s 
        JOIN star_systems ss ON s.system_id = ss.system_id
        JOIN political_positions pp ON s.owner_political_id = pp.position_id
        JOIN players p ON pp.player_id = p.player_id
        WHERE s.game_id = ?
    """
    params = [args.game]
    if not getattr(args, 'all', False):
        query += " AND p.status = 'active'"
    query += " ORDER BY p.player_name, s.name"

    ships = conn.execute(query, params).fetchall()

    if not ships:
        print(f"No ships in game {args.game}.")
    else:
        print(f"\nShips in game {args.game}:")
        print(f"{'ID':<12} {'Name':<20} {'Owner':<18} {'Account':<12} {'Location':<10} {'TU':<10} {'Status':<10}")
        print("-" * 92)
        for s in ships:
            loc = f"{s['grid_col']}{s['grid_row']:02d}"
            dock = f" [D]" if s['docked_at_base_id'] else ""
            status = "SUSPENDED" if s['player_status'] == 'suspended' else ""
            print(f"{s['ship_id']:<12} {s['name']:<20} {s['owner_name']:<18} "
                  f"{s['account_number']:<12} {loc}{dock:<10} {s['tu_remaining']}/{s['tu_per_turn']:<4} {status}")

    conn.close()


# ======================================================================
# TURN MANAGEMENT
# ======================================================================

def cmd_advance_turn(args):
    """Advance to the next game turn."""
    db_path = Path(args.db) if args.db else None
    resolver = TurnResolver(db_path, game_id=args.game)

    game = resolver.get_game()
    old_turn = f"{game['current_year']}.{game['current_week']}"

    year, week = resolver.advance_turn()
    new_turn = f"{year}.{week}"

    # Reset TU for all ships
    conn = get_connection(db_path)
    conn.execute(
        "UPDATE ships SET tu_remaining = tu_per_turn WHERE game_id = ?",
        (args.game,)
    )
    conn.commit()
    conn.close()

    print(f"Turn advanced: {old_turn} -> {new_turn}")
    print("All ship TUs reset.")
    resolver.close()


def cmd_edit_credits(args):
    """Set credits for a political position."""
    db_path = Path(args.db) if args.db else None
    conn = get_connection(db_path)

    political = conn.execute(
        "SELECT * FROM political_positions WHERE position_id = ?",
        (args.political,)
    ).fetchone()
    if political:
        conn.execute(
            "UPDATE political_positions SET credits = ? WHERE position_id = ?",
            (args.amount, args.political)
        )
        conn.commit()
        print(f"Credits for {political['name']} ({args.political}) set to {args.amount:,.0f}")
    else:
        print(f"Political position {args.political} not found.")

    conn.close()


# ======================================================================
# MAIN
# ======================================================================

def cmd_suspend_player(args):
    """Suspend a player account."""
    db_path = Path(args.db) if args.db else None
    suspend_player(
        db_path=db_path,
        game_id=args.game,
        account_number=getattr(args, 'account', None),
        email=getattr(args, 'email', None)
    )


def cmd_reinstate_player(args):
    """Reinstate a suspended player account."""
    db_path = Path(args.db) if args.db else None
    reinstate_player(
        db_path=db_path,
        game_id=args.game,
        account_number=getattr(args, 'account', None),
        email=getattr(args, 'email', None)
    )


def cmd_list_players(args):
    """List all players in the game."""
    db_path = Path(args.db) if args.db else None
    list_players(
        db_path=db_path,
        game_id=args.game,
        include_suspended=getattr(args, 'all', False)
    )


def cmd_process_inbox(args):
    """
    Process all order files from an inbox directory.

    Scans a directory (simulating an email inbox) for order files.
    Each file should be in a subdirectory named by the sender's email:
        inbox/
          alice@example.com/
            orders_12345678.yaml
          bob@example.com/
            orders_87654321.yaml

    Or flat with email embedded in filename:
        inbox/
          alice@example.com_orders_12345678.yaml

    For each file found, runs the same validation and filing as submit-orders.
    """
    db_path = Path(args.db) if args.db else None
    inbox_dir = Path(args.inbox)
    folders = TurnFolders(db_path=db_path, game_id=args.game)

    if not inbox_dir.exists():
        print(f"Error: inbox directory '{inbox_dir}' not found.")
        return

    turn_str = folders.get_current_turn_str()
    conn = get_connection(db_path)

    print(f"\n=== Processing Inbox - Game {args.game}, Turn {turn_str} ===\n")

    # Collect order files to process
    order_files = []

    # Pattern 1: subdirectories named by email
    for item in sorted(inbox_dir.iterdir()):
        if item.is_dir() and '@' in item.name:
            email = item.name
            for f in sorted(item.iterdir()):
                if f.suffix in ('.yaml', '.yml', '.txt') and f.name.startswith('orders'):
                    order_files.append((email, f))
        # Pattern 2: flat files with email prefix
        elif item.is_file() and '@' in item.name and item.name.startswith(('orders', 'email_')):
            # Try to extract email from filename: email_user@domain.com_orders_SHIPID.yaml
            parts = item.name.split('_', 1)
            if '@' in parts[0]:
                email = parts[0]
                order_files.append((email, item))

    if not order_files:
        print("No order files found in inbox.")
        conn.close()
        return

    accepted = 0
    rejected = 0
    skipped = 0

    for email, filepath in order_files:
        print(f"  Processing: {email} / {filepath.name}")

        # Read and parse the orders file
        try:
            content = filepath.read_text()
            parsed = parse_orders_file(str(filepath))
        except Exception as e:
            print(f"    ERROR: could not parse file: {e}")
            rejected += 1
            continue

        if parsed.get('error'):
            print(f"    REJECTED: {parsed['error']}")
            rejected += 1
            continue

        orders = parsed.get('orders', [])
        if not orders:
            errors = parsed.get('errors', [])
            if errors:
                print(f"    REJECTED: {'; '.join(errors)}")
                rejected += 1
            else:
                print(f"    SKIP: no valid orders found in file")
                skipped += 1
            continue

        # Determine ship_id from the parsed header
        ship_id = parsed.get('ship')
        if not ship_id:
            print(f"    SKIP: no ship ID found in orders file")
            skipped += 1
            continue

        try:
            ship_id = int(ship_id)
        except (ValueError, TypeError):
            print(f"    REJECTED: invalid ship ID '{ship_id}'")
            rejected += 1
            continue

        # Validate ownership (also checks suspension)
        valid, account_number, error = folders.validate_ship_ownership(email, ship_id)

        if not valid:
            print(f"    REJECTED: {error}")
            folders.store_rejected(turn_str, email, ship_id, content, [error])
            rejected += 1
            continue

        # Store the orders
        stored_path = folders.store_incoming_orders(turn_str, email, ship_id, content)

        # Write to database
        game = conn.execute(
            "SELECT current_year, current_week FROM games WHERE game_id = ?",
            (args.game,)
        ).fetchone()
        player = conn.execute(
            "SELECT player_id FROM players WHERE email = ? AND game_id = ?",
            (email, args.game)
        ).fetchone()

        # Clear previous orders for this ship/turn
        conn.execute("""
            DELETE FROM turn_orders
            WHERE game_id = ? AND turn_year = ? AND turn_week = ?
              AND subject_type = 'ship' AND subject_id = ?
        """, (args.game, game['current_year'], game['current_week'], ship_id))

        # Insert new orders
        for seq, order in enumerate(orders, 1):
            params = json.dumps(order.get('params', {})) if order.get('params') else None
            conn.execute("""
                INSERT INTO turn_orders
                    (game_id, turn_year, turn_week, player_id,
                     subject_type, subject_id, order_sequence, command, parameters)
                VALUES (?, ?, ?, ?, 'ship', ?, ?, ?, ?)
            """, (args.game, game['current_year'], game['current_week'],
                  player['player_id'], ship_id, seq,
                  order['command'], params))

        conn.commit()

        # Store receipt
        receipt_info = {
            'status': 'accepted',
            'order_count': len(orders),
        }
        folders.store_receipt(turn_str, email, ship_id, receipt_info)

        ship_name = conn.execute(
            "SELECT name FROM ships WHERE ship_id = ?", (ship_id,)
        ).fetchone()
        name_str = f" ({ship_name['name']})" if ship_name else ""
        print(f"    ACCEPTED: {len(orders)} orders for ship {ship_id}{name_str}")
        accepted += 1

        # Move processed file to avoid re-processing
        if not args.keep:
            processed_dir = inbox_dir / "_processed"
            processed_dir.mkdir(exist_ok=True)
            dest = processed_dir / f"{email}_{filepath.name}"
            filepath.rename(dest)

    conn.close()

    print(f"\n  Summary: {accepted} accepted, {rejected} rejected, {skipped} skipped")
    print(f"  Run 'turn-status' to see who is still outstanding.")


def main():
    parser = argparse.ArgumentParser(
        description="Stellar Dominion - PBEM Strategy Game Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Typical workflow:
  1. setup-game --demo           Create game with sample players
  2. list-ships --game OMICRON101   Find ship IDs and player emails
  3. submit-orders <file> --email alice@example.com
  4. turn-status                 Check who has submitted
  5. run-turn --game OMICRON101     Resolve and generate reports
  6. advance-turn                Move to next week

Player management:
  suspend-player --email alice@example.com   Suspend a player
  reinstate-player --account 12345678        Reinstate a player
  list-players --all                         Show all players inc. suspended

Batch processing:
  process-inbox --inbox /path/to/inbox       Process all orders from inbox
        """
    )
    parser.add_argument('--db', help='Database file path', default=None)

    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # --- setup-game ---
    sp = subparsers.add_parser('setup-game', help='Create a new game')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')
    sp.add_argument('--name', help='Game name')
    sp.add_argument('--demo', action='store_true', help='Create demo game with 2 players')

    # --- add-player ---
    sp = subparsers.add_parser('add-player', help='Add a player (GM command)')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')
    sp.add_argument('--name', required=True, help='Player name')
    sp.add_argument('--email', required=True, help='Player email')
    sp.add_argument('--political', help='Political position name')
    sp.add_argument('--ship-name', help='Starting ship name')
    sp.add_argument('--start-col', default='I', help='Starting grid column')
    sp.add_argument('--start-row', type=int, default=6, help='Starting grid row')

    # --- join-game ---
    sp = subparsers.add_parser('join-game', help='Interactive new player registration')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')

    # --- submit-orders ---
    sp = subparsers.add_parser('submit-orders', help='Submit orders from file')
    sp.add_argument('orders_file', help='Path to orders file (YAML or text)')
    sp.add_argument('--email', required=True,
                    help='Submitter email (validates ownership)')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')
    sp.add_argument('--dry-run', action='store_true',
                    help='File orders but do not store in database')

    # --- run-turn ---
    sp = subparsers.add_parser('run-turn', help='Resolve turn and generate reports')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')
    sp.add_argument('--ship', type=int, help='Specific ship ID (or resolve all)')
    sp.add_argument('--verbose', '-v', action='store_true',
                    help='Print reports to console')

    # --- turn-status ---
    sp = subparsers.add_parser('turn-status', help='Show incoming/processed status')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')
    sp.add_argument('--turn', help='Turn string (default: current turn)')

    # --- show-map ---
    sp = subparsers.add_parser('show-map', help='Display system map')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')
    sp.add_argument('--system', type=int, default=101, help='System ID')

    # --- show-status ---
    sp = subparsers.add_parser('show-status', help='Show ship/position status')
    sp.add_argument('--ship', type=int, help='Ship ID')

    # --- list-ships ---
    sp = subparsers.add_parser('list-ships', help='List all ships')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')
    sp.add_argument('--all', action='store_true', help='Include suspended players\' ships')

    # --- advance-turn ---
    sp = subparsers.add_parser('advance-turn', help='Advance to next turn')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')

    # --- edit-credits ---
    sp = subparsers.add_parser('edit-credits', help='Set credits for a political position')
    sp.add_argument('--political', type=int, required=True, help='Political position ID')
    sp.add_argument('--amount', type=float, required=True, help='Credit amount')

    # --- suspend-player ---
    sp = subparsers.add_parser('suspend-player', help='Suspend a player account')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')
    sp.add_argument('--account', help='Player account number')
    sp.add_argument('--email', help='Player email')

    # --- reinstate-player ---
    sp = subparsers.add_parser('reinstate-player', help='Reinstate a suspended player')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')
    sp.add_argument('--account', help='Player account number')
    sp.add_argument('--email', help='Player email')

    # --- list-players ---
    sp = subparsers.add_parser('list-players', help='List all players')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')
    sp.add_argument('--all', action='store_true', help='Include suspended players')

    # --- process-inbox ---
    sp = subparsers.add_parser('process-inbox', help='Batch process orders from inbox directory')
    sp.add_argument('--inbox', required=True, help='Path to inbox directory')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')
    sp.add_argument('--keep', action='store_true',
                    help='Keep processed files in place (default: move to _processed)')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    commands = {
        'setup-game': cmd_setup_game,
        'add-player': cmd_add_player,
        'join-game': cmd_join_game,
        'submit-orders': cmd_submit_orders,
        'run-turn': cmd_run_turn,
        'turn-status': cmd_turn_status,
        'show-map': cmd_show_map,
        'show-status': cmd_show_status,
        'list-ships': cmd_list_ships,
        'advance-turn': cmd_advance_turn,
        'edit-credits': cmd_edit_credits,
        'suspend-player': cmd_suspend_player,
        'reinstate-player': cmd_reinstate_player,
        'list-players': cmd_list_players,
        'process-inbox': cmd_process_inbox,
    }

    if args.command in commands:
        # Auto-migrate database schema if DB exists
        db_path = Path(args.db) if args.db else None
        try:
            migrate_db(db_path)
        except Exception:
            pass  # DB may not exist yet (e.g. setup-game)
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
