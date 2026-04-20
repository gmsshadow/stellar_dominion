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


def wrap_log_line(text, indent="    ", width=REPORT_WIDTH):
    """
    Word-wrap a turn-log message line to fit within the report width.
    The first line gets the indent; continuation lines get an extra two
    spaces so they're visually subordinate.
    Returns a list of indented strings ready to append to lines[].
    """
    max_first = width - len(indent)
    max_cont = width - len(indent) - 2
    cont_indent = indent + "  "

    if len(text) <= max_first:
        return [f"{indent}{text}"]

    result = []
    words = text.split()
    current = ""
    for word in words:
        test = f"{current} {word}".strip() if current else word
        limit = max_first if not result else max_cont
        if len(test) <= limit:
            current = test
        else:
            if current:
                prefix = indent if not result else cont_indent
                result.append(f"{prefix}{current}")
            current = word
    if current:
        prefix = indent if not result else cont_indent
        result.append(f"{prefix}{current}")
    return result


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
    start_system_id = turn_result['system_id']
    system_id = turn_result.get('final_system_id', start_system_id)

    # Fetch additional data
    ship = conn.execute("SELECT * FROM ships WHERE ship_id = ?", (ship_id,)).fetchone()
    system = conn.execute("SELECT * FROM star_systems WHERE system_id = ?", (system_id,)).fetchone()
    start_system = conn.execute("SELECT * FROM star_systems WHERE system_id = ?", (start_system_id,)).fetchone()
    prefect = conn.execute(
        "SELECT * FROM prefects WHERE prefect_id = ?",
        (ship['owner_prefect_id'],)
    ).fetchone()
    officers = conn.execute("SELECT * FROM officers WHERE ship_id = ?", (ship_id,)).fetchall()
    installed = conn.execute("""
        SELECT ii.quantity, sc.component_id, sc.name, sc.category, sc.st_cost,
               sc.cargo_capacity, sc.crew_capacity, sc.life_capacity,
               sc.thrust, sc.engine_efficiency, sc.sensor_rating,
               sc.jump_range, sc.jump_oc_cost
        FROM installed_items ii
        JOIN ship_components sc ON ii.component_id = sc.component_id
        WHERE ii.ship_id = ?
        ORDER BY sc.category, sc.component_id
    """, (ship_id,)).fetchall()
    cargo = conn.execute(
        "SELECT * FROM cargo_items WHERE ship_id = ? AND item_type_id != 401", (ship_id,)
    ).fetchall()
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

    # Get landed body name with coordinates and terrain
    landed_name = None
    if turn_result.get('landed'):
        body = conn.execute(
            "SELECT * FROM celestial_bodies WHERE body_id = ?",
            (turn_result['landed'],)
        ).fetchone()
        if body:
            lx = turn_result.get('landed_x', 1)
            ly = turn_result.get('landed_y', 1)
            # Look up terrain at landing site
            terrain_row = conn.execute(
                "SELECT terrain_type FROM planet_surface WHERE body_id = ? AND x = ? AND y = ?",
                (turn_result['landed'], lx, ly)
            ).fetchone()
            terrain_str = f" - {terrain_row['terrain_type']}" if terrain_row else ""
            landed_name = (f"{body['body_type'].title()} {body['name']} ({body['body_id']}) "
                          f"[{body['gravity']}g] at ({lx},{ly}){terrain_str}")

    now = datetime.now()
    turn_str = f"{turn_result['turn_year']}.{turn_result['turn_week']}"
    start_loc = f"{turn_result['start_col']}{turn_result['start_row']:02d}"
    final_loc = f"{turn_result['final_col']}{turn_result['final_row']:02d}"
    faction = get_faction(conn, prefect['faction_id']) if prefect else {'abbreviation': 'IND', 'name': 'Independent'}
    faction_str = faction['name']
    display_name = faction_display_name(conn, ship_name, prefect['faction_id']) if prefect else ship_name

    # Look up player account number
    player = conn.execute(
        "SELECT account_number FROM players WHERE player_id = ?",
        (prefect['player_id'],)
    ).fetchone() if prefect else None
    account_number = player['account_number'] if player else '???'

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
    lines.append(center_text(f"Account: {account_number}"))
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
    start_landed = turn_result.get('start_landed')
    ss_name = start_system['name'] if start_system else system['name']
    ss_id = start_system_id
    if start_docked:
        start_base = conn.execute("SELECT * FROM starbases WHERE base_id = ?",
                                   (start_docked,)).fetchone()
        if start_base:
            lines.append(f"    Docked at {start_base['base_type']} {start_base['name']} "
                          f"({start_base['base_id']}) - {ss_name} System ({ss_id})")
        else:
            lines.append(f"    {start_loc} - {ss_name} System ({ss_id})")
    elif start_landed:
        start_body = conn.execute("SELECT * FROM celestial_bodies WHERE body_id = ?",
                                   (start_landed,)).fetchone()
        if start_body:
            slx = turn_result.get('start_landed_x', 1)
            sly = turn_result.get('start_landed_y', 1)
            terrain_row = conn.execute(
                "SELECT terrain_type FROM planet_surface WHERE body_id = ? AND x = ? AND y = ?",
                (start_landed, slx, sly)
            ).fetchone()
            terrain_str = f" - {terrain_row['terrain_type']}" if terrain_row else ""
            lines.append(f"    Landed on {start_body['body_type'].title()} {start_body['name']} "
                          f"({start_body['body_id']}) [{start_body['gravity']}g] "
                          f"at ({slx},{sly}){terrain_str} - {ss_name} System ({ss_id})")
        else:
            lines.append(f"    {start_loc} - {ss_name} System ({ss_id})")
    elif start_orbiting:
        start_body = conn.execute("SELECT * FROM celestial_bodies WHERE body_id = ?",
                                   (start_orbiting,)).fetchone()
        if start_body:
            lines.append(f"    {start_body['name']} ({start_body['body_id']}) [{start_body['gravity']}g] "
                          f"Orbit - {ss_name} System ({ss_id})")
        else:
            lines.append(f"    {start_loc} - {ss_name} System ({ss_id})")
    else:
        lines.append(f"    {start_loc} - {ss_name} System ({ss_id})")
    lines.append("")

    # Turn execution log
    for entry in turn_result['log']:
        cmd = entry['command']
        params = entry.get('params', '')
        tu_before = entry['tu_before']

        if params is not None and params != '':
            lines.append(f">OC {tu_before}: {cmd} {{{params}}}")
        else:
            lines.append(f">OC {tu_before}: {cmd}")

        # Indent and word-wrap the message (skip wrapping for map output - preserves grid alignment)
        if entry['command'] in ('SCANSURFACE', 'SCANSYSTEM', 'SURFACESCAN', 'SYSTEMSCAN'):
            for msg_line in entry['message'].split('\n'):
                lines.append(msg_line)
        else:
            for msg_line in entry['message'].split('\n'):
                lines.extend(wrap_log_line(msg_line))
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
    eff = ship['efficiency']
    eff_str = f"Efficiency: {eff:.0f}%"
    if eff < 100:
        penalty = 100 - eff
        eff_str += f" (+{penalty:.0f}% OC penalty)"
    lines.append(section_line(eff_str.ljust(COL_LEFT) +
                               f"OCs left: {turn_result['final_tu']}"))
    lines.append(section_line())

    ship_size = ship['ship_size'] if 'ship_size' in ship.keys() else ship['hull_count']
    st_capacity = ship_size * 50
    st_used = sum(c['st_cost'] * c['quantity'] for c in installed)
    # Engines: 1 per 10 ship size (min 1). Extra engines above optimal don't help (spares).
    engine_row = conn.execute("""
        SELECT COALESCE(SUM(ii.quantity), 0) AS engine_count
        FROM installed_items ii
        JOIN ship_components sc ON ii.component_id = sc.component_id
        WHERE ii.ship_id = ? AND sc.category = 'engine'
    """, (ship_id,)).fetchone()
    engine_count = int(engine_row['engine_count']) if engine_row else 0
    optimal_engines = max(1, ship_size // 10)
    engine_pct = 0
    if optimal_engines > 0 and engine_count > 0:
        engine_pct = int(min(100, round((engine_count / optimal_engines) * 100)))
    hull_info = f"Size: {ship_size} ({ship['hull_type']})"
    lines.append(section_line(f"Design: {ship['design']}"))
    integ_val = ship['integrity'] or 0
    max_integ = ship['max_integrity'] if 'max_integrity' in ship.keys() and ship['max_integrity'] else ship_size
    if not max_integ:
        max_integ = 1
    integ_pct = (integ_val / max_integ) * 100
    lines.append(section_line(f"{hull_info}".ljust(COL_LEFT) +
                               f"Integrity: {integ_pct:.0f}%"))
    lines.append(section_line(f"Internal: {st_used}/{st_capacity} ST".ljust(COL_LEFT) +
                               f"Gravity Rating: {ship['gravity_rating']:.1f}"))
    lines.append(section_line(f"Engines: {engine_count}/{optimal_engines} -> {engine_pct}%".ljust(COL_LEFT) +
                               "MOVE cost scales with engines"))
    lines.append(section_line())

    # ==========================================
    # NAVIGATION REPORT
    # ==========================================
    lines.append(section_header("Navigation Report"))
    lines.append(section_line())
    lines.append(section_line("LOCATION"))

    if docked_name:
        lines.append(section_line(f"Docked at {docked_name} - {system['name']} System ({system_id})"))
    elif landed_name:
        lines.append(section_line(f"Landed on {landed_name} - {system['name']} System ({system_id})"))
    elif orbiting_name:
        lines.append(section_line(f"Orbiting {orbiting_name} - {system['name']} System ({system_id})"))
    else:
        lines.append(section_line(f"{final_loc} - {system['name']} System ({system_id})"))

    lines.append(section_line(f"{system['name']} ({system_id}) - {{{final_loc}}}"))
    lines.append(section_line())
    sensor_profile = ship['sensor_profile'] if 'sensor_profile' in ship.keys() and ship['sensor_profile'] is not None else (ship['ship_size'] / 100.0 if 'ship_size' in ship.keys() else 0.5)
    lines.append(section_line(f"Sensor Rating: {ship['sensor_rating']}%".ljust(COL_LEFT) +
                               f"Cargo: {ship['cargo_used']}/{ship['cargo_capacity']} ST"))
    lines.append(section_line(f"Sensor Profile: {sensor_profile:.2f}".ljust(COL_LEFT) +
                               f"(detection signature)"))
    lines.append(section_line())

    # ==========================================
    # CREW REPORT
    # ==========================================
    lines.append(section_header("Crew Report"))
    lines.append(section_line())
    lines.append(section_line("OFFICERS"))
    if officers:
        for off in officers:
            wages = off['wages'] if 'wages' in off.keys() else 5
            rank_info = f"[ {off['specialty']} {off['experience']} Xp ] +{off['crew_factors']} CF  {wages} cr/wk"
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

    life_support = ship['life_support_capacity'] if 'life_support_capacity' in ship.keys() else 20
    crew_line = (f"Crew: {ship['crew_count']}/{life_support}".ljust(COL_LEFT) +
                 f"Required: {ship['crew_required']}")
    lines.append(section_line(crew_line))
    if ship['crew_count'] < ship['crew_required']:
        penalty = 100.0 - min(100.0, (ship['crew_count'] / max(1, ship['crew_required'])) * 100.0)
        lines.append(section_line(
            f"*** WARNING: Ship undermanned! +{penalty:.0f}% OC penalty on all actions. ***"
        ))
    lines.append(section_line())

    # ==========================================
    # CARGO REPORT
    # ==========================================
    lines.append(section_header("Cargo Report"))
    lines.append(section_line())
    lines.append(section_line(f"Cargo: {ship['cargo_used']}/{ship['cargo_capacity']} ST"))
    if cargo:
        for item in cargo:
            total_mu = item['quantity'] * item['mass_per_unit']
            lines.append(section_line(
                f"{item['quantity']:>8}  {item['item_name']} ({item['item_type_id']})"
                f" - {item['mass_per_unit']} ST each = {total_mu} ST"
            ))
    else:
        lines.append(section_line("Cargo hold empty."))
    lines.append(section_line())

    # ==========================================
    # SPACE COMBAT SUMMARY
    # ==========================================
    # Always show — players need doctrine visible. Defensive stats, weapons,
    # magazines, and PD only appear if the ship has them.
    from db.database import SHIELD_THICKNESS_FACTOR
    armour_val = ship['armour'] if 'armour' in ship.keys() and ship['armour'] else 0
    shield_sp = ship['shield_sp'] if 'shield_sp' in ship.keys() and ship['shield_sp'] is not None else 0
    max_shield = ship['max_shield_sp'] if 'max_shield_sp' in ship.keys() and ship['max_shield_sp'] else 0
    missiles = ship['missiles_loaded'] if 'missiles_loaded' in ship.keys() and ship['missiles_loaded'] is not None else 0
    max_missiles = ship['max_missiles'] if 'max_missiles' in ship.keys() and ship['max_missiles'] else 0
    torpedoes = ship['torpedoes_loaded'] if 'torpedoes_loaded' in ship.keys() and ship['torpedoes_loaded'] is not None else 0
    max_torpedoes = ship['max_torpedoes'] if 'max_torpedoes' in ship.keys() and ship['max_torpedoes'] else 0
    doctrine = (ship['combat_doctrine'] if 'combat_doctrine' in ship.keys() and ship['combat_doctrine'] else 'defensive')

    # Pull weapons and PD from installed_items + ship_components
    weapons_rows = conn.execute(
        """SELECT sc.name, sc.category, sc.weapon_damage, sc.weapon_range,
                  sc.weapon_shots_per_round, sc.weapon_accuracy,
                  sc.ammo_type, sc.flight_rounds, ii.quantity
           FROM installed_items ii
           JOIN ship_components sc ON ii.component_id = sc.component_id
           WHERE ii.ship_id = ? AND sc.category IN ('weapon', 'pd')
           ORDER BY sc.category, sc.name""",
        (ship['ship_id'],)
    ).fetchall()

    weapon_entries = [w for w in weapons_rows if w['category'] == 'weapon']
    pd_entries = [w for w in weapons_rows if w['category'] == 'pd']

    # Helper: format a float as "N" if integer or "N.NN" with trailing zeros stripped
    def _fmt_salvos(val):
        if val == int(val):
            return f"{int(val)}"
        # Up to 2 decimals, strip trailing zeros
        s = f"{val:.2f}".rstrip('0').rstrip('.')
        return s

    lines.append(section_header("Space Combat Summary"))
    lines.append(section_line())

    # Defences (if any)
    if armour_val > 0 or max_shield > 0:
        thickness = ((SHIELD_THICKNESS_FACTOR * shield_sp) // ship_size) if ship_size > 0 and shield_sp > 0 else 0
        lines.append(section_line("DEFENCES"))
        if armour_val > 0:
            lines.append(section_line(f"  Armour: {armour_val} (non-ablative, flat reduction per hit)"))
        if max_shield > 0:
            lines.append(section_line(
                f"  Shields: {shield_sp}/{max_shield} SP, thickness {thickness} "
                f"(absorbs up to {thickness} dmg/hit, ablates SP)"))
        lines.append(section_line())

    # Weapons (if any)
    if weapon_entries:
        lines.append(section_line("WEAPONS"))
        # Max content width is 74. Format: 2 indent + 26 name + 4 qty + 5 dmg + 5 rng + 5 acc + 5 shots + 20 notes = 72
        comp_fmt = "  {:<26s} {:>3s} {:>4s} {:>4s} {:>4s} {:>4s} {}"
        lines.append(section_line(comp_fmt.format(
            "Weapon", "Qty", "Dmg", "Rng", "Acc", "Shot", "Notes")))
        lines.append(section_line(comp_fmt.format(
            "-"*26, "-"*3, "-"*4, "-"*4, "-"*4, "-"*4, "-"*18)))
        for w in weapon_entries:
            qty = w['quantity']
            acc = w['weapon_accuracy'] if w['weapon_accuracy'] is not None else 1.0
            # Compact notes: "missile/1rd", "torp/2rd", or "instant"
            if w['ammo_type']:
                notes = f"{w['ammo_type']}/{w['flight_rounds']}rd"
            else:
                notes = "instant"
            lines.append(section_line(comp_fmt.format(
                w['name'][:26], str(qty),
                str(w['weapon_damage'] or 0),
                str(w['weapon_range'] or 0),
                f"{acc:.2f}",
                str((w['weapon_shots_per_round'] or 0) * qty),
                notes
            )))
        lines.append(section_line())

    # Ammunition (aggregate per ammo type — salvos at current launcher count)
    if max_missiles > 0 or max_torpedoes > 0:
        lines.append(section_line("AMMUNITION"))
        for ammo_label, loaded, max_cap in [
            ('Missiles', missiles, max_missiles),
            ('Torpedoes', torpedoes, max_torpedoes),
        ]:
            if max_cap <= 0:
                continue
            # Total launcher shots/round for this ammo type
            ammo_type_key = ammo_label.lower().rstrip('s')  # "missile" / "torpedoe" — strip 's'
            # Actually the ammo_type column uses 'missile'/'torpedo' — match properly
            if ammo_label == 'Missiles':
                ammo_type_key = 'missile'
            else:
                ammo_type_key = 'torpedo'
            shots_per_round = sum(
                (w['weapon_shots_per_round'] or 0) * w['quantity']
                for w in weapon_entries
                if w['ammo_type'] == ammo_type_key
            )
            if shots_per_round > 0:
                salvos = loaded / shots_per_round
                salvos_str = (
                    f"{_fmt_salvos(salvos)} salvo{'s' if salvos != 1 else ''} "
                    f"at {shots_per_round} shot{'s' if shots_per_round != 1 else ''}/round"
                )
            else:
                salvos_str = "no launchers installed to fire these"
            lines.append(section_line(
                f"  {ammo_label}: {loaded}/{max_cap} loaded — {salvos_str}"
            ))
        lines.append(section_line())

    # Point Defence (if any)
    if pd_entries:
        lines.append(section_line("POINT DEFENCE"))
        pd_fmt = "  {:<26s} {:>3s} {:>4s} {:>4s} {}"
        lines.append(section_line(pd_fmt.format(
            "Turret", "Qty", "Acc", "Shot", "Notes")))
        lines.append(section_line(pd_fmt.format(
            "-"*26, "-"*3, "-"*4, "-"*4, "-"*30)))
        total_pd_shots = 0
        for p in pd_entries:
            qty = p['quantity']
            acc = p['weapon_accuracy'] if p['weapon_accuracy'] is not None else 1.0
            shots = (p['weapon_shots_per_round'] or 0) * qty
            total_pd_shots += shots
            lines.append(section_line(pd_fmt.format(
                p['name'][:26], str(qty), f"{acc:.2f}", str(shots),
                "intercepts missiles/torpedoes"
            )))
        lines.append(section_line(
            f"  Total: {total_pd_shots} intercept shot"
            f"{'s' if total_pd_shots != 1 else ''}/round"
            " (torpedoes prioritised first)"
        ))
        lines.append(section_line())

    # Combat doctrine (always shown at end of summary)
    doctrine_descs = {
        'aggressive': "pursue and engage; retreat only below 25% integrity",
        'defensive':  "engage if attacked; retreat below 50% integrity",
        'evasive':    "prefer to flee; retreat below 75% integrity",
    }
    lines.append(section_line(
        f"COMBAT DOCTRINE: {doctrine.upper()}  "
        f"({doctrine_descs.get(doctrine, '')})"
    ))
    lines.append(section_line())

    # Combat lists (target / defend / avoid)
    list_rows = conn.execute(
        """SELECT list_type, entry_type, entry_id
           FROM ship_combat_lists
           WHERE game_id = ? AND ship_id = ?
           ORDER BY list_type, entry_type, entry_id""",
        (game_id, ship['ship_id'])
    ).fetchall()
    grouped = {'target': [], 'defend': [], 'avoid': []}
    for r in list_rows:
        if r['list_type'] in grouped:
            grouped[r['list_type']].append((r['entry_type'], r['entry_id']))

    def _describe_entry(conn, entry_type, entry_id):
        """Resolve an entry to 'Name (id)' form. Falls back to raw on lookup failure."""
        if entry_type == 'ship':
            r = conn.execute("SELECT name FROM ships WHERE ship_id = ?", (entry_id,)).fetchone()
            if r:
                return f"{r['name']} (ship {entry_id})"
            return f"ship {entry_id}"
        if entry_type == 'base':
            # Could be starbase, port, or outpost — try each
            for tbl, col in [('starbases', 'base_id'), ('surface_ports', 'port_id'),
                              ('outposts', 'outpost_id')]:
                r = conn.execute(f"SELECT name FROM {tbl} WHERE {col} = ?", (entry_id,)).fetchone()
                if r:
                    return f"{r['name']} (base {entry_id})"
            return f"base {entry_id}"
        if entry_type == 'faction':
            r = conn.execute("SELECT name FROM factions WHERE faction_id = ?", (entry_id,)).fetchone()
            if r:
                return f"{r['name']} (faction {entry_id})"
            return f"faction {entry_id}"
        return f"{entry_type} {entry_id}"

    lines.append(section_line("COMBAT LISTS"))
    for list_type in ('target', 'defend', 'avoid'):
        entries = grouped[list_type]
        if not entries:
            lines.append(section_line(f"  {list_type.upper()}: (empty)"))
        else:
            lines.append(section_line(f"  {list_type.upper()}:"))
            for entry_type, entry_id in entries:
                lines.append(section_line(f"    {_describe_entry(conn, entry_type, entry_id)}"))
    lines.append(section_line())

    # ==========================================
    # ==========================================
    # INSTALLED COMPONENTS
    # ==========================================
    lines.append(section_header("Ship Components"))
    lines.append(section_line(f"Internal Capacity: {st_used}/{st_capacity} ST ({st_capacity - st_used} ST free)"))
    lines.append(section_line())
    if installed:
        # Header
        comp_fmt = "{:<30s} {:>5s} {:>3s} {:>6s} {:>6s}"
        lines.append(section_line(comp_fmt.format("Component", "ID", "Qty", "Each", "Total")))
        lines.append(section_line(comp_fmt.format("-"*30, "-"*5, "-"*3, "-"*6, "-"*6)))
        for item in installed:
            qty = item['quantity']
            total_st = item['st_cost'] * qty
            lines.append(section_line(comp_fmt.format(
                item['name'][:30],
                str(item['component_id']),
                str(qty),
                f"{item['st_cost']} ST",
                f"{total_st} ST"
            )))
        lines.append(section_line())
    else:
        lines.append(section_line("No components installed."))
        lines.append(section_line())

    # ==========================================
    # ==========================================
    # CONTACTS
    # ==========================================
    lines.append(section_header("Contacts"))
    lines.append(section_line())
    if contacts:
        # Split contacts by whether they were seen this turn (passive sweep)
        # or are lingering entries from earlier turns
        current_year = turn_result.get('turn_year')
        current_week = turn_result.get('turn_week')
        current_contacts = [c for c in contacts
                             if c['discovered_turn_year'] == current_year
                             and c['discovered_turn_week'] == current_week]
        earlier_contacts = [c for c in contacts if c not in current_contacts]

        def _contact_line(c):
            loc = f"{c['location_col']}{c['location_row']:02d}"
            ctype = (c['object_type'] or '').lower()
            # Try to get the column even if it's missing in older rows
            try:
                hull_type = c['target_hull_type']
            except (IndexError, KeyError):
                hull_type = None
            try:
                ship_size = c['target_ship_size']
            except (IndexError, KeyError):
                ship_size = None
            try:
                drange = c['detection_range']
            except (IndexError, KeyError):
                drange = None

            if ctype == 'ship':
                size_str = f"Size {ship_size} " if ship_size else ""
                hull_str = f"{hull_type} Hull" if hull_type else ""
                parts = [f"{c['object_name']} ({c['object_id']})"]
                if size_str or hull_str:
                    parts.append(f"{size_str}{hull_str}".strip())
                parts.append(f"at {loc}")
                if drange is not None:
                    parts.append(f"(range {drange})")
                return "- " + " ".join(parts)
            else:
                kind = ctype.title() if ctype else "Object"
                return f"- {kind} {c['object_name']} ({c['object_id']}) at {loc}"

        if current_contacts:
            lines.append(section_line("Passive contacts this turn:"))
            for c in current_contacts:
                lines.append(section_line(_contact_line(c)))
            lines.append(section_line())
        if earlier_contacts:
            lines.append(section_line("Earlier contacts:"))
            for c in earlier_contacts:
                lines.append(section_line(_contact_line(c)))
    else:
        lines.append(section_line("No known contacts."))
    lines.append(section_line())

    # ==========================================
    # COMBAT
    # ==========================================
    current_year = turn_result.get('turn_year')
    current_week = turn_result.get('turn_week')
    # Find any engagements this ship participated in this turn
    combat_rows = conn.execute(
        """SELECT DISTINCT cl.engagement_id, ce.system_id, ce.grid_col, ce.grid_row,
                  ce.status, ce.resolution
           FROM combat_log cl
           JOIN combat_engagements ce ON cl.engagement_id = ce.engagement_id
           JOIN combat_participants cp ON cp.engagement_id = ce.engagement_id
           WHERE cp.participant_kind = 'ship' AND cp.participant_id_value = ?
             AND cl.turn_year = ? AND cl.turn_week = ?""",
        (ship_id, current_year, current_week)
    ).fetchall()
    if combat_rows:
        lines.append(section_header("Combat"))
        lines.append(section_line())
        for cr in combat_rows:
            eng_id = cr['engagement_id']
            status_label = cr['status'].upper() if cr['status'] else 'ACTIVE'
            lines.append(section_line(
                f"Engagement #{eng_id} at {cr['grid_col']}{cr['grid_row']:02d} "
                f"system {cr['system_id']} — status: {status_label}"
            ))
            if cr['resolution']:
                lines.append(section_line(f"  Resolution: {cr['resolution']}"))
            # Show participants and their final integrity
            parts = conn.execute(
                """SELECT participant_kind, participant_id_value, status, integrity_at_join, integrity_at_end
                   FROM combat_participants WHERE engagement_id = ?""",
                (eng_id,)
            ).fetchall()
            lines.append(section_line(f"  Participants:"))
            for p in parts:
                if p['participant_kind'] == 'ship':
                    nrow = conn.execute(
                        "SELECT name, integrity, max_integrity FROM ships WHERE ship_id = ?",
                        (p['participant_id_value'],)
                    ).fetchone()
                    pname = nrow['name'] if nrow else f"Ship {p['participant_id_value']}"
                    cur_integ = nrow['integrity'] if nrow else 0
                    max_integ = nrow['max_integrity'] if nrow and nrow['max_integrity'] else 100
                    integ_pct = (cur_integ / max_integ) * 100 if max_integ else 0
                    integ_str = f"integrity {cur_integ:.0f}/{max_integ:.0f} ({integ_pct:.0f}%)"
                else:
                    tbl_map = {'starbase': ('starbases', 'base_id'),
                               'port': ('surface_ports', 'port_id'),
                               'outpost': ('outposts', 'outpost_id')}
                    tbl, idcol = tbl_map.get(p['participant_kind'], (None, None))
                    nrow = conn.execute(
                        f"SELECT name FROM {tbl} WHERE {idcol} = ?",
                        (p['participant_id_value'],)
                    ).fetchone() if tbl else None
                    pname = nrow['name'] if nrow else f"{p['participant_kind'].title()} {p['participant_id_value']}"
                    integ_str = "integrity -"
                marker = ' (you)' if (p['participant_kind'] == 'ship'
                                        and p['participant_id_value'] == ship_id) else ''
                p_status = p['status'].upper() if p['status'] else 'ACTIVE'
                lines.append(section_line(
                    f"    {pname} ({p['participant_id_value']}){marker}"
                    f" - {integ_str}, status: {p_status}"
                ))
            # Show this ship's combat events round by round
            lines.append(section_line(f"  Combat log this turn:"))
            log_entries = conn.execute(
                """SELECT round_number, action, target_kind, target_id, damage,
                          integrity_after, detail
                   FROM combat_log
                   WHERE engagement_id = ?
                     AND turn_year = ? AND turn_week = ?
                     AND ((actor_kind = 'ship' AND actor_id = ?)
                          OR action IN ('engage', 'destroyed'))
                   ORDER BY log_id""",
                (eng_id, current_year, current_week, ship_id)
            ).fetchall()
            for le in log_entries:
                rn = le['round_number']
                act = le['action']
                detail = le['detail'] or ''
                lines.append(section_line(f"    R{rn}: {detail}"))
            lines.append(section_line())
    # ==========================================
    # OVERFLOW ORDERS (carry forward to next turn)
    # ==========================================
    lines.append(section_header("Overflow Orders"))
    lines.append(section_line())
    overflow = turn_result.get('overflow', [])
    if overflow:
        lines.append(section_line("The following orders will run automatically next turn"))
        lines.append(section_line("(submit CLEAR as first order to cancel them):"))
        lines.append(section_line())
        for i, ov in enumerate(overflow, 1):
            params_str = ""
            if ov.get('params'):
                p = ov['params']
                if isinstance(p, dict) and 'col' in p:
                    params_str = f" {{{p['col']}{p['row']:02d}}}"
                elif isinstance(p, dict) and 'target_id' in p and 'text' in p:
                    params_str = f" {{{p['target_id']} ...}}"
                else:
                    params_str = f" {{{p}}}"
            lines.append(section_line(
                f"{i:>3}. {ov['command']}{params_str}"
            ))
    else:
        lines.append(section_line("No overflow orders."))
    lines.append(section_line())

    # ==========================================
    # FOOTER
    # ==========================================
    lines.append(section_close())
    lines.append("")
    lines.append(center_text("=== END REPORT ==="))

    conn.close()
    return "\n".join(lines)


def generate_base_report(base_type, base_id, db_path=None, game_id="OMICRON101",
                         order_results=None):
    """
    Generate a turn report for a starbase, surface port, or outpost.
    base_type: 'starbase', 'port', or 'outpost'
    order_results: list of (command, params, result_message) tuples or None
    """
    from db.database import (get_installed_modules, recalculate_base_stats)

    conn = get_connection(db_path)
    game = conn.execute("SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()
    turn_year = game['current_year']
    turn_week = game['current_week']
    now_str = datetime.now().strftime("%d %B %Y")

    # Load base data
    if base_type == 'starbase':
        base = conn.execute("SELECT * FROM starbases WHERE base_id = ?", (base_id,)).fetchone()
        base_name = base['name']
        id_field = base['base_id']
        system = conn.execute("SELECT name FROM star_systems WHERE system_id = ?",
                               (base['system_id'],)).fetchone()
        location_str = f"{base['grid_col']}{base['grid_row']:02d} - {system['name']} System ({base['system_id']})"
        if base['orbiting_body_id']:
            body = conn.execute("SELECT name FROM celestial_bodies WHERE body_id = ?",
                                 (base['orbiting_body_id'],)).fetchone()
            location_str += f"\nOrbiting {body['name']} ({base['orbiting_body_id']})" if body else ""
    elif base_type == 'port':
        base = conn.execute("SELECT * FROM surface_ports WHERE port_id = ?", (base_id,)).fetchone()
        base_name = base['name']
        id_field = base['port_id']
        body = conn.execute("SELECT cb.*, ss.name as system_name, ss.system_id FROM celestial_bodies cb JOIN star_systems ss ON cb.system_id = ss.system_id WHERE body_id = ?",
                             (base['body_id'],)).fetchone()
        location_str = f"{body['name']} ({base['body_id']}) at ({base['surface_x']},{base['surface_y']})"
        location_str += f" - {body['system_name']} System ({body['system_id']})"
    else:  # outpost
        base = conn.execute("SELECT * FROM outposts WHERE outpost_id = ?", (base_id,)).fetchone()
        base_name = base['name']
        id_field = base['outpost_id']
        body = conn.execute("SELECT cb.*, ss.name as system_name, ss.system_id FROM celestial_bodies cb JOIN star_systems ss ON cb.system_id = ss.system_id WHERE body_id = ?",
                             (base['body_id'],)).fetchone()
        location_str = f"{body['name']} ({base['body_id']}) at ({base['surface_x']},{base['surface_y']})"
        location_str += f" - {body['system_name']} System ({body['system_id']})"

    # Faction from owner
    faction_str = "Unaffiliated"
    faction_abbr = ""
    if base['owner_prefect_id']:
        prefect = conn.execute("SELECT * FROM prefects WHERE prefect_id = ?",
                                (base['owner_prefect_id'],)).fetchone()
        if prefect:
            faction = get_faction(conn, prefect['faction_id'])
            if faction:
                faction_str = faction['name']
                faction_abbr = faction['abbreviation']

    # Get stats from modules
    kwargs = {'starbase_id': base_id} if base_type == 'starbase' else \
             {'port_id': base_id} if base_type == 'port' else \
             {'outpost_id': base_id}
    stats = recalculate_base_stats(conn, **kwargs)
    modules = get_installed_modules(conn, **kwargs)

    # Type label for display
    type_labels = {'starbase': 'STARBASE', 'port': 'SURFACE PORT', 'outpost': 'OUTPOST'}
    type_label = type_labels.get(base_type, base_type.upper())
    display_name = f"{faction_abbr} {type_label}: {base_name}" if faction_abbr else f"{type_label}: {base_name}"

    lines = []
    lines.append(center_text("=== BEGIN REPORT ==="))
    lines.append("")
    lines.append(center_text("Stellar Dominion"))
    lines.append(center_text("PBEM Strategy Game"))
    lines.append("")
    lines.append(center_text(f"{display_name} ({id_field})"))
    lines.append("")
    lines.append(f"Printed on {now_str}, Star Date {turn_year}.{turn_week}")
    lines.append("")

    # ==========================================
    # TURN ORDERS (if any)
    # ==========================================
    if order_results:
        lines.append(section_header("Turn Orders"))
        lines.append(section_line())
        for cmd, params, result_msg in order_results:
            params_str = ""
            if isinstance(params, dict):
                parts = [f"{k}={v}" for k, v in params.items()]
                params_str = " {" + ", ".join(parts) + "}"
            lines.append(section_line(f"> {cmd}{params_str}"))
            for rline in result_msg.split('\n'):
                lines.append(section_line(f"    {rline}"))
            lines.append(section_line())

    # ==========================================
    # STATUS BLOCK
    # ==========================================
    lines.append(section_header("Status Report"))
    lines.append(section_line())

    lines.append(section_line(f"Name: {base_name} ({id_field})"))
    lines.append(section_line(f"Faction: {faction_str}".ljust(COL_LEFT) +
                               f"Efficiency: {stats['overall_efficiency']}%"))
    lines.append(section_line(f"Type: {type_label}"))
    lines.append(section_line())

    # Location
    for loc_line in location_str.split('\n'):
        lines.append(section_line(loc_line))
    lines.append(section_line())

    # Employees
    lines.append(section_line(f"Employees: {stats['employees']}/{stats['employees_required']} required -> {stats['employee_pct']}%"))
    lines.append(section_line(f"Employee Capacity: {stats['employee_capacity']}".ljust(COL_LEFT) +
                               f"Command: {stats['command_count']}/{stats['command_required']} -> {stats['command_pct']}%"))
    if stats['docking_capacity']:
        lines.append(section_line(f"Docking Slots: {stats['docking_capacity']}"))
    if stats['storage_capacity']:
        # Calculate used storage
        inv_col = 'starbase_id' if base_type == 'starbase' else 'port_id' if base_type == 'port' else 'outpost_id'
        storage_used = conn.execute(
            f"SELECT COALESCE(SUM(quantity * mass_per_unit), 0) FROM base_inventory WHERE {inv_col} = ?",
            (base_id,)
        ).fetchone()[0]
        lines.append(section_line(f"Storage: {storage_used}/{stats['storage_capacity']} ST"))
    if stats['mining_capacity']:
        lines.append(section_line(f"Mining Capacity: {stats['mining_capacity']}"))
    if stats['factory_capacity']:
        lines.append(section_line(f"Factory Capacity: {stats['factory_capacity']}"))
    if stats['repair_capacity']:
        lines.append(section_line(f"Repair Capacity: {stats['repair_capacity']}"))
    if stats['defence_rating']:
        lines.append(section_line(f"Defence Rating: {stats['defence_rating']}"))
    lines.append(section_line(f"Sensor Profile: {stats.get('sensor_profile', 1.0):.2f}".ljust(COL_LEFT) +
                               f"(detection signature)"))
    lines.append(section_line(f"Sensor Rating:  {stats.get('sensor_rating', 0)}".ljust(COL_LEFT) +
                               f"(scan strength)"))
    lines.append(section_line())

    # ==========================================
    # MODULES
    # ==========================================
    lines.append(section_header("Installed Modules"))
    lines.append(section_line())
    if modules:
        mod_fmt = "{:<28s} {:>5s} {:>3s} {:>5s}"
        lines.append(section_line(mod_fmt.format("Module", "ID", "Qty", "Emp")))
        lines.append(section_line(mod_fmt.format("-"*28, "-"*5, "-"*3, "-"*5)))
        for m in modules:
            lines.append(section_line(mod_fmt.format(
                m['name'][:28], str(m['module_id']), str(m['quantity']),
                str(m['employees_required'] * m['quantity']))))
        lines.append(section_line())
        lines.append(section_line(f"Total Modules: {stats['total_modules']}"))
    else:
        lines.append(section_line("No modules installed."))
    lines.append(section_line())

    # ==========================================
    # COMBAT LISTS (target + defend; no avoid for bases)
    # ==========================================
    base_kind = base_type  # 'starbase' | 'port' | 'outpost'
    list_rows = conn.execute(
        """SELECT list_type, entry_type, entry_id
           FROM base_combat_lists
           WHERE game_id = ? AND base_kind = ? AND base_id = ?
           ORDER BY list_type, entry_type, entry_id""",
        (game_id, base_kind, base_id)
    ).fetchall()
    grouped = {'target': [], 'defend': []}
    for r in list_rows:
        if r['list_type'] in grouped:
            grouped[r['list_type']].append((r['entry_type'], r['entry_id']))

    def _describe_entry(conn, entry_type, entry_id):
        if entry_type == 'ship':
            r = conn.execute("SELECT name FROM ships WHERE ship_id = ?", (entry_id,)).fetchone()
            return f"{r['name']} (ship {entry_id})" if r else f"ship {entry_id}"
        if entry_type == 'base':
            for tbl, col in [('starbases', 'base_id'), ('surface_ports', 'port_id'),
                              ('outposts', 'outpost_id')]:
                r = conn.execute(f"SELECT name FROM {tbl} WHERE {col} = ?", (entry_id,)).fetchone()
                if r:
                    return f"{r['name']} (base {entry_id})"
            return f"base {entry_id}"
        if entry_type == 'faction':
            r = conn.execute("SELECT name FROM factions WHERE faction_id = ?", (entry_id,)).fetchone()
            return f"{r['name']} (faction {entry_id})" if r else f"faction {entry_id}"
        return f"{entry_type} {entry_id}"

    lines.append(section_header("Combat Lists"))
    lines.append(section_line())
    for list_type in ('target', 'defend'):
        entries = grouped[list_type]
        if not entries:
            lines.append(section_line(f"  {list_type.upper()}: (empty)"))
        else:
            lines.append(section_line(f"  {list_type.upper()}:"))
            for entry_type, entry_id in entries:
                lines.append(section_line(f"    {_describe_entry(conn, entry_type, entry_id)}"))
    lines.append(section_line())

    # ==========================================
    # MARKET REPORT (starbases and ports only)
    # ==========================================
    if base_type in ('starbase', 'port'):
        lines.append(section_header("Market Report"))
        lines.append(section_line())

        # Get market data - for starbases use base_id, for ports look up linked starbase
        market_base_id = None
        if base_type == 'starbase':
            market_base_id = base_id
        elif base_type == 'port':
            # Check if a starbase is linked to this port
            linked_base = conn.execute(
                "SELECT base_id FROM starbases WHERE surface_port_id = ?", (base_id,)
            ).fetchone()
            if linked_base:
                market_base_id = linked_base['base_id']

        if market_base_id:
            from engine.game_setup import get_market_weeks_remaining
            # Determine market cycle
            cycle_length = 4
            cycle_week = ((game['current_week'] - 1) // cycle_length) * cycle_length + 1
            cycle_year = game['current_year']

            prices = conn.execute("""
                SELECT mp.*, tg.name as item_name, tg.mass_per_unit,
                       btc.trade_role
                FROM market_prices mp
                JOIN trade_goods tg ON mp.item_id = tg.item_id
                JOIN base_trade_config btc ON mp.base_id = btc.base_id
                    AND mp.item_id = btc.item_id AND btc.game_id = mp.game_id
                WHERE mp.game_id = ? AND mp.base_id = ?
                AND mp.turn_year = ? AND mp.turn_week = ?
                ORDER BY mp.item_id
            """, (game_id, market_base_id, cycle_year, cycle_week)).fetchall()

            if prices:
                role_labels = {'produces': 'Supply', 'average': 'Std', 'demands': 'Demand'}
                weeks_left = get_market_weeks_remaining(game['current_week'])
                mkt_fmt = "{:<26s} {:>6s} {:>5s} {:>5s} {:>5s} {:>5s} {}"
                lines.append(section_line(mkt_fmt.format('Item', 'ID', 'Buy', 'Sell', 'Stk', 'Dmd', 'Role')))
                lines.append(section_line(mkt_fmt.format('-'*26, '-'*6, '-'*5, '-'*5, '-'*5, '-'*5, '------')))
                for p in prices:
                    lines.append(section_line(mkt_fmt.format(
                        p['item_name'][:26], str(p['item_id']),
                        f"{p['buy_price']:.0f}", f"{p['sell_price']:.0f}",
                        str(p['stock']), str(p['demand']),
                        role_labels.get(p['trade_role'], '')
                    )))
                lines.append(section_line())
                if weeks_left <= 1:
                    lines.append(section_line("Market refreshes next week."))
                else:
                    lines.append(section_line(f"{weeks_left} weeks to market refresh."))
            else:
                lines.append(section_line("No market data available."))
        else:
            lines.append(section_line("No market configured."))
        lines.append(section_line())

    # ==========================================
    # INVENTORY
    # ==========================================
    lines.append(section_header("Inventory"))
    lines.append(section_line())

    inv_col = 'starbase_id' if base_type == 'starbase' else 'port_id' if base_type == 'port' else 'outpost_id'
    inventory = conn.execute(
        f"SELECT * FROM base_inventory WHERE {inv_col} = ? AND quantity > 0 ORDER BY item_name",
        (base_id,)
    ).fetchall()

    if inventory:
        inv_fmt = "{:<30s} {:>6s} {:>5s} {:>8s}"
        lines.append(section_line(inv_fmt.format("Item", "ID", "Qty", "Mass")))
        lines.append(section_line(inv_fmt.format("-"*30, "-"*6, "-"*5, "-"*8)))
        total_mass = 0
        for item in inventory:
            mass = item['quantity'] * item['mass_per_unit']
            total_mass += mass
            lines.append(section_line(inv_fmt.format(
                item['item_name'][:30], str(item['item_type_id']),
                str(item['quantity']), f"{mass} ST"
            )))
        lines.append(section_line())
        if stats['storage_capacity']:
            lines.append(section_line(f"Storage Used: {total_mass}/{stats['storage_capacity']} ST"))
        else:
            lines.append(section_line(f"Total Mass: {total_mass} ST"))
    else:
        lines.append(section_line("Inventory empty."))
    lines.append(section_line())

    # ==========================================
    lines.append(section_close())
    lines.append("")
    lines.append(center_text("=== END REPORT ==="))

    conn.close()
    return "\n".join(lines)


def generate_prefect_report(prefect_id, db_path=None, game_id="OMICRON101",
                            between_turn_messages=None, trade_summary=None):
    """
    Generate a prefect turn report.
    
    trade_summary: {ship_id: {'income': N, 'expenses': N, 'trades': [...]}}
    """
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
    lines.append(center_text(f"Account: {player['account_number']}"))
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
        else:
            lines.append(section_line(f"Aboard ship {prefect['location_id']} (not found)"))
    elif prefect['location_type'] == 'starbase':
        loc_base = conn.execute(
            "SELECT b.*, ss.name as system_name FROM starbases b "
            "JOIN star_systems ss ON b.system_id = ss.system_id WHERE b.base_id = ?",
            (prefect['location_id'],)
        ).fetchone()
        if loc_base:
            lines.append(section_line(
                f"At {loc_base['name']} ({loc_base['base_id']}) - "
                f"{loc_base['system_name']} System ({loc_base['system_id']})"
            ))
        else:
            lines.append(section_line(f"At starbase {prefect['location_id']} (not found)"))
    elif prefect['location_type'] == 'surface_port':
        loc_port = conn.execute("SELECT * FROM surface_ports WHERE port_id = ?",
                                 (prefect['location_id'],)).fetchone()
        if loc_port:
            body = conn.execute(
                "SELECT cb.name, ss.name as system_name, ss.system_id "
                "FROM celestial_bodies cb JOIN star_systems ss ON cb.system_id = ss.system_id "
                "WHERE cb.body_id = ?", (loc_port['body_id'],)
            ).fetchone()
            loc_str = f"At {loc_port['name']} ({loc_port['port_id']})"
            if body:
                loc_str += f" on {body['name']} - {body['system_name']} System ({body['system_id']})"
            lines.append(section_line(loc_str))
        else:
            lines.append(section_line(f"At surface port {prefect['location_id']} (not found)"))
    elif prefect['location_type'] == 'outpost':
        loc_op = conn.execute("SELECT * FROM outposts WHERE outpost_id = ?",
                               (prefect['location_id'],)).fetchone()
        if loc_op:
            body = conn.execute(
                "SELECT cb.name, ss.name as system_name, ss.system_id "
                "FROM celestial_bodies cb JOIN star_systems ss ON cb.system_id = ss.system_id "
                "WHERE cb.body_id = ?", (loc_op['body_id'],)
            ).fetchone()
            loc_str = f"At {loc_op['name']} ({loc_op['outpost_id']})"
            if body:
                loc_str += f" on {body['name']} - {body['system_name']} System ({body['system_id']})"
            lines.append(section_line(loc_str))
        else:
            lines.append(section_line(f"At outpost {prefect['location_id']} (not found)"))
    elif prefect['location_type'] == 'none' or not prefect['location_type']:
        lines.append(section_line("No assigned location"))
    else:
        lines.append(section_line(f"Unknown location: {prefect['location_type']} {prefect['location_id']}"))
    lines.append(section_line())

    # ==========================================
    # FINANCIAL REPORT
    # ==========================================
    lines.append(section_header("Financial Report"))
    lines.append(section_line())
    lines.append(section_line(
        f"{'POSITION':<38} {'INCOME':>8}  {'EXPENSES':>8}  {'NET':>8}"
    ))

    if trade_summary is None:
        trade_summary = {}

    total_income = 0
    total_expenses = 0
    for s in ships:
        ship_trade = trade_summary.get(s['ship_id'], {})
        income = ship_trade.get('income', 0)
        expenses = ship_trade.get('expenses', 0)
        net = income - expenses
        total_income += income
        total_expenses += expenses
        ship_display = faction_display_name(conn, s['name'], prefect['faction_id'])
        lines.append(section_line(
            f"{ship_display} ({s['ship_id']})".ljust(38) +
            f"{income:>8,}  {expenses:>8,}  {net:>8,}"
        ))

    lines.append(section_line(
        f"{'':38} {'-------':>8}  {'-------':>8}  {'-------':>8}"
    ))
    total_net = total_income - total_expenses
    lines.append(section_line(
        f"{'':38} {total_income:>8,}  {total_expenses:>8,}  "
        f"{total_net:>8,}"
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
            f"OC: {s['tu_remaining']}/{s['tu_per_turn']}  "
            f"Size: {s['ship_size'] if 'ship_size' in s.keys() else s['hull_count']} ({s['hull_type']})"
        ))
        ls = s['life_support_capacity'] if 'life_support_capacity' in s.keys() else 20
        crew_status = ""
        if s['crew_count'] < s['crew_required']:
            crew_status = " [UNDERMANNED]"
        lines.append(section_line(
            f"   Crew: {s['crew_count']}/{ls}".ljust(COL_LEFT) +
            f"Required: {s['crew_required']}{crew_status}"
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
