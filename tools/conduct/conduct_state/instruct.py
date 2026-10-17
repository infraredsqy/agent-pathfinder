"""Standing instructions: the account and everything about "what is visible from where".

Owns the account's vocabulary and read/write helpers, the position rules that decide which
entries a reader sees from where the pipeline stands, and the `instruction` subcommands
(add / list / resolve) with their CLI dispatcher.
"""
import json
import sys
from pathlib import Path

from .store import (_atomic_write, _ledger_lock, _now, _read_json, _read_state, ledger_dir_for)
from .pipeline_loader import _load_pipeline


# ── Standing instructions ────────────────────────────────────────────────────
# One append-only account, one writer (this CLI). Any session may ADD one; the visibility of what it
# added is decided by the entry's own `scope`, and the session that closes a node may RESOLVE one.
# Three scopes; the defaults lean the "noisy" way on purpose (see the module notes below).

#: The vocabulary `scope` is written in. `entry_scope()` over in conduct_pipeline.py is the one reader
#: of an entry's scope, and this module loads it lazily (`_scope_rules`): two copies of that
#: judgement is how one entry ends up read two ways.
VALID_SCOPES = ('global', 'cycle', 'node')
VALID_TARGETS = ('stage', 'boundary')

#: The account has two states, and one of them is written by the owner alone: `live` (in force) and
#: `withdrawn` (archived -- still on the account, no longer read). Nothing else ends an entry: where
#: it can be seen from is derived on every read from its scope and the bound node/cycle, so a scoped
#: entry goes out of sight on its own (`_visible_from_here`). A record an older tool left carrying
#: another word in `state` is not one of these; it is read as archived, and nothing writes it back
#: (`_retired`).
VALID_INSTRUCTION_STATES = ('live', 'withdrawn')

_SCOPE_RULES = None


def _scope_rules():
    """`conduct_pipeline.py` loaded by path: it owns the reading of the older `lifetime` field.

    Deliberately not a copy: `entry_scope()` there is the single place that decides what an entry
    with `once` / `run` / `forever` -- or with neither field -- is scoped as, and this command has to
    read the account exactly the way the pipeline reads it.
    """
    global _SCOPE_RULES
    if _SCOPE_RULES is None:
        _SCOPE_RULES = _load_pipeline('conduct_pipeline_scopes')
    return _SCOPE_RULES


def _scope_from_flag(raw: str | None) -> str | None:
    """A `--scope` value as a scope, or None when it is not one of the three."""
    if raw is None:
        return None
    return raw if raw in _scope_rules().SCOPES else None


def _instructions_path(project: str | None = None) -> Path:
    """The one call site that decides where the account is read from and written to.

    The account is part of the ledger, so it is `<project>/.pf/instructions.json`.
    """
    return ledger_dir_for(project) / 'instructions.json'


def _read_instructions(project: str | None = None) -> dict:
    """The account, exactly as it is on disk.

    Reading resolves nothing and writes nothing: an entry's scope is read through `entry_scope`
    (`conduct_pipeline`, the one place that judgement lives) wherever it is actually needed. A read must
    never rewrite the workspace it read.
    """
    data = _read_json(_instructions_path(project), 'instructions.json')
    if not data:
        return {'instructions': []}
    data.setdefault('instructions', [])
    return data


def _write_instructions(data: dict, project: str | None = None) -> None:
    _atomic_write(_instructions_path(project), data)


def _retired(entry: dict) -> bool:
    """True unless the entry is in force: `live` is the only state that is read.

    `withdrawn` is the archive, and a record an older tool wrote with any other word in `state` is
    archived the same way -- out of force, still on the account, never rewritten.
    """
    return not _is_live(entry)


def _is_live(entry: dict) -> bool:
    return entry.get('state', 'live') == 'live'


# ── where the pipeline stands, and what is visible from there ──

def _holding_ids(cycle: dict) -> list:
    """这条线此刻握着的那组节点（`holding` 的唯一读法）。"""
    holding = cycle.get('holding')
    if isinstance(holding, list):
        return [item['id'] if isinstance(item, dict) else item for item in holding if item]
    return []


