"""Standing instructions: the account and everything about "what is visible from where".

Owns the account's vocabulary, the position rules that decide which entries a reader sees from where
the pipeline stands, and the `instruction` subcommands (add / list / resolve).
"""
import json
import sys

from .store import (_holding_ids, _ledger_lock, _now, _read_dag, _read_instructions, _read_state,
                    _write_instructions)
from .pipeline_loader import _load_pipeline


# ── Standing instructions ────────────────────────────────────────────────────
# One append-only account, one writer (this CLI). Any session may ADD one; the visibility of what it
# added is decided by the entry's own `scope`, and the session that closes a node may RESOLVE one.
# Three scopes; the defaults lean the "noisy" way on purpose (see the module notes below).

VALID_TARGETS = ('stage', 'boundary')

#: 条目的状态词，一词一个名字：`state` 字段缺省读作 `LIVE_STATE`（一个字段都没有的旧条目照旧算数）。
LIVE_STATE = 'live'
DONE_STATE = 'done'
SUPERSEDED_STATE = 'superseded'
WITHDRAWN_STATE = 'withdrawn'
#: `_retired` 收的两个词：一个是人亲手拿掉的，一个是被后来的条目顶掉的。
RETIRED_STATES = (SUPERSEDED_STATE, WITHDRAWN_STATE)
#: On a global entry these mean something different from "the constraint is over":
#: `applied` = honoured somewhere, still in force; `n/a` = nothing in this project falls under it.
#: Only a human writes `withdrawn`.
CONSTRAINT_MARKS = ('applied', 'n/a')
CONSTRAINT_MARK_LABELS = {'applied': '已兑现（仍然生效）', 'n/a': '本项目用不到（仍然生效）'}
#: 清单视图（`--all`）给每一行的记号：状态词各一个，两个标记词都记成 `·`。
STATE_MARKS = {LIVE_STATE: '●', DONE_STATE: '✓', SUPERSEDED_STATE: '↳', WITHDRAWN_STATE: '✗',
               **{mark: '·' for mark in CONSTRAINT_MARKS}}
#: `resolve --state` 收的词：三个状态词，加两个**标记词** —— 标记词不改条目的状态，只记「这条
#: 约束在这里算数了 / 这里没有它的份」。`DONE_STATE` / `SUPERSEDED_STATE` 也是 `global` 条目不
#: 许被标的两个词（它们会把约束从下游读者眼前整个拿掉）。
RESOLVE_STATES = (DONE_STATE, SUPERSEDED_STATE, WITHDRAWN_STATE) + CONSTRAINT_MARKS

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


def valid_scopes() -> tuple:
    """`scope` 的词表：`conduct_pipeline` 的那一份，不是这里的第二个副本。

    `SCOPES` 在 `conduct_pipeline_impl/instruct.py`（判定的正本）里；这个工具只借不抄 —— 抄一份
    的下场是给判定加第四个 scope 之后，`--scope` 先在 argparse 那一步把它拒了。
    """
    return _scope_rules().SCOPES


def _scope_from_flag(raw: str | None) -> str | None:
    """A `--scope` value as a scope, or None when it is not one of the three."""
    if raw is None:
        return None
    return raw if raw in _scope_rules().SCOPES else None


def _retired(entry: dict) -> bool:
    """True once a human has taken this entry out of force."""
    return entry.get('state') in RETIRED_STATES


def _is_live(entry: dict) -> bool:
    return entry.get('state', LIVE_STATE) == LIVE_STATE


