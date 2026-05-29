import importlib
import sys


def test_package_import_does_not_load_qgis_modules():
    importlib.import_module("mgb_vec_hydro")

    forbidden_prefixes = ("qgis", "PyQt5", "processing")
    loaded = [name for name in sys.modules if name.startswith(forbidden_prefixes)]

    assert loaded == []
