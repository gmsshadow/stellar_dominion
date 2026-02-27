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

from db.database import init_db, get_connection, get_faction, faction_display_name, backup_state, split_legacy_db, migrate_db
from db.universe_admin import add_system, add_body, add_link, add_trade_good, list_universe
from engine.game_setup import create_game, add_player, setup_demo_game, join_game, suspend_player, reinstate_player, list_players
from engine.orders.parser import parse_orders_file, parse_yaml_orders, parse_text_orders
from engine.registration import parse_registration_file, validate_registration
from engine.order_processor import (
    detect_content_type, process_single_order, process_single_registration,
    format_received_ack, format_reply_text,
)
from engine.resolution.resolver import TurnResolver
from engine.reports.report_gen import generate_ship_report, generate_prefect_report
from engine.reports.pdf_export import report_file_to_pdf, is_available as pdf_available
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
        prefect_name=args.prefect or f"Commander {args.name}",
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

    account = orders.get('account', '')
    if not account:
        print("Error: orders file must specify an account number.")
        print("  Add 'account: YOUR_ACCOUNT_NUMBER' to your orders file.")
        return

    raw_content = filepath.read_text(encoding='utf-8')

    # Validate ownership: does this email + account own this ship?
    valid, account_number, error_msg = folders.validate_ship_ownership(email, ship_id, account)

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
    1. Gather stored orders from database for all ships
    2. Resolve interleaved by TU cost (cheapest actions first across all ships)
    3. Generate ship and prefect reports
    4. Store reports in processed/{turn}/{account_number}/
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
               JOIN prefects pp ON s.owner_prefect_id = pp.prefect_id
               JOIN players p ON pp.player_id = p.player_id
               WHERE s.game_id = ? AND p.status = 'active'""",
            (args.game,)
        ).fetchall()

    print(f"=== Resolving Turn {turn_str} for game {args.game} ===\n")

    # Phase 1: Gather all ships' orders
    ship_orders_map = {}
    ship_meta = {}  # display_name, account_number per ship

    for ship in ships:
        ship_id = ship['ship_id']
        prefect_id = ship['owner_prefect_id']

        prefect = conn.execute(
            "SELECT faction_id FROM prefects WHERE prefect_id = ?",
            (prefect_id,)
        ).fetchone()
        faction = get_faction(conn, prefect['faction_id']) if prefect else {'abbreviation': 'IND'}
        display_name = f"{faction['abbreviation']} {ship['name']}"
        account_number = folders.get_account_for_prefect(prefect_id)

        stored_orders = conn.execute("""
            SELECT * FROM turn_orders 
            WHERE game_id = ? AND turn_year = ? AND turn_week = ?
            AND subject_type = 'ship' AND subject_id = ? AND status = 'pending'
            ORDER BY order_sequence
        """, (args.game, game['current_year'], game['current_week'], ship_id)).fetchall()

        if not stored_orders:
            print(f"  {display_name} ({ship_id}): No orders this turn.")
            continue

        order_list = []
        for so in stored_orders:
            params = json.loads(so['parameters']) if so['parameters'] else None
            order_list.append({
                'sequence': so['order_sequence'],
                'command': so['command'],
                'params': params,
            })

        ship_orders_map[ship_id] = order_list
        ship_meta[ship_id] = {
            'display_name': display_name,
            'account_number': account_number,
        }
        print(f"  {display_name} ({ship_id}): {len(order_list)} orders queued")

    # Phase 2: Interleaved resolution (cheapest TU actions first)
    if ship_orders_map:
        print(f"\n  Resolving {len(ship_orders_map)} ships interleaved by TU cost...")
        results = resolver.resolve_turn_interleaved(ship_orders_map)
    else:
        results = {}

    # Phase 3: Generate reports and mark orders resolved
    # Also collect trade summaries per prefect for financial report
    trade_by_prefect = {}  # {prefect_id: {ship_id: {'income': N, 'expenses': N, 'trades': [...]}}}

    for ship_id, result in results.items():
        meta = ship_meta[ship_id]
        display_name = meta['display_name']
        account_number = meta['account_number']

        if result.get('error'):
            print(f"    Error for {display_name}: {result['error']}")
            continue

        # Mark stored orders as resolved
        conn.execute("""
            UPDATE turn_orders SET status = 'resolved'
            WHERE game_id = ? AND turn_year = ? AND turn_week = ?
            AND subject_type = 'ship' AND subject_id = ? AND status = 'pending'
        """, (args.game, game['current_year'], game['current_week'], ship_id))
        conn.commit()

        # Collect trade data from this ship's action log
        ship_row = conn.execute("SELECT owner_prefect_id FROM ships WHERE ship_id = ?",
                                (ship_id,)).fetchone()
        if ship_row:
            pid = ship_row['owner_prefect_id']
            if pid not in trade_by_prefect:
                trade_by_prefect[pid] = {}
            ship_income = 0
            ship_expenses = 0
            ship_trades = []
            for action in result.get('log', []):
                if action.get('command') == 'BUY' and action.get('success'):
                    spent = action.get('credits_spent', 0)
                    ship_expenses += spent
                    ship_trades.append({
                        'type': 'BUY',
                        'item': action.get('item_name', '?'),
                        'qty': action.get('quantity', 0),
                        'credits': spent,
                    })
                elif action.get('command') == 'SELL' and action.get('success'):
                    earned = action.get('credits_earned', 0)
                    ship_income += earned
                    ship_trades.append({
                        'type': 'SELL',
                        'item': action.get('item_name', '?'),
                        'qty': action.get('quantity', 0),
                        'credits': earned,
                    })
            trade_by_prefect[pid][ship_id] = {
                'income': ship_income,
                'expenses': ship_expenses,
                'trades': ship_trades,
            }

        # Generate ship report
        report = generate_ship_report(result, db_path, args.game)
        report_file = folders.store_ship_report(turn_str, account_number, ship_id, report)
        print(f"    Ship report:      {report_file}")

        # Generate PDF version
        if pdf_available():
            pdf_path = report_file_to_pdf(report_file)
            if pdf_path:
                print(f"    Ship PDF:         {pdf_path}")

        if args.verbose:
            print()
            print(report)
            print()

    # Close the main connection before generating reports
    # (ensures fresh reads see all committed trade/movement changes)
    conn.close()

    # Generate one prefect report per active prefect
    conn = get_connection(db_path)
    all_prefects = conn.execute("""
        SELECT DISTINCT pp.prefect_id, p.email, p.account_number
        FROM prefects pp
        JOIN players p ON pp.player_id = p.player_id
        WHERE pp.game_id = ? AND p.status = 'active'
    """, (args.game,)).fetchall()

    print()
    for pol in all_prefects:
        prefect_id = pol['prefect_id']
        account_number = pol['account_number']
        ship_trades = trade_by_prefect.get(prefect_id, {})
        prefect_report = generate_prefect_report(
            prefect_id, db_path, args.game,
            trade_summary=ship_trades
        )
        pol_file = folders.store_prefect_report(turn_str, account_number, prefect_id, prefect_report)
        email = pol['email']
        print(f"  Account {account_number} -> {email}")
        print(f"    Prefect report: {pol_file}")

        # Generate PDF version
        if pdf_available():
            pdf_path = report_file_to_pdf(pol_file)
            if pdf_path:
                print(f"    Prefect PDF:    {pdf_path}")

        # Show total files to send
        player_reports = folders.get_player_reports(turn_str, account_number)
        if len(player_reports) > 1:
            print(f"    Total files to email: {len(player_reports)}")

    resolver.close()
    conn.close()
    print(f"\n=== Turn {turn_str} resolution complete ===")

    # Backup game state after successful turn
    state_db = Path(args.db) if args.db else None
    backup_path = backup_state(turn_label=turn_str, state_db_path=state_db)
    if backup_path:
        print(f"  State backed up to: {backup_path.name}")

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
# SEND TURNS
# ======================================================================

def cmd_send_turns(args):
    """
    Email processed turn reports to all players via Gmail.

    For each active player, collects all report files from the processed
    folder (ship reports, prefect reports) and sends them as attachments
    in a single email.

    Requires Gmail API credentials (same as fetch-mail).
    """
    db_path = Path(args.db) if args.db else None
    folders = TurnFolders(db_path=db_path, game_id=args.game)

    # Determine which turn to send
    if args.turn:
        turn_str = args.turn
    else:
        turn_str = folders.get_current_turn_str()

    # Get all processed reports for the turn
    processed = folders.list_processed(turn_str)
    if not processed:
        print(f"No processed reports found for turn {turn_str}.")
        return

    print(f"=== Send Turn Reports - Game {args.game}, Turn {turn_str} ===\n")

    # Build the send list
    send_list = []
    for account_number, report_files in processed.items():
        email = folders.get_email_for_account(account_number)
        if not email:
            print(f"  [{account_number}] WARNING: no email found, skipping")
            continue
        send_list.append((account_number, email, report_files))

    if not send_list:
        print("No players to send to.")
        return

    # Show what will be sent
    print(f"  Reports to send: {len(send_list)} players\n")
    for account_number, email, report_files in send_list:
        file_list = ', '.join(f.name for f in report_files)
        print(f"  {account_number} -> {email}")
        print(f"    Files: {file_list}")
    print()

    if args.dry_run:
        print("  (Dry run - no emails sent)")
        return

    # Only check Gmail deps when we're actually going to send
    from engine.gmail import check_dependencies
    ok, error_msg = check_dependencies()
    if not ok:
        print(f"Error: {error_msg}")
        return

    from engine.gmail import get_gmail_service, send_with_attachments

    if not args.credentials:
        print("Error: --credentials is required when sending (omit --dry-run, or provide credentials).")
        return

    credentials_path = Path(args.credentials)
    token_path = Path(args.token)

    if not credentials_path.exists():
        print(f"Error: credentials file '{credentials_path}' not found.")
        return

    # Connect to Gmail
    print("Connecting to Gmail...")
    try:
        service = get_gmail_service(credentials_path, token_path, port=args.port)
    except Exception as e:
        print(f"Error connecting to Gmail: {e}")
        return

    sent = 0
    errors = 0

    for account_number, email, report_files in send_list:
        # Build subject and body
        subject = f"Stellar Dominion - {args.game} Turn {turn_str} Reports"

        body_lines = [
            f"Stellar Dominion - Turn Reports",
            f"=" * 32,
            f"",
            f"Game:   {args.game}",
            f"Turn:   {turn_str}",
            f"",
            f"Your turn reports are attached ({len(report_files)} file{'s' if len(report_files) != 1 else ''}):",
            f"",
        ]
        for f in report_files:
            body_lines.append(f"  - {f.name}")
        body_lines.append("")
        body_lines.append("-- Stellar Dominion Game Engine")
        body_text = "\n".join(body_lines)

        try:
            msg_id = send_with_attachments(
                service, email, subject, body_text, report_files
            )
            print(f"  SENT to {email} ({len(report_files)} files) [{msg_id}]")
            sent += 1
        except Exception as e:
            print(f"  ERROR sending to {email}: {e}")
            errors += 1

    print(f"\n  Summary: {sent} sent, {errors} errors")


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
        print(f"        Prefect: {player['prefect_name']} ({player['prefect_id']})")
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
        "SELECT * FROM star_systems WHERE system_id = ?",
        (args.system or 101,)
    ).fetchone()

    if not system:
        print("System not found.")
        conn.close()
        return

    system_id = system['system_id']

    # Only celestial bodies go on the grid (no ships, no bases)
    objects = []
    bodies = conn.execute(
        "SELECT * FROM celestial_bodies WHERE system_id = ? ORDER BY grid_col, grid_row",
        (system_id,)
    ).fetchall()
    for b in bodies:
        objects.append({'type': b['body_type'], 'col': b['grid_col'], 'row': b['grid_row'],
                        'symbol': b['map_symbol'], 'name': b['name']})

    system_data = {
        'star_col': system['star_grid_col'],
        'star_row': system['star_grid_row']
    }

    title = f"{system['name']} System ({system_id})"
    print(f"\n{title}")
    print("=" * len(title))
    print(render_system_map(system_data, objects))

    # Structured legend: star, then celestial bodies with nested bases
    print(f"\nCelestial Bodies:")
    print(f"  *  {system['star_name']} ({system['star_spectral_type']}) at "
          f"{system['star_grid_col']}{system['star_grid_row']:02d}")

    # Get all bases indexed by orbiting_body_id
    bases = conn.execute(
        "SELECT * FROM starbases WHERE system_id = ? AND game_id = ?",
        (system_id, args.game)
    ).fetchall()
    bases_by_body = {}
    for b in bases:
        body_id = b['orbiting_body_id']
        if body_id not in bases_by_body:
            bases_by_body[body_id] = []
        bases_by_body[body_id].append(b)

    for b in bodies:
        loc = f"{b['grid_col']}{b['grid_row']:02d}"
        indent = "  " if not b['parent_body_id'] else "      "
        type_label = b['body_type'].replace('_', ' ').title()
        print(f"{indent}{b['map_symbol']}  {b['name']} ({b['body_id']}) at {loc} - {type_label}")

        # Show bases orbiting this body
        if b['body_id'] in bases_by_body:
            for base in bases_by_body[b['body_id']]:
                base_loc = f"{base['grid_col']}{base['grid_row']:02d}"
                print(f"{indent}     [{base['base_type']}] {base['name']} ({base['base_id']}) at {base_loc}"
                      f" - Docking: {base['docking_capacity']}")

    conn.close()


def cmd_show_status(args):
    """Show status of a ship."""
    db_path = Path(args.db) if args.db else None
    conn = get_connection(db_path)

    if args.ship:
        ship = conn.execute(
            "SELECT s.*, ss.name as system_name, pp.faction_id FROM ships s "
            "JOIN star_systems ss ON s.system_id = ss.system_id "
            "JOIN prefects pp ON s.owner_prefect_id = pp.prefect_id "
            "WHERE s.ship_id = ?", (args.ship,)
        ).fetchone()
        if ship:
            display_name = faction_display_name(conn, ship['name'], ship['faction_id'])
            loc = f"{ship['grid_col']}{ship['grid_row']:02d}"
            dock_info = f" [Docked at {ship['docked_at_base_id']}]" if ship['docked_at_base_id'] else ""
            orbit_info = f" [Orbiting {ship['orbiting_body_id']}]" if ship['orbiting_body_id'] else ""
            faction = get_faction(conn, ship['faction_id'])
            print(f"Ship: {display_name} ({ship['ship_id']})")
            print(f"  Faction: {faction['abbreviation']} - {faction['name']}")
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
        SELECT s.*, ss.name as system_name, pp.name as owner_name, pp.faction_id,
               p.email, p.account_number, p.status as player_status
        FROM ships s 
        JOIN star_systems ss ON s.system_id = ss.system_id
        JOIN prefects pp ON s.owner_prefect_id = pp.prefect_id
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
        print(f"{'ID':<12} {'Name':<24} {'Owner':<18} {'Account':<12} {'Location':<10} {'TU':<10} {'Status':<10}")
        print("-" * 96)
        for s in ships:
            faction = get_faction(conn, s['faction_id'])
            display_name = f"{faction['abbreviation']} {s['name']}"
            loc = f"{s['grid_col']}{s['grid_row']:02d}"
            dock = f" [D]" if s['docked_at_base_id'] else ""
            status = "SUSPENDED" if s['player_status'] == 'suspended' else ""
            print(f"{s['ship_id']:<12} {display_name:<24} {s['owner_name']:<18} "
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
    """Set credits for a prefect."""
    db_path = Path(args.db) if args.db else None
    conn = get_connection(db_path)

    prefect = conn.execute(
        "SELECT * FROM prefects WHERE prefect_id = ?",
        (args.prefect,)
    ).fetchone()
    if prefect:
        conn.execute(
            "UPDATE prefects SET credits = ? WHERE prefect_id = ?",
            (args.amount, args.prefect)
        )
        conn.commit()
        print(f"Credits for {prefect['name']} ({args.prefect}) set to {args.amount:,.0f}")
    else:
        print(f"Prefect position {args.prefect} not found.")

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


# ======================================================================
# REGISTRATION FORM COMMANDS
# ======================================================================

def cmd_generate_form(args):
    """
    Generate a blank registration form for new players.
    Lists available planets so players can choose their starting location (in orbit).
    Outputs both YAML and text formats to the specified directory.
    """
    db_path = Path(args.db) if args.db else None
    conn = get_connection(db_path)

    game = conn.execute("SELECT * FROM games WHERE game_id = ?", (args.game,)).fetchone()
    if not game:
        print(f"Error: Game {args.game} not found.")
        conn.close()
        return

    # Get available planets (starting locations)
    planets = conn.execute(
        "SELECT cb.*, ss.name as system_name "
        "FROM celestial_bodies cb "
        "JOIN star_systems ss ON cb.system_id = ss.system_id "
        "WHERE ss.game_id = ? AND cb.body_type = 'planet' "
        "ORDER BY cb.name",
        (args.game,)
    ).fetchall()

    turn_str = f"{game['current_year']}.{game['current_week']}"
    conn.close()

    # Build planet listing
    planet_lines_yaml = []
    planet_lines_text = []
    for p in planets:
        loc = f"{p['grid_col']}{p['grid_row']:02d}"
        type_label = p['body_type'].replace('_', ' ').title()
        desc = f"{p['name']} ({p['body_id']}) - {type_label} at {loc} - {p['system_name']} ({p['system_id']})"
        planet_lines_yaml.append(f"#   {desc}")
        planet_lines_text.append(f"#   {desc}")

    # Generate YAML form
    yaml_form = f"""# ============================================================
