"""Branches: images are nodes, and image-to-video clips are the connections between them.

The graph is derived from what recipes already record -- a clip's start and end image, and the clip a
saved frame came from -- so anything made from Generate, the CLI or an agent appears without extra steps.
A clip end that has not been saved as an image yet is a *virtual* node named after its clip."""


def frame_of(asset):
    """'first' or 'last' for an image saved from a clip's frame, otherwise None.

    Frames saved before last frames existed carry no `frame` field; they are all first frames."""
    recipe = asset['recipe']
    if recipe.get('operation') != 'frame' or not recipe.get('parent_asset_id'):
        return None
    return recipe.get('frame', 'first')


def clip_seconds(recipe):
    if recipe.get('frames') and recipe.get('fps'):
        return round(recipe['frames']/recipe['fps'], 1)
    return None


def image_node(asset, added=False):
    return {'id': asset['id'], 'kind': 'image', 'asset_id': asset['id'], 'added': added,
            'prompt': asset['recipe'].get('prompt', ''), 'created': asset['created'],
            'thumbnail': f'/api/assets/{asset["id"]}/thumbnail', 'image': f'/api/assets/{asset["id"]}/file',
            'available': True}


def virtual_node(clip, which, available):
    frame = f'/api/branches/frames/{clip["id"]}/{which}'
    return {'id': f'{clip["id"]}:{which}', 'kind': 'virtual', 'clip_id': clip['id'], 'frame': which,
            'prompt': clip['recipe'].get('prompt', ''), 'created': clip['created'],
            'thumbnail': frame+'?thumbnail=true' if available else None, 'image': frame if available else None,
            'available': available}


def missing_node(asset_id, created):
    """An image a clip started or ended on that has since been deleted. Its clips stay in the tree."""
    return {'id': f'missing:{asset_id}', 'kind': 'missing', 'asset_id': asset_id, 'prompt': '',
            'created': created, 'thumbnail': None, 'image': None, 'available': False}


def pending_node(job, which):
    return {'id': f'job:{job["id"]}:{which}', 'kind': 'pending', 'prompt': job['recipe'].get('prompt', ''),
            'created': job['created'], 'thumbnail': None, 'image': None, 'available': False}


