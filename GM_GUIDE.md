# Stellar Dominion — Game Master Guide

This guide covers game setup, the turn pipeline, moderator actions, ship components, universe building, and a worked turn example.

## Setting Up a Game

```bash
python pbem.py setup-game --demo        # Creates OMICRON101 with 2 players, 3 systems
python pbem.py join-game --game OMICRON101   # Interactive player registration
python pbem.py list-players --game OMICRON101
python pbem.py list-ships --game OMICRON101
python pbem.py show-map --game OMICRON101
python pbem.py list-components           # Ship component catalogue
```

## Ship Component System

Ships are built from modular internal components stored in the `ship_components` catalogue (universe.db). Each component has a 3-digit ID for future trading.

### ST Capacity

Ship internal capacity is determined by size: `ST_capacity = ship_size × 50`. The starting Light Trader MK I is size 10 = 500 ST.

### Component Categories

| Range | Category | Key Stats |
|-------|----------|-----------|
| 100-109 | Bridge/Command | Required for operation |
| 110-119 | Thrusters | Thrust → gravity rating |
| 120-129 | Sublight Engines | Efficiency → movement cost |
| 130-139 | Cargo Systems | Cargo capacity (ST) |
| 140-149 | Crew Quarters | Crew capacity + life support |
| 150-159 | Sensors | Sensor rating |
| 160-169 | Jump Drives | Jump range + OC cost |

### Stat Derivation

All ship stats are calculated from installed components:

- **cargo_capacity** = sum of all cargo_capacity values
- **life_support_capacity** = sum of all life_capacity values
- **sensor_rating** = sum of all sensor_rating values
- **gravity_rating** = total_thrust / ship_size
- **crew_required** = ship_size (1 per size point)
- **engine_efficiency** = sum (capped at 1 engine per 10 size)
- **jump_range** = best installed jump drive range

When components change, call `recalculate_ship_stats()` to update the ship record.

### Starting Ship (Light Trader MK I)

```
Standard Bridge ×1      20 ST
Thruster Array ×1       50 ST  (thrust 20 → gravity 2.0)
Commercial Engine ×1    60 ST  (efficiency 1.0)
Cargo Bay ×5           200 ST  (500 cargo)
Crew Quarters ×1        30 ST  (20 crew, 20 life)
Basic Sensor Array ×1   20 ST  (sensor 5)
Jump Drive Mk1 ×1     120 ST  (range 5, 150 OC)
                       -------
Total:                 500/500 ST
```

### Viewing Components

```bash
python pbem.py list-components    # Full catalogue with stats and prices
```

### Adding Components to universe.db

New components can be added directly to the `ship_components` table in universe.db. They'll be available immediately. Key columns: `component_id`, `name`, `category`, `st_cost`, and the relevant stat columns.

### Component Orders

Players can refit ships at starbases using these orders:

- **BUY base_id comp_id qty INSTALL** — Buy and install directly (checks ST capacity)
- **BUY base_id comp_id qty** — Buy to cargo (components take `st_cost` as cargo mass)
- **INSTALL comp_id [qty]** — Install from cargo (10 OC, checks ST capacity)
- **UNINSTALL comp_id [qty]** — Remove to cargo (10 OC, checks cargo space)
- **SCRAP comp_id [qty]** — Destroy from cargo (0 OC)

Components are sold at catalogue `base_price` at any starbase. Hull restrictions (e.g. `military`) are enforced on buy and install.

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
python pbem.py respond-action --game OMICRON101 --action-id 1 --response "Approved! Sensors upgraded."
python pbem.py release-turn --game OMICRON101
python pbem.py run-turn --game OMICRON101
```

## Order Moderation

```bash
python pbem.py review-orders --game OMICRON101
python pbem.py edit-order --game OMICRON101 --order-id N --command "MOVE M13"
python pbem.py delete-order --game OMICRON101 --order-id N
python pbem.py inject-order --game OMICRON101 --ship S --command "SYSTEMSCAN"
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
3. **Phase 1.5: Wages** — Deduct crew wages (1 cr/crew + 5 cr/officer per week)
4. **Phase 1.6: Faction changes** — Apply approved transfers, notify denied requests
5. **Phase 2: Interleaved resolution** — Priority queue by OC cost across all ships
6. **Phase 3: Reports** — Generate ship + prefect reports (ASCII + PDF)
7. **Phase 4: Cleanup** — Mark processed, save overflow, backup game state

## Worked Example: Running a Turn

### 1. Check Pipeline
```bash
$ python pbem.py turn-pipeline --game OMICRON101
  Status: OPEN - accepting orders
  Ships without orders: 2
```

### 2. Submit Orders
```bash
$ python pbem.py submit-orders alice_orders.yaml --email alice@example.com
  Orders filed: 4 orders for ship 17761429
```

Alice's orders include a MODERATOR request:
```yaml
orders:
  - MODERATOR: Can I swap a Cargo Bay for a Deep Space Scanner?
  - LOCATIONSCAN
  - MOVE: H15
```

### 3. Run Turn (auto-holds)
```bash
$ python pbem.py run-turn --game OMICRON101
  *** TURN AUTO-HELD: 1 moderator action(s) require GM response ***
    #1: Li Chen/Boethius: "Can I swap a Cargo Bay for a Deep Space Scanner?"
```

### 4. GM Responds
```bash
$ python pbem.py respond-action --game OMICRON101 --action-id 1 \
    --response "Approved! Cargo Bay removed, Deep Space Scanner installed. 1200 cr deducted."
```

The GM could also modify the ship's components in the database directly at this point.

### 5. Release and Run
```bash
$ python pbem.py release-turn --game OMICRON101
$ python pbem.py run-turn --game OMICRON101
  === Turn 500.3 resolution complete ===
```

### 6. Send Reports and Advance
```bash
$ python pbem.py send-turns --credentials creds.json --game OMICRON101
$ python pbem.py advance-turn --game OMICRON101
  Turn advanced: 500.3 -> 500.4
```

## Troubleshooting

**Turn stuck in PROCESSING:** `python pbem.py release-turn --game OMICRON101` then retry.

**Skip moderator actions:** `python pbem.py run-turn --game OMICRON101 --force`

**Restore from backup:** `cp game_data/saves/game_state_500.2.db game_data/game_state.db`

**Player can't submit:** Check turn is OPEN (`turn-pipeline`), email correct (`list-players`), not suspended.