# STELLAR DOMINION - New Player Registration Form
# Game: {game['game_name']} ({args.game})
# Current Turn: {turn_str}
# ============================================================
#
# Fill in the fields below and return this file to the GM.
#
# AVAILABLE STARTING PLANETS:
{chr(10).join(planet_lines_yaml)}
#
# Choose one planet body ID from the list above for your starting
# location. Your ship will begin the game in orbit around it.
# ============================================================

game: {args.game}
player_name: 
email: 
prefect_name: 
ship_name: 
planet: 
"""

    # Generate text form
    text_form = f"""# ============================================================
# STELLAR DOMINION - New Player Registration Form
# Game: {game['game_name']} ({args.game})
# Current Turn: {turn_str}
# ============================================================
#
# Fill in the fields below and return this file to the GM.
# Each field must be on its own line: FIELD_NAME value
#
# AVAILABLE STARTING PLANETS:
{chr(10).join(planet_lines_text)}
#
# Choose one planet body ID from the list above for your starting
# location. Your ship will begin the game in orbit around it.
# ============================================================

GAME {args.game}
PLAYER_NAME 
EMAIL 
PREFECT_NAME 
SHIP_NAME 
PLANET 
"""

    # Write forms to output directory
    output_dir = Path(args.output) if args.output else Path('.')
    output_dir.mkdir(parents=True, exist_ok=True)

    yaml_file = output_dir / f"registration_{args.game}.yaml"
    text_file = output_dir / f"registration_{args.game}.txt"

    yaml_file.write_text(yaml_form, encoding='utf-8')
    text_file.write_text(text_form, encoding='utf-8')

    print(f"Registration forms generated for game {args.game}:")
    print(f"  YAML: {yaml_file}")
    print(f"  Text: {text_file}")
    print(f"\nAvailable starting planets:")
    for p in planets:
        loc = f"{p['grid_col']}{p['grid_row']:02d}"
        type_label = p['body_type'].replace('_', ' ').title()
        print(f"  {p['name']} ({p['body_id']}) - {type_label} at {loc} - {p['system_name']} ({p['system_id']})")


def cmd_register_player(args):
    """
    Process a filled-in registration form and create the player account.
    Validates all fields, creates player/prefect/ship, starts in orbit at chosen planet,
    and generates welcome reports.
    """
    db_path = Path(args.db) if args.db else None

    # Parse the form
    filepath = Path(args.form)
    if not filepath.exists():
        print(f"Error: File '{filepath}' not found.")
        return

    data = parse_registration_file(filepath)

    if data.get('error'):
        print(f"Parse error: {data['error']}")
        return

    # Validate required fields
    errors = validate_registration(data)
    if errors:
        print("Registration REJECTED - missing or invalid fields:")
        for e in errors:
            print(f"  - {e}")
        return

    game_id = data['game']
    conn = get_connection(db_path)

    # Verify game exists
    game = conn.execute("SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()
    if not game:
        print(f"Error: Game '{game_id}' not found.")
        conn.close()
        return

    # Check email not already registered
    existing = conn.execute(
        "SELECT player_name FROM players WHERE email = ? AND game_id = ?",
        (data['email'], game_id)
    ).fetchone()
    if existing:
        print(f"Error: Email '{data['email']}' is already registered to {existing['player_name']}.")
        conn.close()
        return

    # Verify planet exists (and belongs to this game)
    try:
        planet_id = int(data['planet'])
    except (ValueError, TypeError):
        print(f"Error: planet must be a numeric body ID (got '{data.get('planet', '')}').")
        conn.close()
        return
    planet = conn.execute(
        "SELECT cb.*, ss.name as system_name FROM celestial_bodies cb "
        "JOIN star_systems ss ON cb.system_id = ss.system_id "
        "WHERE cb.body_id = ? AND ss.game_id = ?",
        (planet_id, game_id)
    ).fetchone()
    if not planet:
        print(f"Error: Planet/body {planet_id} not found in game {game_id}.")
        conn.close()
        return

    conn.close()

    # Create the player via add_player
    print(f"\nProcessing registration for: {data['player_name']} ({data['email']})")
    print(f"  Starting in orbit of: {planet['name']} ({planet_id}) in {planet['system_name']} System")
    print()

    result = add_player(
        db_path=db_path,
        game_id=game_id,
        player_name=data['player_name'],
        email=data['email'],
        prefect_name=data['prefect_name'],
        ship_name=data['ship_name'],
        start_orbit_body=planet_id,
    )

    if result:
        print(f"\nRegistration complete. Send the welcome reports to {data['email']}.")


def cmd_process_inbox(args):
    """
    Process all submissions from an inbox directory.

    Auto-detects each file as player orders or a registration form:
      - Orders: validates ownership, files into turn folders, stores in DB
      - Registration: validates fields, creates player/prefect/ship, generates welcome reports

    Expected directory structure (created by fetch-mail or manually):
        inbox/
          alice@example.com/
            msg_abc123.yaml          (from fetch-mail)
            orders_12345678.yaml     (manually placed)
            registration.yaml        (manually placed)
          bob@example.com/
            msg_def456.txt
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

    # Collect all submission files from email subdirectories
    submission_files = []

    for item in sorted(inbox_dir.iterdir()):
        if item.name.startswith(('_', '.')):
            continue
        if item.is_dir() and '@' in item.name:
            email = item.name
            for f in sorted(item.iterdir()):
                if f.suffix in ('.yaml', '.yml', '.txt') and not f.name.startswith('.'):
                    submission_files.append((email, f))

    if not submission_files:
        print("No submission files found in inbox.")
        conn.close()
        return

    orders_accepted = 0
    orders_rejected = 0
    registrations = 0
    reg_rejected = 0
    skipped = 0

    for email, filepath in submission_files:
        print(f"  Processing: {email} / {filepath.name}")

        try:
            content = filepath.read_text(encoding='utf-8')
        except Exception as e:
            print(f"    ERROR: could not read file: {e}")
            skipped += 1
            continue

        content_type = detect_content_type(content)

        if content_type == 'orders':
            result = process_single_order(conn, folders, turn_str, args.game, email, content)

            if result['status'] == 'accepted':
                name_str = f" ({result['ship_name']})" if result['ship_name'] else ""
                print(f"    ORDERS ACCEPTED: {result['order_count']} orders for ship {result['ship_id']}{name_str}")
                orders_accepted += 1
            elif result['status'] == 'rejected':
                print(f"    ORDERS REJECTED: {result['error']}")
                orders_rejected += 1
            else:
                print(f"    ORDERS SKIP: {result['error']}")
                skipped += 1

        elif content_type == 'registration':
            result = process_single_registration(db_path, args.game, email, content)

            if result['status'] == 'registered':
                print(f"    REGISTERED: {result['player_name']} - ship '{result['ship_name']}'"
                      f" at {result['planet_name']}")
                print(f"      Account: {result['account_number']}  ** Send to player **")
                registrations += 1
            else:
                print(f"    REGISTRATION REJECTED: {result['error']}")
                reg_rejected += 1

        else:
            print(f"    SKIP: could not identify as orders or registration")
            skipped += 1
            continue

        # Move processed file
        if not args.keep:
            processed_dir = inbox_dir / "_processed"
            processed_dir.mkdir(exist_ok=True)
            dest = processed_dir / f"{email}__{filepath.name}"
            filepath.rename(dest)

    conn.close()

    print(f"\n  Summary:")
    print(f"    Orders:        {orders_accepted} accepted, {orders_rejected} rejected")
    print(f"    Registrations: {registrations} created, {reg_rejected} rejected")
    print(f"    Skipped:       {skipped}")
    if orders_accepted > 0:
        print(f"  Run 'turn-status' to see who is still outstanding.")
    if registrations > 0:
        print(f"  Welcome reports have been generated for new players.")


