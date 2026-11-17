"""Тесты расчёта: выплаты, работы, распределение и итоги."""

from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from calculator import Calculator, natural_key, split_amount  # noqa: E402
from loader import Contract, Dataset, Payment, Work  # noqa: E402

AS_OF = date(2026, 3, 1)
D = Decimal


def contract(
    contract_id: str = "D-1",
    amount: str = "1000.00",
    signed_at: date = date(2026, 1, 1),
    contractor: str = "ООО Тест",
) -> Contract:
    return Contract(contract_id, contractor, Decimal(amount), signed_at, 2)


def work(
    work_id: str = "W-1",
    contract_id: str = "D-1",
    price: str = "100.00",
    status: str = "planned",
    completed_at: date | None = None,
    cancelled_at: date | None = None,
    title: str | None = None,
) -> Work:
    return Work(
        work_id,
        contract_id,
        title or f"Работа {work_id}",
        Decimal(price),
        status,
        completed_at,
        cancelled_at,
        2,
    )


def payment(
    payment_id: str = "P-1",
    contract_id: str = "D-1",
    amount: str = "100.00",
    status: str = "paid",
    created_at: date = date(2026, 1, 2),
    paid_at: date | None = date(2026, 1, 3),
    refunded_at: date | None = None,
    work_id: str | None = None,
    purpose: str = "",
) -> Payment:
    return Payment(
        payment_id,
        contract_id,
        work_id,
        purpose,
        Decimal(amount),
        status,
        created_at,
        paid_at,
        refunded_at,
        2,
    )


def dataset(contracts=(), works=(), payments=()) -> Dataset:
    return Dataset(
        contracts={item.contract_id: item for item in contracts},
        works={item.work_id: item for item in works},
        payments={item.payment_id: item for item in payments},
    )


def calc(data: Dataset, as_of: date = AS_OF):
    return Calculator(data, as_of).run()


def by_work(report, work_id: str):
    return next(item for item in report.works if item.work.work_id == work_id)


