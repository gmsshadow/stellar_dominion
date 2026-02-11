# Stellar Dominion – PBEM Strategy Game Engine v1.0

A play-by-email (PBEM) grand strategy game engine inspired by Phoenix-BSE style games.
Deterministic turn resolution, ASCII reports, persistent SQLite universe.

## Quick Start

```bash
# 1. Set up a demo game (Omicron system with 2 players)
python pbem.py setup-game --demo

# 2. List ships to find IDs, account numbers, and owners
python pbem.py list-ships --game OMICRON101

# 3. New players can join at any time
python pbem.py join-game --game OMICRON101

# 4. View the system map
python pbem.py show-map --game OMICRON101

# 5. Submit orders (--email identifies the submitting player)
python pbem.py submit-orders orders.yaml --email alice@example.com

# 6. Check who has submitted orders
python pbem.py turn-status --game OMICRON101

# 7. Resolve the turn and generate reports
python pbem.py run-turn --game OMICRON101 -v

# 8. Advance to the next turn (resets all TUs)
python pbem.py advance-turn --game OMICRON101
```

## Requirements

- Python 3.10+
- PyYAML (`pip install pyyaml`)
- SQLite (built-in)

## Project Structure

```
stellar_dominion/
├── pbem.py                          # Main CLI entry point
├── db/
│   └── database.py                  # SQLite schema & connection
├── engine/
│   ├── game_setup.py                # Game/player creation & join-game registration
│   ├── turn_folders.py              # Turn folder manager (incoming/processed)
│   ├── maps/
│   │   └── system_map.py            # 25x25 ASCII grid renderer
│   ├── orders/
│   │   └── parser.py                # YAML & text order parser
│   ├── resolution/
│   │   └── resolver.py              # Turn resolution engine (TU system)
│   └── reports/
│       └── report_gen.py            # Phoenix-style ASCII report generator
└── game_data/                       # Created by setup-game
    ├── stellar_dominion.db          # Persistent game database
    └── turns/
        ├── incoming/                # Player orders filed by email address
        └── processed/               # Resolved reports filed by account number
```

## Player Identity

Each player has three types of identifier:

| Identifier | Visibility | Purpose |
|------------|-----------|---------|
| **Account Number** | Secret — known only to the player and the GM | Used for order validation and report folder routing. Never appears in reports or scans. |
| **Political ID** | Public — discoverable by other players via scanning | In-game identity for diplomacy, contacts, and ownership. |
| **Ship/Base IDs** | Public — discoverable via scanning | Identify assets on the map. |

The account number is generated when a player joins the game and must be kept
secret. It is used alongside the player's email address to validate order
submissions. Political IDs and ship IDs are the public-facing identifiers that
other players encounter through the game's scanning and contact systems.

## Joining a Game

New players can join at any point during the game using the interactive
registration form:

```bash
python pbem.py join-game --game OMICRON101
```

The form prompts for:
- **Real name** — the player's name (for GM reference)
- **Email address** — must be unique, used for order submission and report delivery
- **Political character name** — in-game identity (e.g. "Admiral Chen", "Warlord Zax")
- **Ship name** — the player's starting vessel (e.g. "VFS Boethius", "SS Vengeance")

The engine then:
1. Generates a unique account number, political ID, and ship ID
2. Picks a random starbase and docks the new ship there
3. Creates the political position with 10,000 starting credits
4. Displays the account number with a reminder to keep it secret

Players can also be added directly by the GM using `add-player` with explicit
parameters.

## Turn Folder Structure

Orders and reports are managed through a structured folder layout that separates
incoming orders (keyed by email) from processed output (keyed by account number).

### Incoming — `turns/incoming/{turn}/{email}/`

When a player submits orders, they are filed under the sender's email address.
This is the natural key at the point of arrival (especially for future IMAP
integration where the email address comes from the envelope).

