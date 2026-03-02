"""
Stellar Dominion - Order Parser
Parses player orders from YAML or text format.
"""

import yaml
import re
from pathlib import Path


# Valid commands and their parameter types
VALID_COMMANDS = {
    'WAIT': {'params': 'integer', 'description': 'Wait for n TU'},
    'MOVE': {'params': 'coordinate', 'description': 'Move to grid coordinate'},
    'LOCATIONSCAN': {'params': 'none', 'description': 'Scan nearby cells'},
    'SYSTEMSCAN': {'params': 'none', 'description': 'Produce system map'},
    'ORBIT': {'params': 'body_id', 'description': 'Orbit a celestial body'},
    'DOCK': {'params': 'base_id', 'description': 'Dock at a starbase'},
    'UNDOCK': {'params': 'none', 'description': 'Leave docked starbase'},
    'LAND': {'params': 'land_order', 'description': 'Land on a planet or moon at coordinates'},
    'TAKEOFF': {'params': 'none', 'description': 'Take off from planet surface to orbit'},
    'SURFACESCAN': {'params': 'none', 'description': 'Scan the surface of the planet you are landed on'},
    'BUY': {'params': 'trade_order', 'description': 'Buy items from base market'},
    'SELL': {'params': 'trade_order', 'description': 'Sell items to base market'},
    'GETMARKET': {'params': 'base_id', 'description': 'View base market prices'},
    'JUMP': {'params': 'system_id', 'description': 'Jump to a linked star system'},
    'MESSAGE': {'params': 'message_order', 'description': 'Send a message to another position'},
    'MAKEOFFICER': {'params': 'makeofficer_order', 'description': 'Promote a crew member to officer'},
    'RENAMESHIP': {'params': 'rename_id_name', 'description': 'Rename a ship'},
    'RENAMEBASE': {'params': 'rename_id_name', 'description': 'Rename a starbase'},
    'RENAMEPREFECT': {'params': 'rename_id_name', 'description': 'Rename a prefect'},
    'RENAMEOFFICER': {'params': 'rename_officer', 'description': 'Rename an officer'},
    'CHANGEFACTION': {'params': 'changefaction_order', 'description': 'Request to change faction (GM-moderated)'},
    'MODERATOR': {'params': 'moderator_order', 'description': 'Submit a free-text request to the GM'},
    'CLEAR': {'params': 'none', 'description': 'Clear all pending overflow orders from previous turns'},
}

# Grid coordinate pattern: A-Y followed by 01-25
COORD_PATTERN = re.compile(r'^([A-Y])(\d{2})$', re.IGNORECASE)


def validate_coordinate(coord):
    """Validate a grid coordinate like 'M13' or 'D08'."""
    match = COORD_PATTERN.match(coord.upper())
    if not match:
        return None, None
    col = match.group(1).upper()
    row = int(match.group(2))
    if row < 1 or row > 25:
        return None, None
    if col < 'A' or col > 'Y':
        return None, None
    return col, row


