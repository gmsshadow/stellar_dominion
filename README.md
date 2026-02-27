# Stellar Dominion -- PBEM Strategy Game Engine v1.3

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
- ReportLab (`pip install reportlab`) — optional, enables PDF report export

## Project Structure

```
stellar_dominion/
|-- pbem.py                          # Main CLI entry point
|-- gmail_fetch.py                   # Standalone Gmail fetch (for testing)
|-- db/
|   |-- database.py                  # Two-DB schema, connections & migration
|   +-- universe_admin.py            # Universe content management (add systems/bodies/links)
|-- engine/
|   |-- game_setup.py                # Game/player creation & join-game registration
|   |-- gmail.py                     # Gmail API integration (auth, fetch, labels)
|   |-- order_processor.py           # Shared order validation & filing logic
|   |-- registration.py              # Registration form parser (YAML & text)
|   |-- turn_folders.py              # Turn folder manager (incoming/processed)
|   |-- maps/
|   |   |-- system_map.py            # 25x25 ASCII grid renderer
|   |   +-- surface_gen.py           # Variable-size planet surface terrain generator
|   |-- orders/
|   |   +-- parser.py                # YAML & text order parser
|   |-- resolution/
|   |   +-- resolver.py              # Turn resolution engine (TU system)
|   +-- reports/
|       +-- report_gen.py            # Phoenix-style ASCII report generator
+-- game_data/                       # Created by setup-game
    |-- universe.db                  # World definition (GM-editable)
    |-- game_state.db                # Live game state (engine-managed)
    |-- saves/                       # Turn backups (auto after run-turn)
    +-- turns/
        |-- incoming/                # Player orders filed by email address
        +-- processed/               # Resolved reports filed by account number
```

## Database Architecture

The game uses a **two-database model**:

**universe.db** — World definition, GM-editable. Contains star systems, celestial bodies,
system links, factions, and trade goods catalogue. You can open this directly in DB Browser
for SQLite, add a new system, and it's live next turn.

**game_state.db** — Live game state, engine-managed. Contains players, ships, starbases,
market prices, orders, turn logs. Automatically backed up after each `run-turn` to
`saves/game_state_{turn}.db`.

The engine opens `game_state.db` and ATTACHes `universe.db`, so all queries work
through a single connection — table names are unique across both databases.

### Universe Management CLI

```bash
python pbem.py list-universe                              # Show all systems, bodies, links
python pbem.py add-system --name "Proxima" --spectral-type K1V
python pbem.py add-body --name "Haven" --system-id 102 --col K --row 8 \
    --gravity 0.95 --temperature 285 --atmosphere Standard \
    --tectonic 3 --hydrosphere 55 --life Sentient
python pbem.py add-link 101 102 --known                   # Hyperspace link between systems
```

New content gets a `created_turn` provenance stamp automatically.

### Legacy Migration

To split an old single-file `stellar_dominion.db` into the two-DB model:
```bash
python pbem.py split-db path/to/stellar_dominion.db
```
This creates `universe.db` + `game_state.db` in the same directory. The original file
is preserved. Schema migrations (v0→v3) are applied automatically before splitting.

## Player Identity

Each player has three types of identifier:

| Identifier | Visibility | Purpose |
|------------|-----------|---------|
| **Account Number** | Secret -- known only to the player and the GM | Used for order validation and report folder routing. Never appears in reports or scans. |
| **Prefect ID** | Public -- discoverable by other players via scanning | In-game identity for diplomacy, contacts, and ownership. |
| **Ship/Base IDs** | Public -- discoverable via scanning | Identify assets on the map. |

The account number is generated when a player joins the game and must be kept
secret. It is used alongside the player's email address to validate order
submissions. Prefect IDs and ship IDs are the public-facing identifiers that
other players encounter through the game's scanning and contact systems.

## Joining a Game

New players can join at any point during the game using the interactive
registration form:

```bash
python pbem.py join-game --game OMICRON101
```

