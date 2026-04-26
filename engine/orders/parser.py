"""
Stellar Dominion - Order Parser
Parses player orders from YAML or text format.
"""

import yaml
import re
from pathlib import Path


# Valid commands, parameter types, and which subject they attach to.
# 'subject' field: 'ship' (default), 'prefect', or 'both'.
VALID_COMMANDS = {
    'WAIT': {'params': 'integer', 'description': 'Wait for n Operational Cycles (OC)'},
    'MOVE': {'params': 'coordinate', 'description': 'Move to grid coordinate'},
    'SCANLOCATION': {'params': 'optional_integer', 'description': 'Active scan: spend N OCs staring at nearby cells (default 1)'},
    'SCANSYSTEM': {'params': 'none', 'description': 'Produce system map'},
    'ORBIT': {'params': 'body_id', 'description': 'Orbit a celestial body'},
    'DOCK': {'params': 'base_id', 'description': 'Dock at a starbase'},
    'UNDOCK': {'params': 'none', 'description': 'Leave docked starbase'},
    'LEAVEORBIT': {'params': 'none', 'description': 'Leave orbit of a celestial body'},
    'LAND': {'params': 'land_order', 'description': 'Land on a planet or moon at coordinates'},
    'TAKEOFF': {'params': 'none', 'description': 'Take off from planet surface to orbit'},
    'SCANSURFACE': {'params': 'none', 'description': 'Scan the surface of the planet you are orbiting or landed on'},
    'SURVEY': {'params': 'none', 'description': 'Survey jump routes leaving the current system (reveals neighbour system IDs)'},
    'BUY': {'params': 'trade_order', 'description': 'Buy items from base market'},
    'SELL': {'params': 'trade_order', 'description': 'Sell items to base market'},
    'LOADMAGAZINE':   {'params': 'magazine_op', 'description': 'Move ammo from cargo to magazine: LOADMAGAZINE <missile|torpedo> <qty>'},
    'UNLOADMAGAZINE': {'params': 'magazine_op', 'description': 'Move ammo from magazine to cargo: UNLOADMAGAZINE <missile|torpedo> <qty>'},
    'GETMARKET': {'params': 'base_id', 'description': 'View base market prices'},
    'JUMP': {'params': 'system_id', 'description': 'Jump to a linked star system'},
    'MESSAGE': {'params': 'message_order', 'description': 'Send a message to another position'},
    'MAKEOFFICER': {'params': 'makeofficer_order', 'description': 'Promote a crew member to officer'},
    'INSTALL': {'params': 'component_order', 'description': 'Install a component from cargo'},
    'UNINSTALL': {'params': 'component_order', 'description': 'Uninstall a component to cargo'},
    'SCRAP': {'params': 'component_order', 'description': 'Scrap a component from cargo'},
    'RENAMESHIP': {'params': 'rename_id_name', 'description': 'Rename a ship'},
    'RENAMEBASE': {'params': 'rename_id_name', 'description': 'Rename a starbase'},
    'RENAMEPREFECT': {'params': 'rename_id_name', 'subject': 'both', 'description': 'Rename a prefect'},
    'RENAMEOFFICER': {'params': 'rename_officer', 'description': 'Rename an officer'},
    'CHANGEFACTION': {'params': 'changefaction_order', 'subject': 'prefect', 'description': 'Request to change faction (GM-moderated, prefect-scoped)'},
    'MODERATOR': {'params': 'moderator_order', 'description': 'Submit a free-text request to the GM'},
    'CLEAR': {'params': 'none', 'description': 'Clear all pending overflow orders from previous turns'},
    # Combat list management (per-ship; ships and bases have their own lists)
    'TARGET': {'params': 'list_op', 'description': 'Manage target list: TARGET <add|remove|clear> [type] <id>'},
    'DEFEND': {'params': 'list_op', 'description': 'Manage defend list: DEFEND <add|remove|clear> [type] <id>'},
    'AVOID':  {'params': 'list_op', 'description': 'Manage avoid list (ships only): AVOID <add|remove|clear> [type] <id>'},
    'DOCTRINE': {'params': 'doctrine_choice', 'description': 'Set combat doctrine: DOCTRINE <aggressive|defensive|evasive>'},
    # Base/port/outpost commands
    'BUILD': {'params': 'build_order', 'description': 'Build/install a module on a base'},
    'SETBUY': {'params': 'setprice_order', 'description': 'Set market buy price for an item'},
    'SETSELL': {'params': 'setprice_order', 'description': 'Set market sell price for an item'},
    # Magazine management (per-ship): transfer missiles/torpedoes between cargo and magazine
    'LOAD':   {'params': 'magazine_op', 'description': 'Load ammo into magazine from cargo: LOAD MAGAZINE MISSILE|TORPEDO <qty>'},
    'UNLOAD': {'params': 'magazine_op', 'description': 'Unload ammo from magazine to cargo: UNLOAD MAGAZINE MISSILE|TORPEDO <qty>'},
}


