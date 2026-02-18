"""
Stellar Dominion - Report Generator
Generates Phoenix-BSE-style ASCII turn reports for email delivery.
"""

from datetime import datetime
from db.database import get_connection, get_faction, faction_display_name, get_faction_for_prefect


REPORT_WIDTH = 78
HEADER_CHAR = '-'
SECTION_CHAR = '-'
BOX_INNER = REPORT_WIDTH - 2  # chars between the | pipes
CONTENT_WIDTH = BOX_INNER - 2 # usable content area (1-char margin each side)
COL_LEFT = 35                  # two-column layout: left column width


def center_text(text, width=REPORT_WIDTH):
    """Center text within the report width."""
    return text.center(width)


def section_header(title, char=SECTION_CHAR, width=REPORT_WIDTH):
    """Generate a centered section header bar: |---------- Title ----------|"""
    inner_width = width - 2
    centered = f" {title} ".center(inner_width, char)
    return f"|{centered}|"


def section_line(content="", width=REPORT_WIDTH):
    """
    Generate bordered line(s): | content                          |
    
    If content exceeds the available width, word-wraps onto continuation
    lines with a 3-space indent. Returns a single string that may contain
    embedded newlines.
    """
    max_content = width - 4  # 1 margin + 1 pipe each side

    if len(content) <= max_content:
        return f"| {content}{' ' * (max_content - len(content))} |"

    # Word-wrap: split into lines that fit
    indent = "   "  # continuation indent
    result_lines = []
    words = content.split()
    current = ""

    for word in words:
        test = f"{current} {word}".strip() if current else word
        limit = max_content if not result_lines else max_content - len(indent)
        if len(test) <= limit:
            current = test
        else:
            # Flush current line
            if current:
                pad = max_content - len(current) if not result_lines else max_content - len(indent) - len(current)
                if result_lines:
                    result_lines.append(f"| {indent}{current}{' ' * max(0, pad)} |")
                else:
                    result_lines.append(f"| {current}{' ' * max(0, pad)} |")
            current = word

    # Flush last line
    if current:
        if result_lines:
            pad = max_content - len(indent) - len(current)
            result_lines.append(f"| {indent}{current}{' ' * max(0, pad)} |")
        else:
            pad = max_content - len(current)
            result_lines.append(f"| {current}{' ' * max(0, pad)} |")

    return "\n".join(result_lines)


def section_close(width=REPORT_WIDTH):
    """Generate a section closing bar."""
    return "|" + SECTION_CHAR * (width - 2) + "|"


