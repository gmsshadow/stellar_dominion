# Stellar Dominion — Game Master Guide

This guide covers everything a GM needs to set up, run, and moderate a Stellar Dominion game: the turn pipeline, moderator actions, player management, universe building, and a full worked example of running a turn from start to finish.

## Setting Up a Game

### Demo Game

The quickest way to start is with the built-in demo:

```bash
python pbem.py setup-game --demo
```

This creates game `OMICRON101` with two players (Alice and Bob), three star systems (Omicron, Tartarus, Meridian), three starbases, hyperspace links between all systems, and six factions. Each player gets a Light Trader MK I with 15 crew, a captain, and 10,000 credits.

### Adding Players

New players can join interactively at any time:

```bash
python pbem.py join-game --game OMICRON101
```

The system prompts for name, email, prefect name, and ship name, then lets the player choose a starting planet. A secret account number is generated — the player must note this down.

Players can also be added directly:

```bash
python pbem.py add-player --game OMICRON101 --name "Charlie" --email charlie@example.com
```

### Inspecting the Game

```bash
python pbem.py list-players --game OMICRON101        # All players with status
python pbem.py list-ships --game OMICRON101          # All ships with positions
python pbem.py show-map --game OMICRON101            # ASCII system map
python pbem.py show-status --game OMICRON101         # Ship/position details
python pbem.py list-universe                          # All systems, bodies, links
python pbem.py list-factions                          # Available factions
```

## The Turn Pipeline

Each turn flows through a state machine on the game record:

```
┌──────────┐    hold-turn    ┌──────────┐   release-turn   ┌──────────┐
│          │ ──────────────> │          │ ───────────────> │          │
│   OPEN   │                 │   HELD   │                   │   OPEN   │
│          │ <────────────── │          │                   │          │
└────┬─────┘   release-turn  └──────────┘                   └────┬─────┘
     │                                                           │
     │ run-turn                                                  │ run-turn
     ▼                                                           ▼
┌──────────────┐                                         ┌──────────────┐
│  PROCESSING  │                                         │  PROCESSING  │
└──────┬───────┘                                         └──────┬───────┘
       │ (automatic on completion)                              │
       ▼                                                        ▼
┌──────────────┐                                         ┌──────────────┐
│  COMPLETED   │                                         │  COMPLETED   │
└──────┬───────┘                                         └──────┬───────┘
       │ advance-turn                                           │
       ▼                                                        ▼
┌──────────┐                                             ┌──────────┐
│   OPEN   │  (next turn)                                │   OPEN   │
└──────────┘                                             └──────────┘
```