class AllocationTest(unittest.TestCase):
    def test_contract_payment_split_by_price(self) -> None:
        report = calc(
            dataset(
                [contract(amount="300.00")],
                [work("W-1", price="100.00"), work("W-2", price="200.00")],
                [payment(amount="150.00")],
            )
        )
        self.assertEqual(by_work(report, "W-1").paid, D("50.00"))
        self.assertEqual(by_work(report, "W-2").paid, D("100.00"))
        self.assertEqual(report.totals.unallocated_total, D("0.00"))

    def test_remainder_kopecks_follow_work_id(self) -> None:
        report = calc(
            dataset(
                [contract(amount="300.00")],
                [work("W-1"), work("W-2"), work("W-3")],
                [payment(amount="100.00")],
            )
        )
        self.assertEqual(by_work(report, "W-1").paid, D("33.34"))
        self.assertEqual(by_work(report, "W-2").paid, D("33.33"))
        self.assertEqual(by_work(report, "W-3").paid, D("33.33"))
        self.assertEqual(sum(item.paid for item in report.works), D("100.00"))

    def test_direct_payment_uses_only_its_work(self) -> None:
        report = calc(
            dataset(
                [contract(amount="200.00")],
                [work("W-1"), work("W-2")],
                [payment(amount="150.00", work_id="W-2")],
            )
        )
        self.assertEqual(by_work(report, "W-1").paid, D("0.00"))
        self.assertEqual(by_work(report, "W-2").paid, D("150.00"))
        self.assertEqual(by_work(report, "W-2").overpayment, D("50.00"))
        self.assertEqual(by_work(report, "W-2").remaining, D("0.00"))

    def test_purpose_with_work_id_binds_to_work(self) -> None:
        report = calc(
            dataset(
                [contract(amount="200.00")],
                [work("W-1"), work("W-2")],
                [payment(amount="30.00", purpose="Оплата по работе w-2")],
            )
        )
        self.assertEqual(by_work(report, "W-2").paid, D("30.00"))
        self.assertEqual(report.payments[0].binding_code, "purpose_id")
        self.assertEqual(report.payments[0].target_work_id, "W-2")

    def test_purpose_with_unique_title_binds_to_work(self) -> None:
        report = calc(
            dataset(
                [contract(amount="200.00")],
                [work("W-1", title="Логотип"), work("W-2", title="Упаковка")],
                [payment(amount="20.00", purpose="Оплата Логотип")],
            )
        )
        self.assertEqual(by_work(report, "W-1").paid, D("20.00"))
        self.assertEqual(report.payments[0].binding_code, "purpose_title")

    def test_two_work_ids_in_purpose_are_ambiguous(self) -> None:
        report = calc(
            dataset(
                [contract(amount="200.00")],
                [work("W-1"), work("W-2")],
                [payment(amount="100.00", purpose="Оплата W-1 и W-2")],
            )
        )
        self.assertEqual(by_work(report, "W-1").paid, D("50.00"))
        self.assertEqual(report.payments[0].binding_code, "contract")
        self.assertTrue(
            any("несколько работ" in issue.message for issue in report.issues)
        )

    def test_short_title_is_not_used_for_binding(self) -> None:
        report = calc(
            dataset(
                [contract(amount="200.00")],
                [work("W-1", title="Да"), work("W-2", title="Нет")],
                [payment(amount="100.00", purpose="Оплата Да")],
            )
        )
        self.assertEqual(report.payments[0].binding_code, "contract")
        self.assertEqual(by_work(report, "W-1").paid, D("50.00"))

    def test_work_id_of_foreign_contract_is_reported(self) -> None:
        report = calc(
            dataset(
                [contract("D-1", amount="100.00"), contract("D-2", amount="100.00")],
                [work("W-1"), work("W-2", contract_id="D-2")],
                [payment(amount="50.00", purpose="Оплата W-2")],
            )
        )
        self.assertEqual(report.payments[0].binding_code, "contract")
        self.assertTrue(
            any("другого договора" in issue.message for issue in report.issues)
        )

    def test_payment_above_work_prices_is_partly_unallocated(self) -> None:
        report = calc(
            dataset(
                [contract(amount="200.00")],
                [work("W-1"), work("W-2")],
                [payment(amount="500.00")],
            )
        )
        contract_result = report.contracts[0]
        self.assertEqual(contract_result.allocated_active, D("200.00"))
        self.assertEqual(contract_result.unallocated, D("300.00"))
        self.assertEqual(contract_result.remaining_obligations, D("0.00"))
        self.assertEqual(report.totals.paid_net, D("500.00"))

    def test_payment_without_eligible_works_is_unallocated(self) -> None:
        report = calc(
            dataset(
                [contract(amount="100.00")],
                [work("W-1", status="cancelled", cancelled_at=date(2026, 1, 10))],
                [payment(amount="100.00", paid_at=date(2026, 2, 1))],
            )
        )
        contract_result = report.contracts[0]
        self.assertEqual(contract_result.unallocated, D("100.00"))
        self.assertEqual(contract_result.allocated_cancelled, D("0.00"))
        self.assertEqual(report.totals.unallocated_total, D("100.00"))


