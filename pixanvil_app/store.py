import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, created TEXT NOT NULL, updated TEXT NOT NULL,
                    state TEXT NOT NULL, prompt_id TEXT, recipe TEXT NOT NULL,
                    graph TEXT NOT NULL, error TEXT, outputs TEXT NOT NULL DEFAULT '[]');
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY, job_id TEXT, created TEXT NOT NULL,
                    root TEXT NOT NULL, path TEXT NOT NULL, thumbnail TEXT NOT NULL,
                    recipe TEXT NOT NULL, width INTEGER, height INTEGER,
                    favorite INTEGER NOT NULL DEFAULT 0, rating INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(job_id,path));
                CREATE TABLE IF NOT EXISTS presets (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, data TEXT NOT NULL);
                -- Branches: the graph itself is derived from recipes; these hold only what recipes cannot.
                CREATE TABLE IF NOT EXISTS branch_layout (
                    node_id TEXT PRIMARY KEY, x REAL NOT NULL, y REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS branch_names (
                    root_id TEXT PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS branch_added (
                    asset_id TEXT PRIMARY KEY, created TEXT NOT NULL);
                -- Keyed by image identity (see branches.label_key), so a label survives saving or deleting its image.
                CREATE TABLE IF NOT EXISTS branch_labels (
                    image_id TEXT PRIMARY KEY, label TEXT NOT NULL);
                -- What the user chose about the takes between two images: the preferred take and the connection label.
                CREATE TABLE IF NOT EXISTS branch_pairs (
                    start_key TEXT NOT NULL, end_key TEXT NOT NULL, preferred_clip TEXT,
                    label_mode TEXT NOT NULL DEFAULT 'stats', label TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(start_key, end_key));
                -- Chains saved from the chain builder. Steps are JSON: each step's clip and the image keys it joins, so
                -- a step whose take was deleted can use another take between the same two images.
                -- Each finished clip's take number between its two images, given once and kept, so deleting a take never
                -- renumbers the others. Rows stay after their clip is deleted, so a number is never given twice.
                CREATE TABLE IF NOT EXISTS branch_takes (
                    clip_id TEXT PRIMARY KEY, start_key TEXT NOT NULL, end_key TEXT NOT NULL, number INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS branch_chains (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, steps TEXT NOT NULL,
                    created TEXT NOT NULL, updated TEXT NOT NULL);
                -- Groups fold images into one block (see _plans/branches.md, Groups). Members are keyed by image identity,
                -- as labels are, and an image is in at most one group. parent_id is for nesting later; ports holds port
                -- names typed by the user, by the outside image's key.
                CREATE TABLE IF NOT EXISTS branch_groups (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, parent_id TEXT, collapsed INTEGER NOT NULL DEFAULT 0,
                    ports TEXT NOT NULL DEFAULT '{}', created TEXT NOT NULL, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS branch_group_members (
                    image_key TEXT PRIMARY KEY, group_id TEXT NOT NULL);
            ''')
            # When each item was first looked at; NULL marks a new result nobody has viewed yet. Items from before
            # this column existed count as seen.
            if 'seen' not in {r[1] for r in db.execute('PRAGMA table_info(assets)')}:
                db.execute('ALTER TABLE assets ADD COLUMN seen TEXT')
                db.execute('UPDATE assets SET seen=created')
            # A group's box (left, top, right, bottom, in saved positions), sized by the user; NULL until first drawn.
            if 'box' not in {r[1] for r in db.execute('PRAGMA table_info(branch_groups)')}:
                db.execute('ALTER TABLE branch_groups ADD COLUMN box TEXT')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        d = dict(row)
        for key in ('recipe', 'graph', 'outputs', 'data'):
            if key in d:
                d[key] = json.loads(d[key])
        return d

    def create_job(self, recipe, graph):
        job_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO jobs(id,created,updated,state,recipe,graph) VALUES(?,?,?,?,?,?)',
                       (job_id, now(), now(), 'queued', json.dumps(recipe), json.dumps(graph)))
        return self.job(job_id)

    def job(self, job_id):
        with self.connect() as db:
            return self.decode(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def jobs(self, limit=100):
        with self.connect() as db:
            return [self.decode(r) for r in db.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT ?', (limit,))]

    def active(self):
        with self.connect() as db:
            return [self.decode(r) for r in db.execute("SELECT * FROM jobs WHERE state IN ('queued','submitting','running','collecting','unknown') ORDER BY created")]

    def update_job(self, job_id, **values):
        if not set(values).issubset({'state','prompt_id','error','outputs','recipe','graph'}):
            raise ValueError('Invalid job update')
        for key in ('outputs','recipe','graph'):
            if key in values:
                values[key] = json.dumps(values[key])
        values['updated'] = now()
        with self.connect() as db:
            db.execute('UPDATE jobs SET ' + ','.join(k+'=?' for k in values) + ' WHERE id=?', (*values.values(), job_id))

    def add_asset(self, asset):
        """A job's result starts unseen. Anything made by hand (an import, a saved frame, a joined chain) was just
        looked at, so it starts seen."""
        created = now()
        with self.connect() as db:
            db.execute('''INSERT OR IGNORE INTO assets
                (id,job_id,created,root,path,thumbnail,recipe,width,height,seen) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (asset['id'],asset['job_id'],created,asset['root'],asset['path'],asset['thumbnail'],
                 json.dumps(asset['recipe']),asset['width'],asset['height'],None if asset['job_id'] else created))

    def mark_seen(self, ids=None):
        """Mark the items `ids`, or every item when None, as seen. Items already seen keep their first time."""
        with self.connect() as db:
            if ids is None:
                db.execute('UPDATE assets SET seen=? WHERE seen IS NULL', (now(),))
            elif ids:
                db.execute(f'UPDATE assets SET seen=? WHERE seen IS NULL AND id IN ({",".join("?"*len(ids))})', (now(), *ids))

    def unseen_counts(self):
        """How many results nobody has viewed yet: all of them, and the clips between two images (Branches)."""
        with self.connect() as db:
            total, clips = db.execute("SELECT COUNT(*), COALESCE(SUM(json_extract(recipe,'$.operation')='img2vid'),0) "
                                      'FROM assets WHERE seen IS NULL').fetchone()
        return {'total': total, 'clips': clips}

    # How the gallery groups items. A batch is everything one submission made; items made before batches were
    # recorded, or made alone, are their own group. A take set is the clips with the same start and end image,
    # the same pairs as Branches; anything else is its own group.
    GROUP_KEYS = {
        'batch': "COALESCE(json_extract(recipe,'$.batch'),id)",
        'takes': "CASE WHEN json_extract(recipe,'$.media')='video' AND json_extract(recipe,'$.source_asset_id') IS NOT NULL "
                 "AND json_extract(recipe,'$.end_asset_id') IS NOT NULL "
                 "THEN json_extract(recipe,'$.source_asset_id')||'>'||json_extract(recipe,'$.end_asset_id') ELSE id END",
    }

    def assets(self, q='', model='', offset=0, limit=60, favorites=False, media='', group=''):
        """A page of gallery items, newest first. With `group`, a page of groups instead: each item is the group's
        cover with a `group` field ({kind, key, count}). Filters pick the matching items, and a group is listed
        when any of its items match; its count is all of its items."""
        clauses, args = [], []
        if media:
            # Older and upscaled images carry no media field; only clips say 'video'.
            clauses.append("COALESCE(json_extract(recipe,'$.media'),'image')=?"); args.append(media)
        if q:
            clauses.append('recipe LIKE ?'); args.append('%'+q+'%')
        if model:
            clauses.append("json_extract(recipe,'$.model')=?"); args.append(model)
        if favorites:
            clauses.append('favorite=1')
        where = (' WHERE '+' AND '.join(clauses)) if clauses else ''
        if group:
            return self.grouped_assets(group, where, args, offset, limit)
        with self.connect() as db:
            total = db.execute('SELECT COUNT(*) FROM assets'+where,args).fetchone()[0]
            rows = db.execute('SELECT * FROM assets'+where+' ORDER BY created DESC LIMIT ? OFFSET ?', (*args,limit,offset))
            return {'items':[self.decode(r) for r in rows], 'total':total}

    def grouped_assets(self, group, where, args, offset, limit):
        key = self.GROUP_KEYS[group]
        with self.connect() as db:
            total = db.execute(f'SELECT COUNT(DISTINCT {key}) FROM assets'+where, args).fetchone()[0]
            keys = [r[0] for r in db.execute(f'SELECT {key} AS gkey, MAX(created) AS latest FROM assets'+where+
                                              ' GROUP BY gkey ORDER BY latest DESC LIMIT ? OFFSET ?', (*args,limit,offset))]
            members = {}
            if keys:
                marks = ','.join('?'*len(keys))
                for r in db.execute(f'SELECT *, {key} AS gkey FROM assets WHERE {key} IN ({marks}) ORDER BY created', keys):
                    row = self.decode(r); members.setdefault(row.pop('gkey'), []).append(row)
        numbers, pairs = (self.take_numbers()[0], self.branch_pairs()) if group == 'takes' else ({}, {})
        items = []
        for gkey in keys:
            found = members[gkey]
            cover = found[0]
            if group == 'takes' and len(found) > 1:
                # The take Branches plays for this pair: the chosen one, else the lowest-numbered.
                chosen = pairs.get(tuple(gkey.split('>', 1)), {}).get('preferred_clip')
                cover = next((a for a in found if a['id'] == chosen), None) or min(found, key=lambda a: (numbers.get(a['id'], 1e9), a['created']))
            items.append(cover | {'group': {'kind': group, 'key': gkey, 'count': len(found),
                                            'unseen': sum(a['seen'] is None for a in found)}})
        return {'items': items, 'total': total}

    def group_members(self, group, gkey):
        """Every item in one gallery group, oldest first."""
        key = self.GROUP_KEYS[group]
        with self.connect() as db:
            return [self.decode(r) for r in db.execute(f'SELECT * FROM assets WHERE {key}=? ORDER BY created', (gkey,))]

    def asset(self, asset_id):
        with self.connect() as db:
            return self.decode(db.execute('SELECT * FROM assets WHERE id=?',(asset_id,)).fetchone())

    def delete_job(self, job_id):
        """Remove a job and the gallery entries it produced. Returns the removed asset rows."""
        with self.connect() as db:
            removed = [self.decode(r) for r in db.execute('SELECT * FROM assets WHERE job_id=?', (job_id,))]
            db.execute('DELETE FROM assets WHERE job_id=?', (job_id,))
            db.execute('DELETE FROM jobs WHERE id=?', (job_id,))
            for asset in removed:
                self.forget_branch_node(db, asset['id'])
        return removed

    @staticmethod
    def forget_branch_node(db, asset_id):
        """Drop Branches data about a deleted asset, including the virtual frame nodes of a clip.

        A tree's name and the image's label are kept: the deleted image may still stand in the tree as a
        placeholder, which should keep both."""
        db.execute('DELETE FROM branch_layout WHERE node_id IN (?,?,?)', (asset_id, asset_id+':first', asset_id+':last'))
        db.execute('DELETE FROM branch_added WHERE asset_id=?', (asset_id,))

    def branch_positions(self):
        with self.connect() as db:
            return {r['node_id']:{'x':r['x'],'y':r['y']} for r in db.execute('SELECT * FROM branch_layout')}

    def set_branch_positions(self, positions):
        """Save node positions; a value of None forgets that node's position."""
        with self.connect() as db:
            for node_id, position in positions.items():
                if position is None:
                    db.execute('DELETE FROM branch_layout WHERE node_id=?', (node_id,))
                else:
                    db.execute('INSERT OR REPLACE INTO branch_layout(node_id,x,y) VALUES(?,?,?)',
                               (node_id, position['x'], position['y']))

    def move_branch_position(self, old_id, new_id):
        """A virtual node made real keeps its place under its new asset id."""
        with self.connect() as db:
            db.execute('UPDATE OR IGNORE branch_layout SET node_id=? WHERE node_id=?', (new_id, old_id))

    def branch_names(self):
        with self.connect() as db:
            return {r['root_id']:r['name'] for r in db.execute('SELECT * FROM branch_names')}

    def set_branch_name(self, root_id, name):
        with self.connect() as db:
            if name:
                db.execute('INSERT OR REPLACE INTO branch_names(root_id,name) VALUES(?,?)', (root_id, name))
            else:
                db.execute('DELETE FROM branch_names WHERE root_id=?', (root_id,))

    def branch_labels(self):
        with self.connect() as db:
            return {r['image_id']:r['label'] for r in db.execute('SELECT * FROM branch_labels')}

    def set_branch_label(self, image_id, label):
        with self.connect() as db:
            if label:
                db.execute('INSERT OR REPLACE INTO branch_labels(image_id,label) VALUES(?,?)', (image_id, label))
            else:
                db.execute('DELETE FROM branch_labels WHERE image_id=?', (image_id,))

    def branch_pairs(self):
        """{(start key, end key): {'preferred_clip', 'label_mode', 'label'}} for every pair with a saved choice."""
        with self.connect() as db:
            return {(r['start_key'], r['end_key']): {k: r[k] for k in ('preferred_clip', 'label_mode', 'label')}
                    for r in db.execute('SELECT * FROM branch_pairs')}

    def set_branch_pair(self, start_key, end_key, **values):
        """Save choices about a pair; a value of None leaves that choice as it was."""
        if not set(values).issubset({'preferred_clip', 'label_mode', 'label'}):
            raise ValueError('Invalid pair update')
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO branch_pairs(start_key,end_key) VALUES(?,?)', (start_key, end_key))
            for key, value in values.items():
                if value is not None:
                    db.execute(f'UPDATE branch_pairs SET {key}=? WHERE start_key=? AND end_key=?', (value, start_key, end_key))

    def take_numbers(self):
        """({clip id: take number}, {(start key, end key): highest number ever given})."""
        with self.connect() as db:
            rows = db.execute('SELECT * FROM branch_takes').fetchall()
        highest = {}
        for r in rows:
            key = (r['start_key'], r['end_key'])
            highest[key] = max(highest.get(key, 0), r['number'])
        return {r['clip_id']: r['number'] for r in rows}, highest

    def add_take_numbers(self, rows):
        """Keep new take numbers: (clip id, start key, end key, number). A clip already numbered keeps its number."""
        with self.connect() as db:
            db.executemany('INSERT OR IGNORE INTO branch_takes(clip_id,start_key,end_key,number) VALUES(?,?,?,?)', rows)

    def branch_chains(self):
        """Every saved chain, most recently saved first."""
        with self.connect() as db:
            return [self.decode_chain(r) for r in db.execute('SELECT * FROM branch_chains ORDER BY updated DESC, id')]

    def branch_chain(self, chain_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM branch_chains WHERE id=?', (chain_id,)).fetchone()
        return self.decode_chain(row) if row else None

    def save_branch_chain(self, chain_id, name, steps):
        """Create the chain, or replace the name and steps of a saved one."""
        stamp = now()
        with self.connect() as db:
            db.execute('INSERT INTO branch_chains(id,name,steps,created,updated) VALUES(?,?,?,?,?) '
                       'ON CONFLICT(id) DO UPDATE SET name=excluded.name, steps=excluded.steps, updated=excluded.updated',
                       (chain_id, name, json.dumps(steps), stamp, stamp))

    def delete_branch_chain(self, chain_id):
        with self.connect() as db:
            db.execute('DELETE FROM branch_chains WHERE id=?', (chain_id,))

    @staticmethod
    def decode_chain(row):
        return dict(row) | {'steps': json.loads(row['steps'])}

    def branch_groups(self):
        """Every group with its member image keys, oldest first."""
        with self.connect() as db:
            groups = {r['id']: dict(r) | {'collapsed': bool(r['collapsed']), 'ports': json.loads(r['ports']), 'members': [],
                                          'box': json.loads(r['box']) if r['box'] else None}
                      for r in db.execute('SELECT * FROM branch_groups ORDER BY created, id')}
            for row in db.execute('SELECT * FROM branch_group_members ORDER BY image_key'):
                if row['group_id'] in groups:
                    groups[row['group_id']]['members'].append(row['image_key'])
        return list(groups.values())

    def branch_group(self, group_id):
        return next((g for g in self.branch_groups() if g['id'] == group_id), None)

    @staticmethod
    def grouped_elsewhere(db, keys, group_id=None):
        """Which of `keys` are already members of a group other than `group_id`, as {key: group name}."""
        found = {}
        for key in keys:
            row = db.execute('SELECT g.id, g.name FROM branch_group_members m JOIN branch_groups g ON g.id=m.group_id '
                             'WHERE m.image_key=?', (key,)).fetchone()
            if row and row['id'] != group_id:
                found[key] = row['name']
        return found

    def create_branch_group(self, group_id, name, keys, box=None):
        """Make a group of these images. Raises ValueError naming the group of any image already in one."""
        stamp = now()
        with self.connect() as db:
            taken = self.grouped_elsewhere(db, keys)
            if taken:
                raise ValueError(taken)
            db.execute('INSERT INTO branch_groups(id,name,box,created,updated) VALUES(?,?,?,?,?)',
                       (group_id, name, json.dumps(box) if box else None, stamp, stamp))
            db.executemany('INSERT INTO branch_group_members(image_key,group_id) VALUES(?,?)', [(k, group_id) for k in keys])

    def update_branch_group(self, group_id, name=None, collapsed=None, ports=None, box=None, clear_box=False):
        """Change what is given. `clear_box` forgets the box, so the page fits a new one around the members."""
        values = {k: v for k, v in (('name', name), ('collapsed', None if collapsed is None else int(collapsed)),
                                     ('ports', None if ports is None else json.dumps(ports)),
                                     ('box', None if box is None else json.dumps(box))) if v is not None}
        if clear_box:
            values['box'] = None
        values['updated'] = now()
        with self.connect() as db:
            db.execute('UPDATE branch_groups SET '+','.join(k+'=?' for k in values)+' WHERE id=?', (*values.values(), group_id))

    def change_group_members(self, group_id, add=(), remove=()):
        """Add and remove members. Raises ValueError naming the group of any image to add that is in another group. A
        group left with no members is removed. Returns whether the group still exists."""
        with self.connect() as db:
            taken = self.grouped_elsewhere(db, add, group_id)
            if taken:
                raise ValueError(taken)
            db.executemany('INSERT OR IGNORE INTO branch_group_members(image_key,group_id) VALUES(?,?)', [(k, group_id) for k in add])
            db.executemany('DELETE FROM branch_group_members WHERE image_key=? AND group_id=?', [(k, group_id) for k in remove])
            db.execute('UPDATE branch_groups SET updated=? WHERE id=?', (now(), group_id))
            if not db.execute('SELECT 1 FROM branch_group_members WHERE group_id=?', (group_id,)).fetchone():
                db.execute('DELETE FROM branch_groups WHERE id=?', (group_id,))
                return False
        return True

    def delete_branch_group(self, group_id):
        """Ungroup: the group and its memberships go; the images and clips are untouched."""
        with self.connect() as db:
            db.execute('DELETE FROM branch_group_members WHERE group_id=?', (group_id,))
            db.execute('DELETE FROM branch_groups WHERE id=?', (group_id,))

    def branch_added(self):
        with self.connect() as db:
            return [r['asset_id'] for r in db.execute('SELECT asset_id FROM branch_added ORDER BY created')]

    def add_branch_image(self, asset_id):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO branch_added(asset_id,created) VALUES(?,?)', (asset_id, now()))

    def remove_branch_image(self, asset_id):
        with self.connect() as db:
            db.execute('DELETE FROM branch_added WHERE asset_id=?', (asset_id,))

    def all_assets(self):
        with self.connect() as db:
            return [self.decode(r) for r in db.execute('SELECT * FROM assets')]

    # History export and import (pixanvil_app/history.py). Records travel as plain JSON, not as this file, so an old
    # export still imports after the schema changes.
    BRANCH_TABLES = ('branch_layout', 'branch_names', 'branch_added', 'branch_labels', 'branch_pairs', 'branch_chains', 'branch_takes',
                     'branch_groups', 'branch_group_members')
    JOB_COLUMNS = ('id', 'created', 'updated', 'state', 'prompt_id', 'recipe', 'graph', 'error', 'outputs')
    ASSET_COLUMNS = ('id', 'job_id', 'created', 'root', 'path', 'thumbnail', 'recipe', 'width', 'height', 'favorite', 'rating')

    def export_records(self):
        """Every job, asset and preset, decoded, and every Branches row, by table."""
        with self.connect() as db:
            return {'jobs': [self.decode(r) for r in db.execute('SELECT * FROM jobs ORDER BY created')],
                    'assets': [self.decode(r) for r in db.execute('SELECT * FROM assets ORDER BY created')],
                    'presets': [self.decode(r) for r in db.execute('SELECT * FROM presets ORDER BY name')],
                    'branches': {t: [dict(r) for r in db.execute('SELECT * FROM '+t)] for t in self.BRANCH_TABLES}}

    def record_ids(self):
        with self.connect() as db:
            return {t: {r[0] for r in db.execute(f'SELECT id FROM {t}')} for t in ('jobs', 'assets', 'presets')}

    def import_records(self, jobs, assets, presets, branches, replace=None):
        """Insert imported records in one transaction, keeping their created times. A record whose id (or a Branches
        row whose key) is already present is skipped, so local values win, except rows of a table whose `id` is in
        `replace` ({table: ids}), which replace the local row. Returns how many of each were added."""
        replace = replace or {}
        def insert(db, table, columns, row):
            names = [c for c in columns if c in row]
            values = [json.dumps(row[c]) if isinstance(row[c], (dict, list)) else row[c] for c in names]
            verb = 'INSERT OR REPLACE' if row.get('id') in replace.get(table, ()) else 'INSERT OR IGNORE'
            return db.execute(f'{verb} INTO {table}({",".join(names)}) VALUES({",".join("?"*len(names))})', values).rowcount
        added = {'jobs': 0, 'assets': 0, 'presets': 0, 'branch_rows': 0}
        with self.connect() as db:
            for job in jobs:
                added['jobs'] += insert(db, 'jobs', self.JOB_COLUMNS, job)
            for asset in assets:
                # Imported items are history, not new results, so they arrive seen.
                added['assets'] += insert(db, 'assets', self.ASSET_COLUMNS+('seen',), asset | {'seen': asset.get('seen') or asset.get('created') or now()})
            for preset in presets:
                added['presets'] += insert(db, 'presets', ('id', 'name', 'data'), preset)
            # A replaced group takes the export's members, so its members here are cleared first.
            for group_id in replace.get('branch_groups', ()):
                db.execute('DELETE FROM branch_group_members WHERE group_id=?', (group_id,))
            for table, rows in branches.items():
                if table not in self.BRANCH_TABLES:
                    continue
                columns = [r[1] for r in db.execute(f'PRAGMA table_info({table})')]
                for row in rows:
                    if isinstance(row, dict):
                        added['branch_rows'] += insert(db, table, columns, row)
        return added

    def job_count(self):
        with self.connect() as db:
            return db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]

    def clear_history(self):
        """Remove every job, gallery entry and Branches record, keeping presets. Returns the removed asset rows."""
        with self.connect() as db:
            removed = [self.decode(r) for r in db.execute('SELECT * FROM assets')]
            db.execute('DELETE FROM assets')
            db.execute('DELETE FROM jobs')
            for table in self.BRANCH_TABLES:
                db.execute('DELETE FROM '+table)
        return removed

    def delete_asset(self, asset_id):
        with self.connect() as db:
            db.execute('DELETE FROM assets WHERE id=?', (asset_id,))
            self.forget_branch_node(db, asset_id)

    def input_in_use(self, name):
        """True when any remaining job still needs this saved source snapshot."""
        with self.connect() as db:
            return db.execute("SELECT 1 FROM jobs WHERE json_extract(recipe,'$.input_name')=? OR json_extract(recipe,'$.end_input_name')=? LIMIT 1",
                              (name, name)).fetchone() is not None

    def mark_asset(self, asset_id, favorite, rating):
        with self.connect() as db:
            db.execute('UPDATE assets SET favorite=?,rating=? WHERE id=?',(int(favorite),rating,asset_id))

    def presets(self):
        with self.connect() as db:
            return [self.decode(r) for r in db.execute('SELECT * FROM presets ORDER BY name')]

    def save_preset(self, name, data, preset_id=None):
        preset_id = preset_id or uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO presets(id,name,data) VALUES(?,?,?)',(preset_id,name,json.dumps(data)))
        return preset_id

    def delete_preset(self, preset_id):
        with self.connect() as db:
            db.execute('DELETE FROM presets WHERE id=?',(preset_id,))