```
incoming/
  500.1/
    alice@example.com/
      orders_57131458.yaml          # Accepted orders for ship 57131458
      orders_57131458.yaml.receipt  # Confirmation with timestamp & order count
    bob@example.com/
      orders_17579149.yaml          # Bob's valid orders
      orders_17579149.yaml.receipt
      rejected_57131458.yaml        # Bob tried to submit for Alice's ship
      rejected_57131458.reason      # Explanation of why it was rejected
```

Validation at submission checks:
- Is the email registered to a player in this game?
- Does that player's political position own the ship?
- Are there any valid orders after parsing?

If validation fails, the orders are stored as `rejected_` with a `.reason` file.
Resubmitting valid orders for the same ship replaces the previous submission.

### Processed — `turns/processed/{turn}/{account_number}/`

Resolved reports are filed under the player's account number — a secret,
permanent identifier that never changes. The email address is looked up from
the database at send time, keeping the folder structure stable even if a
player updates their email.

```
processed/
  500.1/
    25384359/                       # Alice's account number (secret)
      ship_57131458.txt             # Ship turn report
      political_76106713.txt        # Political summary (finances, fleet, contacts)
    13475868/                       # Bob's account number (secret)
      ship_17579149.txt
      political_57142790.txt
```

Using account numbers rather than political IDs for the folder structure means
that even if a player shares their political ID with another player (through
diplomacy, contacts, or scanning), it doesn't expose where their turn reports
are stored on the file system.

When SMTP integration is added, the send step simply iterates each account
folder, looks up the email from the database, and sends everything in the folder.

## The Omicron System (101)

The demo game creates the **Omicron** star system — a 25×25 grid:

| Object              | Type       | Location | Notes                      |
|---------------------|------------|----------|----------------------------|
| Omicron Prime       | Star       | M13      | Central star               |
| Orion (247985)      | Planet     | H04      | 0.9g, Standard atmosphere  |
| Tartarus (301442)   | Planet     | R08      | 1.2g, Dense atmosphere     |
| Leviathan (155230)  | Gas Giant  | E18      | 2.5g, Hydrogen             |
| Callyx (88341)      | Moon       | F19      | Moon of Leviathan, 0.3g    |
| Meridian (412003)   | Planet     | T20      | 0.7g, Thin atmosphere      |
| Citadel Station     | Starbase   | H04      | Market, 5 docking slots    |
| Tartarus Depot      | Outpost    | R08      | Market, 3 docking slots    |
| Meridian Waystation | Outpost    | T20      | Market, 3 docking slots    |

### Map Symbols
```
*  Star          @  Player Ship     B  Base/Station
O  Planet        o  Moon            G  Gas Giant
#  Asteroid      ?  Detected        .  Empty Space
```

## Orders Format

### YAML (preferred)
```yaml
game: OMICRON101
ship: 12345678
orders:
  - WAIT: 50
  - MOVE: M13
  - LOCATIONSCAN: {}
  - MOVE: H04
  - ORBIT: 247985
  - DOCK: 45687590
```

### Plain Text
```
GAME OMICRON101
SHIP 12345678
WAIT 50
MOVE M13
LOCATIONSCAN
MOVE H04
ORBIT 247985
DOCK 45687590
```

## Supported Commands (v1)

| Command          | TU Cost | Description                      |
|------------------|---------|----------------------------------|
| `WAIT {n}`       | n       | Consume n TU doing nothing       |
| `MOVE {coord}`   | 20      | Move to grid coordinate (e.g. M13) |
| `LOCATIONSCAN`   | 20      | Scan nearby cells for objects    |
| `SYSTEMSCAN`     | 20      | Produce full system ASCII map    |
| `ORBIT {bodyId}` | 10      | Enter orbit of a celestial body  |
| `DOCK {baseId}`  | 30      | Dock at a starbase               |
| `UNDOCK`          | 10      | Leave docked starbase            |

## Turn Resolution Rules

- Each ship starts each turn with a fixed TU allowance (default: 300)
- Orders execute **sequentially** in the order listed
- If a ship has insufficient TU, the order is skipped and added to **pending**
- Failed orders (wrong location, etc.) are logged and added to pending
- Failed orders do **not** deduct TU
- Resolution is **deterministic** using a seeded RNG

