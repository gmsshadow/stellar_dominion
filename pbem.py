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

    # Check turn_status - reject orders if turn is held or processing
    conn_check = get_connection(db_path)
    game_check = conn_check.execute(
        "SELECT turn_status FROM games WHERE game_id = ?", (args.game,)
    ).fetchone()
    if game_check and game_check['turn_status'] in ('held', 'processing', 'completed'):
        print(f"Error: Turn is currently {game_check['turn_status']}. Orders cannot be submitted.")
        print(f"  Ask the GM to release the turn or advance to the next turn.")
        conn_check.close()
        return
    conn_check.close()

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

    # Check turn_status - block if held (unless --force)
    force = hasattr(args, 'force') and args.force
    if game['turn_status'] == 'held' and not force:
        print(f"Error: Turn is HELD by GM. Cannot process.")
        print(f"  Use 'release-turn' first, or 'run-turn --force' to override.")
        conn.close()
        return
    if game['turn_status'] == 'completed' and not force:
        print(f"Error: Turn already completed. Use 'advance-turn' to move to next turn.")
        conn.close()
        return

    # Mark turn as processing
    conn.execute(
        "UPDATE games SET turn_status = 'processing' WHERE game_id = ?",
        (args.game,)
    )
    conn.commit()

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

        # Load new orders from this turn
        stored_orders = conn.execute("""
            SELECT * FROM turn_orders 
            WHERE game_id = ? AND turn_year = ? AND turn_week = ?
            AND subject_type = 'ship' AND subject_id = ? AND status = 'pending'
            ORDER BY order_sequence
        """, (args.game, game['current_year'], game['current_week'], ship_id)).fetchall()

        new_orders = []
        for so in stored_orders:
            params = json.loads(so['parameters']) if so['parameters'] else None
            new_orders.append({
                'sequence': so['order_sequence'],
                'command': so['command'],
                'params': params,
            })

        # Load overflow orders from previous turns
        overflow_rows = conn.execute("""
            SELECT * FROM pending_orders
            WHERE game_id = ? AND subject_type = 'ship' AND subject_id = ?
            ORDER BY order_sequence
        """, (args.game, ship_id)).fetchall()

        overflow_orders = []
        for ov in overflow_rows:
            params = json.loads(ov['parameters']) if ov['parameters'] else None
            overflow_orders.append({
                'command': ov['command'],
                'params': params,
            })

        # Check if new orders contain CLEAR — if so, discard overflow
        has_clear = any(o['command'] == 'CLEAR' for o in new_orders)
        if has_clear and overflow_orders:
            print(f"  {display_name} ({ship_id}): CLEAR — discarding {len(overflow_orders)} overflow orders from last turn")
            overflow_orders = []

        # Delete stored overflow regardless (will be re-saved after resolution)
        conn.execute("""
            DELETE FROM pending_orders
            WHERE game_id = ? AND subject_type = 'ship' AND subject_id = ?
        """, (args.game, ship_id))
        conn.commit()

        # Merge: overflow first, then new orders
        combined = overflow_orders + new_orders

        if not combined:
            print(f"  {display_name} ({ship_id}): No orders this turn.")
            continue

        parts = []
        if overflow_orders:
            parts.append(f"{len(overflow_orders)} overflow")
        if new_orders:
            parts.append(f"{len(new_orders)} new")
        print(f"  {display_name} ({ship_id}): {' + '.join(parts)} = {len(combined)} orders queued")

        ship_orders_map[ship_id] = combined
        ship_meta[ship_id] = {
            'display_name': display_name,
            'account_number': account_number,
        }

    # Phase 1.1: Check for MODERATOR orders — auto-hold if pending
    mod_orders_found = []
    for sid, orders in ship_orders_map.items():
        ship_row = conn.execute("SELECT name, owner_prefect_id FROM ships WHERE ship_id = ?", (sid,)).fetchone()
        for order in orders:
            if order['command'] == 'MODERATOR':
                mod_orders_found.append({
                    'ship_id': sid,
                    'ship_name': ship_row['name'],
                    'prefect_id': ship_row['owner_prefect_id'],
                    'text': order['params']['text'],
                })

    if mod_orders_found:
        # Create moderator_actions records for any that don't already exist
        for mo in mod_orders_found:
            existing = conn.execute("""
                SELECT action_id FROM moderator_actions
                WHERE game_id = ? AND ship_id = ? AND request_text = ?
                AND requested_turn_year = ? AND requested_turn_week = ?
            """, (args.game, mo['ship_id'], mo['text'],
                  game['current_year'], game['current_week'])).fetchone()
            if not existing:
                conn.execute("""
                    INSERT INTO moderator_actions
                    (game_id, ship_id, prefect_id, request_text, status,
                     requested_turn_year, requested_turn_week)
                    VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """, (args.game, mo['ship_id'], mo['prefect_id'], mo['text'],
                      game['current_year'], game['current_week']))
        conn.commit()

        # Check for any still-pending (unresponded) moderator actions this turn
        pending_actions = conn.execute("""
            SELECT ma.action_id, ma.request_text, s.name as ship_name, p.name as prefect_name
            FROM moderator_actions ma
            JOIN ships s ON ma.ship_id = s.ship_id
            JOIN prefects p ON ma.prefect_id = p.prefect_id
            WHERE ma.game_id = ? AND ma.requested_turn_year = ? AND ma.requested_turn_week = ?
            AND ma.status = 'pending'
        """, (args.game, game['current_year'], game['current_week'])).fetchall()

        if pending_actions:
            # Auto-hold the turn
            conn.execute(
                "UPDATE games SET turn_status = 'held' WHERE game_id = ?",
                (args.game,)
            )
            conn.commit()
            print(f"\n  *** TURN AUTO-HELD: {len(pending_actions)} moderator action(s) require GM response ***")
            for pa in pending_actions:
                print(f"    #{pa['action_id']}: {pa['prefect_name']}/{pa['ship_name']}: \"{pa['request_text']}\"")
            print(f"\n  Use 'list-actions --game {args.game}' to review.")
            print(f"  Use 'respond-action --action-id N --response \"...\"' to respond.")
            print(f"  Then 'release-turn --game {args.game}' and 'run-turn --game {args.game}' to continue.")
            resolver.close()
            conn.close()
            return

    # Phase 1.5: Crew wage deduction
    # Regular crew: 1 cr/week, Officers: 5 cr/week
    wage_messages = {}  # {ship_id: [msg_lines]}
    all_ships = conn.execute(
        "SELECT s.*, p.credits as prefect_credits FROM ships s "
        "JOIN prefects p ON s.owner_prefect_id = p.prefect_id "
        "WHERE s.game_id = ?", (args.game,)
    ).fetchall()

    wage_by_prefect = {}  # {prefect_id: total_wages}
    for ship in all_ships:
        # Count officers and their wages
        officers = conn.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(wages), 0) as total_wages "
            "FROM officers WHERE ship_id = ?",
            (ship['ship_id'],)
        ).fetchone()
        officer_count = officers['cnt']
        officer_wages = int(officers['total_wages'])

        # Count cargo crew
        cargo_crew_row = conn.execute(
            "SELECT COALESCE(SUM(quantity), 0) as cnt FROM cargo_items "
            "WHERE ship_id = ? AND item_type_id = 401",
            (ship['ship_id'],)
        ).fetchone()
        cargo_crew = cargo_crew_row['cnt']

        crew_count = cargo_crew + officer_count
        crew_required = ship['crew_required'] if ship['crew_required'] else 1
        efficiency = min(100.0, (crew_count / max(1, crew_required)) * 100.0)

        # Update crew_count and efficiency in DB
        conn.execute(
            "UPDATE ships SET crew_count = ?, efficiency = ? WHERE ship_id = ?",
            (crew_count, efficiency, ship['ship_id'])
        )

        crew_wages = cargo_crew * 1  # 1 cr per regular crew
        total_wages = crew_wages + officer_wages
        if total_wages <= 0:
            continue

        prefect_id = ship['owner_prefect_id']
        if prefect_id not in wage_by_prefect:
            wage_by_prefect[prefect_id] = 0
        wage_by_prefect[prefect_id] += total_wages

        msgs = []
        parts = []
        if cargo_crew > 0:
            parts.append(f"{cargo_crew} crew x 1 cr = {crew_wages} cr")
        if officer_count > 0:
            parts.append(f"{officer_count} officer{'s' if officer_count != 1 else ''} x 5 cr = {officer_wages} cr")
        msgs.append(f"Wages: {' + '.join(parts)} = {total_wages} cr deducted.")
        if crew_count < ship['crew_required']:
            msgs.append(
                f"  WARNING: Ship undermanned! {crew_count}/{ship['crew_required']} crew. "
                f"Efficiency: {efficiency:.0f}% (+{100 - efficiency:.0f}% TU penalty)."
            )
        life_support = ship['life_support_capacity'] if ship['life_support_capacity'] else 20
        msgs.append(f"  Life support: {crew_count}/{life_support} capacity.")
        wage_messages[ship['ship_id']] = msgs

    # Deduct wages per prefect (aggregate)
    for prefect_id, total_wages in wage_by_prefect.items():
        conn.execute(
            "UPDATE prefects SET credits = credits - ? WHERE prefect_id = ?",
            (total_wages, prefect_id)
        )
    conn.commit()

    # Phase 1.6: Process approved faction change requests
    faction_messages = {}  # {prefect_id: [msg_lines]}
    approved_requests = conn.execute("""
        SELECT fr.*, cf.abbreviation as old_abbr, cf.name as old_name,
               tf.abbreviation as new_abbr, tf.name as new_name
        FROM faction_requests fr
        LEFT JOIN factions cf ON fr.current_faction_id = cf.faction_id
        LEFT JOIN factions tf ON fr.target_faction_id = tf.faction_id
        WHERE fr.game_id = ? AND fr.status = 'approved'
    """, (args.game,)).fetchall()

    for req in approved_requests:
        # Apply the faction change
        conn.execute(
            "UPDATE prefects SET faction_id = ? WHERE prefect_id = ?",
            (req['target_faction_id'], req['prefect_id'])
        )
        # Mark request as processed
        conn.execute("""
            UPDATE faction_requests
            SET status = 'completed',
                processed_turn_year = ?, processed_turn_week = ?
            WHERE request_id = ?
        """, (game['current_year'], game['current_week'], req['request_id']))

        old = req['old_abbr'] or 'IND'
        new = req['new_abbr'] or 'IND'
        msgs = [
            f"FACTION CHANGE APPROVED: {old} -> {new} ({req['new_name']})",
        ]
        if req['gm_note']:
            msgs.append(f"  GM note: \"{req['gm_note']}\"")
        msgs.append(f"  All ships now fly under the {req['new_abbr']} banner.")

        pid = req['prefect_id']
        if pid not in faction_messages:
            faction_messages[pid] = []
        faction_messages[pid].extend(msgs)

    # Also notify denied requests
    denied_requests = conn.execute("""
        SELECT fr.*, tf.abbreviation as target_abbr, tf.name as target_name
        FROM faction_requests fr
        LEFT JOIN factions tf ON fr.target_faction_id = tf.faction_id
        WHERE fr.game_id = ? AND fr.status = 'denied'
        AND fr.processed_turn_year IS NULL
    """, (args.game,)).fetchall()

    for req in denied_requests:
        conn.execute("""
            UPDATE faction_requests
            SET processed_turn_year = ?, processed_turn_week = ?
            WHERE request_id = ?
        """, (game['current_year'], game['current_week'], req['request_id']))

        msgs = [
            f"FACTION CHANGE DENIED: request to join {req['target_abbr']} ({req['target_name']}) was denied.",
        ]
        if req['gm_note']:
            msgs.append(f"  GM note: \"{req['gm_note']}\"")

        pid = req['prefect_id']
        if pid not in faction_messages:
            faction_messages[pid] = []
        faction_messages[pid].extend(msgs)

    if approved_requests or denied_requests:
        conn.commit()
        print(f"  Faction changes: {len(approved_requests)} approved, {len(denied_requests)} denied")

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

        # Save overflow orders (TU exhaustion carry-forward) to pending_orders
        overflow = result.get('overflow', [])
        if overflow:
            for seq, ov_order in enumerate(overflow, 1):
                params_json = json.dumps(ov_order['params']) if ov_order['params'] else None
                conn.execute("""
                    INSERT INTO pending_orders
                    (game_id, subject_type, subject_id, order_sequence, command, parameters, reason)
                    VALUES (?, 'ship', ?, ?, ?, ?, 'TU overflow')
                """, (args.game, ship_id, seq, ov_order['command'], params_json))
            print(f"    {display_name}: {len(overflow)} orders carry forward to next turn")

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
        # Query undelivered messages for this ship
        incoming_msgs = conn.execute("""
            SELECT * FROM messages
            WHERE game_id = ? AND recipient_type = 'ship' AND recipient_id = ? AND delivered = 0
            ORDER BY message_id
        """, (args.game, ship_id)).fetchall()

        between_msgs = None
        # Start with wage messages for this ship
        ship_wage_msgs = wage_messages.get(ship_id, [])
        if incoming_msgs or ship_wage_msgs:
            between_msgs = []
            # Wage info first
            if ship_wage_msgs:
                between_msgs.extend(ship_wage_msgs)
                between_msgs.append("")
            # Then player messages
            for m in incoming_msgs:
                between_msgs.append(
                    f"Message from {m['sender_name']} ({m['sender_id']}) "
                    f"[{m['sent_turn_year']}.{m['sent_turn_week']}]:"
                )
                between_msgs.append(f"  \"{m['message_text']}\"")
                between_msgs.append("")
            # Mark as delivered
            conn.execute("""
                UPDATE messages SET delivered = 1
                WHERE game_id = ? AND recipient_type = 'ship' AND recipient_id = ? AND delivered = 0
            """, (args.game, ship_id))
            conn.commit()

        report = generate_ship_report(result, db_path, args.game,
                                      between_turn_messages=between_msgs)
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

        # Query undelivered messages for this prefect
        incoming_msgs = conn.execute("""
            SELECT * FROM messages
            WHERE game_id = ? AND recipient_type = 'prefect' AND recipient_id = ? AND delivered = 0
            ORDER BY message_id
        """, (args.game, prefect_id)).fetchall()

        # Also include messages sent to bases owned by this prefect
        base_msgs = conn.execute("""
            SELECT m.* FROM messages m
            JOIN starbases b ON m.recipient_id = b.base_id AND m.recipient_type = 'base'
            WHERE m.game_id = ? AND b.owner_prefect_id = ? AND m.delivered = 0
            ORDER BY m.message_id
        """, (args.game, prefect_id)).fetchall()

        all_prefect_msgs = list(incoming_msgs) + list(base_msgs)

        # Build prefect between-turn messages: wages + faction changes + player messages
        prefect_wage_total = wage_by_prefect.get(prefect_id, 0)
        prefect_faction_msgs = faction_messages.get(prefect_id, [])
        has_content = all_prefect_msgs or prefect_wage_total > 0 or prefect_faction_msgs

        prefect_between_msgs = None
        if has_content:
            prefect_between_msgs = []

            # Faction change notifications (most important, show first)
            if prefect_faction_msgs:
                for fm in prefect_faction_msgs:
                    prefect_between_msgs.append(fm)
                prefect_between_msgs.append("")

            # Wage summary for all ships
            if prefect_wage_total > 0:
                prefect_ships = conn.execute(
                    "SELECT ship_id, name, crew_count, crew_required FROM ships "
                    "WHERE owner_prefect_id = ? AND game_id = ?",
                    (prefect_id, args.game)
                ).fetchall()
                prefect_between_msgs.append(f"Wage deductions: {prefect_wage_total} cr total")
                for ps in prefect_ships:
                    # Get officer count and cargo crew separately
                    off = conn.execute(
                        "SELECT COUNT(*) as cnt, COALESCE(SUM(wages),0) as ow "
                        "FROM officers WHERE ship_id = ?", (ps['ship_id'],)
                    ).fetchone()
                    cc_row = conn.execute(
                        "SELECT COALESCE(SUM(quantity),0) as cnt FROM cargo_items "
                        "WHERE ship_id = ? AND item_type_id = 401", (ps['ship_id'],)
                    ).fetchone()
                    o_cnt = off['cnt']
                    o_wages = int(off['ow'])
                    c_cnt = cc_row['cnt']
                    c_wages = c_cnt * 1
                    sw = c_wages + o_wages
                    if sw <= 0:
                        continue
                    status = ""
                    if ps['crew_count'] < ps['crew_required']:
                        status = " [UNDERMANNED]"
                    detail = f"{c_cnt} crew + {o_cnt} officers" if o_cnt else f"{c_cnt} crew"
                    prefect_between_msgs.append(
                        f"  {ps['name']} ({ps['ship_id']}): "
                        f"{detail}, {sw} cr{status}"
                    )
                prefect_between_msgs.append("")

            for m in all_prefect_msgs:
                dest = ""
                if m['recipient_type'] == 'base':
                    dest = f" (via base {m['recipient_id']})"
                prefect_between_msgs.append(
                    f"Message from {m['sender_name']} ({m['sender_id']}) "
                    f"[{m['sent_turn_year']}.{m['sent_turn_week']}]{dest}:"
                )
                prefect_between_msgs.append(f"  \"{m['message_text']}\"")
                prefect_between_msgs.append("")
            # Mark all as delivered
            msg_ids = [m['message_id'] for m in all_prefect_msgs]
            placeholders = ','.join('?' * len(msg_ids))
            conn.execute(f"UPDATE messages SET delivered = 1 WHERE message_id IN ({placeholders})", msg_ids)
            conn.commit()

        prefect_report = generate_prefect_report(
            prefect_id, db_path, args.game,
            between_turn_messages=prefect_between_msgs,
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

    # Mark turn as completed
    conn2 = get_connection(db_path)
    conn2.execute(
        "UPDATE games SET turn_status = 'completed' WHERE game_id = ?",
        (args.game,)
    )
    conn2.commit()
    conn2.close()

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

    # Show pipeline status
    db_path = Path(args.db) if args.db else None
    conn_p = get_connection(db_path)
    game_p = conn_p.execute("SELECT turn_status FROM games WHERE game_id = ?", (args.game,)).fetchone()
    if game_p:
        status = game_p['turn_status'] if 'turn_status' in game_p.keys() else 'open'
        labels = {'open': 'OPEN', 'held': 'HELD', 'processing': 'PROCESSING', 'completed': 'COMPLETED'}
        print(f"  Pipeline: {labels.get(status, status.upper())}")
        print()
    conn_p.close()

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

    # Build lookup of moons by parent body_id
    children_by_parent = {}
    top_level = []
    for b in bodies:
        if b['parent_body_id']:
            children_by_parent.setdefault(b['parent_body_id'], []).append(b)
        else:
            top_level.append(b)

    def print_body(b, indent):
        loc = f"{b['grid_col']}{b['grid_row']:02d}"
        type_label = b['body_type'].replace('_', ' ').title()
        print(f"{indent}{b['map_symbol']}  {b['name']} ({b['body_id']}) at {loc} - {type_label}")
        if b['body_id'] in bases_by_body:
            for base in bases_by_body[b['body_id']]:
                base_loc = f"{base['grid_col']}{base['grid_row']:02d}"
                print(f"{indent}     [{base['base_type']}] {base['name']} ({base['base_id']}) at {base_loc}"
                      f" - Docking: {base['docking_capacity']}")
        for child in children_by_parent.get(b['body_id'], []):
            print_body(child, "      ")

    for b in top_level:
        print_body(b, "  ")

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
    # Reset turn status to open for new turn
    conn.execute(
        "UPDATE games SET turn_status = 'open' WHERE game_id = ?",
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
        "WHERE cb.body_type = 'planet' "
        "ORDER BY cb.name"
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
        "WHERE cb.body_id = ?",
        (planet_id,)
    ).fetchone()
    if not planet:
        print(f"Error: Planet/body {planet_id} not found.")
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


def cmd_add_port(args):
    """Add a surface port to a planet."""
    conn = get_connection(Path(args.db) if args.db else None)

    # Verify the body exists
    body = conn.execute(
        "SELECT * FROM celestial_bodies WHERE body_id = ?", (args.body_id,)
    ).fetchone()
    if not body:
        print(f"Error: Body {args.body_id} not found in universe.")
        conn.close()
        return

    # Verify parent base exists if specified
    if args.parent_base:
        base = conn.execute(
            "SELECT * FROM starbases WHERE base_id = ?", (args.parent_base,)
        ).fetchone()
        if not base:
            print(f"Error: Starbase {args.parent_base} not found.")
            conn.close()
            return

    # Auto-detect game_id
    game = conn.execute("SELECT game_id FROM games LIMIT 1").fetchone()
    game_id = game['game_id'] if game else 'DEFAULT'

    conn.execute("""
        INSERT INTO surface_ports
        (port_id, game_id, name, body_id, surface_x, surface_y,
         parent_base_id, complexes, workers, troops)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (args.port_id, game_id, args.name, args.body_id,
          args.x, args.y, args.parent_base,
          args.complexes or 0, args.workers or 0, args.troops or 0))
    conn.commit()

    print(f"Surface Port '{args.name}' ({args.port_id}) added:")
    print(f"  Body: {body['name']} ({args.body_id})")
    print(f"  Position: ({args.x},{args.y})")
    if args.parent_base:
        print(f"  Parent Starbase: {args.parent_base}")
    conn.close()


def cmd_add_outpost(args):
    """Add an outpost to a planet or moon."""
    conn = get_connection(Path(args.db) if args.db else None)

    body = conn.execute(
        "SELECT * FROM celestial_bodies WHERE body_id = ?", (args.body_id,)
    ).fetchone()
    if not body:
        print(f"Error: Body {args.body_id} not found in universe.")
        conn.close()
        return

    game = conn.execute("SELECT game_id FROM games LIMIT 1").fetchone()
    game_id = game['game_id'] if game else 'DEFAULT'

    conn.execute("""
        INSERT INTO outposts
        (outpost_id, game_id, name, body_id, surface_x, surface_y,
         outpost_type, workers)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (args.outpost_id, game_id, args.name, args.body_id,
          args.x, args.y, args.type or 'General', args.workers or 0))
    conn.commit()

    print(f"Outpost '{args.name}' ({args.outpost_id}) added:")
    print(f"  Body: {body['name']} ({args.body_id})")
    print(f"  Position: ({args.x},{args.y})")
    print(f"  Type: {args.type or 'General'}")
    conn.close()


def cmd_list_universe(args):
    """Show all universe content."""
    from db.universe_admin import list_universe
    list_universe()


def cmd_list_factions(args):
    """List all available factions."""
    from db.database import get_universe_connection
    conn = get_universe_connection()
    factions = conn.execute("SELECT * FROM factions ORDER BY faction_id").fetchall()
    print(f"\nAvailable Factions:")
    print(f"{'ID':<6} {'Abbr':<6} {'Name':<30} Description")
    print("-" * 80)
    for f in factions:
        print(f"{f['faction_id']:<6} {f['abbreviation']:<6} {f['name']:<30} {f['description']}")
    conn.close()


def cmd_faction_requests(args):
    """List faction change requests."""
    from db.database import get_connection
    conn = get_connection()

    status_filter = args.status if hasattr(args, 'status') and args.status else 'pending'
    if status_filter == 'all':
        requests = conn.execute("""
            SELECT fr.*, p.name as prefect_name, pl.player_name,
                   cf.abbreviation as current_abbr, cf.name as current_name,
                   tf.abbreviation as target_abbr, tf.name as target_name
            FROM faction_requests fr
            JOIN prefects p ON fr.prefect_id = p.prefect_id
            JOIN players pl ON p.player_id = pl.player_id
            LEFT JOIN factions cf ON fr.current_faction_id = cf.faction_id
            LEFT JOIN factions tf ON fr.target_faction_id = tf.faction_id
            WHERE fr.game_id = ?
            ORDER BY fr.request_id
        """, (args.game,)).fetchall()
    else:
        requests = conn.execute("""
            SELECT fr.*, p.name as prefect_name, pl.player_name,
                   cf.abbreviation as current_abbr, cf.name as current_name,
                   tf.abbreviation as target_abbr, tf.name as target_name
            FROM faction_requests fr
            JOIN prefects p ON fr.prefect_id = p.prefect_id
            JOIN players pl ON p.player_id = pl.player_id
            LEFT JOIN factions cf ON fr.current_faction_id = cf.faction_id
            LEFT JOIN factions tf ON fr.target_faction_id = tf.faction_id
            WHERE fr.game_id = ? AND fr.status = ?
            ORDER BY fr.request_id
        """, (args.game, status_filter)).fetchall()

    print(f"\nFaction Change Requests ({status_filter}):")
    if not requests:
        print("  No requests found.")
    else:
        for r in requests:
            cur = r['current_abbr'] or 'IND'
            tgt = r['target_abbr'] or '???'
            print(f"  #{r['request_id']}: {r['player_name']}/{r['prefect_name']} "
                  f"({r['prefect_id']}): {cur} -> {tgt} ({r['target_name']})")
            print(f"    Requested: {r['requested_turn_year']}.{r['requested_turn_week']}  "
                  f"Status: {r['status'].upper()}")
            if r['reason']:
                print(f"    Reason: \"{r['reason']}\"")
            if r['gm_note']:
                print(f"    GM Note: \"{r['gm_note']}\"")
    conn.close()


def cmd_approve_faction(args):
    """Approve a faction change request."""
    from db.database import get_connection
    conn = get_connection()

    request = conn.execute(
        "SELECT * FROM faction_requests WHERE request_id = ? AND game_id = ?",
        (args.request_id, args.game)
    ).fetchone()
    if not request:
        print(f"Error: request #{args.request_id} not found.")
        return
    if request['status'] != 'pending':
        print(f"Error: request #{args.request_id} is already {request['status']}.")
        return

    note = args.note if hasattr(args, 'note') and args.note else ''
    conn.execute("""
        UPDATE faction_requests SET status = 'approved', gm_note = ?
        WHERE request_id = ?
    """, (note, args.request_id))
    conn.commit()

    prefect = conn.execute("SELECT name FROM prefects WHERE prefect_id = ?",
                            (request['prefect_id'],)).fetchone()
    target = conn.execute("SELECT abbreviation, name FROM factions WHERE faction_id = ?",
                           (request['target_faction_id'],)).fetchone()
    print(f"Approved: {prefect['name']} -> {target['abbreviation']} ({target['name']})")
    print(f"  Change will take effect at next turn processing.")
    conn.close()


def cmd_deny_faction(args):
    """Deny a faction change request."""
    from db.database import get_connection
    conn = get_connection()

    request = conn.execute(
        "SELECT * FROM faction_requests WHERE request_id = ? AND game_id = ?",
        (args.request_id, args.game)
    ).fetchone()
    if not request:
        print(f"Error: request #{args.request_id} not found.")
        return
    if request['status'] != 'pending':
        print(f"Error: request #{args.request_id} is already {request['status']}.")
        return

    note = args.note if hasattr(args, 'note') and args.note else ''
    conn.execute("""
        UPDATE faction_requests SET status = 'denied', gm_note = ?
        WHERE request_id = ?
    """, (note, args.request_id))
    conn.commit()

    prefect = conn.execute("SELECT name FROM prefects WHERE prefect_id = ?",
                            (request['prefect_id'],)).fetchone()
    target = conn.execute("SELECT abbreviation FROM factions WHERE faction_id = ?",
                           (request['target_faction_id'],)).fetchone()
    print(f"Denied: {prefect['name']} -> {target['abbreviation']}")
    if note:
        print(f"  Note: {note}")
    conn.close()


# ======================================================================
# MODERATOR / GM ACTIONS
# ======================================================================

def cmd_turn_pipeline(args):
    """Show the current turn pipeline status."""
    db_path = Path(args.db) if args.db else None
    conn = get_connection(db_path)
    game = conn.execute("SELECT * FROM games WHERE game_id = ?", (args.game,)).fetchone()
    if not game:
        print(f"Error: Game {args.game} not found.")
        conn.close()
        return

    turn_str = f"{game['current_year']}.{game['current_week']}"
    status = game['turn_status'] if 'turn_status' in game.keys() else 'open'

    status_display = {
        'open': 'OPEN - accepting orders',
        'held': 'HELD - orders locked, awaiting GM review/release',
        'processing': 'PROCESSING - turn is being resolved',
        'completed': 'COMPLETED - turn resolved, ready to advance',
    }

    print(f"\n=== Turn Pipeline: {args.game} ===")
    print(f"  Turn:   {turn_str}")
    print(f"  Status: {status_display.get(status, status.upper())}")

    # Count orders
    order_count = conn.execute("""
        SELECT COUNT(*) as cnt FROM turn_orders
        WHERE game_id = ? AND turn_year = ? AND turn_week = ? AND status = 'pending'
    """, (args.game, game['current_year'], game['current_week'])).fetchone()
    overflow_count = conn.execute("""
        SELECT COUNT(*) as cnt FROM pending_orders WHERE game_id = ?
    """, (args.game,)).fetchone()

    print(f"  Orders: {order_count['cnt']} pending, {overflow_count['cnt']} overflow from previous turns")

    # Ships with/without orders
    ships = conn.execute("""
        SELECT s.ship_id, s.name, p.name as prefect_name, f.abbreviation as faction
        FROM ships s
        JOIN prefects p ON s.owner_prefect_id = p.prefect_id
        LEFT JOIN factions f ON p.faction_id = f.faction_id
        JOIN players pl ON p.player_id = pl.player_id
        WHERE s.game_id = ? AND pl.status = 'active'
    """, (args.game,)).fetchall()

    ships_with = []
    ships_without = []
    for s in ships:
        has = conn.execute("""
            SELECT COUNT(*) as cnt FROM turn_orders
            WHERE game_id = ? AND turn_year = ? AND turn_week = ?
            AND subject_type = 'ship' AND subject_id = ? AND status = 'pending'
        """, (args.game, game['current_year'], game['current_week'], s['ship_id'])).fetchone()
        if has['cnt'] > 0:
            ships_with.append(s)
        else:
            ships_without.append(s)

    print(f"\n  Ships with orders:    {len(ships_with)}")
    for s in ships_with:
        fac = s['faction'] or 'IND'
        print(f"    {fac} {s['name']} ({s['ship_id']}) - {s['prefect_name']}")
    print(f"  Ships without orders: {len(ships_without)}")
    for s in ships_without:
        fac = s['faction'] or 'IND'
        print(f"    {fac} {s['name']} ({s['ship_id']}) - {s['prefect_name']}")
    conn.close()


def cmd_hold_turn(args):
    """Hold the turn - lock orders, prevent run-turn."""
    db_path = Path(args.db) if args.db else None
    conn = get_connection(db_path)
    game = conn.execute("SELECT * FROM games WHERE game_id = ?", (args.game,)).fetchone()
    if not game:
        print(f"Error: Game {args.game} not found.")
        conn.close()
        return

    status = game['turn_status'] if 'turn_status' in game.keys() else 'open'
    if status == 'held':
        print(f"Turn is already held.")
        conn.close()
        return
    if status in ('processing', 'completed'):
        print(f"Cannot hold: turn is {status}.")
        conn.close()
        return

    conn.execute(
        "UPDATE games SET turn_status = 'held' WHERE game_id = ?",
        (args.game,)
    )
    conn.commit()
    turn_str = f"{game['current_year']}.{game['current_week']}"
    print(f"Turn {turn_str} is now HELD.")
    print(f"  Orders are locked — no new submissions accepted.")
    print(f"  Use 'review-orders' to inspect, then 'release-turn' when ready.")
    conn.close()


def cmd_release_turn(args):
    """Release a held turn - allow run-turn or reopen for orders."""
    db_path = Path(args.db) if args.db else None
    conn = get_connection(db_path)
    game = conn.execute("SELECT * FROM games WHERE game_id = ?", (args.game,)).fetchone()
    if not game:
        print(f"Error: Game {args.game} not found.")
        conn.close()
        return

    status = game['turn_status'] if 'turn_status' in game.keys() else 'open'
    if status == 'open':
        print(f"Turn is already open.")
        conn.close()
        return

    conn.execute(
        "UPDATE games SET turn_status = 'open' WHERE game_id = ?",
        (args.game,)
    )
    conn.commit()
    turn_str = f"{game['current_year']}.{game['current_week']}"
    print(f"Turn {turn_str} released — status: OPEN (was {status.upper()}).")
    print(f"  Ready for order submissions or 'run-turn'.")
    conn.close()


def cmd_review_orders(args):
    """Review all pending orders for the current turn."""
    db_path = Path(args.db) if args.db else None
    conn = get_connection(db_path)
    game = conn.execute("SELECT * FROM games WHERE game_id = ?", (args.game,)).fetchone()
    if not game:
        print(f"Error: Game {args.game} not found.")
        conn.close()
        return

    turn_str = f"{game['current_year']}.{game['current_week']}"

    # Get all orders grouped by ship
    ships = conn.execute("""
        SELECT DISTINCT s.ship_id, s.name, p.name as prefect_name, pl.player_name,
               f.abbreviation as faction
        FROM turn_orders o
        JOIN ships s ON o.subject_id = s.ship_id AND o.subject_type = 'ship'
        JOIN prefects p ON s.owner_prefect_id = p.prefect_id
        JOIN players pl ON p.player_id = pl.player_id
        LEFT JOIN factions f ON p.faction_id = f.faction_id
        WHERE o.game_id = ? AND o.turn_year = ? AND o.turn_week = ? AND o.status = 'pending'
    """, (args.game, game['current_year'], game['current_week'])).fetchall()

    print(f"\n=== Order Review: Turn {turn_str} ===\n")

    if not ships:
        print("  No pending orders.")
        # Check overflow
        overflow = conn.execute(
            "SELECT COUNT(*) as cnt FROM pending_orders WHERE game_id = ?",
            (args.game,)
        ).fetchone()
        if overflow['cnt'] > 0:
            print(f"  ({overflow['cnt']} overflow orders from previous turns)")
        conn.close()
        return

    for ship in ships:
        fac = ship['faction'] or 'IND'
        print(f"{fac} {ship['name']} ({ship['ship_id']}) - {ship['player_name']}/{ship['prefect_name']}")

        orders = conn.execute("""
            SELECT order_id, order_sequence, command, parameters FROM turn_orders
            WHERE game_id = ? AND turn_year = ? AND turn_week = ?
            AND subject_type = 'ship' AND subject_id = ? AND status = 'pending'
            ORDER BY order_sequence
        """, (args.game, game['current_year'], game['current_week'],
              ship['ship_id'])).fetchall()

        for o in orders:
            params_display = o['parameters'] if o['parameters'] else ''
            # Truncate long params
            if len(params_display) > 60:
                params_display = params_display[:57] + '...'
            print(f"  [{o['order_id']}] #{o['order_sequence']}: {o['command']} {params_display}")
        print()

    # Overflow orders
    overflow = conn.execute("""
        SELECT po.*, s.name as ship_name
        FROM pending_orders po
        JOIN ships s ON po.subject_id = s.ship_id
        WHERE po.game_id = ?
        ORDER BY po.subject_id, po.order_sequence
    """, (args.game,)).fetchall()
    if overflow:
        print(f"--- Overflow Orders (from previous turns) ---")
        for o in overflow:
            params_display = o['parameters'] if o['parameters'] else ''
            if len(params_display) > 60:
                params_display = params_display[:57] + '...'
            print(f"  {o['ship_name']} ({o['subject_id']}): #{o['order_sequence']}: {o['command']} {params_display}")

    conn.close()


def cmd_edit_order(args):
    """Edit an existing pending order."""
    db_path = Path(args.db) if args.db else None
    conn = get_connection(db_path)

    order = conn.execute(
        "SELECT * FROM turn_orders WHERE order_id = ? AND game_id = ? AND status = 'pending'",
        (args.order_id, args.game)
    ).fetchone()
    if not order:
        print(f"Error: order #{args.order_id} not found or not pending.")
        conn.close()
        return

    old_cmd = order['command']
    old_params = order['parameters']

    # Parse new command if provided
    if args.command_str:
        from engine.orders.parser import parse_single_order
        parts = args.command_str.strip().split(None, 1)
        new_cmd = parts[0].upper()
        new_params_raw = parts[1] if len(parts) > 1 else ''
        cmd, params, error = parse_single_order(new_cmd, new_params_raw)
        if error:
            print(f"Parse error: {error}")
            conn.close()
            return

        new_params_json = json.dumps(params) if params else None
        conn.execute("""
            UPDATE turn_orders SET command = ?, parameters = ?
            WHERE order_id = ?
        """, (cmd, new_params_json, args.order_id))
        conn.commit()
        print(f"Order #{args.order_id} updated:")
        print(f"  Was: {old_cmd} {old_params or ''}")
        print(f"  Now: {cmd} {new_params_json or ''}")
    else:
        print(f"Order #{args.order_id}: {old_cmd} {old_params or ''}")
        print(f"  Use --command to provide new command string")

    conn.close()


def cmd_delete_order(args):
    """Delete a pending order."""
    db_path = Path(args.db) if args.db else None
    conn = get_connection(db_path)

    order = conn.execute(
        "SELECT * FROM turn_orders WHERE order_id = ? AND game_id = ? AND status = 'pending'",
        (args.order_id, args.game)
    ).fetchone()
    if not order:
        print(f"Error: order #{args.order_id} not found or not pending.")
        conn.close()
        return

    conn.execute("DELETE FROM turn_orders WHERE order_id = ?", (args.order_id,))
    conn.commit()
    print(f"Deleted order #{args.order_id}: {order['command']} {order['parameters'] or ''}")
    print(f"  (Ship {order['subject_id']}, sequence #{order['order_sequence']})")
    conn.close()


def cmd_inject_order(args):
    """Inject a GM order for any ship."""
    db_path = Path(args.db) if args.db else None
    conn = get_connection(db_path)

    game = conn.execute("SELECT * FROM games WHERE game_id = ?", (args.game,)).fetchone()
    if not game:
        print(f"Error: Game {args.game} not found.")
        conn.close()
        return

    ship = conn.execute(
        "SELECT s.*, p.player_id FROM ships s "
        "JOIN prefects p ON s.owner_prefect_id = p.prefect_id "
        "WHERE s.ship_id = ? AND s.game_id = ?",
        (args.ship, args.game)
    ).fetchone()
    if not ship:
        print(f"Error: Ship {args.ship} not found.")
        conn.close()
        return

    from engine.orders.parser import parse_single_order
    parts = args.command_str.strip().split(None, 1)
    new_cmd = parts[0].upper()
    new_params_raw = parts[1] if len(parts) > 1 else ''
    cmd, params, error = parse_single_order(new_cmd, new_params_raw)
    if error:
        print(f"Parse error: {error}")
        conn.close()
        return

    # Find next sequence number
    max_seq = conn.execute("""
        SELECT MAX(order_sequence) as mx FROM turn_orders
        WHERE game_id = ? AND turn_year = ? AND turn_week = ?
        AND subject_type = 'ship' AND subject_id = ?
    """, (args.game, game['current_year'], game['current_week'],
          args.ship)).fetchone()

    seq_num = args.sequence if hasattr(args, 'sequence') and args.sequence else (max_seq['mx'] or 0) + 1
    params_json = json.dumps(params) if params else None

    conn.execute("""
        INSERT INTO turn_orders (game_id, turn_year, turn_week, player_id,
                                  subject_type, subject_id, order_sequence,
                                  command, parameters, status)
        VALUES (?, ?, ?, ?, 'ship', ?, ?, ?, ?, 'pending')
    """, (args.game, game['current_year'], game['current_week'],
          ship['player_id'], args.ship, seq_num, cmd, params_json))
    conn.commit()

    print(f"Injected order for {ship['name']} ({args.ship}):")
    print(f"  #{seq_num}: {cmd} {params_json or ''}")
    print(f"  (GM-injected)")
    conn.close()


def cmd_list_actions(args):
    """List moderator action requests for GM review."""
    db_path = Path(args.db) if args.db else None
    conn = get_connection(db_path)

    status_filter = args.status if hasattr(args, 'status') and args.status else 'pending'
    game = conn.execute("SELECT * FROM games WHERE game_id = ?", (args.game,)).fetchone()
    if not game:
        print(f"Error: Game {args.game} not found.")
        conn.close()
        return

    if status_filter == 'all':
        actions = conn.execute("""
            SELECT ma.*, s.name as ship_name, p.name as prefect_name, pl.player_name
            FROM moderator_actions ma
            JOIN ships s ON ma.ship_id = s.ship_id
            JOIN prefects p ON ma.prefect_id = p.prefect_id
            JOIN players pl ON p.player_id = pl.player_id
            WHERE ma.game_id = ?
            ORDER BY ma.action_id
        """, (args.game,)).fetchall()
    else:
        actions = conn.execute("""
            SELECT ma.*, s.name as ship_name, p.name as prefect_name, pl.player_name
            FROM moderator_actions ma
            JOIN ships s ON ma.ship_id = s.ship_id
            JOIN prefects p ON ma.prefect_id = p.prefect_id
            JOIN players pl ON p.player_id = pl.player_id
            WHERE ma.game_id = ? AND ma.status = ?
            ORDER BY ma.action_id
        """, (args.game, status_filter)).fetchall()

    print(f"\nModerator Actions ({status_filter}):")
    if not actions:
        print("  No actions found.")
    else:
        for a in actions:
            print(f"  #{a['action_id']}: {a['player_name']}/{a['prefect_name']} "
                  f"via {a['ship_name']} ({a['ship_id']})")
            print(f"    Turn: {a['requested_turn_year']}.{a['requested_turn_week']}  "
                  f"Status: {a['status'].upper()}")
            print(f"    Request: \"{a['request_text']}\"")
            if a['gm_response']:
                print(f"    Response: \"{a['gm_response']}\"")
            print()
    conn.close()


def cmd_respond_action(args):
    """Respond to a moderator action request."""
    db_path = Path(args.db) if args.db else None
    conn = get_connection(db_path)

    action = conn.execute(
        "SELECT * FROM moderator_actions WHERE action_id = ? AND game_id = ?",
        (args.action_id, args.game)
    ).fetchone()
    if not action:
        print(f"Error: action #{args.action_id} not found.")
        conn.close()
        return
    if action['status'] not in ('pending', 'responded'):
        print(f"Error: action #{args.action_id} is already {action['status']}.")
        conn.close()
        return

    conn.execute("""
        UPDATE moderator_actions SET status = 'responded', gm_response = ?
        WHERE action_id = ?
    """, (args.response, args.action_id))
    conn.commit()

    ship = conn.execute("SELECT name FROM ships WHERE ship_id = ?",
                         (action['ship_id'],)).fetchone()
    print(f"Responded to action #{args.action_id}:")
    print(f"  Ship: {ship['name']} ({action['ship_id']})")
    print(f"  Request: \"{action['request_text']}\"")
    print(f"  Response: \"{args.response}\"")

    # Check if all actions for this turn are now responded
    remaining = conn.execute("""
        SELECT COUNT(*) as cnt FROM moderator_actions
        WHERE game_id = ? AND requested_turn_year = ? AND requested_turn_week = ?
        AND status = 'pending'
    """, (args.game, action['requested_turn_year'],
          action['requested_turn_week'])).fetchone()

    if remaining['cnt'] == 0:
        print(f"\n  All moderator actions responded. Use 'release-turn' then 'run-turn' to continue.")
    else:
        print(f"\n  {remaining['cnt']} action(s) still pending.")
    conn.close()


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
    sp.add_argument('--force', action='store_true',
                    help='Override held/completed status')

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

    # --- add-port ---
    sp = subparsers.add_parser('add-port', help='Add a surface port to a planet')
    sp.add_argument('port_id', type=int, help='Unique port ID')
    sp.add_argument('body_id', type=int, help='Planet/moon body ID')
    sp.add_argument('name', help='Port name')
    sp.add_argument('x', type=int, help='Surface X coordinate')
    sp.add_argument('y', type=int, help='Surface Y coordinate')
    sp.add_argument('--parent-base', type=int, help='Linked orbital starbase ID')
    sp.add_argument('--complexes', type=int, default=0, help='Number of complexes')
    sp.add_argument('--workers', type=int, default=0, help='Worker count')
    sp.add_argument('--troops', type=int, default=0, help='Troop count')

    # --- add-outpost ---
    sp = subparsers.add_parser('add-outpost', help='Add an outpost to a planet or moon')
    sp.add_argument('outpost_id', type=int, help='Unique outpost ID')
    sp.add_argument('body_id', type=int, help='Planet/moon body ID')
    sp.add_argument('name', help='Outpost name')
    sp.add_argument('x', type=int, help='Surface X coordinate')
    sp.add_argument('y', type=int, help='Surface Y coordinate')
    sp.add_argument('--type', default='General', help='Outpost type (e.g. Mining, Communications)')
    sp.add_argument('--workers', type=int, default=0, help='Worker count')

    # --- list-universe ---
    sp = subparsers.add_parser('list-universe', help='Show all universe content (systems, bodies, links)')

    # --- list-factions ---
    sp = subparsers.add_parser('list-factions', help='List all available factions')

    # --- faction-requests ---
    sp = subparsers.add_parser('faction-requests', help='List faction change requests')
    sp.add_argument('--game', required=True, help='Game ID')
    sp.add_argument('--status', default='pending', help='Filter: pending, approved, denied, all')

    # --- approve-faction ---
    sp = subparsers.add_parser('approve-faction', help='Approve a faction change request')
    sp.add_argument('--game', required=True, help='Game ID')
    sp.add_argument('--request-id', type=int, required=True, help='Request ID to approve')
    sp.add_argument('--note', default='', help='GM note to include')

    # --- deny-faction ---
    sp = subparsers.add_parser('deny-faction', help='Deny a faction change request')
    sp.add_argument('--game', required=True, help='Game ID')
    sp.add_argument('--request-id', type=int, required=True, help='Request ID to deny')
    sp.add_argument('--note', default='', help='GM note / reason for denial')

    # --- GM / Moderator Actions ---
    sp = subparsers.add_parser('turn-pipeline', help='Show current turn pipeline status')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')
    sp.add_argument('--db', help='Path to game_state.db')

    sp = subparsers.add_parser('hold-turn', help='Hold turn — lock orders, block run-turn')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')
    sp.add_argument('--db', help='Path to game_state.db')

    sp = subparsers.add_parser('release-turn', help='Release a held turn')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')
    sp.add_argument('--db', help='Path to game_state.db')
    sp.add_argument('--reopen', action='store_true', help='Reopen for additional orders')

    sp = subparsers.add_parser('review-orders', help='Review all pending orders for GM inspection')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')
    sp.add_argument('--db', help='Path to game_state.db')

    sp = subparsers.add_parser('edit-order', help='Edit a pending order')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')
    sp.add_argument('--db', help='Path to game_state.db')
    sp.add_argument('--order-id', type=int, required=True, help='Order ID to edit')
    sp.add_argument('--command', dest='command_str', help='New command string e.g. "MOVE F10"')

    sp = subparsers.add_parser('delete-order', help='Delete a pending order')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')
    sp.add_argument('--db', help='Path to game_state.db')
    sp.add_argument('--order-id', type=int, required=True, help='Order ID to delete')

    sp = subparsers.add_parser('inject-order', help='Inject a GM order for any ship')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')
    sp.add_argument('--db', help='Path to game_state.db')
    sp.add_argument('--ship', type=int, required=True, help='Ship ID')
    sp.add_argument('--command', dest='command_str', required=True, help='Command string e.g. "MOVE F10"')
    sp.add_argument('--sequence', type=int, help='Order sequence number (default: append)')

    # --- list-actions ---
    sp = subparsers.add_parser('list-actions', help='List moderator action requests')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')
    sp.add_argument('--db', help='Path to game_state.db')
    sp.add_argument('--status', default='pending', help='Filter: pending, responded, resolved, all')

    # --- respond-action ---
    sp = subparsers.add_parser('respond-action', help='Respond to a moderator action request')
    sp.add_argument('--game', default='OMICRON101', help='Game ID')
    sp.add_argument('--db', help='Path to game_state.db')
    sp.add_argument('--action-id', type=int, required=True, help='Action ID to respond to')
    sp.add_argument('--response', required=True, help='GM response text')

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
        'add-port': cmd_add_port,
        'add-outpost': cmd_add_outpost,
        'list-universe': cmd_list_universe,
        'list-factions': cmd_list_factions,
        'faction-requests': cmd_faction_requests,
        'approve-faction': cmd_approve_faction,
        'deny-faction': cmd_deny_faction,
        'turn-pipeline': cmd_turn_pipeline,
        'hold-turn': cmd_hold_turn,
        'release-turn': cmd_release_turn,
        'review-orders': cmd_review_orders,
        'edit-order': cmd_edit_order,
        'delete-order': cmd_delete_order,
        'inject-order': cmd_inject_order,
        'list-actions': cmd_list_actions,
        'respond-action': cmd_respond_action,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