# ======================================================================
# GMAIL FETCH
# ======================================================================

def cmd_fetch_mail(args):
    """
    Fetch submissions from Gmail and save to a staging inbox directory.

    Stage 1 of the two-stage workflow:
    1. Connects to Gmail via OAuth
    2. Searches for emails matching the orders label
    3. For each email: extracts sender and text content
    4. Saves to inbox/{email}/msg_{gmail_id}.txt
    5. Optionally sends a 'received' acknowledgement reply
    6. Relabels the Gmail message (orders -> processed)

    Does NOT validate orders or interact with the game database.
    Run 'process-inbox' afterwards to validate and file.

    Gmail labels provide exactly-once fetch: even if a message is
    later marked unread, it won't be re-fetched unless the orders
    label is reapplied.
    """
    # Check Gmail dependencies
    from engine.gmail import check_dependencies
    ok, error_msg = check_dependencies()
    if not ok:
        print(f"Error: {error_msg}")
        return

    from email import message_from_bytes
    from engine.gmail import (
        get_gmail_service, ensure_label, fetch_candidate_message_ids,
        read_message_raw, extract_email_address, find_orders_text,
        apply_post_process_labels, get_message_metadata, send_reply,
    )

    credentials_path = Path(args.credentials)
    token_path = Path(args.token)
    inbox_dir = Path(args.inbox)

    if not credentials_path.exists():
        print(f"Error: credentials file '{credentials_path}' not found.")
        print("Download OAuth client secrets from Google Cloud Console.")
        return

    # Connect to Gmail
    print(f"Connecting to Gmail...")
    try:
        service = get_gmail_service(credentials_path, token_path, port=args.port)
    except Exception as e:
        print(f"Error connecting to Gmail: {e}")
        return

    # Ensure labels exist
    orders_label_id = ensure_label(service, args.orders_label)
    processed_label_id = ensure_label(service, args.processed_label)

    query = args.query or f'label:"{args.orders_label}" is:unread'
    print(f"Searching: {query}")

    msg_ids = fetch_candidate_message_ids(service, query, max_results=args.max_results)
    print(f"Found {len(msg_ids)} messages\n")

    if not msg_ids:
        print("No new mail to fetch.")
        return

    inbox_dir.mkdir(parents=True, exist_ok=True)

    fetched = 0
    skipped = 0
    gmail_errors = 0

    for msg_id in msg_ids:
        # Fetch the raw email
        try:
            raw_bytes, _full = read_message_raw(service, msg_id)
        except Exception as e:
            print(f"  [{msg_id}] ERROR: could not fetch: {e}")
            gmail_errors += 1
            continue

        eml = message_from_bytes(raw_bytes)
        from_email = extract_email_address(eml.get("From", ""))
        subject = (eml.get("Subject") or "").strip()

        # Extract text content
        text = find_orders_text(eml)
        if not text:
            print(f"  [{msg_id}] SKIP from {from_email} - no text content (subj: '{subject}')")
            skipped += 1
            # Still relabel to avoid re-fetching
            if not args.dry_run:
                try:
                    apply_post_process_labels(
                        service, msg_id,
                        remove_label_ids=[orders_label_id],
                        add_label_ids=[processed_label_id],
                    )
                except Exception:
                    pass
            continue

        # Save to inbox/{email}/msg_{gmail_id}.txt
        email_dir = inbox_dir / from_email
        email_dir.mkdir(parents=True, exist_ok=True)
        out_path = email_dir / f"msg_{msg_id}.txt"
        out_path.write_text(text.strip() + "\n", encoding="utf-8")

        print(f"  [{msg_id}] from {from_email} -> {out_path.name}")
        fetched += 1

        # Send 'received' acknowledgement
        if args.reply and not args.dry_run:
            try:
                metadata = get_message_metadata(service, msg_id)
                ack_body = format_received_ack(args.game)
                send_reply(service, metadata, ack_body)
                print(f"    ACK sent to {from_email}")
            except Exception as e:
                print(f"    WARNING: could not send ack: {e}")
                gmail_errors += 1

        # Relabel in Gmail
        if not args.dry_run:
            try:
                apply_post_process_labels(
                    service, msg_id,
                    remove_label_ids=[orders_label_id],
                    add_label_ids=[processed_label_id],
                )
            except Exception as e:
                print(f"    WARNING: could not relabel: {e}")
                gmail_errors += 1

    print(f"\n  Summary: {fetched} fetched, {skipped} skipped, {gmail_errors} gmail errors")
    print(f"  Inbox: {inbox_dir.resolve()}")
    if args.dry_run:
        print("  (Dry run - Gmail labels not modified, no acks sent)")
    elif args.reply:
        print("  (Acknowledgement replies sent to players)")
    print(f"\n  Next step: python pbem.py process-inbox --inbox {inbox_dir} --game {args.game}")


