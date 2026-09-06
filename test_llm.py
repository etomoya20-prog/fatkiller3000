"""Проверка выбора итоговой цифры в FoodReport._resolve(): python test_llm.py

Без фреймворка и без сети: _resolve() — чистая функция над уже прочитанными
числами, и именно в ней жила ошибка, из-за которой съеденное 2018 записалось
как 2300. Случаи ниже — реальные скриншоты, на которых логика ломалась.
"""
from llm import FoodReport


def report(**kw):
    base = dict(
        understood=True, is_full_day=True,
        kcal_consumed=None, kcal_goal=None, kcal_remaining=None,
        protein_g=None, fat_g=None, carb_g=None,
        macros_are_remaining=False, estimated_from_photo=False, comment="",
    )
    base.update(kw)
    return FoodReport(**base)


CASES = [
    ("случай из жалобы: съедено 2018 против вычитания 2300, БЖУ за 2018",
     dict(kcal_consumed=2018, kcal_goal=2755, kcal_remaining=455,
          protein_g=90, fat_g=104, carb_g=186), 2018),

    ("вчерашний скрин Anna: съедено и вычитание сходятся",
     dict(kcal_consumed=1519, kcal_goal=1600, kcal_remaining=81,
          protein_g=94.62, fat_g=63.13, carb_g=138.46), 1519),

    ("модель продублировала остаток в «съедено», БЖУ нет",
     dict(kcal_consumed=620, kcal_goal=2300, kcal_remaining=620), 1680),

    ("только цель и остаток — старое поведение",
     dict(kcal_goal=1951, kcal_remaining=444), 1507),

    ("расходятся, БЖУ за вычитание",
     dict(kcal_consumed=620, kcal_goal=2300, kcal_remaining=700,
          protein_g=90, fat_g=70, carb_g=170), 1600),

    ("расходятся, рассудить нечем — берём подписанное",
     dict(kcal_consumed=1800, kcal_goal=2500, kcal_remaining=300), 1800),

    ("только съеденное, БЖУ резко против — побеждают БЖУ",
     dict(kcal_consumed=620, protein_g=90, fat_g=70, carb_g=170), 1670),

    ("калорий не видно, есть только БЖУ",
     dict(protein_g=100, fat_g=50, carb_g=200), 1650),
]

failed = 0
for title, fields, expected in CASES:
    r = report(**fields)
    ok = r.kcal == expected
    failed += not ok
    print(f"{'OK ' if ok else 'FAIL'} {title}: {r.kcal} (ждали {expected})")
    if r.note:
        print(f"     note: {r.note}")

print("\nпровалено:", failed)
raise SystemExit(1 if failed else 0)
