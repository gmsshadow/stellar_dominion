# Stellar Dominion — TODO

This is the working backlog for game development. Items are grouped by category, marked with a priority flag, and annotated with dependencies where relevant.

## How to read this file

**Priorities:**
- **P1** — Foundational: blocks other work or affects core gameplay
- **P2** — Significant feature: meaningful new system, wanted but not blocking
- **P3** — Polish / refinement: improves existing systems, quality of life
- **P4** — Speculative: ideas worth recording but not committed

**Status indicators:**
- `[ ]` — Not started
- `[~]` — In design / partial spec
- `[x]` — Complete (moves to Changelog)

**Conventions:**
- "Depends on X" means the item cannot ship until X is done
- "Related to X" means the items influence each other but neither blocks
- Star Date references (e.g. Y500.W18) mark when something landed if relevant

---

## In Progress

(none)

---

## Planned

### Economy & Production

#### Production / Economy System (P1)
*Major new system. Design conversation needed before implementation.*

Currently base modules have capacity stats (`mining_capacity`, `factory_capacity`, `market_income`) but they don't actually produce anything. Trade goods exist with prices but no production source. Workers exist but their employment doesn't drive output.

- [~] Design pass: how do mining yields scale with celestial body resource stats?
- [~] Design pass: how do orbital starbases produce (no body to mine)?
- [~] Design pass: which module types are affected by body yield (extractive only, or all)?
- [ ] Resource yield multipliers per celestial body — schema work
- [ ] Mining modules generate raw resources from the planet they're on
- [ ] Factory modules turn raw resources into refined goods
- [ ] Output rate scales with workers employed and command efficiency
- [ ] Resource flow / supply chains (raw → refined → consumed)
- [ ] Worker employment gates output — `employees_required` becomes meaningful
- [ ] Wage pressure / contentment effects on output
- [ ] Cargo flow between linked installations (auto-shipping vs player-driven)

*Depends on*: design conversation. Worker contentment piece is **related to** Crew Morale System.

#### Wage System Audit (P3)
Wages are deducted per-turn but the mechanism could be revisited once morale lands.

- [ ] Confirm current per-turn wage deduction works correctly with morale interactions
- [ ] Optional: variable wages (player can pay above/below market for morale effects)

*Related to*: Crew Morale System.

---

### Combat & Tactics

#### Combat Balance Pass (P2)
*Tuning, not new mechanics. Numbers from playtesting feedback.*

Test scenarios revealed PD looks too dominant — Tartarus Depot's PD intercepted ~100% of incoming missile salvos. Beam vs shield ratios may also need adjustment.

- [ ] PD effectiveness — reduce accuracy, fewer shots, or saturation cap?
- [ ] Beam damage vs shield thickness ratios
- [ ] Missile / torpedo damage values
- [ ] Surface base shield SP — currently weak (60 SP for 1 shield gen → 0 thickness)
- [ ] Document tuning rationale

*Depends on*: actual playtesting data.

#### Boarding Combat (P2)
*Two sub-systems with different combat models.*

##### Space Boarding (ship-on-ship, ship-on-starbase)
- [ ] Marines / boarding troops as a ship resource (new field)
- [ ] BOARD order: ship targets adjacent ship or docked starbase
- [ ] Range-0 grappling requirement (must be in same grid square)
- [ ] Boarding combat resolution — turn-by-turn marine vs marine
- [ ] Capture vs destroy outcome (transfer ownership or wipe)
- [ ] Defenders include base PD or ship internal defences

##### Ground Boarding (surface ports + outposts)
- [ ] Ground troops as a ship/base resource (extends existing `troops` field on surface_ports)
- [ ] LANDTROOPS order: deploy from ship to body surface
- [ ] Ground combat resolution — different model from space boarding
- [ ] Capture installation vs destroy modules

*Depends on*: marines/troops being a meaningful ship resource. Currently `troops` exists on surface_ports only — needs extension to ships and possibly starbases.

#### Tactical Retreat / FTL Escape (P3)
Players currently can't disengage from combat once engaged. A FLEE doctrine exists but doesn't physically escape.

- [ ] Disengage mechanic during combat round
- [ ] Engine efficiency check (damaged engines = harder to escape)
- [ ] Pursuing ships get parting shots

#### Damaged Component States (P3)
*Subtle but high-impact for verisimilitude.*

Currently ships have a single integrity pool. A damaged ship at 30% HP is otherwise fully functional.

- [ ] Engines damaged → reduced movement / higher OC cost
- [ ] Jump drive damaged → can't jump or higher chance of failure
- [ ] Sensors damaged → reduced detection range
- [ ] Life support damaged → crew morale loss
- [ ] Damage allocation: where does hull damage go?

