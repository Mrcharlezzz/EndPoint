from __future__ import annotations

from importlib import import_module
from pkgutil import walk_packages
from types import ModuleType

import evaluation.tests
import processing.tests


def _load_package_tests(package: ModuleType) -> None:
    for module_info in walk_packages(package.__path__, prefix=f"{package.__name__}."):
        module_name = module_info.name
        leaf_name = module_name.rsplit(".", 1)[-1]
        if ".manual." in module_name:
            continue
        if leaf_name in {"__init__", "conftest", "fixtures"}:
            continue
        if not leaf_name.startswith("test_"):
            continue

        module = import_module(module_name)
        module_key = module_name.replace(".", "__")
        for name, value in vars(module).items():
            if name.startswith("test_"):
                globals()[f"test__{module_key}__{name}"] = value
            elif isinstance(value, type) and name.startswith("Test"):
                globals()[f"Test__{module_key}__{name}"] = value


_load_package_tests(processing.tests)
_load_package_tests(evaluation.tests)
