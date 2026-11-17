"""Чтение и валидация исходных CSV.

Модуль только разбирает файлы и проверяет данные: расчёт живёт в
``calculator.py``, вывод - в ``reporter.py``. Проблемы данных собираются с
указанием файла, строки и поля.

Фатальные проблемы поднимаются как :class:`DataError` (отчёт построить нельзя):
нет файла или обязательной колонки, не разбирается дата или сумма, дубль
идентификатора, ссылка на несуществующий договор/работу, работа из чужого
договора, нарушенная хронология событий, отсутствие даты, которое делает
историческое состояние неопределимым.

Мягкие расхождения (статус не совпадает с датами, платёж раньше подписания
договора и т.п.) попадают в :attr:`Dataset.issues` как ``warning`` и не мешают
расчёту: историческое состояние восстанавливается по датам событий.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable, Mapping, Sequence

CONTRACTS_FILE = "contracts.csv"
WORKS_FILE = "works.csv"
PAYMENTS_FILE = "payments.csv"

DATA_FILES = (CONTRACTS_FILE, WORKS_FILE, PAYMENTS_FILE)

MONEY_CENT = Decimal("0.01")
DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y")

PAYMENT_STATUSES = ("paid", "queued", "refunded")
WORK_STATUSES = ("planned", "done", "cancelled")

REQUIRED_COLUMNS: Mapping[str, Sequence[str]] = {
    CONTRACTS_FILE: ("contract_id", "contractor", "amount", "signed_at"),
    WORKS_FILE: (
        "work_id",
        "contract_id",
        "title",
        "price",
        "status",
        "completed_at",
        "cancelled_at",
    ),
    PAYMENTS_FILE: (
        "payment_id",
        "contract_id",
        "work_id",
        "purpose",
        "amount",
        "status",
        "created_at",
        "paid_at",
        "refunded_at",
    ),
}

WARNING = "warning"


class DataError(Exception):
    """Фатальная проблема входных данных: отчёт построить нельзя."""

    def __init__(self, message: str, problems: Iterable[str] = ()) -> None:
        self.message = message
        self.problems = list(problems)
        super().__init__(self.full_text)

    @property
    def full_text(self) -> str:
        lines = [self.message]
        lines.extend(f"  - {problem}" for problem in self.problems)
        return "\n".join(lines)


@dataclass(frozen=True)
class Issue:
    """Замечание к данным, которое не мешает расчёту."""

    level: str
    source: str
    line: int | None
    field: str | None
    message: str

    @property
    def location(self) -> str:
        parts: list[str] = [self.source]
        if self.line is not None:
            parts.append(f"строка {self.line}")
        if self.field:
            parts.append(f"поле {self.field}")
        return ", ".join(parts)

    def as_text(self) -> str:
        return f"[{self.level}] {self.location}: {self.message}"


@dataclass(frozen=True)
class Contract:
    contract_id: str
    contractor: str
    amount: Decimal
    signed_at: date
    line_no: int


@dataclass(frozen=True)
class Work:
    work_id: str
    contract_id: str
    title: str
    price: Decimal
    status: str
    completed_at: date | None
    cancelled_at: date | None
    line_no: int


@dataclass(frozen=True)
class Payment:
    payment_id: str
    contract_id: str
    work_id: str | None
    purpose: str
    amount: Decimal
    status: str
    created_at: date
    paid_at: date | None
    refunded_at: date | None
    line_no: int


@dataclass
class Dataset:
    """Разобранные и проверенные данные выгрузки."""

    contracts: dict[str, Contract] = field(default_factory=dict)
    works: dict[str, Work] = field(default_factory=dict)
    payments: dict[str, Payment] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    data_dir: Path | None = None

    def works_of(self, contract_id: str) -> list[Work]:
        return [work for work in self.works.values() if work.contract_id == contract_id]

    def payments_of(self, contract_id: str) -> list[Payment]:
        return [
            payment
            for payment in self.payments.values()
            if payment.contract_id == contract_id
        ]


# --------------------------------------------------------------------------
# Разбор отдельных значений
# --------------------------------------------------------------------------


def parse_date(
    raw: str | None,
    *,
    source: str,
    line: int | None,
    field_name: str,
    required: bool = True,
) -> date | None:
    """Разобрать дату в формате YYYY-MM-DD или DD.MM.YYYY."""
    value = (raw or "").strip()
    if not value:
        if required:
            raise DataError(
                f"{source}, строка {line}, поле {field_name}: "
                "отсутствует обязательное значение даты"
            )
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise DataError(
        f"{source}, строка {line}, поле {field_name}: "
        f"не удалось разобрать дату {value!r} "
        "(поддерживаются форматы DD.MM.YYYY и YYYY-MM-DD)"
    )


def parse_cli_date(value: str) -> date:
    """Разобрать дату из параметра командной строки."""
    try:
        return parse_date(value, source="--as-of", line=None, field_name="as_of")
    except DataError as error:
        raise ValueError(
            f"не удалось разобрать дату {value!r}: "
            "поддерживаются форматы DD.MM.YYYY и YYYY-MM-DD"
        ) from error


def parse_money(
    raw: str | None,
    *,
    source: str,
    line: int | None,
    field_name: str,
    issues: list[Issue] | None = None,
) -> Decimal:
    """Разобрать денежное значение в Decimal без промежуточного float."""
    value = (raw or "").strip().replace("\u00a0", "").replace(" ", "")
    if not value:
        raise DataError(
            f"{source}, строка {line}, поле {field_name}: "
            "отсутствует обязательное денежное значение"
        )
    # В выгрузке используется точка, но запятую как десятичный разделитель
    # тоже принимаем: файл могли сохранить из русской локали.
    if "," in value and "." not in value:
        value = value.replace(",", ".")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise DataError(
            f"{source}, строка {line}, поле {field_name}: "
            f"не удалось разобрать сумму {raw!r}"
        ) from None
    if not amount.is_finite():
        raise DataError(
            f"{source}, строка {line}, поле {field_name}: "
            f"сумма {raw!r} не является конечным числом"
        )
    if amount < 0:
        raise DataError(
            f"{source}, строка {line}, поле {field_name}: "
            f"отрицательная сумма {amount}"
        )
    quantized = amount.quantize(MONEY_CENT, rounding=ROUND_HALF_UP)
    if quantized != amount and issues is not None:
        issues.append(
            Issue(
                level=WARNING,
                source=source,
                line=line,
                field=field_name,
                message=(
                    f"сумма {amount} имеет больше двух знаков после запятой, "
                    f"округлена до {quantized} (правило: ROUND_HALF_UP)"
                ),
            )
        )
    return quantized


# --------------------------------------------------------------------------
# Чтение CSV
# --------------------------------------------------------------------------


def _read_rows(
    path: Path, required_columns: Sequence[str]
) -> list[tuple[int, dict[str, str]]]:
    if not path.is_file():
        raise DataError(f"Не найден обязательный файл: {path}")

    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as error:
        raise DataError(f"{path.name}: не удалось открыть файл ({error})") from error

    with handle:
        reader = csv.DictReader(handle)
        header = list(reader.fieldnames or [])
        missing = [column for column in required_columns if column not in header]
        if missing:
            raise DataError(
                f"{path.name}: в файле нет обязательных колонок: "
                f"{', '.join(missing)}",
                [f"найдены колонки: {', '.join(header) or '(заголовок пуст)'}"],
            )

        rows: list[tuple[int, dict[str, str]]] = []
        try:
            for row in reader:
                if None in row:
                    raise DataError(
                        f"{path.name}, строка {reader.line_num}: "
                        "в строке больше значений, чем колонок в заголовке"
                    )
                rows.append((reader.line_num, row))
        except csv.Error as error:
            raise DataError(
                f"{path.name}, строка {reader.line_num}: "
                f"не удалось разобрать CSV ({error})"
            ) from error
        return rows


def _require_text(
    row: Mapping[str, str],
    key: str,
    *,
    source: str,
    line: int,
) -> str:
    value = (row.get(key) or "").strip()
    if not value:
        raise DataError(
            f"{source}, строка {line}, поле {key}: отсутствует обязательное значение"
        )
    return value


def _is_blank_row(row: Mapping[str, str]) -> bool:
    return all(not (value or "").strip() for value in row.values())


# --------------------------------------------------------------------------
# Загрузка файлов
# --------------------------------------------------------------------------


def _load_contracts(path: Path, issues: list[Issue]) -> dict[str, Contract]:
    source = path.name
    contracts: dict[str, Contract] = {}
    for line, row in _read_rows(path, REQUIRED_COLUMNS[CONTRACTS_FILE]):
        if _is_blank_row(row):
            issues.append(Issue(WARNING, source, line, None, "пустая строка пропущена"))
            continue
        contract_id = _require_text(row, "contract_id", source=source, line=line)
        if contract_id in contracts:
            raise DataError(
                f"{source}, строка {line}, поле contract_id: "
                f"идентификатор {contract_id!r} уже встречался "
                f"в строке {contracts[contract_id].line_no}"
            )
        signed_at = parse_date(
            row.get("signed_at"), source=source, line=line, field_name="signed_at"
        )
        if signed_at is None:  # обязательное поле: parse_date уже проверил значение
            raise DataError(
                f"{source}, строка {line}, поле signed_at: "
                "не удалось определить дату подписания договора"
            )
        contracts[contract_id] = Contract(
            contract_id=contract_id,
            contractor=_require_text(row, "contractor", source=source, line=line),
            amount=parse_money(
                row.get("amount"),
                source=source,
                line=line,
                field_name="amount",
                issues=issues,
            ),
            signed_at=signed_at,
            line_no=line,
        )
    return contracts


def _load_works(path: Path, issues: list[Issue]) -> dict[str, Work]:
    source = path.name
    works: dict[str, Work] = {}
    for line, row in _read_rows(path, REQUIRED_COLUMNS[WORKS_FILE]):
        if _is_blank_row(row):
            issues.append(Issue(WARNING, source, line, None, "пустая строка пропущена"))
            continue
        work_id = _require_text(row, "work_id", source=source, line=line)
        if work_id in works:
            raise DataError(
                f"{source}, строка {line}, поле work_id: "
                f"идентификатор {work_id!r} уже встречался "
                f"в строке {works[work_id].line_no}"
            )
        status = _require_text(row, "status", source=source, line=line)
        if status not in WORK_STATUSES:
            raise DataError(
                f"{source}, строка {line}, поле status: неизвестный статус "
                f"{status!r} (допустимо: {', '.join(WORK_STATUSES)})"
            )
        works[work_id] = Work(
            work_id=work_id,
            contract_id=_require_text(row, "contract_id", source=source, line=line),
            title=_require_text(row, "title", source=source, line=line),
            price=parse_money(
                row.get("price"),
                source=source,
                line=line,
                field_name="price",
                issues=issues,
            ),
            status=status,
            completed_at=parse_date(
                row.get("completed_at"),
                source=source,
                line=line,
                field_name="completed_at",
                required=False,
            ),
            cancelled_at=parse_date(
                row.get("cancelled_at"),
                source=source,
                line=line,
                field_name="cancelled_at",
                required=False,
            ),
            line_no=line,
        )
    return works


def _load_payments(path: Path, issues: list[Issue]) -> dict[str, Payment]:
    source = path.name
    payments: dict[str, Payment] = {}
    for line, row in _read_rows(path, REQUIRED_COLUMNS[PAYMENTS_FILE]):
        if _is_blank_row(row):
            issues.append(Issue(WARNING, source, line, None, "пустая строка пропущена"))
            continue
        payment_id = _require_text(row, "payment_id", source=source, line=line)
        if payment_id in payments:
            raise DataError(
                f"{source}, строка {line}, поле payment_id: "
                f"идентификатор {payment_id!r} уже встречался "
                f"в строке {payments[payment_id].line_no}"
            )
        status = _require_text(row, "status", source=source, line=line)
        if status not in PAYMENT_STATUSES:
            raise DataError(
                f"{source}, строка {line}, поле status: неизвестный статус "
                f"{status!r} (допустимо: {', '.join(PAYMENT_STATUSES)})"
            )
        work_id = (row.get("work_id") or "").strip() or None
        created_at = parse_date(
            row.get("created_at"), source=source, line=line, field_name="created_at"
        )
        if created_at is None:  # обязательное поле: parse_date уже проверил значение
            raise DataError(
                f"{source}, строка {line}, поле created_at: "
                "не удалось определить дату создания платежа"
            )
        payments[payment_id] = Payment(
            payment_id=payment_id,
            contract_id=_require_text(row, "contract_id", source=source, line=line),
            work_id=work_id,
            purpose=(row.get("purpose") or "").strip(),
            amount=parse_money(
                row.get("amount"),
                source=source,
                line=line,
                field_name="amount",
                issues=issues,
            ),
            status=status,
            created_at=created_at,
            paid_at=parse_date(
                row.get("paid_at"),
                source=source,
                line=line,
                field_name="paid_at",
                required=False,
            ),
            refunded_at=parse_date(
                row.get("refunded_at"),
                source=source,
                line=line,
                field_name="refunded_at",
                required=False,
            ),
            line_no=line,
        )
    return payments


# --------------------------------------------------------------------------
# Проверка целостности
# --------------------------------------------------------------------------


def _validate_links(
    contracts: Mapping[str, Contract],
    works: Mapping[str, Work],
    payments: Mapping[str, Payment],
) -> None:
    problems: list[str] = []

    for work in works.values():
        if work.contract_id not in contracts:
            problems.append(
                f"{WORKS_FILE}, строка {work.line_no}, поле contract_id: "
                f"работа {work.work_id} ссылается на несуществующий договор "
                f"{work.contract_id}"
            )

    for payment in payments.values():
        source = PAYMENTS_FILE
        if payment.contract_id not in contracts:
            problems.append(
                f"{source}, строка {payment.line_no}, поле contract_id: "
                f"платёж {payment.payment_id} ссылается на несуществующий "
                f"договор {payment.contract_id}"
            )
        if payment.work_id is not None:
            work = works.get(payment.work_id)
            if work is None:
                problems.append(
                    f"{source}, строка {payment.line_no}, поле work_id: "
                    f"платёж {payment.payment_id} ссылается на несуществующую "
                    f"работу {payment.work_id}"
                )
            elif work.contract_id != payment.contract_id:
                problems.append(
                    f"{source}, строка {payment.line_no}, поле work_id: "
                    f"платёж {payment.payment_id} относится к договору "
                    f"{payment.contract_id}, а работа {payment.work_id} - "
                    f"к договору {work.contract_id}"
                )

    if problems:
        raise DataError("Найдены ошибки ссылочной целостности:", problems)


def _validate_chronology(payments: Mapping[str, Payment]) -> None:
    source = PAYMENTS_FILE
    problems: list[str] = []
    for payment in payments.values():
        if payment.paid_at is not None and payment.paid_at < payment.created_at:
            problems.append(
                f"{source}, строка {payment.line_no}, поле paid_at: платёж "
                f"{payment.payment_id} оплачен {payment.paid_at:%d.%m.%Y} "
                f"раньше, чем создан ({payment.created_at:%d.%m.%Y})"
            )
        if payment.status == "paid" and payment.paid_at is None:
            problems.append(
                f"{source}, строка {payment.line_no}, поле paid_at: у платежа "
                f"{payment.payment_id} статус paid, но дата оплаты не заполнена"
            )
        if payment.status == "refunded":
            if payment.paid_at is None:
                problems.append(
                    f"{source}, строка {payment.line_no}, поле paid_at: у платежа "
                    f"{payment.payment_id} статус refunded, но дата оплаты "
                    "не заполнена"
                )
            elif payment.refunded_at is None:
                problems.append(
                    f"{source}, строка {payment.line_no}, поле refunded_at: "
                    f"у платежа {payment.payment_id} статус refunded, но дата "
                    "возврата не заполнена"
                )
        if payment.refunded_at is not None:
            if payment.paid_at is None:
                problems.append(
                    f"{source}, строка {payment.line_no}, поле refunded_at: "
                    f"у платежа {payment.payment_id} есть дата возврата, но нет "
                    "даты оплаты"
                )
            elif payment.refunded_at < payment.paid_at:
                problems.append(
                    f"{source}, строка {payment.line_no}, поле refunded_at: "
                    f"платёж {payment.payment_id} возвращён "
                    f"{payment.refunded_at:%d.%m.%Y} раньше, чем оплачен "
                    f"({payment.paid_at:%d.%m.%Y})"
                )
    if problems:
        raise DataError("Найдены ошибки хронологии платежей:", problems)


def _collect_consistency_issues(
    contracts: Mapping[str, Contract],
    works: Mapping[str, Work],
    payments: Mapping[str, Payment],
    issues: list[Issue],
) -> None:
    """Собрать мягкие расхождения: статус против дат, платёж до подписания."""
    for work in works.values():
        source = WORKS_FILE
        line = work.line_no
        if work.status == "done" and work.completed_at is None:
            issues.append(
                Issue(
                    WARNING,
                    source,
                    line,
                    "completed_at",
                    f"статус работы {work.work_id} - done, но дата приёмки "
                    "не заполнена; на отчётную дату работа считается "
                    "незавершённой",
                )
            )
        if work.status == "cancelled" and work.cancelled_at is None:
            issues.append(
                Issue(
                    WARNING,
                    source,
                    line,
                    "cancelled_at",
                    f"статус работы {work.work_id} - cancelled, но дата отмены "
                    "не заполнена; отмена не датирована и в историческом "
                    "состоянии не учитывается",
                )
            )
        if work.status == "planned" and work.completed_at is not None:
            issues.append(
                Issue(
                    WARNING,
                    source,
                    line,
                    "completed_at",
                    f"статус работы {work.work_id} - planned, но заполнена дата "
                    f"приёмки {work.completed_at:%d.%m.%Y}; расчёт опирается "
                    "на даты",
                )
            )
        if work.status == "planned" and work.cancelled_at is not None:
            issues.append(
                Issue(
                    WARNING,
                    source,
                    line,
                    "cancelled_at",
                    f"статус работы {work.work_id} - planned, но заполнена дата "
                    f"отмены {work.cancelled_at:%d.%m.%Y}; расчёт опирается "
                    "на даты",
                )
            )
        if work.status == "done" and work.cancelled_at is not None:
            issues.append(
                Issue(
                    WARNING,
                    source,
                    line,
                    "status",
                    f"у работы {work.work_id} статус done, но заполнена дата "
                    f"отмены {work.cancelled_at:%d.%m.%Y}; приоритет у отмены",
                )
            )
        if work.completed_at is not None and work.cancelled_at is not None:
            issues.append(
                Issue(
                    WARNING,
                    source,
                    line,
                    "cancelled_at",
                    f"у работы {work.work_id} заполнены и дата приёмки "
                    f"({work.completed_at:%d.%m.%Y}), и дата отмены "
                    f"({work.cancelled_at:%d.%m.%Y}); в расчёте отмена "
                    "закрывает работу",
                )
            )

    for payment in payments.values():
        source = PAYMENTS_FILE
        line = payment.line_no
        contract = contracts.get(payment.contract_id)
        if payment.status == "queued" and payment.paid_at is not None:
            issues.append(
                Issue(
                    WARNING,
                    source,
                    line,
                    "status",
                    f"у платежа {payment.payment_id} статус queued, но заполнена "
                    f"дата оплаты {payment.paid_at:%d.%m.%Y}; расчёт опирается "
                    "на даты и считает платёж исполненным",
                )
            )
        if payment.refunded_at is not None and payment.status != "refunded":
            issues.append(
                Issue(
                    WARNING,
                    source,
                    line,
                    "status",
                    f"у платежа {payment.payment_id} заполнена дата возврата, "
                    f"но статус {payment.status!r}; расчёт опирается на дату "
                    "возврата",
                )
            )
        if contract is not None:
            if payment.created_at < contract.signed_at:
                issues.append(
                    Issue(
                        WARNING,
                        source,
                        line,
                        "created_at",
                        f"платёж {payment.payment_id} заведён "
                        f"{payment.created_at:%d.%m.%Y} раньше подписания "
                        f"договора {contract.contract_id} "
                        f"({contract.signed_at:%d.%m.%Y}) - возможная ошибка "
                        "исходных данных",
                    )
                )
            elif payment.paid_at is not None and payment.paid_at < contract.signed_at:
                issues.append(
                    Issue(
                        WARNING,
                        source,
                        line,
                        "paid_at",
                        f"платёж {payment.payment_id} оплачен "
                        f"{payment.paid_at:%d.%m.%Y} раньше подписания договора "
                        f"{contract.contract_id} "
                        f"({contract.signed_at:%d.%m.%Y}) - возможная ошибка "
                        "исходных данных",
                    )
                )


def load_dataset(data_dir: str | Path) -> Dataset:
    """Прочитать и проверить выгрузку из каталога ``data_dir``."""
    directory = Path(data_dir)
    if not directory.exists():
        raise DataError(f"Каталог с данными не найден: {directory}")
    if not directory.is_dir():
        raise DataError(f"Путь к данным не является каталогом: {directory}")

    missing = [name for name in DATA_FILES if not (directory / name).is_file()]
    if missing:
        raise DataError(
            f"В каталоге {directory} нет обязательных файлов: {', '.join(missing)}",
            [f"ожидаются файлы: {', '.join(DATA_FILES)}"],
        )

    issues: list[Issue] = []
    contracts = _load_contracts(directory / CONTRACTS_FILE, issues)
    if not contracts:
        raise DataError(f"{CONTRACTS_FILE}: файл не содержит ни одного договора")
    works = _load_works(directory / WORKS_FILE, issues)
    payments = _load_payments(directory / PAYMENTS_FILE, issues)

    _validate_links(contracts, works, payments)
    _validate_chronology(payments)
    _collect_consistency_issues(contracts, works, payments, issues)

    return Dataset(
        contracts=contracts,
        works=works,
        payments=payments,
        issues=issues,
        data_dir=directory,
    )