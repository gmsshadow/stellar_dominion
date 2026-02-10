"""
Stellar Dominion - Map Renderer
Generates 25x25 ASCII grid maps for star systems.
"""

# Grid: columns A-Y (25), rows 01-25
COLS = [chr(i) for i in range(ord('A'), ord('Y') + 1)]  # A through Y
ROWS = list(range(1, 26))  # 1 through 25

# Map symbols
SYMBOLS = {
    'star': '*',
    'planet': 'O',
    'moon': 'o',
    'gas_giant': 'G',
    'asteroid': '#',
    'base': 'B',
    'ship': '@',
    'contact': '?',
    'empty': '.',
}


def col_to_index(col_letter):
    """Convert column letter to 0-based index."""
    return ord(col_letter.upper()) - ord('A')


def index_to_col(index):
    """Convert 0-based index to column letter."""
    return chr(ord('A') + index)


def grid_distance(col1, row1, col2, row2):
    """Calculate Chebyshev distance between two grid cells."""
    c1 = col_to_index(col1)
    c2 = col_to_index(col2)
    return max(abs(c1 - c2), abs(row1 - row2))


def render_system_map(system_data, objects, ship_position=None, title=None):
    """
    Render a 25x25 ASCII system map.
    
    system_data: dict with star info
    objects: list of dicts with {type, col, row, symbol, name}
    ship_position: tuple (col, row) for current ship or None
    title: optional title above map
    """
    # Initialize empty grid
    grid = [['.' for _ in range(25)] for _ in range(25)]

    # Place star at center (M13 = index 12, row 13 → index 12)
    star_col = col_to_index(system_data.get('star_col', 'M'))
    star_row = system_data.get('star_row', 13) - 1
    grid[star_row][star_col] = SYMBOLS['star']

    # Place objects
    for obj in objects:
        c = col_to_index(obj['col'])
        r = obj['row'] - 1
        if 0 <= c < 25 and 0 <= r < 25:
            grid[r][c] = obj.get('symbol', SYMBOLS.get(obj.get('type', 'contact'), '?'))

    # Place ship position (overrides other symbols)
    if ship_position:
        sc = col_to_index(ship_position[0])
        sr = ship_position[1] - 1
        if 0 <= sc < 25 and 0 <= sr < 25:
            grid[sr][sc] = SYMBOLS['ship']

    # Render to string
    lines = []
    if title:
        lines.append(title)

    # Header row
    header = "    " + "  ".join(f"{c}" for c in COLS)
    lines.append(header)

    # Grid rows
    for r in range(25):
        row_num = f"{r + 1:02d}"
        cells = "  ".join(f"{grid[r][c]}" for c in range(25))
        lines.append(f"{row_num}  {cells}")

    return "\n".join(lines)


def render_location_scan(system_id, scan_col, scan_row, objects, scan_radius=5):
    """
    Render results of a location scan - objects within scan radius.
    Returns list of detected objects.
    """
    detected = []
    for obj in objects:
        dist = grid_distance(scan_col, scan_row, obj['col'], obj['row'])
        if dist <= scan_radius and not (obj['col'] == scan_col and obj['row'] == scan_row):
            detected.append(obj)
    return detected