def _cycle_of_node(active: list, node: str) -> str | None:
    """节点反查 cycle：这个节点正拿在哪一条 active cycle 手里？

    节点 id 在整个 DAG 里唯一，cycle 的记录里写着它此刻握着的那组节点 —— 于是「读者站在哪条线上」
    这件事，在两条 active cycle 并排时也能从盘上读出来，不必猜一个「当前 cycle」。只有恰好一条
    命中才算数：两条线都握着同一个节点时（同一条活被两条线同时开跑），答案不止一个，那还是给 None。
    """
    hits = [c['id'] for c in active if node in _holding_ids(c)]
    return hits[0] if len(hits) == 1 else None


def _position(project: str | None = None, node: str | None = None) -> tuple:
    """`(current cycle id, current node id)` -- what `cycle current` reads, or `(None, None)`.

    Read straight off `state.json` rather than through the module-level `LEDGER_DIR`, so `--project`
    decides which workspace answers. When the answer is unclear -- no active cycle, two of them --
    both halves come back None, and the caller falls back to judging each entry by the status of what
    it is bound to.

    `node` is the reader saying where it stands (`instruction list --node <id>`). It is the one fact
    that survives two active cycles side by side: the pointer in state.json cannot say which of the
    two lines is asking, but a node id can be traced back to the cycle holding it, so the position is
    determinate again. When that lookup lands on nothing, both halves stay None exactly as before.
    """
    ledger = ledger_dir_for(project)
    state = _read_json(ledger / 'state.json', 'state.json') or {}
    active = [c for c in state.get('cycles', []) if c.get('status') == 'active']
    if len(active) == 1:
        cycle = active[0]
    elif node:
        cycle_id = _cycle_of_node(active, node)
        if cycle_id is None:
            return (None, None)
        cycle = next(c for c in active if c['id'] == cycle_id)
    else:
        return (None, None)
    holding = _holding_ids(cycle)
    if node and node in holding:
        return (cycle['id'], node)
    return (cycle['id'], holding[0] if holding else None)


def _cycle_statuses(project: str | None = None) -> dict:
    """{cycle id: status}, for the entries whose cycle is no longer the current one.

    This reader goes straight to the file (it takes a project root rather than the process cwd), so
    it reads the word as written: every comparison here is against `active`.
    """
    state = _read_json(ledger_dir_for(project) / 'state.json', 'state.json') or {}
    return {c.get('id'): c.get('status')
            for c in state.get('cycles', []) if c.get('id')}


def _dag_nodes(project: str | None = None) -> dict:
    """The DAG's nodes: the one fact a `node` entry is measured against (is that node completed?).

    The graph is part of the ledger, so it is `<project>/.pf/ideas.json`.
    """
    return (_read_json(ledger_dir_for(project) / 'ideas.json', 'ideas.json') or {}).get('nodes') or {}


