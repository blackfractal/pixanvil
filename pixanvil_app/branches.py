"""Branches: images are nodes, and image-to-video clips are the connections between them.

The graph is derived from what recipes already record -- a clip's start and end image, and the clip a
saved frame came from -- so anything made from Generate, the CLI or an agent appears without extra steps.
A clip end that has not been saved as an image yet is a *virtual* node named after its clip."""


def frame_asset_id(clip_id, which):
    """The id of the image saved from a clip's first or last frame. Fixed by clip and frame, never by pixels,
    so a deleted frame can be recognised and saving it again restores the same identity."""
    return f'frame-{clip_id}-{which}'


def parse_frame_asset_id(asset_id):
    """(clip id, 'first' or 'last') for an id made by frame_asset_id, otherwise None."""
    if not asset_id.startswith('frame-'):
        return None
    clip_id, _, which = asset_id[len('frame-'):].rpartition('-')
    return (clip_id, which) if clip_id and which in ('first', 'last') else None


def split_asset_id(clip_id, frame):
    """The id of the image a split is: the frame's number in its clip, so splitting there again finds it."""
    return f'frame-{clip_id}-at{int(frame)}'


def frame_of(asset):
    """'first' or 'last' for an image saved from a clip's frame, the frame's number for a split, otherwise None.

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
            'frame_asset_id': frame_asset_id(clip['id'], which),
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
    frames, splits = {}, {}
    for asset in assets:
        which = frame_of(asset)
        if isinstance(which, int):
            splits.setdefault(asset['recipe']['parent_asset_id'], []).append(asset)
        elif which:
            frames.setdefault((asset['recipe']['parent_asset_id'], which), asset)
    added = set(added)
    nodes, clips = {}, []

    def real(asset):
        nodes.setdefault(asset['id'], image_node(asset, asset['id'] in added))
        return asset['id']

    def virtual(clip, which):
        available = which == 'first' or has_last_frame(clip)
        return nodes.setdefault(f'{clip["id"]}:{which}', virtual_node(clip, which, available))['id']

    def recorded(asset_id, created):
        """The node for an image a recipe names: the image itself, or what stands in for it if it was deleted.

        A deleted frame goes back to being its clip's virtual node, the same node the clip itself now ends
        on, so a chain joined through that frame stays one tree. Any other deleted image is a placeholder."""
        if asset_id in by_id:
            return real(by_id[asset_id])
        frame = parse_frame_asset_id(asset_id)
        if frame and by_id.get(frame[0], {}).get('recipe', {}).get('media') == 'video':
            return virtual(by_id[frame[0]], frame[1])
        node = nodes.setdefault(f'missing:{asset_id}', missing_node(asset_id, created))
        node['created'] = min(node['created'], created)
        return node['id']

    for clip in assets:
        recipe = clip['recipe']
        # A joined chain is a finished product made from clips, not a step in a tree.
        if recipe.get('media') != 'video' or recipe.get('operation') == 'sequence':
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
            start_id = virtual(clip, 'first')
        if end_id:
            target_id = recorded(end_id, clip['created'])
        elif last:
            target_id = real(last)
        else:
            target_id = virtual(clip, 'last')
        made = {'pending': False, 'prompt': recipe.get('prompt', ''), 'workflow': recipe.get('workflow'), 'fps': recipe.get('fps'),
                'seen': clip.get('seen') is not None, 'file': f'/api/assets/{clip["id"]}/file'}
        clips.append(made | {'id': clip['id'], 'source': start_id, 'target': target_id, 'seconds': clip_seconds(recipe),
                             'created': clip['created'], 'thumbnail': f'/api/assets/{clip["id"]}/thumbnail'})
        # Each split adds two pieces of the whole take, through the split's image: start to F, and F to the end.
        # A piece is the take's file played over a frame range; F is the last frame of one and the first of the other.
        for split in sorted(splits.get(clip['id'], []), key=lambda s: s['recipe']['frame']):
            r = split['recipe']
            frame, fps, total = r['frame'], r['fps'], r['clip_frames']
            middle = real(split)
            nodes[middle]['split'] = {'clip': clip['id'], 'frame': frame, 'frames': total, 'fps': fps}
            for side, source, target, low, high in (('before', start_id, middle, 0, frame+1), ('after', middle, target_id, frame, total)):
                clips.append(made | {
                    'id': f'piece:{split["id"]}:{side}', 'source': source, 'target': target, 'created': split['created'],
                    'seconds': round((high-low)/fps, 1), 'fps': fps,
                    'thumbnail': f'/api/assets/{clip["id"] if side == "before" else split["id"]}/thumbnail',
                    'piece': {'clip': clip['id'], 'split': split['id'], 'start_frame': low, 'end_frame': high,
                              'start': low/fps, 'end': high/fps}})

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


def label_key(node):
    """The image a node stands for, which stays the same through the node's life: a clip frame not yet saved
    already carries the id it will be saved under, and a deleted image's placeholder keeps the image's id.
    Pending nodes have none."""
    return node.get('asset_id') or node.get('frame_asset_id')


def pair_keys(nodes, clip):
    """The two image keys a clip joins, as stored for its pair. A pending end has no key."""
    return label_key(nodes[clip['source']]), label_key(nodes[clip['target']])


def mark_takes(nodes, clips, pairs, numbers, highest):
    """Number the takes between each pair of nodes and mark the preferred one, in place.

    Takes are the finished clips with the same start and end node. A take keeps the number it was first given
    (`numbers`, by clip id); a new take gets one more than the highest number its pair ever had (`highest`, by
    pair keys), so numbers never shift or repeat. Returns the new numbers to keep, as
    (clip id, start key, end key, number). The preferred take is the one saved in `pairs` while it still exists,
    otherwise the lowest-numbered take, so a new take never changes a chain on its own. Every clip of a pair,
    pending ones included, carries the pair's label choice."""
    groups, new = {}, []
    for clip in clips:
        groups.setdefault((clip['source'], clip['target']), []).append(clip)
    for group in groups.values():
        takes = sorted((c for c in group if not c['pending']), key=lambda c: (c['created'], c['id']))
        keys = pair_keys(nodes, takes[0] if takes else group[0])
        stored = pairs.get(keys, {})
        top = max([highest.get(keys, 0)]+[numbers[c['id']] for c in takes if c['id'] in numbers])
        for clip in takes:
            if clip['id'] not in numbers:
                top += 1
                numbers[clip['id']] = top
                new.append((clip['id'], *keys, top))
        takes.sort(key=lambda c: numbers[c['id']])
        preferred = next((c for c in takes if c['id'] == stored.get('preferred_clip')), takes[0] if takes else None)
        for clip in takes:
            clip.update(take=numbers[clip['id']], takes=len(takes), preferred=clip is preferred)
        for clip in group:
            clip.update(pair_label_mode=stored.get('label_mode') or 'stats', pair_label=stored.get('label') or '')
    return new