def parse_order(command_str, params):
    """
    Parse and validate a single order.
    Returns (command, parsed_params, error) tuple.
    """
    command = command_str.upper().strip()

    if command not in VALID_COMMANDS:
        return command, params, f"Unknown command: {command}"

    spec = VALID_COMMANDS[command]

    if spec['params'] == 'none':
        return command, None, None

    elif spec['params'] == 'integer':
        try:
            value = int(params)
            if value < 0:
                return command, params, f"{command}: value must be >= 0"
            return command, value, None
        except (ValueError, TypeError):
            return command, params, f"{command}: expected integer, got '{params}'"

    elif spec['params'] == 'coordinate':
        if isinstance(params, str):
            col, row = validate_coordinate(params)
            if col is None:
                return command, params, f"{command}: invalid coordinate '{params}'"
            return command, {'col': col, 'row': row}, None
        return command, params, f"{command}: expected coordinate string"

    elif spec['params'] in ('body_id', 'base_id', 'system_id'):
        try:
            value = int(params)
            return command, value, None
        except (ValueError, TypeError):
            return command, params, f"{command}: expected numeric ID, got '{params}'"

    elif spec['params'] == 'trade_order':
        # BUY/SELL: needs base_id, item_id, quantity
        # YAML: {base: 45687590, item: 101, qty: 10} or "45687590 101 10"
        # Text: BUY 45687590 101 10
        if isinstance(params, dict):
            try:
                base_id = int(params.get('base', params.get('base_id', 0)))
                item_id = int(params.get('item', params.get('item_id', 0)))
                qty = int(params.get('qty', params.get('quantity', 0)))
                if base_id <= 0 or item_id <= 0 or qty <= 0:
                    return command, params, f"{command}: base, item, and qty must be positive integers"
                return command, {'base_id': base_id, 'item_id': item_id, 'quantity': qty}, None
            except (ValueError, TypeError):
                return command, params, f"{command}: invalid trade parameters"
        elif isinstance(params, str):
            parts = params.strip().split()
            if len(parts) != 3:
                return command, params, f"{command}: expected 'base_id item_id quantity', got '{params}'"
            try:
                base_id = int(parts[0])
                item_id = int(parts[1])
                qty = int(parts[2])
                if base_id <= 0 or item_id <= 0 or qty <= 0:
                    return command, params, f"{command}: base, item, and qty must be positive integers"
                return command, {'base_id': base_id, 'item_id': item_id, 'quantity': qty}, None
            except ValueError:
                return command, params, f"{command}: expected numeric values, got '{params}'"
        return command, params, f"{command}: expected trade parameters (base_id item_id quantity)"

    elif spec['params'] == 'land_order':
        # LAND: needs body_id x y
        # YAML: {body: 247985, x: 5, y: 10} or "247985 5 10"
        # Text: LAND 247985 5 10
        if isinstance(params, dict):
            try:
                body_id = int(params.get('body', params.get('body_id', 0)))
                x = int(params.get('x', 1))
                y = int(params.get('y', 1))
                if body_id <= 0:
                    return command, params, f"{command}: body_id must be a positive integer"
                if not (1 <= x <= 31) or not (1 <= y <= 31):
                    return command, params, f"{command}: coordinates must be 1-31, got ({x},{y})"
                return command, {'body_id': body_id, 'x': x, 'y': y}, None
            except (ValueError, TypeError):
                return command, params, f"{command}: invalid land parameters"
        elif isinstance(params, (int, float)):
            # Just a body_id with no coordinates - default to (1,1)
            return command, {'body_id': int(params), 'x': 1, 'y': 1}, None
        elif isinstance(params, str):
            parts = params.strip().split()
            if len(parts) == 1:
                # Just body_id, default coords
                try:
                    body_id = int(parts[0])
                    return command, {'body_id': body_id, 'x': 1, 'y': 1}, None
                except ValueError:
                    return command, params, f"{command}: expected numeric body_id, got '{params}'"
            elif len(parts) == 3:
                try:
                    body_id = int(parts[0])
                    x = int(parts[1])
                    y = int(parts[2])
                    if body_id <= 0:
                        return command, params, f"{command}: body_id must be a positive integer"
                    if not (1 <= x <= 31) or not (1 <= y <= 31):
                        return command, params, f"{command}: coordinates must be 1-31, got ({x},{y})"
                    return command, {'body_id': body_id, 'x': x, 'y': y}, None
                except ValueError:
                    return command, params, f"{command}: expected 'body_id x y', got '{params}'"
            else:
                return command, params, f"{command}: expected 'body_id x y', got '{params}'"
        return command, params, f"{command}: expected land parameters (body_id x y)"

    elif spec['params'] == 'message_order':
        # MESSAGE: target_id followed by free text
        # YAML: {target: 75695302, text: "Hello"} or "75695302 Hello there"
        # Text: MESSAGE 75695302 Hello there captain
        if isinstance(params, dict):
            try:
                target_id = int(params.get('target', params.get('target_id', 0)))
                text = str(params.get('text', params.get('message', '')))
                if target_id <= 0:
                    return command, params, f"{command}: target_id must be a positive integer"
                if not text.strip():
                    return command, params, f"{command}: message text cannot be empty"
                return command, {'target_id': target_id, 'text': text.strip()}, None
            except (ValueError, TypeError):
                return command, params, f"{command}: invalid message parameters"
        elif isinstance(params, str):
            parts = params.strip().split(None, 1)
            if len(parts) < 2:
                return command, params, f"{command}: expected 'target_id message_text'"
            try:
                target_id = int(parts[0])
                text = parts[1].strip()
                if target_id <= 0:
                    return command, params, f"{command}: target_id must be a positive integer"
                if not text:
                    return command, params, f"{command}: message text cannot be empty"
                return command, {'target_id': target_id, 'text': text}, None
            except ValueError:
                return command, params, f"{command}: expected numeric target_id, got '{parts[0]}'"
        return command, params, f"{command}: expected message parameters (target_id text)"

    elif spec['params'] == 'makeofficer_order':
        # MAKEOFFICER: ship_id crew_type_id [name]
        # Text: MAKEOFFICER 52589098 401 Marcus Varro
        # YAML: {ship: 52589098, crew_type: 401, name: "Marcus Varro"}
        #   or: "52589098 401 Marcus Varro"
        if isinstance(params, dict):
            try:
                ship_id = int(params.get('ship', params.get('ship_id', 0)))
                crew_type = int(params.get('crew_type', params.get('crew_type_id', 0)))
                if ship_id <= 0 or crew_type <= 0:
                    return command, params, f"{command}: ship_id and crew_type_id must be positive integers"
                result = {'ship_id': ship_id, 'crew_type_id': crew_type}
                name = params.get('name', '').strip()
                if name:
                    result['name'] = name
                return command, result, None
            except (ValueError, TypeError):
                return command, params, f"{command}: invalid parameters"
        elif isinstance(params, str):
            parts = params.strip().split()
            if len(parts) < 2:
                return command, params, f"{command}: expected 'ship_id crew_type_id [name]'"
            try:
                ship_id = int(parts[0])
                crew_type = int(parts[1])
                if ship_id <= 0 or crew_type <= 0:
                    return command, params, f"{command}: ship_id and crew_type_id must be positive integers"
                result = {'ship_id': ship_id, 'crew_type_id': crew_type}
                if len(parts) > 2:
                    result['name'] = ' '.join(parts[2:])
                return command, result, None
            except ValueError:
                return command, params, f"{command}: expected numeric values for ship_id and crew_type_id"
        return command, params, f"{command}: expected parameters (ship_id crew_type_id [name])"

    elif spec['params'] == 'rename_id_name':
        # RENAMESHIP/RENAMEBASE/RENAMEPREFECT: id new_name
        # Text: RENAMESHIP 52589098 The Indomitable
        # YAML: {id: 52589098, name: "The Indomitable"} or "52589098 The Indomitable"
        if isinstance(params, dict):
            try:
                target_id = int(params.get('id', params.get('target', 0)))
                name = str(params.get('name', '')).strip()
                if target_id <= 0:
                    return command, params, f"{command}: id must be a positive integer"
                if not name:
                    return command, params, f"{command}: name cannot be empty"
                return command, {'id': target_id, 'name': name}, None
            except (ValueError, TypeError):
                return command, params, f"{command}: invalid parameters"
        elif isinstance(params, str):
            parts = params.strip().split(None, 1)
            if len(parts) < 2:
                return command, params, f"{command}: expected 'id new_name'"
            try:
                target_id = int(parts[0])
                name = parts[1].strip()
                if target_id <= 0:
                    return command, params, f"{command}: id must be a positive integer"
                if not name:
                    return command, params, f"{command}: name cannot be empty"
                return command, {'id': target_id, 'name': name}, None
            except ValueError:
                return command, params, f"{command}: expected numeric id, got '{parts[0]}'"
        return command, params, f"{command}: expected parameters (id new_name)"

    elif spec['params'] == 'rename_officer':
        # RENAMEOFFICER: ship_id crew_number new_name
        # Text: RENAMEOFFICER 52589098 2 Marcus Varro
        # YAML: {ship: 52589098, crew_number: 2, name: "Marcus Varro"}
        if isinstance(params, dict):
            try:
                ship_id = int(params.get('ship', params.get('ship_id', 0)))
                crew_num = int(params.get('crew_number', params.get('number', 0)))
                name = str(params.get('name', '')).strip()
                if ship_id <= 0 or crew_num <= 0:
                    return command, params, f"{command}: ship_id and crew_number must be positive integers"
                if not name:
                    return command, params, f"{command}: name cannot be empty"
                return command, {'ship_id': ship_id, 'crew_number': crew_num, 'name': name}, None
            except (ValueError, TypeError):
                return command, params, f"{command}: invalid parameters"
        elif isinstance(params, str):
            parts = params.strip().split(None, 2)
            if len(parts) < 3:
                return command, params, f"{command}: expected 'ship_id crew_number new_name'"
            try:
                ship_id = int(parts[0])
                crew_num = int(parts[1])
                name = parts[2].strip()
                if ship_id <= 0 or crew_num <= 0:
                    return command, params, f"{command}: ship_id and crew_number must be positive integers"
                if not name:
                    return command, params, f"{command}: name cannot be empty"
                return command, {'ship_id': ship_id, 'crew_number': crew_num, 'name': name}, None
            except ValueError:
                return command, params, f"{command}: expected numeric ship_id and crew_number"
        return command, params, f"{command}: expected parameters (ship_id crew_number new_name)"

    elif spec['params'] == 'changefaction_order':
        # CHANGEFACTION: faction_id [reason]
        # Text: CHANGEFACTION 12 Want to join the traders
        # YAML: {faction: 12, reason: "Want to join"} or "12 Want to join"
        if isinstance(params, dict):
            try:
                faction_id = int(params.get('faction', params.get('faction_id', -1)))
                reason = str(params.get('reason', '')).strip()
                if faction_id < 0:
                    return command, params, f"{command}: faction_id must be a non-negative integer"
                return command, {'faction_id': faction_id, 'reason': reason}, None
            except (ValueError, TypeError):
                return command, params, f"{command}: invalid parameters"
        elif isinstance(params, str):
            parts = params.strip().split(None, 1)
            if len(parts) < 1:
                return command, params, f"{command}: expected 'faction_id [reason]'"
            try:
                faction_id = int(parts[0])
                reason = parts[1].strip() if len(parts) > 1 else ''
                if faction_id < 0:
                    return command, params, f"{command}: faction_id must be a non-negative integer"
                return command, {'faction_id': faction_id, 'reason': reason}, None
            except ValueError:
                return command, params, f"{command}: expected numeric faction_id"
        return command, params, f"{command}: expected parameters (faction_id [reason])"

    elif spec['params'] == 'moderator_order':
        # MODERATOR: free-text request to GM
        # Text: MODERATOR Can I retrofit my ship with better sensors?
        # YAML: {text: "Can I retrofit my ship?"} or "Can I retrofit?"
        if isinstance(params, dict):
            text = str(params.get('text', params.get('message', ''))).strip()
            if not text:
                return command, params, f"{command}: request text cannot be empty"
            return command, {'text': text}, None
        elif isinstance(params, str):
            text = params.strip()
            if not text:
                return command, params, f"{command}: request text cannot be empty"
            return command, {'text': text}, None
        return command, params, f"{command}: expected free-text request"

    return command, params, f"Unknown parameter type for {command}"