def _visible_from_here(project: str | None, entries: list, node: str | None = None) -> list:
    """The entries a reader standing where the pipeline stands now should see.

    This is the whole of the instruction channel: no session is handed its constraints, every
    session reads them, so the rule has to be the one the reader can act on. `global` is always
    visible; a `cycle` entry only while its cycle is the current one; a `node` entry only while its
    node is the current one. An entry with nothing to compare -- no binding, or no pointer to
    compare it against -- is visible, and when the pointer is missing each entry is judged by the
    status of what it is bound to instead. Every unanswerable case leans visible: a constraint a
    reader cannot see is a constraint that does not exist for it.

    One case overrides that lean, and only one: two or more active cycles at once. Then "its cycle
    is still active" is true of every line the project is running, so a `cycle` entry is visible
    only where the reader's own line is pinned down -- by the pointer when there is a single active
    cycle, or by the node the reader reports (`--node`). With neither, the entry is read out to
    nobody: showing it to both lines is how one line's requirement lands on the other, which is the
    failure this rule exists for. The reader is told, rather than left with silence
    (`_ambiguous_cycle_view`). `node` entries keep their own rule (their node unfinished), because
    that is the life of a node entry and not a question about where the reader stands.
    """
    rules = _scope_rules()
    cur_cycle, cur_node = _position(project, node=node)
    statuses, nodes = _cycle_statuses(project), _dag_nodes(project)
    ambiguous = cur_cycle is None and sum(1 for s in statuses.values() if s == 'active') > 1
    out = []
    for entry in entries:
        if not _is_live(entry):
            continue
        scope = rules.entry_scope(entry)
        if scope == 'global':
            out.append(entry)
            continue
        if scope == 'cycle' and entry.get('cycle'):
            if cur_cycle is not None:
                if entry['cycle'] == cur_cycle:
                    out.append(entry)
                continue
            if ambiguous:
                continue
            bound_status = statuses.get(entry['cycle'])
            if bound_status in (None, 'active'):
                out.append(entry)
            continue
        if scope == 'node' and entry.get('node'):
            if cur_node is not None:
                if entry['node'] == cur_node:
                    out.append(entry)
                continue
            if (nodes.get(entry['node']) or {}).get('status') != 'completed':
                out.append(entry)
            continue
        # Nothing to measure against, or a scope this reader does not know: show it.
        out.append(entry)
    return out


def _ambiguous_cycle_view(project: str | None, entries: list,
                          node: str | None = None) -> int:
    """几条 active cycle 并排、位置又钉不下来时，`cycle` 作用域的条目两边都不念。

    返回 active cycle 的条数（0 = 这件事跟这次读无关），只用来在默认视图「看不见指令」的时候
    补一句解套的话：沉默必须能被读成「说一句你站在哪条线上」，而不是「这里没有约束」。
    """
    if _position(project, node=node)[0] is not None:
        return 0
    active = [cid for cid, st in _cycle_statuses(project).items() if st == 'active']
    if len(active) < 2:
        return 0
    rules = _scope_rules()
    if not any(_is_live(e) and rules.entry_scope(e) == 'cycle'
               for e in entries):
        return 0
    return len(active)


def _fold(entry: dict) -> dict:
    """One entry as the reader-facing view carries it: the sentence, where it is visible, how it is
    delivered. Ids and bookkeeping stay in the account."""
    return {
        'id': entry.get('id'),
        'text': entry.get('text', ''),
        'scope': _scope_rules().entry_scope(entry),
        'target': entry.get('target'),
    }


def _instruct_empty_lines(total: int, scope: str | None, global_total: int) -> list:
    """Nothing matched one of the ACCOUNT views (`--scope` / `--all` / `--live`), or the account is
    empty. Say which of the four situations this is.

    The distinctions matter to a reader: "no instructions" is a green light, while "the account
    exists but nothing matched" is a different thing and must not be described as the first. Getting
    this wrong in the reassuring direction is exactly how a live instruction reads as absent. The
    vocabulary is the account's: an entry is `global` / `cycle` / `node`, and the older word for the
    first of those has no place in an answer.
    """
    if not total:
        return ['本项目没有任何指令记录。',
                '这不是错误：可以照常开工。']
    if scope:
        return [f'没有 scope={scope} 且仍在生效的指令。',
                f'（指令记录里有 {total} 条记录。）']
    if not global_total:
        return ['本项目没有任何全局指令。',
                f'（指令记录里有 {total} 条记录，但都不是全局的。）']
    return ['当前没有任何仍在生效的指令记录。',
            f'（指令记录里有 {total} 条记录，都不在生效。把一条指令归档只有机主能做。）']


def _nothing_visible_lines(total: int) -> list:
    """The default view found nothing, on a project that has an account.

    This is the dangerous silence: a session that reads it must be able to tell "there
    is nothing for me here" from "there is nothing at all", and the second sentence is what keeps it
    from reading as a green light.
    """
    return ['当前这一步没有看得见的指令。',
            f'（指令记录里有 {total} 条记录：它们要么绑在别的 cycle 或别的节点上，要么已经不在生效'
            f'状态。刚开一轮、指针还没写下来时，换个时候再读一次。）']


