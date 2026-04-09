"""
Planet surface terrain generation.

Generates a terrain grid for planets and moons based on their
physical properties (temperature, atmosphere, tectonic activity,
hydrosphere, life). Grid size is per-body (surface_size column).
Deterministic -- seeded by body_id.

Terrain types (21):
  Water:       Shallows, Sea
  Cold:        Ice, Tundra
  Temperate:   Grassland, Plains, Forest, Jungle
  Wet:         Swamp, Marsh
  Elevated:    Hills, Mountains
  Barren:      Rock, Dust, Crater, Desert, Volcanic
  Civilisation: Cultivated, Ruin, Urban
  Gas:         Gas (gas giants only)
"""

import random

DEFAULT_GRID_SIZE = 31
MAX_GRID_SIZE = 50

# ASCII symbols for surface map
TERRAIN_SYMBOLS = {
    'Shallows':   '~',
    'Sea':        'S',
    'Ice':        '#',
    'Tundra':     ':',
    'Grassland':  '"',
    'Plains':     '.',
    'Forest':     'T',
    'Jungle':     '&',
    'Swamp':      '%',
    'Marsh':      ';',
    'Hills':      '^',
    'Mountains':  'A',
    'Rock':       '_',
    'Dust':       ',',
    'Crater':     'o',
    'Volcanic':   '!',
    'Desert':     '=',
    'Cultivated': '+',
    'Ruin':       '?',
    'Urban':      '@',
    'Gas':        '*',
}


def _get_size(body):
    """Extract grid size from body, clamped to valid range."""
    try:
        size = body['surface_size']
    except (KeyError, TypeError):
        size = None
    if not size or size < 5:
        bt = (body.get('body_type') or 'planet').lower()
        if bt == 'gas_giant':
            size = 50
        elif bt == 'moon':
            size = 15
        elif bt == 'asteroid':
            size = 11
        else:
            size = DEFAULT_GRID_SIZE
    return min(size, MAX_GRID_SIZE)