def build(assets, active_jobs, added, has_last_frame):
    """Every node and clip in Branches.

    `has_last_frame(clip)` reports whether a clip's last frame was saved beside it."""
    by_id = {a['id']: a for a in assets}
    frames = {}
    for asset in assets:
        which = frame_of(asset)
        if which:
            frames.setdefault((asset['recipe']['parent_asset_id'], which), asset)
    added = set(added)
    nodes, clips = {}, []

    def real(asset):
        nodes.setdefault(asset['id'], image_node(asset, asset['id'] in added))
        return asset['id']

    def recorded(asset_id, created):
        """The node for an image a recipe names: the image itself, or a placeholder if it was deleted."""
        if asset_id in by_id:
            return real(by_id[asset_id])
        node = nodes.setdefault(f'missing:{asset_id}', missing_node(asset_id, created))
        node['created'] = min(node['created'], created)
        return node['id']

    for clip in assets:
        recipe = clip['recipe']
        if recipe.get('media') != 'video':
            continue
        source_id, end_id = recipe.get('source_asset_id'), recipe.get('end_asset_id')
        first, last = frames.get((clip['id'], 'first')), frames.get((clip['id'], 'last'))
        # A text-to-video clip joins Branches once one of its frames has been saved as an image.
        if not (source_id or end_id or first or last):
            continue
        # The image a recipe records wins over a saved frame; a deleted one becomes a placeholder.
        if source_id:
            start_id = recorded(source_id, clip['created'])
        elif first:
            start_id = real(first)
        else:
            start_id = nodes.setdefault(f'{clip["id"]}:first', virtual_node(clip, 'first', True))['id']
        if end_id:
            target_id = recorded(end_id, clip['created'])
        elif last:
            target_id = real(last)
        else:
            target_id = nodes.setdefault(f'{clip["id"]}:last', virtual_node(clip, 'last', has_last_frame(clip)))['id']
        clips.append({'id': clip['id'], 'source': start_id, 'target': target_id, 'pending': False,
                      'seconds': clip_seconds(recipe), 'prompt': recipe.get('prompt', ''),
                      'workflow': recipe.get('workflow'), 'created': clip['created'],
                      'thumbnail': f'/api/assets/{clip["id"]}/thumbnail', 'file': f'/api/assets/{clip["id"]}/file'})

    # Clips still on their way are drawn dashed, ending at a placeholder until they finish.
    for job in active_jobs:
        recipe = job['recipe']
        if recipe.get('operation') != 'img2vid':
            continue
        source_id, end_id = recipe.get('source_asset_id'), recipe.get('end_asset_id')
        start_id = recorded(source_id, job['created']) if source_id else nodes.setdefault(f'job:{job["id"]}:start', pending_node(job, 'start'))['id']
        target_id = recorded(end_id, job['created']) if end_id else nodes.setdefault(f'job:{job["id"]}:end', pending_node(job, 'end'))['id']
        clips.append({'id': f'job:{job["id"]}', 'job_id': job['id'], 'source': start_id, 'target': target_id,
                      'pending': True, 'state': job['state'], 'seconds': clip_seconds(recipe),
                      'prompt': recipe.get('prompt', ''), 'workflow': recipe.get('workflow'), 'created': job['created'],
                      'thumbnail': None, 'file': None})

    # Images added by hand show as trees before they have any clips.
    for asset_id in added:
        if asset_id in by_id:
            real(by_id[asset_id])
    return nodes, clips


def trees(nodes, clips):
    """Split the graph into trees: groups of nodes linked by clips, ignoring direction.

    A tree is identified by its root, its earliest-created real image. When a clip joins two trees,
    the older tree's root stays the root, so its saved name carries over."""
    parent = {node_id: node_id for node_id in nodes}

    def find(node_id):
        while parent[node_id] != node_id:
            parent[node_id] = parent[parent[node_id]]
            node_id = parent[node_id]
        return node_id

    for clip in clips:
        parent[find(clip['source'])] = find(clip['target'])
    members = {}
    for node_id in nodes:
        members.setdefault(find(node_id), []).append(node_id)
    links = {}
    for clip in clips:
        links.setdefault(find(clip['source']), []).append(clip)
    result = []
    for key, node_ids in members.items():
        # The root is the earliest real image; a tree whose images were all deleted falls back to a placeholder.
        candidates = ([nodes[n] for n in node_ids if nodes[n]['kind'] == 'image']
                      or [nodes[n] for n in node_ids if nodes[n]['kind'] == 'missing'])
        if not candidates:
            continue
        root = min(candidates, key=lambda n: n['created'])
        result.append({'id': root['id'], 'root': root, 'nodes': node_ids, 'clips': links.get(key, [])})
    return result


def summary(tree, nodes, names):
    finished = [c for c in tree['clips'] if not c['pending']]
    return {'id': tree['id'], 'name': names.get(tree['id']) or tree['root']['prompt'][:80] or 'Untitled tree',
            'thumbnail': tree['root']['thumbnail'], 'nodes': sum(1 for n in tree['nodes'] if nodes[n]['kind'] != 'pending'),
            'clips': len(finished), 'pending': len(tree['clips'])-len(finished),
            'seconds': round(sum(c['seconds'] or 0 for c in finished), 1),
            'updated': max([nodes[n]['created'] for n in tree['nodes']]+[c['created'] for c in tree['clips']])}


def find_tree(all_trees, item_id):
    """The tree holding a node or a clip with this id."""
    return next((t for t in all_trees if item_id in t['nodes'] or any(c['id'] == item_id for c in t['clips'])), None)
