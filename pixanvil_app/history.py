"""Saving the whole history to one zip, and merging such a zip back in (see _plans/history_save_load.md).

A history zip holds every job, gallery item, preset and Branches record as plain JSON, every library file under
`library/` at its path relative to its library folder, and every saved source image under `inputs/`. Importing
merges: anything whose id is already present is skipped, so importing the same zip twice changes nothing."""
import json
import shutil
import threading
import uuid
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .media import safe_path

FORMAT = 1
RECORDS = ('manifest.json', 'jobs.json', 'assets.json', 'presets.json', 'branches.json')
BRANCH_TABLES = ('branch_layout', 'branch_names', 'branch_added', 'branch_labels', 'branch_pairs', 'branch_chains', 'branch_takes')
# Already compressed, so stored as they are; everything else is deflated.
MEDIA = {'.png', '.jpg', '.jpeg', '.webp', '.mp4'}
# A job still in one of these states is imported as cancelled, so imported work never starts rendering on its own.
FINISHED = ('complete', 'failed', 'cancelled', 'abandoned')
SETTINGS = ('library', 'comfy_url', 'ssh_host')
# What to do with an incoming saved chain whose id is already here with different steps or name.
CHAIN_CHOICES = ('copy', 'overwrite', 'skip')


class Cancelled(Exception):
    pass


class Task:
    """An export or import running in the background, with progress the page polls."""
    def __init__(self, kind):
        self.id = uuid.uuid4().hex
        self.kind = kind
        self.stage = 'Starting'
        self.files_done = self.files_total = self.bytes_done = self.bytes_total = 0
        self.result = None
        self.error = None
        self.file = None
        self.finished = False
        self.cancel_requested = False
        # An import with saved-chain conflicts waits here for a choice per chain (see choose_chains).
        self.conflicts = None
        self.choices = None
        self.chosen = threading.Event()

    def update(self, **values):
        for key, value in values.items():
            setattr(self, key, value)

    def check(self):
        if self.cancel_requested:
            raise Cancelled()

    def public(self):
        return {'id': self.id, 'kind': self.kind, 'stage': self.stage, 'files_done': self.files_done,
                'files_total': self.files_total, 'bytes_done': self.bytes_done, 'bytes_total': self.bytes_total,
                'result': self.result, 'error': self.error, 'finished': self.finished, 'download': self.file is not None,
                'conflicts': self.conflicts}


def export_history(task, store, settings, asset_files, inputs, path):
    """Write the history zip to `path`. `asset_files(asset)` lists an asset's files and `inputs` the saved source
    images. A main file or thumbnail missing from disk is listed in the manifest instead of failing the export."""
    task.update(stage='Collecting records')
    records = store.export_records()
    files, missing, assets = [], [], []
    for asset in records['assets']:
        root = Path(asset['root']).resolve()
        required = {safe_path(root, asset['path']), safe_path(root, asset['thumbnail'])}
        kept = []
        for file in asset_files(asset):
            relative = file.relative_to(root).as_posix()
            if file.is_file():
                files.append((file, 'library/'+relative)); kept.append(relative)
            elif file in required:
                missing.append(relative)
        assets.append({k: v for k, v in asset.items() if k != 'root'} | {'files': kept})
    files += [(file, 'inputs/'+file.name) for file in inputs]
    sizes = [file.stat().st_size for file, _ in files]
    manifest = {'format': FORMAT, 'app': 'pixanvil', 'exported': datetime.now(timezone.utc).isoformat(),
                'counts': {'jobs': len(records['jobs']), 'assets': len(assets), 'presets': len(records['presets']),
                           'inputs': len(inputs), 'files': len(files)},
                'bytes': sum(sizes), 'missing': missing, 'settings': {k: settings.get(k) for k in SETTINGS}}
    task.update(stage='Writing files', files_total=len(files), bytes_total=sum(sizes))
    try:
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in (('manifest.json', manifest), ('jobs.json', records['jobs']), ('assets.json', assets),
                               ('presets.json', records['presets']), ('branches.json', records['branches'])):
                archive.writestr(name, json.dumps(data, indent=1), compress_type=zipfile.ZIP_DEFLATED)
            for (file, name), size in zip(files, sizes):
                task.check()
                archive.write(file, name, compress_type=zipfile.ZIP_STORED if file.suffix.lower() in MEDIA else zipfile.ZIP_DEFLATED)
                task.update(files_done=task.files_done+1, bytes_done=task.bytes_done+size)
        # Read back what was written, so an archive is only offered once every entry is confirmed intact.
        with zipfile.ZipFile(path) as archive:
            check_contents(task, archive, 'Checking the archive')
    except BaseException:
        Path(path).unlink(missing_ok=True); raise
    task.update(stage='Ready to download', file=Path(path), result=manifest['counts'] | {'missing': missing, 'complete': not missing})