# ── where the pipeline stands, and what is visible from there ──

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

    Read through the store's own door (`_read_state`) rather than the module-level `LEDGER_DIR`, so
    `--project` decides which workspace answers.

    `node` is the reader saying where it stands (`instruction list --node <id>`), and it stands as
    named: the node in the line's hand is not substituted for it. That is what the flag is for -- the
    hand says where the LINE is, and the reader is saying which node it is the reader of. With one
    active cycle the cycle half comes from state.json; with two or more side by side the named node
    is what pins the line down as well (the active cycle holding it, when exactly one does).

    When nothing says where the reader stands -- no active cycle, two of them and no node named, or a
    named node no active line holds -- both halves come back None, and the reader judges each entry
    by the status of what it is bound to instead (the wide reading, see `instruction_in_scope`).
    """
    state = _read_state(project)
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
    if node:
        return (cycle['id'], node)
    holding = _holding_ids(cycle)
    return (cycle['id'], holding[0] if holding else None)


def _dag_nodes(project: str | None = None) -> dict:
    """The DAG's nodes: the one fact a `node` entry is measured against (is that node completed?).

    Read through the store's door for the fourth record, `<project>/.pf/ideas.json`.
    """
    return _read_dag(project)


def _active_cycles(project: str | None = None) -> set:
    """这个项目此刻在服务的线：scope 判定要的那一半事实（另一半是 DAG 的节点表）。"""
    return {c.get('id') for c in _read_state(project).get('cycles', [])
            if c.get('status') == 'active' and c.get('id')}


def _fold(entry: dict) -> dict:
    """One entry as the reader-facing view carries it: the sentence, where it is visible, how it is
    delivered. Ids and bookkeeping stay in the account."""
    return {
        'id': entry.get('id'),
        'text': entry.get('text', ''),
        'scope': _scope_rules().entry_scope(entry),
        'target': entry.get('target'),
    }


def _last_applied(entry: dict) -> dict | None:
    applied = entry.get('applied') or []
    return applied[-1] if applied else None


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
            f'（指令记录里有 {total} 条记录，都已被兑现或撤销。）']


def _nothing_visible_lines(total: int) -> list:
    """The default view found nothing, on a project that has an account.

    This is the dangerous silence: a session that reads it must be able to tell "there
    is nothing for me here" from "there is nothing at all", and the second sentence is what keeps it
    from reading as a green light.
    """
    return ['当前这一步没有看得见的指令。',
            f'（指令记录里有 {total} 条记录：它们要么绑在别的 cycle 或别的节点上，要么已经不在生效'
            f'状态。位置钉不下来时这些条目照念 —— 读成空，是它们真的到不了这一步。）']


#: How a scope is spelled for the reader. The vocabulary is English in the account and in the flag
#: (`--scope global|cycle|node`); the sentences a session reads are Chinese, and none of them
#: carries the word `cycle`, which is also how the cycle ids in this account start.
SCOPE_WORDS = {'global': '全局（任何位置都算数）',
               'cycle': '本次运行期间（这次运行结束就不再算数）',
               'node': '仅当前节点（这个节点做完就不再算数）'}


def _render_constraints(items: list) -> None:
    """The reader-facing rendering: what a session reads off `instruction list`.

    Text, where it is visible from, and how it is delivered. The bookkeeping (`applied`,
    ids, which cycle and node it was bound to) stays in the account and in `--json`: a reader has no
    use for it, and showing it invites treating a past application as a reason the instruction might
    not apply now.
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
    means. A `cycle` / `node` entry with no binding has nothing to measure against, so it is read as
    visible everywhere and never goes out of sight -- those two scopes are refused here rather than
    written that way. `global` needs no binding; it still records where it was said, for the
    account's own sake.

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
        print(f'Error: --scope must be one of {list(valid_scopes())}', file=sys.stderr)
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
            'state': LIVE_STATE,
            'cycle': cycle_id,
            'node': node_id,
            'said_by': said_by,
            'at': _now(),
            'resolved_at': None,
            'resolved_by': None,
            'superseded_by': None,
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
    view is therefore computed from WHERE the pipeline stands -- the line this session works on and
    the node in its hand -- and not from the whole account: a view that is narrower than the truth is
    a constraint that does not exist for the session reading it.

    `--node <id>` is how a session says where it stands when the pointer cannot: that node stands as
    named, and with two active cycles side by side it is also what pins the line down -- a `cycle`
    entry goes to the line whose round holds that node and to nobody else.

    `--json` is the machine view of the same answer (a parseable array, `[]` when there is nothing).
    `--all`, `--live` and `--scope` are the account views: a caller asking for the raw rows gets the
    raw rows, with ids, marks and every entry of the account.
    """
    all_items = _read_instructions(project)['instructions']
    rules = _scope_rules()
    global_total = len([i for i in all_items if rules.entry_scope(i) == 'global'])

    if not all_entries and scope is None and not live_only:
        # 默认视图：位置由 `_position` 读出来，读不读由管道的那一个判定说了算
        # （`instruction_in_scope`）—— 收尾结算一个节点时站的是同一个位置，用的是同一个判定。
        cur_cycle, cur_node = _position(project, node=node)
        nodes, active = _dag_nodes(project), _active_cycles(project)
        visible = [_fold(i) for i in all_items
                   if _is_live(i) and not _retired(i)
                   and rules.instruction_in_scope(i, nodes, active,
                                                  cycle=cur_cycle, node=cur_node)]
        if as_json:
            print(json.dumps(visible, ensure_ascii=False, indent=2))
            return
        if not visible:
            # The helpful sentences are for a human or a model reading stdout; `--json` stays a
            # parseable array on purpose, so a caller can branch on `[]` without string matching.
            lines = (_instruct_empty_lines(len(all_items), None, global_total) if not all_items
                     else _nothing_visible_lines(len(all_items)))
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
    for i in items:
        mark = STATE_MARKS.get(i.get('state', LIVE_STATE), '?')
        scoped = rules.entry_scope(i)
        deliver = {'stage': '干活可见', 'boundary': '仅收尾时读'}.get(i.get('target'), '?')
        extra = f'  ← {i["superseded_by"]}' if i.get('superseded_by') else ''
        print(f'{mark} {i["id"]}  [{scoped}/{deliver}]  {i.get("text","")}{extra}')
        if i.get('note'):
            print(f'      note: {i["note"]}')


def cmd_instruction_resolve(ins_id: str, state: str, note: str | None,
                         superseded_by: str | None, project: str | None = None) -> None:
    """Record what became of one instruction.

    Visibility is not this command's business, and neither is the ending of a `cycle` / `node`
    entry's life: `scope` decides where an entry can be seen from, and that is derived on every read
    from the bound node/cycle and the DAG as it stands. A scoped entry therefore needs no resolve at
    all -- it goes out of sight on its own. What is left here is the account's own record-keeping:
    `global` entries, which stay in force and take `applied` (honoured here) or `n/a` (nothing here
    falls under it), plus `withdrawn`, the owner's lever for taking any constraint off. `done` /
    `superseded` remain accepted for tidying an account by hand.
    """
    if state not in RESOLVE_STATES:
        print(f'Error: resolve --state must be one of {RESOLVE_STATES} (not {state!r})',
              file=sys.stderr)
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

        global_entry = hit.get('scope') == 'global'
        if global_entry and state in (DONE_STATE, SUPERSEDED_STATE):
            print(f'Error: {ins_id} is a global constraint (scope=global) and cannot be marked '
                  f'{state!r}.\n'
                  f'       A constraint stays in force until a human withdraws it. Record what happened\n'
                  f'       to it instead: --state applied (honoured this round) or --state n/a (nothing\n'
                  f'       in this project falls under it). Taking the constraint off is `--state\n'
                  f'       withdrawn`, which is the owner\'s call.',
                  file=sys.stderr)
            sys.exit(1)
        if not global_entry and state in CONSTRAINT_MARKS:
            print(f'Error: {ins_id} is scoped to one {hit.get("scope")} (scope={hit.get("scope")}); a '
                  f'scoped entry needs no resolve -- it goes out of sight on its own when its node is '
                  f'completed or its cycle ends. {state!r} is a mark for a global entry.',
                  file=sys.stderr)
            sys.exit(1)

        if state in CONSTRAINT_MARKS:
            mark = {'state': state, 'at': _now(), 'note': note or ''}
            cycle = hit.get('cycle')
            if cycle:
                mark['cycle'] = cycle
            hit.setdefault('applied', []).append(mark)
            # `state` stays `live`: the constraint is still in force, which is the whole point.
            _write_instructions(data, project)
            print(f'INSTRUCTION {ins_id} → {state}（约束仍然生效）')
            return

        hit['state'] = state
        hit['resolved_at'] = _now()
        hit['resolved_by'] = 'boundary'
        if note:
            hit['note'] = note
        if superseded_by:
            hit['superseded_by'] = superseded_by
        _write_instructions(data, project)
    print(f'INSTRUCTION {ins_id} → {state}')