def generate_ship_report(turn_result, db_path=None, game_id="OMICRON101",
                         between_turn_messages=None):
    """
    Generate a full Phoenix-style turn report for a ship.
    
    turn_result: dict from TurnResolver.resolve_ship_turn()
    between_turn_messages: optional list of strings to show in between-turn section
    """
    conn = get_connection(db_path)

    ship_id = turn_result['ship_id']
    ship_name = turn_result['ship_name']
    system_id = turn_result['system_id']

    # Fetch additional data
    ship = conn.execute("SELECT * FROM ships WHERE ship_id = ?", (ship_id,)).fetchone()
    system = conn.execute("SELECT * FROM star_systems WHERE system_id = ?", (system_id,)).fetchone()
    prefect = conn.execute(
        "SELECT * FROM prefects WHERE prefect_id = ?",
        (ship['owner_prefect_id'],)
    ).fetchone()
    officers = conn.execute("SELECT * FROM officers WHERE ship_id = ?", (ship_id,)).fetchall()
    installed = conn.execute("SELECT * FROM installed_items WHERE ship_id = ?", (ship_id,)).fetchall()
    cargo = conn.execute("SELECT * FROM cargo_items WHERE ship_id = ?", (ship_id,)).fetchall()
    contacts = conn.execute(
        "SELECT * FROM known_contacts WHERE prefect_id = ? AND location_system = ?",
        (ship['owner_prefect_id'], system_id)
    ).fetchall()

    # Get base name if docked
    docked_name = None
    if turn_result['docked_at']:
        base = conn.execute(
            "SELECT * FROM starbases WHERE base_id = ?",
            (turn_result['docked_at'],)
        ).fetchone()
        if base:
            docked_name = f"{base['base_type']} {base['name']} ({base['base_id']})"

    # Get orbiting body name
    orbiting_name = None
    if turn_result['orbiting']:
        body = conn.execute(
            "SELECT * FROM celestial_bodies WHERE body_id = ?",
            (turn_result['orbiting'],)
        ).fetchone()
        if body:
            orbiting_name = f"{body['name']} ({body['body_id']}) [{body['gravity']}g]"

    now = datetime.now()
    turn_str = f"{turn_result['turn_year']}.{turn_result['turn_week']}"
    start_loc = f"{turn_result['start_col']}{turn_result['start_row']:02d}"
    final_loc = f"{turn_result['final_col']}{turn_result['final_row']:02d}"
    faction = get_faction(conn, prefect['faction_id']) if prefect else {'abbreviation': 'IND', 'name': 'Independent'}
    faction_str = faction['name']
    display_name = faction_display_name(conn, ship_name, prefect['faction_id']) if prefect else ship_name

    lines = []

    # ==========================================
    # REPORT HEADER
    # ==========================================
    lines.append(center_text("=== BEGIN REPORT ==="))
    lines.append("")
    lines.append(center_text("Stellar Dominion"))
    lines.append(center_text("PBEM Strategy Game"))
    lines.append("")
    lines.append(center_text(f"{faction['abbreviation']} SHIP {ship_name} ({ship_id})"))
    lines.append("")
    lines.append(f"Printed on {now.strftime('%d %B %Y')}, Star Date {turn_str}")
    lines.append("")

    # ==========================================
    # BETWEEN TURN REPORT (scans from environment)
    # ==========================================
    lines.append(HEADER_CHAR * REPORT_WIDTH)
    lines.append(center_text("BETWEEN TURN REPORT"))
    lines.append(HEADER_CHAR * REPORT_WIDTH)
    lines.append("")
    if between_turn_messages:
        for msg in between_turn_messages:
            lines.append(msg)
        lines.append("")
    else:
        lines.append("No between-turn events.")
        lines.append("")

    # ==========================================
    # TURN REPORT
    # ==========================================
    lines.append(HEADER_CHAR * REPORT_WIDTH)
    lines.append(center_text("TURN REPORT"))
    lines.append(HEADER_CHAR * REPORT_WIDTH)
    lines.append("")
    lines.append(f"Starting Location:")
    # Check starting orbit/dock state (from before turn resolution)
    start_orbiting = turn_result.get('start_orbiting')
    start_docked = turn_result.get('start_docked')
    if start_docked:
        start_base = conn.execute("SELECT * FROM starbases WHERE base_id = ?",
                                   (start_docked,)).fetchone()
        if start_base:
            lines.append(f"    Docked at {start_base['base_type']} {start_base['name']} "
                          f"({start_base['base_id']}) - {system['name']} System ({system_id})")
        else:
            lines.append(f"    {start_loc} - {system['name']} System ({system_id})")
    elif start_orbiting:
        start_body = conn.execute("SELECT * FROM celestial_bodies WHERE body_id = ?",
                                   (start_orbiting,)).fetchone()
        if start_body:
            lines.append(f"    {start_body['name']} ({start_body['body_id']}) [{start_body['gravity']}g] "
                          f"Orbit - {system['name']} System ({system_id})")
        else:
            lines.append(f"    {start_loc} - {system['name']} System ({system_id})")
    else:
        lines.append(f"    {start_loc} - {system['name']} System ({system_id})")
    lines.append("")

    # Turn execution log
    for entry in turn_result['log']:
        cmd = entry['command']
        params = entry.get('params', '')
        tu_before = entry['tu_before']

        if params is not None and params != '':
            lines.append(f">TU {tu_before}: {cmd} {{{params}}}")
        else:
            lines.append(f">TU {tu_before}: {cmd}")

        # Indent the message
        for msg_line in entry['message'].split('\n'):
            lines.append(f"    {msg_line}")
        lines.append("")

    # ==========================================
    # COMMAND REPORT
    # ==========================================
    lines.append(section_header("Command Report"))
    lines.append(section_line())
    lines.append(section_line(f"Name: {display_name} ({ship_id})".ljust(COL_LEFT) +
                               f"Faction: {faction_str}"))
    lines.append(section_line(f"Wealth: {prefect['credits']:,.0f} Credits".ljust(COL_LEFT) +
                               "Ownership: Player owned"))
    lines.append(section_line(f"Efficiency: {ship['efficiency']:.0f}%".ljust(COL_LEFT) +
                               f"TUs left: {turn_result['final_tu']} tus"))
    lines.append(section_line())

    hull_info = f"Size: {ship['hull_count']} ({ship['hull_type']})"
    lines.append(section_line(f"Design: {ship['design']}"))
    lines.append(section_line(f"{hull_info}".ljust(COL_LEFT) +
                               f"Integrity: {ship['integrity']:.0f}%"))
    lines.append(section_line())

    # ==========================================
    # NAVIGATION REPORT
    # ==========================================
    lines.append(section_header("Navigation Report"))
    lines.append(section_line())
    lines.append(section_line("LOCATION"))

    if docked_name:
        lines.append(section_line(f"Docked at {docked_name} - {system['name']} System ({system_id})"))
    elif orbiting_name:
        lines.append(section_line(f"Orbiting {orbiting_name} - {system['name']} System ({system_id})"))
    else:
        lines.append(section_line(f"{final_loc} - {system['name']} System ({system_id})"))

    lines.append(section_line(f"{system['name']} ({system_id}) - {{{final_loc}}}"))
    lines.append(section_line())
    lines.append(section_line(f"Sensor Rating: {ship['sensor_rating']}%".ljust(COL_LEFT) +
                               f"Cargo: {ship['cargo_used']}/{ship['cargo_capacity']}"))
    lines.append(section_line())

    # ==========================================
    # CREW REPORT
    # ==========================================
    lines.append(section_header("Crew Report"))
    lines.append(section_line())
    lines.append(section_line("OFFICERS"))
    if officers:
        for off in officers:
            rank_info = f"[ {off['specialty']} {off['experience']} Xp ] +{off['crew_factors']} CF"
            lines.append(section_line(
                f"[{off['crew_number']}] {off['rank']} {off['name']}".ljust(45) + rank_info
            ))
    else:
        lines.append(section_line("No officers assigned."))
    lines.append(section_line())

    # Prefect (not an officer -- listed separately)
    if prefect:
        lines.append(section_line("PREFECT"))
        lines.append(section_line(f"{prefect['name']} ({prefect['prefect_id']})"))
        lines.append(section_line())

    crew_line = f"Crew: {ship['crew_count']}".ljust(COL_LEFT) + f"Required: {ship['crew_required']}"
    lines.append(section_line(crew_line))
    lines.append(section_line())

    # ==========================================
    # CARGO REPORT
    # ==========================================
    lines.append(section_header("Cargo Report"))
    lines.append(section_line())
    lines.append(section_line(f"Cargo: {ship['cargo_used']}/{ship['cargo_capacity']}"))
    if cargo:
        for item in cargo:
            lines.append(section_line(
                f"{item['quantity']:>8}  {item['item_name']} - {item['mass_per_unit']} mus"
            ))
    else:
        lines.append(section_line("Cargo hold empty."))
    lines.append(section_line())

    # ==========================================
    # COMBAT REPORT (placeholder)
    # ==========================================
    lines.append(section_header("Space Combat Report"))
    lines.append(section_line())
    lines.append(section_line("No combat this turn."))
    lines.append(section_line())

    # ==========================================
    # INSTALLED ITEMS
    # ==========================================
    lines.append(section_header("Installed Items"))
    lines.append(section_line())
    if installed:
        for item in installed:
            lines.append(section_line(
                f"{item['quantity']:>8}  {item['item_name']} - {item['mass_per_unit']} mus"
            ))
    else:
        lines.append(section_line("No items installed."))
    lines.append(section_line())

    # ==========================================
    # CONTACTS
    # ==========================================
    lines.append(section_header("Contacts"))
    lines.append(section_line())
    if contacts:
        for c in contacts:
            loc = f"{c['location_col']}{c['location_row']:02d}"
            lines.append(section_line(
                f"- {c['object_type'].title()} {c['object_name']} ({c['object_id']}) at {loc}"
            ))
    else:
        lines.append(section_line("No known contacts."))
    lines.append(section_line())

    # ==========================================
    # PENDING ORDERS
    # ==========================================
    lines.append(section_header("Pending Orders"))
    lines.append(section_line())
    if turn_result['pending']:
        for i, pend in enumerate(turn_result['pending'], 1):
            params_str = f" {{{pend['params']}}}" if pend['params'] else ""
            lines.append(section_line(
                f"{i:>3}. {pend['command']}{params_str}"
            ))
            if pend.get('reason'):
                lines.append(section_line(f"     Reason: {pend['reason']}"))
    else:
        lines.append(section_line("No pending orders."))
    lines.append(section_line())

    # ==========================================
    # FOOTER
    # ==========================================
    lines.append(section_close())
    lines.append("")
    lines.append(center_text("=== END REPORT ==="))

    conn.close()
    return "\n".join(lines)