The form prompts for:
- **Real name** -- the player's name (for GM reference)
- **Email address** -- must be unique, used for order submission and report delivery
- **Prefect character name** -- in-game identity (e.g. "Li Chen", "Warlord Zax")
- **Ship name** -- the player's starting vessel (e.g. "Boethius", "Vengeance")

The engine then:
1. Generates a unique account number, prefect ID, and ship ID
2. Lets the player choose a starting planet and places the new ship in orbit around it
3. Creates the prefect with 10,000 starting credits
4. Displays the account number with a reminder to keep it secret

Players can also be added directly by the GM using `add-player` with explicit
parameters.

## Turn Folder Structure

Orders and reports are managed through a structured folder layout that separates
incoming orders (keyed by email) from processed output (keyed by account number).

### Incoming -- `turns/incoming/{turn}/{email}/`

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
- Does that player's prefect own the ship?
- Are there any valid orders after parsing?

If validation fails, the orders are stored as `rejected_` with a `.reason` file.
Resubmitting valid orders for the same ship replaces the previous submission.

### Processed -- `turns/processed/{turn}/{account_number}/`

Resolved reports are filed under the player's account number -- a secret,
permanent identifier that never changes. The email address is looked up from
the database at send time, keeping the folder structure stable even if a
player updates their email.

```
processed/
  500.1/
    25384359/                       # Alice's account number (secret)
      ship_57131458.txt             # Ship turn report
      prefect_76106713.txt        # Prefect summary (finances, fleet, contacts)
    13475868/                       # Bob's account number (secret)
      ship_17579149.txt
      prefect_57142790.txt
```

Using account numbers rather than prefect IDs for the folder structure means
that even if a player shares their prefect ID with another player (through
diplomacy, contacts, or scanning), it doesn't expose where their turn reports
are stored on the file system.

When SMTP integration is added, the send step simply iterates each account
folder, looks up the email from the database, and sends everything in the folder.

## The Omicron System (101)

The demo game creates the **Omicron** star system -- a 25x25 grid:

| Object              | Type       | Location | Notes                              |
|---------------------|------------|----------|------------------------------------|
| Omicron Prime       | Star       | M13      | Central star                       |
| Orion (247985)      | Planet     | H04      | 0.9g, Standard atmosphere          |
| Tartarus (301442)   | Planet     | R08      | 1.2g, Dense atmosphere             |
| Leviathan (155230)  | Gas Giant  | E18      | 2.5g, Hydrogen                     |
| Callyx (88341)      | Moon       | F19      | Moon of Leviathan, 0.3g            |
| Meridian (412003)   | Planet     | T20      | 0.7g, Thin atmosphere              |
| Citadel Station     | Starbase   | H04      | Produces Comp Cores, demands Metals |
| Tartarus Depot      | Outpost    | R08      | Produces Metals, demands Food       |
| Meridian Waystation | Outpost    | T20      | Produces Food, demands Comp Cores   |

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
account: 35846634
ship: 12345678
orders:
  - WAIT: 50
  - MOVE: M13
  - LOCATIONSCAN: {}
  - MOVE: H04
  - ORBIT: 247985
  - DOCK: 45687590
  - GETMARKET: 45687590
  - BUY: "45687590 101 20"
  - SELL: "45687590 103 10"
  - UNDOCK
  - LAND: "247985 15 20"
  - SURFACESCAN
  - TAKEOFF