class SplitAmountTest(unittest.TestCase):
    def test_shares_never_exceed_remaining(self) -> None:
        weights = {"W-1": D("0.01"), "W-2": D("100.00")}
        shares, leftover = split_amount(D("50.00"), weights, dict(weights))
        self.assertEqual(leftover, D("0.00"))
        self.assertEqual(sum(shares.values()), D("50.00"))
        for work_id, share in shares.items():
            self.assertLessEqual(share, weights[work_id])
        self.assertEqual(shares["W-2"], D("50.00"))

    def test_three_equal_weights_sum_exactly(self) -> None:
        weights = {"W-1": D("100.00"), "W-2": D("100.00"), "W-3": D("100.00")}
        shares, leftover = split_amount(D("100.00"), weights, dict(weights))
        self.assertEqual(leftover, D("0.00"))
        self.assertEqual(sum(shares.values()), D("100.00"))
        self.assertEqual(
            sorted(shares.values()),
            [D("33.33"), D("33.33"), D("33.34")],
        )

    def test_zero_amount_gives_empty_shares(self) -> None:
        weights = {"W-1": D("1.00")}
        shares, leftover = split_amount(D("0.00"), weights, dict(weights))
        self.assertEqual(shares, {})
        self.assertEqual(leftover, D("0.00"))

    def test_natural_key_orders_numbers(self) -> None:
        self.assertLess(natural_key("W-2"), natural_key("W-10"))


class TimelineTest(unittest.TestCase):
    def test_payment_after_report_date_not_counted(self) -> None:
        report = calc(
            dataset(
                [contract(amount="100.00")],
                [work("W-1")],
                [payment(amount="100.00", paid_at=date(2026, 3, 5))],
            )
        )
        self.assertEqual(by_work(report, "W-1").paid, D("0.00"))
        self.assertEqual(report.totals.paid_net, D("0.00"))
        self.assertEqual(report.totals.pending_total, D("100.00"))
        self.assertEqual(report.payments[0].state_code, "paid_later")

    def test_payment_created_after_report_date_is_excluded(self) -> None:
        report = calc(
            dataset(
                [contract(amount="100.00")],
                [work("W-1")],
                [
                    payment(
                        "P-9",
                        amount="100.00",
                        created_at=date(2026, 3, 5),
                        paid_at=date(2026, 3, 6),
                    )
                ],
            )
        )
        self.assertEqual(report.totals.paid_net, D("0.00"))
        self.assertEqual(report.totals.excluded_payments_count, 1)
        self.assertEqual(
            [item.payment_id for item in report.excluded_payments], ["P-9"]
        )
        self.assertEqual(report.payments, [])

    def test_queued_payment_does_not_reduce_remaining(self) -> None:
        report = calc(
            dataset(
                [contract(amount="100.00")],
                [work("W-1")],
                [
                    payment(
                        amount="100.00",
                        status="queued",
                        created_at=date(2026, 2, 1),
                        paid_at=None,
                    )
                ],
            )
        )
        self.assertEqual(by_work(report, "W-1").remaining, D("100.00"))
        self.assertEqual(report.totals.queued_total, D("100.00"))
        self.assertEqual(report.payments[0].state_code, "queued")
        self.assertEqual(report.contracts[0].queued_total, D("100.00"))

    def test_refund_before_report_date_returns_money(self) -> None:
        report = calc(
            dataset(
                [contract(amount="100.00")],
                [work("W-1")],
                [
                    payment(
                        amount="100.00",
                        status="refunded",
                        refunded_at=date(2026, 1, 20),
                    )
                ],
            )
        )
        self.assertEqual(by_work(report, "W-1").paid, D("0.00"))
        self.assertEqual(report.totals.paid_gross, D("100.00"))
        self.assertEqual(report.totals.refunded, D("100.00"))
        self.assertEqual(report.totals.paid_net, D("0.00"))
        self.assertEqual(report.payments[0].state_code, "refunded")

    def test_refund_after_report_date_keeps_payment(self) -> None:
        report = calc(
            dataset(
                [contract(amount="100.00")],
                [work("W-1")],
                [
                    payment(
                        amount="100.00",
                        status="refunded",
                        refunded_at=date(2026, 3, 10),
                    )
                ],
            )
        )
        self.assertEqual(by_work(report, "W-1").paid, D("100.00"))
        self.assertEqual(report.totals.refunded, D("0.00"))
        self.assertEqual(report.payments[0].state_code, "paid")

    def test_refund_restores_original_shares(self) -> None:
        report = calc(
            dataset(
                [contract(amount="200.00")],
                [work("W-1"), work("W-2")],
                [
                    payment(
                        "P-1",
                        amount="100.00",
                        paid_at=date(2026, 1, 5),
                        status="refunded",
                        refunded_at=date(2026, 2, 1),
                    ),
                    payment(
                        "P-2",
                        amount="100.00",
                        paid_at=date(2026, 1, 20),
                        work_id="W-2",
                    ),
                ],
            )
        )
        self.assertEqual(by_work(report, "W-1").paid, D("0.00"))
        self.assertEqual(by_work(report, "W-2").paid, D("100.00"))
        self.assertEqual(report.totals.paid_net, D("100.00"))

    def test_refund_does_not_redistribute_later_payments(self) -> None:
        report = calc(
            dataset(
                [contract(amount="200.00")],
                [work("W-1"), work("W-2")],
                [
                    payment(
                        "P-1",
                        amount="100.00",
                        paid_at=date(2026, 1, 5),
                        status="refunded",
                        refunded_at=date(2026, 2, 1),
                    ),
                    payment("P-2", amount="50.00", paid_at=date(2026, 1, 10)),
                ],
            )
        )
        self.assertEqual(by_work(report, "W-1").paid, D("25.00"))
        self.assertEqual(by_work(report, "W-2").paid, D("25.00"))
        second = next(
            item for item in report.payments if item.payment.payment_id == "P-2"
        )
        self.assertEqual(
            second.net_by_work(), {"W-1": D("25.00"), "W-2": D("25.00")}
        )

    def test_refund_after_cancellation_removes_paid_amount(self) -> None:
        report = calc(
            dataset(
                [contract(amount="200.00")],
                [
                    work("W-1", status="cancelled", cancelled_at=date(2026, 2, 1)),
                    work("W-2"),
                ],
                [
                    payment(
                        "P-1",
                        amount="100.00",
                        paid_at=date(2026, 1, 5),
                        status="refunded",
                        refunded_at=date(2026, 2, 10),
                    )
                ],
            )
        )
        self.assertEqual(by_work(report, "W-1").paid, D("0.00"))
        self.assertEqual(report.contracts[0].allocated_cancelled, D("0.00"))
        self.assertEqual(report.totals.approval_total, D("0.00"))
        self.assertEqual(report.totals.paid_net, D("0.00"))

    def test_same_day_payments_follow_payment_id_order(self) -> None:
        report = calc(
            dataset(
                [contract(amount="300.00")],
                [work("W-1", price="100.00"), work("W-2", price="200.00")],
                [
                    payment(
                        "P-1",
                        amount="100.00",
                        paid_at=date(2026, 1, 5),
                        work_id="W-1",
                    ),
                    payment("P-2", amount="100.00", paid_at=date(2026, 1, 5)),
                ],
            )
        )
        self.assertEqual(by_work(report, "W-1").paid, D("100.00"))
        self.assertEqual(by_work(report, "W-2").paid, D("100.00"))
        self.assertEqual(by_work(report, "W-1").overpayment, D("0.00"))


