# Stellar Dominion — Player Guide

Welcome to Stellar Dominion, a play-by-email (PBEM) space strategy game. You command a starship, trade goods between starbases, recruit and promote crew, and explore a persistent galaxy. Each week you submit a set of orders and receive a detailed report showing what happened.

## Getting Started

When you join a game you receive:

- A **Prefect** — your in-game persona, with a name and starting credits (10,000 cr)
- A **Ship** — a Light Trader MK I (size 50, 2500 ST internal capacity, 300 OC per turn)
- A **Starting crew** — 15 Human Crew in cargo plus a Captain officer
- A **Secret account number** — used to validate your orders. Keep this private.

Your ship starts in orbit around a planet in one of the game's star systems.

## Your Ship

Ships are built from modular internal components. Your starting Light Trader MK I (size 50) has 2500 ST of internal capacity, with 1850 ST used and 650 ST free for upgrades:

| Component | Qty | ST Used | Effect |
|-----------|:---:|--------:|--------|
| Standard Bridge | 1 | 50 | Required for ship operation |
| Thruster Array | 14 | 280 | 70 thrust → gravity rating 1.02 |
| Commercial Sublight Engine | 5 | 50 | Movement (5/5 optimal for size 50) |
| Cargo Bay | 50 | 1250 | 1000 ST cargo capacity |
| Crew Quarters | 4 | 120 | 80 crew capacity, 80 life support |
| Basic Sensor Array | 5 | 50 | Sensor rating 25 |
| Jump Drive Mk1 | 1 | 50 | Range 5 systems, 50 OC per activation |
| **Total** | | **1850/2500** | **650 ST free** |

Ship stats like cargo capacity, sensor rating, life support, and gravity rating are all derived from your installed components. To upgrade, install new components into your free ST space — or remove existing ones to make room.

### Engine Efficiency

Movement requires engines. The optimal number is 1 engine per 10 ship size (size 50 = 5 engines optimal). With fewer engines, MOVE costs increase. With zero engines, your ship cannot move at all. Your ship report shows your engine status (e.g. `Engines: 5/5 -> 100%`).

### Crew Efficiency

Your ship needs crew based on its size (1 crew per 2 hull points, rounded up). A size 50 ship needs 25 crew. If undermanned, OC costs for all actions increase proportionally. Officers count as crew but cost 5 cr/week vs 1 cr/week for regular crew. Life support capacity (from Crew Quarters) caps total crew + officers aboard.

### Gravity Rating

Your ship's gravity rating reflects how responsive it is in a gravity well. It's calculated from thrust vs effective mass:

```
effective_mass = ship_size + (total_installed_st / 100)
gravity_rating = total_thrust / effective_mass
```

The hull itself contributes mass equal to its size, and every 100 ST of installed components adds another mass unit. An empty 50-hull ship is lighter than one packed to 2500 ST of components. Building a fast ship means balancing thrust, hulls, and how much gear you bolt on.

Gravity rating directly affects three commands:

- **ORBIT**: cost = `ceil(10 × body_gravity / ship_gravity)`
- **LAND**: cost = `ceil(20 × body_gravity / ship_gravity)`
- **TAKEOFF**: cost = `ceil(20 × body_gravity / ship_gravity)`

A starter ship with gravity 1.0 lands on a 1g planet for 20 OC. Strip out some cargo bays or add thrusters and that drops. Pack the ship full of trade goods and mining rigs, and the cost climbs. A ship with no thrusters cannot orbit, land, or take off at all.

### Sensor Profile

Every ship has a **sensor profile** equal to `ship_size / 100`. A size 50 starter has profile 0.5; a size 300 cruiser has profile 3.0. This represents how easy your ship is to detect — bigger ships throw off a stronger signature. It's compared against another ship's sensor rating when scans happen, so hiding a small scout is much easier than concealing a freighter or warship. Your sensor profile is shown in the Navigation Report.

## Your Identifiers

| Identifier | Visibility | Purpose |
|------------|-----------|---------|
| **Account Number** | Secret — only you and the GM know it | Validates your orders |
| **Prefect ID** | Public — discoverable by other players | Your in-game identity |
| **Ship ID** | Public — visible on scans | Identifies your ship |

## Submitting Orders

Orders can be submitted as YAML or plain text files.

