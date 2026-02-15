"""
Stellar Dominion - Registration Form Parser
Parses player registration forms from YAML or text format.
"""

import yaml
from pathlib import Path


def parse_yaml_registration(yaml_content):
    """
    Parse registration from YAML content.
    
    Expected format:
    game: OMICRON101
    player_name: Alice Smith
    email: alice@example.com
    prefect_name: Li Chen
    ship_name: Boethius
    starbase: 45687590
    """
    try:
        data = yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        return {'error': f"YAML parse error: {e}"}

    if not isinstance(data, dict):
        return {'error': "Registration must be a YAML mapping"}

    return {
        'game': str(data.get('game') or '').strip(),
        'player_name': str(data.get('player_name') or '').strip(),
        'email': str(data.get('email') or '').strip(),
        'prefect_name': str(data.get('prefect_name') or '').strip(),
        'ship_name': str(data.get('ship_name') or '').strip(),
        'starbase': str(data.get('starbase') or '').strip(),
        'errors': [],
    }


def parse_text_registration(text_content):
    """
    Parse registration from plain text format.
    
    Expected format:
    GAME OMICRON101
    PLAYER_NAME Alice Smith
    EMAIL alice@example.com
    PREFECT_NAME Li Chen
    SHIP_NAME Boethius
    STARBASE 45687590
    """
    result = {
        'game': '',
        'player_name': '',
        'email': '',
        'prefect_name': '',
        'ship_name': '',
        'starbase': '',
        'errors': [],
    }

    field_map = {
        'GAME': 'game',
        'PLAYER_NAME': 'player_name',
        'EMAIL': 'email',
        'PREFECT_NAME': 'prefect_name',
        'SHIP_NAME': 'ship_name',
        'STARBASE': 'starbase',
    }

    lines = [l.strip() for l in text_content.strip().splitlines()
             if l.strip() and not l.strip().startswith('#')]

    for line in lines:
        parts = line.split(None, 1)
        key = parts[0].upper()
        value = parts[1].strip() if len(parts) > 1 else ''

        if key in field_map:
            result[field_map[key]] = value
        else:
            result['errors'].append(f"Unknown field: {key}")

    return result


def parse_registration_file(filepath):
    """Parse a registration file, auto-detecting format."""
    path = Path(filepath)
    content = path.read_text(encoding='utf-8')

    if path.suffix in ('.yaml', '.yml'):
        return parse_yaml_registration(content)
    else:
        return parse_text_registration(content)


def validate_registration(data):
    """
    Validate all required fields are present.
    Returns list of error strings (empty = valid).
    """
    errors = list(data.get('errors', []))

    if not data.get('game'):
        errors.append("Missing required field: game")
    if not data.get('player_name'):
        errors.append("Missing required field: player_name")
    if not data.get('email') or '@' not in data.get('email', ''):
        errors.append("Missing or invalid field: email")
    if not data.get('prefect_name'):
        errors.append("Missing required field: prefect_name")
    if not data.get('ship_name'):
        errors.append("Missing required field: ship_name")
    if not data.get('starbase'):
        errors.append("Missing required field: starbase")

    return errors