# ======================================================================
# UNIVERSE MANAGEMENT COMMANDS
# ======================================================================

def cmd_split_db(args):
    """Split a legacy single-file database into universe.db + game_state.db."""
    legacy_path = Path(args.legacy_db)
    split_legacy_db(legacy_path)


def cmd_add_system(args):
    """Add a new star system to the universe."""
    from db.universe_admin import add_system

    # Get current turn for provenance from game state
    created_turn = None
    if not args.no_turn_stamp:
        try:
            state_path = Path(args.db) if args.db else None
            conn = get_connection(state_path)
            game = conn.execute("SELECT * FROM games LIMIT 1").fetchone()
            if game:
                created_turn = f"{game['current_year']}.{game['current_week']}"
            conn.close()
        except Exception:
            pass

    add_system(
        system_id=args.system_id,
        name=args.name,
        star_name=args.star_name,
        spectral_type=args.spectral_type or 'G2V',
        star_col=args.star_col or 'M',
        star_row=args.star_row or 13,
        created_turn=created_turn,
    )


def cmd_add_body(args):
    """Add a celestial body to a system."""
    from db.universe_admin import add_body

    created_turn = None
    if not args.no_turn_stamp:
        try:
            state_path = Path(args.db) if args.db else None
            conn = get_connection(state_path)
            game = conn.execute("SELECT * FROM games LIMIT 1").fetchone()
            if game:
                created_turn = f"{game['current_year']}.{game['current_week']}"
            conn.close()
        except Exception:
            pass

    add_body(
        body_id=args.body_id,
        system_id=args.system_id,
        name=args.name,
        body_type=args.body_type or 'planet',
        parent_body_id=args.parent,
        grid_col=args.col,
        grid_row=args.row,
        gravity=args.gravity or 1.0,
        temperature=args.temperature or 300,
        atmosphere=args.atmosphere or 'Standard',
        tectonic_activity=args.tectonic or 0,
        hydrosphere=args.hydrosphere or 0,
        life=args.life or 'None',
        surface_size=args.surface_size,
        resource_id=args.resource_id,
        created_turn=created_turn,
    )


