"""Расчёт нормы калорий и БЖУ по формуле Миффлина-Сан Жеора."""

from __future__ import annotations

from config import ACTIVITY_FACTORS

# Дефицит для снижения веса. 20% — общепринятый безопасный темп (примерно 0.5-0.7 кг в неделю).
DEFICIT = 0.20

# Ниже этих значений не опускаемся даже при большом дефиците: слишком низкая
# калорийность бьёт по обмену веществ и почти всегда приводит к срывам.
MIN_KCAL = {"male": 1500, "female": 1200}

# Граммы на килограмм целевого веса.
PROTEIN_PER_KG = 1.8
FAT_PER_KG = 0.9

KCAL_PER_G = {"protein": 4, "fat": 9, "carb": 4}


def bmr(gender: str, weight_kg: float, height_cm: float, age: int) -> float:
    """Базовый обмен — сколько тело тратит в полном покое."""
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base + 5 if gender == "male" else base - 161


def calculate(
    gender: str,
    age: int,
    height_cm: float,
    weight_kg: float,
    target_weight_kg: float,
    activity: str,
) -> dict[str, int]:
    """Возвращает дневную норму калорий и БЖУ в граммах."""
    base = bmr(gender, weight_kg, height_cm, age)
    factor = ACTIVITY_FACTORS[activity][0]
    maintenance = base * factor

    target_kcal = maintenance * (1 - DEFICIT)

    # Не уходим ниже базового обмена и ниже абсолютного минимума по полу.
    target_kcal = max(target_kcal, base, MIN_KCAL[gender])
    kcal_norm = int(round(target_kcal / 10) * 10)

    # Белок и жир считаем от целевого веса, углеводы — остаток.
    protein_g = round(PROTEIN_PER_KG * target_weight_kg)
    fat_g = round(FAT_PER_KG * target_weight_kg)

    protein_kcal = protein_g * KCAL_PER_G["protein"]
    fat_kcal = fat_g * KCAL_PER_G["fat"]
    carb_kcal = kcal_norm - protein_kcal - fat_kcal

    # У низкой нормы белок с жиром могут перекрыть весь калораж — режем жир,
    # белок при похудении трогать нельзя, он держит мышцы.
    if carb_kcal < kcal_norm * 0.15:
        carb_kcal = int(kcal_norm * 0.15)
        fat_kcal = kcal_norm - protein_kcal - carb_kcal
        fat_g = max(round(fat_kcal / KCAL_PER_G["fat"]), round(0.5 * target_weight_kg))

    carb_g = max(round(carb_kcal / KCAL_PER_G["carb"]), 0)

    return {
        "maintenance_kcal": int(round(maintenance / 10) * 10),
        "kcal_norm": kcal_norm,
        "protein_g": int(protein_g),
        "fat_g": int(fat_g),
        "carb_g": int(carb_g),
    }


def weeks_to_goal(weight_kg: float, target_weight_kg: float, kcal_norm: int,
                  maintenance_kcal: int) -> int | None:
    """Грубая оценка срока: 7700 ккал дефицита ≈ 1 кг жира."""
    to_lose = weight_kg - target_weight_kg
    daily_deficit = maintenance_kcal - kcal_norm
    if to_lose <= 0 or daily_deficit <= 0:
        return None
    weeks = (to_lose * 7700) / (daily_deficit * 7)
    return max(1, int(round(weeks)))