### YAML Format
```yaml
game: OMICRON101
ship: 52589098
account: 87654321
orders:
  - SCANLOCATION
  - MOVE: F10
  - DOCK: 45687590
  - BUY: {base: 45687590, item: 100101, qty: 50}
  - UNDOCK
```

### Text Format
```
game: OMICRON101
ship: 52589098
account: 87654321

SCANLOCATION
MOVE F10
DOCK 45687590
BUY 45687590 100101 50
UNDOCK
```

Orders are processed in sequence. Failed orders are dropped. If you run out of OC, remaining orders carry forward to next turn as overflow.

### Prefect Orders

Some orders are filed against your **prefect** rather than a ship — things like CHANGEFACTION that apply to you as a player, not to any specific vessel. For these, use a `prefect` field instead of `ship`:

```yaml
game: OMICRON101
prefect: 48814452
account: 87654321
orders:
  - CHANGEFACTION: {faction_id: 12, reason: "Tired of the academy"}
```

Or in text format:
```
game: OMICRON101
prefect: 48814452
account: 87654321

CHANGEFACTION 12 Tired of the academy
```

A single order file belongs to exactly one subject — either a ship or a prefect, never both. Ship-only commands filed in a PREFECT block (or prefect-only commands in a SHIP block) will be rejected with a clear error message.

## Operational Cycles (OC)

Every action costs OC. Your ship starts each turn with 300 OC.

| Command | Base OC Cost | Description |
|---------|-------------|-------------|
| **MOVE** | 2 per step | Move one grid square (engine/crew penalties may increase) |
| **SCANLOCATION** | 20 | Scan nearby area |
| **SCANSYSTEM** | 20 | Full system map |
| **ORBIT** | 10 × body_grav / ship_grav | Enter orbit around a body |
| **LEAVEORBIT** | 0 | Leave orbit, return to grid square |
| **DOCK** | 30 | Dock at a starbase |
| **UNDOCK** | 10 | Leave a starbase |
| **LAND** | 20 × body_grav / ship_grav | Land on a planet surface |
| **TAKEOFF** | 20 × body_grav / ship_grav | Take off to orbit |
| **SCANSURFACE** | 20 | Scan planet surface |
| **JUMP** | per drive | Jump to a star system (Mk1: 50 OC per activation, range 5 hops) |
| **MAKEOFFICER** | 10 | Promote a crew member |
| **INSTALL** | 10 | Install a component from cargo |
| **UNINSTALL** | 10 | Uninstall a component to cargo |
| **SCRAP** | 0 | Scrap a component from cargo |
| **BUY / SELL** | 0 | Trade at market (docked) |
| **GETMARKET** | 0 | View prices (docked) |
| **MESSAGE** | 0 | Send message to any position |
| **MODERATOR** | 0 | Free-text request to GM |
| **WAIT** | variable | Wait a number of OC |
| **RENAMESHIP/BASE/PREFECT/OFFICER** | 0 | Rename things |
| **CHANGEFACTION** | 0 | Request faction change |
| **CLEAR** | 0 | Cancel overflow orders |

Note: old command names LOCATIONSCAN, SYSTEMSCAN, and SURFACESCAN still work as aliases.

## Command Reference

### Movement

**MOVE** `<coordinate>` — Move toward a grid coordinate (e.g. `MOVE F10`). Base cost is 2 OC per square, but engine efficiency and crew undermanning may increase this. Ships without a Sublight Engine cannot move.

**JUMP** `<system_id>` — Jump to another star system. Requires a Jump Drive. The drive determines the maximum range per activation and the OC cost per activation. Jump Drive Mk1 covers up to 5 system hops per activation at 50 OC; Mk2 covers up to 10 hops at 40 OC. Destinations beyond the drive's range require multiple activations (e.g. a system 8 hops away with Mk1 = 2 activations = 100 OC). Must be at least 10 squares from the primary star (waived in starless nexus systems).

### Scanning

**SCANLOCATION** — Shows objects near your position. **SCANSYSTEM** — Full 25×25 system map. **SCANSURFACE** — Planet terrain (must be landed or orbiting).

### Orbital & Docking

**ORBIT** `<body_id>` · **LEAVEORBIT** · **DOCK** `<base_id>` · **UNDOCK** · **LAND** `<body_id> <x> <y>` · **TAKEOFF**

