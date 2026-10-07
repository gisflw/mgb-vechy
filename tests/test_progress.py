from io import StringIO
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


@pytest.mark.parametrize('terminal', [False, True])
@pytest.mark.parametrize('fail', [False, True])
def test_progress_output_and_failure_cleanup(monkeypatch, terminal, fail):
    class Stream(StringIO):
        def isatty(self):
            return terminal

    stream = Stream()
    monkeypatch.setattr('mgb_vec_hydro.cli.sys.stderr', stream)

    def stage(spec, *, progress):
        progress(StageProgress('preparing'))
        progress(StageProgress('processing', 0, 3))
        progress(StageProgress('processing', 1, 3))
        if fail:
            raise RuntimeError('worker failed')
        progress(StageProgress('processing', 2, 3))
        progress(StageProgress('processing', 3, 3))
        progress(StageProgress('finalizing'))
        return SimpleNamespace(timings={})

    if fail:
        with pytest.raises(RuntimeError, match='worker failed'):
            _run_stage(stage, None)
    else:
        _run_stage(stage, None)
    output = stream.getvalue()
    if terminal:
        assert '33%' in output and '1/3' in output
        if fail:
            assert '100%' not in output and 'Finalizing outputs' not in output
        assert output.endswith('\n')
    else:
        assert output.splitlines() == ['Preparing inputs', 'Processing batches'] + (
            [] if fail else ['Finalizing outputs'])


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
    monkeypatch.setattr('mgb_vec_hydro.cli.sys.stderr', StringIO())

    def stage(spec, *, progress):
        progress(StageProgress('processing', 0, 1))
        progress(StageProgress('processing', 1, 1))
        progress(StageProgress('finalizing'))
        raise RuntimeError('publication failed')

    with pytest.raises(RuntimeError, match='publication failed'):
        _run_stage(stage, None)
    assert bars[0].label == 'Finalizing outputs'
    assert bars[0].pos == 0 and not bars[0].finished