## Turn Number Format

Turns follow `YEAR.WEEK` format: `500.1` through `500.52`, then `501.1`.

## CLI Commands

| Command | Description |
|---------|-------------|
| `setup-game [--demo]` | Create a new game (--demo adds 2 test players) |
| `join-game [--game ID]` | Interactive new player registration |
| `add-player --name --email` | Add a player directly (GM command) |
| `submit-orders <file> --email <addr>` | Submit orders, validating ownership by email |
| `run-turn --game ID [-v]` | Resolve turn, generate reports to processed folder |
| `turn-status [--game ID] [--turn N]` | Show who has submitted and what's been processed |
| `show-map --game ID` | Display system ASCII map |
| `show-status --ship ID` | Show ship status |
| `list-ships --game ID` | List all ships with owner and account info |
| `advance-turn --game ID` | Advance to next turn, reset TUs |
| `edit-credits --political ID --amount N` | Set player credits |

### join-game details

```bash
python pbem.py join-game [--game OMICRON101]
```

Interactive text form for new player registration. Prompts for name, email,
political character name, and ship name. The new ship starts docked at a
random starbase. Players can join at any point during the game.

On completion, the player receives their secret account number which they must
keep private. Their political ID and ship ID are public identifiers.

### submit-orders details

```bash
python pbem.py submit-orders <orders_file> --email <player_email> [--game OMICRON101] [--dry-run]
```

The `--email` flag identifies the submitting player. The engine validates that
the email is registered and that the player owns the ship specified in the
orders file. If validation fails, the orders are stored as rejected with an
explanation. When IMAP integration is added, the email will be extracted from
the message envelope automatically.

Use `--dry-run` to file the orders and write a receipt without storing them in
the database for resolution.

### turn-status details

```bash
python pbem.py turn-status [--game OMICRON101] [--turn 500.1]
```

Shows a dashboard of all players, their account numbers, which ships have
orders submitted, any rejections, and whether reports have been generated.
Useful for the GM to check everyone is in before running the turn.

## Report Sections

Ship turn reports include:
- **Between Turn Report** — passive events between turns
- **Turn Report** — order-by-order execution log with TU tracking
- **Command Report** — ship identity, affiliation, efficiency, TU remaining
- **Navigation Report** — current location, docked/orbiting status
- **Crew Report** — officers and crew complement
- **Cargo Report** — cargo hold contents
- **Space Combat Report** — combat status (placeholder for v1)
- **Installed Items** — ship modules and equipment
- **Contacts** — known objects in the system
- **Pending Orders** — orders that failed and carry forward

Political reports include financial summaries, ship fleet overview, and known contacts.

## Game Concepts

### Account Number
Every player receives a unique 8-digit account number when they join. This is
their secret out-of-game identifier used for order validation and report
delivery. It should never be shared with other players. If a future feature
requires sharing political IDs (diplomacy, faction membership, etc.), the
account number remains private.

### Political Position
Every player has one political position that:
- Owns all their ships and bases
- Tracks credits (in-game currency)
- Stores known contacts and intelligence
- Has rank, affiliation, and influence
- Has a public ID that other players can discover through scanning

### Credits
- Starting amount: 10,000
- Game owner can adjust via `edit-credits`
- Will be used for trading and base operations (future)

### Information Asymmetry
- Players only see what their ships scan
- LOCATIONSCAN reveals nearby objects
- SYSTEMSCAN produces a full system map
- Contact information persists between turns

## Future Roadmap

- [ ] Email ingest (IMAP) and send (SMTP)
- [ ] Inter-system jump travel
- [ ] Combat system (naval, ground, boarding)
- [ ] Trading between bases (buy/sell cargo)
- [ ] Base complex management and production
- [ ] Crew wages and morale
- [ ] Faction system with shared knowledge
- [ ] Planetary surface maps
- [ ] Web portal for turn upload/display
- [ ] Distance-based movement costs
- [ ] Standing orders
