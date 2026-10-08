"""Which conversation turns the current work serves, so every audited call traces back to the input behind it.

A context variable reaches everywhere the work goes: into the graph event loop (run_coroutine_threadsafe runs
the coroutine in a copy of the caller's context) and into asyncio.to_thread, so Claude calls MemoryEngine makes are
attributed to the same turns. Outcomes are ids, counts and the next stage only; never prompt or reply text.
"""
from contextlib import contextmanager
from contextvars import ContextVar

CURRENT = ContextVar('phro_trace', default=None)


@contextmanager
def serving(turns, write=None):
    """Attribute calls made inside to `turns`; `write(row_id, outcome)` stores what note() records."""
    trace = {'turns': sorted({t for t in turns if t}), 'rows': [], 'write': write}
    token = CURRENT.set(trace)
    try:
        yield trace
    finally:
        CURRENT.reset(token)


def turns():
    trace = CURRENT.get()
    return trace['turns'] if trace else []


def record(row_id):
    """Called by the audit log for each row it writes inside a trace."""
    trace = CURRENT.get()
    if trace is not None:
        trace['rows'].append(row_id)


def note(outcome):
    """Attach the outcome of the step just logged (the latest audited row) to that row."""
    trace = CURRENT.get()
    if trace and trace['rows'] and trace['write']:
        trace['write'](trace['rows'][-1], outcome)
