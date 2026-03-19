# Stellar Dominion — PBEM Strategy Game Engine

A play-by-email (PBEM) grand strategy game engine inspired by classic Phoenix-BSE style games. Players command starships, trade goods, recruit officers, and navigate a persistent galaxy — submitting orders by email and receiving detailed ASCII and PDF reports each turn.

Built in Python with SQLite. No web server required — the entire game runs from the command line.

## Features

- **Modular ship components** — ships built from internal components (thrusters, engines, cargo bays, sensors, jump drives) that determine all ship stats. Buy, install, uninstall, and scrap components at starbases. Components use 3-digit IDs (100-169).
- **Modular base system** — starbases, surface ports, and outposts use installable modules (500-589) for docking, mining, manufacturing, trade, storage, defence, and habitat. Employee efficiency and command efficiency drive base output.
- **Turn-based order resolution** with an Operational Cycle (OC) system and interleaved priority queue across all ships
- **Play-by-email** — players submit YAML or text order files; the engine validates, resolves, and emails back reports
- **ASCII + PDF reports** — Phoenix-style ship and prefect reports with system maps, cargo manifests, crew rosters, and component breakdowns
- **Persistent universe** — two-database architecture separating world definition (GM-editable) from live game state (engine-managed)
- **Trading economy** — market cycles, base specialisation, price fluctuation, cargo capacity, and circular trade routes
- **Crew management** — hire crew at starbases, promote officers, pay wages, and manage ship efficiency
- **Engine efficiency** — movement cost scales with installed engines vs ship size; ships without engines cannot move
- **Faction system** — six factions with GM-moderated transfers
- **GM NPC system** — GM account with unlimited credits, multiple prefects across factions, NPC ships, local order submission (gm_orders/), and local report output (gm_reports/)
- **Moderator actions** — players submit free-text requests to the GM; the turn auto-holds for GM review and response before resolving
- **GM moderation tools** — hold/release turn pipeline, review/edit/delete/inject orders, respond to moderator actions
- **Gmail integration** — fetch orders from Gmail, send reports back automatically
- **Universe expansion** — add star systems, celestial bodies, hyperspace links, surface ports, starbases, and outposts via CLI

## Quick Start

```bash
python pbem.py setup-game --demo
python pbem.py show-map --game OMICRON101
python pbem.py list-components                     # Ship component catalogue
python pbem.py list-modules                        # Base module catalogue
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
| [GM Guide](GM_GUIDE.md) | Game Masters | Turn pipeline, NPC management, base modules, moderation tools, universe management |
| [CLI Reference](CLI_REFERENCE.md) | All | Complete command reference with all parameters |

## Ship Component System

Ships are built from modular internal components. Each ship has an ST (Stellar Ton) capacity determined by its size: `ST_capacity = ship_size × 50`. The starting Light Trader MK I is size 50 = 2500 ST, with 500 ST of starting components and 2000 ST free for upgrades.

Component categories: Bridge, Thrusters, Sublight Engines, Cargo Bays, Crew Quarters, Sensors, and Jump Drives. All components have 3-digit IDs (100-169 range).

Ship stats (cargo capacity, sensor rating, life support, gravity rating, engine efficiency) are derived entirely from installed components.

```bash
python pbem.py list-components    # View the full component catalogue
```

## Base Module System

Starbases, surface ports, and outposts are equipped with installable modules (500-589 range) that determine their capabilities. Surface ports are built on planet surfaces, starbases orbit above them, and outposts are lightweight surface installations. Modules have location restrictions — some are starbase-only (docking, repair), some surface-only (mining, market).

Module categories: Command, Docking, Mining, Factory, Maintenance, Market, Storage, Habitat, and Defence. Base efficiency depends on having enough employees and command modules (1 per 100 modules).

```bash
python pbem.py list-modules                # View the full module catalogue
python pbem.py base-status --id 45687590   # Detailed status for a base
```

## Project Structure

```
stellar_dominion/
├── pbem.py                          # Main CLI entry point
├── db/
│   ├── database.py                  # Two-DB schema, connections, migrations, helpers
│   └── universe_admin.py            # Universe content management
├── engine/
│   ├── game_setup.py                # Game/player/GM creation, market generation
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
    ├── universe.db                  # World definition + component/module catalogues
    ├── game_state.db                # Live game state
    ├── turns/                       # Orders and reports
    ├── gm_orders/                   # GM NPC order files (local submission)
    └── gm_reports/                  # GM NPC turn reports (local output)
```

## Database Architecture

**universe.db** — World definition. Star systems, celestial bodies, hyperspace links, planet surfaces, factions, trade goods, planetary resources, the **ship component catalogue**, and the **base module catalogue**. GM-editable.

**game_state.db** — Live game state. Players, prefects, ships, installed ship components, installed base modules, cargo, officers, starbases, surface ports, outposts, orders, messages, moderator actions. Auto-backed up after each turn.

## CLI Command Summary

### Game & Universe
`setup-game --demo` · `join-game` · `list-players` · `list-ships` · `show-map` · `list-universe` · `list-factions` · `list-components` · `list-modules` · `base-status`

### Turn Processing
`submit-orders` · `turn-pipeline` · `hold-turn` / `release-turn` · `review-orders` · `edit-order` / `delete-order` / `inject-order` · `list-actions` / `respond-action` · `run-turn` · `advance-turn`

### Factions & Moderation
`faction-requests` · `approve-faction` / `deny-faction` · `edit-credits` · `suspend-player` / `reinstate-player`

### GM NPC System
`add-gm` · `add-gm-prefect` · `add-gm-ship`

See the [GM Guide](GM_GUIDE.md) for detailed usage.

## Licence

This project is not currently under an open-source licence. All rights reserved.
