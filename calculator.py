"""Расчёт выплат и остатков на отчётную дату.

Логика в одном месте:

* состояние работ и платежей восстанавливается по датам событий, а не по
  полю ``status`` (в выгрузке это состояние на момент выгрузки);
* все платежи обрабатываются в единой хронологии по ``paid_at``
  (при равенстве дат - по ``payment_id``);
* возвраты - отдельные события по ``refunded_at``: они снимают ровно те суммы
  с тех работ, куда платёж был распределён при оплате, и не распределяются
  заново;
* платежи без однозначной привязки распределяются пропорционально остаткам
  стоимости работ договора, деньги считаются в Decimal до копейки.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_FLOOR
from functools import lru_cache

from loader import (
    CONTRACTS_FILE,
    PAYMENTS_FILE,
    Contract,
    Dataset,
    Issue,
    Payment,
    WARNING,
    Work,
)

ZERO = Decimal("0.00")
CENT = Decimal("0.01")

# Более короткие названия работ не ищем в назначении платежа: слишком высок
# риск случайного совпадения.
MIN_TITLE_MATCH_LENGTH = 4

DIRECT_WORK_ID = "work_id"
PURPOSE_ID = "purpose_id"
PURPOSE_TITLE = "purpose_title"
CONTRACT_LEVEL = "contract"

BINDING_LABELS = {
    DIRECT_WORK_ID: "прямая привязка work_id",
    PURPOSE_ID: "по назначению (номер работы)",
    PURPOSE_TITLE: "по назначению (название работы)",
    CONTRACT_LEVEL: "по договору (пропорционально)",
}

STATE_PAID = "paid"
STATE_REFUNDED = "refunded"
STATE_QUEUED = "queued"
STATE_PAID_LATER = "paid_later"

_EVENT_PAYMENT = 0
_EVENT_REFUND = 1


def natural_key(value: str) -> tuple[tuple[int, object], ...]:
    """Ключ сортировки, в котором W-2 идёт раньше W-10."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", value)
    )


def format_date(value: date | None) -> str:
    return value.strftime("%d.%m.%Y") if value else "—"


def format_money(value: Decimal) -> str:
    """Денежная сумма с двумя знаками и пробелом между разрядами."""
    return f"{value:,.2f}".replace(",", " ")


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


@lru_cache(maxsize=None)
def _token_pattern(token: str) -> re.Pattern[str]:
    """Скомпилированный шаблон токена; кэш на все встречающиеся номера работ."""
    return re.compile(rf"(?<![\w-]){re.escape(token)}(?![\w-])", re.IGNORECASE)


def contains_token(text: str, token: str) -> bool:
    """Найти идентификатор как отдельный токен (W-14, но не W-140)."""
    if not token:
        return False
    return _token_pattern(token).search(text) is not None


def split_amount(
    amount: Decimal,
    weights: dict[str, Decimal],
    caps: dict[str, Decimal],
) -> tuple[dict[str, Decimal], Decimal]:
    """Разделить ``amount`` пропорционально ``weights`` до копейки.

    Возвращает (доли по работам, нераспределённый остаток). Доли усекаются
    вниз до копейки, затем оставшиеся копейки раздаются работам с наибольшим
    дробным остатком, при равенстве - в порядке ``work_id``. Сумма долей
    никогда не превышает ``caps`` (остаток стоимости работы).
    """
    if amount <= ZERO or not weights:
        return {}, amount
    total_weight = sum(weights.values())
    if total_weight <= ZERO:
        return {}, amount

    exact = {
        work_id: amount * weight / total_weight
        for work_id, weight in weights.items()
    }
    shares = {
        work_id: exact_value.quantize(CENT, rounding=ROUND_FLOOR)
        for work_id, exact_value in exact.items()
    }
    leftover_cents = int((amount - sum(shares.values())) * 100)
    order = sorted(
        shares,
        key=lambda work_id: (
            -(exact[work_id] - shares[work_id]),
            natural_key(work_id),
        ),
    )
    for work_id in order:
        if leftover_cents <= 0:
            break
        if shares[work_id] + CENT > caps[work_id]:
            continue
        shares[work_id] += CENT
        leftover_cents -= 1

    distributed = sum(shares.values())
    return {k: v for k, v in shares.items() if v > ZERO}, amount - distributed


