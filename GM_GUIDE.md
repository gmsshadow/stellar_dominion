# Stellar Dominion — Game Master Guide

This guide covers game setup, the GM NPC system, base modules, the turn pipeline, moderator actions, ship components, universe building, and a worked turn example.

## Setting Up a Game

```bash
python pbem.py setup-game --demo        # Creates OMICRON101 with 2 players, 3 systems
python pbem.py join-game --game OMICRON101   # Interactive player registration
python pbem.py list-players --game OMICRON101
python pbem.py list-ships --game OMICRON101
python pbem.py show-map --game OMICRON101
python pbem.py list-components           # Ship component catalogue
python pbem.py list-modules              # Base module catalogue
```

## GM NPC System

The GM can control NPC ships, bases, and entire factions through a dedicated GM account. The GM player has special privileges: multiple prefects across different factions, unlimited credits, local order/report handling.

### Setting Up the GM

```bash
# 1. Create the GM account (one per game)
python pbem.py add-gm --game OMICRON101
  # Creates: gm@local account, game_data/gm_orders/, game_data/gm_reports/

# 2. Create NPC prefects (one per faction or role)
python pbem.py add-gm-prefect --game OMICRON101 --name "Admiral Voss" --faction 13
python pbem.py add-gm-prefect --game OMICRON101 --name "Shadow Broker" --faction 15

# 3. Create NPC ships
python pbem.py add-gm-ship --game OMICRON101 --prefect 25782959 \
    --ship-name "ISS Vengeance" --hull-type Military --size 80 \
    --system 101 --col M --row 13
```

### How GM Differs from Regular Players

| Feature | Regular Player | GM |
|---------|:-:|:-:|
| Prefects per account | 1 | Unlimited |
| Factions | 1 at a time | Any per prefect |
| Credits | Tracked, deducted | Unlimited (no deductions) |
| Order submission | Email or inbox folder | game_data/gm_orders/ |
| Report delivery | Email | game_data/gm_reports/{turn}/ |
| Wages | Deducted weekly | Skipped |
| show in send-turns | Yes | Skipped |

### Submitting GM Orders

Drop YAML order files in `game_data/gm_orders/`. Format is identical to player orders, using the GM account number:

```yaml
game: OMICRON101
account: 51747534
ship: 81224005
orders:
  - SCANLOCATION: {}
  - MOVE: "H04"
  - DOCK: 45687590
```

When you run `process-inbox`, the engine scans `gm_orders/` after the regular inbox. Processed files are moved to `gm_orders/_processed/`.

### GM Reports

During `run-turn`, GM reports are automatically copied to `game_data/gm_reports/{turn}/`. The GM account is skipped by `send-turns`, so no emails clutter the GM inbox.

### Listing GM Status

```bash
python pbem.py list-players --game OMICRON101    # Shows [GM] tag on GM account
python pbem.py list-ships --game OMICRON101      # Shows all ships including NPC
```

## Ship Component System

Ships are built from modular internal components stored in the `ship_components` catalogue (universe.db). Each component has a 3-digit ID.

### ST Capacity

Ship internal capacity is determined by size: `ST_capacity = ship_size × 50`. The starting Light Trader MK I is size 50 = 2500 ST, with 500 ST used and 2000 ST free.

### Component Categories

| Range | Category | Key Stats |
|-------|----------|-----------|
| 100-109 | Bridge/Command | Required for operation |
| 110-119 | Thrusters | Thrust → gravity rating |
| 120-129 | Sublight Engines | Engine efficiency → movement cost |
| 130-139 | Cargo Systems | Cargo capacity (ST) |
| 140-149 | Crew Quarters | Crew capacity + life support |
| 150-159 | Sensors | Sensor rating |
| 160-169 | Jump Drives | Jump range + OC cost |

### Engine Efficiency

Movement cost depends on installed engines. Optimal = 1 engine per 10 ship size (size 50 needs 5). With fewer engines, MOVE cost scales inversely (e.g. 1/5 engines = 5× base cost). With zero engines, the ship cannot move. Crew undermanning penalty stacks on top.

### Stat Derivation

All ship stats are calculated from installed components:

- **cargo_capacity** = sum of all cargo_capacity values
- **life_support_capacity** = sum of all life_capacity values
- **sensor_rating** = sum of all sensor_rating values
- **gravity_rating** = total_thrust / ship_size
- **crew_required** = ship_size (1 per size point)
- **engine_efficiency** = engines / optimal × 100%
- **jump_range** = best installed jump drive range (systems per activation)

