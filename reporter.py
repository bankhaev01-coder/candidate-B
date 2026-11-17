"""Формирование отчётов: markdown для человека и CSV для проверки.

Модуль ничего не считает: он только раскладывает готовый :class:`Report` по
файлам. Все суммы выводятся с двумя знаками после запятой, markdown содержит
разделы «Сводка», «Работы», «Договоры», «Платежи и распределение»,
«Требует согласования» и «Наблюдения и проблемы данных».
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Iterable, Sequence

from calculator import (
    CONTRACT_LEVEL,
    STATE_REFUNDED,
    ContractResult,
    PaymentResult,
    Report,
    format_date,
    format_money,
    natural_key,
)

REPORT_MD = "report.md"
WORKS_CSV = "report_works.csv"
CONTRACTS_CSV = "report_contracts.csv"
PAYMENTS_CSV = "report_payments.csv"
LEDGER_CSV = "report_ledger.csv"
ISSUES_CSV = "report_issues.csv"


def _csv_money(value) -> str:
    return f"{value:.2f}"


def _csv_date(value) -> str:
    """Дата для CSV: пустая строка вместо прочерка."""
    return value.strftime("%d.%m.%Y") if value else ""


def _cell(value: object) -> str:
    """Экранировать значение для ячейки markdown-таблицы."""
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _table(header: Sequence[str], rows: Iterable[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(_cell(item) for item in header) + " |"]
    lines.append("|" + "|".join("---" for _ in header) + "|")
    for row in rows:
        lines.append("| " + " | ".join(_cell(item) for item in row) + " |")
    return lines


def _distribution_text(item: PaymentResult) -> str:
    if not item.lines:
        return "—"
    parts: list[str] = []
    by_work = item.net_by_work()
    for work_id in sorted(by_work, key=natural_key):
        parts.append(f"{work_id} — {format_money(by_work[work_id])}")
    if item.unallocated != 0:
        parts.append(f"нераспределено — {format_money(item.unallocated)}")
    if not parts:
        return "распределение снято возвратом"
    return "; ".join(parts)


def _binding_text(item: PaymentResult) -> str:
    if item.state_code in ("queued", "paid_later") and item.binding_code == CONTRACT_LEVEL:
        return "по договору (распределение при оплате)"
    return item.binding


def build_report_markdown(report: Report) -> str:
    totals = report.totals
    allocated_active = sum((item.allocated_active for item in report.contracts), start=0)
    allocated_cancelled = sum(
        (item.allocated_cancelled for item in report.contracts), start=0
    )
    lines: list[str] = []
    lines.append("# Отчёт о выплатах по договорам")
    lines.append("")
    lines.append(
        f"Отчётная дата: **{format_date(totals.as_of)}** (включительно). "
        "Все суммы - в рублях с двумя знаками после запятой."
    )
    lines.append("")
    lines.append(
        "Состояние восстановлено по датам событий (`paid_at`, `refunded_at`, "
        "`completed_at`, `cancelled_at`), а не по полю `status`: в выгрузке "
        "статус отражает момент выгрузки."
    )
    lines.append("")

    lines.append("## 1. Сводка")
    lines.append("")
    summary = [
        ("Отчётная дата", format_date(totals.as_of), "включительно"),
        (
            "Договоров в сводке",
            str(totals.contracts_count),
            "подписаны на отчётную дату"
            if not totals.inactive_contracts_count
            else "подписаны на отчётную дату; остальные исключены",
        ),
        (
            "Работ в сводке",
            str(totals.works_count),
            f"отменены на отчётную дату: {totals.cancelled_works_count}",
        ),
        ("Общая стоимость работ", format_money(totals.works_total), "сумма цен всех работ"),
        (
            "Стоимость активных работ",
            format_money(totals.active_works_total),
            "без работ, отменённых на отчётную дату",
        ),
        (
            "Стоимость отменённых работ",
            format_money(totals.cancelled_works_total),
            "обязательством по оплате не является",
        ),
        (
            "Выплачено (брутто)",
            format_money(totals.paid_gross),
            "платежи с paid_at не позже отчётной даты",
        ),
        (
            "Возвращено",
            format_money(totals.refunded),
            "возвраты с refunded_at не позже отчётной даты",
        ),
        (
            "Фактически выплачено (нетто)",
            format_money(totals.paid_net),
            "брутто минус возвраты",
        ),
        (
            "Остаток обязательств по активным работам",
            format_money(totals.remaining_obligations),
            "цена активных работ минус выплаты по ним",
        ),
        (
            "Платежи в очереди",
            format_money(totals.queued_total),
            "созданы до отчётной даты, деньги не ушли",
        ),
        (
            "Платежи с оплатой после отчётной даты",
            format_money(totals.pending_total),
            "на отчётную дату ещё не исполнены",
        ),
        (
            "Нераспределённые суммы",
            format_money(totals.unallocated_total),
            "выплачено, но не отнесено ни на одну работу",
        ),
        (
            "Требует согласования",
            format_money(totals.approval_total),
            "выплаты по отменённым работам и переплаты",
        ),
        (
            "Исключено платежей",
            str(totals.excluded_payments_count),
            "созданы позже отчётной даты и не существовали на неё",
        ),
    ]
    if totals.inactive_contracts_count or totals.inactive_works_count:
        summary.append(
            (
                "Не включено в сводку",
                f"договоров {totals.inactive_contracts_count}, "
                f"работ {totals.inactive_works_count}",
                "подписаны позже отчётной даты",
            )
        )
    lines.extend(_table(("Показатель", "Значение", "Что означает"), summary))
    lines.append("")
    lines.append(
        f"Контроль: выплачено (нетто) {format_money(totals.paid_net)} = "
        f"распределено на активные работы {format_money(allocated_active)} + "
        f"на отменённые работы {format_money(allocated_cancelled)} + "
        f"нераспределено {format_money(totals.unallocated_total)}."
    )
    lines.append("")

    lines.append("## 2. Работы")
    lines.append("")
    work_rows = []
    for item in report.works:
        remaining = "—" if item.is_cancelled else format_money(item.remaining)
        work_rows.append(
            (
                item.work.contract_id,
                item.contract.contractor,
                f"{item.work.work_id} — {item.work.title}",
                format_money(item.price),
                format_money(item.paid),
                remaining,
                item.status_at_report,
                "; ".join(item.notes),
            )
        )
    lines.extend(
        _table(
            (
                "Договор",
                "Подрядчик",
                "Работа",
                "Цена",
                "Выплачено",
                "Осталось оплатить",
                "Статус",
                "Примечание",
            ),
            work_rows,
        )
    )
    lines.append("")
    lines.append(
        "«Выплачено» - чистая сумма: распределённые платежи минус возвраты. "
        "По отменённым работам остаток не показывается: это не обязательство, "
        "а сумма к согласованию."
    )
    lines.append("")

    lines.append("## 3. Договоры")
    lines.append("")
    for index, item in enumerate(report.contracts):
        block = _contract_lines(item)
        while block and not block[-1]:
            block.pop()
        if index:
            lines.append("")
        lines.extend(block)
    lines.append("")

    lines.append("## 4. Платежи и распределение")
    lines.append("")
    payment_rows = []
    for item in report.payments:
        payment = item.payment
        payment_rows.append(
            (
                payment.payment_id,
                payment.contract_id,
                payment.purpose,
                format_money(payment.amount),
                format_date(payment.paid_at),
                item.state,
                _binding_text(item),
                _distribution_text(item),
            )
        )
    lines.extend(
        _table(
            (
                "Платёж",
                "Договор",
                "Назначение",
                "Сумма",
                "Оплачен",
                "Состояние на отчётную дату",
                "Привязка",
                "Распределение",
            ),
            payment_rows,
        )
    )
    lines.append("")
    lines.append(
        "Каждое распределение можно проверить построчно в `report_ledger.csv` "
        "(платёж, дата события, работа, сумма, основание)."
    )
    lines.append("")

    lines.append("## 5. Требует согласования")
    lines.append("")
    approval_rows = []
    for item in report.works:
        if item.approval <= 0:
            continue
        reason = (
            f"работа отменена {format_date(item.work.cancelled_at)}, "
            "выплата остаётся за ней"
            if item.is_cancelled
            else "выплачено больше цены работы"
        )
        approval_rows.append(
            (
                f"{item.work.work_id} — {item.work.title}",
                format_money(item.approval),
                reason,
                "возможный возврат или зачёт",
            )
        )
    for item in report.contracts:
        if item.unallocated > 0:
            approval_rows.append(
                (
                    f"{item.contract.contract_id} — {item.contract.contractor}",
                    format_money(item.unallocated),
                    "нераспределённая сумма на уровне договора",
                    "уточнить, к какой работе относится",
                )
            )
    if approval_rows:
        lines.extend(
            _table(
                ("Объект", "Сумма", "Почему", "Что нужно"),
                approval_rows,
            )
        )
    else:
        lines.append("Сумм, требующих отдельного решения, нет.")
    lines.append("")

    lines.append("## 6. Наблюдения и проблемы данных")
    lines.append("")
    observations: list[str] = []
    for issue in report.issues:
        observations.append(f"- `{issue.location}`: {issue.message}")
    if not observations:
        observations.append(
            "- Проверки не нашли противоречий между статусами и датами."
        )
    for payment in report.excluded_payments:
        observations.append(
            f"- Платёж `{payment.payment_id}` ({format_money(payment.amount)}, "
            f"договор {payment.contract_id}) создан "
            f"{format_date(payment.created_at)} - позже отчётной даты, "
            "в расчёте не участвует."
        )
    for item in report.payments:
        payment = item.payment
        if payment.paid_at is not None and payment.paid_at > totals.as_of:
            observations.append(
                f"- Платёж `{payment.payment_id}` оплачен "
                f"{format_date(payment.paid_at)} - после отчётной даты, "
                "на дату отчёта считается неисполненным."
            )
        if payment.refunded_at is not None and payment.refunded_at > totals.as_of:
            observations.append(
                f"- Возврат платежа `{payment.payment_id}` - "
                f"{format_date(payment.refunded_at)}, после отчётной даты, "
                "поэтому на отчётную дату платёж считается выплаченным."
            )
    for item in report.works:
        work = item.work
        if work.completed_at is not None and work.completed_at > totals.as_of:
            observations.append(
                f"- Работа `{work.work_id}` принята по выгрузке "
                f"{format_date(work.completed_at)} - после отчётной даты, "
                "на дату отчёта не завершена."
            )
        if work.cancelled_at is not None and work.cancelled_at > totals.as_of:
            observations.append(
                f"- Работа `{work.work_id}` отменена по выгрузке "
                f"{format_date(work.cancelled_at)} - после отчётной даты, "
                "на дату отчёта действует."
            )
    lines.extend(observations)
    lines.append("")

    lines.append("## 7. Как считалось (кратко)")
    lines.append("")
    lines.append(
        "- Платёж с `work_id` относится на эту работу целиком, даже если сумма "
        "больше цены: превышение показано как переплата."
    )
    lines.append(
        "- Платёж без `work_id` привязывается к работе только при однозначном "
        "номере или названии работы в назначении; иначе распределяется "
        "пропорционально остаткам стоимости работ договора."
    )
    lines.append(
        "- Возврат снимает ровно те суммы с тех работ, куда платёж был "
        "распределён при оплате, и заново не распределяется."
    )
    lines.append(
        "- Платежи в очереди не уменьшают остаток обязательств и показаны "
        "отдельно."
    )
    lines.append(
        "- Деньги считаются в Decimal до копейки; остаточные копейки "
        "распределяются по наибольшим остаткам, при равенстве - по номеру "
        "работы. Подробности - в `DECISIONS.md`."
    )

    return "\n".join(lines)


def _contract_lines(result: ContractResult) -> list[str]:
    contract = result.contract
    lines = [f"### {contract.contract_id} — {contract.contractor}", ""]
    if not result.is_active:
        lines.append(
            f"Договор подписан {format_date(contract.signed_at)} - позже отчётной "
            "даты, поэтому его обязательства в сводку не включены."
        )
        lines.append("")
    rows = [
        ("Дата подписания", format_date(contract.signed_at)),
        ("Первоначальная сумма договора", format_money(contract.amount)),
        ("Общая стоимость работ", format_money(result.works_total)),
        ("Стоимость активных работ", format_money(result.active_works_total)),
        ("Стоимость отменённых работ", format_money(result.cancelled_works_total)),
        ("Выплачено (брутто)", format_money(result.paid_gross)),
        ("Возвращено", format_money(result.refunded)),
        ("Выплачено (нетто)", format_money(result.paid_net)),
        ("Распределено на активные работы", format_money(result.allocated_active)),
        ("Распределено на отменённые работы", format_money(result.allocated_cancelled)),
        ("Нераспределено", format_money(result.unallocated)),
        ("Остаток обязательств", format_money(result.remaining_obligations)),
        ("Платежи в очереди", format_money(result.queued_total)),
        (
            "Платежи с оплатой после отчётной даты",
            format_money(result.pending_total),
        ),
        (
            "Расхождение: сумма договора минус стоимость работ",
            format_money(result.works_diff),
        ),
    ]
    lines.extend(_table(("Показатель", "Значение"), rows))
    lines.append("")
    if result.works_diff > 0:
        lines.append(
            f"Сумма договора больше стоимости работ на "
            f"{format_money(result.works_diff)}: в выгрузке перечислены не все "
            "работы либо договор заключён с запасом. На работы эта сумма "
            "не разносится."
        )
    elif result.works_diff < 0:
        lines.append(
            f"Стоимость работ больше суммы договора на "
            f"{format_money(-result.works_diff)}: возможна ошибка выгрузки или "
            "дополнительное соглашение."
        )
    else:
        lines.append("Сумма договора совпадает со стоимостью работ.")
    if result.queued_payments:
        lines.append("")
        lines.append(
            "Ожидают оплаты (в очереди), в остаток обязательств не входят:"
        )
        for payment in result.queued_payments:
            lines.append(
                f"- `{payment.payment_id}` — {format_money(payment.amount)}, "
                f"«{payment.purpose}», создан {format_date(payment.created_at)}"
            )
        lines.append("")
    if result.pending_payments:
        lines.append("")
        lines.append(
            "Оплачены после отчётной даты, на дату отчёта не исполнены:"
        )
        for payment in result.pending_payments:
            lines.append(
                f"- `{payment.payment_id}` — {format_money(payment.amount)}, "
                f"«{payment.purpose}», оплачен {format_date(payment.paid_at)}"
            )
    return lines


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------


def _write_csv(
    path: Path,
    header: Sequence[str],
    rows: Iterable[Sequence[object]],
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow(["" if value is None else value for value in row])
    return path


def write_markdown(report: Report, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_report_markdown(report) + "\n", encoding="utf-8", newline="\n")
    return path


def write_works_csv(report: Report, path: Path) -> Path:
    header = [
        "contract_id",
        "contractor",
        "work_id",
        "title",
        "status_at_report",
        "price",
        "paid",
        "remaining",
        "overpayment",
        "approval",
        "completed_at",
        "cancelled_at",
        "note",
    ]
    rows = [
        [
            item.work.contract_id,
            item.contract.contractor,
            item.work.work_id,
            item.work.title,
            item.status_at_report,
            _csv_money(item.price),
            _csv_money(item.paid),
            "" if item.is_cancelled else _csv_money(item.remaining),
            _csv_money(item.overpayment),
            _csv_money(item.approval),
            _csv_date(item.work.completed_at),
            _csv_date(item.work.cancelled_at),
            "; ".join(item.notes),
        ]
        for item in report.works
    ]
    return _write_csv(path, header, rows)


def write_contracts_csv(report: Report, path: Path) -> Path:
    header = [
        "contract_id",
        "contractor",
        "signed_at",
        "contract_amount",
        "works_total",
        "active_works_total",
        "cancelled_works_total",
        "paid_gross",
        "refunded",
        "paid_net",
        "allocated_active",
        "allocated_cancelled",
        "unallocated",
        "remaining_obligations",
        "queued_total",
        "pending_total",
        "works_diff",
        "approval",
        "overpayment",
        "is_active",
    ]
    rows = [
        [
            item.contract.contract_id,
            item.contract.contractor,
            _csv_date(item.contract.signed_at),
            _csv_money(item.contract.amount),
            _csv_money(item.works_total),
            _csv_money(item.active_works_total),
            _csv_money(item.cancelled_works_total),
            _csv_money(item.paid_gross),
            _csv_money(item.refunded),
            _csv_money(item.paid_net),
            _csv_money(item.allocated_active),
            _csv_money(item.allocated_cancelled),
            _csv_money(item.unallocated),
            _csv_money(item.remaining_obligations),
            _csv_money(item.queued_total),
            _csv_money(item.pending_total),
            _csv_money(item.works_diff),
            _csv_money(item.approval),
            _csv_money(item.overpayment),
            "yes" if item.is_active else "no",
        ]
        for item in report.contracts
    ]
    return _write_csv(path, header, rows)


def _distribution_csv(item: PaymentResult) -> str:
    parts = [
        f"{work_id}: {_csv_money(value)}"
        for work_id, value in sorted(item.net_by_work().items(), key=lambda pair: natural_key(pair[0]))
    ]
    if item.unallocated != 0:
        parts.append(f"unallocated: {_csv_money(item.unallocated)}")
    if not parts and item.state_code == STATE_REFUNDED:
        return "возвращено полностью"
    return "; ".join(parts)


def write_payments_csv(report: Report, path: Path) -> Path:
    header = [
        "payment_id",
        "contract_id",
        "purpose",
        "amount",
        "status_export",
        "created_at",
        "paid_at",
        "refunded_at",
        "state_at_report",
        "binding",
        "target_work_id",
        "allocated_net",
        "unallocated_net",
        "distribution",
    ]
    rows = [
        [
            item.payment.payment_id,
            item.payment.contract_id,
            item.payment.purpose,
            _csv_money(item.payment.amount),
            item.payment.status,
            _csv_date(item.payment.created_at),
            _csv_date(item.payment.paid_at),
            _csv_date(item.payment.refunded_at),
            item.state,
            _binding_text(item),
            item.target_work_id or "",
            _csv_money(item.allocated),
            _csv_money(item.unallocated),
            _distribution_csv(item),
        ]
        for item in report.payments
    ]
    excluded = [
        [
            payment.payment_id,
            payment.contract_id,
            payment.purpose,
            _csv_money(payment.amount),
            payment.status,
            _csv_date(payment.created_at),
            _csv_date(payment.paid_at),
            _csv_date(payment.refunded_at),
            "не существовал на отчётную дату",
            "",
            "",
            "",
            "",
            "",
        ]
        for payment in report.excluded_payments
    ]
    return _write_csv(path, header, rows + excluded)


def write_ledger_csv(report: Report, path: Path) -> Path:
    header = [
        "payment_id",
        "event",
        "event_date",
        "contract_id",
        "work_id",
        "kind",
        "amount",
        "reason",
    ]
    rows = [
        [
            line.payment_id,
            line.event,
            _csv_date(line.date),
            line.contract_id,
            line.work_id or "",
            line.kind,
            _csv_money(line.amount),
            line.reason,
        ]
        for line in report.ledger
    ]
    return _write_csv(path, header, rows)


def write_issues_csv(report: Report, path: Path) -> Path:
    header = ["level", "file", "line", "field", "message"]
    rows = [
        [issue.level, issue.source, issue.line or "", issue.field or "", issue.message]
        for issue in report.issues
    ]
    return _write_csv(path, header, rows)


def write_all(report: Report, out_dir: str | Path) -> dict[str, Path]:
    """Записать все отчёты в каталог ``out_dir`` и вернуть пути к файлам."""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    return {
        REPORT_MD: write_markdown(report, directory / REPORT_MD),
        WORKS_CSV: write_works_csv(report, directory / WORKS_CSV),
        CONTRACTS_CSV: write_contracts_csv(report, directory / CONTRACTS_CSV),
        PAYMENTS_CSV: write_payments_csv(report, directory / PAYMENTS_CSV),
        LEDGER_CSV: write_ledger_csv(report, directory / LEDGER_CSV),
        ISSUES_CSV: write_issues_csv(report, directory / ISSUES_CSV),
    }


def summary_lines(report: Report) -> list[str]:
    totals = report.totals
    lines = [
        f"Отчёт на {format_date(totals.as_of)} (включительно)",
        (
            f"Договоров: {totals.contracts_count}; работ: {totals.works_count} "
            f"(отменены на дату: {totals.cancelled_works_count})"
        ),
        (
            f"Стоимость работ: {format_money(totals.works_total)}; "
            f"активных: {format_money(totals.active_works_total)}; "
            f"отменённых: {format_money(totals.cancelled_works_total)}"
        ),
        (
            f"Выплачено (брутто): {format_money(totals.paid_gross)}; "
            f"возвращено: {format_money(totals.refunded)}; "
            f"фактически выплачено: {format_money(totals.paid_net)}"
        ),
        (
            "Остаток обязательств по активным работам: "
            f"{format_money(totals.remaining_obligations)}"
        ),
        (
            f"Платежи в очереди: {format_money(totals.queued_total)}; "
            "с оплатой после отчётной даты: "
            f"{format_money(totals.pending_total)}"
        ),
        (
            f"Нераспределённые суммы: {format_money(totals.unallocated_total)}; "
            f"требуют согласования: {format_money(totals.approval_total)}"
        ),
    ]
    if totals.excluded_payments_count:
        lines.append(
            "Исключены платежи, созданные после отчётной даты: "
            f"{totals.excluded_payments_count}"
        )
    if totals.inactive_contracts_count or totals.inactive_works_count:
        lines.append(
            "Не включены в сводку (договор подписан позже отчётной даты): "
            f"договоров {totals.inactive_contracts_count}, "
            f"работ {totals.inactive_works_count}"
        )
    if report.issues:
        lines.append(f"Замечаний к данным: {len(report.issues)} (см. {ISSUES_CSV})")
    else:
        lines.append("Замечаний к данным нет")
    return lines


def print_summary(
    report: Report,
    paths: dict[str, Path] | None = None,
    stream=None,
) -> None:
    output = stream or sys.stdout
    for line in summary_lines(report):
        print(line, file=output)
    if paths:
        print("", file=output)
        print("Отчёты:", file=output)
        for name, path in paths.items():
            print(f"  {name}: {path.resolve()}", file=output)