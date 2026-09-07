#!/usr/bin/env python3
"""
STL Batch Fix — Terminal UI
Reads/writes a .fixcfg settings file next to this script, then runs
stl_batch_fix.process_file_safe in a ProcessPoolExecutor while displaying
live per-worker panels, a progress bar, and a running summary table.

Run via:  bash run.sh  (or directly: python stl_batch_fix_tui.py)
"""

import concurrent.futures
import multiprocessing
import os
import signal
import sys
import time
import threading
from collections import deque
from pathlib import Path

# How many finished results to retain for the final listing.  The live panel
# only ever shows the last 30; this bound keeps the parent's memory flat on
# very large batches while still giving a useful end-of-run summary.
_RESULTS_KEPT = 500

# NOTE: ProcessPoolExecutor's max_tasks_per_child is deliberately NOT used to
# recycle workers.  Setting it forces the 'spawn' start method, which re-imports
# this module in every worker and loses the fork-inherited state the pipeline
# relies on.  Idle memory is reclaimed with malloc_trim() after each file
# instead (see release_worker_memory in stl_batch_fix.py).

# ---------------------------------------------------------------------------
# Resolve script directory — config files live here regardless of cwd.
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Import the main script's functions (workers, helpers, globals).
# ---------------------------------------------------------------------------
sys.path.insert(0, str(SCRIPT_DIR))
import stl_batch_fix as _fix

# ---------------------------------------------------------------------------
# Config file handling
# ---------------------------------------------------------------------------

CFG_FIELDS = [
    # (key, label, type, description)
    ('INPUT_FOLDER',  'Input folder',      str,   'Source folder containing STL/OBJ files'),
    ('OUTPUT_SUFFIX', 'Output suffix',     str,   'Appended to output filename stem (blank = none)'),
    ('MERGE_DIST',    'Merge distance mm', float, 'Vertex merge radius for T-junction fix'),
    ('WORKERS',       'Workers',           int,   'Parallel worker processes (0 = auto from RAM and cores)'),
    ('TIMEOUT',       'Timeout (s)',       int,   'Per-file limit; the worker is killed if a file exceeds it'),
    ('MAX_FACES',     'Max faces',         int,   'Decimate threshold (0 = disabled)'),
    ('RECURSIVE',     'Recursive',         bool,  'Walk subdirectories'),
]

CFG_DEFAULTS = {
    'INPUT_FOLDER':  _fix.INPUT_FOLDER,
    'OUTPUT_SUFFIX': _fix.OUTPUT_SUFFIX,
    'MERGE_DIST':    str(_fix.MERGE_DIST),
    'WORKERS':       str(_fix.WORKERS),
    'TIMEOUT':       str(_fix.TIMEOUT),
    'MAX_FACES':     str(_fix.MAX_FACES),
    'RECURSIVE':     str(_fix.RECURSIVE),
}


def _pid_is_live(pid):
    """True if `pid` is a running process, treating a zombie as dead.

    os.kill(pid, 0) is not enough: it succeeds for a zombie, because the process
    entry survives until the parent reaps it.  A SIGKILLed worker therefore kept
    its row in the worker panel with the clock still counting up — which is what
    made a killed run look alive.  /proc/<pid>/stat field 3 is the state letter;
    'Z' means the process is gone in every sense that matters here."""
    try:
        with open(f'/proc/{pid}/stat') as f:
            # The comm field can contain spaces and parentheses, so the state
            # letter is read relative to the LAST ')', not by splitting.
            data = f.read()
        return data[data.rindex(')') + 2] != 'Z'
    except (OSError, ValueError, IndexError, TypeError):
        return False


def _status_snapshot(proxy, timeout=2.0):
    """Read a Manager dict proxy without ever blocking the caller.

    Manager proxies talk to a separate process over a socket, and the call has
    no timeout: if a worker is SIGKILLed while holding the connection lock, or
    the manager itself is wedged, an ordinary .items() never returns.  The read
    therefore happens on a daemon thread that is waited on, not joined — if it
    does not answer in `timeout` seconds it is abandoned (it dies with the
    interpreter) and None is returned.

    Returns a list of (key, value) pairs, or None if the read did not complete.
    """
    box = {}

    def _read():
        try:
            box['v'] = list(proxy.items())
        except Exception:
            box['v'] = None

    t = threading.Thread(target=_read, daemon=True, name='status-read')
    t.start()
    t.join(timeout)
    return box.get('v') if not t.is_alive() else None


def _abandon_pool(pool):
    """Discard a pool whose worker was SIGKILLed, without ever blocking.

    shutdown(wait=False) is not safe here.  It still closes the executor's
    _call_queue, and Queue.close() joins the feeder thread — which is blocked
    writing to a pipe whose reader was the killed worker, holding the queue's
    internal lock that the dead process never released.  The parent then waits
    on that futex forever: observed as a run stuck at 0% CPU with no children
    while the TUI kept redrawing stale worker rows.

    So the shutdown is handed to a daemon thread and never joined.  If it hangs
    there it hangs alone; the daemon flag keeps it from holding up interpreter
    exit.  The pool's processes are already dead or dying, and any file that was
    in flight has been requeued by the caller."""
    def _drain():
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
    threading.Thread(target=_drain, daemon=True,
                     name='abandon-pool').start()


def _child_pids(pid):
    """Direct children of `pid`, read from /proc.

    Used to reach a Blender launched by a worker: the worker's own tracked
    handle is a global inside that process and is not visible from here."""
    out = []
    try:
        task_dir = f'/proc/{pid}/task'
        for tid in os.listdir(task_dir):
            try:
                with open(f'{task_dir}/{tid}/children') as f:
                    out.extend(int(k) for k in f.read().split())
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return out


def _cfg_path(name):
    return SCRIPT_DIR / name


def _find_cfg_files():
    return sorted(SCRIPT_DIR.glob('*.fixcfg'))


def _read_cfg(path):
    cfg = dict(CFG_DEFAULTS)
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                k, _, v = line.partition('=')
                cfg[k.strip()] = v.strip()
    return cfg


def _write_cfg(path, cfg):
    with open(path, 'w') as f:
        f.write('# STL Batch Fix configuration\n')
        for key, label, typ, desc in CFG_FIELDS:
            f.write(f'# {desc}\n')
            f.write(f'{key} = {cfg[key]}\n\n')


