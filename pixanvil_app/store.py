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
            ''')

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
        with self.connect() as db:
            db.execute('''INSERT OR IGNORE INTO assets
                (id,job_id,created,root,path,thumbnail,recipe,width,height) VALUES(?,?,?,?,?,?,?,?,?)''',
                (asset['id'],asset['job_id'],now(),asset['root'],asset['path'],asset['thumbnail'],
                 json.dumps(asset['recipe']),asset['width'],asset['height']))

    def assets(self, q='', model='', offset=0, limit=60, favorites=False, media=''):
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
        with self.connect() as db:
            total = db.execute('SELECT COUNT(*) FROM assets'+where,args).fetchone()[0]
            rows = db.execute('SELECT * FROM assets'+where+' ORDER BY created DESC LIMIT ? OFFSET ?', (*args,limit,offset))
            return {'items':[self.decode(r) for r in rows], 'total':total}

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

        A tree's name is kept: the deleted image may still stand in the tree as a placeholder, and the
        tree should keep its name."""
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

    def job_count(self):
        with self.connect() as db:
            return db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]

    def clear_history(self):
        """Remove every job, gallery entry and Branches record, keeping presets. Returns the removed asset rows."""
        with self.connect() as db:
            removed = [self.decode(r) for r in db.execute('SELECT * FROM assets')]
            db.execute('DELETE FROM assets')
            db.execute('DELETE FROM jobs')
            for table in ('branch_layout', 'branch_names', 'branch_added'):
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
