# Stellar Dominion — Player Guide

Welcome to Stellar Dominion, a play-by-email (PBEM) space strategy game. You command a starship, trade goods between starbases, recruit and promote crew, and explore a persistent galaxy. Each week you submit a set of orders and receive a detailed report showing what happened.

## Getting Started

When you join a game you receive:

- A **Prefect** — your in-game persona, with a name and starting credits (10,000 cr)
- A **Ship** — a Light Trader MK I (50 hull, 500 MU cargo, 300 TU per turn)
- A **Starting crew** — 15 Human Crew in cargo plus a Captain officer
- A **Secret account number** — used to validate your orders. Keep this private.

Your ship starts in orbit around a planet in one of the game's star systems. From there you can move, scan, dock at starbases, trade, and jump between systems.

## Your Identifiers

| Identifier | Visibility | Purpose |
|------------|-----------|---------|
| **Account Number** | Secret — only you and the GM know it | Validates your orders. Never shown in reports or scans. |
| **Prefect ID** | Public — other players can discover it | Your in-game identity for diplomacy and contacts. |
| **Ship ID** | Public — visible on scans | Identifies your ship on the map. |

## Submitting Orders

Orders can be submitted as YAML or plain text files. Both formats require your ship ID and account number.

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

Orders are processed in sequence. If an order fails (e.g. you try to dock but aren't at the base's location), that order is dropped and the next one executes. If you run out of TU, remaining orders carry forward to next turn as overflow.

## Time Units (TU)

Every action costs TU. Your ship starts each turn with 300 TU. Once you run out, remaining orders carry forward to next turn automatically.

| Command | Base TU Cost | Description |
|---------|-------------|-------------|
| **MOVE** | 2 per step | Move one grid square toward a target coordinate |
| **LOCATIONSCAN** | 20 | Scan the area around your current position |
| **SYSTEMSCAN** | 20 | Produce a full system map |
| **ORBIT** | 10 | Enter orbit around a celestial body |
| **DOCK** | 30 | Dock at a starbase (must be at same location) |
| **UNDOCK** | 10 | Leave a starbase |
| **LAND** | 20 | Land on a planet surface at specific coordinates |
| **TAKEOFF** | 20 | Take off from a planet surface back to orbit |
| **SURFACESCAN** | 20 | Scan the surface of the planet you're landed on |
| **JUMP** | 60 | Jump to a linked star system via hyperspace |
| **MAKEOFFICER** | 10 | Promote a crew member to officer |
| **BUY / SELL** | 0 | Trade at a starbase market (must be docked) |
| **GETMARKET** | 0 | View market prices at a starbase (must be docked) |
| **MESSAGE** | 0 | Send a message to any ship, base, or prefect |
| **MODERATOR** | 0 | Submit a free-text request to the GM |
| **WAIT** | variable | Wait a specific number of TU |
| **RENAMESHIP** | 0 | Rename your ship |
| **RENAMEBASE** | 0 | Rename a starbase you own |
| **RENAMEPREFECT** | 0 | Rename your prefect |
| **RENAMEOFFICER** | 0 | Rename an officer on your ship |
| **CHANGEFACTION** | 0 | Request a faction change (GM-moderated) |
| **CLEAR** | 0 | Cancel all overflow orders from previous turns |

## Command Reference

### Movement

**MOVE** `<coordinate>` — Move toward a grid coordinate (e.g. `MOVE F10`). The ship pathfinds step by step, costing 2 TU per square. Coordinates use letters A-Y for columns and numbers 01-25 for rows.

**JUMP** `<system_id>` — Jump to another star system. Costs 60 TU. Requirements: not docked/landed/orbiting, at least 10 squares from the primary star, and a hyperspace link must exist between systems.

### Scanning

**LOCATIONSCAN** — Shows objects near your position (ships, starbases, celestial bodies) and basic details about nearby contacts.

**SYSTEMSCAN** — Produces a full 25×25 ASCII map of the current star system showing all known objects.

**SURFACESCAN** — When landed on a planet, shows the terrain grid, surface ports, and outposts.

### Orbital & Docking

**ORBIT** `<body_id>` — Enter orbit around a planet or moon. You must be at the same grid coordinates.

**DOCK** `<base_id>` — Dock at a starbase. You must be at the same location. Docking enables trading (BUY/SELL/GETMARKET).

**UNDOCK** — Leave the starbase you're docked at.

**LAND** `<body_id> <x> <y>` — Land on a planet surface at grid coordinates. You must be orbiting the body.

**TAKEOFF** — Return to orbit from a planet surface.

### Trading

**GETMARKET** `<base_id>` — View current prices, stock, and demand at a starbase. Must be docked.

**BUY** `<base_id> <item_id> <quantity>` — Buy items from the base market. If you request more than available stock or more than your cargo hold can fit, the order is capped to the maximum possible (not rejected).

**SELL** `<base_id> <item_id> <quantity>` — Sell items to the base market. Capped to available demand.

YAML format for trades:
```yaml
- BUY: {base: 45687590, item: 100101, qty: 50}
- SELL: {base: 45687590, item: 100103, qty: 25}
```

### Crew & Officers

**MAKEOFFICER** `<ship_id> <crew_type_id> [name]` — Promote one crew cargo item to an officer. Consumes 1 crew from cargo, creates a new Ensign. Optionally provide a name, otherwise one is generated randomly.

```
MAKEOFFICER 52589098 401 Marcus Varro    # Named officer
MAKEOFFICER 52589098 401                 # Random name
```

