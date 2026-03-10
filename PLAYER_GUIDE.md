# Stellar Dominion — Player Guide

Welcome to Stellar Dominion, a play-by-email (PBEM) space strategy game. You command a starship, trade goods between starbases, recruit and promote crew, and explore a persistent galaxy. Each week you submit a set of orders and receive a detailed report showing what happened.

## Getting Started

When you join a game you receive:

- A **Prefect** — your in-game persona, with a name and starting credits (10,000 cr)
- A **Ship** — a Light Trader MK I (size 10, 500 ST internal capacity, 300 OC per turn)
- A **Starting crew** — 15 Human Crew in cargo plus a Captain officer
- A **Secret account number** — used to validate your orders. Keep this private.

Your ship starts in orbit around a planet in one of the game's star systems.

## Your Ship

Ships are built from modular internal components. Your starting Light Trader MK I (size 10) has 500 ST of internal capacity, fully loaded with:

| Component | Qty | ST Used | Effect |
|-----------|:---:|--------:|--------|
| Standard Bridge | 1 | 20 | Required for ship operation |
| Thruster Array | 1 | 50 | 20 thrust → gravity rating 2.0 |
| Commercial Sublight Engine | 1 | 60 | 1.0 efficiency (enables movement) |
| Cargo Bay | 5 | 200 | 500 ST cargo capacity |
| Crew Quarters | 1 | 30 | 20 crew capacity, 20 life support |
| Basic Sensor Array | 1 | 20 | Sensor rating 5 |
| Jump Drive Mk1 | 1 | 120 | Jump range 5, costs 150 OC |
| **Total** | | **500/500** | |

Ship stats like cargo capacity, sensor rating, life support, and gravity rating are all derived from your installed components. To upgrade one stat, you may need to remove a component to free ST space — for example, removing a Cargo Bay (freeing 40 ST) to install a Deep Space Scanner.

Your ship report shows a full breakdown of all installed components with their stats.

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
  - LOCATIONSCAN
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

LOCATIONSCAN
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
| **MOVE** | 2 per step | Move one grid square toward a coordinate |
| **LOCATIONSCAN** | 20 | Scan nearby area |
| **SYSTEMSCAN** | 20 | Full system map |
| **ORBIT** | 10 | Enter orbit around a body |
| **DOCK** | 30 | Dock at a starbase |
| **UNDOCK** | 10 | Leave a starbase |
| **LAND** | 20 | Land on a planet surface |
| **TAKEOFF** | 20 | Take off to orbit |
| **SURFACESCAN** | 20 | Scan planet surface |
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

## Command Reference

### Movement

**MOVE** `<coordinate>` — Move toward a grid coordinate (e.g. `MOVE F10`). Costs 2 OC per square. Ships without a Sublight Engine cannot move.

**JUMP** `<system_id>` — Jump to another star system. Requires a Jump Drive. Cost depends on installed drive (Mk1: 150 OC). Must be at least 10 squares from the primary star.

### Scanning

**LOCATIONSCAN** — Shows objects near your position. **SYSTEMSCAN** — Full 25×25 system map. **SURFACESCAN** — Planet terrain (must be landed).

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
UNINSTALL 130 1              # Free 40 ST internal by removing a Cargo Bay
BUY 45687590 152 1 INSTALL   # Buy + install Deep Space Scanner (40 ST, 1800 cr)
SCRAP 130 1                  # Destroy the uninstalled Cargo Bay from cargo
```

### Messaging & Moderator

**MESSAGE** `<target_id> <text>` — Send a message to any ship, base, or prefect.

**MODERATOR** `<text>` — Submit a free-text request to the GM. The turn auto-pauses for GM review. The response appears in your ship report. Use for anything non-standard: ship upgrades, special actions, negotiations, rule questions.

```
MODERATOR Can I refit my ship with a Deep Space Scanner? Willing to pay 1800 cr.
```

### Faction Changes

**CHANGEFACTION** `<faction_id> [reason]` — Request to join a different faction (GM-moderated).

## Crew & Efficiency

Your ship needs crew based on its size (1 crew per size point). If undermanned, OC costs increase proportionally. Officers count as crew but cost 5 cr/week vs 1 cr/week for regular crew.

Life support capacity (from Crew Quarters) caps total crew + officers aboard.

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

**Ship Report** — Order results, status block (size, ST capacity, components breakdown), navigation, crew, cargo, and contacts.

**Prefect Report** — Finances, all ships, between-turn messages, wage deductions, faction notifications.

## Tips

1. Scan first — `LOCATIONSCAN` and `SYSTEMSCAN` reveal the map.
2. Trade between bases — buy where goods are produced, sell where they're demanded.
3. Keep your ship crewed — undermanning increases all OC costs.
4. Use `MODERATOR` for anything non-standard — the GM can modify your ship between orders.
5. Your ship is fully loaded at 500/500 ST — to add components, you'll need to remove something first.