```

### Plain Text
```
GAME OMICRON101
ACCOUNT 35846634
SHIP 12345678
WAIT 50
MOVE M13
LOCATIONSCAN
MOVE H04
ORBIT 247985
DOCK 45687590
GETMARKET 45687590
BUY 45687590 101 20
SELL 45687590 103 10
UNDOCK
SURFACESCAN
LAND 247985 15 20
SURFACESCAN
TAKEOFF
```

## Supported Commands (v1.2)

### Movement & Scanning

| Command          | TU Cost | Description                      |
|------------------|---------|----------------------------------|
| `WAIT {n}`       | n       | Consume n TU doing nothing       |
| `MOVE {coord}`   | 2/sq    | Move to grid coordinate step-by-step (e.g. M13) |
| `LOCATIONSCAN`   | 20      | Scan nearby cells for objects    |
| `SYSTEMSCAN`     | 20      | Produce full system ASCII map    |
| `ORBIT {bodyId}` | 10      | Enter orbit of a celestial body  |
| `DOCK {baseId}`  | 30      | Dock at a starbase (must be at same location) |
| `UNDOCK`          | 10      | Leave docked starbase            |
| `LAND {bodyId} {x} {y}` | 20 | Land at surface coordinates (must be in orbit) |
| `TAKEOFF`        | 20      | Take off from surface, return to orbit |
| `SURFACESCAN`    | 20      | Produce terrain map (must be orbiting or landed) |

### Trading (must be docked)

| Command                          | TU Cost | Description                      |
|----------------------------------|---------|----------------------------------|
| `GETMARKET {baseId}`             | 0       | View buy/sell prices, stock, and demand. Works docked or in orbit. |
| `BUY {baseId} {itemId} {qty}`   | 0       | Buy items from the base market   |
| `SELL {baseId} {itemId} {qty}`   | 0       | Sell items to the base market    |

Trade item IDs: `100101` Tartarus Precious Metals, `100102` Orion Computer Cores, `100103` Meridian Food Supplies.

In YAML, trade parameters are passed as a quoted string:
```yaml
- BUY: "45687590 101 20"
- SELL: "45687590 103 10"
```

## Turn Resolution Rules

- Each ship starts each turn with a fixed TU allowance (default: 300)
- Orders are resolved **interleaved** across all ships using a priority queue
- The ship whose next action completes earliest goes first (Phoenix BSE-style)
- MOVE orders are broken into **per-square steps** (2 TU each), so ships can
  see each other mid-move -- a scan will detect ships at their current position,
  not just their starting or ending locations
- Ship positions are **committed to the database after every action**, making
  them visible to other ships' scans in real time during the turn
- If a ship has insufficient TU, the order is skipped and added to **pending**
- Failed orders (wrong location, etc.) are logged and added to pending
- Failed orders do **not** deduct TU
- Resolution is **deterministic** using a seeded RNG
- Movement costs are per-ship (currently 2 TU/square for all ships, but the
  engine supports variable speeds for future ship designs)

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
| `edit-credits --prefect ID --amount N` | Set player credits |
| `suspend-player --email/--account` | Suspend a player (hide all assets) |
| `reinstate-player --email/--account` | Reinstate a suspended player |
| `list-players [--all]` | List players (--all includes suspended) |
| `generate-form --game ID [--output dir]` | Generate blank registration form for new players |
| `register-player <form>` | Process a filled-in registration form |
| `process-inbox --inbox <dir>` | Process orders + registrations from inbox |
| `fetch-mail --credentials <json>` | Fetch from Gmail to staging inbox |
| `send-turns --credentials <json>` | Email turn reports to players via Gmail |
| **Universe Management** | |
| `list-universe` | Show all systems, bodies, links, goods, factions |
| `add-system --name <name>` | Add a star system to universe.db |
| `add-body --name <name> --system-id N --col X --row Y` | Add a planet/moon/gas giant to a system |
| `add-link <sys_a> <sys_b> [--known]` | Add a hyperspace link between two systems |
| `split-db <legacy.db>` | Split old single-file DB into universe.db + game_state.db |

### join-game details

```bash
python pbem.py join-game [--game OMICRON101]
```

Interactive text form for new player registration. Prompts for name, email,
prefect character name, and ship name. The player selects a starting planet
from a list, and the new ship begins in orbit around that planet. Players can
join at any point during the game.

On completion, the player receives their secret account number which they must
keep private. Their prefect ID and ship ID are public identifiers.

### Registration forms (remote players)

For players who can't run the CLI directly, the GM can use registration forms:

```bash
# 1. GM generates blank forms (lists available starting planets)
python pbem.py generate-form --game OMICRON101 --output forms/

