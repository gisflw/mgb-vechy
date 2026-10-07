"""One 607 before/after run; requires the existing scratch inputs and psutil."""

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import psutil

ROOT = Path('/workspace')
RESULTS = ROOT / 'scratch/tdxhydro/output_benchmark/memory-fix/607'


def commands(python, output, baseline):
    vectors = ROOT / 'scratch/tdxhydro'
    common = ['--memory-limit-mb', '32768', '--workers', '4', '--io-slots', '2',
              '--output-dir', str(output)]
    stages = [
        ['define-roi', '--crs', 'EPSG:4326', '--catchments', str(vectors / 'catchments_607.fgb'),
         '--catchments-source-crs', 'EPSG:4326', '--segments', str(vectors / 'streams_607.fgb'),
         '--segments-source-crs', 'EPSG:4326', '--outlet-id', '630237033',
         '--outlet-id', '630233110', '--id-col', 'linkno', '--id-down-col', 'dslinkno',
         '--strahler-order-col', 'strmOrder'] + (['--batch-size', '4997'] if baseline else []),
        ['aggregate', '--roi-catchments', str(output / 'roi_catchments.fgb'),
         '--roi-segments', str(output / 'roi_segments.fgb'), '--uparea-min', '60', '--lmin', '6'],
        ['prepare', '--dem', str(ROOT / 'scratch/dem_sa.tif'), '--dem-scale', '0.01',
         '--mini-catchments', str(output / 'mini_catchments.fgb'),
         '--mini-segments', str(output / 'mini_segments.fgb'),
         '--categorical-raster', 'hru', str(ROOT / 'scratch/lc_br.tif')],
        ['terrain-products', '--dem', str(output / 'dem.tif'), '--cells', str(output / 'cells.tif'),
         '--drainage', str(output / 'drainage.tif'), '--direction-source', 'dem'],
        ['sample-minis', '--mini-catchments', str(output / 'mini_catchments.fgb'),
         '--mini-segments', str(output / 'mini_segments.fgb')] +
        [arg for name in ('dem', 'cells', 'drainage', 'hand', 'ltnd', 'hru')
         for arg in ('--' + name, str(output / (name + '.tif')))],
    ]
    for stage in stages:
        options = common.copy()
        if stage[0] == 'prepare':
            options[options.index('--workers') + 1] = '8'
        yield [python, '-m', 'mgb_vec_hydro.cli', *stage, *options]


def run(variant, python, source):
    output = RESULTS / variant / 'run-1'
    output.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, PYTHONPATH=str(source / 'src'))
    version_code = ('import importlib.metadata as m,json;'
                    'print(json.dumps({n:m.version(n) for n in '
                    "['numpy','pandas','pyarrow','pyogrio','rasterio','shapely','numba','pyproj','click']}))")
    versions = json.loads(subprocess.check_output([python, '-c', version_code], env=env))
    results = {'source': str(source), 'python': python, 'versions': versions, 'stages': {}}
    for command in commands(python, output, variant == 'before'):
        name = command[3]
        print(variant, name, flush=True)
        (output / (name + '.command.txt')).write_text(shlex.join(command) + '\n')
        counters = {}
        peak_rss = peak_scratch = 0
        start = time.perf_counter()
        with (output / (name + '.log')).open('w') as log:
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            parent = psutil.Process(process.pid)
            next_scratch = 0
            while process.poll() is None:
                try:
                    tree = [parent, *parent.children(recursive=True)]
                except psutil.Error:
                    tree = []
                rss = 0
                for child in tree:
                    try:
                        rss += child.memory_info().rss
                        counters[child.pid] = child.io_counters()._asdict()
                    except psutil.Error:
                        pass
                peak_rss = max(peak_rss, rss)
                if time.monotonic() >= next_scratch:
                    size = 0
                    for directory in output.parent.glob('.' + output.name + '*'):
                        for path in directory.rglob('*'):
                            try:
                                if path.is_file():
                                    size += path.stat().st_size
                            except FileNotFoundError:
                                pass
                    peak_scratch = max(peak_scratch, size)
                    next_scratch = time.monotonic() + 1
                time.sleep(.05)
        results['stages'][name] = {
            'wall_seconds': time.perf_counter() - start, 'peak_rss_bytes': peak_rss,
            'peak_disk_staging_bytes': peak_scratch, 'returncode': process.returncode,
            'io': {key: sum(value[key] for value in counters.values())
                   for key in next(iter(counters.values()), {})},
        }
        (output / 'measurements.json').write_text(json.dumps(results, indent=2))
        if process.returncode:
            raise RuntimeError(f'{variant} {name} failed; see {output / (name + ".log")}')
    return output


if __name__ == '__main__':
    variant, python, source = sys.argv[1:]
    run(variant, python, Path(source))
