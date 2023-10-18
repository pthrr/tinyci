import tempfile
import types

import pytest

from src.server import Database


class NestedNamespace(types.SimpleNamespace):
    def __init__(self, dictionary: dict, **kwargs) -> None:
        super().__init__(**kwargs)

        for key, value in dictionary.items():
            if isinstance(value, dict):
                self.__setattr__(key, NestedNamespace(value))
            else:
                self.__setattr__(key, value)


@pytest.fixture(name="database", scope="function")
def fixture_database() -> Database:
    yield Database(path_databasefile=tempfile.mktemp())


# @pytest.fixture(name="settings", scope="session")
# def fixture_settings(request) -> NestedNamespace:
#     database_file = request.config.getoption("--database_file")
#     settings_dict = {"database_file": database_file}
#     settings = NestedNamespace(settings_dict)
#     return settings


# def pytest_addoption(parser) -> None:
#     parser.addoption(
#         "--database_file",
#         action="store",
#         nargs="?",
#         const=None,
#         default=None,
#         help="File path to use for database.",
#     )