#: How a scope is spelled for the reader. The vocabulary is English in the account and in the flag
#: (`--scope global|cycle|node`); the sentences a session reads are Chinese, and none of them
#: carries the word `cycle`, which is also how the cycle ids in this account start.
SCOPE_WORDS = {'global': '全局（任何位置都算数）',
               'cycle': '本次运行期间（这次运行结束就不再算数）',
               'node': '仅当前节点（这个节点做完就不再算数）'}


def _render_constraints(items: list) -> None:
    """The reader-facing rendering: what a session reads off `instruction list`.

    Text, where it is visible from, and how it is delivered. The bookkeeping (ids, and which cycle
    and node the entry was bound to) stays in the account and in `--json`: a reader has no use for
    it, and showing it invites treating a past reading as a reason the instruction might not apply
    now.
    """
    print(f'本项目有 {len(items)} 条在当前这一步看得见的指令：')
    print()
    for i, it in enumerate(items, 1):
        print(f'{i}. {it["text"]}')
        if it.get('scope'):
            print(f'   可见性：{SCOPE_WORDS.get(it["scope"], it["scope"])}')
        deliver = {'stage': '所有节点', 'boundary': '仅在收尾时读'}.get(it.get('target'))
        if deliver:
            print(f'   范围：{deliver}')
        print()


def _new_entry_binding(scope: str, cycle: str | None, node: str | None,
                       state: dict) -> tuple:
    """`(cycle, node)` for one new entry -- what its scope will be measured against.

    Two sources, in this order: what the caller named (`--cycle` / `--node`), then the pipeline's own
    position (which exists only while exactly one cycle is active). `scope` decides what "neither"
    means. A `cycle` / `node` entry with no binding is read as "nothing to compare against" --
    visible everywhere and never out of sight (`_visible_from_here`'s last line) -- so those two
    scopes are refused here rather than written that way. `global` needs no binding; it still records
    where it was said, for the account's own sake.

    The two names are checked against the state before they are written: a mistyped `--cycle` would
    otherwise make an entry nobody can ever read, which is the same silence in the other direction.
    """
    active = [c for c in state['cycles'] if c.get('status') == 'active']
    known = [c['id'] for c in state['cycles'] if c.get('id')]
    if cycle is not None and cycle not in known:
        print(f'Error: --cycle {cycle!r} is not in state.json; the account has {known or "no cycle"}',
              file=sys.stderr)
        sys.exit(1)
    cycle_id = cycle if cycle is not None else (active[0]['id'] if len(active) == 1 else None)

    if node is not None:
        nodes = _dag_nodes()
        if nodes and node not in nodes:
            print(f'Error: --node {node!r} is not in the DAG', file=sys.stderr)
            sys.exit(1)
        node_id = node
    else:
        node_id = None

    if cycle_id:
        cycle = next((c for c in state['cycles'] if c.get('id') == cycle_id), None) or {}
        if node_id is None:
            holding = _holding_ids(cycle)
            node_id = holding[0] if holding else None

    if scope == 'cycle' and not cycle_id:
        print(f'Error: a cycle-scoped entry needs a line to bind to. '
              f'{len(active)} active cycle(s) {[c["id"] for c in active]}, so name it: `--cycle '
              f'<id>`. Without a binding the entry would be read as visible to everyone, forever, '
              f'so nothing was written.', file=sys.stderr)
        sys.exit(1)
    if scope == 'node' and not node_id:
        print('Error: a node-scoped entry needs a node to bind to, and the state does not name one '
              '(no active cycle, or the cycle it names is holding no node). Name it: `--node <id>`. '
              'Without a binding the entry would be read as visible to everyone, so nothing was '
              'written.', file=sys.stderr)
        sys.exit(1)
    return cycle_id, node_id


