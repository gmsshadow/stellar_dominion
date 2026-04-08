# Stellar Dominion — CLI Reference

Complete reference for all `pbem.py` commands with parameters.

**Global option** (applies to all commands):

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--db` | No | `game_data/game_state.db` | Path to game_state.db (most commands accept this) |

---

## Game Setup

### setup-game
Create a new game with optional demo data (2 players, 3 starbases, trade goods).

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | No | `OMICRON101` | Game ID |
| `--name` | No | — | Game display name |
| `--demo` | No | — | Flag: create demo game with 2 sample players (Alice & Bob) |

```bash
python pbem.py setup-game --demo
python pbem.py setup-game --game MYGAME --name "Campaign Alpha"
```

### join-game
Interactive new player registration. Prompts for name, email, ship name, and starting location.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | No | `OMICRON101` | Game ID |

### add-player
Non-interactive player creation (GM use).

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | No | `OMICRON101` | Game ID |
| `--name` | **Yes** | — | Player name |
| `--email` | **Yes** | — | Player email address |
| `--prefect` | No | — | Prefect character name |
| `--ship-name` | No | — | Starting ship name |
| `--start-col` | No | `I` | Starting grid column |
| `--start-row` | No | `6` | Starting grid row |

---

## Player Registration (Email-Based)

### generate-form
Generate blank YAML and text registration forms for new players. Lists available starting planets.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | No | `OMICRON101` | Game ID |
| `--output` | No | `.` | Output directory for form files |

### register-player
Process a filled-in registration form and create the player account.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `form` | **Yes** | — | Path to the completed registration form (positional) |

---

## Player & Prefect Management

### list-players
List all players in the game. Shows GM accounts with `[GM]` tag.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | No | `OMICRON101` | Game ID |
| `--all` | No | — | Flag: include suspended players |

### edit-credits
Set a prefect's credit balance directly.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--prefect` | **Yes** | — | Prefect ID |
| `--amount` | **Yes** | — | New credit amount |

### suspend-player
Suspend a player account. Ships remain but cannot submit orders. Provide either `--account` or `--email`.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | No | `OMICRON101` | Game ID |
| `--account` | No | — | Player account number |
| `--email` | No | — | Player email address |

### reinstate-player
Reinstate a suspended player account. Same parameters as `suspend-player`.

---

## GM NPC System

### add-gm
Create the GM player account. One GM per game. Creates `game_data/gm_orders/` and `game_data/gm_reports/` directories.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | No | `OMICRON101` | Game ID |
| `--name` | No | `Game Master` | GM display name |
| `--email` | No | `gm@local` | GM email (for validation only — reports go to local folder) |

### add-gm-prefect
Create an NPC prefect under the GM account. GM can have multiple prefects across different factions. All GM prefects have unlimited credits.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | No | `OMICRON101` | Game ID |
| `--name` | **Yes** | — | Prefect name (e.g. `"Admiral Voss"`) |
| `--faction` | **Yes** | — | Faction ID (0=IND, 11=STA, 12=MTG, 13=IMP, 14=FRN, 15=SYN) |
| `--credits` | No | `0` | Starting credits (cosmetic — unlimited anyway) |

### add-gm-ship
Create an NPC ship under a GM prefect. Ships are created with standard starting components, crew, and a captain.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | No | `OMICRON101` | Game ID |
| `--prefect` | **Yes** | — | GM prefect ID (from `add-gm-prefect`) |
| `--ship-name` | No | Auto-generated | Ship name |
| `--hull-type` | No | `Commercial` | Hull type (Commercial, Military, etc.) |
| `--size` | No | `50` | Ship size (ST capacity = size × 50) |
| `--system` | **Yes** | — | Starting system ID |
| `--col` | **Yes** | — | Starting grid column (e.g. `H`) |
| `--row` | **Yes** | — | Starting grid row (e.g. `4`) |