*Depends on*: per-component HP tracking (substantial refactor). **Related to**: Repair Systems, Crew Morale.

#### Wreckage / Salvage (P4)
Destroyed ships and bases just disappear. Could leave salvageable wreckage.

- [ ] Wreckage entity — drops at destruction location
- [ ] SALVAGE order — recover credits / components from wreckage
- [ ] Wreckage decay over turns

#### Combat Doctrine Extensions (P4)
- [ ] Additional doctrines: ambush, cautious, berserk
- [ ] Per-target doctrine (treat X as defensive, Y as aggressive)

---

### Ship Systems

#### Crew Morale System (P1)
*Core mechanic. Multiple feedback loops.*

- [ ] Add `morale` field to ships (0-100, default 80)
- [ ] Per-turn morale decay when not docked at a base (e.g. -2/turn)
- [ ] Morale damage from combat hits (e.g. -5 per hull-damage event)
- [ ] Below 50% morale: efficiency penalty applied to all OC costs
- [ ] Below 25% morale: severe penalty + risk of mutiny / desertion (P2)
- [ ] Display morale in ship report
- [ ] GM GUI: edit morale field directly
- [ ] PLAYER_GUIDE documentation

*Required for*: Shoreleave Order. **Related to**: Production worker contentment (similar concept), Officer Roles.

#### Shoreleave Order (P1)
*Restores morale at a base.*

- [ ] SHORELEAVE order: ship must be docked, costs N OC over M turns
- [ ] Crew morale recovers by X per turn during shoreleave
- [ ] Crew unavailable for other orders during shoreleave
- [ ] Cost may scale with crew size or base type
- [ ] Surface ports vs starbases — different recovery rates?

*Depends on*: Crew Morale System.

#### Atmospheric Streamlining (P3)
*Boolean ship attribute, restricted to small ships.*

- [ ] New ship component: "Atmospheric Streamlining" (ID TBD in 100-169 range)
- [ ] Component installation gated to ship_size <= 100
- [ ] Component contributes a `streamlined` flag to ship
- [ ] LANDORBIT to an atmospheric body fails for unstreamlined ships
- [ ] TAKEOFF from atmospheric body fails for unstreamlined ships
- [ ] Vacuum bodies — anyone can land/take-off (no gate)
- [ ] Order error message clearly explains why
- [ ] PLAYER_GUIDE documentation

*Standalone item — minimal dependencies.*

#### Officer Roles (P3)
*Specialised crew members with skill effects.*

- [ ] Officer types: captain, gunner, navigator, engineer, doctor
- [ ] Each officer has a skill rating (0-100)
- [ ] Officer effects: navigator reduces OC costs, gunner improves accuracy, doctor slows morale loss, etc.
- [ ] Officers can be hired at starbases (extends existing crew system)
- [ ] Officer death in combat / disease / age

*Related to*: Crew Morale System. Could be combined into a "Crew & Officers" rework.

#### Cargo Specialisation (P4)
- [ ] Refrigerated cargo modules (perishables)
- [ ] Hazardous cargo handling
- [ ] Passenger berths

---

### Bases / Installations

#### Base Repair (P2)
*Design needed.*

For starbases (HP-pool model), repair is HP restoration. For surface bases (siege model), repair is rebuilding destroyed modules. Different mechanics.

- [~] Design: starbase HP repair — same model as ship repair, scaled up?
- [~] Design: surface base module reconstruction — new modules built from scratch?
- [ ] Implementation TBD

*Depends on*: design conversation.

#### Module BUILD / REMOVE Orders (P2)
*Currently modules can only be added at game setup.*

- [ ] BUILD-MODULE order: install a new module on a base
- [ ] REMOVE-MODULE order: uninstall a module (refund partial credits)
- [ ] Construction time and worker requirements
- [ ] Inventory consumption (mass for the module)

*Required for*: meaningful base evolution after game start. **Related to**: Production/Economy if modules require produced inputs.

#### Player-driven Construction (P3)
*Currently bases/ports/outposts only created by GM.*

- [ ] FOUND-OUTPOST order: deploy a new outpost on a body
- [ ] FOUND-PORT order: establish a new surface port
- [ ] FOUND-STARBASE order: build a new starbase in a system
- [ ] Resource and time requirements
- [ ] Limits: per-prefect, per-system, per-faction

*Depends on*: Module BUILD/REMOVE Orders, Production/Economy.

---

### Information / Knowledge

#### "Via Faction" Attribution in Reports (P3)
*Already deferred from Knowledge System Phase 4.*

When a ship report shows a known contact, it currently doesn't indicate which knowledge layer revealed it (public/personal/faction). Could append a `[via faction]` tag for faction-shared items.

