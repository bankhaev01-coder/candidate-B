"""Тесты чтения и валидации CSV."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from loader import (  # noqa: E402
    DataError,
    Issue,
    load_dataset,
    parse_cli_date,
    parse_date,
    parse_money,
)

DATA_DIR = PROJECT_ROOT / "data"

CONTRACTS_HEADER = "contract_id,contractor,amount,signed_at\n"
WORKS_HEADER = "work_id,contract_id,title,price,status,completed_at,cancelled_at\n"
PAYMENTS_HEADER = (
    "payment_id,contract_id,work_id,purpose,amount,status,created_at,paid_at,"
    "refunded_at\n"
)

VALID_CONTRACTS = CONTRACTS_HEADER + "D-1,ООО Тест,100.00,2026-01-01\n"
VALID_WORKS = WORKS_HEADER + "W-1,D-1,Работа,100.00,planned,,\n"
VALID_PAYMENTS = PAYMENTS_HEADER + "P-1,D-1,,Аванс,50.00,paid,2026-01-02,2026-01-03,\n"


class ValueParsingTest(unittest.TestCase):
    def test_parse_date_supports_both_formats(self) -> None:
        for raw in ("2026-11-10", "10.11.2026"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    parse_date(raw, source="works.csv", line=2, field_name="x"),
                    date(2026, 11, 10),
                )

    def test_optional_date_may_be_empty(self) -> None:
        self.assertIsNone(
            parse_date("", source="works.csv", line=2, field_name="paid_at", required=False)
        )
        self.assertIsNone(
            parse_date(None, source="works.csv", line=2, field_name="paid_at", required=False)
        )

    def test_required_date_error_mentions_file_line_field(self) -> None:
        with self.assertRaises(DataError) as ctx:
            parse_date("", source="contracts.csv", line=7, field_name="signed_at")
        message = str(ctx.exception)
        for part in ("contracts.csv", "строка 7", "signed_at", "обязательное"):
            self.assertIn(part, message)

    def test_invalid_date_error_mentions_value_and_formats(self) -> None:
        with self.assertRaises(DataError) as ctx:
            parse_date("31.02.2026", source="works.csv", line=3, field_name="completed_at")
        message = str(ctx.exception)
        self.assertIn("31.02.2026", message)
        self.assertIn("DD.MM.YYYY", message)

    def test_cli_date_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            parse_cli_date("10/11/2026")

    def test_money_is_decimal_without_float(self) -> None:
        self.assertEqual(
            parse_money("1234567.89", source="payments.csv", line=2, field_name="amount"),
            Decimal("1234567.89"),
        )
        self.assertIsInstance(
            parse_money("1.00", source="payments.csv", line=2, field_name="amount"),
            Decimal,
        )

    def test_money_accepts_comma_and_spaces(self) -> None:
        self.assertEqual(
            parse_money(" 1 000,50 ", source="payments.csv", line=2, field_name="amount"),
            Decimal("1000.50"),
        )

    def test_money_extra_precision_rounds_half_up_and_warns(self) -> None:
        issues: list[Issue] = []
        value = parse_money(
            "10.005", source="works.csv", line=3, field_name="price", issues=issues
        )
        self.assertEqual(value, Decimal("10.01"))
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].level, "warning")

    def test_money_negative_and_garbage_raise(self) -> None:
        for raw in ("-1.00", "12,3,4", "abc", ""):
            with self.subTest(raw=raw):
                with self.assertRaises(DataError):
                    parse_money(raw, source="works.csv", line=3, field_name="price")


class RealDataTest(unittest.TestCase):
    """Проверка на реальной выгрузке: структура, ссылки, предупреждения."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = load_dataset(DATA_DIR)

    def test_counts(self) -> None:
        self.assertEqual(len(self.dataset.contracts), 2)
        self.assertEqual(len(self.dataset.works), 9)
        self.assertEqual(len(self.dataset.payments), 8)

    def test_quoted_field_with_comma(self) -> None:
        self.assertEqual(
            self.dataset.contracts["D-2"].contractor, "Иван Петров, дизайнер"
        )

    def test_money_parsed_exactly(self) -> None:
        self.assertEqual(self.dataset.contracts["D-1"].amount, Decimal("600000.00"))
        self.assertEqual(self.dataset.payments["P-2"].amount, Decimal("50000.00"))

    def test_optional_dates_are_none_when_empty(self) -> None:
        self.assertIsNone(self.dataset.payments["P-4"].paid_at)
        self.assertIsNone(self.dataset.works["W-13"].completed_at)
        self.assertEqual(self.dataset.works["W-13"].cancelled_at, date(2026, 10, 12))

    def test_real_data_has_no_warnings(self) -> None:
        self.assertEqual(
            [issue.as_text() for issue in self.dataset.issues],
            [],
        )

    def test_purpose_with_comma_parsed(self) -> None:
        self.assertEqual(self.dataset.payments["P-2"].purpose, "Аванс, ошибочный платёж")