def cmd_instruction_add(text: str, scope: str, target: str, said_by: str,
                     note: str | None, stop_after_nodes: int | None,
                     cycle: str | None = None, node: str | None = None) -> None:
    """Record one instruction, VERBATIM.

    The caller supplies the sentence and the scope, and nothing else: the cycle and node it was said
    in are read from state, so a busy session does not have to know where it is.
    Defaults are the safe direction -- `global` + `stage` -- because missing a real constraint is
    silent while an over-broad one is merely noisy, and a rhythm instruction that fires again only
    parks the run once more, where a human can correct it.

    `--scope` is what the entry is scoped to. `--cycle` / `--node` are the caller
    naming that binding itself, which the state cannot do on its own while two cycles are active side
    by side.
    """
    if not text or not text.strip():
        print('Error: --text must not be empty', file=sys.stderr)
        sys.exit(1)
    scope = _scope_from_flag(scope)
    if scope is None:
        print(f'Error: --scope must be one of {list(VALID_SCOPES)}', file=sys.stderr)
        sys.exit(1)
    if target not in VALID_TARGETS:
        print(f'Error: --target must be one of {list(VALID_TARGETS)}', file=sys.stderr)
        sys.exit(1)

    # 读-追加-整份写回必须在锁里：两个会话并发 add 时，后写者会整份覆盖、先写的条目无声消失
    # （顺带各得一个同号 id）。id 按 `len(items)+1` 取，也只有在锁里才是无歧义的。
    with _ledger_lock():
        state = _read_state()
        cycle_id, node_id = _new_entry_binding(scope, cycle, node, state)

        data = _read_instructions()
        items = data['instructions']
        entry = {
            'id': f'i-{len(items) + 1:04d}',
            'text': text,
            'scope': scope,
            'target': target,
            'state': 'live',
            'cycle': cycle_id,
            'node': node_id,
            'said_by': said_by,
            'at': _now(),
            'resolved_at': None,
            'resolved_by': None,
            'note': note,
        }
        if stop_after_nodes is not None:
            entry['stop_after_nodes'] = stop_after_nodes
        items.append(entry)
        _write_instructions(data)
    if scope == 'global':
        # 全局条目没有绑定：`@ 线 / 节点` 那截尾巴只会让人误读成局部条目。
        print(f'INSTRUCTION {entry["id"]}: [{scope}/{target}] {text}')
    else:
        where = (f'{cycle_id} / {node_id[:8]}' if (cycle_id and node_id)
                 else cycle_id or node_id or '(no binding)')
        print(f'INSTRUCTION {entry["id"]}: [{scope}/{target}] {text}   @ {where}')


