"""Compare every scientific output and summarize the single 607 run."""

import json
import sys
from pathlib import Path

import numpy as np
import pyogrio
import rasterio
import shapely


def compare(before, after):
    checked = []
    for name in ('roi_catchments.fgb', 'roi_segments.fgb', 'mini_catchments.fgb', 'mini_segments.fgb'):
        left, right = (pyogrio.read_dataframe(root / name).sort_values('id').reset_index(drop=True)
                       for root in (before, after))
        assert left.crs == right.crs, name
        assert left.drop(columns='geometry').equals(right.drop(columns='geometry')), name
        assert np.array_equal(shapely.to_wkb(shapely.normalize(left.geometry.array)),
                              shapely.to_wkb(shapely.normalize(right.geometry.array))), name
        checked.append(name)
    for name in ('cells', 'drainage', 'dem', 'hru', 'hand', 'ltnd'):
        with rasterio.open(before / (name + '.tif')) as left, rasterio.open(after / (name + '.tif')) as right:
            for attribute in ('crs', 'transform', 'width', 'height', 'count', 'dtypes',
                              'nodatavals', 'mask_flag_enums', 'units', 'scales', 'offsets', 'colorinterp'):
                assert getattr(left, attribute) == getattr(right, attribute), (name, attribute)
            assert left.tags() == right.tags(), name
            assert left.tags(ns='IMAGE_STRUCTURE') == right.tags(ns='IMAGE_STRUCTURE'), name
            assert left.overviews(1) == right.overviews(1), name
            for _, window in left.block_windows(1):
                assert np.array_equal(left.read(window=window), right.read(window=window), equal_nan=True), name
                assert np.array_equal(left.read_masks(window=window), right.read_masks(window=window)), name
        checked.append(name + '.tif')
    for name in ('source_to_mini.csv', 'sampled_minis.csv'):
        assert (before / name).read_bytes() == (after / name).read_bytes(), name
        checked.append(name)
    results = [json.loads((root / 'measurements.json').read_text()) for root in (before, after)]
    assert results[0]['versions'] == results[1]['versions']
    performance = {}
    for stage in results[0]['stages']:
        a, b = (result['stages'][stage] for result in results)
        performance[stage] = {'before_seconds': a['wall_seconds'], 'after_seconds': b['wall_seconds'],
                              'change_percent': 100 * (b['wall_seconds'] / a['wall_seconds'] - 1),
                              'before_rss_gib': a['peak_rss_bytes'] / 1024**3,
                              'after_rss_gib': b['peak_rss_bytes'] / 1024**3}
    for label, getter in (
        ('wall_seconds', lambda stage: stage['wall_seconds']),
        ('logical_io_bytes', lambda stage: stage['io'].get('read_chars', 0) + stage['io'].get('write_chars', 0)),
    ):
        values = [sum(getter(stage) for stage in result['stages'].values()) for result in results]
        performance[label] = {'before': values[0], 'after': values[1],
                              'change_percent': 100 * (values[1] / values[0] - 1)}
    report = {'identical_outputs': checked, 'performance': performance, 'repetitions': 1}
    (after / 'comparison.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    compare(*(Path(arg) for arg in sys.argv[1:]))