def generate_surface(body):
    """
    Generate a terrain grid for a celestial body.

    Grid size comes from body['surface_size'] (defaults by body_type).

    body: sqlite3.Row or dict with keys:
        body_id, body_type, temperature, atmosphere, tectonic_activity,
        hydrosphere, life, (optional) surface_size

    Returns: list of (x, y, terrain_type) tuples (1-indexed)
    """
    body_id = body['body_id']
    body_type = (body['body_type'] or 'planet').lower()
    temp = body['temperature'] or 300
    atmo = (body['atmosphere'] or 'None').lower()
    tectonic = body['tectonic_activity'] or 0
    hydro = body['hydrosphere'] or 0
    life = (body['life'] or 'None').lower()

    GS = _get_size(body)
    rng = random.Random(body_id)

    # Gas giants: entire surface is Gas
    if body_type == 'gas_giant':
        tiles = []
        for y in range(1, GS + 1):
            for x in range(1, GS + 1):
                tiles.append((x, y, 'Gas'))
        return tiles

    # Initialise grid (1-indexed, stored as [y][x])
    grid = [[None for _ in range(GS + 1)] for _ in range(GS + 1)]

    # --- Temperature classification ---
    frozen = temp < 150
    cold = 150 <= temp < 230
    temperate = 230 <= temp < 310
    hot = 310 <= temp < 380
    scorching = temp >= 380

    has_atmo = atmo not in ('none', 'trace', 'hydrogen', 'helium')
    has_breathable = atmo in ('standard', 'dense', 'oxygen,nitrogen')
    has_life = life not in ('none',)
    has_vegetation = life in ('plant', 'animal', 'sentient')
    has_civilisation = life == 'sentient'

    # --- Layer 1: Water placement ---
    raw = [[rng.random() for _ in range(GS + 1)] for _ in range(GS + 1)]
    for _pass in range(3):
        new = [[0.0] * (GS + 1) for _ in range(GS + 1)]
        for y in range(1, GS + 1):
            for x in range(1, GS + 1):
                total = raw[y][x] * 2
                count = 2
                for dy, dx in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                    ny, nx = y + dy, x + dx
                    if 1 <= ny <= GS and 1 <= nx <= GS:
                        total += raw[ny][nx]
                        count += 1
                new[y][x] = total / count
        raw = new

    values = []
    for y in range(1, GS + 1):
        for x in range(1, GS + 1):
            values.append(raw[y][x])
    values.sort()

    water_cells = max(0, min(100, hydro))
    if water_cells > 0:
        threshold_idx = int(len(values) * water_cells / 100)
        threshold_idx = min(threshold_idx, len(values) - 1)
        water_threshold = values[threshold_idx]
    else:
        water_threshold = -1

    for y in range(1, GS + 1):
        for x in range(1, GS + 1):
            if raw[y][x] <= water_threshold:
                is_edge = False
                for dy, dx in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                    ny, nx = y + dy, x + dx
                    if 1 <= ny <= GS and 1 <= nx <= GS:
                        if raw[ny][nx] > water_threshold:
                            is_edge = True
                            break
                    else:
                        is_edge = True
                        break
                grid[y][x] = 'Shallows' if is_edge else 'Sea'

    # --- Layer 2: Ice ---
    if frozen:
        for y in range(1, GS + 1):
            for x in range(1, GS + 1):
                if grid[y][x] in ('Sea', 'Shallows'):
                    grid[y][x] = 'Ice'
    elif cold:
        for y in range(1, GS + 1):
            for x in range(1, GS + 1):
                lat = abs(y - (GS + 1) / 2) / ((GS + 1) / 2)
                if grid[y][x] in ('Sea', 'Shallows'):
                    if rng.random() < 0.6 + lat * 0.4:
                        grid[y][x] = 'Ice'
    elif temperate:
        for y in range(1, GS + 1):
            for x in range(1, GS + 1):
                lat = abs(y - (GS + 1) / 2) / ((GS + 1) / 2)
                if lat > 0.8 and grid[y][x] in ('Sea', 'Shallows'):
                    if rng.random() < (lat - 0.8) * 5:
                        grid[y][x] = 'Ice'

    # --- Layer 3: Elevation ---
    if tectonic > 0:
        num_chains = tectonic // 2 + 1
        for _ in range(num_chains):
            cx, cy = rng.randint(1, GS), rng.randint(1, GS)
            chain_len = rng.randint(3, 5 + tectonic)
            dx, dy = rng.choice([(1, 0), (0, 1), (1, 1), (1, -1)])
            for step in range(chain_len):
                if 1 <= cx <= GS and 1 <= cy <= GS:
                    if grid[cy][cx] is None:
                        grid[cy][cx] = 'Mountains'
                        for fy, fx in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                            hy, hx = cy + fy, cx + fx
                            if (1 <= hx <= GS and 1 <= hy <= GS
                                    and grid[hy][hx] is None and rng.random() < 0.4):
                                grid[hy][hx] = 'Hills'
                cx += dx + rng.randint(-1, 1)
                cy += dy + rng.randint(-1, 1)
                cx = max(1, min(GS, cx))
                cy = max(1, min(GS, cy))

        num_volcanic = tectonic // 2
        for _ in range(num_volcanic):
            vx, vy = rng.randint(1, GS), rng.randint(1, GS)
            if grid[vy][vx] is None or grid[vy][vx] == 'Mountains':
                grid[vy][vx] = 'Volcanic'
                for _ in range(rng.randint(1, 3)):
                    sx = vx + rng.randint(-2, 2)
                    sy = vy + rng.randint(-2, 2)
                    if (1 <= sx <= GS and 1 <= sy <= GS
                            and grid[sy][sx] is None and rng.random() < 0.5):
                        grid[sy][sx] = 'Volcanic'

    # --- Layer 4: Craters ---
    if atmo in ('none', 'trace', 'thin'):
        num_craters = rng.randint(3, 8)
    else:
        num_craters = rng.randint(0, 2)

    for _ in range(num_craters):
        cx, cy = rng.randint(2, max(2, GS - 1)), rng.randint(2, max(2, GS - 1))
        radius = rng.randint(1, 2)
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if dx * dx + dy * dy <= radius * radius:
                    px, py = cx + dx, cy + dy
                    if 1 <= px <= GS and 1 <= py <= GS:
                        if grid[py][px] is None:
                            if abs(dx) == radius or abs(dy) == radius:
                                grid[py][px] = 'Crater'
                            elif rng.random() < 0.3:
                                grid[py][px] = 'Crater'

    # --- Layer 5: Fill remaining land with biome ---
    for y in range(1, GS + 1):
        for x in range(1, GS + 1):
            if grid[y][x] is not None:
                continue

            lat = abs(y - (GS + 1) / 2) / ((GS + 1) / 2)

            nearby_water = 0
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    ny, nx = y + dy, x + dx
                    if 1 <= ny <= GS and 1 <= nx <= GS:
                        if grid[ny][nx] in ('Sea', 'Shallows', 'Ice'):
                            nearby_water += 1
            moisture = min(1.0, nearby_water / 12)

            if frozen:
                r = rng.random()
                if r < 0.5:
                    grid[y][x] = 'Ice'
                elif r < 0.7 and has_atmo:
                    grid[y][x] = 'Tundra'
                else:
                    grid[y][x] = 'Rock'

            elif cold:
                r = rng.random()
                if lat > 0.7:
                    grid[y][x] = 'Ice' if r < 0.5 else 'Tundra'
                elif moisture > 0.3 and has_atmo:
                    if has_vegetation and r < 0.3:
                        grid[y][x] = 'Forest'
                    elif r < 0.5:
                        grid[y][x] = 'Tundra'
                    else:
                        grid[y][x] = 'Plains'
                else:
                    grid[y][x] = rng.choice(['Rock', 'Dust', 'Tundra'] if has_atmo else ['Rock', 'Dust'])

            elif temperate:
                r = rng.random()
                if lat > 0.85:
                    grid[y][x] = 'Tundra' if has_atmo else 'Ice'
                elif moisture > 0.5 and has_breathable:
                    if has_vegetation:
                        choices = ['Forest', 'Forest', 'Grassland', 'Swamp', 'Marsh']
                        grid[y][x] = rng.choice(choices)
                    else:
                        grid[y][x] = rng.choice(['Plains', 'Grassland', 'Marsh'])
                elif moisture > 0.2 and has_atmo:
                    if has_vegetation:
                        grid[y][x] = rng.choice(['Grassland', 'Plains', 'Forest', 'Plains'])
                    else:
                        grid[y][x] = rng.choice(['Plains', 'Grassland', 'Rock'])
                else:
                    if has_atmo:
                        grid[y][x] = rng.choice(['Plains', 'Dust', 'Rock', 'Desert'])
                    else:
                        grid[y][x] = rng.choice(['Rock', 'Dust', 'Desert'])

            elif hot:
                r = rng.random()
                if moisture > 0.4 and has_breathable:
                    if has_vegetation:
                        grid[y][x] = rng.choice(['Jungle', 'Jungle', 'Swamp', 'Forest', 'Marsh'])
                    else:
                        grid[y][x] = rng.choice(['Marsh', 'Swamp', 'Plains'])
                elif moisture > 0.1 and has_atmo:
                    if has_vegetation:
                        grid[y][x] = rng.choice(['Jungle', 'Grassland', 'Desert', 'Plains'])
                    else:
                        grid[y][x] = rng.choice(['Desert', 'Plains', 'Rock'])
                else:
                    grid[y][x] = rng.choice(['Desert', 'Desert', 'Rock', 'Dust'])

            else:  # scorching
                grid[y][x] = rng.choice(['Rock', 'Dust', 'Volcanic', 'Desert', 'Crater'])

    # --- Layer 6: Civilisation ---
    if has_civilisation:
        habitable = {'Grassland', 'Plains', 'Forest', 'Cultivated', 'Hills'}
        num_cities = rng.randint(2, max(2, GS // 6))
        for _ in range(num_cities):
            attempts = 0
            margin = max(3, GS // 10)
            while attempts < 50:
                ux = rng.randint(margin, GS - margin + 1)
                uy = rng.randint(margin, GS - margin + 1)
                if grid[uy][ux] in habitable:
                    water_near = any(
                        grid[uy + dy][ux + dx] in ('Sea', 'Shallows')
                        for dy in range(-3, 4) for dx in range(-3, 4)
                        if 1 <= uy + dy <= GS and 1 <= ux + dx <= GS
                    )
                    if water_near or rng.random() < 0.3:
                        grid[uy][ux] = 'Urban'
                        for dy in range(-2, 3):
                            for dx in range(-2, 3):
                                cy, cx = uy + dy, ux + dx
                                if (1 <= cx <= GS and 1 <= cy <= GS
                                        and grid[cy][cx] in habitable
                                        and rng.random() < 0.5):
                                    grid[cy][cx] = 'Cultivated'
                        break
                attempts += 1

        num_ruins = rng.randint(1, max(1, GS // 10))
        for _ in range(num_ruins):
            rx, ry = rng.randint(1, GS), rng.randint(1, GS)
            if grid[ry][rx] in habitable or grid[ry][rx] in ('Desert', 'Dust', 'Rock'):
                grid[ry][rx] = 'Ruin'

    # Build result tuples
    tiles = []
    for y in range(1, GS + 1):
        for x in range(1, GS + 1):
            terrain = grid[y][x] or 'Rock'
            tiles.append((x, y, terrain))

    return tiles


def render_surface_map(tiles, body_name, body_id, planetary_data=None, ship_pos=None,
                       port_positions=None, outpost_positions=None):
    """
    Render a surface map as ASCII text. Grid size is auto-detected from tiles.

    tiles: list of (x, y, terrain_type)
    planetary_data: dict with temperature, gravity, atmosphere, etc.
    ship_pos: tuple (x, y) of ship location to mark with X, or None
    port_positions: list of (x, y, name) tuples for surface ports (shown in data, not on map)
    outpost_positions: list of (x, y, name, type) tuples — shown in data, NOT on map

    Returns: list of strings (lines)
    """
    # Auto-detect grid size from tile coordinates
    GS = max(max(t[0] for t in tiles), max(t[1] for t in tiles)) if tiles else DEFAULT_GRID_SIZE

    # Build grid lookup
    grid = {}
    for x, y, terrain in tiles:
        grid[(x, y)] = terrain

    lines = []

    # Title
    lines.append(f"Surface Map: {body_name} ({body_id}) [{GS}x{GS}]")
    lines.append("")

    # Column headers
    col_nums_top = "     "
    for x in range(1, GS + 1):
        col_nums_top += f"{x:>2} "
    lines.append(col_nums_top.rstrip())

    # Grid rows (top = y=max, bottom = y=1 like Phoenix)
    for y in range(GS, 0, -1):
        row_str = f"{y:>3}  "
        for x in range(1, GS + 1):
            if ship_pos and ship_pos[0] == x and ship_pos[1] == y:
                row_str += " X "
            else:
                terrain = grid.get((x, y), 'Rock')
                symbol = TERRAIN_SYMBOLS.get(terrain, '?')
                row_str += f" {symbol} "
        row_str += f" {y:>2}"
        lines.append(row_str)

    # Bottom column headers
    lines.append(col_nums_top.rstrip())

    # Planetary data block
    if planetary_data:
        lines.append("")
        lines.append("Planetary Data:")
        pd = planetary_data
        lines.append(f"  Gravity: {pd.get('gravity', '?')}g"
                     f"          Temperature: {pd.get('temperature', '?')}K"
                     f"      Atmosphere: {pd.get('atmosphere', '?')}")
        lines.append(f"  Tectonic Activity: {pd.get('tectonic_activity', '?')}"
                     f"    Hydrosphere: {pd.get('hydrosphere', '?')}%"
                     f"        Life: {pd.get('life', '?')}")
        lines.append(f"  Surface Size: {GS}x{GS}")

    # Ship position
    if ship_pos:
        terrain = grid.get(ship_pos, 'Unknown')
        lines.append(f"  Ship Position: ({ship_pos[0]},{ship_pos[1]}) - {terrain}")

    # Surface port info
    if port_positions:
        for px, py, pname in port_positions:
            terrain = grid.get((px, py), 'Unknown')
            lines.append(f"  Surface Port: {pname} at ({px},{py}) - {terrain}")

    # Outpost info (not shown on map due to small size)
    if outpost_positions:
        for ox, oy, oname, otype in outpost_positions:
            terrain = grid.get((ox, oy), 'Unknown')
            lines.append(f"  Outpost: {oname} [{otype}] at ({ox},{oy}) - {terrain}")

    # Legend
    lines.append("")
    lines.append("Terrain Key:")
    symbols = list(TERRAIN_SYMBOLS.items())
    for i in range(0, len(symbols), 4):
        row_items = symbols[i:i + 4]
        row_str = "  " + "  ".join(f"{sym} {name:<12}" for name, sym in row_items)
        lines.append(row_str)
    if ship_pos:
        lines.append("  X Ship         ")

    return lines


def store_surface(conn, body_id, tiles):
    """Store generated surface tiles to database."""
    conn.execute("DELETE FROM planet_surface WHERE body_id = ?", (body_id,))
    conn.executemany(
        "INSERT INTO planet_surface (body_id, x, y, terrain_type) VALUES (?, ?, ?, ?)",
        [(body_id, x, y, t) for x, y, t in tiles]
    )
    conn.commit()


def get_or_generate_surface(conn, body):
    """Get surface from DB, or generate and store it."""
    existing = conn.execute(
        "SELECT x, y, terrain_type FROM planet_surface WHERE body_id = ? ORDER BY y, x",
        (body['body_id'],)
    ).fetchall()

    if existing:
        return [(r['x'], r['y'], r['terrain_type']) for r in existing]

    tiles = generate_surface(body)
    store_surface(conn, body['body_id'], tiles)
    return tiles
