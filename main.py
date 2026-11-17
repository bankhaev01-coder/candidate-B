"""Точка входа.

Запуск без параметров: ``python main.py`` - данные из ``data/``, отчётная дата
10.11.2026, отчёты в текущем каталоге.

С параметрами::

    python main.py --data-dir data --as-of 10.11.2026
    python main.py --data-dir data --as-of 2026-11-10 --out-dir out
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

import reporter
from calculator import Calculator
from loader import DataError, load_dataset, parse_cli_date

DEFAULT_AS_OF = date(2026, 11, 10)
DEFAULT_DATA_DIR = "data"
DEFAULT_OUT_DIR = "."


def _as_of_argument(value: str) -> date:
    try:
        return parse_cli_date(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Расчёт фактически выплаченных сумм и остатков обязательств "
            "по договорам на отчётную дату."
        )
    )
    parser.add_argument(
        "--data-dir",
        default=DEFAULT_DATA_DIR,
        help="каталог с contracts.csv, works.csv, payments.csv (по умолчанию: data)",
    )
    parser.add_argument(
        "--as-of",
        type=_as_of_argument,
        default=DEFAULT_AS_OF,
        help=(
            "отчётная дата включительно в формате DD.MM.YYYY или YYYY-MM-DD "
            "(по умолчанию: 10.11.2026)"
        ),
    )
    parser.add_argument(
        "--out-dir",
        default=DEFAULT_OUT_DIR,
        help="каталог для файлов отчёта (по умолчанию: текущий)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        dataset = load_dataset(args.data_dir)
    except DataError as error:
        print(f"Ошибка данных, отчёт не построен:\n{error.full_text}", file=sys.stderr)
        return 1

    try:
        report = Calculator(dataset, args.as_of).run()
        paths = reporter.write_all(report, args.out_dir)
    except OSError as error:
        print(f"Не удалось записать отчёты: {error}", file=sys.stderr)
        return 1
    except RuntimeError as error:
        print(f"Внутренняя ошибка расчёта: {error}", file=sys.stderr)
        return 1

    reporter.print_summary(report, paths)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())