**Auto-hold on MODERATOR orders:** If any ship submits a `MODERATOR` order, `run-turn` automatically creates the action records, sets the turn to HELD, and stops before resolution. The GM must respond to all pending actions, then release and re-run the turn to continue. See [Moderator Actions](#moderator-actions) below.

### Pipeline States

| State | Orders Accepted? | run-turn Allowed? | Description |
|-------|:---:|:---:|-------------|
| **OPEN** | Yes | Yes | Normal state — players submit orders, GM can run the turn |
| **HELD** | No | No | GM has locked the turn for review (manual or auto-hold from MODERATOR orders) |
| **PROCESSING** | No | No | Turn is being resolved. If it crashes, use `release-turn` to recover |
| **COMPLETED** | No | No | Turn resolved. Use `advance-turn` to move to the next week |

### Pipeline Commands

```bash
python pbem.py turn-pipeline --game OMICRON101    # Dashboard: status, orders, ships
python pbem.py hold-turn --game OMICRON101        # Lock orders for review
python pbem.py release-turn --game OMICRON101     # Unlock (from held or processing)
python pbem.py run-turn --game OMICRON101         # Resolve the turn
python pbem.py run-turn --game OMICRON101 --force # Override held/completed status
python pbem.py advance-turn --game OMICRON101     # Move to next week, reset TUs
```

## Moderator Actions

The MODERATOR order lets players submit free-text requests to the GM as part of their turn orders. This is the primary mechanism for non-standard actions: ship upgrades, narrative events, special negotiations, rule clarifications, or anything that requires GM adjudication.

### How It Works

1. A player includes `MODERATOR Can I retrofit my ship?` in their orders
2. When `run-turn` is called, it scans all orders before resolution begins
3. If any MODERATOR orders are found, the engine creates action records and **auto-holds** the turn
4. The GM reviews the requests, makes any needed game-state changes, writes responses
5. The GM releases the turn and calls `run-turn` again
6. This time all actions are responded — the turn proceeds normally
7. During resolution, the MODERATOR order appears in the ship report with the GM's response

This design means the GM can modify the game state (edit a ship, adjust credits, add items) **before** the rest of the player's orders execute. A MODERATOR request at the top of an order list can affect the outcome of all subsequent orders.

### GM Workflow

```bash
# 1. Run turn — auto-holds if MODERATOR orders exist
$ python pbem.py run-turn --game OMICRON101

  *** TURN AUTO-HELD: 1 moderator action(s) require GM response ***
    #1: Li Chen/Boethius: "Can I retrofit my ship with better sensors?"

  Use 'list-actions --game OMICRON101' to review.

# 2. Review pending actions
$ python pbem.py list-actions --game OMICRON101

Moderator Actions (pending):
  #1: Alice/Li Chen via Boethius (77783574)
    Turn: 500.2  Status: PENDING
    Request: "Can I retrofit my ship with better sensors?"

# 3. (Optional) Make game-state changes before responding
$ python pbem.py edit-credits --game OMICRON101 --prefect-id 90583097 --credits 9500

# 4. Respond to the action
$ python pbem.py respond-action --game OMICRON101 --action-id 1 \
    --response "Approved! Advanced Sensors installed. 500 cr deducted."

  All moderator actions responded. Use 'release-turn' then 'run-turn' to continue.

# 5. Release and re-run
$ python pbem.py release-turn --game OMICRON101
$ python pbem.py run-turn --game OMICRON101
```

The player's ship report will show:

```
MODERATOR REQUEST: "Can I retrofit my ship with better sensors?"
  GM RESPONSE: "Approved! Advanced Sensors installed. 500 cr deducted."
```

### Action Status Flow

Each moderator action goes through these states:

- **pending** — Created when run-turn detects a MODERATOR order. Awaiting GM response.
- **responded** — GM has written a response. Turn can proceed.
- **resolved** — Turn has been processed and the response delivered in the ship report.

### CLI Commands

```bash
# List actions (default: pending only)
python pbem.py list-actions --game OMICRON101
python pbem.py list-actions --game OMICRON101 --status all        # Include resolved
python pbem.py list-actions --game OMICRON101 --status responded  # Only responded

# Respond to an action
python pbem.py respond-action --game OMICRON101 --action-id 1 \
    --response "Denied — insufficient funds for that upgrade."
```

### Multiple Actions Per Turn

If multiple ships submit MODERATOR orders, or one ship submits several, all are collected and the turn holds until every one has been responded to. The `respond-action` command shows how many are still pending after each response.

## Order Moderation

While the turn is held (or open), you can inspect and modify any player's orders.

### Reviewing Orders

```bash
python pbem.py review-orders --game OMICRON101
```

Output shows all pending orders grouped by ship, with order IDs in square brackets:

```
=== Order Review: Turn 500.2 ===

STA Boethius (17761429) - Alice/Li Chen
  [1] #1: LOCATIONSCAN
  [2] #2: MODERATOR {"text": "Can I retrofit my sensors?"}
  [3] #3: MOVE {"col": "F", "row": 10}

STA Resolute (67726162) - Bob/Erik Voss
  [4] #1: WAIT 50
  [5] #2: LOCATIONSCAN
```

### Editing Orders

Replace an order's command entirely using its order ID:

```bash
python pbem.py edit-order --game OMICRON101 --order-id 3 --command "MOVE M13"
```

### Deleting Orders

Remove an order:

```bash
python pbem.py delete-order --game OMICRON101 --order-id 4
```

### Injecting GM Orders

Add an order to any ship. It appends to the end of the ship's order queue by default:

```bash
python pbem.py inject-order --game OMICRON101 --ship 67726162 --command "SYSTEMSCAN"
python pbem.py inject-order --game OMICRON101 --ship 67726162 --command "MOVE K15" --sequence 1
```

Use `--sequence N` to insert at a specific position.

## Player Management

### Suspending Players

Suspended players' ships are skipped during turn resolution:

```bash
python pbem.py suspend-player --email alice@example.com --game OMICRON101
python pbem.py reinstate-player --account 87654321 --game OMICRON101
```

### Editing Credits

Set a prefect's credit balance directly:

```bash
python pbem.py edit-credits --game OMICRON101 --prefect-id 42981894 --credits 5000
```

## Faction Management

Players request faction changes via the `CHANGEFACTION` order. These are queued for GM approval.

```bash
# Check pending requests
python pbem.py faction-requests --game OMICRON101
python pbem.py faction-requests --game OMICRON101 --status all

# Approve with a note
python pbem.py approve-faction --game OMICRON101 --request-id 1 --note "Welcome to the guild!"

# Deny with a reason
python pbem.py deny-faction --game OMICRON101 --request-id 2 --note "Insufficient reputation."
```

Approved changes take effect at the start of the next `run-turn`. Denied requests are notified in the player's between-turn report. Both include any GM note.

### Available Factions

| ID | Abbrev | Name |
|----|--------|------|
| 0 | IND | Independent |
| 11 | STA | Stellar Training Academy |
| 12 | MTG | Merchant Trade Guild |
| 13 | IMP | Imperial Navy |
| 14 | FRN | Frontier Coalition |
| 15 | SYN | Syndicate |

New factions can be added directly to `universe.db` in the `factions` table.

## Universe Building

The universe is stored in `universe.db` and can be edited live — changes take effect next turn.

### Adding Systems and Bodies

```bash
# Add a new star system
python pbem.py add-system --name "Proxima" --spectral-type K1V

# Add a planet to that system
python pbem.py add-body --name "Haven" --system-id 102 --col K --row 8 \
    --body-type Planet --gravity 0.95 --temperature 285 \
    --atmosphere Standard --tectonic 3 --hydrosphere 55 --life Sentient

# Add a moon
python pbem.py add-body --name "Haven Minor" --system-id 102 --col K --row 8 \
    --body-type Moon --parent-id 201 --gravity 0.2

# Create a hyperspace link
python pbem.py add-link 101 102 --known

# Add surface infrastructure
python pbem.py add-port --body-id 201 --name "Haven Downport" --x 5 --y 3
python pbem.py add-outpost --body-id 201 --name "Mining Camp Alpha" --x 10 --y 7 --type mine
```

### Editing universe.db Directly

You can also open `game_data/universe.db` in any SQLite editor (e.g. DB Browser for SQLite) and modify tables directly. Key tables: `star_systems`, `celestial_bodies`, `system_links`, `factions`, `trade_goods`, `resources`.

## Turn Resolution Details

When `run-turn` executes, the following phases run in order:

1. **Phase 1: Order gathering** — Load pending orders for all active ships, including overflow from previous turns
2. **Phase 1.1: Moderator action check** — Scan all orders for MODERATOR commands. If found, create action records. If any are still pending (unresponded), auto-hold the turn and stop. If all are responded, continue.
3. **Phase 1.5: Wages** — Deduct crew wages (1 cr/crew + 5 cr/officer per week), calculate efficiency
4. **Phase 1.6: Faction changes** — Apply approved faction transfers, notify denied requests
5. **Phase 2: Interleaved resolution** — All ships' orders are placed in a priority queue sorted by TU cost. The cheapest action across all ships executes first, then the next cheapest, and so on. MODERATOR orders resolve here and pick up the GM's response from the database.
6. **Phase 3: Report generation** — Generate ship reports (ASCII + PDF), prefect reports, and file them in `turns/processed/{turn}/{account}/`
7. **Phase 4: Cleanup** — Mark orders as processed, save overflow orders, back up game state

### Overflow Orders

If a ship runs out of TU, its remaining orders are saved to the `pending_orders` table and execute before new orders next turn. Orders that fail for non-TU reasons (wrong location, missing cargo, etc.) are dropped.

## Email Integration

### Two-Stage Fetch and Process

```bash
python pbem.py fetch-mail --credentials creds.json --inbox ./inbox
python pbem.py process-inbox --inbox ./inbox --game OMICRON101
python pbem.py send-turns --credentials creds.json --game OMICRON101
```

### Manual Order Submission

```bash
python pbem.py submit-orders orders.yaml --email alice@example.com --game OMICRON101
```

## Worked Example: Running a Full Turn

Here's a complete walkthrough of a typical turn cycle, including a moderator action.

### 1. Check the Pipeline

```bash
$ python pbem.py turn-pipeline --game OMICRON101

=== Turn Pipeline: OMICRON101 ===
  Turn:   500.3
  Status: OPEN - accepting orders
  Orders: 0 pending, 0 overflow from previous turns

  Ships with orders:    0
  Ships without orders: 2
    STA Boethius (17761429) - Li Chen
    MTG Resolute (67726162) - Erik Voss
```

### 2. Receive and Submit Orders

Players email their orders, or you submit them manually:

```bash
$ python pbem.py submit-orders alice_orders.yaml --email alice@example.com
Orders filed: 4 orders for ship 17761429 (turn 500.3)

$ python pbem.py submit-orders bob_orders.yaml --email bob@example.com
Orders filed: 2 orders for ship 67726162 (turn 500.3)
```

Alice's orders include a MODERATOR request:
```yaml
orders:
  - MODERATOR: Can I buy a cargo hold expansion? Willing to pay 2000 cr.
  - LOCATIONSCAN
  - MOVE: H15
  - DOCK: 45687590
```

### 3. Run the Turn

```bash
$ python pbem.py run-turn --game OMICRON101

=== Resolving Turn 500.3 for game OMICRON101 ===

  STA Boethius (17761429): 4 new = 4 orders queued
  MTG Resolute (67726162): 2 new = 2 orders queued

  *** TURN AUTO-HELD: 1 moderator action(s) require GM response ***
    #1: Li Chen/Boethius: "Can I buy a cargo hold expansion? Willing to pay 2000 cr."

  Use 'list-actions --game OMICRON101' to review.
```

The turn has auto-held. No orders have been resolved yet.

### 4. Review and Respond to Moderator Actions

```bash
$ python pbem.py list-actions --game OMICRON101

Moderator Actions (pending):
  #1: Alice/Li Chen via Boethius (17761429)
    Turn: 500.3  Status: PENDING
    Request: "Can I buy a cargo hold expansion? Willing to pay 2000 cr."
```

You decide to approve. First, deduct credits and note the upgrade:

```bash
$ python pbem.py edit-credits --game OMICRON101 --prefect-id 56688690 --credits 8000

$ python pbem.py respond-action --game OMICRON101 --action-id 1 \
    --response "Approved! Cargo Hold MK II installed (+200 MU). 2000 cr deducted."

  All moderator actions responded. Use 'release-turn' then 'run-turn' to continue.
```

### 5. Optionally Review Other Orders

While the turn is held you can also inspect and modify orders:

```bash
$ python pbem.py review-orders --game OMICRON101

=== Order Review: Turn 500.3 ===

STA Boethius (17761429) - Alice/Li Chen
  [1] #1: MODERATOR {"text": "Can I buy a cargo hold expansion? ..."}
  [2] #2: LOCATIONSCAN
  [3] #3: MOVE {"col": "H", "row": 15}
  [4] #4: DOCK {"base_id": 45687590}

MTG Resolute (67726162) - Bob/Erik Voss
  [5] #1: UNDOCK
  [6] #2: MOVE {"col": "M", "row": 13}
```

Everything looks fine — release and run.

### 6. Release and Run

```bash
$ python pbem.py release-turn --game OMICRON101
Turn 500.3 released — status: OPEN (was HELD).

$ python pbem.py run-turn --game OMICRON101

=== Resolving Turn 500.3 for game OMICRON101 ===
  ...
  Resolving 2 ships interleaved by TU cost...
  ...

=== Turn 500.3 resolution complete ===
  State backed up to: game_state_500.3.db
```

### 7. Send Reports

```bash
$ python pbem.py send-turns --credentials creds.json --game OMICRON101
```

Alice's ship report will include:

```
MODERATOR REQUEST: "Can I buy a cargo hold expansion? Willing to pay 2000 cr."
  GM RESPONSE: "Approved! Cargo Hold MK II installed (+200 MU). 2000 cr deducted."
```

### 8. Handle Faction Requests (if any)

```bash
$ python pbem.py faction-requests --game OMICRON101
# Approve or deny as needed
```

### 9. Advance to Next Turn

```bash
$ python pbem.py advance-turn --game OMICRON101
Turn advanced: 500.3 -> 500.4
All ship TUs reset.
```

The pipeline resets to OPEN and the cycle begins again.

## Troubleshooting

### Turn stuck in PROCESSING

If `run-turn` crashes mid-way, the status stays at PROCESSING. Recovery:

```bash
python pbem.py release-turn --game OMICRON101   # Reset to OPEN
python pbem.py run-turn --game OMICRON101        # Try again
```

### Turn auto-held but I want to skip moderator actions

Use `--force` to override:

```bash
python pbem.py run-turn --game OMICRON101 --force
```

Unresponded MODERATOR orders will resolve with "(no response — request noted)" in the ship report.

### Player submitted wrong orders

Hold the turn, delete the bad orders, optionally inject corrected ones:

```bash
python pbem.py hold-turn --game OMICRON101
python pbem.py review-orders --game OMICRON101
python pbem.py delete-order --game OMICRON101 --order-id 7
python pbem.py inject-order --game OMICRON101 --ship 12345678 --command "MOVE K10"
python pbem.py release-turn --game OMICRON101
```

### Restoring from backup

After every `run-turn`, the game state is backed up to `game_data/saves/game_state_{turn}.db`. To restore:

```bash
cp game_data/saves/game_state_500.2.db game_data/game_state.db
```

### Player can't submit orders

Check: is the turn OPEN? (`turn-pipeline`), is the email correct? (`list-players`), is the player suspended? (`list-players --all`).