def _cfg_to_values(cfg):
    """Convert string dict to typed dict."""
    out = {}
    for key, _, typ, _ in CFG_FIELDS:
        v = cfg.get(key, CFG_DEFAULTS[key])
        if typ == bool:
            out[key] = v.lower() in ('true', '1', 'yes')
        elif typ == int:
            out[key] = int(v)
        elif typ == float:
            out[key] = float(v)
        else:
            out[key] = v
    return out


# ---------------------------------------------------------------------------
# Rich imports
# ---------------------------------------------------------------------------
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn, MofNCompleteColumn
from rich.prompt import Prompt, Confirm
from rich.table import Table
from rich.text import Text
from rich import box
import rich.traceback
rich.traceback.install()

console = Console()

# ---------------------------------------------------------------------------
# Config screen (plain prompt-based, no curses)
# ---------------------------------------------------------------------------

# Signal files the run writes, with what each one means and whether deleting it
# makes the pipeline retry that file.  Order is the order shown in the menu.
_SIGNAL_KINDS = [
    ('.failed.stl',     'Failed',     'copy of source; transient error', True),
    ('.timeout.stl',    'Timed out',  'copy of source; exceeded the time limit', True),
    ('.unrepaired.stl', 'Unrepaired', 'copy of source; non-manifold edges remain', True),
    ('.broken.stl',     'Broken',     'copy of source; permanently bad mesh', True),
    ('.open.stl',       'Open edges', 'REPAIRED output; open edges remain', True),
    ('.original.stl',   'Bbox check', 'copy of source; repair moved the bounding box', False),
]


def _find_signal_files(out_root):
    """Return {suffix: [paths]} for every signal file under out_root."""
    found = {suf: [] for suf, _, _, _ in _SIGNAL_KINDS}
    for root, _dirs, files in os.walk(out_root):
        for name in files:
            for suf in found:
                if name.endswith(suf):
                    found[suf].append(os.path.join(root, name))
                    break
    return found


def run_delete_signals_screen(cfg):
    """Delete signal files by kind, so a run can retry what they suppress.

    The markers are full copies of the source, so this is the one screen that
    can remove real data — every deletion is counted, sized and confirmed
    before anything is touched."""
    out_root = os.path.join(
        os.path.dirname(os.path.abspath(cfg.get('INPUT_FOLDER', '.'))), 'Fixed')
    console.print()
    console.rule('[bold yellow]Delete signal files[/bold yellow]')
    console.print(f"[dim]Output folder: {out_root}[/dim]")
    console.print()
    if not os.path.isdir(out_root):
        console.print(f"[red]Output folder does not exist.[/red]")
        console.print()
        return

    found = _find_signal_files(out_root)
    if not any(found.values()):
        console.print("[green]No signal files found — nothing to delete.[/green]")
        console.print()
        return

    t = Table(box=box.SIMPLE, show_header=True, header_style='bold')
    t.add_column('#', style='dim', width=3)
    t.add_column('Kind', style='cyan', width=12)
    t.add_column('Files', style='yellow', justify='right', width=6)
    t.add_column('Size', style='yellow', justify='right', width=10)
    t.add_column('Meaning', style='dim')
    shown = []
    for suf, label, desc, retries in _SIGNAL_KINDS:
        paths = found.get(suf, [])
        if not paths:
            continue
        size = sum(os.path.getsize(p) for p in paths if os.path.exists(p))
        shown.append((suf, label, paths))
        note = desc + ('' if retries else '  (deleting does NOT change what runs)')
        t.add_row(str(len(shown)), label, str(len(paths)), _human_bytes(size), note)
    console.print(t)
    _all = sum(len(p) for _, _, p in shown)
    _allsize = sum(os.path.getsize(p) for _, _, ps in shown for p in ps
                   if os.path.exists(p))
    console.print(f"Enter a number to delete that kind, [yellow]a[/yellow] for all "
                  f"({_all} files, {_human_bytes(_allsize)}), "
                  f"[green]b[/green] to go back.")
    console.print()

    while True:
        choice = Prompt.ask('Delete', default='b').strip().lower()
        if choice in ('b', ''):
            console.print()
            return
        if choice == 'a':
            targets = [p for _, _, ps in shown for p in ps]
            what = 'ALL signal files'
        elif choice.isdigit() and 1 <= int(choice) <= len(shown):
            suf, label, targets = shown[int(choice) - 1]
            what = f'{label} ({suf})'
        else:
            console.print('[red]Not a valid choice.[/red]')
            continue

        size = sum(os.path.getsize(p) for p in targets if os.path.exists(p))
        console.print(f"[yellow]About to delete {len(targets)} file(s), "
                      f"{_human_bytes(size)} — {what}.[/yellow]")
        console.print("[dim]These are full copies of your source meshes. The "
                      "originals in the input folder are not touched.[/dim]")
        if not Confirm.ask('Delete them?', default=False):
            console.print('[dim]Nothing deleted.[/dim]')
            console.print()
            return
        n = failed = 0
        for p in targets:
            try:
                os.unlink(p)
                n += 1
            except OSError:
                failed += 1
        console.print(f"[green]Deleted {n} file(s).[/green]"
                      + (f" [red]{failed} could not be removed.[/red]" if failed else ""))
        console.print()
        return


def run_config_screen(cfg):
    """Show current config, let user edit fields, return updated cfg dict."""
    console.rule('[bold cyan]STL Batch Fix — Configuration[/bold cyan]')
    console.print()

    # Display current values
    t = Table(box=box.SIMPLE, show_header=True, header_style='bold')
    t.add_column('#', style='dim', width=3)
    t.add_column('Setting', style='cyan')
    t.add_column('Value', style='yellow')
    t.add_column('Description', style='dim')
    for i, (key, label, typ, desc) in enumerate(CFG_FIELDS, 1):
        t.add_row(str(i), label, str(cfg[key]), desc)
    console.print(t)

    console.print("Enter a field number to edit, [green]s[/green] to start, "
                  "[yellow]d[/yellow] to delete signal files, [red]q[/red] to quit.")
    console.print()

    while True:
        choice = Prompt.ask('Choice', default='s').strip().lower()
        if choice == 'q':
            console.print('Bye.')
            sys.exit(0)
        if choice == 's':
            break
        if choice == 'd':
            run_delete_signals_screen(cfg)
            continue
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(CFG_FIELDS):
                key, label, typ, desc = CFG_FIELDS[idx]
                current = cfg[key]
                if typ == bool:
                    new_val = Confirm.ask(f'  {label}', default=(current.lower() in ('true','1','yes')))
                    cfg[key] = str(new_val)
                else:
                    new_val = Prompt.ask(f'  {label}', default=current)
                    cfg[key] = new_val
                console.print()
                # Re-display table
                t = Table(box=box.SIMPLE, show_header=True, header_style='bold')
                t.add_column('#', style='dim', width=3)
                t.add_column('Setting', style='cyan')
                t.add_column('Value', style='yellow')
                t.add_column('Description', style='dim')
                for i, (k, lbl, tp, ds) in enumerate(CFG_FIELDS, 1):
                    t.add_row(str(i), lbl, str(cfg[k]), ds)
                console.print(t)
                console.print("Enter a field number to edit, [green]s[/green] to start, [red]q[/red] to quit.")
            else:
                console.print('[red]Invalid number.[/red]')
        else:
            console.print('[red]Enter a number, s, or q.[/red]')

    return cfg


