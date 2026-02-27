"""
Stellar Dominion - Order Processor
Shared logic for validating, filing, and storing submissions from any source.
Handles both player orders and registration forms.
"""

import json
import yaml
from datetime import datetime
from engine.orders.parser import parse_yaml_orders, parse_text_orders
from engine.registration import (
    parse_yaml_registration, parse_text_registration,
    validate_registration,
)
from engine.game_setup import add_player


# ======================================================================
# Content Type Detection
# ======================================================================

def detect_content_type(content):
    """
    Detect whether content is player orders or a registration form.
    
    Returns: 'orders' | 'registration' | 'unknown'
    
    Detection rules:
      YAML: 'orders' key or 'ship'+'account' keys -> orders
            'player_name' or 'prefect_name' key  -> registration
      Text: PLAYER_NAME or PREFECT_NAME line     -> registration
            SHIP or ACCOUNT line                  -> orders
    """
    content = content.strip()
    if not content:
        return 'unknown'

    # Try YAML first
    try:
        data = yaml.safe_load(content)
        if isinstance(data, dict):
            # Registration markers
            if any(k in data for k in ('player_name', 'prefect_name', 'planet')):
                return 'registration'
            # Order markers
            if 'orders' in data or ('ship' in data and 'account' in data):
                return 'orders'
    except yaml.YAMLError:
        pass

    # Fall back to text line scanning
    for line in content.splitlines():
        line = line.strip().upper()
        if not line or line.startswith('#'):
            continue
        first_word = line.split(None, 1)[0] if line.split() else ''
        if first_word in ('PLAYER_NAME', 'PREFECT_NAME', 'PLANET'):
            return 'registration'
        if first_word in ('SHIP', 'ACCOUNT'):
            return 'orders'

    return 'unknown'


# ======================================================================
# Order Processing
# ======================================================================

