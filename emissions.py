# emissions.py
from __future__ import annotations
from typing import Literal, TypedDict, TypeAlias, Dict, Set

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
    kgCO2e: float
    factor_kg_per_km: float
    mode: Mode
    passengers: int
    source: Literal["emissions.py"]

# Very rough per-passenger emission factors (kg CO2e per km)
_FACTORS: Dict[Mode, float] = {
    "car": 0.192,        # average gasoline car
    "car_gas": 0.192,
    "car_hybrid": 0.120,
    "rideshare": 0.212,  # simple surcharge for deadheading/idling
    "bus": 0.082,
    "train": 0.041,
    "subway": 0.045,
    "bike": 0.0,
    "walk": 0.0,
}

# These are per-vehicle and divided by passengers
_PER_VEHICLE: Set[Mode] = {"car", "car_gas", "car_hybrid", "rideshare"}

def _factor_for(mode: str) -> float:
    m = (mode or "").lower()
    return _FACTORS.get(m, _FACTORS["car"])

def estimate_emissions(
    distance_km: float,
    mode: Mode | str,
    *,
    passengers: int = 1,
) -> EmissionEstimate:
    """
    Convert a single trip's distance (km) to CO₂e using coarse factors.

    - For per-vehicle modes (car-like), divide by passengers (min=1).
    - For transit, factors are per passenger already.
    """
    if distance_km <= 0:
        raise ValueError("distance_km must be > 0")

    normalized: Mode | str = (mode or "car").lower()
    if normalized not in _FACTORS:
        normalized = "car"

    f = _factor_for(normalized)
    pax = int(passengers) if isinstance(passengers, int) else 1
    if pax < 1:
        pax = 1

    divisor = pax if normalized in _PER_VEHICLE else 1
    kg = round((distance_km * f) / divisor, 4)

    return {
        "kgCO2e": kg,
        "factor_kg_per_km": f,
        "mode": normalized,  # type: ignore[typeddict-item]
        "passengers": divisor,
        "source": "emissions.py",
    }
