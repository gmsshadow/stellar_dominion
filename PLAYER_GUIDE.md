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

Ships are built from modular internal components. Your starting Light Trader MK I (size 50) has 2500 ST of internal capacity, with 500 ST used and 2000 ST free for upgrades:

| Component | Qty | ST Used | Effect |
|-----------|:---:|--------:|--------|
| Standard Bridge | 1 | 20 | Required for ship operation |
| Thruster Array | 1 | 50 | 20 thrust → gravity rating 0.4 |
| Commercial Sublight Engine | 1 | 60 | Enables movement (1 of 5 optimal) |
| Cargo Bay | 5 | 200 | 500 ST cargo capacity |
| Crew Quarters | 1 | 30 | 20 crew capacity, 20 life support |
| Basic Sensor Array | 1 | 20 | Sensor rating 5 |
| Jump Drive Mk1 | 1 | 120 | Jump range 5, costs 150 OC |
| **Total** | | **500/2500** | **2000 ST free** |

Ship stats like cargo capacity, sensor rating, life support, and gravity rating are all derived from your installed components. To upgrade, install new components into your free ST space — or remove existing ones to make room.

### Engine Efficiency

Movement requires engines. The optimal number is 1 engine per 10 ship size (size 50 = 5 engines optimal). With fewer engines, MOVE costs increase. With zero engines, your ship cannot move at all. Your ship report shows your engine status (e.g. `Engines: 1/5 -> 20%`).

### Crew Efficiency

Your ship needs crew based on its size (1 crew per size point). If undermanned, OC costs for all actions increase proportionally. Officers count as crew but cost 5 cr/week vs 1 cr/week for regular crew. Life support capacity (from Crew Quarters) caps total crew + officers aboard.

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

## Operational Cycles (OC)

Every action costs OC. Your ship starts each turn with 300 OC.

| Command | Base OC Cost | Description |
|---------|-------------|-------------|
| **MOVE** | 2 per step | Move one grid square (engine/crew penalties may increase) |
| **SCANLOCATION** | 20 | Scan nearby area |
| **SCANSYSTEM** | 20 | Full system map |
| **ORBIT** | 10 | Enter orbit around a body |
| **DOCK** | 30 | Dock at a starbase |
| **UNDOCK** | 10 | Leave a starbase |
| **LAND** | 20 | Land on a planet surface |
| **TAKEOFF** | 20 | Take off to orbit |
| **SCANSURFACE** | 20 | Scan planet surface |
| **JUMP** | 150 | Jump to a linked star system |
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

**JUMP** `<system_id>` — Jump to another star system. Requires a Jump Drive. Cost depends on installed drive (Mk1: 150 OC). Must be at least 10 squares from the primary star.

### Scanning

**SCANLOCATION** — Shows objects near your position. **SCANSYSTEM** — Full 25×25 system map. **SCANSURFACE** — Planet terrain (must be landed or orbiting).

### Orbital & Docking

**ORBIT** `<body_id>` · **DOCK** `<base_id>` · **UNDOCK** · **LAND** `<body_id> <x> <y>` · **TAKEOFF**

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

**CHANGEFACTION** `<faction_id> [reason]` — Request to join a different faction (GM-moderated).

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
4. Buy more engines early — your starting ship has 1 of 5 optimal engines, making movement very expensive.
5. You have 2000 ST free — plenty of room for new components without removing anything.
6. Use `MODERATOR` for anything non-standard — the GM can modify your ship between orders.