class ValidationTest(unittest.TestCase):
    """Фатальные ошибки и мягкие предупреждения на временных CSV."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._contracts = VALID_CONTRACTS
        self._works = VALID_WORKS
        self._payments = VALID_PAYMENTS

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, text: str) -> None:
        (self.dir / name).write_text(text, encoding="utf-8")

    def build(
        self,
        contracts: str = VALID_CONTRACTS,
        works: str = VALID_WORKS,
        payments: str = VALID_PAYMENTS,
    ):
        self.write("contracts.csv", contracts)
        self.write("works.csv", works)
        self.write("payments.csv", payments)
        return load_dataset(self.dir)

    def assert_error(self, *parts: str) -> None:
        with self.assertRaises(DataError) as ctx:
            self.build(
                contracts=self._contracts,
                works=self._works,
                payments=self._payments,
            )
        message = str(ctx.exception)
        for part in parts:
            self.assertIn(part, message)

    def test_valid_fixture_loads(self) -> None:
        dataset = self.build()
        self.assertEqual(len(dataset.contracts), 1)
        self.assertEqual(dataset.works["W-1"].price, Decimal("100.00"))

    def test_missing_file_is_reported(self) -> None:
        self.write("works.csv", VALID_WORKS)
        self.write("payments.csv", VALID_PAYMENTS)
        with self.assertRaises(DataError) as ctx:
            load_dataset(self.dir)
        self.assertIn("contracts.csv", str(ctx.exception))

    def test_missing_directory_is_reported(self) -> None:
        with self.assertRaises(DataError) as ctx:
            load_dataset(self.dir / "нет-такого-каталога")
        self.assertIn("не найден", str(ctx.exception))

    def test_missing_column_is_reported(self) -> None:
        self._works = (
            "work_id,contract_id,title,price,status\n"
            "W-1,D-1,Работа,100.00,planned\n"
        )
        self.assert_error("works.csv", "completed_at", "cancelled_at")

    def test_bad_date_reports_file_line_and_field(self) -> None:
        self._works = WORKS_HEADER + "W-1,D-1,Работа,100.00,planned,2026-13-40,\n"
        self.assert_error("works.csv", "строка 2", "completed_at")

    def test_bad_money_reports_field(self) -> None:
        self._payments = (
            PAYMENTS_HEADER + "P-1,D-1,,Аванс,сто,paid,2026-01-02,2026-01-03,\n"
        )
        self.assert_error("payments.csv", "строка 2", "amount")

    def test_negative_money_is_rejected(self) -> None:
        self._contracts = CONTRACTS_HEADER + "D-1,ООО Тест,-1.00,2026-01-01\n"
        self.assert_error("отрицательная сумма")

    def test_duplicate_ids_are_rejected(self) -> None:
        self._contracts = VALID_CONTRACTS + "D-1,Другой,10.00,2026-01-01\n"
        self.assert_error("D-1", "уже встречался")

    def test_unknown_contract_reference_is_rejected(self) -> None:
        self._works = WORKS_HEADER + "W-1,D-9,Работа,100.00,planned,,\n"
        self.assert_error("несуществующий договор", "D-9")

    def test_unknown_work_reference_is_rejected(self) -> None:
        self._payments = (
            PAYMENTS_HEADER
            + "P-1,D-1,W-99,Аванс,50.00,paid,2026-01-02,2026-01-03,\n"
        )
        self.assert_error("несуществующую", "W-99")

    def test_work_from_another_contract_is_rejected(self) -> None:
        self._contracts = VALID_CONTRACTS + "D-2,Второй,100.00,2026-01-01\n"
        self._works = VALID_WORKS + "W-2,D-2,Другая,100.00,planned,,\n"
        self._payments = (
            PAYMENTS_HEADER
            + "P-1,D-1,W-2,Аванс,50.00,paid,2026-01-02,2026-01-03,\n"
        )
        self.assert_error("D-1", "W-2", "D-2")

    def test_payment_paid_before_created_is_rejected(self) -> None:
        self._payments = (
            PAYMENTS_HEADER
            + "P-1,D-1,,Аванс,50.00,paid,2026-01-05,2026-01-03,\n"
        )
        self.assert_error("раньше, чем создан")

    def test_refund_before_payment_is_rejected(self) -> None:
        self._payments = (
            PAYMENTS_HEADER
            + "P-1,D-1,,Аванс,50.00,refunded,2026-01-02,2026-01-05,2026-01-03\n"
        )
        self.assert_error("раньше, чем оплачен")

    def test_paid_status_requires_paid_at(self) -> None:
        self._payments = (
            PAYMENTS_HEADER + "P-1,D-1,,Аванс,50.00,paid,2026-01-02,,\n"
        )
        self.assert_error("статус paid", "дата оплаты не заполнена")

    def test_refunded_status_requires_refunded_at(self) -> None:
        self._payments = (
            PAYMENTS_HEADER
            + "P-1,D-1,,Аванс,50.00,refunded,2026-01-02,2026-01-03,\n"
        )
        self.assert_error("дата возврата не заполнена")

    def test_unknown_status_is_rejected(self) -> None:
        self._works = WORKS_HEADER + "W-1,D-1,Работа,100.00,in_progress,,\n"
        self.assert_error("in_progress", "status")

    def test_empty_contracts_file_is_rejected(self) -> None:
        self._contracts = CONTRACTS_HEADER
        self.assert_error("ни одного договора")

    def test_done_without_completed_at_is_warning(self) -> None:
        dataset = self.build(works=WORKS_HEADER + "W-1,D-1,Работа,100.00,done,,\n")
        self.assertEqual(len(dataset.issues), 1)
        self.assertEqual(dataset.issues[0].level, "warning")
        self.assertIn("done", dataset.issues[0].message)
        self.assertEqual(dataset.issues[0].field, "completed_at")

    def test_queued_with_paid_at_is_warning(self) -> None:
        dataset = self.build(
            payments=PAYMENTS_HEADER
            + "P-1,D-1,,Аванс,50.00,queued,2026-01-02,2026-01-03,\n"
        )
        self.assertEqual(len(dataset.issues), 1)
        self.assertIn("queued", dataset.issues[0].message)

    def test_payment_before_contract_signing_is_warning(self) -> None:
        dataset = self.build(
            contracts=CONTRACTS_HEADER + "D-1,ООО Тест,100.00,2026-02-01\n"
        )
        self.assertEqual(len(dataset.issues), 1)
        self.assertIn("раньше подписания", dataset.issues[0].message)

    def test_blank_row_is_skipped_with_warning(self) -> None:
        dataset = self.build(payments=VALID_PAYMENTS + ",,,,,,,,\n")
        self.assertEqual(len(dataset.payments), 1)
        self.assertEqual(len(dataset.issues), 1)
        self.assertIn("пустая строка", dataset.issues[0].message)

    def test_extra_values_in_row_are_rejected(self) -> None:
        self._payments = (
            PAYMENTS_HEADER
            + "P-1,D-1,,Аванс,50.00,paid,2026-01-02,2026-01-03,,лишнее\n"
        )
        self.assert_error("больше значений")


if __name__ == "__main__":
    unittest.main()