# 2. GM sends the form to the new player (YAML or text format)
# 3. Player fills in their details and chosen starting planet, sends it back
# 4. GM processes the form
python pbem.py register-player forms/alice_registration.yaml
```

The form includes a list of available starting planets with their IDs, locations,
and types. Both YAML and plain text formats are supported, matching
the same dual-format approach used for orders.

On registration, the player's ship is created in orbit around their chosen planet,
a SYSTEMSCAN is automatically run, and welcome reports (ship + prefect) are
generated ready to send to the player.

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

### Player Suspension

```bash
python pbem.py suspend-player --email alice@example.com [--game OMICRON101]
python pbem.py suspend-player --account 12345678 [--game OMICRON101]
python pbem.py reinstate-player --email alice@example.com
```

Suspended players' ships and positions are preserved but invisible to other
players. They won't appear on maps, in scans, or in turn status. Suspended
players cannot submit orders. Use `list-ships --all` or `list-players --all`
to see suspended accounts. All assets are fully restored on reinstatement.

### Two-Stage Email Workflow

The recommended workflow separates fetching from processing:

**Stage 1 -- Fetch mail** (pull from Gmail, send "received" ack):

```bash
python pbem.py fetch-mail --credentials credentials.json --reply
```

This fetches all messages with the `sd-orders` Gmail label, saves the text
content to a staging inbox directory organised by sender email, optionally
sends a "received" acknowledgement, and relabels the Gmail messages.

```
inbox/                              (staging directory)
  alice@example.com/
    msg_18f3a2b.txt                 (orders)
  bob@example.com/
    msg_29d4c1e.txt                 (orders)
  charlie@example.com/
    msg_3ae5f0d.txt                 (registration form)