class WorkStateTest(unittest.TestCase):
    def test_completed_before_report_date(self) -> None:
        report = calc(
            dataset(
                [contract()],
                [work("W-1", status="done", completed_at=date(2026, 2, 1))],
                [],
            )
        )
        item = by_work(report, "W-1")
        self.assertTrue(item.is_completed)
        self.assertEqual(item.status_at_report, "завершена 01.02.2026")

    def test_completion_after_report_date_is_not_counted(self) -> None:
        report = calc(
            dataset(
                [contract()],
                [work("W-1", status="done", completed_at=date(2026, 3, 5))],
                [],
            )
        )
        item = by_work(report, "W-1")
        self.assertFalse(item.is_completed)
        self.assertIn("не завершена", item.status_at_report)
        self.assertEqual(item.remaining, D("100.00"))
        self.assertTrue(
            any("позже отчётной даты" in note for note in item.notes)
        )

    def test_cancelled_work_has_no_obligation(self) -> None:
        report = calc(
            dataset(
                [contract(amount="100.00")],
                [work("W-1", status="cancelled", cancelled_at=date(2026, 2, 1))],
                [payment(amount="100.00", paid_at=date(2026, 1, 5))],
            )
        )
        item = by_work(report, "W-1")
        self.assertTrue(item.is_cancelled)
        self.assertEqual(item.remaining, D("0.00"))
        self.assertEqual(item.approval, D("100.00"))
        contract_result = report.contracts[0]
        self.assertEqual(contract_result.cancelled_works_total, D("100.00"))
        self.assertEqual(contract_result.allocated_cancelled, D("100.00"))
        self.assertEqual(contract_result.remaining_obligations, D("0.00"))
        self.assertEqual(contract_result.active_works_total, D("0.00"))

    def test_cancellation_after_report_date_keeps_work_active(self) -> None:
        report = calc(
            dataset(
                [contract(amount="100.00")],
                [work("W-1", status="cancelled", cancelled_at=date(2026, 3, 10))],
                [],
            )
        )
        item = by_work(report, "W-1")
        self.assertFalse(item.is_cancelled)
        self.assertEqual(item.remaining, D("100.00"))
        self.assertEqual(report.contracts[0].cancelled_works_total, D("0.00"))
        self.assertTrue(
            any("позже отчётной даты" in note for note in item.notes)
        )

    def test_payment_before_cancellation_stays_on_work(self) -> None:
        report = calc(
            dataset(
                [contract(amount="200.00")],
                [
                    work("W-1", status="cancelled", cancelled_at=date(2026, 2, 1)),
                    work("W-2"),
                ],
                [payment(amount="100.00", paid_at=date(2026, 1, 5))],
            )
        )
        self.assertEqual(by_work(report, "W-1").paid, D("50.00"))
        self.assertEqual(by_work(report, "W-2").paid, D("50.00"))
        contract_result = report.contracts[0]
        self.assertEqual(contract_result.allocated_cancelled, D("50.00"))
        self.assertEqual(contract_result.remaining_obligations, D("50.00"))
        self.assertEqual(contract_result.approval, D("50.00"))

    def test_direct_payment_after_cancellation_is_flagged(self) -> None:
        report = calc(
            dataset(
                [contract(amount="100.00")],
                [work("W-1", status="cancelled", cancelled_at=date(2026, 1, 10))],
                [payment(amount="100.00", paid_at=date(2026, 2, 1), work_id="W-1")],
            )
        )
        item = by_work(report, "W-1")
        self.assertEqual(item.paid, D("100.00"))
        self.assertEqual(item.approval, D("100.00"))
        self.assertTrue(any("после отмены" in note for note in item.notes))


