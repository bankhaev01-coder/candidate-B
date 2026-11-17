"""Тесты отчётов: содержимое, CSV, детерминизм и CLI."""

from __future__ import annotations

import csv
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from calculator import Calculator  # noqa: E402
from loader import load_dataset  # noqa: E402
from reporter import (  # noqa: E402
    CONTRACTS_CSV,
    ISSUES_CSV,
    LEDGER_CSV,
    PAYMENTS_CSV,
    REPORT_MD,
    WORKS_CSV,
    build_report_markdown,
    summary_lines,
    write_all,
)

DATA_DIR = PROJECT_ROOT / "data"
AS_OF = date(2026, 11, 10)
D = Decimal
MONEY_CELL = re.compile(r"^-?\d+\.\d{2}$")


def build_report(as_of: date = AS_OF):
    return Calculator(load_dataset(DATA_DIR), as_of).run()


def works_map(report):
    return {item.work.work_id: item for item in report.works}


class RealDataReportTest(unittest.TestCase):
    """Расчёт на реальной выгрузке и арифметические инварианты."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.report = build_report()
        cls.works = works_map(cls.report)

    def test_summary_totals(self) -> None:
        totals = self.report.totals
        self.assertEqual(totals.contracts_count, 2)
        self.assertEqual(totals.works_count, 9)
        self.assertEqual(totals.cancelled_works_count, 1)
        self.assertEqual(totals.works_total, D("690000.00"))
        self.assertEqual(totals.active_works_total, D("590000.00"))
        self.assertEqual(totals.cancelled_works_total, D("100000.00"))
        self.assertEqual(totals.paid_gross, D("440000.00"))
        self.assertEqual(totals.refunded, D("50000.00"))
        self.assertEqual(totals.paid_net, D("390000.00"))
        self.assertEqual(totals.remaining_obligations, D("250000.00"))
        self.assertEqual(totals.queued_total, D("150000.00"))
        self.assertEqual(totals.pending_total, D("0.00"))
        self.assertEqual(totals.unallocated_total, D("0.00"))
        self.assertEqual(totals.approval_total, D("50000.00"))
        self.assertEqual(totals.overpayment_total, D("0.00"))
        self.assertEqual(totals.excluded_payments_count, 1)

    def test_work_payments(self) -> None:
        expected = {
            "W-11": ("50000.00", "50000.00"),
            "W-12": ("50000.00", "50000.00"),
            "W-13": ("50000.00", "0.00"),
            "W-14": ("65000.00", "35000.00"),
            "W-15": ("50000.00", "50000.00"),
            "W-16": ("50000.00", "50000.00"),
            "W-21": ("16666.67", "3333.33"),
            "W-22": ("25000.00", "5000.00"),
            "W-23": ("33333.33", "6666.67"),
        }
        for work_id, (paid, remaining) in expected.items():
            with self.subTest(work_id=work_id):
                self.assertEqual(self.works[work_id].paid, D(paid))
                self.assertEqual(self.works[work_id].remaining, D(remaining))

    def test_cancelled_work_keeps_payment_and_needs_approval(self) -> None:
        item = self.works["W-13"]
        self.assertTrue(item.is_cancelled)
        self.assertTrue(item.is_completed is False)
        self.assertEqual(item.approval, D("50000.00"))
        self.assertTrue(item.notes)

    def test_work_accepted_after_report_date_is_not_completed(self) -> None:
        item = self.works["W-23"]
        self.assertFalse(item.is_completed)
        self.assertIn("не завершена", item.status_at_report)

    def test_contract_totals(self) -> None:
        first = self.report.contract("D-1")
        self.assertEqual(first.works_total, D("600000.00"))
        self.assertEqual(first.active_works_total, D("500000.00"))
        self.assertEqual(first.cancelled_works_total, D("100000.00"))
        self.assertEqual(first.paid_gross, D("365000.00"))
        self.assertEqual(first.refunded, D("50000.00"))
        self.assertEqual(first.paid_net, D("315000.00"))
        self.assertEqual(first.allocated_active, D("265000.00"))
        self.assertEqual(first.allocated_cancelled, D("50000.00"))
        self.assertEqual(first.unallocated, D("0.00"))
        self.assertEqual(first.remaining_obligations, D("235000.00"))
        self.assertEqual(first.queued_total, D("150000.00"))
        self.assertEqual(first.works_diff, D("0.00"))

        second = self.report.contract("D-2")
        self.assertEqual(second.paid_net, D("75000.00"))
        self.assertEqual(second.allocated_active, D("75000.00"))
        self.assertEqual(second.remaining_obligations, D("15000.00"))
        self.assertEqual(second.works_diff, D("0.00"))

    def test_contract_level_invariants(self) -> None:
        for contract in self.report.contracts:
            with self.subTest(contract=contract.contract.contract_id):
                self.assertEqual(
                    contract.allocated_active
                    + contract.allocated_cancelled
                    + contract.unallocated,
                    contract.paid_net,
                )
                self.assertEqual(
                    contract.remaining_obligations,
                    contract.active_works_total - contract.allocated_active,
                )
                active = [
                    item
                    for item in self.report.works_of(contract.contract.contract_id)
                    if not item.is_cancelled
                ]
                self.assertEqual(
                    sum((item.remaining for item in active), D("0.00")),
                    contract.remaining_obligations,
                )

    def test_payment_level_invariants(self) -> None:
        for item in self.report.payments:
            with self.subTest(payment=item.payment.payment_id):
                net = sum((line.amount for line in item.lines), D("0.00"))
                if item.state_code in ("queued", "paid_later"):
                    self.assertEqual(net, D("0.00"))
                    self.assertEqual(item.allocated, D("0.00"))
                elif item.state_code == "refunded":
                    self.assertEqual(net, D("0.00"))
                    self.assertIsNotNone(item.payment.refunded_at)
                    self.assertLessEqual(item.payment.refunded_at, AS_OF)
                else:
                    self.assertEqual(net, item.payment.amount)
                    self.assertEqual(
                        item.allocated + item.unallocated, item.payment.amount
                    )

    def test_refund_reverses_same_works(self) -> None:
        item = next(
            item for item in self.report.payments if item.payment.payment_id == "P-2"
        )
        self.assertEqual(item.state_code, "refunded")
        paid_lines = [line for line in item.lines if line.event == "payment"]
        refund_lines = [line for line in item.lines if line.event == "refund"]
        self.assertEqual(len(paid_lines), 6)
        self.assertEqual(len(paid_lines), len(refund_lines))
        self.assertEqual(
            {line.work_id: line.amount for line in paid_lines},
            {line.work_id: -line.amount for line in refund_lines},
        )

    def test_ledger_totals_match_contract_paid(self) -> None:
        for contract in self.report.contracts:
            contract_id = contract.contract.contract_id
            lines = [
                line
                for line in self.report.ledger
                if line.contract_id == contract_id
            ]
            self.assertEqual(
                sum((line.amount for line in lines), D("0.00")), contract.paid_net
            )

    def test_markdown_has_required_sections(self) -> None:
        text = build_report_markdown(self.report)
        for header in (
            "# Отчёт о выплатах по договорам",
            "## 1. Сводка",
            "## 2. Работы",
            "## 3. Договоры",
            "## 4. Платежи и распределение",
            "## 5. Требует согласования",
            "## 6. Наблюдения и проблемы данных",
        ):
            self.assertIn(header, text)
        self.assertIn("### D-1 — Студия «Свет»", text)
        self.assertIn("### D-2 — Иван Петров, дизайнер", text)
        self.assertIn("W-13 — Съёмка: аксессуары", text)
        self.assertIn("390 000.00", text)
        self.assertIn("235 000.00", text)

    def test_summary_lines(self) -> None:
        lines = summary_lines(self.report)
        self.assertTrue(lines[0].startswith("Отчёт на 10.11.2026"))
        joined = "\n".join(lines)
        self.assertIn("390 000.00", joined)
        self.assertIn("250 000.00", joined)
        self.assertIn("150 000.00", joined)
        self.assertIn("50 000.00", joined)


class CsvOutputTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def read_csv(self, name: str) -> list[dict[str, str]]:
        with (self.out / name).open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_all_files_are_written(self) -> None:
        paths = write_all(build_report(), self.out)
        self.assertEqual(
            set(paths),
            {REPORT_MD, WORKS_CSV, CONTRACTS_CSV, PAYMENTS_CSV, LEDGER_CSV, ISSUES_CSV},
        )
        for path in paths.values():
            self.assertTrue(path.is_file())
            self.assertGreater(path.stat().st_size, 0)

    def test_works_csv(self) -> None:
        write_all(build_report(), self.out)
        rows = self.read_csv(WORKS_CSV)
        self.assertEqual(len(rows), 9)
        self.assertEqual(
            list(rows[0]),
            [
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
            ],
        )
        cancelled = next(row for row in rows if row["work_id"] == "W-13")
        self.assertEqual(cancelled["remaining"], "")
        self.assertEqual(cancelled["paid"], "50000.00")
        self.assertEqual(cancelled["approval"], "50000.00")
        for row in rows:
            for field in ("price", "paid", "overpayment", "approval"):
                self.assertRegex(row[field], MONEY_CELL)

    def test_contracts_csv(self) -> None:
        write_all(build_report(), self.out)
        rows = self.read_csv(CONTRACTS_CSV)
        self.assertEqual(len(rows), 2)
        first = next(row for row in rows if row["contract_id"] == "D-1")
        self.assertEqual(first["paid_net"], "315000.00")
        self.assertEqual(first["refunded"], "50000.00")
        self.assertEqual(first["allocated_cancelled"], "50000.00")
        self.assertEqual(first["remaining_obligations"], "235000.00")
        self.assertEqual(first["queued_total"], "150000.00")
        self.assertEqual(first["is_active"], "yes")

    def test_payments_csv_includes_excluded_payment(self) -> None:
        write_all(build_report(), self.out)
        rows = self.read_csv(PAYMENTS_CSV)
        self.assertEqual(len(rows), 8)
        excluded = next(row for row in rows if row["payment_id"] == "P-8")
        self.assertEqual(
            excluded["state_at_report"], "не существовал на отчётную дату"
        )
        refunded = next(row for row in rows if row["payment_id"] == "P-2")
        self.assertEqual(refunded["allocated_net"], "0.00")
        queued = next(row for row in rows if row["payment_id"] == "P-4")
        self.assertEqual(queued["distribution"], "")

    def test_ledger_csv(self) -> None:
        write_all(build_report(), self.out)
        rows = self.read_csv(LEDGER_CSV)
        self.assertTrue(rows)
        for row in rows:
            self.assertRegex(row["amount"], r"^-?\d+\.\d{2}$")
        refunds = [row for row in rows if row["event"] == "refund"]
        self.assertEqual(len(refunds), 6)

    def test_issues_csv_has_only_header(self) -> None:
        write_all(build_report(), self.out)
        self.assertEqual(self.read_csv(ISSUES_CSV), [])
        text = (self.out / ISSUES_CSV).read_text(encoding="utf-8-sig")
        self.assertEqual(text.splitlines()[0], "level,file,line,field,message")


class DeterminismTest(unittest.TestCase):
    def test_repeated_calculation_is_identical(self) -> None:
        first = build_report()
        second = build_report()
        self.assertEqual(
            build_report_markdown(first), build_report_markdown(second)
        )
        self.assertEqual(
            [(line.work_id, line.amount) for line in first.ledger],
            [(line.work_id, line.amount) for line in second.ledger],
        )

    def test_repeated_write_produces_same_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            first = write_all(build_report(), one)
            write_all(build_report(), two)
            for name in first:
                self.assertEqual(
                    (Path(one) / name).read_bytes(),
                    (Path(two) / name).read_bytes(),
                    msg=name,
                )


class CliTest(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, "-X", "utf8", "main.py", *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
        )

    def test_default_run_matches_explicit_date(self) -> None:
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            default = self.run_cli("--out-dir", one)
            explicit = self.run_cli(
                "--data-dir",
                "data",
                "--as-of",
                "2026-11-10",
                "--out-dir",
                two,
            )
            self.assertEqual(default.returncode, 0, default.stderr)
            self.assertEqual(explicit.returncode, 0, explicit.stderr)
            for name in (REPORT_MD, WORKS_CSV, CONTRACTS_CSV, PAYMENTS_CSV):
                self.assertEqual(
                    (Path(one) / name).read_bytes(),
                    (Path(two) / name).read_bytes(),
                    msg=name,
                )
            self.assertIn("Отчёт на 10.11.2026", default.stdout)
            self.assertIn("390 000.00", default.stdout)
            self.assertIn("250 000.00", default.stdout)

    def test_dotted_date_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            result = self.run_cli("--as-of", "10.11.2026", "--out-dir", out)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Отчёт на 10.11.2026", result.stdout)

    def test_invalid_date_exits_with_usage_error(self) -> None:
        result = self.run_cli("--as-of", "10/11/2026")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--as-of", result.stderr)

    def test_missing_data_dir_exits_with_error(self) -> None:
        result = self.run_cli("--data-dir", "нет-такого-каталога")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Ошибка данных", result.stderr)


if __name__ == "__main__":
    unittest.main()