import pytest

from src.main import ResultsEntry


def test_create(database) -> None:
    pass


def test_add(database) -> None:
    result = ResultsEntry(repo="framework", id="123456", status="")