### Messaging

**MESSAGE** `<target_id> <text>` — Send a free-text message to any position (ship, starbase, or prefect). The message is delivered in the recipient's next between-turn report. Messages to starbases are routed to the owning prefect.

```
MESSAGE 78901234 Greetings captain, interested in a trade alliance?
```

### Moderator Actions

**MODERATOR** `<text>` — Submit a free-text request directly to the Game Master. This is the way to ask for anything that isn't covered by a standard order: ship upgrades, special actions, rule clarifications, narrative requests, or anything else that requires GM adjudication.

```
MODERATOR Can I retrofit my ship with better sensors? Willing to pay 500 cr.
MODERATOR I want to attempt to negotiate passage through the blockade.
MODERATOR Requesting permission to establish a mining outpost on Tartarus IV.
```

YAML format:
```yaml
- MODERATOR: Can I retrofit my ship with better sensors?
- MODERATOR: {text: "I want to negotiate passage through the blockade."}
```

When your orders include a MODERATOR request, the turn **automatically pauses** before resolution. The GM reviews your request, writes a response, and then the turn continues. The GM's response appears in your ship report alongside the order result:

```
MODERATOR REQUEST: "Can I retrofit my ship with better sensors?"
  GM RESPONSE: "Approved! Advanced Sensors installed. 500 cr deducted."
```

Because the turn pauses before resolution, the GM can make any necessary game-state changes (editing your ship, adjusting credits, placing items) before your remaining orders execute. This means a MODERATOR request early in your order list can affect the outcome of later orders.

You can include multiple MODERATOR requests in a single turn, and they can appear anywhere in your order sequence. The turn won't proceed until the GM has responded to all of them.

### Renaming

All rename commands are free (0 TU):

```
RENAMESHIP 52589098 The Indomitable
RENAMEBASE 45687590 Varro Station
RENAMEPREFECT 24162199 Lord Varro
RENAMEOFFICER 52589098 1 Helena Blackwood    # 1 = crew number from report
```

### Faction Changes

**CHANGEFACTION** `<faction_id> [reason]` — Request to join a different faction. This is free (0 TU) but requires GM approval. Only one pending request at a time.

```
CHANGEFACTION 12 Want to focus on trading routes
```

The GM will approve or deny the request, and you'll be notified in your next between-turn report.

## Overflow Orders

If you run out of TU mid-turn, your remaining orders automatically carry forward to next turn. They run before your new orders. Use **CLEAR** to cancel all overflow orders if you change your mind.

## Crew & Efficiency

Your ship has a crew requirement based on hull size (1 crew per 5 hull). If you're undermanned, all TU costs increase:

```
Efficiency = crew_count / crew_required × 100%
TU penalty = (100% - efficiency)

Example: 8 crew out of 10 required = 80% efficiency = +20% TU penalty
  MOVE: 2 TU → 3 TU per step
  LOCATIONSCAN: 20 TU → 24 TU
  JUMP: 60 TU → 72 TU
```

Officers count as crew for efficiency purposes but cost more in wages (5 cr/week vs 1 cr/week for regular crew). Life support capacity (default 20) caps total crew + officers aboard.

## Trading Economy

Starbases have markets with rotating prices on a 4-week cycle. Each base specialises in one good (cheap to buy), trades another at average price, and demands a third (expensive to buy from you). This creates profitable circular trade routes.

**Human Crew** is a special fixed-price item available at all starbases: buy at 5 cr, sell at 3 cr, 100 stock per cycle.

Markets refresh every 4 weeks. Stock depletes as players buy, demand depletes as players sell. GETMARKET shows a countdown to the next refresh.

## Factions

Every prefect belongs to a faction. New players start in the **Stellar Training Academy (STA)**. Available factions:

| ID | Abbrev | Name | Description |
|----|--------|------|-------------|
| 0 | IND | Independent | No faction affiliation |
| 11 | STA | Stellar Training Academy | Default starting faction |
| 12 | MTG | Merchant Trade Guild | Traders and commerce captains |
| 13 | IMP | Imperial Navy | Military arm of the Terran Empire |
| 14 | FRN | Frontier Coalition | Settlers and explorers |
| 15 | SYN | Syndicate | Smugglers, pirates, and opportunists |

Your faction prefix appears on your ship name in reports and scans (e.g. "STA Boethius", "MTG Resolute").

## Reading Your Reports

Each turn you receive two reports:

**Ship Report** — Detailed results of every order you submitted, plus a full status block showing your ship's position, hull, cargo, crew, officers, and installed systems. If you submitted a MODERATOR request, the GM's response is shown inline with your order results.

**Prefect Report** — Overview of your prefect's finances, all ships under your command, and a between-turn section showing wage deductions, incoming messages, and faction change notifications.

Both are available as plain text (.txt) and PDF (.pdf).

## Tips for New Players

1. Start by scanning — `LOCATIONSCAN` and `SYSTEMSCAN` reveal the map around you.
2. Dock at a starbase and check prices with `GETMARKET`.
3. Buy cheap goods at one base, sell dear at another — look for the produce/demand pattern.
4. Keep your ship crewed — undermanning increases all TU costs.
5. Promote an officer or two — they're worth the extra wages for their crew factors.
6. Send messages to other players via `MESSAGE` to negotiate trades or alliances.
7. Use `MODERATOR` to ask the GM for anything non-standard — upgrades, special actions, narrative requests.
8. Use `CLEAR` if your overflow orders from last turn no longer make sense.
