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
    ('WORKERS',       'Workers',           int,   'Parallel worker processes'),
    ('TIMEOUT',       'Timeout (s)',       int,   'Per-file timeout before killing Blender'),
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

    console.print("Enter a field number to edit, [green]s[/green] to start, [red]q[/red] to quit.")
    console.print()

    while True:
        choice = Prompt.ask('Choice', default='s').strip().lower()
        if choice == 'q':
            console.print('Bye.')
            sys.exit(0)
        if choice == 's':
            break
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
    'failed':     '[bold red]FAILED[/bold red]',
}

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
        if result.get('split'):
            return f'[green]OK[/green] [dim]SPLIT({result["split"]})+MERGE[/dim]'
        if result.get('bypassed'):
            return '[green]OK[/green] [dim]clean copy[/dim]'
        if result.get('pymeshfix'):
            return '[green]OK[/green] [dim]+pymeshfix[/dim]'
    return _STATUS_STYLE.get(s, s)


def _worker_mem_limit(n_workers):
    """Per-worker RLIMIT_AS in bytes, or 0 to leave the address space uncapped.

    run.sh exports WORKER_MEM_MAX (bytes) derived from the cgroup MemoryMax, so
    each worker gets a fair share with headroom left for the parent and Blender.
    A worker that exceeds its share raises MemoryError and fails that one file,
    instead of the cgroup OOM-killer picking a victim at random — which could be
    the parent, orphaning every worker (todo L3)."""
    raw = os.environ.get('WORKER_MEM_MAX', '').strip()
    if not raw:
        return 0
    try:
        total = int(raw)
    except ValueError:
        return 0
    if total <= 0 or n_workers <= 0:
        return 0
    # Reserve ~25% of the budget for the parent process and Blender children,
    # which are separate processes and not covered by a worker's RLIMIT_AS.
    return int(total * 0.75 / n_workers)


def run_progress_screen(values, files, cfg):
    """Drive the worker pool and display live progress."""
    n_workers = values['WORKERS']
    total = len(files)

    # Apply values to the module globals so workers pick them up.
    _fix.INPUT_FOLDER  = values['INPUT_FOLDER']
    _fix.OUTPUT_SUFFIX = values['OUTPUT_SUFFIX']
    _fix.MERGE_DIST    = values['MERGE_DIST']
    _fix.WORKERS       = values['WORKERS']
    _fix.TIMEOUT       = values['TIMEOUT']
    _fix.MAX_FACES     = values['MAX_FACES']
    _fix.RECURSIVE     = values['RECURSIVE']

    # Prepare log file.
    log_dir = os.path.dirname(_fix.LOG_FILE)
    os.makedirs(log_dir, exist_ok=True)
    open(_fix.LOG_FILE, 'w').close()

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
    counts = {'ok': 0, 'open': 0, 'skip': 0, 'failed': 0, 'corrupt': 0, 'unrepaired': 0}
    counts_lock = threading.Lock()
    done_count  = [0]

    def _make_layout():
        layout = Layout()
        layout.split_column(
            Layout(name='header', size=4),
            Layout(name='workers', size=n_workers + 4),
            Layout(name='summary', size=6),
            Layout(name='log'),
        )
        return layout

    def _header_panel(progress):
        return Panel(progress, title='[bold cyan]STL Batch Fix[/bold cyan]', border_style='cyan')

    def _workers_panel():
        t = Table(box=box.SIMPLE, show_header=True, header_style='dim')
        t.add_column('Worker', style='dim',    width=9)
        t.add_column('PID',    style='dim',    width=8)
        t.add_column('File',   style='white',  ratio=1)
        t.add_column('Time',   style='yellow', width=8)
        try:
            active = dict(worker_status)
        except Exception:
            active = {}
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
                t.add_row(f'{slot}', '', '[dim]idle[/dim]', '')
            else:
                pid, info = entry
                t.add_row(f'{slot}', str(pid), info['rel'],
                          f"+{now - info['started']:.0f}s")
        return Panel(t, title='[bold]Workers[/bold]', border_style='blue')

    def _summary_panel():
        t = Table(box=box.SIMPLE, show_header=True, header_style='bold')
        for col, style in [('OK','green'),('OPEN','yellow'),('SKIP','dim'),
                           ('FAIL','red'),('CORRUPT','red'),('UNREPAIRED','magenta')]:
            t.add_column(col, style=style, justify='right', width=10)
        with counts_lock:
            t.add_row(str(counts['ok']), str(counts['open']), str(counts['skip']),
                      str(counts['failed']), str(counts['corrupt']), str(counts['unrepaired']))
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
    try:
        with Live(layout, console=console, refresh_per_second=4, screen=True):
            future_to_src = {_pool.submit(_fix.process_file_safe, src): (i, src)
                             for i, src in enumerate(files)}
            pending = set(future_to_src)

            while pending and not _interrupted.is_set():
                layout['header'].update(_header_panel(progress))
                layout['workers'].update(_workers_panel())
                layout['summary'].update(_summary_panel())
                layout['log'].update(_log_panel())

                done, pending = concurrent.futures.wait(
                    pending, timeout=0.25,
                    return_when=concurrent.futures.FIRST_COMPLETED)

                for future in done:
                    i, src = future_to_src[future]
                    rel = os.path.relpath(src, values['INPUT_FOLDER'])
                    try:
                        result = future.result()
                    except concurrent.futures.CancelledError:
                        continue
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

            # Final render pass.
            layout['header'].update(_header_panel(progress))
            layout['workers'].update(_workers_panel())
            layout['summary'].update(_summary_panel())
            layout['log'].update(_log_panel())
    finally:
        _pool.shutdown(wait=False, cancel_futures=True)
        _mgr.shutdown()

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
                       ('FAIL','red'),('CORRUPT','red'),('UNREPAIRED','magenta')]:
        t.add_column(col, style=style, justify='right', width=12)
    t.add_row(str(counts['ok']), str(counts['open']), str(counts['skip']),
              str(counts['failed']), str(counts['corrupt']), str(counts['unrepaired']))
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
    if not files:
        console.print(f"[yellow]No STL files found in:[/yellow] {values['INPUT_FOLDER']}")
        sys.exit(0)

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
    run_progress_screen(values, files, cfg)


if __name__ == '__main__':
    main()
