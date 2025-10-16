# emissions.py
from __future__ import annotations
from typing import Literal, TypedDict, TypeAlias

# ------------------------------------------------------------
# Type Definitions
# ------------------------------------------------------------

# Allowed commute modes
Mode: TypeAlias = Literal[
    "car",
    "car_gas",
    "car_hybrid",
    "rideshare",
    "bus",
    "train",
    "subway",
    "bike",
    "walk",
]

class EmissionEstimate(TypedDict):
    """CO₂e emission estimate for a single trip mode."""
    kgCO2e: float
    factor_kg_per_km: float
    mode: Mode
    passengers: int
    source: Literal["emissions.py"]

# ------------------------------------------------------------
# Base Factors (kg CO₂e per km per passenger or per vehicle)
# ------------------------------------------------------------
_FACTORS: dict[Mode, float] = {
    "car": 0.192,        # average gasoline vehicle
    "car_gas": 0.192,
    "car_hybrid": 0.120,
    "rideshare": 0.212,  # added for detours / idle
    "bus": 0.082,
    "train": 0.041,
    "subway": 0.045,
    "bike": 0.0,
    "walk": 0.0,
}

# Modes that emit per vehicle instead of per passenger
_PER_VEHICLE: set[Mode] = {"car", "car_gas", "car_hybrid", "rideshare"}

# ------------------------------------------------------------
# Internal Utilities
# ------------------------------------------------------------
def _factor_for(mode: str) -> float:
    """Return emission factor for a mode, defaulting to 'car' if unknown."""
    m = (mode or "").lower()
    return _FACTORS.get(m, _FACTORS["car"])

# ------------------------------------------------------------
# Core Estimator
# ------------------------------------------------------------
def estimate_emissions(
    mode: Mode | str,
    distance_km: float,
    *,
    passengers: int = 1,
) -> dict:
    """
    Return a per-trip emissions estimate.

    Args:
        mode: Commute mode (case-insensitive)
        distance_km: Trip distance (km)
        passengers: Passenger count (defaults to 1)
    """
    if distance_km <= 0:
        raise ValueError("distance_km must be > 0")

    normalized_mode = (mode or "car").lower()
    if normalized_mode not in _FACTORS:
        normalized_mode = "car"

    f = _factor_for(normalized_mode)
    pax = int(passengers) if isinstance(passengers, int) else 1
    if pax < 1:
        pax = 1

    divisor = pax if normalized_mode in _PER_VEHICLE else 1
    kg = round((distance_km * f) / divisor, 4)

    return {
        "mode": normalized_mode,
        "distance_km": round(distance_km, 2),
        "passengers": divisor,
        "factor_kg_per_km": f,
        "kgCO2e": kg,
        "estimated_co2_kg": kg,  # extra field for compare endpoints
        "source": "emissions.py",
    }

# ------------------------------------------------------------
# Comparison Helper
# ------------------------------------------------------------
def compare_modes(distance_km: float, passengers: int = 1):
    """
    Compare emissions for all modes at the same distance.
    Returns list sorted by lowest CO₂e.
    """
    results = []
    for mode in _FACTORS.keys():
        res = estimate_emissions(mode, distance_km, passengers=passengers)
        results.append(res)
    results.sort(key=lambda x: x["kgCO2e"])
    return results