def generate_prefect_report(prefect_id, db_path=None, game_id="OMICRON101",
                            between_turn_messages=None):
    """Generate a prefect turn report."""
    conn = get_connection(db_path)

    prefect = conn.execute(
        "SELECT * FROM prefects WHERE prefect_id = ?",
        (prefect_id,)
    ).fetchone()
    if not prefect:
        conn.close()
        return "Error: Prefect position not found."

    player = conn.execute(
        "SELECT * FROM players WHERE player_id = ?",
        (prefect['player_id'],)
    ).fetchone()

    game = conn.execute(
        "SELECT * FROM games WHERE game_id = ?", (game_id,)
    ).fetchone()

    # Get all ships owned by this prefect
    ships = conn.execute(
        "SELECT s.*, ss.name as system_name FROM ships s "
        "JOIN star_systems ss ON s.system_id = ss.system_id "
        "WHERE s.owner_prefect_id = ? AND s.game_id = ?",
        (prefect_id, game_id)
    ).fetchall()

    now = datetime.now()
    turn_str = f"{game['current_year']}.{game['current_week']}"
    faction = get_faction(conn, prefect['faction_id'])
    faction_str = faction['name']

    lines = []
    lines.append(center_text("=== BEGIN REPORT ==="))
    lines.append("")
    lines.append(center_text("Stellar Dominion"))
    lines.append(center_text("PBEM Strategy Game"))
    lines.append("")
    lines.append(center_text(f"{faction['abbreviation']} PREFECT {prefect['name']} ({prefect_id})"))
    lines.append("")
    lines.append(f"Printed on {now.strftime('%d %B %Y')}, Star Date {turn_str}")
    lines.append("")

    # ==========================================
    # BETWEEN TURN REPORT
    # ==========================================
    if between_turn_messages:
        lines.append(HEADER_CHAR * REPORT_WIDTH)
        lines.append(center_text("BETWEEN TURN REPORT"))
        lines.append(HEADER_CHAR * REPORT_WIDTH)
        lines.append("")
        for msg in between_turn_messages:
            lines.append(msg)
        lines.append("")

    # ==========================================
    # TURN REPORT
    # ==========================================
    lines.append(HEADER_CHAR * REPORT_WIDTH)
    lines.append(center_text("TURN REPORT"))
    lines.append(HEADER_CHAR * REPORT_WIDTH)
    lines.append("")

    # ==========================================
    # PLAYER REPORTS
    # ==========================================
    lines.append(HEADER_CHAR * REPORT_WIDTH)
    lines.append(center_text("PLAYER REPORTS"))
    lines.append(HEADER_CHAR * REPORT_WIDTH)

    # Prefect summary
    lines.append(section_header("Prefect Report"))
    lines.append(section_line())
    lines.append(section_line(
        f"Name: {prefect['name']} ({prefect_id})".ljust(COL_LEFT) +
        f"Faction: {faction_str}"
    ))
    lines.append(section_line(
        f"Rank: {prefect['rank']}".ljust(COL_LEFT) +
        f"Influence: {prefect['influence']}"
    ))
    lines.append(section_line(
        f"Created: {prefect['created_turn_year']}.{prefect['created_turn_week']}"
    ))
    lines.append(section_line())

    # Location
    lines.append(section_line("LOCATION"))
    if prefect['location_type'] == 'ship':
        loc_ship = conn.execute(
            "SELECT s.*, ss.name as system_name FROM ships s "
            "JOIN star_systems ss ON s.system_id = ss.system_id WHERE s.ship_id = ?",
            (prefect['location_id'],)
        ).fetchone()
        if loc_ship:
            ship_display = faction_display_name(conn, loc_ship['name'], prefect['faction_id'])
            if loc_ship['docked_at_base_id']:
                base = conn.execute("SELECT * FROM starbases WHERE base_id = ?",
                                     (loc_ship['docked_at_base_id'],)).fetchone()
                lines.append(section_line(
                    f"Aboard {ship_display} ({loc_ship['ship_id']}), "
                    f"Docked at {base['name']} ({base['base_id']}) - "
                    f"{loc_ship['system_name']} System ({loc_ship['system_id']})"
                ))
            else:
                lines.append(section_line(
                    f"Aboard {ship_display} ({loc_ship['ship_id']}) - "
                    f"{loc_ship['system_name']} System ({loc_ship['system_id']})"
                ))
    lines.append(section_line())

    # ==========================================
    # FINANCIAL REPORT
    # ==========================================
    lines.append(section_header("Financial Report"))
    lines.append(section_line())
    lines.append(section_line(
        f"{'POSITION':<38} {'INCOME':>8}  {'EXPENSES':>8}  {'NET':>8}"
    ))

    total_income = 0
    total_expenses = 0
    for s in ships:
        income = 0
        expenses = 0
        net = income - expenses
        total_income += income
        total_expenses += expenses
        ship_display = faction_display_name(conn, s['name'], prefect['faction_id'])
        lines.append(section_line(
            f"{ship_display} ({s['ship_id']})".ljust(38) +
            f"{income:>8}  {expenses:>8}  {net:>8}"
        ))

    lines.append(section_line(
        f"{'':38} {'-------':>8}  {'-------':>8}  {'-------':>8}"
    ))
    lines.append(section_line(
        f"{'':38} {total_income:>8}  {total_expenses:>8}  "
        f"{total_income - total_expenses:>8}"
    ))
    lines.append(section_line())
    lines.append(section_line(f"Wealth: {prefect['credits']:,.0f} Credits"))
    lines.append(section_line())

    # ==========================================
    # SHIPS REPORT
    # ==========================================
    lines.append(section_header("Ships"))
    lines.append(section_line())
    for s in ships:
        loc = f"{s['grid_col']}{s['grid_row']:02d}"
        ship_display = faction_display_name(conn, s['name'], prefect['faction_id'])
        dock_info = ""
        if s['docked_at_base_id']:
            base = conn.execute("SELECT name FROM starbases WHERE base_id = ?",
                                 (s['docked_at_base_id'],)).fetchone()
            dock_info = f" [Docked at {base['name']}]" if base else " [Docked]"

        lines.append(section_line(
            f"{ship_display} ({s['ship_id']})".ljust(COL_LEFT) +
            f"{s['system_name']} ({s['system_id']}) {loc}{dock_info}"
        ))
        lines.append(section_line(
            f"   {s['design']}".ljust(COL_LEFT) +
            f"TU: {s['tu_remaining']}/{s['tu_per_turn']}  "
            f"Size: {s['hull_count']} ({s['hull_type']})"
        ))
        lines.append(section_line())

    # ==========================================
    # KNOWN ITEMS / CONTACTS
    # ==========================================
    contacts = conn.execute(
        "SELECT * FROM known_contacts WHERE prefect_id = ? ORDER BY object_type, object_name",
        (prefect_id,)
    ).fetchall()

    lines.append(section_header("Known Contacts"))
    lines.append(section_line())
    if contacts:
        current_type = None
        for c in contacts:
            if c['object_type'] != current_type:
                current_type = c['object_type']
                lines.append(section_line(f"{current_type.upper()}S:"))
            loc = f"{c['location_col']}{c['location_row']:02d}"
            lines.append(section_line(
                f"  {c['object_name']} ({c['object_id']}) at {loc}"
            ))
    else:
        lines.append(section_line("No known contacts."))
    lines.append(section_line())

    lines.append(section_close())
    lines.append("")
    lines.append(center_text("=== END REPORT ==="))

    conn.close()
    return "\n".join(lines)
