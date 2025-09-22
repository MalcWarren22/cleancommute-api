# emissions.py
from __future__ import annotations

from typing import Literal, TypedDict, TypeAlias

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
_FACTORS: dict[Mode, float] = {
    "car": 0.192,  # average gasoline car
    "car_gas": 0.192,
    "car_hybrid": 0.120,
    "rideshare": 0.212,  # extra for detours/idle
    "bus": 0.082,
    "train": 0.041,
    "subway": 0.045,
    "bike": 0.0,
    "walk": 0.0,
}

_PER_VEHICLE: set[Mode] = {"car", "car_gas", "car_hybrid", "rideshare"}


def _factor_for(mode: str) -> float:
    """Return factor for a (possibly unrecognized) mode, defaulting to car."""
    m = (mode or "").lower()
    return _FACTORS.get(m, _FACTORS["car"])


def estimate_emissions(
    distance_km: float,
    mode: Mode | str,
    *,
    passengers: int = 1,
) -> EmissionEstimate:
    """
    Return a simple per-trip emissions estimate.

    Args:
        distance_km: Trip distance in kilometers (> 0).
        mode: A supported mode (case-insensitive); defaults to 'car' if unknown.
        passengers: For car-like modes, divide per-vehicle emissions by passengers (min 1).

    """
    if distance_km <= 0:
        raise ValueError("distance_km must be > 0")

    normalized_mode: Mode | str = (mode or "car").lower()
    if normalized_mode not in _FACTORS:
        normalized_mode = "car"

    f = _factor_for(normalized_mode)
    pax = int(passengers) if isinstance(passengers, int) else 1
    if pax < 1:
        pax = 1

    divisor = pax if normalized_mode in _PER_VEHICLE else 1
    kg = round((distance_km * f) / divisor, 4)

    return {
        "kgCO2e": kg,
        "factor_kg_per_km": f,
        "mode": normalized_mode,  # type: ignore[typeddict-item]
        "passengers": divisor,
        "source": "emissions.py",
    }