def name_key(tree, nodes, names):
    """Where a tree's name is stored: under the oldest node that has a saved name, else under the root.

    Names are keyed by image identity (see label_key), so a tree keeps its name when its root image is
    deleted, and after a merge the older tree's name wins."""
    ordered = sorted((nodes[n] for n in tree['nodes']), key=lambda n: n['created'])
    return next((label_key(n) for n in ordered if label_key(n) in names), label_key(tree['root']))


def summary(tree, nodes, names):
    # Pieces are parts of takes already counted, so clips and footage count whole takes only.
    finished = [c for c in tree['clips'] if not c['pending'] and not c.get('piece')]
    return {'id': tree['id'], 'name': names.get(name_key(tree, nodes, names)) or tree['root']['prompt'][:80] or 'Untitled tree',
            'thumbnail': tree['root']['thumbnail'], 'nodes': sum(1 for n in tree['nodes'] if nodes[n]['kind'] != 'pending'),
            'clips': len(finished), 'pending': sum(1 for c in tree['clips'] if c['pending']),
            'unseen': sum(1 for c in finished if not c['seen']),
            'connections': len({(c['source'], c['target']) for c in tree['clips'] if not c['pending']}),
            'seconds': round(sum(c['seconds'] or 0 for c in finished), 1),
            'updated': max([nodes[n]['created'] for n in tree['nodes']]+[c['created'] for c in tree['clips']])}


