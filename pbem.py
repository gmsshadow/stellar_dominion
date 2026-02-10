#!/usr/bin/env python3
"""
Stellar Dominion - PBEM Strategy Game Engine
Main CLI entry point.

Usage:
    python pbem.py setup-game                  # Create the demo game
    python pbem.py add-player --name "Alice" --email "alice@example.com" ...
    python pbem.py submit-orders <orders_file> # Submit orders for a ship
    python pbem.py run-turn --game HANF231     # Resolve all pending orders
    python pbem.py show-map --game HANF231     # Display system map
    python pbem.py show-status --ship <id>     # Show ship status
    python pbem.py advance-turn --game HANF231 # Advance to next turn
    python pbem.py list-ships --game HANF231   # List all ships
"""

import argparse
import sys
import os
import json
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from db.database import init_db, get_connection
from engine.game_setup import create_game, add_player, setup_demo_game
from engine.orders.parser import parse_orders_file, parse_yaml_orders, parse_text_orders
from engine.resolution.resolver import TurnResolver
from engine.reports.report_gen import generate_ship_report, generate_political_report
from engine.maps.system_map import render_system_map


def cmd_setup_game(args):
    """Create the demo game with Hanf system."""
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


def cmd_submit_orders(args):
    """Parse and submit orders from a file."""
    db_path = Path(args.db) if args.db else None

    orders = parse_orders_file(args.orders_file)

    if orders.get('error'):
        print(f"Error: {orders['error']}")
        return

    if orders.get('errors'):
        print("Order validation errors:")
        for e in orders['errors']:
            print(f"  - {e}")
        if not orders.get('orders'):
            return

    print(f"Game: {orders['game']}")
    print(f"Ship: {orders['ship']}")
    print(f"Valid orders: {len(orders['orders'])}")

    if orders.get('errors'):
        print(f"Errors: {len(orders['errors'])}")

    for o in orders['orders']:
        params_str = f" {o['params']}" if o['params'] else ""
        print(f"  {o['sequence']}. {o['command']}{params_str}")

    # Store parsed orders
    if not args.dry_run:
        conn = get_connection(db_path)
        game = conn.execute("SELECT * FROM games WHERE game_id = ?", (orders['game'],)).fetchone()
        if not game:
            print(f"Error: Game {orders['game']} not found.")
            conn.close()
            return

        ship = conn.execute("SELECT * FROM ships WHERE ship_id = ?", (int(orders['ship']),)).fetchone()
        if not ship:
            print(f"Error: Ship {orders['ship']} not found.")
            conn.close()
            return

        # Find player
        political = conn.execute(
            "SELECT pp.*, p.player_id FROM political_positions pp "
            "JOIN players p ON pp.player_id = p.player_id "
            "WHERE pp.position_id = ?",
            (ship['owner_political_id'],)
        ).fetchone()

        for o in orders['orders']:
            params_json = json.dumps(o['params']) if o['params'] is not None else None
            conn.execute("""
                INSERT INTO turn_orders 
                (game_id, turn_year, turn_week, player_id, subject_type, subject_id,
                 order_sequence, command, parameters, status)
                VALUES (?, ?, ?, ?, 'ship', ?, ?, ?, ?, 'pending')
            """, (
                orders['game'], game['current_year'], game['current_week'],
                political['player_id'], int(orders['ship']),
                o['sequence'], o['command'], params_json
            ))

        conn.commit()
        conn.close()
        print(f"\nOrders submitted for turn {game['current_year']}.{game['current_week']}")
    else:
        print("\n(Dry run - orders not stored)")