def check_names(names):
    """Refuse a zip with any entry that is not one of ours or could land outside its folder."""
    for name in names:
        if name.endswith('/'):
            continue
        parts = PurePosixPath(name).parts
        if '\\' in name or name.startswith('/') or '..' in parts or (parts and ':' in parts[0]):
            raise ValueError(f'The zip has an unsafe entry, {name}, so nothing was imported.')
        if name not in RECORDS and not (parts[0] == 'library' and len(parts) > 1) and not (parts[0] == 'inputs' and len(parts) == 2):
            raise ValueError(f'The zip has an entry PixAnvil does not know, {name}, so nothing was imported.')


def read_records(archive, names):
    if 'manifest.json' not in names:
        raise ValueError('This is not a PixAnvil history export: it has no manifest.json.')
    try:
        manifest = json.loads(archive.read('manifest.json'))
        records = {name: json.loads(archive.read(name+'.json')) if name+'.json' in names else default
                   for name, default in (('jobs', []), ('assets', []), ('presets', []), ('branches', {}))}
    except ValueError as exc:
        raise ValueError(f'The zip\'s records could not be read: {exc}') from exc
    if not isinstance(manifest, dict) or manifest.get('format') != FORMAT:
        raise ValueError(f'This export is format {manifest.get("format") if isinstance(manifest, dict) else "?"}; this PixAnvil reads format {FORMAT}.')
    for name in ('jobs', 'assets', 'presets'):
        if not isinstance(records[name], list) or not all(isinstance(r, dict) and isinstance(r.get('id'), str) for r in records[name]):
            raise ValueError(f'The zip\'s {name}.json is not a list of records with ids.')
    if not isinstance(records['branches'], dict):
        raise ValueError('The zip\'s branches.json is not a set of tables.')
    return manifest, records


def chain_conflicts(local, incoming):
    """Incoming saved chains whose id is already here with a different name or different steps, described for the
    person choosing. Identical chains are not conflicts: they are skipped like any record already present."""
    mine = {c['id']: c for c in local}
    found = []
    for row in incoming:
        here = mine.get(row.get('id')) if isinstance(row, dict) else None
        if not here:
            continue
        steps = json.loads(row['steps']) if isinstance(row.get('steps'), str) else row.get('steps')
        if (row.get('name'), steps) != (here['name'], here['steps']):
            found.append({'id': row['id'],
                          'local': {'name': here['name'], 'steps': len(here['steps']), 'updated': here['updated']},
                          'incoming': {'name': row.get('name'), 'steps': len(steps or []), 'updated': row.get('updated')}})
    return found


def choose_chains(task, conflicts, policy):
    """A choice for each conflicting chain: everything by `policy`, or, when it is 'ask', whatever the person sends
    to the waiting task. Waiting can be cancelled; nothing has been written yet."""
    if policy != 'ask':
        return {c['id']: policy for c in conflicts}
    task.update(stage='Waiting for your choices', conflicts=conflicts)
    while not task.chosen.wait(.25):
        task.check()
    task.check()
    task.update(conflicts=None, stage='Checking the zip')
    return task.choices


