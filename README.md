# Stellar Dominion — PBEM Strategy Game Engine

A play-by-email (PBEM) grand strategy game engine inspired by classic Phoenix-BSE style games. Players command starships, trade goods, recruit officers, and navigate a persistent galaxy — submitting orders by email and receiving detailed ASCII and PDF reports each turn.

Built in Python with SQLite. No web server required — the entire game runs from the command line.

## Features

- **Turn-based order resolution** with a Time Unit (TU) system and interleaved priority queue across all ships
- **Play-by-email** — players submit YAML or text order files; the engine validates, resolves, and emails back reports
- **ASCII + PDF reports** — Phoenix-style ship and prefect reports with system maps, cargo manifests, and crew rosters
- **Persistent universe** — two-database architecture separating world definition (GM-editable) from live game state (engine-managed)
- **Trading economy** — market cycles, base specialisation, price fluctuation, cargo capacity, and circular trade routes
- **Crew management** — hire crew at starbases, promote officers, pay wages, and manage ship efficiency
- **Faction system** — six factions with GM-moderated transfers
- **Moderator actions** — players submit free-text requests to the GM; the turn auto-holds for GM review and response before resolving
- **GM moderation tools** — hold/release turn pipeline, review/edit/delete/inject orders, respond to moderator actions
- **Gmail integration** — fetch orders from Gmail, send reports back automatically
- **Universe expansion** — add star systems, celestial bodies, hyperspace links, surface ports, and outposts via CLI

## Quick Start

```bash
# Set up a demo game with 2 players, 3 star systems, and 3 starbases
python pbem.py setup-game --demo

# View the system map
python pbem.py show-map --game OMICRON101

# Check turn pipeline status
python pbem.py turn-pipeline --game OMICRON101

# Submit orders for a player
python pbem.py submit-orders orders.yaml --email alice@example.com

# Resolve the turn and generate reports
python pbem.py run-turn --game OMICRON101

# Advance to the next turn
python pbem.py advance-turn --game OMICRON101
```

## Requirements

- Python 3.10+
- PyYAML (`pip install pyyaml`)
- SQLite (built-in)
- ReportLab (`pip install reportlab`) — optional, enables PDF report export

## Documentation

| Guide | Audience | Contents |
|-------|----------|----------|
| [Player Guide](PLAYER_GUIDE.md) | Players | Orders reference, ship commands, trading, crew, factions, moderator actions |
| [GM Guide](GM_GUIDE.md) | Game Masters | Turn pipeline, moderation tools, moderator actions, universe management, worked example |

## Project Structure

```
stellar_dominion/
├── pbem.py                          # Main CLI entry point (all commands)
├── gmail_fetch.py                   # Standalone Gmail fetch helper
├── db/
│   ├── database.py                  # Two-DB schema, connections & migrations
│   └── universe_admin.py            # Universe content management
├── engine/
│   ├── game_setup.py                # Game/player creation, market generation
│   ├── gmail.py                     # Gmail API integration
│   ├── order_processor.py           # Order validation & filing logic
│   ├── registration.py              # Registration form parser
│   ├── turn_folders.py              # Turn folder manager (incoming/processed)
│   ├── maps/
│   │   ├── system_map.py            # 25×25 ASCII grid renderer
│   │   └── surface_gen.py           # Planet surface terrain generator
│   ├── orders/
│   │   └── parser.py                # YAML & text order parser
│   ├── resolution/
│   │   └── resolver.py              # Turn resolution engine
│   └── reports/
│       └── report_gen.py            # ASCII + PDF report generator
└── game_data/                       # Created at runtime
    ├── universe.db                  # World definition (GM-editable)
    ├── game_state.db                # Live game state (engine-managed)
    ├── saves/                       # Auto-backups after each run-turn
    └── turns/
        ├── incoming/                # Player orders (keyed by email)
        └── processed/               # Generated reports (keyed by account)
```

## Database Architecture

The game uses a **two-database model**:

**universe.db** — World definition. Contains star systems, celestial bodies, hyperspace links, factions, trade goods, and planetary resources. GM-editable — you can open this in DB Browser for SQLite, add a system, and it's live next turn.

**game_state.db** — Live game state. Contains players, prefects, ships, starbases, officers, cargo, market prices, orders, messages, moderator actions, faction requests, and turn logs. Automatically backed up after each `run-turn`.

The engine ATTACHes `universe.db` to the `game_state.db` connection, so all queries work through a single handle.

## CLI Command Summary

### Game Management
| Command | Description |
|---------|-------------|
| `setup-game --demo` | Create a demo game with sample data |
| `join-game --game ID` | Interactive new player registration |
| `list-players` | List all players |
| `list-ships --game ID` | List all ships with positions |
| `show-map --game ID` | Display ASCII system map |

### Turn Processing
| Command | Description |
|---------|-------------|
| `submit-orders FILE --email EMAIL` | Submit player orders |
| `turn-pipeline --game ID` | Pipeline dashboard |
| `hold-turn` / `release-turn` | GM turn locking |
| `review-orders` | Inspect all pending orders |
| `edit-order` / `delete-order` / `inject-order` | GM order editing |
| `list-actions` | List moderator action requests |
| `respond-action --action-id N --response "..."` | Respond to a moderator action |
| `run-turn --game ID` | Resolve turn and generate reports |
| `advance-turn --game ID` | Move to next week |

### Universe & Factions
| Command | Description |
|---------|-------------|
| `list-universe` | Show all systems, bodies, links |
| `add-system` / `add-body` / `add-link` | Build the universe |
| `list-factions` | Show available factions |
| `approve-faction` / `deny-faction` | Moderate faction requests |

See the [GM Guide](GM_GUIDE.md) for detailed usage of all commands.

## Licence

This project is not currently under an open-source licence. All rights reserved.