```

**Stage 2 -- Process inbox** (validate, file, create players):

```bash
python pbem.py process-inbox --inbox ./inbox
```

Reads every file in the staging directory and auto-detects whether it
contains **player orders** or a **registration form**:

- **Orders** -- validates ownership (email -> account -> ship), files into the
  turn folder structure, stores in the database, writes receipt/rejection
- **Registration** -- validates all fields, creates player/prefect/ship in
  orbit at chosen planet, generates welcome reports

Processed files are moved to `inbox/_processed/`. Use `--keep` to leave
them in place.

You can also place files manually into the inbox directory (e.g. orders
received by other means) and they will be processed the same way.

**Stage 3 -- Send turn reports** (after running the turn):

```bash
python pbem.py run-turn
python pbem.py send-turns --credentials credentials.json
```

Collects all report files from `processed/{turn}/{account}/` and emails
them to each player as attachments. Each player receives a single email
with all their reports (ship reports, prefect report) for the turn.

Use `--dry-run` to preview what would be sent without actually sending
(works without Gmail credentials). Use `--turn 500.1` to send reports
for a specific turn rather than the current one.

### Gmail Setup

Requires the Google API Python client:

```bash
pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
```

**One-time setup:**
1. Enable Gmail API in Google Cloud Console
2. Create OAuth credentials (Desktop App) and download `credentials.json`
3. First run opens a browser for consent and caches a `token.json`

**`fetch-mail` options:**
- `--inbox DIR` -- Staging directory (default: `./inbox`)
- `--orders-label NAME` -- Gmail label for incoming submissions (default: `sd-orders`)
- `--processed-label NAME` -- Label applied after fetching (default: `sd-processed`)
- `--reply` -- Send "received" acknowledgement (threads in player's inbox)
- `--query QUERY` -- Override the Gmail search query
- `--dry-run` -- Fetch and save but don't modify Gmail or send acks
- `--max-results N` -- Max messages per run (default: 25)

**`send-turns` options:**
- `--turn TURN` -- Send a specific turn (default: current turn)
- `--dry-run` -- Preview what would be sent (works without credentials)

The label-based approach gives exactly-once fetching: even if a message
is later marked unread, it won't be re-fetched unless the orders label
is reapplied.

**Note:** The Gmail integration uses `gmail.modify` and `gmail.send` scopes.
If you previously authenticated with only `modify`, delete `token.json` and
re-authenticate.

## Report Sections

Ship turn reports include:
- **Between Turn Report** -- passive events between turns
- **Turn Report** -- order-by-order execution log with TU tracking
  (includes GETMARKET output, BUY/SELL confirmations with prices and quantities)
- **Command Report** -- ship identity, faction, efficiency, TU remaining
- **Navigation Report** -- current location, docked/orbiting status, cargo capacity
- **Crew Report** -- officers and crew complement
- **Cargo Report** -- cargo hold contents with per-item MU breakdown
- **Space Combat Report** -- combat status (placeholder for v1)
- **Installed Items** -- ship modules and equipment
- **Contacts** -- known objects in the system
- **Pending Orders** -- orders that failed and carry forward

Prefect reports include:
- **Financial Report** -- per-ship income/expenses summary and current wealth
- **Ships** -- fleet overview with location and TU status
- **Known Contacts** -- all detected objects across your ships

All reports are generated as both `.txt` and `.pdf` (if reportlab is installed).
PDFs use A4 portrait with monospace font, and surface/system maps are rendered at a
smaller font size so they fit the page without wrapping. Both formats are emailed
to players by `send-turns`.

## Game Concepts

### Account Number
Every player receives a unique 8-digit account number when they join. This is
their secret out-of-game identifier used for order validation and report
delivery. It should never be shared with other players. If a future feature
requires sharing prefect IDs (diplomacy, faction membership, etc.), the
account number remains private.

### Prefect Position
Every player has one prefect that:
- Owns all their ships and bases
- Tracks credits (in-game currency)
- Stores known contacts and intelligence
- Has rank, faction, and influence
- Has a public ID that other players can discover through scanning

### Credits
- Starting amount: 10,000
- Game owner can adjust via `edit-credits`
- Used for buying and selling trade goods at base markets

### Information Asymmetry
- Players only see what their ships scan
- LOCATIONSCAN reveals nearby objects
- SYSTEMSCAN produces a full system map
- Contact information persists between turns

### Planet Surfaces
Ships can land on planets and moons using the LAND command (requires orbiting the
body first). Specify landing coordinates: `LAND 247985 15 20` lands at grid position
(15,20). Omitting coordinates defaults to (1,1). The turn report shows the terrain
type at your landing site. Ships cannot move while landed; use TAKEOFF to return to
orbit. Gas giants cannot be landed on. Each ship has a gravity rating (default
1.5g) which will be checked against the planet's gravity in a future update.

Use SURFACESCAN while orbiting or landed to produce an ASCII terrain map (size
varies by body type: planets 31×31, moons 15×15, gas giants 50×50, asteroids
11×11, or custom via `--surface-size`). When landed, the ship's position is
marked with X on the map. Terrain is procedurally generated from planetary
properties (temperature, atmosphere, tectonic activity, hydrosphere, life
level) and deterministic per body.

**21 Terrain Types:**
| Symbol | Terrain     | Symbol | Terrain     | Symbol | Terrain     | Symbol | Terrain     |
|--------|-------------|--------|-------------|--------|-------------|--------|-------------|
| `~`    | Shallows    | `S`    | Sea         | `#`    | Ice         | `:`    | Tundra      |
| `"`    | Grassland   | `.`    | Plains      | `T`    | Forest      | `&`    | Jungle      |
| `%`    | Swamp       | `;`    | Marsh       | `^`    | Hills       | `A`    | Mountains   |
| `_`    | Rock        | `,`    | Dust        | `o`    | Crater      | `!`    | Volcanic    |
| `=`    | Desert      | `+`    | Cultivated  | `?`    | Ruin        | `@`    | Urban       |
| `*`    | Gas         |        |             |        |             |        |             |