def cmd_instruction_list(as_json: bool, live_only: bool, project: str | None = None,
                      scope: str | None = None, all_entries: bool = False,
                      node: str | None = None) -> None:
    """The instruction channel.

    Nothing is injected into a dispatch prompt any more, so this command is how a session learns
    what the user asked for: it runs it among its first commands and reads the answer. The default
    view is therefore computed from WHERE the pipeline stands -- the current cycle and the current
    node -- and not from the whole account: a view that is narrower than the truth is a constraint
    that does not exist for the session reading it.

    `--node <id>` is how a session says where it stands when the pointer cannot: with two active
    cycles side by side, `cycle` entries go to the line whose round holds that node and to nobody
    else (`_visible_from_here`).

    `--json` is the machine view of the same answer (a parseable array, `[]` when there is nothing).
    `--all`, `--live` and `--scope` are the account views: a caller asking for the raw rows gets the
    raw rows, with ids, marks and every entry of the account.
    """
    all_items = _read_instructions(project)['instructions']
    rules = _scope_rules()
    if scope is not None:
        # argparse restricts this flag to the vocabulary above (the older names included), so all
        # that is left here is spelling the value the way the account is read.
        scope = _scope_from_flag(scope)
    global_total = len([i for i in all_items if rules.entry_scope(i) == 'global'])

    if not all_entries and scope is None and not live_only:
        visible = [_fold(i) for i in _visible_from_here(project, all_items, node=node)]
        if as_json:
            print(json.dumps(visible, ensure_ascii=False, indent=2))
            return
        if not visible:
            # The helpful sentences are for a human or a model reading stdout; `--json` stays a
            # parseable array on purpose, so a caller can branch on `[]` without string matching.
            lines = (_instruct_empty_lines(len(all_items), None, global_total) if not all_items
                     else _nothing_visible_lines(len(all_items)))
            blind = _ambiguous_cycle_view(project, all_items, node=node)
            if blind:
                lines = lines + [
                    f'（本项目有 {blind} 条 active cycle 并排，我分不出你站在哪一条线上，'
                    f'所以 cycle 作用域的指令两边都没念。用 `--node <你手上的节点 id>` 说一句你'
                    f'在哪条线上，它们就会回来。）']
            for line in lines:
                print(line)
            return
        _render_constraints(visible)
        return

    items = all_items
    if not all_entries:
        # `--all` is the archive view; every other path leaves retired entries out of the way.
        items = [i for i in items if not _retired(i)]
    if scope is not None:
        items = [i for i in items if rules.entry_scope(i) == scope]
    if live_only:
        items = [i for i in items if _is_live(i)]

    if as_json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    if not items:
        for line in _instruct_empty_lines(len(all_items), scope, global_total):
            print(line)
        return
    marks = {'live': '●', 'withdrawn': '✗'}
    for i in items:
        # Any word other than `live` -- including one an older tool wrote -- reads as archived.
        mark = marks.get(i.get('state', 'live'), '✗')
        scoped = rules.entry_scope(i)
        deliver = {'stage': '干活可见', 'boundary': '仅收尾时读'}.get(i.get('target'), '?')
        print(f'{mark} {i["id"]}  [{scoped}/{deliver}]  {i.get("text","")}')
        if i.get('note'):
            print(f'      note: {i["note"]}')


def cmd_instruction_resolve(ins_id: str, state: str, note: str | None,
                         project: str | None = None) -> None:
    """Take one instruction off the account: the owner's one lever, `withdrawn`.

    Visibility is not this command's business, and neither is the ending of a `cycle` / `node`
    entry's life: `scope` decides where an entry can be seen from, and that is derived on every read
    from the bound node/cycle and the DAG as it stands. A scoped entry therefore needs no resolve at
    all -- it goes out of sight on its own. The account has two states, and the only move here is
    archiving a live entry; a record an older tool left with another word in `state` is read as
    archived already (`_retired`) and nothing writes those words back.
    """
    allowed = ('withdrawn',)
    if state not in allowed:
        print(f'Error: resolve --state must be one of {list(allowed)} (not {state!r}): withdrawing '
              f'is the only move left, and only the owner makes it. A scoped entry goes out of '
              f'sight on its own when its node is completed or its cycle ends.', file=sys.stderr)
        sys.exit(1)
    with _ledger_lock(project):
        data = _read_instructions(project)
        hit = None
        for item in data['instructions']:
            if item.get('id') == ins_id:
                hit = item
                break
        if hit is None:
            print(f'Error: no instruction {ins_id!r}', file=sys.stderr)
            sys.exit(1)

        hit['state'] = state
        hit['resolved_at'] = _now()
        hit['resolved_by'] = 'boundary'
        if note:
            hit['note'] = note
        _write_instructions(data, project)
    print(f'INSTRUCTION {ins_id} → {state}')


def _dispatch_instruction(args) -> None:
    sub = getattr(args, 'instruction_command', None)
    project = getattr(args, 'project', None)
    if sub == 'add':
        cmd_instruction_add(args.text, args.scope, args.target, args.said_by, args.note,
                            args.stop_after_nodes, getattr(args, 'cycle', None),
                            getattr(args, 'node', None))
    elif sub == 'list':
        cmd_instruction_list(args.json, args.live, project,
                             getattr(args, 'scope', None),
                             getattr(args, 'all_entries', False),
                             getattr(args, 'node', None))
    elif sub == 'resolve':
        cmd_instruction_resolve(args.ins_id, args.state, args.note, project)
    else:
        print('Usage: pf instruction {add|list|resolve} ...', file=sys.stderr)
        sys.exit(1)