def same_file(path, info):
    """Whether a file on disk has exactly the contents of a zip entry: the same size and CRC-32."""
    if path.stat().st_size != info.file_size:
        return False
    crc = 0
    with path.open('rb') as handle:
        while chunk := handle.read(1 << 20):
            crc = zlib.crc32(chunk, crc)
    return crc == info.CRC


def check_contents(task, archive, stage):
    """Read every entry to the end, which makes zipfile check its CRC-32, so a damaged archive is found before it
    is relied on."""
    infos = [i for i in archive.infolist() if not i.is_dir()]
    task.update(stage=stage, files_total=len(infos), files_done=0, bytes_total=sum(i.file_size for i in infos), bytes_done=0)
    for info in infos:
        task.check()
        try:
            with archive.open(info) as entry:
                while entry.read(1 << 20):
                    pass
        except (zipfile.BadZipFile, OSError, EOFError, zlib.error) as exc:
            raise ValueError(f'{info.filename} in the zip is damaged ({exc}).') from exc
        task.update(files_done=task.files_done+1, bytes_done=task.bytes_done+info.file_size)


def unused(path):
    """`path` with .before-import added, numbered if that is taken too."""
    aside, n = path.with_name(path.name+'.before-import'), 2
    while aside.exists():
        aside, n = path.with_name(f'{path.name}.before-import-{n}'), n+1
    return aside