### Inter-System Travel

Ships can jump between linked star systems using the JUMP command:
`JUMP 102` jumps to system 102. Costs 60 TU. Requirements: ship must not be
docked, landed, or orbiting (leave orbit by MOVEing first), must be at least
10 squares from the primary star (Chebyshev distance from M13), and a
hyperspace link must exist between the current and target systems. The ship
arrives at the same grid coordinates in the destination system and can
continue issuing orders (MOVE, SYSTEMSCAN, etc.) with remaining TU.

## Trading Economy

Starbases have markets where players can buy and sell trade goods. The economy
is designed around circular trade routes where buying cheap at one base and
selling at another is profitable.

### Trade Goods

| ID     | Item                      | Base Price | MU/unit |
|--------|---------------------------|------------|---------|
| 100101 | Tartarus Precious Metals  | 20 cr      | 5       |
| 100102 | Orion Computer Cores      | 50 cr      | 2       |
| 100103 | Meridian Food Supplies    | 30 cr      | 3       |

Item IDs use a 6-digit `1xxxxx` range for planet-origin trade goods, keeping
lower ranges available for core items (ship components, equipment, etc.).

### Base Specialisation

Each base permanently produces one item (75% of average price), trades one at
average price, and demands one (150% of average price). This creates profitable
routes between any pair of bases:

| Base                | Produces (cheap)          | Average              | Demands (expensive)       |
|---------------------|---------------------------|----------------------|---------------------------|
| Citadel Station     | Orion Computer Cores      | Meridian Food        | Tartarus Precious Metals  |
| Tartarus Depot      | Tartarus Precious Metals  | Orion Comp Cores     | Meridian Food Supplies    |
| Meridian Waystation | Meridian Food Supplies    | Tartarus Metals      | Orion Computer Cores      |

### Price Fluctuation

Each market cycle, a weekly average is generated per item (base price ±5%).
The base's role modifier is then applied (0.75× / 1.0× / 1.5×), and a ±3%
buy/sell spread ensures you always lose money buying and reselling at the same
base. Prices are deterministic per cycle (seeded RNG).

### Market Cycles (4 weeks)

Markets refresh every 4 weeks. Prices, stock, and demand are generated at the
start of each cycle and **persist across turns** within the cycle. Stock
depletes as players buy, demand depletes as players sell. The GETMARKET command
shows a countdown: "3 weeks to market refresh" or "Market refreshes next week."

This means market intelligence stays useful for several turns -- if you scan
a market on week 1 of a cycle, those prices are still valid on week 3. But
the stock may have been bought out by other players.

### Stock & Demand Limits

Bases have finite quantities that vary by role (±15% fluctuation per cycle).
Specialist roles (produces/demands) hold 10× the quantity to make trade routes
more rewarding:

| Role     | Stock (units to sell) | Demand (units to buy) |
|----------|-----------------------|-----------------------|
| Produces | ~2040-2760            | ~51-69                |
| Average  | ~102-138              | ~102-138              |
| Demands  | ~51-69                | ~2040-2760            |

If you request more than available, the order is **capped** to what's available
(not rejected). The report tells you: "(only 56 in stock, requested 100)".
Once stock or demand hits zero, further trades of that type fail until the next
cycle.

### Cargo Capacity

Ships have a cargo hold measured in Mass Units (MU). The starting Light Trader
MK I has 500 MU capacity across 5 Cargo Hold modules. Each trade good has a
per-unit MU cost, so heavier goods (Tartarus Precious Metals at 5 MU) fill up
faster than lighter ones (Orion Computer Cores at 2 MU).

## Future Roadmap

### Completed