When components change, `recalculate_ship_stats()` is called automatically.

### Starting Ship (Light Trader MK I, Size 50)

```
Standard Bridge ×1      20 ST
Thruster Array ×1       50 ST  (thrust 20 → gravity 0.4)
Commercial Engine ×1    60 ST  (1/5 optimal → 20% engine eff)
Cargo Bay ×5           200 ST  (500 cargo)
Crew Quarters ×1        30 ST  (20 crew, 20 life)
Basic Sensor Array ×1   20 ST  (sensor 5)
Jump Drive Mk1 ×1     120 ST  (range 5 systems, 50 OC/activation)
                       -------
Total:                 500/2500 ST (2000 ST free)
```

### Component Commands

```bash
python pbem.py list-components    # Full catalogue with stats and prices
```

Players refit ships at starbases using these orders:

- **BUY base_id comp_id qty INSTALL** — Buy and install directly (checks ST capacity)
- **BUY base_id comp_id qty** — Buy to cargo (components take `st_cost` as cargo mass)
- **INSTALL comp_id [qty]** — Install from cargo (10 OC, checks ST capacity)
- **UNINSTALL comp_id [qty]** — Remove to cargo (10 OC, checks cargo space)
- **SCRAP comp_id [qty]** — Destroy from cargo (0 OC)

Components are sold at catalogue `base_price` at any starbase. Hull restrictions (e.g. `military`) are enforced on buy and install.

### Adding Components to universe.db

New components can be added directly to the `ship_components` table. Key columns: `component_id`, `name`, `category`, `st_cost`, and the relevant stat columns. They are available immediately.

## Base Module System

Starbases, surface ports, and outposts are equipped with installable modules from the `base_modules` catalogue (universe.db, IDs 500-589). Modules determine what a base can do.

### Architecture

- **Surface Ports** are built on planet surfaces (ground facilities)
- **Starbases** orbit above surface ports (orbital facilities)
- **Outposts** are lightweight surface installations

A starbase references its surface port via `surface_port_id`. Surface ports are created first, then starbases above them.

### Module Categories

| Range | Category | Restriction | Key Stat |
|-------|----------|:-----------:|----------|
| 500-509 | Command | Any | 1 per 100 modules for 100% efficiency |
| 510-519 | Docking | Starbase | Docking slots for ships |
| 520-529 | Mining | Surface | Resource extraction capacity |
| 530-539 | Factory | Any | Manufacturing capacity |
| 540-549 | Maintenance | Starbase | Ship repair capacity |
| 550-559 | Market | Surface | Trade income from population |
| 560-569 | Storage | Any | Bulk goods storage (ST) |
| 570-579 | Habitat | Any/Surface | Employee housing capacity |
| 580-589 | Defence | Any | Defensive rating |

### Base Efficiency

Two factors determine efficiency (lowest wins):

**Command efficiency** — 1 Command Module required per 100 total modules. A base with 9 modules and 1 Command Module = 100%. A base with 150 modules and 1 Command Module = 67% (needs 2).

**Employee efficiency** — Each module type has an `employees_required` value. Total required employees = sum across all installed modules. Efficiency = actual employees / required × 100%.

### Viewing Base Status

```bash
python pbem.py list-modules              # Full module catalogue
python pbem.py base-status --id 45687590 # Detailed base status with modules and efficiency
```

### Adding Modules to universe.db

New modules can be added directly to the `base_modules` table. Key columns: `module_id`, `name`, `category`, `employees_required`, `location_restriction` (NULL, 'starbase', or 'surface'), and the relevant capacity columns.

## The Turn Pipeline

```
OPEN ──hold-turn──> HELD ──release-turn──> OPEN ──run-turn──> PROCESSING ──> COMPLETED ──advance-turn──> OPEN
```

**Auto-hold:** If any ship submits a `MODERATOR` order, `run-turn` auto-holds before resolution.

| State | Orders? | run-turn? | Description |
|-------|:---:|:---:|-------------|
| **OPEN** | Yes | Yes | Normal state |
| **HELD** | No | No | Locked for GM review |
| **PROCESSING** | No | No | Turn resolving |
| **COMPLETED** | No | No | Done — use `advance-turn` |

```bash
python pbem.py turn-pipeline --game OMICRON101
python pbem.py hold-turn --game OMICRON101
python pbem.py release-turn --game OMICRON101
python pbem.py run-turn --game OMICRON101 [--force]
python pbem.py advance-turn --game OMICRON101
```

## Moderator Actions