- [ ] Extend `prefect_knows()` to return source layer (or write a `prefect_knowledge_source()` helper)
- [ ] Update ship report contact rendering to show source

*Standalone — small change.*

#### Knowledge Staleness Indicator (P3)
*Discussed during Knowledge System design.*

For per-turn-snapshot data, show how old the info is.

- [ ] Display "last seen Y500.W15" on contacts
- [ ] Stale threshold: warn if >N turns old

#### Information Markets (P4)
*Speculative.*
- [ ] Buy/sell intel as a tradeable commodity
- [ ] Information broker NPCs

#### Cloaking / Sensor Profile Reduction (P4)
- [ ] Reduce ship sensor profile via component
- [ ] Cloak component blocks detection at certain ranges

---

### Reports & UX

#### GM CLI for Knowledge Operations (P3)
*Already deferred from Knowledge System Phase 4.*

- [ ] `gm grant-knowledge --prefect X --type Y --id Z`
- [ ] `gm set-public --type Y --id Z --public true|false`
- [ ] `gm dump-faction-knowledge --faction X`
- [ ] `gm revoke-knowledge --prefect X --type Y --id Z`

#### `set-base-state` CLI (P3)
*Already deferred from starbase combat work.*

- [ ] `gm set-base-state --base X --integrity N --shields N --status Y`
- [ ] Useful for narrative damage application via script

#### Player-facing System Maps with Knowledge Gating (P3)
- [ ] Player can request a system map showing only known objects
- [ ] Unknown jump destinations shown as "?" or hidden
- [ ] Compatible with knowledge system's union-at-query-time approach

#### Combat Replay / Cinematic Output (P3)
- [ ] Round-by-round narrative log in turn report
- [ ] More readable than current event-list format

#### Faction News Bulletin (P4)
- [ ] Global events (system founded, base destroyed, etc.) reported across factions
- [ ] Visibility based on knowledge layers

---

### Quality of Life

#### Order Templates / Macros (P3)
- [ ] Save common order sequences in a player profile
- [ ] Reuse across turns

#### Auto-pilot / Multi-turn Routes (P3)
- [ ] Set destination, ship navigates over multiple turns automatically
- [ ] Honours fuel, navigation, and OC budget

#### Web GUI for Players (P4)
*Currently CLI / email only.*

---

## Wishlist / Speculative

These are ideas that don't have committed roadmap slots but worth recording so we don't forget them.

- **Faction war / peace state**: distinguish "we're at war" from "I just hate this prefect"
- **Diplomatic actions**: formal alliances, treaties, embargoes
- **Bounty / contract system**: faction-issued missions
- **Dynamic NPC fleets**: pirates, traders, rivals beyond the GM-controlled ones
- **Hyperlane piracy / lane control**: protected vs unprotected jump routes
- **Research / tech tree**: faction-level upgrades
- **Black market / smuggling**: alternative trade with consequences
- **Prison / hostage mechanics**: captured prefects
- **Star system claiming / sovereignty**: territorial control
- **Resource depletion**: bodies' yields decline with extraction

---

## Deferred from Earlier Work

Specific items we've explicitly noted during prior work as "do later":

- [ ] **Ship REPAIR — Repair Bay employees gate throughput** — Currently `repair_capacity` is summed without checking employees. Revisit when Production/Economy lands and worker employment becomes meaningful.
- [ ] **Combat balance numbers** — flagged in starbase combat phases (PD too strong)
- [ ] **Outpost combat loadouts in beta DB** — currently only Orion Landing has a loadout
- [ ] **GM CLI knowledge commands** — see Reports & UX section above
- [ ] **`set-base-state` CLI** — see Reports & UX section above
- [ ] **Knowledge staleness display** — discussed during Knowledge System design
- [ ] **Faction knowledge attribution in reports** — see Information / Knowledge section above
- [ ] **Surface port / outpost combat reports per turn** — currently combat events appear in engagement logs, no dedicated "your port was attacked" summary

---

## Changelog

Completed work, most recent first. Star Date markers correspond to game-state turn at the time of work.

### 2026 — Y500.W18 era