@dataclass(frozen=True)
class WorkState:
    """Историческое состояние работы на отчётную дату."""

    is_cancelled: bool
    is_completed: bool


def work_state_at(work: Work, as_of: date) -> WorkState:
    cancelled = work.cancelled_at is not None and work.cancelled_at <= as_of
    completed = (
        work.completed_at is not None
        and work.completed_at <= as_of
        and not cancelled
    )
    return WorkState(is_cancelled=cancelled, is_completed=completed)


def work_status_label(work: Work, state: WorkState) -> str:
    if state.is_cancelled:
        return f"отменена {format_date(work.cancelled_at)}"
    if state.is_completed:
        return f"завершена {format_date(work.completed_at)}"
    return "не завершена на дату отчёта"


@dataclass(frozen=True)
class LedgerLine:
    """Одна строка распределения: платёж или возврат по конкретной работе."""

    event: str  # "payment" | "refund"
    date: date
    payment_id: str
    contract_id: str
    work_id: str | None  # None - сумма осталась на уровне договора
    kind: str  # work_id / purpose_id / purpose_title / proportional / unallocated / refund
    amount: Decimal
    reason: str = ""

    @property
    def is_unallocated(self) -> bool:
        return self.kind == "unallocated"


@dataclass
class WorkResult:
    work: Work
    contract: Contract
    status_at_report: str
    is_cancelled: bool
    is_completed: bool
    price: Decimal
    allocated: Decimal
    remaining: Decimal
    overpayment: Decimal
    approval: Decimal
    notes: list[str] = field(default_factory=list)

    @property
    def paid(self) -> Decimal:
        """Чистая выплаченная сумма: распределено минус возвраты."""
        return self.allocated


@dataclass
class ContractResult:
    contract: Contract
    is_active: bool
    works_total: Decimal
    active_works_total: Decimal
    cancelled_works_total: Decimal
    paid_gross: Decimal
    refunded: Decimal
    paid_net: Decimal
    allocated_active: Decimal
    allocated_cancelled: Decimal
    unallocated: Decimal
    remaining_obligations: Decimal
    queued_total: Decimal
    pending_total: Decimal
    works_diff: Decimal
    approval: Decimal
    overpayment: Decimal
    queued_payments: list[Payment]
    pending_payments: list[Payment]


@dataclass
class PaymentResult:
    payment: Payment
    state_code: str
    state: str
    binding_code: str
    binding: str
    target_work_id: str | None
    allocated: Decimal
    unallocated: Decimal
    lines: list[LedgerLine] = field(default_factory=list)

    def net_by_work(self) -> dict[str, Decimal]:
        """Итоговое (после возвратов) распределение платежа по работам."""
        totals: dict[str, Decimal] = {}
        for line in self.lines:
            if line.work_id is None:
                continue
            totals[line.work_id] = totals.get(line.work_id, ZERO) + line.amount
        return {work_id: value for work_id, value in totals.items() if value != ZERO}


@dataclass(frozen=True)
class Totals:
    as_of: date
    contracts_count: int
    inactive_contracts_count: int
    works_count: int
    inactive_works_count: int
    cancelled_works_count: int
    works_total: Decimal
    active_works_total: Decimal
    cancelled_works_total: Decimal
    paid_gross: Decimal
    refunded: Decimal
    paid_net: Decimal
    remaining_obligations: Decimal
    queued_total: Decimal
    pending_total: Decimal
    unallocated_total: Decimal
    approval_total: Decimal
    overpayment_total: Decimal
    excluded_payments_count: int