class TotalsTest(unittest.TestCase):
    def test_contract_totals_and_invariants(self) -> None:
        report = calc(
            dataset(
                [contract("D-1", amount="300.00"), contract("D-2", amount="100.00")],
                [
                    work("W-1", "D-1", price="100.00"),
                    work("W-2", "D-1", price="200.00"),
                    work("W-3", "D-2", price="100.00"),
                ],
                [
                    payment("P-1", "D-1", amount="150.00", paid_at=date(2026, 1, 5)),
                    payment(
                        "P-2",
                        "D-1",
                        amount="50.00",
                        status="queued",
                        created_at=date(2026, 2, 1),
                        paid_at=None,
                    ),
                    payment(
                        "P-3",
                        "D-2",
                        amount="100.00",
                        paid_at=date(2026, 1, 6),
                        work_id="W-3",
                    ),
                ],
            )
        )
        first = report.contract("D-1")
        self.assertEqual(first.works_total, D("300.00"))
        self.assertEqual(first.paid_gross, D("150.00"))
        self.assertEqual(first.paid_net, D("150.00"))
        self.assertEqual(first.allocated_active, D("150.00"))
        self.assertEqual(first.remaining_obligations, D("150.00"))
        self.assertEqual(first.queued_total, D("50.00"))
        self.assertEqual(first.works_diff, D("0.00"))
        second = report.contract("D-2")
        self.assertEqual(second.remaining_obligations, D("0.00"))
        self.assertEqual(report.totals.paid_net, D("250.00"))
        self.assertEqual(report.totals.remaining_obligations, D("150.00"))
        self.assertEqual(report.totals.queued_total, D("50.00"))
        self.assertEqual(report.totals.unallocated_total, D("0.00"))

    def test_works_diff_is_reported(self) -> None:
        report = calc(
            dataset([contract(amount="500.00")], [work("W-1", price="300.00")], [])
        )
        contract_result = report.contracts[0]
        self.assertEqual(contract_result.works_diff, D("200.00"))
        self.assertEqual(report.totals.works_total, D("300.00"))

    def test_overpayment_is_not_moved_to_other_work(self) -> None:
        report = calc(
            dataset(
                [contract(amount="300.00")],
                [work("W-1", price="100.00"), work("W-2", price="200.00")],
                [
                    payment(
                        "P-1",
                        amount="150.00",
                        paid_at=date(2026, 1, 5),
                        work_id="W-1",
                    ),
                    payment("P-2", amount="100.00", paid_at=date(2026, 1, 10)),
                ],
            )
        )
        self.assertEqual(by_work(report, "W-1").paid, D("150.00"))
        self.assertEqual(by_work(report, "W-1").overpayment, D("50.00"))
        self.assertEqual(by_work(report, "W-2").paid, D("100.00"))
        self.assertEqual(report.totals.paid_net, D("250.00"))
        self.assertEqual(report.totals.unallocated_total, D("0.00"))