#### Ship REPAIR Order
- New REPAIR order: ship must be docked at a base with Repair Bay (#540) or Shipyard (#541)
- Cost formula: ⌈HP × 0.5⌉ OC from ship pool + HP × 5 credits from prefect pool
- Per-turn order with optional amount: `REPAIR` (max possible) or `REPAIR <amount>` (capped at amount)
- Throughput capped by base `repair_capacity` (Repair Bay = 5/turn/unit, Shipyard = 15/turn/unit)
- FCFS shared pool: multiple ships at the same base draw from one pool; order processing order determines priority
- Combat blocks repair: refuses if ship OR base is in active combat
- Cap notes shown in result message ("requested 50, base capacity exhausted, ship OC exhausted, etc.")
- Edge cases handled: ship at full HP, not docked, no repair facility, insufficient credits, insufficient OC
- Outstanding: Repair Bay employees should gate throughput (deferred — depends on Production/Economy)
- PLAYER_GUIDE Maintenance section
- Bug fix: LOADMAGAZINE/UNLOADMAGAZINE dispatch was broken (referenced nonexistent handlers); now correctly routes to `_cmd_magazine_transfer`. Also added LOAD / UNLOAD aliases.

#### Siege Combat for Surface Installations (Phases 1-3)
- Per-module HP tracking on surface ports and outposts
- Random damage allocation weighted by quantity
- Pooled shield SP from surviving Shield Generators
- Pooled armour from surviving Life Domes (now provides armour_value=3)
- New modules: Surface Missile Silo (#585), Surface Missile Magazine (#586)
- Armour Plating restricted to starbases only
- Atmosphere blocks beam weapons in BOTH directions (vacuum-only beam combat for atmospheric bodies)
- Surface bases can launch missiles via silos through atmosphere
- Module destruction: quantity decrements, row deleted at zero
- Defend-response propagation extended to surface bases
- Surface base reports with per-module HP and combat summary
- GM GUI Combat State extended to handle surface bases (status, shields, missiles, repair)
- PLAYER_GUIDE documentation

#### Knowledge System (Phases 1-4)
- Three-layer knowledge model: public / prefect / faction
- `is_public` flags on systems, bodies, starbases, surface ports, outposts, trade goods
- `prefect_knowledge` table with `discovered_turn_*` and `surface_scanned` fields
- `faction_knowledge` table with contributor attribution
- One-time backfill migration with `knowledge_backfill_v1` stamp
- JUMP gating: destination must be known
- DOCK gating: starbase must be known (auto-grants on physical presence)
- SCANSURFACE flips `surface_scanned` flag on body knowledge row
- SCANSYSTEM grants knowledge of detected bases
- GETMARKET knowledge gating + grant on physical presence
- New SURVEY order: 5 OC, reveals neighbouring system existences
- New SHARE order (prefect-scoped): SHARE \<type\> \<id\> [FACTION | PREFECT \<id\>]
- Helpers: `prefect_knows`, `grant_knowledge`, `grant_system_knowledge`, `prefect_knowledge_set`, `grant_faction_knowledge`, `faction_knowledge_set`, `get_faction_knowledge_attribution`, `is_object_public`
- Faction layer checked at query time — leaving a faction immediately revokes access
- GM GUI Knowledge tab: prefect picker, personal + faction trees, GM grant/revoke, public flag toggles
- PLAYER_GUIDE Knowledge System section

#### Starbase Combat (Phases 1-4)
- Starbase HP / shield SP / armour as combat stats
- New modules: Defence Turret (#580), Shield Generator (#581), Armour Plating (#582), Base Point Defence (#583)
- Starbase weapons fire on TARGET-list ships in range 2
- PD intercepts incoming missiles and torpedoes
- Damage pipeline: shields → armour → integrity
- Starbase destruction cascades to destroy all docked ships
- Status filtering across economy, scan, dock, combat (destroyed bases excluded)
- Base report Space Combat Summary section
- Ship report `[DESTROYED]` flag on known destroyed starbases
- GM GUI Base Editor Combat State box
- PLAYER_GUIDE Starbase Combat section

#### Ship Missile / Torpedo / PD System
- Magazine-based ammo storage on ships
- New ship components for missile launchers, torpedo launchers, point defence, magazines
- BUY MAGAZINE flag for purchasing into a magazine
- LOAD / UNLOAD orders for magazine ↔ cargo transfers
- Projectile launch with flight-round delay before impact
- PD intercepts (torpedoes prioritised first)
- Combat report shows ammunition status and PD effectiveness

### Earlier work (existing in codebase before recent sessions)

- Module-driven base architecture (starbases, surface ports, outposts)
- Ship component system (engines, cargo, sensors, jump drives)
- Turn-based order resolution with OC system
- YAML and text order parsing
- Email integration (Gmail fetch + send reports)
- ASCII + PDF reports (Phoenix-BSE style)
- Two-database architecture (universe / game state)
- Trading economy with market cycles
- Crew hire / wage / efficiency system
- Six-faction system with GM-moderated transfers
- GM NPC system (unlimited credits, multiple prefects, local order/report I/O)
- Moderator action system (auto-hold turns for GM review)
- Universe expansion CLI (add systems, bodies, links, etc.)
- GM GUI: Turn Wizard, Turn Ops, Players, Universe, Bases, Moderator, Combat, Previews, DB Browser, Ship Editor, Base Editor, Settings tabs