Players use `MODERATOR <text>` to submit free-text requests. The turn auto-holds; the GM responds; then the turn proceeds with the response embedded in the ship report.

```bash
python pbem.py list-actions --game OMICRON101 [--status all]
python pbem.py respond-action --game OMICRON101 --action-id 1 --response "Approved!"
python pbem.py release-turn --game OMICRON101
python pbem.py run-turn --game OMICRON101
```

## Order Moderation

```bash
python pbem.py review-orders --game OMICRON101
python pbem.py edit-order --game OMICRON101 --order-id N --command "MOVE M13"
python pbem.py delete-order --game OMICRON101 --order-id N
python pbem.py inject-order --game OMICRON101 --ship S --command "SCANSYSTEM"
```

## Player & Faction Management

```bash
python pbem.py edit-credits --game OMICRON101 --prefect-id P --credits 5000
python pbem.py suspend-player --email alice@example.com --game OMICRON101
python pbem.py faction-requests --game OMICRON101
python pbem.py approve-faction --game OMICRON101 --request-id N --note "Welcome!"
python pbem.py deny-faction --game OMICRON101 --request-id N --note "Not yet."
```

## Universe Building

```bash
python pbem.py add-system --name "Proxima" --spectral-type K1V
python pbem.py add-body --name "Haven" --system-id 102 --col K --row 8 --body-type Planet
python pbem.py add-link 101 102 --known
python pbem.py add-port --body-id 201 --name "Downport" --x 5 --y 3
python pbem.py list-universe
```

## Turn Resolution Phases

1. **Phase 1: Order gathering** — Load orders for all active ships (including overflow)
2. **Phase 1.1: Moderator check** — If MODERATOR orders found, create action records, auto-hold if any unresponded
3. **Phase 1.5: Wages** — Deduct crew wages (1 cr/crew + 5 cr/officer per week). GM prefects with unlimited credits are skipped.
4. **Phase 1.6: Faction changes** — Apply approved transfers, notify denied requests
5. **Phase 2: Interleaved resolution** — Priority queue by OC cost across all ships
6. **Phase 3: Reports** — Generate ship + prefect reports (ASCII + PDF). GM reports copied to gm_reports/.
7. **Phase 4: Cleanup** — Mark processed, save overflow, backup game state

## Worked Example: Full Turn with GM NPCs

### 1. Setup GM and NPCs
```bash
$ python pbem.py add-gm --game OMICRON101
  Account: 51747534, Orders: game_data/gm_orders/

$ python pbem.py add-gm-prefect --name "Admiral Voss" --faction 13
  Prefect: Admiral Voss (25782959), Faction: IMP

$ python pbem.py add-gm-ship --prefect 25782959 --ship-name "ISS Vengeance" \
    --hull-type Military --size 80 --system 101 --col M --row 13
  Ship: IMP ISS Vengeance (81224005) at M13
```

### 2. Submit GM Orders
```bash
$ cat > game_data/gm_orders/voss_patrol.yaml
game: OMICRON101
account: 51747534
ship: 81224005
orders:
  - SCANLOCATION: {}
  - MOVE: "H04"
```

### 3. Process All Orders
```bash
$ python pbem.py process-inbox --inbox ./inbox --game OMICRON101
  ORDERS ACCEPTED: 4 orders for ship 17761429 (Boethius)
  --- GM Orders (game_data/gm_orders/) ---
  GM ORDERS ACCEPTED: 2 orders for ship 81224005 (ISS Vengeance)
```

### 4. Run Turn
```bash
$ python pbem.py run-turn --game OMICRON101
  Account 51747534 -> GM (local)
    GM reports copied to: game_data/gm_reports/500.2/
  Account 38049441 -> alice@example.com
    Total files to email: 4
```

### 5. Send Reports and Advance
```bash
$ python pbem.py send-turns --credentials creds.json --game OMICRON101
  [51747534] GM account — reports in gm_reports/, skipping email
  38049441 -> alice@example.com: 4 files sent

$ python pbem.py advance-turn --game OMICRON101
  Turn advanced: 500.2 -> 500.3
```

## Troubleshooting

**Turn stuck in PROCESSING:** `python pbem.py release-turn --game OMICRON101` then retry.

**Skip moderator actions:** `python pbem.py run-turn --game OMICRON101 --force`

**Restore from backup:** `cp game_data/saves/game_state_500.2.db game_data/game_state.db`

**Player can't submit:** Check turn is OPEN (`turn-pipeline`), email correct (`list-players`), not suspended.

**GM orders not picked up:** Ensure `add-gm` was run first and the GM account number in the order file matches.