class ContractStateTest(unittest.TestCase):
    def test_contract_signed_after_report_date_is_excluded_from_totals(self) -> None:
        report = calc(
            dataset(
                [contract("D-2", amount="100.00", signed_at=date(2026, 4, 1))],
                [work("W-3", "D-2", price="100.00")],
                [],
            )
        )
        self.assertEqual(report.totals.contracts_count, 0)
        self.assertEqual(report.totals.works_count, 0)
        self.assertEqual(report.totals.works_total, D("0.00"))
        self.assertEqual(report.totals.remaining_obligations, D("0.00"))
        self.assertEqual(report.totals.inactive_contracts_count, 1)
        self.assertEqual(report.totals.inactive_works_count, 1)
        self.assertFalse(report.contracts[0].is_active)
        self.assertTrue(
            any(
                "позже отчётной даты" in issue.message for issue in report.issues
            )
        )
        self.assertTrue(
            any("в сводку не включена" in note for note in report.works[0].notes)
        )

    def test_repeated_run_does_not_duplicate_issues(self) -> None:
        calculator = Calculator(
            dataset(
                [contract("D-2", amount="100.00", signed_at=date(2026, 4, 1))],
                [work("W-3", "D-2", price="100.00")],
                [],
            ),
            AS_OF,
        )
        first = calculator.run()
        second = calculator.run()
        self.assertEqual(
            [issue.message for issue in first.issues],
            [issue.message for issue in second.issues],
        )
        self.assertEqual(first.totals, second.totals)
        self.assertEqual(first.ledger, second.ledger)


class InconsistentDatasetTest(unittest.TestCase):
    def test_refund_without_applied_payment_is_reported(self) -> None:
        data = dataset(
            [contract(amount="100.00")],
            [work("W-1", price="100.00")],
            [
                payment(
                    "P-1",
                    amount="50.00",
                    status="refunded",
                    created_at=date(2026, 1, 1),
                    paid_at=date(2026, 3, 10),
                    refunded_at=date(2026, 2, 1),
                )
            ],
        )
        with self.assertRaises(RuntimeError) as ctx:
            calc(data)
        self.assertIn("не имеет исходного распределения", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()