**LEAVEORBIT** — Leave orbit and return to the grid square in open space. No OC cost. You can also leave orbit implicitly by issuing a MOVE command, but LEAVEORBIT is clearer when you just want to break orbit without moving.

### Trading

**GETMARKET** `<base_id>` — View prices. **BUY** `<base_id> <item_id> <quantity>` — Buy items (capped to available stock/cargo). **SELL** `<base_id> <item_id> <quantity>` — Sell items.

### Crew & Officers

**MAKEOFFICER** `<ship_id> <crew_type_id> [name]` — Promote one crew to officer.

### Ship Components

Components can be bought at any starbase at catalogue prices, installed from cargo, uninstalled back to cargo, or scrapped. Components in cargo take up cargo space equal to their ST cost.

**BUY with INSTALL** — Buy a component and install it directly, bypassing cargo. Checks ST capacity.

```
BUY 45687590 152 1 INSTALL         # Buy + install a Deep Space Scanner
```

YAML: `{base: 45687590, item: 152, qty: 1, install: true}`

Without the INSTALL flag, bought components go to cargo as normal items.

**INSTALL** `<component_id> [quantity]` — Install a component from cargo into the ship. Costs 10 OC. Checks that total installed ST doesn't exceed ship capacity (`ship_size × 50`).

```
INSTALL 130 2          # Install 2× Cargo Bay from cargo
INSTALL 152            # Install 1× Deep Space Scanner
```

**UNINSTALL** `<component_id> [quantity]` — Remove an installed component to cargo. Costs 10 OC. Checks cargo space is available. Note: uninstalling cargo bays reduces your cargo capacity, so make sure you have room.

```
UNINSTALL 130 1        # Uninstall 1× Cargo Bay to cargo
```

**SCRAP** `<component_id> [quantity]` — Destroy a component from cargo. Free (0 OC). The component is permanently lost.

```
SCRAP 130 1            # Destroy 1× Cargo Bay from cargo
```

**Typical refit sequence** (while docked at a starbase):
```
BUY 45687590 152 1 INSTALL   # Buy + install Deep Space Scanner (40 ST, 1800 cr)
BUY 45687590 120 4 INSTALL   # Buy + install 4 more engines (240 ST, 4800 cr)
```

### Messaging & Moderator

**MESSAGE** `<target_id> <text>` — Send a message to any ship, base, or prefect.

**MODERATOR** `<text>` — Submit a free-text request to the GM. The turn auto-pauses for GM review. The response appears in your ship report. Use for anything non-standard: special actions, negotiations, rule questions.

### Faction Changes

**CHANGEFACTION** `<faction_id> [reason]` — Request to join a different faction (GM-moderated). This is a **prefect-scoped order** — file it in a `prefect:` block, not a `ship:` block. The turn auto-holds when you submit this so the GM can review; once approved your faction updates immediately and all your ships fly the new banner.

## Trading Economy

Starbases have markets with rotating prices on a 4-week cycle. Each base specialises in one good (cheap), trades another at average, and demands a third (expensive). Human Crew is fixed-price at all bases (buy 5 cr, sell 3 cr).

## Factions

| ID | Abbrev | Name |
|----|--------|------|
| 0 | IND | Independent |
| 11 | STA | Stellar Training Academy (default) |
| 12 | MTG | Merchant Trade Guild |
| 13 | IMP | Imperial Navy |
| 14 | FRN | Frontier Coalition |
| 15 | SYN | Syndicate |

## Reading Your Reports

**Ship Report** — Order results, status block (size, ST capacity, engine/crew efficiency, components table), navigation, crew, cargo, and contacts.

**Prefect Report** — Finances, all ships, between-turn messages, wage deductions, faction notifications.

## Tips

1. Scan first — `SCANLOCATION` and `SCANSYSTEM` reveal the map.
2. Trade between bases — buy where goods are produced, sell where they're demanded.
3. Keep your ship crewed — undermanning increases all OC costs.
4. Your starter has 5/5 engines and 1000 ST cargo — ready for trading from day one.
5. You have 650 ST free — room for additional sensors, jump drive upgrades, or specialised modules.
6. Use `MODERATOR` for anything non-standard — the GM can modify your ship between orders.