@dataclass
class Report:
    as_of: date
    works: list[WorkResult]
    contracts: list[ContractResult]
    payments: list[PaymentResult]
    excluded_payments: list[Payment]
    ledger: list[LedgerLine]
    issues: list[Issue]
    totals: Totals

    def works_of(self, contract_id: str) -> list[WorkResult]:
        return [item for item in self.works if item.work.contract_id == contract_id]

    def contract(self, contract_id: str) -> ContractResult:
        for item in self.contracts:
            if item.contract.contract_id == contract_id:
                return item
        raise KeyError(contract_id)


class Calculator:
    """Считает историческое состояние и распределение платежей."""

    def __init__(self, dataset: Dataset, as_of: date) -> None:
        self.dataset = dataset
        self.as_of = as_of
        # self.issues создаётся в _reset(), который вызывается ниже.
        self._works_by_contract: dict[str, list[Work]] = {}
        for work in sorted(dataset.works.values(), key=lambda w: natural_key(w.work_id)):
            self._works_by_contract.setdefault(work.contract_id, []).append(work)
        self._contract_order = sorted(
            dataset.contracts.values(), key=lambda c: natural_key(c.contract_id)
        )
        self._state: dict[str, WorkState] = {
            work.work_id: work_state_at(work, as_of)
            for work in dataset.works.values()
        }
        self._reset()

    # ------------------------------------------------------------------
    # Основной сценарий
    # ------------------------------------------------------------------

    def run(self) -> Report:
        self._reset()
        self._check_contract_dates()
        self._process_events()
        work_results = self._build_work_results()
        contract_results = self._build_contract_results(work_results)
        payment_results = self._build_payment_results()
        excluded = [
            payment
            for payment in sorted(
                self.dataset.payments.values(), key=lambda p: natural_key(p.payment_id)
            )
            if payment.created_at > self.as_of
        ]
        totals = self._build_totals(contract_results, work_results, excluded)
        self._verify_invariants(contract_results)
        return Report(
            as_of=self.as_of,
            works=work_results,
            contracts=contract_results,
            payments=payment_results,
            excluded_payments=excluded,
            ledger=list(self._ledger),
            issues=self.issues,
            totals=totals,
        )

    def _reset(self) -> None:
        # Замечания пересобираются с нуля: повторный run() не должен
        # дублировать предупреждения, добавленные при прошлом расчёте.
        self.issues: list[Issue] = list(self.dataset.issues)
        self._ledger: list[LedgerLine] = []
        self._work_allocated: dict[str, Decimal] = {
            work_id: ZERO for work_id in self.dataset.works
        }
        self._unallocated: dict[str, Decimal] = {
            contract_id: ZERO for contract_id in self.dataset.contracts
        }
        self._payment_lines: dict[str, list[LedgerLine]] = {}
        self._binding: dict[str, tuple[str, str | None]] = {}
        self._payment_state: dict[str, tuple[str, str]] = {}

    # ------------------------------------------------------------------
    # Проверки, зависящие от отчётной даты
    # ------------------------------------------------------------------

    def _check_contract_dates(self) -> None:
        for contract in self._contract_order:
            if contract.signed_at > self.as_of:
                self.issues.append(
                    Issue(
                        WARNING,
                        CONTRACTS_FILE,
                        contract.line_no,
                        "signed_at",
                        f"договор {contract.contract_id} подписан "
                        f"{contract.signed_at:%d.%m.%Y} - позже отчётной даты; "
                        "обязательства по нему в сводку не включены",
                    )
                )

    # ------------------------------------------------------------------
    # События
    # ------------------------------------------------------------------

    def _process_events(self) -> None:
        events: list[tuple[date, int, tuple, Payment]] = []
        for payment in self.dataset.payments.values():
            if payment.created_at > self.as_of:
                continue
            if payment.paid_at is not None and payment.paid_at <= self.as_of:
                events.append(
                    (
                        payment.paid_at,
                        _EVENT_PAYMENT,
                        natural_key(payment.payment_id),
                        payment,
                    )
                )
            if (
                payment.refunded_at is not None
                and payment.refunded_at <= self.as_of
                and payment.paid_at is not None
            ):
                events.append(
                    (
                        payment.refunded_at,
                        _EVENT_REFUND,
                        natural_key(payment.payment_id),
                        payment,
                    )
                )
        events.sort(key=lambda event: (event[0], event[1], event[2]))

        for _, event_type, _, payment in events:
            if event_type == _EVENT_PAYMENT:
                self._apply_payment(payment)
            else:
                self._apply_refund(payment)

    def _apply_payment(self, payment: Payment) -> None:
        if payment.paid_at is None:
            raise RuntimeError(
                "внутренняя ошибка расчёта: в поток событий попал платёж "
                f"{payment.payment_id} без даты оплаты"
            )
        binding_code, target = self._binding_for(payment)

        if target is not None:
            self._allocate_to_work(
                payment=payment,
                work_id=target,
                amount=payment.amount,
                kind=binding_code,
                reason="сумма отнесена на работу целиком",
            )
            return

        self._distribute_over_contract(payment)

    def _distribute_over_contract(self, payment: Payment) -> None:
        if payment.paid_at is None:
            raise RuntimeError(
                "внутренняя ошибка расчёта: распределение платежа "
                f"{payment.payment_id} без даты оплаты"
            )
        candidates: list[tuple[Work, Decimal]] = []
        for work in self._works_by_contract.get(payment.contract_id, []):
            if work.cancelled_at is not None and work.cancelled_at <= payment.paid_at:
                continue
            remaining = self._remaining(work.work_id)
            if remaining > ZERO:
                candidates.append((work, remaining))

        if not candidates:
            self._add_line(
                payment=payment,
                moment=payment.paid_at,
                work_id=None,
                kind="unallocated",
                amount=payment.amount,
                reason="в договоре нет работ с положительным остатком стоимости",
            )
            return

        weights = {work.work_id: remaining for work, remaining in candidates}
        total_remaining = sum(weights.values())
        to_distribute = min(payment.amount, total_remaining)
        shares, _ = split_amount(to_distribute, weights, dict(weights))

        for work_id in sorted(shares, key=natural_key):
            self._allocate_to_work(
                payment=payment,
                work_id=work_id,
                amount=shares[work_id],
                kind="proportional",
                reason="пропорционально остаткам стоимости работ договора",
            )

        # Неразобранным остаётся всё, что не поместилось в остатки работ:
        # и превышение суммы платежа над стоимостью работ, и копейки,
        # которые не удалось раздать из-за ограничения по остатку.
        rest = payment.amount - sum(shares.values())
        if rest > ZERO:
            self._add_line(
                payment=payment,
                moment=payment.paid_at,
                work_id=None,
                kind="unallocated",
                amount=rest,
                reason="сумма платежа больше остатка стоимости работ договора",
            )

    # ------------------------------------------------------------------
    # Привязка платежа к работе
    # ------------------------------------------------------------------

    def _binding_for(self, payment: Payment) -> tuple[str, str | None]:
        cached = self._binding.get(payment.payment_id)
        if cached is not None:
            return cached
        resolved = self._resolve_binding(payment)
        self._binding[payment.payment_id] = resolved
        return resolved

    def _resolve_binding(self, payment: Payment) -> tuple[str, str | None]:
        """Определить работу по work_id или по однозначному назначению."""
        if payment.work_id is not None:
            return DIRECT_WORK_ID, payment.work_id

        contract_works = self._works_by_contract.get(payment.contract_id, [])
        id_matches = [
            work.work_id
            for work in contract_works
            if contains_token(payment.purpose, work.work_id)
        ]

        foreign_ids = [
            work.work_id
            for work in self.dataset.works.values()
            if work.contract_id != payment.contract_id
            and contains_token(payment.purpose, work.work_id)
        ]
        if foreign_ids:
            self.issues.append(
                Issue(
                    WARNING,
                    PAYMENTS_FILE,
                    payment.line_no,
                    "purpose",
                    f"в назначении платежа {payment.payment_id} указан номер "
                    f"работы другого договора ({', '.join(sorted(foreign_ids))}); "
                    "привязка не выполнена",
                )
            )

        if len(id_matches) == 1:
            return PURPOSE_ID, id_matches[0]
        if len(id_matches) > 1:
            self.issues.append(
                Issue(
                    WARNING,
                    PAYMENTS_FILE,
                    payment.line_no,
                    "purpose",
                    f"в назначении платежа {payment.payment_id} найдено "
                    f"несколько работ ({', '.join(sorted(id_matches))}); "
                    "привязка неоднозначна, платёж распределён по договору",
                )
            )
            return CONTRACT_LEVEL, None

        title_matches = self._title_matches(payment.purpose, contract_works)
        if len(title_matches) == 1:
            return PURPOSE_TITLE, title_matches[0]
        if len(title_matches) > 1:
            self.issues.append(
                Issue(
                    WARNING,
                    PAYMENTS_FILE,
                    payment.line_no,
                    "purpose",
                    "в назначении платежа "
                    f"{payment.payment_id} совпало несколько названий работ "
                    f"({', '.join(sorted(title_matches))}); привязка "
                    "неоднозначна, платёж распределён по договору",
                )
            )
        return CONTRACT_LEVEL, None

    @staticmethod
    def _title_matches(purpose: str, works: list[Work]) -> list[str]:
        normalized_purpose = normalize_text(purpose)
        matched: list[str] = []
        for work in works:
            title = normalize_text(work.title)
            if len(title) < MIN_TITLE_MATCH_LENGTH:
                continue
            if title in normalized_purpose:
                matched.append(work.work_id)
        return matched

    # ------------------------------------------------------------------
    # Записи распределения
    # ------------------------------------------------------------------

    def _remaining(self, work_id: str) -> Decimal:
        work = self.dataset.works[work_id]
        return max(work.price - self._work_allocated[work_id], ZERO)

    def _allocate_to_work(
        self,
        *,
        payment: Payment,
        work_id: str,
        amount: Decimal,
        kind: str,
        reason: str,
    ) -> None:
        if payment.paid_at is None:
            raise RuntimeError(
                "внутренняя ошибка расчёта: распределение платежа "
                f"{payment.payment_id} без даты оплаты"
            )
        self._add_line(
            payment=payment,
            moment=payment.paid_at,
            work_id=work_id,
            kind=kind,
            amount=amount,
            reason=reason,
        )

    def _add_line(
        self,
        *,
        payment: Payment,
        moment: date,
        work_id: str | None,
        kind: str,
        amount: Decimal,
        reason: str,
        event: str = "payment",
    ) -> None:
        line = LedgerLine(
            event=event,
            date=moment,
            payment_id=payment.payment_id,
            contract_id=payment.contract_id,
            work_id=work_id,
            kind=kind,
            amount=amount,
            reason=reason,
        )
        self._ledger.append(line)
        self._payment_lines.setdefault(payment.payment_id, []).append(line)
        if work_id is None:
            self._unallocated[payment.contract_id] += amount
        else:
            self._work_allocated[work_id] += amount

    # ------------------------------------------------------------------
    # Возвраты
    # ------------------------------------------------------------------

    def _apply_refund(self, payment: Payment) -> None:
        """Снять с работ ровно те суммы, которые были туда отнесены."""
        if payment.refunded_at is None:
            raise RuntimeError(
                "внутренняя ошибка расчёта: в поток событий попал возврат "
                f"платежа {payment.payment_id} без даты возврата"
            )
        original = [
            line
            for line in self._payment_lines.get(payment.payment_id, [])
            if line.event == "payment"
        ]
        if not original:
            raise RuntimeError(
                "внутренняя ошибка расчёта: возврат платежа "
                f"{payment.payment_id} не имеет исходного распределения"
            )
        for line in original:
            self._add_line(
                payment=payment,
                moment=payment.refunded_at,
                work_id=line.work_id,
                kind="refund",
                amount=-line.amount,
                reason=(
                    "возврат снимает исходное распределение платежа "
                    f"{payment.payment_id}"
                ),
                event="refund",
            )

    # ------------------------------------------------------------------
    # Итоги по работам
    # ------------------------------------------------------------------

    def _build_work_results(self) -> list[WorkResult]:
        results: list[WorkResult] = []
        for contract in self._contract_order:
            for work in self._works_by_contract.get(contract.contract_id, []):
                state = self._state[work.work_id]
                allocated = self._work_allocated[work.work_id]
                overpayment = max(allocated - work.price, ZERO)
                remaining = (
                    ZERO if state.is_cancelled else max(work.price - allocated, ZERO)
                )
                approval = allocated if state.is_cancelled else overpayment
                results.append(
                    WorkResult(
                        work=work,
                        contract=contract,
                        status_at_report=work_status_label(work, state),
                        is_cancelled=state.is_cancelled,
                        is_completed=state.is_completed,
                        price=work.price,
                        allocated=allocated,
                        remaining=remaining,
                        overpayment=overpayment,
                        approval=approval,
                        notes=self._work_notes(work, state, allocated, overpayment),
                    )
                )
        return results

    def _work_notes(
        self,
        work: Work,
        state: WorkState,
        allocated: Decimal,
        overpayment: Decimal,
    ) -> list[str]:
        notes: list[str] = []
        cancellation = work.cancelled_at
        contract = self.dataset.contracts[work.contract_id]
        if contract.signed_at > self.as_of:
            notes.append(
                f"Договор подписан {format_date(contract.signed_at)} - позже "
                "отчётной даты, работа в сводку не включена"
            )
        if state.is_cancelled:
            if allocated > ZERO:
                notes.append(
                    f"Отменена {format_date(cancellation)}; выплачено "
                    f"{format_money(allocated)} - остаётся за работой и требует "
                    "согласования (возврат или зачёт)"
                )
            else:
                notes.append(
                    f"Отменена {format_date(cancellation)}; выплат по работе нет"
                )
        elif work.status == "cancelled" and cancellation is not None:
            notes.append(
                f"В выгрузке статус cancelled, отмена "
                f"{format_date(cancellation)} позже отчётной даты - на дату "
                "отчёта работа действует"
            )

        if (
            not state.is_cancelled
            and not state.is_completed
            and work.completed_at is not None
        ):
            notes.append(
                f"Приёмка {format_date(work.completed_at)} позже отчётной даты - "
                "на дату отчёта работа не завершена"
            )
        if (
            work.status == "done"
            and not state.is_completed
            and not state.is_cancelled
        ):
            notes.append(
                "В выгрузке статус done, но завершение на отчётную дату "
                "не подтверждено"
            )
        if work.status == "planned" and state.is_completed:
            notes.append(
                f"В выгрузке статус planned, но приёмка "
                f"{format_date(work.completed_at)} раньше отчётной даты - "
                "работа считается завершённой"
            )
        if overpayment > ZERO:
            notes.append(
                f"Переплата {format_money(overpayment)} сверх цены работы - "
                "на другие работы не переносится"
            )
        if state.is_cancelled and cancellation is not None:
            for line in self._payment_lines_of_work(work.work_id):
                if line.date >= cancellation and line.kind in (
                    DIRECT_WORK_ID,
                    PURPOSE_ID,
                    PURPOSE_TITLE,
                ):
                    notes.append(
                        f"Прямая выплата {format_money(line.amount)} от "
                        f"{format_date(line.date)} сделана уже после отмены - "
                        "требует согласования"
                    )
        return notes

    def _payment_lines_of_work(self, work_id: str) -> list[LedgerLine]:
        return [
            line
            for line in self._ledger
            if line.work_id == work_id and line.event == "payment"
        ]

    # ------------------------------------------------------------------
    # Итоги по договорам
    # ------------------------------------------------------------------

    def _build_contract_results(
        self, work_results: list[WorkResult]
    ) -> list[ContractResult]:
        by_contract: dict[str, list[WorkResult]] = {}
        for item in work_results:
            by_contract.setdefault(item.work.contract_id, []).append(item)

        results: list[ContractResult] = []
        for contract in self._contract_order:
            items = by_contract.get(contract.contract_id, [])
            payments = sorted(
                self.dataset.payments_of(contract.contract_id),
                key=lambda payment: natural_key(payment.payment_id),
            )
            existing = [
                payment for payment in payments if payment.created_at <= self.as_of
            ]
            paid_gross = sum(
                (
                    payment.amount
                    for payment in existing
                    if payment.paid_at is not None and payment.paid_at <= self.as_of
                ),
                ZERO,
            )
            refunded = sum(
                (
                    payment.amount
                    for payment in existing
                    if payment.refunded_at is not None
                    and payment.refunded_at <= self.as_of
                ),
                ZERO,
            )
            queued = [payment for payment in existing if payment.paid_at is None]
            pending = [
                payment
                for payment in existing
                if payment.paid_at is not None and payment.paid_at > self.as_of
            ]
            active = [item for item in items if not item.is_cancelled]
            cancelled = [item for item in items if item.is_cancelled]
            works_total = sum((item.price for item in items), ZERO)

            results.append(
                ContractResult(
                    contract=contract,
                    is_active=contract.signed_at <= self.as_of,
                    works_total=works_total,
                    active_works_total=sum((item.price for item in active), ZERO),
                    cancelled_works_total=sum((item.price for item in cancelled), ZERO),
                    paid_gross=paid_gross,
                    refunded=refunded,
                    paid_net=paid_gross - refunded,
                    allocated_active=sum((item.allocated for item in active), ZERO),
                    allocated_cancelled=sum(
                        (item.allocated for item in cancelled), ZERO
                    ),
                    unallocated=self._unallocated.get(contract.contract_id, ZERO),
                    remaining_obligations=sum((item.remaining for item in active), ZERO),
                    queued_total=sum((payment.amount for payment in queued), ZERO),
                    pending_total=sum((payment.amount for payment in pending), ZERO),
                    works_diff=contract.amount - works_total,
                    approval=sum((item.approval for item in items), ZERO),
                    overpayment=sum((item.overpayment for item in items), ZERO),
                    queued_payments=queued,
                    pending_payments=pending,
                )
            )
        return results

    # ------------------------------------------------------------------
    # Платежи
    # ------------------------------------------------------------------

    def _state_at_report(self, payment: Payment) -> tuple[str, str]:
        if payment.paid_at is None:
            return STATE_QUEUED, "в очереди, не исполнен"
        if payment.paid_at > self.as_of:
            return (
                STATE_PAID_LATER,
                f"оплачен {format_date(payment.paid_at)} - позже отчётной даты",
            )
        if payment.refunded_at is not None and payment.refunded_at <= self.as_of:
            return (
                STATE_REFUNDED,
                f"возвращён {format_date(payment.refunded_at)}",
            )
        if payment.refunded_at is not None:
            return (
                STATE_PAID,
                f"выплачен (возврат {format_date(payment.refunded_at)} - "
                "после отчётной даты)",
            )
        return STATE_PAID, "выплачен"

    def _build_payment_results(self) -> list[PaymentResult]:
        results: list[PaymentResult] = []
        payments = sorted(
            self.dataset.payments.values(), key=lambda p: natural_key(p.payment_id)
        )
        for payment in payments:
            if payment.created_at > self.as_of:
                continue
            state_code, state_text = self._state_at_report(payment)
            binding_code, target = self._binding_for(payment)
            lines = list(self._payment_lines.get(payment.payment_id, []))
            results.append(
                PaymentResult(
                    payment=payment,
                    state_code=state_code,
                    state=state_text,
                    binding_code=binding_code,
                    binding=BINDING_LABELS[binding_code],
                    target_work_id=target,
                    allocated=sum(
                        (line.amount for line in lines if line.work_id is not None),
                        ZERO,
                    ),
                    unallocated=sum(
                        (line.amount for line in lines if line.work_id is None),
                        ZERO,
                    ),
                    lines=lines,
                )
            )
        return results

    # ------------------------------------------------------------------
    # Сводка и самопроверка
    # ------------------------------------------------------------------

    def _build_totals(
        self,
        contract_results: list[ContractResult],
        work_results: list[WorkResult],
        excluded: list[Payment],
    ) -> Totals:
        active_contracts = [item for item in contract_results if item.is_active]
        inactive_ids = {
            item.contract.contract_id for item in contract_results if not item.is_active
        }
        active_works = [
            item
            for item in work_results
            if item.contract.contract_id not in inactive_ids
        ]
        works_total = sum((item.works_total for item in active_contracts), ZERO)
        cancelled_works_total = sum(
            (item.cancelled_works_total for item in active_contracts), ZERO
        )
        return Totals(
            as_of=self.as_of,
            contracts_count=len(active_contracts),
            inactive_contracts_count=len(contract_results) - len(active_contracts),
            works_count=len(active_works),
            inactive_works_count=len(work_results) - len(active_works),
            cancelled_works_count=sum(
                1 for item in active_works if item.is_cancelled
            ),
            works_total=works_total,
            active_works_total=works_total - cancelled_works_total,
            cancelled_works_total=cancelled_works_total,
            paid_gross=sum((item.paid_gross for item in active_contracts), ZERO),
            refunded=sum((item.refunded for item in active_contracts), ZERO),
            paid_net=sum((item.paid_net for item in active_contracts), ZERO),
            remaining_obligations=sum(
                (item.remaining_obligations for item in active_contracts), ZERO
            ),
            queued_total=sum((item.queued_total for item in active_contracts), ZERO),
            pending_total=sum((item.pending_total for item in active_contracts), ZERO),
            unallocated_total=sum(
                (item.unallocated for item in active_contracts), ZERO
            ),
            approval_total=sum((item.approval for item in active_contracts), ZERO),
            overpayment_total=sum(
                (item.overpayment for item in active_contracts), ZERO
            ),
            excluded_payments_count=len(excluded),
        )

    def _verify_invariants(self, contract_results: list[ContractResult]) -> None:
        """Проверить, что распределённые суммы сходятся с выплатами."""
        for result in contract_results:
            allocated = (
                result.allocated_active
                + result.allocated_cancelled
                + result.unallocated
            )
            if allocated != result.paid_net:
                raise RuntimeError(
                    "внутренняя ошибка расчёта: по договору "
                    f"{result.contract.contract_id} распределено {allocated}, "
                    f"а чисто выплачено {result.paid_net}"
                )
            if result.unallocated < ZERO:
                raise RuntimeError(
                    "внутренняя ошибка расчёта: отрицательная нераспределённая "
                    f"сумма по договору {result.contract.contract_id}"
                )
        for work_id, amount in self._work_allocated.items():
            if amount < ZERO:
                raise RuntimeError(
                    f"внутренняя ошибка расчёта: по работе {work_id} "
                    f"отрицательная сумма выплат {amount}"
                )