# ---------------------------------------------------------------------------
# Progress screen
# ---------------------------------------------------------------------------

_STATUS_STYLE = {
    'ok':         '[green]OK[/green]',
    'open':       '[yellow]OPEN[/yellow]',
    'skip':       '[dim]SKIP[/dim]',
    'corrupt':    '[red]CORRUPT[/red]',
    'unrepaired': '[magenta]UNREPAIRED[/magenta]',
    'interrupted':'[yellow]INTERRUPTED[/yellow]',
    'failed':     '[bold red]FAILED[/bold red]',
}

def _mmss(seconds):
    """Format a duration as mm:ss (or h:mm:ss past an hour)."""
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _human_bytes(n):
    """Compact size for display: 4.2 MB, 812 KB, 1.1 GB."""
    n = float(n or 0)
    for unit, div in (('GB', 1024**3), ('MB', 1024**2), ('KB', 1024)):
        if n >= div:
            return f"{n/div:.1f} {unit}"
    return f"{int(n)} B"


def _extract_detail(result):
    """Pull the one interesting line out of a result's Blender output.

    Called once, as the result arrives, so the multi-KB stdout/stderr can be
    dropped immediately rather than retained for the whole run (todo M8)."""
    detail = result.get('reason') or ''
    if detail:
        return detail[:200]
    for line in reversed((result.get('stdout') or '').splitlines()):
        line = line.strip()
        if line and not line.startswith('Fra:') and 'blender' not in line.lower()[:7]:
            return line[:200]
    return ''


def _trim_result(result):
    """Return a compact copy of a result with the bulky output fields replaced
    by the single extracted detail line."""
    trimmed = {k: v for k, v in result.items() if k not in ('stdout', 'stderr')}
    if result['status'] in ('open', 'unrepaired', 'failed'):
        detail = _extract_detail(result)
        if detail:
            trimmed['detail'] = detail
    return trimmed


def _status_label(result):
    s = result['status']
    if s == 'ok':
        # An 'ok' whose post-verify scan could not run has been checked by
        # nothing.  Say so on the line itself — a plain green OK is what gets
        # acted on, and 'verified' is absent only on results from older runs.
        if result.get('verified') is False:
            return '[green]OK[/green] [yellow]UNVERIFIED[/yellow]'
        if result.get('split'):
            return f'[green]OK[/green] [dim]SPLIT({result["split"]})+MERGE[/dim]'
        if result.get('bypassed'):
            return '[green]OK[/green] [dim]clean copy[/dim]'
        if result.get('pymeshfix'):
            return '[green]OK[/green] [dim]+pymeshfix[/dim]'
    return _STATUS_STYLE.get(s, s)


def _worker_mem_limit(n_workers):
    """Per-worker RLIMIT_AS in bytes.  Returns 0 — the cap is deliberately off.

    This used to divide WORKER_MEM_MAX between the workers, but RLIMIT_AS caps
    *virtual address space*, not resident memory, and the mesh libraries are C++
    code that reserves far more VA than it ever resides.  Three workers under a
    2 GiB cap each failed every decimation with std::bad_alloc while actually
    using ~1 GB RSS apiece (3.1 GB total, against an 8 G cgroup that was never
    close to full).  numpy reported it plainly: "Unable to allocate 42.0 MiB".

    Resident memory is what needs bounding, and the cgroup MemoryMax from run.sh
    already does that correctly across the whole process tree — workers and
    Blender alike.  Keeping the function (rather than deleting the call site)
    leaves one documented place to reintroduce a real limit, should a per-worker
    RSS cap ever be wanted; RLIMIT_AS is not that mechanism.
    """
    return 0