def cmd_add_link(args):
    """Add a hyperspace link between two systems."""
    from db.universe_admin import add_link

    created_turn = None
    if not args.no_turn_stamp:
        try:
            state_path = Path(args.db) if args.db else None
            conn = get_connection(state_path)
            game = conn.execute("SELECT * FROM games LIMIT 1").fetchone()
            if game:
                created_turn = f"{game['current_year']}.{game['current_week']}"
            conn.close()
        except Exception:
            pass

    add_link(
        system_a=args.system_a,
        system_b=args.system_b,
        known_by_default=1 if args.known else 0,
        created_turn=created_turn,
    )


def cmd_list_universe(args):
    """Show all universe content."""
    from db.universe_admin import list_universe
    list_universe()


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
  process-inbox --inbox /path/to/inbox       Process orders + registrations from inbox

Gmail integration (two-stage workflow):
  fetch-mail --credentials creds.json        Fetch from Gmail -> staging inbox
  process-inbox --inbox ./inbox              Validate and file all submissions
  send-turns --credentials creds.json        Email turn reports to players
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
    sp.add_argument('--prefect', help='Prefect position name')
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
    sp = subparsers.add_parser('edit-credits', help='Set credits for a prefect')
    sp.add_argument('--prefect', type=int, required=True, help='Prefect position ID')
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

    # --- generate-form ---
    sp = subparsers.add_parser('generate-form', help='Generate blank registration form for new players')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')
    sp.add_argument('--output', default='.', help='Output directory for form files')

    # --- register-player ---
    sp = subparsers.add_parser('register-player', help='Process a filled-in registration form')
    sp.add_argument('form', help='Path to the filled registration form')

    # --- process-inbox ---
    sp = subparsers.add_parser('process-inbox', help='Batch process orders from inbox directory')
    sp.add_argument('--inbox', required=True, help='Path to inbox directory')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')
    sp.add_argument('--keep', action='store_true',
                    help='Keep processed files in place (default: move to _processed)')

    # --- fetch-mail ---
    sp = subparsers.add_parser('fetch-mail',
                               help='Fetch submissions from Gmail to staging inbox')
    sp.add_argument('--credentials', required=True,
                    help='Path to OAuth client secrets JSON (credentials.json)')
    sp.add_argument('--token', default='./token.json',
                    help='Token cache path (default: ./token.json)')
    sp.add_argument('--game', default='OMICRON101', help='Game ID (for ack replies)')
    sp.add_argument('--inbox', default='./inbox',
                    help='Staging inbox directory (default: ./inbox)')
    sp.add_argument('--orders-label', default='sd-orders',
                    help='Gmail label for incoming submissions (default: sd-orders)')
    sp.add_argument('--processed-label', default='sd-processed',
                    help='Gmail label for fetched messages (default: sd-processed)')
    sp.add_argument('--query', default=None,
                    help='Override Gmail search query')
    sp.add_argument('--max-results', type=int, default=25,
                    help='Max messages to fetch per run (default: 25)')
    sp.add_argument('--port', type=int, default=0,
                    help='OAuth local server port (0 = auto)')
    sp.add_argument('--dry-run', action='store_true',
                    help='Fetch and save but do not modify Gmail or send acks')
    sp.add_argument('--reply', action='store_true',
                    help='Send received acknowledgement reply to each sender')

    # --- send-turns ---
    sp = subparsers.add_parser('send-turns',
                               help='Email processed turn reports to players via Gmail')
    sp.add_argument('--credentials', default=None,
                    help='Path to OAuth client secrets JSON (required unless --dry-run)')
    sp.add_argument('--token', default='./token.json',
                    help='Token cache path (default: ./token.json)')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')
    sp.add_argument('--turn', default=None,
                    help='Turn to send (default: current turn)')
    sp.add_argument('--port', type=int, default=0,
                    help='OAuth local server port (0 = auto)')
    sp.add_argument('--dry-run', action='store_true',
                    help='Show what would be sent without sending')

    # --- split-db ---
    sp = subparsers.add_parser('split-db', help='Split legacy single DB into universe.db + game_state.db')
    sp.add_argument('legacy_db', help='Path to the legacy stellar_dominion.db file')

    # --- add-system ---
    sp = subparsers.add_parser('add-system', help='Add a star system to the universe')
    sp.add_argument('--name', required=True, help='System name')
    sp.add_argument('--system-id', type=int, default=None, help='System ID (auto-assigned if omitted)')
    sp.add_argument('--star-name', default=None, help='Star name (defaults to "<name> Prime")')
    sp.add_argument('--spectral-type', default='G2V', help='Spectral type (default: G2V)')
    sp.add_argument('--star-col', default='M', help='Star grid column (default: M)')
    sp.add_argument('--star-row', type=int, default=13, help='Star grid row (default: 13)')
    sp.add_argument('--no-turn-stamp', action='store_true', help='Skip created_turn provenance')

    # --- add-body ---
    sp = subparsers.add_parser('add-body', help='Add a celestial body to a system')
    sp.add_argument('--name', required=True, help='Body name')
    sp.add_argument('--system-id', type=int, required=True, help='System to add body to')
    sp.add_argument('--body-id', type=int, default=None, help='Body ID (auto-assigned if omitted)')
    sp.add_argument('--body-type', default='planet', choices=['planet', 'moon', 'gas_giant', 'asteroid'])
    sp.add_argument('--parent', type=int, default=None, help='Parent body ID (for moons)')
    sp.add_argument('--col', required=True, help='Grid column (e.g. H)')
    sp.add_argument('--row', type=int, required=True, help='Grid row (e.g. 4)')
    sp.add_argument('--gravity', type=float, default=1.0, help='Surface gravity')
    sp.add_argument('--temperature', type=int, default=300, help='Surface temperature (K)')
    sp.add_argument('--atmosphere', default='Standard', help='Atmosphere type')
    sp.add_argument('--tectonic', type=int, default=0, help='Tectonic activity (0-10)')
    sp.add_argument('--hydrosphere', type=int, default=0, help='Hydrosphere percentage (0-100)')
    sp.add_argument('--life', default='None', help='Life level (None/Microbial/Plant/Animal/Sentient)')
    sp.add_argument('--surface-size', type=int, default=None,
                    help='Surface grid size 5-50 (default: planet=31, moon=15, gas_giant=50, asteroid=11)')
    sp.add_argument('--resource-id', type=int, default=None,
                    help='Trade good ID that can be gathered from this body (e.g. 100101)')
    sp.add_argument('--no-turn-stamp', action='store_true', help='Skip created_turn provenance')

    # --- add-link ---
    sp = subparsers.add_parser('add-link', help='Add a hyperspace link between two systems')
    sp.add_argument('system_a', type=int, help='First system ID')
    sp.add_argument('system_b', type=int, help='Second system ID')
    sp.add_argument('--known', action='store_true', help='Link is known by default (visible to all)')
    sp.add_argument('--no-turn-stamp', action='store_true', help='Skip created_turn provenance')

    # --- list-universe ---
    sp = subparsers.add_parser('list-universe', help='Show all universe content (systems, bodies, links)')

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
        'generate-form': cmd_generate_form,
        'register-player': cmd_register_player,
        'process-inbox': cmd_process_inbox,
        'fetch-mail': cmd_fetch_mail,
        'send-turns': cmd_send_turns,
        'split-db': cmd_split_db,
        'add-system': cmd_add_system,
        'add-body': cmd_add_body,
        'add-link': cmd_add_link,
        'list-universe': cmd_list_universe,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
