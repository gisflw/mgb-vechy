import sys


def test_package_import_does_not_load_qgis_modules():
    import mgb_vec_hydro  # noqa: F401

    forbidden = {"qgis", "processing", "PyQt5"}
    assert forbidden.isdisjoint(sys.modules)