def cmd_run_turn(args):
    """Resolve turn for a specific ship or all ships."""
    db_path = Path(args.db) if args.db else None
    resolver = TurnResolver(db_path, game_id=args.game)

    conn = get_connection(db_path)
    game = conn.execute("SELECT * FROM games WHERE game_id = ?", (args.game,)).fetchone()
    turn_str = f"{game['current_year']}.{game['current_week']}"

    if args.ship:
        ships = [conn.execute("SELECT * FROM ships WHERE ship_id = ? AND game_id = ?",
                               (args.ship, args.game)).fetchone()]
        if not ships[0]:
            print(f"Ship {args.ship} not found.")
            return
    else:
        ships = conn.execute(
            "SELECT * FROM ships WHERE game_id = ?", (args.game,)
        ).fetchall()

    print(f"=== Resolving Turn {turn_str} for game {args.game} ===\n")

    for ship in ships:
        ship_id = ship['ship_id']

        # Get stored orders for this ship/turn
        stored_orders = conn.execute("""
            SELECT * FROM turn_orders 
            WHERE game_id = ? AND turn_year = ? AND turn_week = ?
            AND subject_type = 'ship' AND subject_id = ? AND status = 'pending'
            ORDER BY order_sequence
        """, (args.game, game['current_year'], game['current_week'], ship_id)).fetchall()

        if not stored_orders and not args.orders_file:
            print(f"Ship {ship['name']} ({ship_id}): No orders for this turn.")
            continue

        # Build orders list
        if args.orders_file:
            # Direct orders from file
            parsed = parse_orders_file(args.orders_file)
            order_list = parsed.get('orders', [])
        else:
            order_list = []
            for so in stored_orders:
                params = json.loads(so['parameters']) if so['parameters'] else None
                order_list.append({
                    'sequence': so['order_sequence'],
                    'command': so['command'],
                    'params': params,
                })

        print(f"Resolving {ship['name']} ({ship_id}) - {len(order_list)} orders...")
        result = resolver.resolve_ship_turn(ship_id, order_list)

        if result.get('error'):
            print(f"  Error: {result['error']}")
            continue

        # Mark stored orders as resolved
        conn.execute("""
            UPDATE turn_orders SET status = 'resolved'
            WHERE game_id = ? AND turn_year = ? AND turn_week = ?
            AND subject_type = 'ship' AND subject_id = ? AND status = 'pending'
        """, (args.game, game['current_year'], game['current_week'], ship_id))
        conn.commit()

        # Generate report
        report = generate_ship_report(result, db_path, args.game)

        # Save report to file
        report_dir = Path(args.output or "reports")
        report_dir.mkdir(parents=True, exist_ok=True)
        report_file = report_dir / f"report_turn{turn_str}_{ship['name'].replace(' ', '_')}_{ship_id}.txt"
        report_file.write_text(report)
        print(f"  Report saved: {report_file}")

        # Also print to console if verbose
        if args.verbose:
            print()
            print(report)
            print()

        # Generate political report
        political_report = generate_political_report(
            ship['owner_political_id'], db_path, args.game
        )
        pol_file = report_dir / f"report_turn{turn_str}_political_{ship['owner_political_id']}.txt"
        pol_file.write_text(political_report)
        print(f"  Political report saved: {pol_file}")

    resolver.close()
    conn.close()
    print(f"\n=== Turn {turn_str} resolution complete ===")


def cmd_show_map(args):
    """Display the system map."""
    db_path = Path(args.db) if args.db else None
    conn = get_connection(db_path)

    system = conn.execute(
        "SELECT * FROM star_systems WHERE game_id = ? AND system_id = ?",
        (args.game, args.system or 231)
    ).fetchone()

    if not system:
        print("System not found.")
        conn.close()
        return

    system_id = system['system_id']

    # Gather objects
    objects = []
    bodies = conn.execute("SELECT * FROM celestial_bodies WHERE system_id = ?", (system_id,)).fetchall()
    for b in bodies:
        objects.append({'type': b['body_type'], 'col': b['grid_col'], 'row': b['grid_row'],
                        'symbol': b['map_symbol'], 'name': b['name']})

    bases = conn.execute("SELECT * FROM starbases WHERE system_id = ? AND game_id = ?",
                          (system_id, args.game)).fetchall()
    for b in bases:
        objects.append({'type': 'base', 'col': b['grid_col'], 'row': b['grid_row'],
                        'symbol': 'B', 'name': b['name']})

    ships = conn.execute("SELECT * FROM ships WHERE system_id = ? AND game_id = ?",
                          (system_id, args.game)).fetchall()
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

    # Legend
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
    """Show status of a ship or political position."""
    db_path = Path(args.db) if args.db else None
    conn = get_connection(db_path)

    if args.ship:
        ship = conn.execute("SELECT s.*, ss.name as system_name FROM ships s "
                             "JOIN star_systems ss ON s.system_id = ss.system_id "
                             "WHERE s.ship_id = ?", (args.ship,)).fetchone()
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

    ships = conn.execute("""
        SELECT s.*, ss.name as system_name, pp.name as owner_name
        FROM ships s 
        JOIN star_systems ss ON s.system_id = ss.system_id
        JOIN political_positions pp ON s.owner_political_id = pp.position_id
        WHERE s.game_id = ?
    """, (args.game,)).fetchall()

    if not ships:
        print(f"No ships in game {args.game}.")
    else:
        print(f"\nShips in game {args.game}:")
        print(f"{'ID':<12} {'Name':<25} {'Owner':<20} {'Location':<15} {'TU':<10}")
        print("-" * 82)
        for s in ships:
            loc = f"{s['grid_col']}{s['grid_row']:02d} ({s['system_name']})"
            print(f"{s['ship_id']:<12} {s['name']:<25} {s['owner_name']:<20} {loc:<15} {s['tu_remaining']}/{s['tu_per_turn']}")

    conn.close()


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
    conn.execute("""
        UPDATE ships SET tu_remaining = tu_per_turn WHERE game_id = ?
    """, (args.game,))
    conn.commit()
    conn.close()

    print(f"Turn advanced: {old_turn} → {new_turn}")
    print("All ship TUs reset.")
    resolver.close()


