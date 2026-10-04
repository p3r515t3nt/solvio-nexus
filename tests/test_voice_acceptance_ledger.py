"""One inherited acceptance book, including process races and uncertain endings."""
import json
import multiprocessing
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.realtime import acceptance_ledger as L


def _attempt(path, barrier, queue):
    barrier.wait()
    try:
        run = L.beginnen(path)
        queue.put(('started', run.index))
    except L.BudgetErschoepft:
        queue.put(('refused', None))


def _book(path, budget=300, count=2):
    path.write_text(json.dumps({'budget_s':budget,'gespraeche_max':count,'laeufe':[]}))


def _refuses(fn):
    try:
        fn()
    except L.BudgetErschoepft:
        return
    raise AssertionError('unconfirmed acceptance allowance was admitted')


def t_missing_corrupt_and_nan_never_create_allowance():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)/'budget.json'
        _refuses(lambda:L.beginnen(path))
        require(not path.exists())
        for content in ('{', '{}', '{"budget_s":NaN,"gespraeche_max":2,"laeufe":[]}',
                        '{"budget_s":300,"gespraeche_max":2,"laeufe":[{}]}'):
            path.write_text(content)
            _refuses(lambda:L.beginnen(path,allow_create=True,budget_s=300,gespraeche_max=2))
            require_equal(path.read_text(),content)


def t_existing_closed_prototype_history_preserves_limits_and_remaining_time():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)/'budget.json'
        # Real old wire format has no token; closed old runs remain readable.
        path.write_text(json.dumps({'budget_s':300,'gespraeche_max':2,'laeufe':[
            {'pid':1,'beginn':1000,'zuletzt':1200,'beendet':True,'grund':'abschluss'}]}))
        now=[1200.0]
        run=L.beginnen(path,budget_s=900,gespraeche_max=9,uhr=lambda:now[0])
        require_equal(run.rest,100)
        now[0]+=90
        require_equal(run.ticken(),10)
        now[0]+=12 # Slow cleanup must still be booked, not capped to allowance.
        run.beenden('abschluss')
        require_equal(L.verbraucht(L.lesen(path)),302)
        require_equal(L.rest(path),0)
        _refuses(lambda:L.beginnen(path))


def t_crashed_or_concurrent_predecessor_never_releases_unknown_rest():
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'budget.json'
        _book(path)
        run=L.beginnen(path,uhr=lambda:1000)
        before=path.read_bytes()
        _refuses(lambda:L.beginnen(path,uhr=lambda:1001))
        require_equal(path.read_bytes(),before)
        run.beenden('provider_closed')
        require_equal(L.beginnen(path,uhr=lambda:1002).index,1)


def t_two_real_processes_share_exactly_one_admission():
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'budget.json'
        _book(path)
        ctx=multiprocessing.get_context('spawn')
        barrier=ctx.Barrier(2)
        queue=ctx.Queue()
        children=[ctx.Process(target=_attempt,args=(str(path),barrier,queue)) for _ in range(2)]
        try:
            for child in children: child.start()
            outcomes=[queue.get(timeout=15) for _ in children]
            for child in children:
                child.join(15)
                require_equal(child.exitcode,0)
            require_equal(sorted(x[0] for x in outcomes),['refused','started'])
            require_equal(len(L.lesen(path)['laeufe']),1)
        finally:
            for child in children:
                if child.is_alive(): child.terminate()
                child.join(5)
            queue.close()


def t_replaced_book_cannot_be_finished_by_old_run():
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'budget.json'
        _book(path)
        run=L.beginnen(path,uhr=lambda:1000)
        _book(path)
        successor=L.beginnen(path,uhr=lambda:1000)
        before=path.read_bytes()
        _refuses(lambda:run.beenden('old_late_close'))
        require_equal(path.read_bytes(),before)
        successor.beenden('provider_closed')


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