def process_single_order(conn, folders, turn_str, game_id, email, content):
    """
    Validate, file, and store a single order submission.
    
    Args:
        conn: open database connection
        folders: TurnFolders instance
        turn_str: current turn string (e.g. "500.1")
        game_id: game ID string
        email: sender's email address
        content: raw orders text content
    
    Returns dict with:
        status: 'accepted' | 'rejected' | 'skipped'
        type: 'orders'
        ship_id: int or None
        ship_name: str or None
        order_count: int (if accepted)
        orders_summary: list of str (if accepted)
        error: str (if rejected/skipped)
    """
    base = {'type': 'orders'}

    # Try YAML first, fall back to text
    try:
        parsed = parse_yaml_orders(content)
        if parsed.get('error') or (not parsed.get('orders') and not parsed.get('ship')):
            parsed = parse_text_orders(content)
    except Exception as e:
        return {**base, 'status': 'rejected', 'ship_id': None, 'ship_name': None,
                'order_count': 0, 'orders_summary': [],
                'error': f"Parse error: {e}"}

    if parsed.get('error'):
        return {**base, 'status': 'rejected', 'ship_id': None, 'ship_name': None,
                'order_count': 0, 'orders_summary': [],
                'error': parsed['error']}

    orders = parsed.get('orders', [])
    if not orders:
        errors = parsed.get('errors', [])
        if errors:
            return {**base, 'status': 'rejected', 'ship_id': None, 'ship_name': None,
                    'order_count': 0, 'orders_summary': [],
                    'error': '; '.join(errors)}
        return {**base, 'status': 'skipped', 'ship_id': None, 'ship_name': None,
                'order_count': 0, 'orders_summary': [],
                'error': 'No valid orders found'}

    # Extract ship ID
    ship_id = parsed.get('ship')
    if not ship_id:
        return {**base, 'status': 'skipped', 'ship_id': None, 'ship_name': None,
                'order_count': 0, 'orders_summary': [],
                'error': 'No ship ID in orders'}

    try:
        ship_id = int(ship_id)
    except (ValueError, TypeError):
        return {**base, 'status': 'rejected', 'ship_id': None, 'ship_name': None,
                'order_count': 0, 'orders_summary': [],
                'error': f"Invalid ship ID '{ship_id}'"}

    # Check account number
    account = parsed.get('account', '')
    if not account:
        folders.store_rejected(turn_str, email, ship_id, content,
                               ["No account number specified in orders file"])
        return {**base, 'status': 'rejected', 'ship_id': ship_id, 'ship_name': None,
                'order_count': 0, 'orders_summary': [],
                'error': 'No account number in orders'}

    # Validate ownership (also checks suspension and account match)
    valid, account_number, error = folders.validate_ship_ownership(email, ship_id, account)

    if not valid:
        folders.store_rejected(turn_str, email, ship_id, content, [error])
        return {**base, 'status': 'rejected', 'ship_id': ship_id, 'ship_name': None,
                'order_count': 0, 'orders_summary': [],
                'error': error}

    # Store the incoming orders file
    folders.store_incoming_orders(turn_str, email, ship_id, content)

    # Write to database
    game = conn.execute(
        "SELECT current_year, current_week FROM games WHERE game_id = ?",
        (game_id,)
    ).fetchone()
    player = conn.execute(
        "SELECT player_id FROM players WHERE email = ? AND game_id = ?",
        (email, game_id)
    ).fetchone()

    # Clear previous orders for this ship/turn (resubmission replaces)
    conn.execute("""
        DELETE FROM turn_orders
        WHERE game_id = ? AND turn_year = ? AND turn_week = ?
          AND subject_type = 'ship' AND subject_id = ?
    """, (game_id, game['current_year'], game['current_week'], ship_id))

    # Insert new orders
    for seq, order in enumerate(orders, 1):
        params = json.dumps(order.get('params', {})) if order.get('params') else None
        conn.execute("""
            INSERT INTO turn_orders
                (game_id, turn_year, turn_week, player_id,
                 subject_type, subject_id, order_sequence, command, parameters)
            VALUES (?, ?, ?, ?, 'ship', ?, ?, ?, ?)
        """, (game_id, game['current_year'], game['current_week'],
              player['player_id'], ship_id, seq,
              order['command'], params))

    conn.commit()

    # Store receipt
    folders.store_receipt(turn_str, email, ship_id, {
        'status': 'accepted',
        'order_count': len(orders),
    })

    # Get ship name for display
    ship_row = conn.execute(
        "SELECT name FROM ships WHERE ship_id = ?", (ship_id,)
    ).fetchone()
    ship_name = ship_row['name'] if ship_row else None

    # Build order summary for display
    orders_summary = []
    for o in orders:
        params_str = f" {o['params']}" if o.get('params') else ""
        orders_summary.append(f"{o['command']}{params_str}")

    return {**base, 'status': 'accepted', 'ship_id': ship_id, 'ship_name': ship_name,
            'order_count': len(orders), 'error': None,
            'orders_summary': orders_summary}


# ======================================================================
# Registration Processing
# ======================================================================