```bash
python pbem.py add-gm --game OMICRON101
python pbem.py add-gm-prefect --name "Admiral Voss" --faction 13
python pbem.py add-gm-ship --prefect 25782959 --ship-name "ISS Vengeance" \
    --hull-type Military --size 80 --system 101 --col M --row 13
```

---

## Order Submission

### submit-orders
Submit a single order file for validation and storage.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `orders_file` | **Yes** | — | Path to YAML or text order file (positional) |
| `--email` | **Yes** | — | Player email for validation |
| `--game` | No | `OMICRON101` | Game ID |
| `--dry-run` | No | — | Flag: validate only, don't store |

### process-inbox
Batch process all submissions from an inbox directory. Auto-detects orders vs registration forms. Also scans `game_data/gm_orders/` for GM NPC orders.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--inbox` | **Yes** | — | Path to inbox directory |
| `--game` | No | `OMICRON101` | Game ID |
| `--keep` | No | — | Flag: don't move processed files to `_processed/` |

---

## Turn Pipeline

### turn-pipeline
Dashboard showing current turn status, order counts, and ships with/without orders.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | No | `OMICRON101` | Game ID |

### turn-status
Show incoming/processed file status for the current or specified turn.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | No | `OMICRON101` | Game ID |
| `--turn` | No | Current turn | Turn string (e.g. `500.3`) |

### hold-turn
Lock the turn. Blocks new order submission and prevents `run-turn`.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | No | `OMICRON101` | Game ID |

### release-turn
Release a held turn, returning it to OPEN state.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | No | `OMICRON101` | Game ID |
| `--reopen` | No | — | Flag: reopen for additional orders |

### run-turn
Resolve the turn: process all orders, generate ship and prefect reports (ASCII + PDF). GM reports are copied to `game_data/gm_reports/{turn}/`.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | No | `OMICRON101` | Game ID |
| `--ship` | No | All ships | Specific ship ID to resolve |
| `--verbose` / `-v` | No | — | Flag: detailed output |
| `--force` | No | — | Flag: override held/completed state, skip moderator holds |

### advance-turn
Reset turn to OPEN, increment week counter, reset all ship OCs. Run after `send-turns`.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | No | `OMICRON101` | Game ID |

---

## Order Moderation

### review-orders
Display all pending orders for GM inspection, grouped by ship.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | No | `OMICRON101` | Game ID |

### edit-order
Replace a pending order's command string.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | No | `OMICRON101` | Game ID |
| `--order-id` | **Yes** | — | Order ID to edit |
| `--command` | No | — | New command string (e.g. `"MOVE F10"`) |

### delete-order
Remove a pending order.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | No | `OMICRON101` | Game ID |
| `--order-id` | **Yes** | — | Order ID to delete |

### inject-order
Insert a GM order for any ship.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | No | `OMICRON101` | Game ID |
| `--ship` | **Yes** | — | Ship ID |
| `--command` | **Yes** | — | Command string (e.g. `"SCANSYSTEM"`) |
| `--sequence` | No | Append | Order sequence number (position in queue) |

---

## Moderator Actions

### list-actions
List moderator action requests from players.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | No | `OMICRON101` | Game ID |
| `--status` | No | `pending` | Filter: `pending`, `responded`, `resolved`, or `all` |

### respond-action
Respond to a player's moderator request. Response is embedded in their ship report.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | No | `OMICRON101` | Game ID |
| `--action-id` | **Yes** | — | Action ID to respond to |
| `--response` | **Yes** | — | GM response text |

---

## Faction Management

### list-factions
List all available factions with IDs and abbreviations.

*No additional parameters.*

### faction-requests
List faction change requests.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | **Yes** | — | Game ID |
| `--status` | No | `pending` | Filter: `pending`, `approved`, `denied`, or `all` |

### approve-faction
Approve a faction change request.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | **Yes** | — | Game ID |
| `--request-id` | **Yes** | — | Request ID to approve |
| `--note` | No | — | GM note to include in notification |

### deny-faction
Deny a faction change request.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | **Yes** | — | Game ID |
| `--request-id` | **Yes** | — | Request ID to deny |
| `--note` | No | — | GM note / reason for denial |

---

## Email Integration

### fetch-mail
Fetch order submissions from Gmail inbox.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--credentials` | **Yes** | — | Path to Gmail API credentials JSON |
| `--token` | No | `./token.json` | Path to stored OAuth token |
| `--game` | No | `OMICRON101` | Game ID (for acknowledgement replies) |
| `--inbox` | No | `./inbox` | Local directory to save fetched files |
| `--orders-label` | No | `sd-orders` | Gmail label for incoming orders |
| `--processed-label` | No | `sd-processed` | Gmail label for processed messages |
| `--query` | No | — | Custom Gmail search query |
| `--max-results` | No | `25` | Maximum messages to fetch |
| `--port` | No | `0` | Custom SMTP port (0 = default) |
| `--dry-run` | No | — | Flag: fetch and display but don't save or label |
| `--reply` | No | — | Flag: send acknowledgement replies |

