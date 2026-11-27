"""Saving the whole history to one zip, and merging such a zip back in (see _plans/history_save_load.md).

A history zip holds every job, gallery item, preset and Branches record as plain JSON, every library file under
`library/` at its path relative to its library folder, and every saved source image under `inputs/`. Importing
merges: anything whose id is already present is skipped, so importing the same zip twice changes nothing."""
import json
import shutil
import uuid
import zipfile
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

    def update(self, **values):
        for key, value in values.items():
            setattr(self, key, value)

    def check(self):
        if self.cancel_requested:
            raise Cancelled()

    def public(self):
        return {'id': self.id, 'kind': self.kind, 'stage': self.stage, 'files_done': self.files_done,
                'files_total': self.files_total, 'bytes_done': self.bytes_done, 'bytes_total': self.bytes_total,
                'result': self.result, 'error': self.error, 'finished': self.finished, 'download': self.file is not None}


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
    except BaseException:
        Path(path).unlink(missing_ok=True); raise
    task.update(stage='Ready to download', file=Path(path), result=manifest['counts'] | {'missing': missing})


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


def import_history(task, store, library, inputs_dir, path, lock):
    """Merge the history zip at `path` into this PixAnvil: new files into the `library` folder, new records in one
    transaction. Everything is checked before anything is written; a failure or cancel removes the files written."""
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
        writes = [('library/'+relative, safe_path(library, relative)) for a in new_assets for relative in a['files']]
        new_inputs = [n for n in names if n.startswith('inputs/') and not (inputs_dir/n[7:]).exists()]
        writes += [(name, safe_path(inputs_dir, name[7:])) for name in new_inputs]
        need = sum(archive.getinfo(name).file_size for name, _ in writes)
        library.mkdir(parents=True, exist_ok=True); inputs_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(library).free
        if need > free:
            raise ValueError(f'The import needs {need/1e9:.1f} GB but the library drive has {free/1e9:.1f} GB free.')
        task.update(stage='Writing files', files_total=len(writes), bytes_total=need)
        written = []
        try:
            for name, target in writes:
                task.check()
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    part = target.with_name(target.name+'.part')
                    with archive.open(name) as source, part.open('wb') as out:
                        shutil.copyfileobj(source, out, 1 << 20)
                    part.replace(target); written.append(target)
                task.update(files_done=task.files_done+1, bytes_done=task.bytes_done+archive.getinfo(name).file_size)
            task.check()
            task.update(stage='Adding records')
            jobs = [imported_job(j, library) for j in records['jobs']]
            assets = [{k: v for k, v in a.items() if k != 'files'} | {'root': str(library), 'recipe': with_library(a.get('recipe'), library)}
                      for a in records['assets']]
            branches = {t: rows for t, rows in records['branches'].items() if t in BRANCH_TABLES and isinstance(rows, list)}
            with lock:
                added = store.import_records(jobs, assets, records['presets'], branches)
        except BaseException:
            for file in written:
                file.unlink(missing_ok=True)
            raise
    counts = {'jobs': len(jobs), 'assets': len(assets), 'presets': len(records['presets'])}
    result = {'added': added | {'inputs': len(new_inputs)},
              'skipped': {k: counts[k]-added[k] for k in counts} | {'inputs': sum(n.startswith('inputs/') for n in names)-len(new_inputs)},
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