- [x] Email ingest (Gmail API) and send (Gmail API)
- [x] Trading between bases (buy/sell cargo with market cycles)
- [x] Interleaved turn resolution (Phoenix BSE-style priority queue)
- [x] Planetary landing (LAND/TAKEOFF with surface locations)
- [x] Planet surface terrain generation (21 terrain types, SURFACESCAN)
- [x] Two-database architecture (universe.db + game_state.db)
- [x] Universe expansion CLI (add-system, add-body, add-link)
- [x] Turn backups (auto snapshot after each run-turn)
- [x] Legacy DB migration (split-db command)
- [x] Financial reports (per-ship income/expenses in prefect reports)
- [x] PDF export (A4 monospace reports with map auto-sizing)
- [x] Configurable planet surface sizes (5-50 grid, per body type)
- [x] Trade quantity capping (BUY/SELL cap to available instead of failing)
- [x] Inter-system JUMP command (60 TU, star proximity check, link validation)
- [x] 6-digit trade item IDs with planet-of-origin naming

### Backlog (prioritised)

Ordered by game design dependencies and implementation complexity — quickest
and simplest changes first, architectural changes last.

**Tier 1 — Quick data/config changes** ✅ All complete

- [x] **Rename orbital outposts as Starbases.** All base_type labels now
      "Starbase". Docking capacity remains as-is; future updates will derive
      it from starbase composition rather than type label.
- [x] **Increase trade stock/demand x10 for specialists.** Produces role now
      holds ~2400 stock, demands role holds ~2400 demand (was ~240 each).
- [x] **Add system of origin to trade goods.** `origin_system_id` column on
      `trade_goods` in universe.db. Scope for future distance-based pricing.
- [x] **Jump distance config.** `JUMP_CONFIG` dict with `max_jump_range`
      (default 1), `min_star_distance` (10), `tu_per_hop` (60). BFS
      pathfinding ready for multi-hop jumps up to 4 systems.

**Tier 2 — New entity types (moderate code, DB schema additions)**

- [x] **Add planetary resources.** `resource_id` column on `celestial_bodies`
      referencing a trade good that can be gathered from that planet. Shown in
      SURFACESCAN planetary data and `list-universe`. CLI: `--resource-id`.
      No gathering mechanics yet — data model for future surface port mining.
- [ ] **Create Surface Port position.** A surface-based facility that links
      to an orbital Starbase. Generate one per planet during setup. In the
      future, a Surface Port must be built before an orbital Starbase can be
      constructed. Needs: new entity in DB, surface coordinates, link to
      parent starbase.
- [ ] **Create Outpost position.** A lighter surface base with fewer
      abilities than a Surface Port. Placeholder implementation for future
      proofing — define the DB schema and entity type now, full mechanics
      later.

**Tier 3 — New game mechanics (significant code, new order types)**

- [ ] **Crew as tradeable items with wages.** Create "Human Crew" as a
      buyable/sellable item at starbases. Crew wages cost 1 credit per crew
      member per turn, deducted automatically during turn processing. Ties
      into ship operating costs and economic pressure.
- [ ] **Inter-position messaging.** New MESSAGE order allowing ships and
      prefects to send free-text messages to other positions by target
      position number. Messages delivered in the recipient's next turn
      report.
- [ ] **Faction change system.** Allow players to request a faction change,
      moderated by the game owner. Needs design work on: request submission
      flow, GM approval/rejection CLI, faction transfer mechanics (does the
      ship change livery? do contacts update?).

**Tier 4 — Architecture changes (turn pipeline restructuring)**

- [ ] **Moderator Action orders.** A new order type where the player submits
      a free-text question/request to the game owner. Requires restructuring
      the turn pipeline: turns without Moderator Actions resolve and send
      normally, turns with them go into a holding queue. The GM reviews held
      turns, writes responses, then releases them for final processing and
      delivery. Needs: order parsing, hold/release states, GM review CLI,
      partial turn sending.

### Other future items

- [ ] Gravity check for landing (ship gravity rating vs planet gravity)
- [ ] Combat system (naval, ground, boarding)
- [ ] Base complex management and production
- [ ] Faction diplomacy and shared knowledge
- [ ] Web portal for turn upload/display
- [ ] Standing orders