### send-turns
Email processed turn reports to all players. GM accounts are automatically skipped.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--credentials` | No | — | Path to Gmail API credentials JSON (required unless `--dry-run`) |
| `--token` | No | `./token.json` | Path to stored OAuth token |
| `--game` | No | `OMICRON101` | Game ID |
| `--turn` | No | Current turn | Specific turn string to send |
| `--port` | No | `0` | Custom SMTP port |
| `--dry-run` | No | — | Flag: show what would be sent without sending |

---

## Viewing Information

### show-map
Display the ASCII system map.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | No | `OMICRON101` | Game ID |
| `--system` | No | `101` | System ID |

### show-surface
Display the surface map for a celestial body. Shows terrain, surface ports, outposts, and any landed ships.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--body` | **Yes** | — | Body ID |

```bash
python pbem.py show-surface --body 247985
```

### preview-ship
Generate a full ship turn report from the current database state without running a turn. Useful for verifying database edits to ship components, crew, cargo, or position. Shows the same report format players receive, but with no orders or resolution — just the Command Report, Navigation, Crew, and Components sections.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--ship` | **Yes** | — | Ship ID |

```bash
python pbem.py preview-ship --ship 30158993
```

### recalc-ships
Recalculate all ship stats from installed components and cargo. Syncs `crew_count` from cargo crew + officers, and recalculates `cargo_capacity`, `life_support_capacity`, `gravity_rating`, `sensor_rating`, and `crew_required` from installed components. Run this after making direct database edits to ships, installed_items, or cargo_items.

*No additional parameters.*

```bash
python pbem.py recalc-ships
```

### recalc-bases
Recalculate all base stats from installed modules and inventory. Updates `docking_capacity`, `employee_capacity`, and `sensor_profile` for every starbase, surface port, and outpost. Run after making direct database edits to bases, installed_modules, or base_inventory.

*No additional parameters.*

```bash
python pbem.py recalc-bases
```

### show-status
Show a ship's current position and status.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--ship` | No | — | Ship ID |

### list-ships
List all ships in the game.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--game` | No | `OMICRON101` | Game ID |
| `--all` | No | — | Flag: include suspended players' ships |

### list-components
List the full ship component catalogue (IDs 100-169) with stats and prices.

*No additional parameters.*

### list-modules
List the full base module catalogue (IDs 500-589) with stats, employee requirements, and location restrictions.

*No additional parameters.*

### base-status
Show detailed status for a starbase, surface port, or outpost. Displays installed modules, efficiency breakdown, and capabilities.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--id` | **Yes** | — | Base, port, or outpost ID |

### list-universe
Show all universe content: star systems, celestial bodies, trade goods, resources, factions, surface ports, and outposts.

*No additional parameters.*

---

## Universe Building

