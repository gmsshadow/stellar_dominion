"""
Planet surface terrain generation.

Generates a 31x31 terrain grid for planets and moons based on their
physical properties (temperature, atmosphere, tectonic activity,
hydrosphere, life). Deterministic -- seeded by body_id.

Terrain types (20):
  Water:       Shallows, Sea
  Cold:        Ice, Tundra
  Temperate:   Grassland, Plains, Forest, Jungle
  Wet:         Swamp, Marsh
  Elevated:    Hills, Mountains
  Barren:      Rock, Dust, Crater, Desert, Volcanic
  Civilisation: Cultivated, Ruin, Urban
"""

import random

GRID_SIZE = 31

# ASCII symbols for surface map
TERRAIN_SYMBOLS = {
    'Shallows':   '~',
    'Sea':        '\u2248',   # ≈
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


def generate_surface(body):
    """
    Generate a 31x31 terrain grid for a celestial body.
    
    body: sqlite3.Row or dict with keys:
        body_id, temperature, atmosphere, tectonic_activity,
        hydrosphere, life
    
    Returns: list of (x, y, terrain_type) tuples (1-indexed)
    """
    body_id = body['body_id']
    body_type = (body['body_type'] or 'planet').lower()
    temp = body['temperature'] or 300
    atmo = (body['atmosphere'] or 'None').lower()
    tectonic = body['tectonic_activity'] or 0
    hydro = body['hydrosphere'] or 0
    life = (body['life'] or 'None').lower()
    
    rng = random.Random(body_id)
    
    # Gas giants: entire surface is Gas
    if body_type == 'gas_giant':
        tiles = []
        for y in range(1, GRID_SIZE + 1):
            for x in range(1, GRID_SIZE + 1):
                tiles.append((x, y, 'Gas'))
        return tiles
    
    # Initialise grid (1-indexed, stored as [y][x])
    grid = [[None for _ in range(GRID_SIZE + 1)] for _ in range(GRID_SIZE + 1)]
    
    # --- Temperature classification ---
    # < 150K: frozen, 150-230K: cold, 230-310K: temperate, 310-380K: hot, > 380K: scorching
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
    # Generate a heightmap-like value per cell using random + neighbour smoothing
    raw = [[rng.random() for _ in range(GRID_SIZE + 1)] for _ in range(GRID_SIZE + 1)]
    # Simple 2-pass smooth for natural-looking continents
    for _pass in range(3):
        new = [[0.0] * (GRID_SIZE + 1) for _ in range(GRID_SIZE + 1)]
        for y in range(1, GRID_SIZE + 1):
            for x in range(1, GRID_SIZE + 1):
                total = raw[y][x] * 2
                count = 2
                for dy, dx in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                    ny, nx = y + dy, x + dx
                    if 1 <= ny <= GRID_SIZE and 1 <= nx <= GRID_SIZE:
                        total += raw[ny][nx]
                        count += 1
                new[y][x] = total / count
        raw = new
    
    # Flatten to sorted list to determine water threshold
    values = []
    for y in range(1, GRID_SIZE + 1):
        for x in range(1, GRID_SIZE + 1):
            values.append(raw[y][x])
    values.sort()
    
    water_cells = max(0, min(100, hydro))
    if water_cells > 0:
        threshold_idx = int(len(values) * water_cells / 100)
        threshold_idx = min(threshold_idx, len(values) - 1)
        water_threshold = values[threshold_idx]
    else:
        water_threshold = -1  # No water
    
    # Place water
    for y in range(1, GRID_SIZE + 1):
        for x in range(1, GRID_SIZE + 1):
            if raw[y][x] <= water_threshold:
                # Edges of water bodies are shallows
                is_edge = False
                for dy, dx in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                    ny, nx = y + dy, x + dx
                    if 1 <= ny <= GRID_SIZE and 1 <= nx <= GRID_SIZE:
                        if raw[ny][nx] > water_threshold:
                            is_edge = True
                            break
                    else:
                        is_edge = True
                        break
                grid[y][x] = 'Shallows' if is_edge else 'Sea'
    
    # --- Layer 2: Ice (frozen/cold worlds, or polar regions) ---
    if frozen:
        # Entire surface frozen -- convert water to Ice, land will be Ice/Tundra
        for y in range(1, GRID_SIZE + 1):
            for x in range(1, GRID_SIZE + 1):
                if grid[y][x] in ('Sea', 'Shallows'):
                    grid[y][x] = 'Ice'
    elif cold:
        # Heavy ice coverage -- most water frozen, polar land is tundra
        for y in range(1, GRID_SIZE + 1):
            for x in range(1, GRID_SIZE + 1):
                # Latitude-based (y=1 and y=31 are poles)
                lat = abs(y - (GRID_SIZE + 1) / 2) / ((GRID_SIZE + 1) / 2)
                if grid[y][x] in ('Sea', 'Shallows'):
                    if rng.random() < 0.6 + lat * 0.4:
                        grid[y][x] = 'Ice'
    elif temperate:
        # Polar ice caps only
        for y in range(1, GRID_SIZE + 1):
            for x in range(1, GRID_SIZE + 1):
                lat = abs(y - (GRID_SIZE + 1) / 2) / ((GRID_SIZE + 1) / 2)
                if lat > 0.8 and grid[y][x] in ('Sea', 'Shallows'):
                    if rng.random() < (lat - 0.8) * 5:
                        grid[y][x] = 'Ice'
    # Hot/scorching worlds: no ice
    
    # --- Layer 3: Elevation (mountains, hills, volcanic) ---
    # Tectonic activity 0-10 drives mountain/volcanic frequency
    if tectonic > 0:
        # Generate mountain chains using random walks
        num_chains = tectonic // 2 + 1
        for _ in range(num_chains):
            cx, cy = rng.randint(1, GRID_SIZE), rng.randint(1, GRID_SIZE)
            chain_len = rng.randint(3, 5 + tectonic)
            dx, dy = rng.choice([(1, 0), (0, 1), (1, 1), (1, -1)])
            for step in range(chain_len):
                if 1 <= cx <= GRID_SIZE and 1 <= cy <= GRID_SIZE:
                    if grid[cy][cx] is None:  # Don't overwrite water/ice
                        grid[cy][cx] = 'Mountains'
                        # Hills on flanks
                        for fy, fx in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                            hy, hx = cy + fy, cx + fx
                            if (1 <= hx <= GRID_SIZE and 1 <= hy <= GRID_SIZE
                                    and grid[hy][hx] is None and rng.random() < 0.4):
                                grid[hy][hx] = 'Hills'
                cx += dx + rng.randint(-1, 1)
                cy += dy + rng.randint(-1, 1)
                cx = max(1, min(GRID_SIZE, cx))
                cy = max(1, min(GRID_SIZE, cy))
        
        # Volcanic spots (more with higher tectonic activity)
        num_volcanic = tectonic // 2
        for _ in range(num_volcanic):
            vx, vy = rng.randint(1, GRID_SIZE), rng.randint(1, GRID_SIZE)
            if grid[vy][vx] is None or grid[vy][vx] == 'Mountains':
                grid[vy][vx] = 'Volcanic'
                # Scatter some nearby
                for _ in range(rng.randint(1, 3)):
                    sx = vx + rng.randint(-2, 2)
                    sy = vy + rng.randint(-2, 2)
                    if (1 <= sx <= GRID_SIZE and 1 <= sy <= GRID_SIZE
                            and grid[sy][sx] is None and rng.random() < 0.5):
                        grid[sy][sx] = 'Volcanic'
    
    # --- Layer 4: Craters (more on low-atmosphere worlds) ---
    if atmo in ('none', 'trace', 'thin'):
        num_craters = rng.randint(3, 8)
    else:
        num_craters = rng.randint(0, 2)
    
    for _ in range(num_craters):
        cx, cy = rng.randint(2, GRID_SIZE - 1), rng.randint(2, GRID_SIZE - 1)
        radius = rng.randint(1, 2)
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if dx * dx + dy * dy <= radius * radius:
                    px, py = cx + dx, cy + dy
                    if 1 <= px <= GRID_SIZE and 1 <= py <= GRID_SIZE:
                        if grid[py][px] is None:
                            if abs(dx) == radius or abs(dy) == radius:
                                grid[py][px] = 'Crater'
                            elif rng.random() < 0.3:
                                grid[py][px] = 'Crater'
    
    # --- Layer 5: Fill remaining land with biome ---
    for y in range(1, GRID_SIZE + 1):
        for x in range(1, GRID_SIZE + 1):
            if grid[y][x] is not None:
                continue
            
            lat = abs(y - (GRID_SIZE + 1) / 2) / ((GRID_SIZE + 1) / 2)
            
            # Count nearby water for moisture
            nearby_water = 0
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    ny, nx = y + dy, x + dx
                    if 1 <= ny <= GRID_SIZE and 1 <= nx <= GRID_SIZE:
                        if grid[ny][nx] in ('Sea', 'Shallows', 'Ice'):
                            nearby_water += 1
            moisture = min(1.0, nearby_water / 12)
            
            if frozen:
                # Everything is ice, tundra, or rock
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
    
    # --- Layer 6: Civilisation (only on worlds with sentient life) ---
    if has_civilisation:
        # Place urban clusters near water on temperate/habitable land
        habitable = {'Grassland', 'Plains', 'Forest', 'Cultivated', 'Hills'}
        num_cities = rng.randint(2, 5)
        for _ in range(num_cities):
            # Find a habitable spot near water
            attempts = 0
            while attempts < 50:
                ux, uy = rng.randint(3, GRID_SIZE - 2), rng.randint(3, GRID_SIZE - 2)
                if grid[uy][ux] in habitable:
                    # Check for nearby water
                    water_near = any(
                        grid[uy + dy][ux + dx] in ('Sea', 'Shallows')
                        for dy in range(-3, 4) for dx in range(-3, 4)
                        if 1 <= uy + dy <= GRID_SIZE and 1 <= ux + dx <= GRID_SIZE
                    )
                    if water_near or rng.random() < 0.3:
                        grid[uy][ux] = 'Urban'
                        # Cultivated ring around cities
                        for dy in range(-2, 3):
                            for dx in range(-2, 3):
                                cy, cx = uy + dy, ux + dx
                                if (1 <= cx <= GRID_SIZE and 1 <= cy <= GRID_SIZE
                                        and grid[cy][cx] in habitable
                                        and rng.random() < 0.5):
                                    grid[cy][cx] = 'Cultivated'
                        break
                attempts += 1
        
        # Scatter some ruins
        num_ruins = rng.randint(1, 3)
        for _ in range(num_ruins):
            rx, ry = rng.randint(1, GRID_SIZE), rng.randint(1, GRID_SIZE)
            if grid[ry][rx] in habitable or grid[ry][rx] in ('Desert', 'Dust', 'Rock'):
                grid[ry][rx] = 'Ruin'
    
    # Build result tuples
    tiles = []
    for y in range(1, GRID_SIZE + 1):
        for x in range(1, GRID_SIZE + 1):
            terrain = grid[y][x] or 'Rock'  # Fallback
            tiles.append((x, y, terrain))
    
    return tiles


def render_surface_map(tiles, body_name, body_id, planetary_data=None, ship_pos=None):
    """
    Render a 31x31 surface map as ASCII text.
    
    tiles: list of (x, y, terrain_type) or queryable from DB
    planetary_data: dict with temperature, gravity, atmosphere, etc.
    ship_pos: tuple (x, y) of ship location to mark with X, or None
    
    Returns: list of strings (lines)
    """
    # Build grid lookup
    grid = {}
    for x, y, terrain in tiles:
        grid[(x, y)] = terrain
    
    lines = []
    
    # Title
    lines.append(f"Surface Map: {body_name} ({body_id})")
    lines.append("")
    
    # Column headers - single digits get a leading space, double digits tight
    col_nums_top = "     "
    col_nums_bot = "     "
    for x in range(1, GRID_SIZE + 1):
        col_nums_top += f"{x:>2} "
    lines.append(col_nums_top.rstrip())
    
    # Grid rows (top = y=31, bottom = y=1 like Phoenix)
    for y in range(GRID_SIZE, 0, -1):
        row_str = f"{y:>3}  "
        for x in range(1, GRID_SIZE + 1):
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
    
    # Ship position
    if ship_pos:
        terrain = grid.get(ship_pos, 'Unknown')
        lines.append(f"  Ship Position: ({ship_pos[0]},{ship_pos[1]}) - {terrain}")
    
    # Legend
    lines.append("")
    lines.append("Terrain Key:")
    symbols = list(TERRAIN_SYMBOLS.items())
    # 4 per row
    for i in range(0, len(symbols), 4):
        row_items = symbols[i:i + 4]
        row_str = "  " + "  ".join(f"{sym} {name:<12}" for name, sym in row_items)
        lines.append(row_str)
    if ship_pos:
        lines.append("  X Ship         ")
    
    return lines


def store_surface(conn, body_id, tiles):
    """Store generated surface tiles to database."""
    # Clear existing
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