def parse_yaml_orders(yaml_content):
    """
    Parse orders from YAML content.
    
    Expected format:
    game: OMICRON101
    account: 35846634
    ship: 2547876
    orders:
      - WAIT: 50
      - MOVE: M13
      - LOCATIONSCAN: {}
      - DOCK: 45687590
    
    Returns dict with game, account, ship, and parsed orders list.
    """
    try:
        data = yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        return {'error': f"YAML parse error: {e}"}

    if not isinstance(data, dict):
        return {'error': "Orders must be a YAML mapping"}

    result = {
        'game': data.get('game', ''),
        'account': str(data.get('account', '')),
        'ship': data.get('ship', ''),
        'orders': [],
        'errors': [],
    }

    raw_orders = data.get('orders', [])
    if not isinstance(raw_orders, list):
        result['errors'].append("'orders' must be a list")
        return result

    for i, order in enumerate(raw_orders):
        if isinstance(order, dict):
            for cmd, params in order.items():
                # Handle empty params (YAML {} becomes empty dict or None)
                if isinstance(params, dict) and not params:
                    params = None
                command, parsed_params, error = parse_order(str(cmd), params)
                if error:
                    result['errors'].append(f"Order {i + 1}: {error}")
                else:
                    result['orders'].append({
                        'sequence': i + 1,
                        'command': command,
                        'params': parsed_params,
                    })
        elif isinstance(order, str):
            # String item: could be "UNDOCK" or "GETMARKET 45687590" or "BUY 45687590 102 1"
            parts = order.strip().split(None, 1)
            cmd_str = parts[0]
            params_str = parts[1] if len(parts) > 1 else None
            command, parsed_params, error = parse_order(cmd_str, params_str)
            if error:
                result['errors'].append(f"Order {i + 1}: {error}")
            else:
                result['orders'].append({
                    'sequence': i + 1,
                    'command': command,
                    'params': parsed_params,
                })

    return result