def find_tree(all_trees, item_id):
    """The tree holding a node or a clip with this id."""
    return next((t for t in all_trees if item_id in t['nodes'] or any(c['id'] == item_id for c in t['clips'])), None)


def chain_errors(tree, clip_ids):
    """Why these clips, in this order, are not a chain that plays as one shot, or None if they are."""
    clips = {c['id']: c for c in tree['clips']}
    if not clip_ids:
        return 'Select at least one clip'
    for clip_id in clip_ids:
        if clip_id not in clips:
            return 'A clip in the chain is not in this tree'
        if clips[clip_id]['pending']:
            return 'A clip in the chain is still rendering'
    for before, after in zip(clip_ids, clip_ids[1:]):
        if clips[before]['target'] != clips[after]['source']:
            return 'Each clip must start on the image the previous one ends on'
    return None


def file_name(text):
    """Text made safe as a file name: letters, digits, dot, dash, underscore and space are kept."""
    safe = ''.join(ch if ch.isalnum() or ch in '._- ' else '_' for ch in text).strip(' .')
    return safe[:60] or 'image'


def chain_steps(tree, nodes, clip_ids):
    """A chain as saved: each step's clip and the image keys it joins, so a step whose take is deleted later can
    use another take between the same two images. The clips must be in the tree (see chain_errors)."""
    clips = {c['id']: c for c in tree['clips']}
    def step(i):
        found = {'clip': i, 'start_key': label_key(nodes[clips[i]['source']]), 'end_key': label_key(nodes[clips[i]['target']])}
        # A piece also records its take and exact frames, which say what played even if the split is removed later.
        piece = clips[i].get('piece')
        if piece:
            found |= {'take': piece['clip'], 'start_frame': piece['start_frame'], 'end_frame': piece['end_frame']}
        return found
    return [step(i) for i in clip_ids]


def tree_chains(tree, nodes, chains):
    """The saved chains that belong to a tree: those with a clip or an image in it, even after some were deleted."""
    clip_ids = {c['id'] for c in tree['clips']}
    keys = {label_key(nodes[n]) for n in tree['nodes']} - {None}
    return [c for c in chains
            if any(s['clip'] in clip_ids or s.get('start_key') in keys or s.get('end_key') in keys for s in c['steps'])]


def keyframe_names(tree, nodes):
    """Default names for a tree's images, used where no label was typed: Keyframe 1, Keyframe 2, ... in the order
    the images were made, so a new image gets the next number. Removing an image entirely renumbers the later ones."""
    ordered = sorted((nodes[n] for n in tree['nodes']), key=lambda n: (n['created'], n['id']))
    return {node['id']: f'Keyframe {number}' for number, node in enumerate(ordered, 1)}


def node_names(tree, nodes, labels):
    """Each node's name: its typed label, else its keyframe name."""
    defaults = keyframe_names(tree, nodes)
    return {n: labels.get(label_key(nodes[n])) or defaults[n] for n in tree['nodes']}