def get_command_subject(command):
    """Return the valid subject type(s) for a command: 'ship', 'prefect', or 'both'.
    Defaults to 'ship' if not specified."""
    spec = VALID_COMMANDS.get(command, {})
    return spec.get('subject', 'ship')


def command_allowed_for_subject(command, subject_type):
    """Check if a command is allowed for the given subject type ('ship' or 'prefect')."""
    allowed = get_command_subject(command)
    if allowed == 'both':
        return True
    return allowed == subject_type

# Backwards-compatible command aliases (old -> new).
# We normalize to the new "SCAN*" naming so reports/logs are consistent.
COMMAND_ALIASES = {
    'LOCATIONSCAN': 'SCANLOCATION',
    'SYSTEMSCAN': 'SCANSYSTEM',
    'SURFACESCAN': 'SCANSURFACE',
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
    command = COMMAND_ALIASES.get(command, command)

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

    elif spec['params'] == 'optional_integer':
        # Allow bare form (defaults to 1) or integer form
        if params is None or params == '' or params == {}:
            return command, {'duration': 1}, None
        try:
            value = int(params)
            if value < 1:
                return command, params, f"{command}: duration must be >= 1"
            return command, {'duration': value}, None
        except (ValueError, TypeError):
            return command, params, f"{command}: expected integer duration, got '{params}'"

    elif spec['params'] == 'list_op':
        # TARGET/DEFEND/AVOID — supports forms:
        #   "ADD <id>"                  -> default type 'ship'
        #   "ADD ship <id>"
        #   "ADD faction <id>"
        #   "ADD base <id>"
        #   "REMOVE <type?> <id>"
        #   "CLEAR"                     -> wipe entire list
        #
        # YAML form: {op: 'add', type: 'ship', id: 12345}
        # Text form: ADD ship 12345 / REMOVE 12345 / CLEAR
        VALID_OPS = ('add', 'remove', 'clear')
        VALID_TYPES = ('ship', 'base', 'faction')

        if isinstance(params, dict):
            op = str(params.get('op', '')).strip().lower()
            entry_type = str(params.get('type', 'ship')).strip().lower() if params.get('type') else 'ship'
            entry_id_raw = params.get('id')
        elif isinstance(params, str):
            tokens = params.strip().split()
            if not tokens:
                return command, params, f"{command}: missing operation. Use ADD/REMOVE/CLEAR."
            op = tokens[0].lower()
            if op == 'clear':
                return command, {'op': 'clear', 'type': None, 'id': None}, None
            # ADD/REMOVE: optional type word, then id
            if len(tokens) == 2:
                entry_type = 'ship'
                entry_id_raw = tokens[1]
            elif len(tokens) >= 3:
                entry_type = tokens[1].lower()
                entry_id_raw = tokens[2]
            else:
                return command, params, f"{command}: missing target ID. Use {op.upper()} [ship|base|faction] <id>."
        else:
            return command, params, f"{command}: expected operation string"

        if op not in VALID_OPS:
            return command, params, f"{command}: unknown operation '{op}'. Use ADD, REMOVE, or CLEAR."
        if op == 'clear':
            return command, {'op': 'clear', 'type': None, 'id': None}, None
        if entry_type not in VALID_TYPES:
            return command, params, f"{command}: unknown entry type '{entry_type}'. Use ship, base, or faction."
        try:
            entry_id = int(entry_id_raw)
        except (ValueError, TypeError):
            return command, params, f"{command}: expected numeric ID, got '{entry_id_raw}'"
        if entry_id <= 0:
            return command, params, f"{command}: ID must be positive"
        return command, {'op': op, 'type': entry_type, 'id': entry_id}, None

    elif spec['params'] == 'doctrine_choice':
        # DOCTRINE aggressive | defensive | evasive
        VALID_DOCTRINES = ('aggressive', 'defensive', 'evasive')
        if isinstance(params, dict):
            value = str(params.get('doctrine', '')).strip().lower()
        elif isinstance(params, str):
            value = params.strip().lower()
        else:
            return command, params, f"{command}: expected one of {VALID_DOCTRINES}"
        if value not in VALID_DOCTRINES:
            return command, params, f"{command}: must be one of {', '.join(VALID_DOCTRINES)}"
        return command, {'doctrine': value}, None

    elif spec['params'] == 'magazine_op':
        # LOAD/UNLOAD MAGAZINE MISSILE|TORPEDO <qty>
        # YAML: {ammo: 'missile'|'torpedo', qty: N}
        # Text: MAGAZINE MISSILE 5  (the verb LOAD/UNLOAD is the command)
        VALID_AMMO = ('missile', 'torpedo')
        if isinstance(params, dict):
            ammo = str(params.get('ammo', '')).strip().lower()
            try:
                qty = int(params.get('qty', params.get('quantity', 0)))
            except (ValueError, TypeError):
                return command, params, f"{command}: qty must be a positive integer"
        elif isinstance(params, str):
            parts = params.strip().split()
            # Accept either "MAGAZINE MISSILE N" or "MISSILE N" (MAGAZINE keyword optional)
            if len(parts) >= 3 and parts[0].upper() == 'MAGAZINE':
                parts = parts[1:]
            if len(parts) < 2:
                return command, params, f"{command}: expected 'MAGAZINE <MISSILE|TORPEDO> <qty>', got '{params}'"
            ammo = parts[0].strip().lower()
            try:
                qty = int(parts[1])
            except (ValueError, TypeError):
                return command, params, f"{command}: qty must be a positive integer, got '{parts[1]}'"
        else:
            return command, params, f"{command}: expected '<MISSILE|TORPEDO> <qty>'"
        if ammo not in VALID_AMMO:
            return command, params, f"{command}: ammo must be one of {', '.join(VALID_AMMO)}, got '{ammo}'"
        if qty <= 0:
            return command, params, f"{command}: qty must be positive"
        return command, {'ammo': ammo, 'qty': qty}, None

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
        # BUY/SELL: needs base_id, item_id, quantity [INSTALL | MAGAZINE]
        # INSTALL: on BUY, auto-install the item as a component
        # MAGAZINE: on BUY, load ammo directly into the ship's magazine
        # YAML: {base: 45687590, item: 101, qty: 10, install: true|magazine: true}
        # Text: BUY 45687590 101 10  or  BUY 45687590 130 2 INSTALL  or  BUY 45687590 501 10 MAGAZINE
        if isinstance(params, dict):
            try:
                base_id = int(params.get('base', params.get('base_id', 0)))
                item_id = int(params.get('item', params.get('item_id', 0)))
                qty = int(params.get('qty', params.get('quantity', 0)))
                install = bool(params.get('install', False))
                magazine = bool(params.get('magazine', False))
                if install and magazine:
                    return command, params, f"{command}: cannot specify both INSTALL and MAGAZINE"
                if base_id <= 0 or item_id <= 0 or qty <= 0:
                    return command, params, f"{command}: base, item, and qty must be positive integers"
                return command, {'base_id': base_id, 'item_id': item_id, 'quantity': qty,
                                   'install': install, 'magazine': magazine}, None
            except (ValueError, TypeError):
                return command, params, f"{command}: invalid trade parameters"
        elif isinstance(params, str):
            parts = params.strip().split()
            if len(parts) < 3:
                return command, params, f"{command}: expected 'base_id item_id quantity [INSTALL|MAGAZINE]', got '{params}'"
            flag = parts[3].upper() if len(parts) >= 4 else ''
            install = flag == 'INSTALL'
            magazine = flag == 'MAGAZINE'
            if flag and not (install or magazine):
                return command, params, f"{command}: unknown flag '{parts[3]}' (expected INSTALL or MAGAZINE)"
            try:
                base_id = int(parts[0])
                item_id = int(parts[1])
                qty = int(parts[2])
                if base_id <= 0 or item_id <= 0 or qty <= 0:
                    return command, params, f"{command}: base, item, and qty must be positive integers"
                return command, {'base_id': base_id, 'item_id': item_id, 'quantity': qty,
                                   'install': install, 'magazine': magazine}, None
            except ValueError:
                return command, params, f"{command}: expected numeric values, got '{params}'"
        return command, params, f"{command}: expected trade parameters (base_id item_id quantity)"

    elif spec['params'] == 'magazine_op':
        # LOADMAGAZINE / UNLOADMAGAZINE: ammo_type and quantity.
        # YAML: {ammo: 'missile', qty: 5} or "missile 5"
        # Text: LOADMAGAZINE MISSILE 5
        if isinstance(params, dict):
            ammo = str(params.get('ammo', params.get('type', ''))).strip().lower()
            try:
                qty = int(params.get('qty', params.get('quantity', 0)))
            except (ValueError, TypeError):
                return command, params, f"{command}: qty must be a positive integer"
            if ammo not in ('missile', 'torpedo'):
                return command, params, f"{command}: ammo type must be 'missile' or 'torpedo', got '{ammo}'"
            if qty <= 0:
                return command, params, f"{command}: qty must be a positive integer"
            return command, {'ammo_type': ammo, 'quantity': qty}, None
        elif isinstance(params, str):
            parts = params.strip().split()
            if len(parts) != 2:
                return command, params, f"{command}: expected 'missile|torpedo <qty>', got '{params}'"
            ammo = parts[0].lower()
            if ammo not in ('missile', 'torpedo'):
                return command, params, f"{command}: ammo type must be 'missile' or 'torpedo', got '{parts[0]}'"
            try:
                qty = int(parts[1])
            except ValueError:
                return command, params, f"{command}: qty must be a positive integer, got '{parts[1]}'"
            if qty <= 0:
                return command, params, f"{command}: qty must be a positive integer"
            return command, {'ammo_type': ammo, 'quantity': qty}, None
        return command, params, f"{command}: expected 'missile|torpedo <qty>'"

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

    elif spec['params'] == 'component_order':
        # INSTALL/UNINSTALL/SCRAP: component_id [quantity]
        # Text: INSTALL 130 2  or  INSTALL 130
        # YAML: {component: 130, qty: 2} or "130 2" or 130
        if isinstance(params, dict):
            try:
                comp_id = int(params.get('component', params.get('component_id', 0)))
                qty = int(params.get('qty', params.get('quantity', 1)))
                if comp_id <= 0:
                    return command, params, f"{command}: component_id must be a positive integer"
                if qty <= 0:
                    return command, params, f"{command}: quantity must be positive"
                return command, {'component_id': comp_id, 'quantity': qty}, None
            except (ValueError, TypeError):
                return command, params, f"{command}: invalid parameters"
        elif isinstance(params, (int, float)):
            return command, {'component_id': int(params), 'quantity': 1}, None
        elif isinstance(params, str):
            parts = params.strip().split()
            if len(parts) < 1:
                return command, params, f"{command}: expected 'component_id [quantity]'"
            try:
                comp_id = int(parts[0])
                qty = int(parts[1]) if len(parts) > 1 else 1
                if comp_id <= 0:
                    return command, params, f"{command}: component_id must be a positive integer"
                if qty <= 0:
                    return command, params, f"{command}: quantity must be positive"
                return command, {'component_id': comp_id, 'quantity': qty}, None
            except ValueError:
                return command, params, f"{command}: expected numeric component_id"
        return command, params, f"{command}: expected parameters (component_id [quantity])"

    elif spec['params'] == 'build_order':
        # BUILD: module_id [quantity]
        # Text: BUILD 510 2  or  BUILD 510
        # YAML: {module: 510, qty: 2} or "510 2" or 510
        if isinstance(params, dict):
            try:
                mod_id = int(params.get('module', params.get('module_id', 0)))
                qty = int(params.get('qty', params.get('quantity', 1)))
                if mod_id <= 0:
                    return command, params, f"{command}: module_id must be a positive integer"
                if qty <= 0:
                    return command, params, f"{command}: quantity must be positive"
                return command, {'module_id': mod_id, 'quantity': qty}, None
            except (ValueError, TypeError):
                return command, params, f"{command}: invalid parameters"
        elif isinstance(params, (int, float)):
            return command, {'module_id': int(params), 'quantity': 1}, None
        elif isinstance(params, str):
            parts = params.strip().split()
            if len(parts) < 1:
                return command, params, f"{command}: expected 'module_id [quantity]'"
            try:
                mod_id = int(parts[0])
                qty = int(parts[1]) if len(parts) > 1 else 1
                if mod_id <= 0:
                    return command, params, f"{command}: module_id must be a positive integer"
                if qty <= 0:
                    return command, params, f"{command}: quantity must be positive"
                return command, {'module_id': mod_id, 'quantity': qty}, None
            except ValueError:
                return command, params, f"{command}: expected numeric module_id"
        return command, params, f"{command}: expected parameters (module_id [quantity])"

    elif spec['params'] == 'setprice_order':
        # SETBUY/SETSELL: item_id price
        # Text: SETBUY 100101 25
        # YAML: {item: 100101, price: 25} or "100101 25"
        if isinstance(params, dict):
            try:
                item_id = int(params.get('item', params.get('item_id', 0)))
                price = int(params.get('price', 0))
                if item_id <= 0:
                    return command, params, f"{command}: item_id must be a positive integer"
                if price < 0:
                    return command, params, f"{command}: price must be non-negative"
                return command, {'item_id': item_id, 'price': price}, None
            except (ValueError, TypeError):
                return command, params, f"{command}: invalid parameters"
        elif isinstance(params, str):
            parts = params.strip().split()
            if len(parts) != 2:
                return command, params, f"{command}: expected 'item_id price'"
            try:
                item_id = int(parts[0])
                price = int(parts[1])
                if item_id <= 0:
                    return command, params, f"{command}: item_id must be a positive integer"
                if price < 0:
                    return command, params, f"{command}: price must be non-negative"
                return command, {'item_id': item_id, 'price': price}, None
            except ValueError:
                return command, params, f"{command}: expected numeric item_id and price"
        return command, params, f"{command}: expected parameters (item_id price)"

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


def _validate_orders_against_subject(result):
    """
    Given a parsed result dict, determine the subject type from the declared
    fields and validate each order against its allowed subject list. Orders
    that don't match are removed and errors are appended.
    """
    subject_type = None
    if result.get('prefect'):
        subject_type = 'prefect'
    elif result.get('ship'):
        subject_type = 'ship'
    # (starbase/port/outpost have their own command set — not validated here)

    if not subject_type:
        return

    valid_orders = []
    for o in result['orders']:
        if not command_allowed_for_subject(o['command'], subject_type):
            required = get_command_subject(o['command'])
            result['errors'].append(
                f"Order #{o['sequence']} {o['command']}: this command must be filed "
                f"against a {required}, not a {subject_type}. "
                f"Move it into a {required.upper()} block."
            )
        else:
            valid_orders.append(o)
    result['orders'] = valid_orders


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
      - SCANLOCATION: {}
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
        'prefect': data.get('prefect', ''),
        'starbase': data.get('starbase', ''),
        'port': data.get('port', ''),
        'outpost': data.get('outpost', ''),
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

    _validate_orders_against_subject(result)
    return result


def parse_text_orders(text_content):
    """
    Parse orders from plain text format.
    
    Expected format (one per line):
    GAME OMICRON101
    ACCOUNT 35846634
    SHIP 2547876          (or PREFECT 48814452, STARBASE 45687590, etc.)
    WAIT 50
    MOVE M13
    SCANLOCATION
    MOVE D08
    ORBIT 247985
    DOCK 45687590
    
    Each order file applies to a single subject. Commands are validated
    against the declared subject type — e.g. CHANGEFACTION is rejected
    in a SHIP block because it's a prefect-scoped order.
    
    Returns same format as parse_yaml_orders.
    """
    result = {
        'game': '',
        'account': '',
        'ship': '',
        'prefect': '',
        'starbase': '',
        'port': '',
        'outpost': '',
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
        elif cmd == 'PREFECT':
            result['prefect'] = params or ''
            continue
        elif cmd == 'STARBASE':
            result['starbase'] = params or ''
            continue
        elif cmd == 'PORT':
            result['port'] = params or ''
            continue
        elif cmd == 'OUTPOST':
            result['outpost'] = params or ''
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

    _validate_orders_against_subject(result)
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