def parse_text_orders(text_content):
    """
    Parse orders from plain text format.
    
    Expected format (one per line):
    GAME OMICRON101
    ACCOUNT 35846634
    SHIP 2547876
    WAIT 50
    MOVE M13
    LOCATIONSCAN
    MOVE D08
    ORBIT 247985
    DOCK 45687590
    
    Returns same format as parse_yaml_orders.
    """
    result = {
        'game': '',
        'account': '',
        'ship': '',
        'orders': [],
        'errors': [],
    }

    lines = [l.strip() for l in text_content.strip().splitlines() if l.strip() and not l.strip().startswith('#')]
    sequence = 0

    for line in lines:
        parts = line.split(None, 1)
        cmd = parts[0].upper()
        params = parts[1] if len(parts) > 1 else None

        if cmd == 'GAME':
            result['game'] = params or ''
            continue
        elif cmd == 'ACCOUNT':
            result['account'] = params or ''
            continue
        elif cmd == 'SHIP':
            result['ship'] = params or ''
            continue

        sequence += 1
        command, parsed_params, error = parse_order(cmd, params)
        if error:
            result['errors'].append(f"Line '{line}': {error}")
        else:
            result['orders'].append({
                'sequence': sequence,
                'command': command,
                'params': parsed_params,
            })

    return result


def parse_orders_file(filepath):
    """Parse orders from a file, auto-detecting format."""
    path = Path(filepath)
    content = path.read_text(encoding='utf-8')

    if path.suffix.lower() in ('.yaml', '.yml'):
        return parse_yaml_orders(content)
    else:
        # Try YAML first, fall back to text
        try:
            result = parse_yaml_orders(content)
            if result.get('orders') or not result.get('error'):
                return result
        except Exception:
            pass
        return parse_text_orders(content)


# Alias for external callers
parse_single_order = parse_order
