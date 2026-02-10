# Stellar Dominion – PBEM Strategy Game Engine v1.0

A play-by-email (PBEM) grand strategy game engine inspired by Phoenix-BSE style games.
Deterministic turn resolution, ASCII reports, persistent SQLite universe.

## Quick Start

```bash
# 1. Set up a demo game (Hanf system with 2 players)
python pbem.py setup-game --demo

# 2. List ships to find IDs
python pbem.py list-ships --game HANF231

# 3. View the system map
python pbem.py show-map --game HANF231

# 4. Submit orders (YAML file)
python pbem.py submit-orders sample_orders.yaml

# 5. Resolve the turn and generate reports
python pbem.py run-turn --game HANF231 -v

# 6. Advance to the next turn (resets all TUs)
python pbem.py advance-turn --game HANF231
```

## Requirements

- Python 3.10+
- PyYAML (`pip install pyyaml`)
- SQLite (built-in)

## Project Structure

```
stellar_dominion/
├── pbem.py                          # Main CLI entry point
├── sample_orders.yaml               # Example orders file
├── db/
│   └── database.py                  # SQLite schema & connection
├── engine/
│   ├── game_setup.py                # Game/player creation
│   ├── maps/
│   │   └── system_map.py            # 25x25 ASCII grid renderer
│   ├── orders/
│   │   └── parser.py                # YAML & text order parser
│   ├── resolution/
│   │   └── resolver.py              # Turn resolution engine (TU system)
│   └── reports/
│       └── report_gen.py            # Phoenix-style ASCII report generator
├── game_data/
│   └── stellar_dominion.db          # Persistent game database (auto-created)
└── reports/                         # Generated turn reports
```

## The Hanf System (231)

The demo game creates the **Hanf** star system — a 25×25 grid:

| Object              | Type       | Location | Notes                      |
|---------------------|------------|----------|----------------------------|
| Hanf Prime          | Star       | M13      | Central star               |
| Orion (247985)      | Planet     | H04      | 0.9g, Standard atmosphere  |
| Tartarus (301442)   | Planet     | R08      | 1.2g, Dense atmosphere     |
| Leviathan (155230)  | Gas Giant  | E18      | 2.5g, Hydrogen             |
| Callyx (88341)      | Moon       | F19      | Moon of Leviathan, 0.3g    |
| Meridian (412003)   | Planet     | T20      | 0.7g, Thin atmosphere      |
| Citadel Station     | Starbase   | H04      | Market, 5 docking slots    |
| Tartarus Depot      | Outpost    | R08      | Market, 3 docking slots    |
| Meridian Waystation | Outpost    | T20      | Market, 3 docking slots    |

### Map Symbols
```
*  Star          @  Player Ship     B  Base/Station
O  Planet        o  Moon            G  Gas Giant
#  Asteroid      ?  Detected        .  Empty Space
```

## Orders Format

### YAML (preferred)
```yaml
game: HANF231
ship: 12345678
orders:
  - WAIT: 50
  - MOVE: M13
  - LOCATIONSCAN: {}
  - MOVE: H04
  - ORBIT: 247985
  - DOCK: 45687590
```

### Plain Text
```
GAME HANF231
SHIP 12345678
WAIT 50
MOVE M13
LOCATIONSCAN
MOVE H04
ORBIT 247985
DOCK 45687590
```

## Supported Commands (v1)

| Command          | TU Cost | Description                      |
|------------------|---------|----------------------------------|
| `WAIT {n}`       | n       | Consume n TU doing nothing       |
| `MOVE {coord}`   | 20      | Move to grid coordinate (e.g. M13) |
| `LOCATIONSCAN`   | 20      | Scan nearby cells for objects    |
| `SYSTEMSCAN`     | 20      | Produce full system ASCII map    |
| `ORBIT {bodyId}` | 10      | Enter orbit of a celestial body  |
| `DOCK {baseId}`  | 30      | Dock at a starbase               |
| `UNDOCK`          | 10      | Leave docked starbase            |

## Turn Resolution Rules

- Each ship starts each turn with a fixed TU allowance (default: 300)
- Orders execute **sequentially** in the order listed
- If a ship has insufficient TU, the order is skipped and added to **pending**
- Failed orders (wrong location, etc.) are logged and added to pending
- Failed orders do **not** deduct TU
- Resolution is **deterministic** using a seeded RNG

## Turn Number Format

Turns follow `YEAR.WEEK` format: `500.1` through `500.52`, then `501.1`.

## CLI Commands

| Command | Description |
|---------|-------------|
| `setup-game [--demo]` | Create a new game (--demo adds 2 test players) |
| `add-player --name --email` | Add a player with ship and political position |
| `submit-orders <file>` | Submit orders from YAML/text file |
| `run-turn --game ID [-v]` | Resolve turn, generate reports |
| `show-map --game ID` | Display system ASCII map |
| `show-status --ship ID` | Show ship status |
| `list-ships --game ID` | List all ships in a game |
| `advance-turn --game ID` | Advance to next turn, reset TUs |
| `edit-credits --political ID --amount N` | Set player credits |

## Report Sections

Ship turn reports include:
- **Between Turn Report** — passive events between turns
- **Turn Report** — order-by-order execution log with TU tracking
- **Command Report** — ship identity, affiliation, efficiency, TU remaining
- **Navigation Report** — current location, docked/orbiting status
- **Crew Report** — officers and crew complement
- **Cargo Report** — cargo hold contents
- **Space Combat Report** — combat status (placeholder for v1)
- **Installed Items** — ship modules and equipment
- **Contacts** — known objects in the system
- **Pending Orders** — orders that failed and carry forward

Political reports include financial summaries, ship fleet overview, and known contacts.

## Game Concepts

### Political Position
Every player has one political position that:
- Owns all their ships and bases
- Tracks credits (in-game currency)
- Stores known contacts and intelligence
- Has rank, affiliation, and influence

### Credits
- Starting amount: 10,000
- Game owner can adjust via `edit-credits`
- Will be used for trading and base operations (future)

### Information Asymmetry
- Players only see what their ships scan
- LOCATIONSCAN reveals nearby objects
- SYSTEMSCAN produces a full system map
- Contact information persists between turns

## Future Roadmap

- [ ] Email ingest (IMAP) and send (SMTP)
- [ ] Inter-system jump travel
- [ ] Combat system (naval, ground, boarding)
- [ ] Trading between bases (buy/sell cargo)
- [ ] Base complex management and production
- [ ] Crew wages and morale
- [ ] Faction system with shared knowledge
- [ ] Planetary surface maps
- [ ] Web portal for turn upload/display
- [ ] Distance-based movement costs
- [ ] Standing orders