def cmd_edit_credits(args):
    """Set credits for a political position."""
    db_path = Path(args.db) if args.db else None
    conn = get_connection(db_path)

    if args.political:
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


def main():
    parser = argparse.ArgumentParser(
        description="Stellar Dominion - PBEM Strategy Game Engine"
    )
    parser.add_argument('--db', help='Database file path', default=None)

    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # setup-game
    sp = subparsers.add_parser('setup-game', help='Create a new game')
    sp.add_argument('--game', default='HANF231', help='Game ID')
    sp.add_argument('--name', help='Game name')
    sp.add_argument('--demo', action='store_true', help='Create demo game with 2 players')

    # add-player
    sp = subparsers.add_parser('add-player', help='Add a player')
    sp.add_argument('--game', default='HANF231', help='Game ID')
    sp.add_argument('--name', required=True, help='Player name')
    sp.add_argument('--email', required=True, help='Player email')
    sp.add_argument('--political', help='Political position name')
    sp.add_argument('--ship-name', help='Starting ship name')
    sp.add_argument('--start-col', default='I', help='Starting grid column')
    sp.add_argument('--start-row', type=int, default=6, help='Starting grid row')

    # submit-orders
    sp = subparsers.add_parser('submit-orders', help='Submit orders from file')
    sp.add_argument('orders_file', help='Path to orders file (YAML or text)')
    sp.add_argument('--db', help='Database file path')
    sp.add_argument('--dry-run', action='store_true', help='Parse only, do not store')

    # run-turn
    sp = subparsers.add_parser('run-turn', help='Resolve turn')
    sp.add_argument('--game', default='HANF231', help='Game ID')
    sp.add_argument('--ship', type=int, help='Specific ship ID (or all)')
    sp.add_argument('--orders-file', help='Direct orders file (bypasses stored orders)')
    sp.add_argument('--output', default='reports', help='Output directory for reports')
    sp.add_argument('--verbose', '-v', action='store_true', help='Print reports to console')

    # show-map
    sp = subparsers.add_parser('show-map', help='Display system map')
    sp.add_argument('--game', default='HANF231', help='Game ID')
    sp.add_argument('--system', type=int, default=231, help='System ID')

    # show-status
    sp = subparsers.add_parser('show-status', help='Show ship/position status')
    sp.add_argument('--ship', type=int, help='Ship ID')

    # list-ships
    sp = subparsers.add_parser('list-ships', help='List all ships')
    sp.add_argument('--game', default='HANF231', help='Game ID')

    # advance-turn
    sp = subparsers.add_parser('advance-turn', help='Advance to next turn')
    sp.add_argument('--game', default='HANF231', help='Game ID')

    # edit-credits
    sp = subparsers.add_parser('edit-credits', help='Set credits for a political position')
    sp.add_argument('--political', type=int, required=True, help='Political position ID')
    sp.add_argument('--amount', type=float, required=True, help='Credit amount')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    commands = {
        'setup-game': cmd_setup_game,
        'add-player': cmd_add_player,
        'submit-orders': cmd_submit_orders,
        'run-turn': cmd_run_turn,
        'show-map': cmd_show_map,
        'show-status': cmd_show_status,
        'list-ships': cmd_list_ships,
        'advance-turn': cmd_advance_turn,
        'edit-credits': cmd_edit_credits,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