def import_history(task, store, library, inputs_dir, path, lock, chains='ask', verify=False):
    """Merge the history zip at `path` into this PixAnvil: new files into the `library` folder, new records in one
    transaction. Everything is checked before anything is written; a failure or cancel undoes the files written.
    `chains` says what to do with saved chains that differ from the ones here: 'ask', or one of CHAIN_CHOICES for all.
    With `verify`, nothing is written: every check runs, every file is read to confirm it is intact, and the result
    says what an import would do.

    A file already at a destination is kept when it is identical to the archived one. A different file there (left
    over from an earlier library, or damaged) is moved aside as `<name>.before-import` and the archived file written
    in its place; the result lists these."""
    task.update(stage='Checking the zip')
    library = Path(library).resolve()
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        check_names(names)
        manifest, records = read_records(archive, names)
        present = set(names)
        for asset in records['assets']:
            files = asset.get('files')
            if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
                raise ValueError(f'Item {asset["id"]} in the zip does not list its files.')
            for relative in files:
                safe_path(library, relative)
                if 'library/'+relative not in present:
                    raise ValueError(f'{relative} is listed in the zip but missing from it, so nothing was imported.')
        # An item whose main file was missing at export cannot be shown, so it is left out and reported. Paths are
        # compared with forward slashes: the zip uses them, while a library made on Windows records backslashes.
        unavailable = [a['id'] for a in records['assets'] if str(a.get('path')).replace('\\', '/') not in a['files']]
        records['assets'] = [a for a in records['assets'] if a['id'] not in unavailable]
        known = store.record_ids()
        new_assets = [a for a in records['assets'] if a['id'] not in known['assets']]
        incoming_chains = [r for r in records['branches'].get('branch_chains') or [] if isinstance(r, dict)]
        conflicts = chain_conflicts(store.branch_chains(), incoming_chains)
        # Every file the import may write, then which are already there unchanged (skipped) or different (replaced).
        candidates = [('library/'+relative, safe_path(library, relative)) for a in new_assets for relative in a['files']]
        candidates += [(name, safe_path(inputs_dir, name[7:])) for name in names if name.startswith('inputs/')]
        writes, replaced = [], []
        for name, target in candidates:
            if not target.exists():
                writes.append((name, target, False))
            elif not same_file(target, archive.getinfo(name)):
                writes.append((name, target, True)); replaced.append(name)
        new_inputs = sum(1 for name, _, _ in writes if name.startswith('inputs/'))
        need = sum(archive.getinfo(name).file_size for name, _, _ in writes)
        if verify:
            check_contents(task, archive, 'Checking files')
            would = {'jobs': sum(j['id'] not in known['jobs'] for j in records['jobs']), 'assets': len(new_assets),
                     'presets': sum(p['id'] not in known['presets'] for p in records['presets']), 'inputs': new_inputs}
            task.update(stage='Done', result={'verified': True, 'counts': manifest.get('counts') or {}, 'would_add': would,
                                              'would_replace': replaced, 'changed_chains': len(conflicts), 'bytes': need,
                                              'unavailable': unavailable, 'exported': manifest.get('exported'),
                                              'settings': manifest.get('settings') or {}, 'missing': manifest.get('missing') or []})
            return
        choices = choose_chains(task, conflicts, chains) if conflicts else {}
        library.mkdir(parents=True, exist_ok=True); inputs_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(library).free
        if need > free:
            raise ValueError(f'The import needs {need/1e9:.1f} GB but the library drive has {free/1e9:.1f} GB free.')
        task.update(stage='Writing files', files_total=len(writes), files_done=0, bytes_total=need, bytes_done=0)
        written, moved = [], []
        try:
            for name, target, replace in writes:
                task.check()
                target.parent.mkdir(parents=True, exist_ok=True)
                part = target.with_name(target.name+'.part')
                with archive.open(name) as source, part.open('wb') as out:
                    shutil.copyfileobj(source, out, 1 << 20)
                if replace:
                    aside = unused(target); target.replace(aside); moved.append((target, aside))
                part.replace(target); written.append(target)
                task.update(files_done=task.files_done+1, bytes_done=task.bytes_done+archive.getinfo(name).file_size)
            task.check()
            task.update(stage='Adding records')
            jobs = [imported_job(j, library) for j in records['jobs']]
            assets = [{k: v for k, v in a.items() if k != 'files'} | {'root': str(library), 'recipe': with_library(a.get('recipe'), library)}
                      for a in records['assets']]
            branches = {t: rows for t, rows in records['branches'].items() if t in BRANCH_TABLES and isinstance(rows, list)}
            # A copy is added under a new id and a marked name; an overwrite replaces the chain here.
            copies = {i for i, c in choices.items() if c == 'copy'}
            branches['branch_chains'] = [r | {'id': 'chain-'+uuid.uuid4().hex, 'name': f'{r.get("name")} (imported)'} if r['id'] in copies else r
                                         for r in incoming_chains if choices.get(r['id']) != 'skip']
            with lock:
                added = store.import_records(jobs, assets, records['presets'], branches,
                                             replace={'branch_chains': {i for i, c in choices.items() if c == 'overwrite'}})
        except BaseException:
            for file in written:
                file.unlink(missing_ok=True)
            for target, aside in moved:
                aside.replace(target)
            raise
    counts = {'jobs': len(jobs), 'assets': len(assets), 'presets': len(records['presets'])}
    result = {'added': added | {'inputs': new_inputs},
              'skipped': {k: counts[k]-added[k] for k in counts} | {'inputs': sum(n.startswith('inputs/') for n in names)-new_inputs},
              'chains': {c: sum(1 for v in choices.values() if v == c) for c in CHAIN_CHOICES},
              'replaced': [{'file': name, 'kept_as': aside.name} for name, (_, aside) in zip(replaced, moved)],
              'unavailable': unavailable, 'exported': manifest.get('exported'), 'settings': manifest.get('settings') or {},
              'missing': manifest.get('missing') or []}
    task.update(stage='Done', result=result)


def with_library(recipe, library):
    """A recipe with its library set to where its files now are. Its ComfyUI address stays, as history."""
    if isinstance(recipe, dict) and 'library' in recipe:
        return recipe | {'library': str(library)}
    return recipe


def imported_job(job, library):
    job = job | {'recipe': with_library(job.get('recipe'), library)}
    if job.get('state') not in FINISHED:
        job |= {'state': 'cancelled', 'error': 'Imported from a history export; not run again'}
    return job