def process_single_registration(db_path, game_id, email, content):
    """
    Validate and process a registration form submission.
    
    Args:
        db_path: Path to database (or None for default)
        game_id: game ID string
        email: sender's email address (from envelope/folder)
        content: raw registration form text
    
    Returns dict with:
        status: 'registered' | 'rejected'
        type: 'registration'
        player_name: str or None
        prefect_name: str or None
        ship_name: str or None
        account_number: str or None
        planet_name: str or None
        error: str (if rejected)
    """
    from db.database import get_connection

    # Try YAML first, fall back to text
    try:
        raw = yaml.safe_load(content)
        if isinstance(raw, dict):
            data = {
                'game': str(raw.get('game') or '').strip(),
                'player_name': str(raw.get('player_name') or '').strip(),
                'email': str(raw.get('email') or '').strip(),
                'prefect_name': str(raw.get('prefect_name') or '').strip(),
                'ship_name': str(raw.get('ship_name') or '').strip(),
                'planet': str(raw.get('planet') or '').strip(),
                'errors': [],
            }
        else:
            data = parse_text_registration(content)
    except yaml.YAMLError:
        data = parse_text_registration(content)

    _fail = {'type': 'registration', 'status': 'rejected',
             'player_name': data.get('player_name'),
             'prefect_name': None, 'ship_name': None,
             'account_number': None, 'planet_name': None}

    # Validate required fields
    errors = validate_registration(data)
    if errors:
        return {**_fail, 'error': '; '.join(errors)}

    # Check game matches
    form_game = data.get('game', '')
    if form_game and form_game != game_id:
        return {**_fail, 'error': f"Form game '{form_game}' does not match --game '{game_id}'"}

    # Check sender email matches form email (if sender known)
    form_email = data['email'].lower().strip()
    if email and email.lower().strip() != form_email:
        return {**_fail, 'error': f"Sender email '{email}' does not match form email '{form_email}'"}

    conn = get_connection(db_path)

    # Verify game exists
    game = conn.execute("SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()
    if not game:
        conn.close()
        return {**_fail, 'error': f"Game '{game_id}' not found"}

    # Check email not already registered
    existing = conn.execute(
        "SELECT player_name FROM players WHERE email = ? AND game_id = ?",
        (form_email, game_id)
    ).fetchone()
    if existing:
        conn.close()
        return {**_fail, 'error': f"Email '{form_email}' already registered to {existing['player_name']}"}

    # Verify planet exists
    try:
        planet_id = int(data['planet'])
    except (ValueError, TypeError):
        conn.close()
        return {**_fail, 'error': f"Planet must be a numeric body ID (got '{data.get('planet', '')}')"}

    planet = conn.execute(
        "SELECT cb.*, ss.name as system_name FROM celestial_bodies cb "
        "JOIN star_systems ss ON cb.system_id = ss.system_id "
        "WHERE cb.body_id = ?",
        (planet_id,)
    ).fetchone()
    if not planet:
        conn.close()
        return {**_fail, 'error': f"Planet/body {planet_id} not found in game {game_id}"}

    conn.close()

    # Create the player
    result = add_player(
        db_path=db_path,
        game_id=game_id,
        player_name=data['player_name'],
        email=form_email,
        prefect_name=data['prefect_name'],
        ship_name=data['ship_name'],
        start_orbit_body=planet_id,
    )

    if result:
        return {
            'type': 'registration',
            'status': 'registered',
            'player_name': data['player_name'],
            'prefect_name': data['prefect_name'],
            'ship_name': data['ship_name'],
            'account_number': result.get('account_number'),
            'planet_name': planet['name'],
            'error': None,
        }
    else:
        return {**_fail, 'error': 'Player creation failed (check console output)'}


# ======================================================================
# Reply / Acknowledgement Formatting
# ======================================================================

def format_received_ack(game_id):
    """
    Format a simple 'received' acknowledgement for the fetch stage.
    Sent immediately when mail is pulled from Gmail -- before validation.
    """
    return "\n".join([
        "Stellar Dominion - Submission Received",
        "=" * 38,
        "",
        f"Game: {game_id}",
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "Your submission has been received and is queued for processing.",
        "You will not receive a separate confirmation after processing;",
        "if there is a problem with your submission the GM will contact",
        "you directly.",
        "",
        "-- Stellar Dominion Game Engine",
    ])


def format_reply_text(result, game_id, turn_str):
    """
    Format a detailed reply for an order processing result.
    """
    lines = [
        "Stellar Dominion - Order Confirmation",
        "=" * 38,
        "",
        f"Game:   {game_id}",
        f"Turn:   {turn_str}",
        f"Time:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    if result['status'] == 'accepted':
        ship_str = str(result['ship_id'])
        if result.get('ship_name'):
            ship_str = f"{result['ship_name']} ({result['ship_id']})"

        lines.append("Status: ACCEPTED")
        lines.append(f"Ship:   {ship_str}")
        lines.append(f"Orders: {result['order_count']} received")
        lines.append("")
        lines.append("Order listing:")
        for i, cmd in enumerate(result.get('orders_summary', []), 1):
            lines.append(f"  {i:>2}. {cmd}")
        lines.append("")
        lines.append("Your orders have been filed for turn resolution.")
        lines.append("Resubmitting orders for the same ship will replace these.")

    elif result['status'] == 'rejected':
        lines.append("Status: REJECTED")
        if result.get('ship_id'):
            lines.append(f"Ship:   {result['ship_id']}")
        lines.append("")
        lines.append(f"Reason: {result.get('error', 'Unknown error')}")
        lines.append("")
        lines.append("Please fix the issue and resubmit your orders.")

    else:  # skipped
        lines.append("Status: NOT PROCESSED")
        lines.append("")
        lines.append(f"Reason: {result.get('error', 'Unknown')}")
        lines.append("")
        lines.append("Your message did not contain recognizable orders.")
        lines.append("Please check the format and resubmit.")

    lines.append("")
    lines.append("-- Stellar Dominion Game Engine")
    return "\n".join(lines)
