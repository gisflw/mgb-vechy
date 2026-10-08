from io import StringIO
import threading
import time
from types import SimpleNamespace

import pytest

from mgb_vec_hydro.cli import _echo_timings, _run_stage
from mgb_vec_hydro.execution.executor import ProgressEvent
from mgb_vec_hydro.execution.progress import StageProgress, StageReporter


def test_phase_timings_cover_the_entire_run(monkeypatch):
    ticks = iter((10.0, 12.0, 17.0, 21.0))
    monkeypatch.setattr('mgb_vec_hydro.execution.progress.time.perf_counter', lambda: next(ticks))
    updates = []
    reporter = StageReporter(updates.append)
    reporter.enter('processing', 2)
    # Worker completions may arrive out of order; only reduced results advance progress.
    for kind, ordinal, reduced in [('completed', 1, 0), ('completed', 0, 0),
                                    ('reduced', 0, 1), ('reduced', 1, 2)]:
        reporter.execution_progress(ProgressEvent(kind, str(ordinal), ordinal, 2, 2, reduced, 0))
    reporter.enter('finalizing')
    timings = {'worker': 50.0}
    reporter.finish(timings)
    assert timings == {'worker': 50.0, 'preparing_wall': 2.0,
                       'processing_wall': 5.0, 'finalizing_wall': 4.0, 'total': 11.0}
    assert updates == [StageProgress('preparing'), StageProgress('processing', 0, 2),
                       StageProgress('processing', 1, 2), StageProgress('processing', 2, 2),
                       StageProgress('finalizing')]


def test_operation_counts_and_labels_do_not_change_phase_timings(monkeypatch):
    ticks = iter((1.0, 4.0, 9.0))
    monkeypatch.setattr('mgb_vec_hydro.execution.progress.time.perf_counter', lambda: next(ticks))
    updates = []
    reporter = StageReporter(updates.append)
    reporter.operation('Reading rows', total=10, unit='features')
    reporter.advance(7)
    reporter.enter('processing', 3, operation='Processing batches', unit='batches')
    timings = {}
    reporter.finish(timings)
    assert updates == [
        StageProgress('preparing'),
        StageProgress('preparing', 0, 10, 'Reading rows', 'features'),
        StageProgress('preparing', 7, 10, 'Reading rows', 'features'),
        StageProgress('processing', 0, 3, 'Processing batches', 'batches'),
    ]
    assert timings == {'preparing_wall': 3.0, 'processing_wall': 5.0, 'total': 8.0}


@pytest.mark.parametrize('terminal', [False, True])
@pytest.mark.parametrize('fail', [False, True])
def test_progress_output_and_failure_cleanup(monkeypatch, terminal, fail):
    class Stream(StringIO):
        def isatty(self):
            return terminal

    stream = Stream()
    monkeypatch.setattr('mgb_vec_hydro.cli.sys.stderr', stream)

    def stage(spec, *, progress):
        progress(StageProgress('preparing', operation='Checking inputs'))
        progress(StageProgress(
            'processing', 0, 3, 'Processing geometry batches', 'batches'
        ))
        progress(StageProgress(
            'processing', 1, 3, 'Processing geometry batches', 'batches'
        ))
        if fail:
            raise RuntimeError('worker failed')
        progress(StageProgress(
            'processing', 2, 3, 'Processing geometry batches', 'batches'
        ))
        progress(StageProgress(
            'processing', 3, 3, 'Processing geometry batches', 'batches'
        ))
        return SimpleNamespace(timings={})

    if fail:
        with pytest.raises(RuntimeError, match='worker failed'):
            _run_stage(stage, None)
    else:
        _run_stage(stage, None)
    output = stream.getvalue()
    if terminal:
        assert 'Processing geometry batches' in output
        assert ('33%' in output and '1/3' in output) if fail else '100%' in output
        assert output.count('\n') == 1
    else:
        assert output == ''


def test_terminal_refresh_is_throttled_and_animates_without_stage_events(monkeypatch):
    class TerminalStream(StringIO):
        def isatty(self):
            return True

    stream = TerminalStream()
    monkeypatch.setattr('mgb_vec_hydro.cli.sys.stderr', stream)

    def stage(spec, *, progress):
        for completed in range(101):
            progress(StageProgress(
                'preparing', completed, 100, 'Scanning vector rows', 'features'
            ))
        time.sleep(0.55)
        return SimpleNamespace(timings={})

    _run_stage(stage, None)
    output = stream.getvalue()
    assert 'Scanning vector rows' in output and '100/100 features (100%)' in output
    assert 3 <= output.count('\r') <= 5
    assert output.count('\n') == 1


@pytest.mark.parametrize('failure', [RuntimeError, KeyboardInterrupt])
def test_terminal_refresh_thread_stops_after_failure(monkeypatch, failure):
    class TerminalStream(StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr('mgb_vec_hydro.cli.sys.stderr', TerminalStream())

    def stage(spec, *, progress):
        raise failure('stage failed')

    with pytest.raises(failure, match='stage failed'):
        _run_stage(stage, None)
    assert not any(
        thread.name == 'mgb-progress' and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_cli_prints_only_elapsed_phase_timings(capsys):
    _echo_timings({'preparing_wall': 1.25, 'processing_wall': 2.0,
                   'finalizing_wall': 0.75, 'total': 4.0, 'worker': 20, 'coordination': 3})
    assert capsys.readouterr().out == 'Elapsed: preparing 1.2s, processing 2.0s, finalizing 0.8s, total 4.0s\n'


def test_publication_failure_does_not_complete_finalization(monkeypatch):
    import click

    bars = []
    original = click.progressbar

    def recording_bar(*args, **kwargs):
        bar = original(*args, **kwargs)
        bars.append(bar)
        return bar

    monkeypatch.setattr(click, 'progressbar', recording_bar)
    class TerminalStream(StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr('mgb_vec_hydro.cli.sys.stderr', TerminalStream())

    def stage(spec, *, progress):
        progress(StageProgress(
            'finalizing', 0, 1, 'Publishing outputs', 'steps'
        ))
        raise RuntimeError('publication failed')

    with pytest.raises(RuntimeError, match='publication failed'):
        _run_stage(stage, None)
    assert bars[0].label == 'Publishing outputs'
    assert bars[0].pos == 0 and not bars[0].finished
    assert '100%' not in bars[0].file.getvalue()
