"""Wallet columns stay inside the mypy gate.

A module-wide ``ignore_errors`` on ``app.db.models`` used to hide a float
assigned to a Decimal balance in the schema that stores money. Call-site
checks do not cover that file: the checker has to read the model module.
"""

from __future__ import annotations

import fnmatch
from configparser import ConfigParser
from pathlib import Path

GATEWAY = Path(__file__).resolve().parents[1]
MYPY_INI = GATEWAY / "mypy.ini"
MODULE = "app.db.models"


def _ignored_modules() -> list[str]:
    parser = ConfigParser()
    read = parser.read(MYPY_INI)
    assert read, f"mypy.ini missing at {MYPY_INI}"
    ignored: list[str] = []
    for section in parser.sections():
        if not section.startswith("mypy-"):
            continue
        if not parser.getboolean(section, "ignore_errors", fallback=False):
            continue
        pattern = section.removeprefix("mypy-")
        if fnmatch.fnmatch(MODULE, pattern):
            ignored.append(section)
    return ignored


def test_mypy_does_not_ignore_db_models() -> None:
    assert _ignored_modules() == []


def test_mypy_flags_float_assigned_to_wallet_balance(tmp_path: Path) -> None:
    """A float written onto WalletModel.balance must be a type error.

    ``ignore_errors`` on this module name makes the same file pass, which is
    the regression this locks out.
    """
    package = tmp_path / "app" / "db"
    package.mkdir(parents=True)
    (tmp_path / "app" / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    source = package / "models.py"
    source.write_text(
        "from decimal import Decimal\n"
        "from sqlmodel import Field, SQLModel\n"
        "\n"
        "class WalletModel(SQLModel, table=True):\n"
        "    wallet_id: str = Field(primary_key=True)\n"
        "    balance: Decimal = Field(default=Decimal('0'))\n"
        "\n"
        "    def credit(self) -> None:\n"
        "        self.balance = 1.5\n"
    )
    from mypy.api import run

    stdout, stderr, code = run(
        [
            "--config-file",
            str(MYPY_INI),
            "--no-incremental",
            "--cache-dir",
            str(tmp_path / "mypy-cache"),
            str(source),
        ]
    )
    assert code != 0, stdout + stderr
    assert 'expression has type "float"' in stdout
    assert 'variable has type "Decimal"' in stdout


def test_real_db_models_module_typechecks(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(GATEWAY)
    from mypy.api import run

    stdout, stderr, code = run(
        [
            "--config-file",
            str(MYPY_INI),
            "--no-incremental",
            "--cache-dir",
            str(tmp_path / "mypy-cache"),
            "app/db/models.py",
        ]
    )
    assert code == 0, stdout + stderr
