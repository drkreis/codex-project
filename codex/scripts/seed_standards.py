# -*- coding: utf-8 -*-
"""Создаёт 10 учебных техдокументов + golden JSON для замера точности.

Это оригинальные учебные фикстуры (не копии ГОСТ), в формате,
который парсер Codex умеет разбирать.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STD = ROOT / "data" / "standards"
GOLD = ROOT / "data" / "golden"

ITEMS = [
    {
        "document_id": "ТУ-42.11-001-2024",
        "title": "Технические условия на насосный агрегат NA-220",
        "product_name": "Насосный агрегат NA-220",
        "params": {
            "power_kw": "2.2 кВт",
            "voltage_v": "380 В",
            "mass_kg": "48 кг",
            "dimensions_mm": "620x340x410",
            "throughput": "12 м3/ч",
        },
        "constraints": "температура окружающей среды −20…+40 °C; относительная влажность ≤80%",
        "notes": "поставка с паспортом и протоколом испытаний",
    },
    {
        "document_id": "ТУ-42.11-002-2024",
        "title": "Технические условия на вентилятор ВР-80",
        "product_name": "Вентилятор радиальный ВР-80",
        "params": {
            "power_kw": "0.75 кВт",
            "voltage_v": "220 В",
            "mass_kg": "19 кг",
            "throughput": "2500 м3/ч",
            "temp_c": "−10…+45 °C",
        },
        "constraints": "устанавливать на виброопоры; не закрывать всас",
        "notes": "класс защиты IP54",
    },
    {
        "document_id": "ТУ-27.90-010-2023",
        "title": "Технические условия на преобразователь частоты ПЧ-15",
        "product_name": "Преобразователь частоты ПЧ-15",
        "params": {
            "power_kw": "15 кВт",
            "voltage_v": "380 В",
            "mass_kg": "11 кг",
            "dimensions_mm": "320x220x180",
            "service_life": "10 лет",
        },
        "constraints": "монтаж в шкафу с вентиляцией; температура 0…+40 °C",
        "notes": "встроенный ЭМС-фильтр",
    },
    {
        "document_id": "ТУ-28.29-021-2024",
        "title": "Технические условия на конвейер ленточный КЛ-12",
        "product_name": "Конвейер ленточный КЛ-12",
        "params": {
            "power_kw": "3.0 кВт",
            "throughput": "40 т/ч",
            "dimensions_mm": "12000x800x900",
            "mass_kg": "860 кг",
            "voltage_v": "380 В",
        },
        "constraints": "уклон не более 15°; температура −15…+40 °C",
        "notes": "лента антистатическая",
    },
    {
        "document_id": "ТУ-26.51-033-2023",
        "title": "Технические условия на датчик давления ДД-100",
        "product_name": "Датчик давления ДД-100",
        "params": {
            "voltage_v": "24 В",
            "mass_kg": "0.35 кг",
            "dimensions_mm": "80x40x40",
            "temp_c": "−40…+85 °C",
            "service_life": "8 лет",
        },
        "constraints": "среда: воздух, вода, нейтральные газы; давление до 1.0 МПа",
        "notes": "выход 4–20 мА",
    },
    {
        "document_id": "ТУ-27.11-044-2024",
        "title": "Технические условия на трансформатор ТС-25",
        "product_name": "Трансформатор сухой ТС-25",
        "params": {
            "power_kw": "25 кВА",
            "voltage_v": "380/220 В",
            "mass_kg": "145 кг",
            "dimensions_mm": "700x450x800",
            "service_life": "25 лет",
        },
        "constraints": "установка в сухих помещениях; влажность ≤70%",
        "notes": "класс изоляции F",
    },
    {
        "document_id": "ТУ-28.22-055-2024",
        "title": "Технические условия на кран-балку КБ-3.2",
        "product_name": "Кран-балка электрическая КБ-3.2",
        "params": {
            "power_kw": "4.5 кВт",
            "voltage_v": "380 В",
            "mass_kg": "1250 кг",
            "throughput": "грузоподъёмность 3.2 т",
            "service_life": "15 лет",
        },
        "constraints": "работа при температуре −20…+40 °C; режим А3",
        "notes": "пульт радиоуправления опционально",
    },
    {
        "document_id": "ТУ-27.12-066-2023",
        "title": "Технические условия на ИБП UPS-3K",
        "product_name": "Источник бесперебойного питания UPS-3K",
        "params": {
            "power_kw": "3.0 кВА",
            "voltage_v": "220 В",
            "mass_kg": "28 кг",
            "dimensions_mm": "440x450x130",
            "service_life": "5 лет",
        },
        "constraints": "температура 0…+40 °C; не устанавливать в сырых помещениях",
        "notes": "время автономии до 10 мин при полной нагрузке",
    },
    {
        "document_id": "ТУ-28.13-077-2024",
        "title": "Технические условия на компрессор ВК-7.5",
        "product_name": "Винтовой компрессор ВК-7.5",
        "params": {
            "power_kw": "7.5 кВт",
            "voltage_v": "380 В",
            "mass_kg": "210 кг",
            "throughput": "1.1 м3/мин",
            "temp_c": "+5…+40 °C",
        },
        "constraints": "давление нагнетания до 0.8 МПа; требуется маслоразделитель",
        "notes": "ресивер 270 л в комплекте",
    },
    {
        "document_id": "ТУ-26.20-088-2024",
        "title": "Технические условия на контроллер ПЛК-М1",
        "product_name": "Промышленный контроллер ПЛК-М1",
        "params": {
            "voltage_v": "24 В",
            "mass_kg": "0.9 кг",
            "dimensions_mm": "120x100x70",
            "temp_c": "−20…+60 °C",
            "service_life": "12 лет",
        },
        "constraints": "монтаж на DIN-рейку; экранированная линия связи",
        "notes": "поддержка Modbus TCP/RTU",
    },
]


def render_doc(item: dict) -> str:
    lines = [
        f"Обозначение: {item['document_id']}",
        f"Наименование: {item['title']}",
        f"Изделие: {item['product_name']}",
        "",
        "Основные характеристики:",
    ]
    # human-readable keys for parser aliases
    label_map = {
        "power_kw": "Мощность",
        "voltage_v": "Напряжение",
        "mass_kg": "Масса",
        "dimensions_mm": "Габариты",
        "throughput": "Производительность",
        "temp_c": "Рабочая температура",
        "service_life": "Срок службы",
    }
    for key, val in item["params"].items():
        lines.append(f"{label_map.get(key, key)}: {val}")
    lines += [
        "",
        f"Ограничения: {item['constraints']}",
        f"Примечание: {item['notes']}",
        "",
        "Документ подготовлен для учебного MVP Codex (оригинальная фикстура).",
    ]
    return "\n".join(lines) + "\n"


def golden_of(item: dict) -> dict:
    params_str = "; ".join(f"{k}={v}" for k, v in item["params"].items())
    return {
        "document_id": item["document_id"],
        "title": item["title"],
        "product_name": item["product_name"],
        "params": params_str,
        "constraints": item["constraints"],
        "notes": item["notes"],
    }


def main() -> None:
    STD.mkdir(parents=True, exist_ok=True)
    GOLD.mkdir(parents=True, exist_ok=True)
    for i, item in enumerate(ITEMS, 1):
        stem = f"std_{i:02d}"
        (STD / f"{stem}.txt").write_text(render_doc(item), encoding="utf-8")
        (GOLD / f"{stem}.json").write_text(
            json.dumps(golden_of(item), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(f"seeded {len(ITEMS)} standards -> {STD}")
    print(f"seeded {len(ITEMS)} golden -> {GOLD}")


if __name__ == "__main__":
    main()
