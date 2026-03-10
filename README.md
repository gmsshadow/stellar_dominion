# Stellar Dominion — PBEM Strategy Game Engine

A play-by-email (PBEM) grand strategy game engine inspired by classic Phoenix-BSE style games. Players command starships, trade goods, recruit officers, and navigate a persistent galaxy — submitting orders by email and receiving detailed ASCII and PDF reports each turn.

Built in Python with SQLite. No web server required — the entire game runs from the command line.

## Features

- **Modular ship components** — ships built from internal components (thrusters, engines, cargo bays, sensors, jump drives) that determine all ship stats. Buy, install, uninstall, and scrap components at starbases. Components use 3-digit IDs for trading.
- **Turn-based order resolution** with an Operational Cycle (OC) system and interleaved priority queue across all ships
- **Play-by-email** — players submit YAML or text order files; the engine validates, resolves, and emails back reports
- **ASCII + PDF reports** — Phoenix-style ship and prefect reports with system maps, cargo manifests, crew rosters, and component breakdowns
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
python pbem.py setup-game --demo
python pbem.py show-map --game OMICRON101
python pbem.py list-components                     # View ship component catalogue
python pbem.py turn-pipeline --game OMICRON101
python pbem.py submit-orders orders.yaml --email alice@example.com
python pbem.py run-turn --game OMICRON101
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
| [Player Guide](PLAYER_GUIDE.md) | Players | Ship components, orders reference, trading, crew, factions |
| [GM Guide](GM_GUIDE.md) | Game Masters | Turn pipeline, moderation tools, universe management, worked example |

## Ship Component System

Ships are built from modular internal components. Each ship has an ST (Stellar Ton) capacity determined by its size: `ST_capacity = ship_size × 50`. A size 10 ship has 500 ST for components.

Component categories: Bridge, Thrusters, Sublight Engines, Cargo Bays, Crew Quarters, Sensors, and Jump Drives. All components have 3-digit IDs (100-169 range) for future trading support.

Ship stats (cargo capacity, sensor rating, life support, gravity rating) are derived entirely from installed components.

```bash
python pbem.py list-components    # View the full component catalogue
```

## Project Structure

```
stellar_dominion/
├── pbem.py                          # Main CLI entry point
├── db/
│   ├── database.py                  # Two-DB schema, connections, migrations, component helpers
│   └── universe_admin.py            # Universe content management
├── engine/
│   ├── game_setup.py                # Game/player creation, market generation
│   ├── maps/
│   │   ├── system_map.py            # 25×25 ASCII grid renderer
│   │   └── surface_gen.py           # Planet surface terrain generator
│   ├── orders/
│   │   └── parser.py                # YAML & text order parser
│   ├── resolution/
│   │   └── resolver.py              # Turn resolution engine
│   └── reports/
│       ├── report_gen.py            # ASCII report generator
│       └── pdf_export.py            # PDF export
└── game_data/
    ├── universe.db                  # World definition + component catalogue
    ├── game_state.db                # Live game state
    └── turns/                       # Orders and reports
```

## Database Architecture

**universe.db** — World definition. Star systems, celestial bodies, hyperspace links, factions, trade goods, planetary resources, and the **ship component catalogue**. GM-editable.

**game_state.db** — Live game state. Players, ships, installed components, cargo, officers, orders, messages, moderator actions. Auto-backed up after each turn.

## CLI Command Summary

### Game & Universe
`setup-game --demo` · `join-game` · `list-players` · `list-ships` · `show-map` · `list-universe` · `list-factions` · `list-components`

### Turn Processing
`submit-orders` · `turn-pipeline` · `hold-turn` / `release-turn` · `review-orders` · `edit-order` / `delete-order` / `inject-order` · `list-actions` / `respond-action` · `run-turn` · `advance-turn`

### Factions & Moderation
`faction-requests` · `approve-faction` / `deny-faction` · `edit-credits` · `suspend-player` / `reinstate-player`

See the [GM Guide](GM_GUIDE.md) for detailed usage.

## Licence

This project is not currently under an open-source licence. All rights reserved.