### add-system
Add a new star system to the universe.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--name` | **Yes** | — | System name |
| `--system-id` | No | Auto-assigned | System ID |
| `--star-name` | No | `"<name> Prime"` | Star name |
| `--spectral-type` | No | `G2V` | Spectral classification |
| `--star-col` | No | `M` | Star grid column |
| `--star-row` | No | `13` | Star grid row |
| `--no-star` | No | — | Flag: create a starless nexus (no star symbol, no jump distance restriction) |
| `--no-turn-stamp` | No | — | Flag: skip created_turn provenance |

```bash
python pbem.py add-system --name "Proxima" --spectral-type K1V
python pbem.py add-system --name "The Void" --no-star    # Starless nexus
```

### add-body
Add a celestial body to a star system.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--name` | **Yes** | — | Body name |
| `--system-id` | **Yes** | — | System to add the body to |
| `--body-id` | No | Auto-assigned | Body ID |
| `--body-type` | No | `planet` | Type: `planet`, `moon`, `gas_giant`, `asteroid` |
| `--parent` | No | — | Parent body ID (required for moons) |
| `--col` | **Yes** | — | Grid column (e.g. `H`) |
| `--row` | **Yes** | — | Grid row (e.g. `4`) |
| `--gravity` | No | `1.0` | Surface gravity |
| `--temperature` | No | `300` | Surface temperature in Kelvin |
| `--atmosphere` | No | `Standard` | Atmosphere type |
| `--tectonic` | No | `0` | Tectonic activity (0-10) |
| `--hydrosphere` | No | `0` | Hydrosphere percentage (0-100) |
| `--life` | No | `None` | Life level: None, Microbial, Plant, Animal, Sentient |
| `--surface-size` | No | Auto (by type) | Surface grid size |
| `--resource-id` | No | — | Linked planetary resource ID |
| `--no-turn-stamp` | No | — | Flag: skip created_turn provenance |

### add-link
Add a hyperspace link between two star systems.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `system_a` | **Yes** | — | First system ID (positional) |
| `system_b` | **Yes** | — | Second system ID (positional) |
| `--known` | No | — | Flag: link is visible to all players by default |
| `--no-turn-stamp` | No | — | Flag: skip created_turn provenance |

### add-port
Add a surface port to a planet. A starbase can be built above it later.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `port_id` | **Yes** | — | Unique port ID (positional) |
| `body_id` | **Yes** | — | Planet/moon body ID (positional) |
| `name` | **Yes** | — | Port name (positional) |
| `x` | **Yes** | — | Surface X coordinate (positional) |
| `y` | **Yes** | — | Surface Y coordinate (positional) |
| `--complexes` | No | `0` | Number of complexes |
| `--workers` | No | `0` | Worker count |
| `--troops` | No | `0` | Troop count |

### add-outpost
Add an outpost to a planet or moon.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `outpost_id` | **Yes** | — | Unique outpost ID (positional) |
| `body_id` | **Yes** | — | Planet/moon body ID (positional) |
| `name` | **Yes** | — | Outpost name (positional) |
| `x` | **Yes** | — | Surface X coordinate (positional) |
| `y` | **Yes** | — | Surface Y coordinate (positional) |
| `--type` | No | `General` | Outpost type (e.g. Mining, Communications) |
| `--workers` | No | `0` | Worker count |

---

### regen-surface
Regenerate the surface terrain for a celestial body. Deletes existing terrain tiles and generates new ones based on the body's current properties (gravity, temperature, atmosphere, hydrosphere, life, tectonic activity). Displays the new surface map after generation.

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `--body-id` | **Yes** | — | Body ID to regenerate |

```bash
python pbem.py regen-surface --body-id 247985
```

### gen-surfaces
Scan all celestial bodies and generate surface terrain for any that are missing. Skips bodies that already have terrain data. Use this after importing bodies via the database or upgrading from a version that didn't pre-generate surfaces.

*No additional parameters.*

```bash
python pbem.py gen-surfaces
```

---

## Database Utilities

### split-db
Split a legacy single-file database into the two-database architecture (universe.db + game_state.db).

| Parameter | Required | Default | Description |
|-----------|:--------:|---------|-------------|
| `legacy_db` | **Yes** | — | Path to the legacy stellar_dominion.db file (positional) |
