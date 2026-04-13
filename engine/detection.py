"""
Stellar Dominion - Detection (passive scanning) helpers.

Probabilistic detection formula:
    raw_chance = (scanner_rating * target_profile) / (distance + 1)^2
    final_chance = clamp(raw_chance, 0, 100)   # percentage points

Distance is measured in grid cells (Chebyshev / king-move distance since the
grid is a flat square of cells). distance=0 means same cell.

Larger targets (higher sensor_profile) are easier to spot. Better scanners
(higher sensor_rating) detect more reliably. Distance has a quadratic falloff:
being one cell further away roughly halves the detection chance.

Example (scanner_rating=25):
    target profile 0.5, d=0 ->  12.5%
    target profile 3.0, d=1 ->  18.75%
    target profile 10,  d=2 ->  27.78%
    target profile 50,  d=3 -> 100%  (clamped)

Scan range: a ship or base with sensor_rating > 0 sweeps all cells within
PASSIVE_SCAN_RANGE of its position (Chebyshev distance), including its own
cell. With the default range 2, that's a 5x5 square (25 cells).
"""

import math
import random

PASSIVE_SCAN_RANGE = 2  # Chebyshev cells


def grid_distance(col_a, row_a, col_b, row_b):
    """Chebyshev distance between two grid squares (letter cols A-Y, int rows)."""
    if not col_a or not col_b:
        return 999
    dcol = abs(ord(col_a.upper()) - ord(col_b.upper()))
    drow = abs(int(row_a) - int(row_b))
    return max(dcol, drow)


def detection_chance(scanner_rating, target_profile, distance):
    """
    Return the probability (0-100) that scanner detects target at this range.
    Both inputs may be larger than 100; the result is clamped to [0, 100].
    """
    if scanner_rating <= 0 or target_profile <= 0:
        return 0.0
    raw = (scanner_rating * target_profile) / ((distance + 1) ** 2)
    if raw < 0:
        return 0.0
    if raw > 100:
        return 100.0
    return raw


def try_detect(scanner_rating, target_profile, distance, rng=None):
    """
    Roll detection once. Returns (detected: bool, chance: float).
    rng is an optional random.Random instance for reproducible tests.
    """
    chance = detection_chance(scanner_rating, target_profile, distance)
    if chance <= 0:
        return False, chance
    if chance >= 100:
        return True, chance
    r = rng.random() if rng else random.random()
    return (r * 100) < chance, chance