def run_progress_screen(values, files, cfg, sized=None):
    """Drive the worker pool and display live progress."""
    n_workers = values['WORKERS']
    total = len(files)
    # (n_tris, path) smallest-first; falls back to unknown sizes if not supplied.
    _sized = sized if sized is not None else [(0, p) for p in files]

    # Apply values to the module globals so workers pick them up.
    _fix.INPUT_FOLDER  = values['INPUT_FOLDER']
    _fix.OUTPUT_SUFFIX = values['OUTPUT_SUFFIX']
    _fix.MERGE_DIST    = values['MERGE_DIST']
    _fix.WORKERS       = values['WORKERS']
    _fix.TIMEOUT       = values['TIMEOUT']
    _fix.MAX_FACES     = values['MAX_FACES']
    _fix.RECURSIVE     = values['RECURSIVE']

    # Prepare log file.  Previous runs age off as .1 … .N (see _fix.LOG_KEEP)
    # rather than being overwritten, so restarting after a bad run does not
    # destroy the log that explains it.
    log_dir = os.path.dirname(_fix.LOG_FILE)
    os.makedirs(log_dir, exist_ok=True)
    _fix.rotate_all_logs()
    open(_fix.LOG_FILE, 'w').close()
    _fix._reset_review_file()
    _fix._reset_summary_file()

    # Clear intermediates a previously killed worker could not clean up (a
    # SIGKILL skips the finally block that normally removes them).
    _n_swept, _bytes_swept = _fix.sweep_orphan_temps(
        os.path.join(os.path.dirname(os.path.abspath(values['INPUT_FOLDER'])), 'Fixed'))
    if _n_swept:
        console.print(f"[dim]Removed {_n_swept} orphaned temp file(s) "
                      f"({_bytes_swept/1e6:.0f} MB) from a previous run[/dim]")

    # Shared state: workers write their current file into worker_status keyed by PID.
    _mgr = multiprocessing.Manager()
    worker_status = _mgr.dict()   # pid -> {'rel': str, 'started': float}
    # Only the last N results are ever rendered, and each raw result carries the
    # full Blender stdout/stderr — tens of KB apiece.  Keeping every one of them
    # for a large batch costs hundreds of MB in the parent (todo M8), so results
    # are trimmed on arrival and the list is bounded.
    results_log  = deque(maxlen=_RESULTS_KEPT)   # (rel, trimmed result_dict)
    # Stable display slot per worker PID, so files don't jump between rows as
    # workers come and go (todo M7).
    worker_slots = {}
    counts = {'ok': 0, 'open': 0, 'skip': 0, 'failed': 0, 'corrupt': 0,
              'unrepaired': 0, 'interrupted': 0}
    counts_lock = threading.Lock()
    done_count  = [0]

    def _make_layout():
        layout = Layout()
        # Fixed-size Layout regions clip silently from the bottom when actual
        # content is taller — Rich doesn't shrink the panel to fit, it cuts off
        # whatever overflows, border and all. The previous sizes (n_workers+4,
        # 6) were 2 and 1 lines short respectively, so the last worker row and
        # the summary panel's bottom border were being clipped on every render.
        # These values are measured against the real Table/Panel output the
        # panel builders below actually produce (box.SIMPLE, show_header=True,
        # one data row per worker) — verify against rendered output, not
        # recomputed from box-drawing theory, if the table styling changes.
        layout.split_column(
            Layout(name='header', size=3),
            Layout(name='workers', size=n_workers + 6),
            Layout(name='summary', size=7),
            Layout(name='log'),
        )
        return layout

    def _header_panel(progress):
        return Panel(progress, title='[bold cyan]STL Batch Fix[/bold cyan]', border_style='cyan')

    def _workers_panel():
        t = Table(box=box.SIMPLE, show_header=True, header_style='dim')
        t.add_column('Worker', style='dim',    width=7)
        t.add_column('PID',    style='dim',    width=8)
        t.add_column('File',   style='white',  ratio=1)
        t.add_column('Size',   style='cyan',   width=9,  justify='right')
        t.add_column('Tris',   style='cyan',   width=11, justify='right')
        t.add_column('Time',   style='yellow', width=8,  justify='right')
        # Bounded read — a Manager proxy call can block forever if a worker was
        # killed while holding the connection lock, and this runs every 0.25s on
        # the same thread that redraws.  Stale data for one frame beats a frozen
        # UI (see _status_snapshot).
        _snap = _status_snapshot(worker_status, timeout=1.0)
        active = dict(_snap) if _snap is not None else {}
        # Drop entries left by workers that died. process_file_safe removes its
        # own entry when a file finishes, but a SIGKILLed worker never runs that
        # line, so its row would otherwise sit here for the rest of the run with
        # a timer counting up on a file nothing is working on.
        for _pid in list(active):
            if not _pid_is_live(_pid):
                active.pop(_pid, None)
                try:
                    worker_status.pop(_pid, None)
                except Exception:
                    pass
        # Assign each PID a stable slot the first time it is seen, and reuse a
        # slot only once its previous occupant is gone — otherwise rows shuffle
        # on every refresh as the dict's iteration order changes (todo M7).
        for pid in active:
            if pid not in worker_slots:
                taken = set(worker_slots.values())
                free = next((s for s in range(n_workers) if s not in taken),
                            len(worker_slots))
                worker_slots[pid] = free
        for pid in [p for p in worker_slots if p not in active]:
            del worker_slots[pid]

        rows = {worker_slots[pid]: (pid, info) for pid, info in active.items()}
        now = time.monotonic()
        for slot in range(max(n_workers, len(rows))):
            entry = rows.get(slot)
            if entry is None:
                t.add_row(f'{slot}', '', '[dim]idle[/dim]', '', '', '')
            else:
                pid, info = entry
                _tris = info.get('tris') or 0
                t.add_row(f'{slot}', str(pid), info['rel'],
                          _human_bytes(info.get('bytes')),
                          f"{_tris:,}" if _tris else '',
                          _mmss(now - info['started']))
        return Panel(t, title='[bold]Workers[/bold]', border_style='blue')

    def _summary_panel():
        t = Table(box=box.SIMPLE, show_header=True, header_style='bold')
        for col, style in [('OK','green'),('OPEN','yellow'),('SKIP','dim'),
                           ('FAIL','red'),('CORRUPT','red'),('UNREPAIRED','magenta'),
                       ('INTERRUPT','yellow')]:
            t.add_column(col, style=style, justify='right', width=10)
        with counts_lock:
            t.add_row(str(counts['ok']), str(counts['open']), str(counts['skip']),
                      str(counts['failed']), str(counts['corrupt']),
                      str(counts['unrepaired']), str(counts['interrupted']))
        return Panel(t, title='[bold]Summary[/bold]', border_style='green')

    def _log_panel():
        text = Text()
        for rel, r in list(results_log)[-30:]:   # last 30 results
            label = _status_label(r)
            text.append(f'{rel}', style='white')
            text.append('  ')
            text.append_text(Text.from_markup(label))
            if r['status'] == 'ok' and r.get('size'):
                text.append(f"  ({r['size']:,} bytes)", style='dim')
            detail = r.get('detail')
            if detail:
                text.append(f"  {detail[:120]}", style='dim')
            text.append('\n')
        return Panel(text, title='[bold]Results[/bold]', border_style='dim')

    progress = Progress(
        TextColumn('[progress.description]{task.description}'),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        expand=True,
    )
    task = progress.add_task('Processing', total=total)

    layout = _make_layout()
    _run_started = time.monotonic()

    _pool = concurrent.futures.ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_fix._worker_init,
        initargs=(worker_status, 10, _worker_mem_limit(n_workers)),
    )
    _interrupted = threading.Event()

    def _shutdown_pool():
        """Cancel pending futures, then tear the worker processes down for real.

        SIGTERM asks each worker to kill its Blender child and exit; anything
        still alive a moment later gets SIGKILL, so a wedged worker can't
        outlive the run (todo M2).  Without this sweep the only backstop is
        systemd scope teardown, which doesn't exist on the run.sh fallback path."""
        _interrupted.set()
        for f in list(future_to_src):
            f.cancel()
        # Snapshot the worker PIDs before shutdown() clears the pool's bookkeeping.
        try:
            pids = list(_pool._processes)  # dict pid->Process (CPython detail)
        except AttributeError:
            pids = []
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        _pool.shutdown(wait=False, cancel_futures=True)
        # Give each worker a moment to run its SIGTERM handler (which kills
        # Blender), then escalate to SIGKILL for whatever ignored it.
        deadline = time.monotonic() + 5.0
        alive = list(pids)
        while alive and time.monotonic() < deadline:
            time.sleep(0.1)
            still = []
            for pid in alive:
                try:
                    os.kill(pid, 0)
                    still.append(pid)
                except OSError:
                    pass       # already gone
            alive = still
        for pid in alive:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    _shutdown_started = threading.Event()

    def _handle_signal(sig, frame):
        # Never print here: Live(screen=True) owns the alternate screen buffer,
        # so anything written now is overwritten or corrupts the layout (todo H3).
        # The interrupt is reported by the normal summary path once Live exits.
        if _shutdown_started.is_set():
            # Second interrupt while shutdown is already running — the user wants
            # out now.  Restore default handling so a third Ctrl+C can't be
            # swallowed either, and leave immediately (todo H4).
            signal.signal(signal.SIGINT,  signal.SIG_DFL)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            os._exit(130)
        _shutdown_started.set()
        _shutdown_pool()

    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    future_to_src = {}
    # Files whose future died with the pool without ever running.  A worker
    # killed by the OOM killer breaks the whole ProcessPoolExecutor, not just
    # its own task: every not-yet-completed future — including files still
    # sitting in the queue, unassigned to anyone — fails with BrokenProcessPool
    # and the pool refuses further work.  One 7M-triangle mesh therefore cost 40
    # untouched files in the last run.  These are collected and retried on a
    # fresh pool rather than written off.
    _to_retry = []
    # How many times each file has been resubmitted after a pool break.  A file
    # that dies once may simply have been in flight beside the real culprit; a
    # file that dies on two independent pools is the culprit.
    _attempts = {}
    # Files that broke a pool more than once — reported, never resubmitted.
    _gave_up = []
    _pool_generation = [0]
    _MAX_POOL_RESTARTS = 3
    try:
        with Live(layout, console=console, refresh_per_second=4, screen=True):
            # Submit progressively rather than all at once.  Memory per worker
            # scales with the mesh in flight, so concurrency has to fall as the
            # files get bigger — submitting everything up front would hand the
            # pool a 20M-triangle mesh to run alongside three others.  Files are
            # ordered smallest-first, and the number allowed in flight is
            # recomputed from the next file's size, dropping toward a single
            # worker as the meshes grow.
            _queue = list(_sized)          # [(n_tris, path)], smallest first
            _next_index = [0]
            # Workers report the relative name; the timeout marker needs the
            # source path to copy from, so keep the mapping back.
            _rel_to_src = {os.path.relpath(p, values['INPUT_FOLDER']): p
                           for _, p in _sized}

            def _fill():
                """Submit while the next file still fits alongside what is running.

                Returns False if the pool is broken and could not accept work —
                the caller then falls through to the restart path.  A dead pool
                raises from submit() itself, not just from future.result(), so
                this has to be caught here or it escapes the whole TUI."""
                while _queue:
                    n_tris, src = _queue[0]
                    allowed = _fix.plan_worker_count(n_tris, n_workers)
                    if len(pending) >= allowed:
                        break
                    _queue.pop(0)
                    try:
                        fut = _pool.submit(_fix.process_file_safe, src)
                    except (concurrent.futures.process.BrokenProcessPool,
                            RuntimeError):
                        # Pool died as this file was being handed over.  It has
                        # not run, so it goes back — but to the BACK of the
                        # queue, like any other interrupted file.  Returning it
                        # to the front would make the replacement pool retry it
                        # first, and if this file is the one killing workers that
                        # burns the whole restart budget on it.
                        _attempts[src] = _attempts.get(src, 0) + 1
                        if _attempts[src] <= 1:
                            _queue.append((n_tris, src))
                        else:
                            _gave_up.append((n_tris, src))
                        return False
                    future_to_src[fut] = (_next_index[0], src)
                    _next_index[0] += 1
                    pending.add(fut)
                    if allowed < n_workers:
                        _fix.log_step('(sched)',
                                      f"{os.path.basename(src)}: {n_tris:,} tris "
                                      f"— limiting to {allowed} concurrent worker(s)")
                return True

            _timed_out = {}      # pid -> rel, so one kill is not repeated

            def _watchdog():
                """Kill any worker that has held a single file past TIMEOUT.

                TIMEOUT used to reach only Blender, via communicate(timeout=).
                Everything else in the pipeline — fast_simplification, pymeshfix,
                pymeshlab — runs in-process inside C++ that holds the GIL, so no
                Python-level timer can interrupt it and a hang there was
                unbounded.  That is the likeliest place to hang, too: pymeshfix
                is ~80% of total runtime.

                The parent is the only process that can enforce this, and it
                already wakes every 0.25s to redraw.  SIGKILL rather than
                SIGTERM because the target is wedged inside a C++ call that will
                not service a handler.  Killing the worker breaks the pool,
                which the existing BrokenProcessPool path already handles: the
                file is retried once, then set aside.  monotonic() is
                system-wide on Linux, so the worker's 'started' is directly
                comparable here."""
                if _fix.TIMEOUT <= 0:
                    return
                now = time.monotonic()
                # worker_status is a Manager().dict() proxy: every read is a
                # blocking socket round-trip to the manager process, with no
                # timeout available.  A worker SIGKILLed while holding that
                # connection's lock leaves this call waiting forever — which is
                # what froze a real run: parent at futex_do_wait, 0% CPU, the
                # TUI still redrawing stale rows because the redraw loop shares
                # this thread.  _status_snapshot() does the read on a throwaway
                # thread and gives up rather than joining it.
                snapshot = _status_snapshot(worker_status)
                if snapshot is None:
                    return                      # manager wedged or gone
                for pid, info in snapshot:
                    try:
                        started = info['started']
                        rel = info.get('rel', '?')
                    except Exception:
                        continue
                    # Keyed by (pid, started) rather than pid alone: a restarted
                    # pool can hand a fresh worker a recycled pid, and a
                    # pid-only guard would exempt that innocent worker from the
                    # timeout for the rest of the run.
                    if (pid, started) in _timed_out:
                        continue
                    held = now - started
                    if held < _fix.TIMEOUT:
                        continue
                    _timed_out[(pid, started)] = rel
                    _fix.log_step(rel, f"TIMEOUT after {held:.0f}s "
                                       f"(limit {_fix.TIMEOUT}s) — killing worker {pid}")
                    # Write the indicator here, in the parent: the worker is
                    # about to be SIGKILLed and will never reach the code that
                    # writes the other .<signal>.stl markers.
                    _src_path = _rel_to_src.get(rel)
                    if _src_path:
                        _marker = _fix.mark_timeout(_src_path,
                                                    values['INPUT_FOLDER'],
                                                    values['OUTPUT_SUFFIX'])
                        if _marker:
                            _fix.log_step(rel, f"wrote {os.path.basename(_marker)} "
                                               f"— delete it to retry this file")
                    # Kill the worker's own children (a Blender it launched)
                    # first.  SIGKILL on the worker alone would reparent that
                    # grandchild to init, where nothing is left to stop it and
                    # it keeps its memory for as long as it runs.  Read from
                    # /proc rather than tracked state: the worker's
                    # _blender_proc global lives in the worker, not here.
                    for _tid in _child_pids(pid):
                        try:
                            os.kill(_tid, signal.SIGKILL)
                        except OSError:
                            pass
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass

            pending = set()
            _fill()

            # Outer loop: each pass drains a pool.  If a worker death broke the
            # pool and left files unrun, a replacement pool is built below and
            # this repeats — the inner `while pending` cannot do it itself,
            # because a broken pool empties `pending` and ends that loop.
            while True:
                while (pending or _queue) and not _interrupted.is_set():
                    layout['header'].update(_header_panel(progress))
                    layout['workers'].update(_workers_panel())
                    layout['summary'].update(_summary_panel())
                    layout['log'].update(_log_panel())

                    done, pending = concurrent.futures.wait(
                        pending, timeout=0.25,
                        return_when=concurrent.futures.FIRST_COMPLETED)

                    _watchdog()

                    for future in done:
                        i, src = future_to_src[future]
                        rel = os.path.relpath(src, values['INPUT_FOLDER'])
                        try:
                            result = future.result()
                        except concurrent.futures.CancelledError:
                            continue
                        except concurrent.futures.process.BrokenProcessPool as exc:
                            # A worker died (OOM killer, segfault) and CPython
                            # failed every outstanding future, running and merely
                            # queued alike.  Being "in flight" does not mean this
                            # file caused the death: with N workers the others
                            # were mid-file on healthy meshes and are equally
                            # innocent.  So retry once — only a file that dies on
                            # two independent pools is treated as the culprit.
                            #
                            # A file the watchdog killed is the exception: it is
                            # known guilty, and a retry would simply burn TIMEOUT
                            # seconds again to reach the same kill.  Report it
                            # straight away, naming the real cause rather than
                            # the generic out-of-memory guess below.
                            if rel in _timed_out.values():
                                result = {'status': 'interrupted', 'rel': rel,
                                          'reason': f'exceeded the {_fix.TIMEOUT}s '
                                                    'per-file timeout — raise Timeout '
                                                    'or decimate this file further',
                                          'stdout': '', 'stderr': ''}
                            elif _attempts.get(src, 0) < 1:
                                _attempts[src] = _attempts.get(src, 0) + 1
                                _to_retry.append(src)
                                continue
                            result = {'status': 'interrupted', 'rel': rel,
                                      'reason': 'killed a worker twice (likely out '
                                                'of memory) — needs more RAM or '
                                                'fewer workers',
                                      'stdout': '', 'stderr': ''}
                        except Exception as exc:
                            result = {'status': 'failed', 'rel': rel,
                                      'reason': str(exc), 'stdout': '', 'stderr': ''}
                        # Drop the bulky Blender output before retaining the result.
                        results_log.append((rel, _trim_result(result)))
                        with counts_lock:
                            s = result['status']
                            if s in counts:
                                counts[s] += 1
                            else:
                                counts['failed'] += 1
                        done_count[0] += 1
                        progress.update(task, advance=1)

                    # A worker freed up — submit whatever now fits.  If the
                    # pool is broken, stop draining and let the restart path
                    # below rebuild it.
                    if not _fill():
                        break

                # The inner loop ended.  If a worker death broke the pool and
                # left files unrun, build a replacement pool and resubmit them —
                # one oversized mesh should cost its own file, not every file
                # queued behind it.
                if ((_to_retry or _queue) and not _interrupted.is_set()
                        and _pool_generation[0] < _MAX_POOL_RESTARTS):
                    _pool_generation[0] += 1
                    # Send the interrupted files to the BACK of the queue, not
                    # the front.  They go back through _fill() so the same memory
                    # throttling applies, but retrying them immediately would put
                    # the heavy file that just killed a worker straight back
                    # alongside the next-heaviest ones — the exact collision that
                    # broke the pool.  Deferring them lets the smaller remaining
                    # work drain first, so a retry runs with the queue nearly
                    # empty and the most memory available.
                    _size_of = {p: n for n, p in _sized}
                    for _s in _to_retry:
                        _queue.append((_size_of.get(_s, 0), _s))
                    _n_back, _to_retry = len(_to_retry), []
                    _abandon_pool(_pool)
                    _pool = concurrent.futures.ProcessPoolExecutor(
                        max_workers=n_workers,
                        initializer=_fix._worker_init,
                        initargs=(worker_status, 10, _worker_mem_limit(n_workers)),
                    )
                    _fix.log_step('(pool)', f"worker died — restarted pool "
                                            f"(#{_pool_generation[0]}); "
                                            f"{_n_back} file(s) requeued, "
                                            f"{len(_queue)} still pending")
                    pending = set()
                    _fill()
                    continue

                # Nothing left to retry (or budget spent) — done.
                break

            # Files that repeatedly killed a worker, plus anything still
            # unretried after the restart budget is spent.
            for _n, _src in _gave_up:
                _rel = os.path.relpath(_src, values['INPUT_FOLDER'])
                results_log.append((_rel, {'status': 'interrupted', 'rel': _rel,
                                           'detail': f'{_n:,} tris — killed a worker '
                                                     'repeatedly; needs more memory'}))
                with counts_lock:
                    counts['interrupted'] += 1
                done_count[0] += 1
                progress.update(task, advance=1)
            for _src in _to_retry:
                _rel = os.path.relpath(_src, values['INPUT_FOLDER'])
                results_log.append((_rel, {'status': 'interrupted', 'rel': _rel,
                                           'detail': 'not processed — pool broke '
                                                     'repeatedly'}))
                with counts_lock:
                    counts['interrupted'] += 1
                done_count[0] += 1
                progress.update(task, advance=1)

            # Final render pass.
            layout['header'].update(_header_panel(progress))
            layout['workers'].update(_workers_panel())
            layout['summary'].update(_summary_panel())
            layout['log'].update(_log_panel())
    finally:
        # Same hazard as the restart path: if a worker was SIGKILLed, a direct
        # shutdown() blocks on the dead process's orphaned queue lock — and this
        # runs on Ctrl-C, which is precisely when that has happened.
        _abandon_pool(_pool)
        try:
            _mgr.shutdown()
        except Exception:
            pass

    # Print final summary outside of Live — this is the first point at which
    # console output is actually visible, so the interrupt is reported here
    # rather than from the signal handler (todo H3).
    if _interrupted.is_set():
        console.rule('[bold yellow]Interrupted[/bold yellow]')
        console.print()
        n_unfinished = total - done_count[0]
        console.print('[yellow]Run stopped early — workers and Blender '
                      'subprocesses have been terminated.[/yellow]')
        if n_unfinished > 0:
            console.print(f'[dim]{n_unfinished} of {total} file(s) were not processed. '
                          f'Re-run to continue where this left off.[/dim]')
    else:
        console.rule('[bold cyan]Done[/bold cyan]')
    console.print()
    t = Table(box=box.SIMPLE, show_header=True, header_style='bold')
    for col, style in [('OK','green'),('OPEN','yellow'),('SKIP','dim'),
                       ('FAIL','red'),('CORRUPT','red'),('UNREPAIRED','magenta'),
                       ('INTERRUPT','yellow')]:
        t.add_column(col, style=style, justify='right', width=12)
    t.add_row(str(counts['ok']), str(counts['open']), str(counts['skip']),
              str(counts['failed']), str(counts['corrupt']),
              str(counts['unrepaired']), str(counts['interrupted']))
    console.print(t)

    # List non-ok files (from the retained tail — see _RESULTS_KEPT).
    n_listed = 0
    for rel, r in results_log:
        if r['status'] not in ('ok', 'skip'):
            console.print(f"  [{r['status'].upper()}] {rel}")
            n_listed += 1
    n_notable = counts['open'] + counts['failed'] + counts['corrupt'] + counts['unrepaired']
    if n_notable > n_listed:
        console.print(f"  [dim]… and {n_notable - n_listed} earlier result(s) — "
                      f"see the full log.[/dim]")

    console.print()
    # Heavily-decimated files worth a look before printing (data lines only —
    # the file starts with '#' comment headers).
    try:
        with open(_fix.REVIEW_FILE) as _rf:
            _review = [ln for ln in _rf if ln.strip() and not ln.startswith('#')]
    except OSError:
        _review = []
    # Files whose worker died before reporting — killed by the OOM killer or a
    # library segfault.  These never produce a normal result, so they would
    # otherwise vanish from every count.
    try:
        _died = [r for r in _fix.read_summary() if r['status'] == 'started']
    except Exception:
        _died = []
    # A row stuck at 'started' only says the worker never reported back — it does
    # not say why.  The watchdog's own kills land here too, and reporting those
    # as "likely out of memory" sends you hunting a memory problem that does not
    # exist.  A .timeout.stl marker next to the output is the discriminator: the
    # watchdog writes it, a crash cannot.
    def _was_timed_out(rel):
        try:
            _d = _fix.output_path(os.path.join(values['INPUT_FOLDER'], rel),
                                  values['OUTPUT_SUFFIX'], values['INPUT_FOLDER'])
            return os.path.exists(os.path.splitext(_d)[0] + '.timeout.stl')
        except Exception:
            return False
    _timed_kills = [r for r in _died if _was_timed_out(r['file'])]
    _crashed = [r for r in _died if r not in _timed_kills]
    if _crashed:
        console.print(f"[bold red]{len(_crashed)} file(s) killed a worker "
                      f"before completing[/bold red] — crash or out of memory:")
        for _r in _crashed[:10]:
            console.print(f"  [red]{_r['file']}[/red] [dim]({_r['path']})[/dim]")
        if len(_crashed) > 10:
            console.print(f"  [dim]… and {len(_crashed) - 10} more[/dim]")
        console.print(f"  [dim]{_fix.SUMMARY_FILE}[/dim]")
        console.print()
    # Files the watchdog killed for exceeding the per-file limit.  These are
    # counted under INTERRUPT, so name them here — otherwise the one thing that
    # distinguishes them from a Ctrl-C is buried in the step log.
    # Two sources, because a timed-out file may never produce a result at all:
    # the watchdog SIGKILLs the worker, so the row can be left at 'started' and
    # only the .timeout.stl marker records what happened.
    _timeouts = [r[0] for r in results_log
                 if r[1].get('status') == 'interrupted'
                 and 'timeout' in str(r[1].get('reason', '')).lower()]
    _timeouts += [r['file'] for r in _timed_kills if r['file'] not in _timeouts]
    if _timeouts:
        console.print(f"[bold yellow]{len(_timeouts)} file(s) hit the "
                      f"{_fix.TIMEOUT}s per-file timeout[/bold yellow] "
                      f"— killed mid-repair:")
        for _rel in _timeouts[:10]:
            console.print(f"  [yellow]{_rel}[/yellow]")
        if len(_timeouts) > 10:
            console.print(f"  [dim]… and {len(_timeouts) - 10} more[/dim]")
        console.print("  [dim]Marked .timeout.stl in the output folder and skipped "
                      "on future runs — delete the marker to retry, or raise "
                      "Timeout.[/dim]")
        console.print()
    if _review:
        console.print(f"[yellow]{len(_review)} file(s) decimated "
                      f"{_fix.REVIEW_RATIO:g}x or more[/yellow] — check thin walls "
                      f"and connector holes:")
        console.print(f"  [dim]{_fix.REVIEW_FILE}[/dim]")
        console.print()
    # Files whose bounding box moved during repair.  Observational only — no
    # policy is applied — so a full-collection run can show how often this
    # happens and by how much before deciding what to do about it.
    try:
        _drifted = [r for r in _fix.read_summary() if r.get('bbox_drift')]
    except Exception:
        _drifted = []
    if _drifted:
        console.print(f"[bold yellow]{len(_drifted)} file(s) changed dimensions "
                      f"during repair[/bold yellow] — worth a look; this catches "
                      f"both lost geometry and removed artifacts:")
        for _r in _drifted[:15]:
            console.print(f"  [yellow]{_r['file']}[/yellow]")
            console.print(f"    [dim]{_r['bbox_drift']}[/dim]")
        if len(_drifted) > 15:
            console.print(f"  [dim]… and {len(_drifted) - 15} more[/dim]")
        console.print(f"  [dim]Full list in {os.path.basename(_fix.SUMMARY_FILE)} "
                      f"(bbox_drift column).[/dim]")
        console.print()
    console.print(f"Total time: [cyan]{_mmss(time.monotonic() - _run_started)}[/cyan]"
                  f"  [dim](mm:ss)[/dim]")
    console.print(f"Log: [dim]{_fix.LOG_FILE}[/dim]")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    # ── Find / choose config file ──────────────────────────────────────────
    cfg_files = _find_cfg_files()

    if not cfg_files:
        cfg_path = _cfg_path('default.fixcfg')
        cfg = dict(CFG_DEFAULTS)
        _write_cfg(cfg_path, cfg)
        console.print(f"[dim]No .fixcfg found — created [cyan]{cfg_path.name}[/cyan] with defaults.[/dim]")
    elif len(cfg_files) == 1:
        cfg_path = cfg_files[0]
        cfg = _read_cfg(cfg_path)
        console.print(f"[dim]Loaded [cyan]{cfg_path.name}[/cyan][/dim]")
    else:
        console.rule('[bold cyan]Multiple .fixcfg files found[/bold cyan]')
        for i, p in enumerate(cfg_files, 1):
            console.print(f"  [cyan]{i}[/cyan]  {p.name}")
        choice = Prompt.ask('Which config to load?', default='1')
        try:
            cfg_path = cfg_files[int(choice) - 1]
        except (ValueError, IndexError):
            console.print('[red]Invalid choice, using first.[/red]')
            cfg_path = cfg_files[0]
        cfg = _read_cfg(cfg_path)
        console.print(f"[dim]Loaded [cyan]{cfg_path.name}[/cyan][/dim]")

    console.print()

    # ── Config screen ──────────────────────────────────────────────────────
    cfg = run_config_screen(cfg)
    _write_cfg(cfg_path, cfg)  # save any edits back

    values = _cfg_to_values(cfg)

    # ── Validate input folder ──────────────────────────────────────────────
    if not os.path.isdir(values['INPUT_FOLDER']):
        console.print(f"[red]Input folder not found:[/red] {values['INPUT_FOLDER']}")
        sys.exit(1)

    if not _fix.shutil.which(_fix.BLENDER):
        console.print(f"[red]Blender not found on PATH.[/red] Install it or set BLENDER_BIN.")
        sys.exit(1)

    # ── Collect files ──────────────────────────────────────────────────────
    files = _fix.collect_stl_files(values['INPUT_FOLDER'], values['RECURSIVE'])
    # Smallest first, with sizes retained: run_progress_screen uses them to
    # decide how many workers may run concurrently as the meshes grow.
    # Drop files that already have an output or an indicator, before any worker
    # is started — otherwise each one costs a process dispatch just to be told
    # it was already handled.
    _found_total = len(files)
    files, _already = _fix.partition_already_done(files, values['INPUT_FOLDER'],
                                                  values['OUTPUT_SUFFIX'])
    if _already:
        _by_reason = {}
        for _p, _why in _already:
            _by_reason[_why] = _by_reason.get(_why, 0) + 1
        _order = [('fixed', 'already fixed', 'green'),
                  ('open', 'left with open edges', 'yellow'),
                  ('unrepaired', 'unrepaired', 'magenta'),
                  ('timeout', 'timed out before', 'yellow'),
                  ('failed', 'previously failed', 'red'),
                  ('broken', 'bad mesh data', 'red')]
        _parts = [f"[{c}]{_by_reason[k]} {label}[/{c}]"
                  for k, label, c in _order if k in _by_reason]
        console.print(f"[dim]Skipping {len(_already)} of {_found_total} file(s) "
                      f"from previous runs:[/dim] " + ", ".join(_parts))
        _retryable = sum(_by_reason.get(k, 0)
                         for k in ('failed', 'unrepaired', 'open', 'timeout'))
        if _retryable:
            console.print(f"[dim]  {_retryable} can be retried — delete the matching "
                          f".failed/.unrepaired/.open/.timeout .stl in the output "
                          f"folder.[/dim]")

    _sized_files = _fix.measure_files(files)
    files = [p for _, p in _sized_files]
    if not files:
        if _already:
            console.print(f"[green]Nothing to do — all {_found_total} file(s) "
                          f"already processed.[/green]")
        else:
            console.print(f"[yellow]No STL files found in:[/yellow] {values['INPUT_FOLDER']}")
        sys.exit(0)

    # WORKERS=0 means auto: pick a ceiling from cores and the memory budget,
    # sized against the heavy end of this particular collection.  Per-file
    # admission still lowers it further for individual large meshes.
    if values['WORKERS'] <= 0:
        values['WORKERS'] = _fix.auto_worker_count(_sized_files)
        _budget = _fix._run_memory_budget()
        console.print(f"[dim]Workers: auto -> [cyan]{values['WORKERS']}[/cyan] "
                      f"({os.cpu_count()} cores, "
                      f"{_budget/1024**3:.0f} GB budget)[/dim]")

    # Never start more workers than there are files.  A worker that never gets
    # work still pays ~80 MB to import numpy/pymeshlab/pymeshfix, so on a run
    # with three files left over from a previous pass the extras are pure waste.
    # Applies to a hand-set WORKERS too, not just the auto value.
    if values['WORKERS'] > len(files):
        _asked = values['WORKERS']
        values['WORKERS'] = max(1, len(files))
        console.print(f"[dim]Workers: {_asked} -> [cyan]{values['WORKERS']}[/cyan] "
                      f"(only {len(files)} file(s) to process)[/dim]")

    # Copy companion files before starting workers.
    companions = _fix.collect_companion_files(values['INPUT_FOLDER'], values['RECURSIVE'])
    if companions:
        copied = skipped = 0
        for src, dst in companions:
            if os.path.exists(dst):
                skipped += 1
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            _fix.shutil.copy2(src, dst)
            copied += 1
        if copied:
            console.print(f"[dim]Copied {copied} companion file(s)"
                          + (f" ({skipped} already present)" if skipped else "") + "[/dim]")

    console.print(f"[cyan]{len(files)}[/cyan] STL file(s) found. Starting {values['WORKERS']} worker(s)…")
    console.print()
    time.sleep(0.5)  # brief pause so user can read the message

    # ── Run ────────────────────────────────────────────────────────────────
    run_progress_screen(values, files, cfg, sized=_sized_files)


if __name__ == '__main__':
    main()