def plan_export(tree, nodes, selected, labels):
    """What Export selected writes: images and clips named after node names (labels or keyframe names), and a graph.

    Returns (images, clips, graph). `images` holds (node, name) for nodes with a picture; `clips` holds
    (clip, name) for finished clips. A clip is named after the nodes it joins, so the preferred take between
    two nodes is `A1-B1.mp4` and the other takes are `A1-B1-take<n>.mp4`, n being the take's number.
    Clips must have been through mark_takes."""
    selected = set(selected)
    in_tree = [nodes[n] for n in tree['nodes']]
    shown = node_names(tree, nodes, labels)
    # Every node gets a name, selected or not, because clip names use the names of both their ends.
    names, used = {}, set()
    for node in sorted(in_tree, key=lambda n: (n['created'], n['id'])):
        base = file_name(shown[node['id']])
        name, count = base, 2
        while name.lower() in used:
            name, count = f'{base}-{count}', count+1
        used.add(name.lower())
        names[node['id']] = name
    images, clips, skipped = [], [], []
    for node in in_tree:
        if node['id'] not in selected:
            continue
        if node['kind'] in ('image', 'virtual') and node['available']:
            images.append((node, names[node['id']]))
        else:
            reason = {'missing': 'deleted image', 'pending': 'still rendering'}.get(node['kind'], 'no saved frame')
            skipped.append({'id': node['id'], 'label': shown[node['id']], 'reason': reason})
    for clip in sorted(tree['clips'], key=lambda c: c['created']):
        if clip['id'] not in selected:
            continue
        if clip['pending']:
            skipped.append({'id': clip['id'], 'reason': 'still rendering'})
            continue
        suffix = '' if clip['preferred'] else f'-take{clip["take"]}'
        clips.append((clip, f'{names[clip["source"]]}-{names[clip["target"]]}{suffix}'))
    graph = {
        'nodes': [{'id': n['id'], 'label': shown[n['id']], 'file': f'images/{name}.png',
                   'prompt': n['prompt']} for n, name in images],
        'clips': [{'id': c['id'], 'from': c['source'], 'to': c['target'],
                   'from_label': shown[c['source']], 'to_label': shown[c['target']],
                   'file': f'clips/{name}.mp4', 'seconds': c['seconds'], 'prompt': c['prompt'],
                   'take': c['take'], 'takes': c['takes'], 'preferred': c['preferred'],
                   'connection_label': c['pair_label'] if c['pair_label_mode'] == 'custom' else ''} for c, name in clips],
        'skipped': skipped,
    }
    return images, clips, graph


def split_users(split_id, clips, chains):
    """What keeps a split from being removed: clips and jobs that start or end on its image, other than its own two
    pieces, and saved chains through it. Returns readable reasons; none means it can be removed."""
    users = [c for c in clips if split_id in (c.get('source_key'), c.get('target_key'))
             and (c.get('piece') or {}).get('split') != split_id]
    reasons = []
    finished = sum(1 for c in users if not c['pending'])
    if finished:
        reasons.append('1 clip starts or ends on it' if finished == 1 else f'{finished} clips start or end on it')
    if len(users) > finished:
        waiting = len(users)-finished
        reasons.append('1 clip on the way uses it' if waiting == 1 else f'{waiting} clips on the way use it')
    named = [c['name'] for c in chains if any(split_id in (s.get('start_key'), s.get('end_key')) for s in c['steps'])]
    if named:
        reasons.append('saved chains go through it: '+', '.join(named))
    return reasons


def tree_groups(tree, nodes, groups):
    """The groups with members in this tree, as drawn: members as node ids, and a port for every outside node with clips
    into the group (inputs) and every outside node the group leads to (outputs), with any name typed for it."""
    by_key = {label_key(nodes[n]): n for n in tree['nodes'] if label_key(nodes[n])}
    found = []
    for group in groups:
        members = [by_key[k] for k in group['members'] if k in by_key]
        if not members:
            continue
        inside = set(members)
        def ports(outside):
            return [{'node': n, 'key': label_key(nodes[n]), 'name': group['ports'].get(label_key(nodes[n]) or '', '')}
                    for n in sorted(outside, key=lambda n: (nodes[n]['created'], n))]
        found.append({'id': group['id'], 'name': group['name'], 'collapsed': group['collapsed'], 'members': members, 'box': group.get('box'),
                      'inputs': ports({c['source'] for c in tree['clips'] if c['target'] in inside and c['source'] not in inside}),
                      'outputs': ports({c['target'] for c in tree['clips'] if c['source'] in inside and c['target'] not in inside})})
    return found
