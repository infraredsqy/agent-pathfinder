"""The `cycle` group: one line of work, and every verb that acts on it.

Owns all eleven verbs of `pf cycle` -- create / start / rename / status / policy / list / current /
show / hold / close / retry -- plus the vocabulary a cycle carries: VALID_TAGS, `_parse_cycle_tags`
and the policy table (`POLICY_*`, and `cycle_policy` as the one reading of it).
"""

import json
import sys
from pathlib import Path

from .store import (CYCLE_VALID_STATUSES, PARALLEL_DEFAULT, VALID_NODE_RESULTS,
                    _find_cycle, _holding_ids, _holding_of, _ledger_lock, _now, _read_signal,
                    _read_state, _resolve_project_root, _set_holding, _signal_path, _shared,
                    _write_signal, _write_state, meta_dir_for)
from .pipeline_loader import _load_pipeline


# ── cycle tags: 这条线在哪些节点里挑人 ─────────────────────────────────────────
# `cycle.tags` 是这条线的**选材范围**：每一次挑选都用这一份。新的 cycle 一律要给；空范围只剩
# 存量记录与过滤助手自己的空集分支会读到。词表与 dag-manage.py 的那份是同样三个词（那边管节点，
# 这边管 cycle 的范围）；一份摆在两个工具里的约定，靠一个测试比对，免得两边分头长出自己的词。
VALID_TAGS = ('writing', 'coding', 'art')


def _require_tags(tags: list, verb: str) -> None:
    """开一条新线必须有选材范围；空范围拒收，并把 `--tags` 写进提示。

    一条线**属于哪一支**（它有没有权因维护度量插维护节点）也由这份 tag 表达：没有 tags 的
    cycle 会让「谁有权反应维护度量」多出第二种答案。存量 cycle 照跑，这条闸门只对新的调用生效。
    """
    if tags:
        return
    print(f'Error: {verb} 必须给 --tags（这条线的选材范围）。'
          f'例如：{verb} <slug> --tags coding。'
          f'可用的词：{"/".join(VALID_TAGS)}（也可以是词表外的词，照收）。'
          f'这条线的每一次挑选、以及维护通道的适用领域，都读这一份范围。',
          file=sys.stderr)
    sys.exit(2)


# ── cycle policies: 一条线自己带的规则 ─────────────────────────────────────────
# `cycle.policies` 是可选的：没写这个键的 cycle 落盘时就是今天这份形状，读的时候按默认值补。
# 头一条是 `when_shelved` —— 节点收了尾却没交付（它于是被搁置、等人救），这条线是停下等人，还是接着
# 挑下一个节点。默认 `stop`：缺省值必须落在保守那一边。
# 第二条是 `parallel` —— 这条线同时能握几个节点、开几个会话。默认 1（一次一格）。它是**上限**：
# 节点能不能并排跑，另由依赖图说了算。
# 与 `conduct_pipeline_impl/store.py` 的那份是同一套词（那边管读，这边管写）。
POLICY_DEFAULTS = {'when_shelved': 'stop', 'parallel': PARALLEL_DEFAULT}
POLICY_VALUES = {'when_shelved': ('stop', 'continue')}
#: 整数型策略：整数值，`off` 是取消。`parallel` 住在这里（下限 1：一条线至少握一格）。
POLICY_COUNTS = ('parallel',)
#: 数值型策略：没有候选值表，值按数字解析；`off` 是取消（缺省即"没配"）。
#: 两条维护线就住在这里：它们是**这条线自己的规则**，与 `tags` 同类，
#: 所以跟着 cycle 记录走 —— 一条不碰代码的线（`art` / `writing`）身上根本没有这两个键。
POLICY_NUMBERS = ('test_seconds_limit', 'issue_pool_weight_limit')
#: 词表型策略：值必须在候选表里，`off` 是清除；没有默认值 —— 没配的线落在调用链的下一级，
#: 行为与这条策略存在之前一字不变。`spawner` 住在这里：这条线的会话由哪份 spawner 开
#: （`dsh` / `zcode`）。只收词、不收路径：cycle 记录随 `.pf/` 跨机器合并，机器本地的路径
#: 落进状态文件就是另一台机器上的死值；要路径的场合走单发 `--spawner <路径>`。
POLICY_WORDS = ('spawner',)
POLICY_WORD_CHOICES = {'spawner': ('dsh', 'zcode')}
#: 合法策略键的并集：四张表就是全部来源，这里算一次 —— `cycle policy` 的报错与 argparse 的
#: `choices`（经 `state-manage.py`）都取这一份。
POLICY_KEYS = tuple(sorted(set(POLICY_VALUES) | set(POLICY_NUMBERS) | set(POLICY_COUNTS)
                           | set(POLICY_WORDS)))


def cycle_policy(cycle: dict) -> dict:
    """这条线现在的有效策略：盘上没有的键（或写了个不认识的词）按默认值补齐。

    数值型策略（`POLICY_NUMBERS`）与词表型策略（`POLICY_WORDS`）没有默认值：
    没配就不出现在这份读数里。
    整数型策略（`POLICY_COUNTS`）有默认值（`parallel` 缺省 1）；盘上写了个非整数就当没配。
    """
    stated = cycle.get('policies') or {}
    out = {}
    for key, default in POLICY_DEFAULTS.items():
        value = stated.get(key)
        if key in POLICY_COUNTS:
            out[key] = value if _policy_count(value) is not None else default
        else:
            out[key] = value if value in POLICY_VALUES.get(key, ()) else default
    for key in POLICY_NUMBERS:
        number = _policy_number(stated.get(key))
        if number is not None:
            out[key] = number
    for key in POLICY_WORDS:
        value = stated.get(key)
        if value in POLICY_WORD_CHOICES.get(key, ()):
            out[key] = value
    return out


def _policy_count(value):
    """盘上的一个整数型策略，或 `None`。只认盘上本来就是正整数的值。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _policy_number(value):
    """盘上的一个数值型策略，或 `None`。只认盘上本来就是数的值：布尔与字符串一律当没配。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _parse_policy_number(key: str, text: str):
    """命令行的数值型策略：正数落盘，`off` 取消。

    0 与负数一律拒收 —— 线是"最后一个可接受的数"，0 等于把每一个读数都判成越线。
    """
    if str(text).strip().lower() == 'off':
        return None
    try:
        parsed = float(text)
    except (TypeError, ValueError):
        print(f'Error: policy {key} takes a positive number or `off`, got {text!r}',
              file=sys.stderr)
        sys.exit(1)
    if parsed <= 0:
        print(f'Error: policy {key} must be positive (a line of 0 fails every reading)',
              file=sys.stderr)
        sys.exit(1)
    return int(parsed) if parsed.is_integer() else parsed


def _parse_policy_count(key: str, text: str):
    """命令行的整数型策略：正整数落盘，`off` 取消（缺省 1）。"""
    if str(text).strip().lower() == 'off':
        return None
    try:
        parsed = int(str(text).strip())
    except (TypeError, ValueError):
        print(f'Error: policy {key} takes a positive integer or `off`, got {text!r}',
              file=sys.stderr)
        sys.exit(1)
    if parsed < 1:
        print(f'Error: policy {key} must be at least 1 (a line holds at least one node)',
              file=sys.stderr)
        sys.exit(1)
    return parsed


def _parse_policy_word(key: str, text: str):
    """命令行的词表型策略：候选表里的词落盘，`off` 清除（清除即"没配"，落回调用链的下一级）。"""
    if str(text).strip().lower() == 'off':
        return None
    if text not in POLICY_WORD_CHOICES.get(key, ()):
        print(f'Error: policy {key} must be one of {list(POLICY_WORD_CHOICES[key])} '
              f'or `off`, got {text!r}', file=sys.stderr)
        sys.exit(1)
    return text


def _parse_cycle_tags(raw: str | list | None) -> list:
    """`--tags coding,writing` → `['coding', 'writing']`：去空白、归一、去重，保序。

    词表外的 tag 警告一行就收下：词表是一份给人看的约定，engine 照单全收 —— 真冒出第四类
    工作时，写进去就能用。同义词层已删：这里不做归一，过滤一律按字面比。

    收字符串也收 list：`cycle start` 先把命令行拆好再建 cycle，`cycle create` 直接拿人敲的那串。
    两种形状都从这一个入口过，`art` 才不会在某条路上被拆成 `['a','r','t']`。
    """
    if isinstance(raw, (list, tuple)):
        raw = ','.join(str(x) for x in raw)
    out = []
    for item in (raw or '').split(','):
        tag = item.strip()
        if tag and tag not in out:
            out.append(tag)
    unknown = [t for t in out if t not in VALID_TAGS]
    if unknown:
        print(f'Warning: tag {", ".join(unknown)} 不在词表 {"/".join(VALID_TAGS)} 里；照收，'
              f'但它只能和写了同一个词的节点对上。', file=sys.stderr)
    return out


def cmd_cycle_create(cycle_id: str, title: str | None,
                     force: bool = False, tags=None) -> None:
    """Create a cycle -- and refuse while another one is still active.

    Two active cycles is not a harmless extra: the bare `next` that everything else relies on can no
    longer tell which cycle it is about, so it degrades into `ask_user` ("continue which one?"),
    and an unattended run has nobody to answer. One cycle at a time is the invariant; `--force` is
    the human saying they know that and want the second one anyway.

    `tags` is this cycle's filter set: which nodes it picks from. It is written down here (and
    nowhere else) because every later pick reads it back -- the set is a property of the LINE, not of
    the step that happened to open it. Empty means the whole graph is in range.

    The cycle starts with nothing in hand: `cycle hold` is what claims the first node.
    """
    # 命令行那串先过一遍唯一的解析入口：`cycle start` 会先拆好再调到这里，人直接敲
    # `cycle create --tags art` 时到这里还是原样的一串。两条路都必须落到同一份 list 上，
    # 否则 `art` 会在其中一条路上被当成三个字母。
    tags = _parse_cycle_tags(tags)
    _require_tags(tags, 'pf cycle create')
    with _ledger_lock():
        state = _read_state()
        for c in state['cycles']:
            if c['id'] == cycle_id:
                print(f'Error: cycle "{cycle_id}" already exists', file=sys.stderr)
                sys.exit(1)

        active = [c['id'] for c in state['cycles'] if c.get('status') == 'active']
        if active and not force:
            print(f'Error: cycle(s) {active} still active -- one cycle at a time. Take them out of '
                  f'service (`pf cycle status <id> inactive`), or pass --force to run two anyway.',
                  file=sys.stderr)
            sys.exit(1)
        if active and force:
            print(f'Note: --force given; {active} stay active, so a bare `next` will have to be told '
                  f'which cycle (--cycle) from now on.', file=sys.stderr)

        now = _now()
        if not title:
            title = f'Cycle {now[:10]}'
        entry = {
            'id': cycle_id, 'title': title,
            'status': 'active',
            # Nothing is in hand until `cycle hold` claims a node; `path` is the steps walked.
            'holding': [], 'path': [],
            'created_at': now, 'updated_at': now,
        }
        if tags:
            entry['tags'] = tags
        state['cycles'].append(entry)
        _write_state(state)

    print(f'CREATED: cycle "{cycle_id}"' + (f' (tags={list(tags)})' if tags else ''))


def cmd_cycle_policy(cycle_id: str, key: str, value: str) -> None:
    """记下这条线的一条策略，并把生效后的整份策略报回来。

    `policies` 是**这条线**的属性（与 `tags` 同一类东西），所以它跟着 cycle 记录走，而不是跟着
    某一次调用：这条线上的每一步、以及每一次收尾，读的都是同一份。

    数值型策略（维护线那两条）收一个正数，`off` 把这一条去掉；整数型（`parallel`）收一个正整数；
    词表型（`spawner`）收候选表里的词，`off` 把这一条去掉。
    """
    if key in POLICY_NUMBERS:
        stored = _parse_policy_number(key, value)
    elif key in POLICY_COUNTS:
        stored = _parse_policy_count(key, value)
    elif key in POLICY_WORDS:
        stored = _parse_policy_word(key, value)
    elif key in POLICY_VALUES:
        allowed = POLICY_VALUES[key]
        if value not in allowed:
            print(f'Error: policy {key} must be one of {list(allowed)}', file=sys.stderr)
            sys.exit(1)
        stored = value
    else:
        print(f'Error: unknown policy {key!r}; valid keys: {list(POLICY_KEYS)}',
              file=sys.stderr)
        sys.exit(1)

    with _ledger_lock():
        state = _read_state()
        cycle = _find_cycle(state, cycle_id)
        policies = cycle.setdefault('policies', {})
        if stored is None:
            policies.pop(key, None)
        else:
            policies[key] = stored
        cycle['updated_at'] = _now()
        _write_state(state)
        effective = cycle_policy(cycle)
    print(f'POLICY: "{cycle_id}" → ' + ', '.join(f'{k}={v}' for k, v in effective.items()))


def cmd_cycle_start(cycle_id: str, title: str | None,
                    force: bool = False, tags=None, parallel=None) -> None:
    """Create a cycle AND pin it on the node the rule picks. One command.

    The launcher should not have to know how the DAG is ranked, so the pick happens here, using the
    same rule a close uses. If the graph has nothing ready, this refuses: there is no work to run,
    and inventing some is pf-route's job, done by a human, not the pipeline's.

    `--tags` is the filter set this cycle will pick from, and it has to reach the PICK here: the
    pick happens while the cycle is being created, so a set that only landed in the cycle record
    would be too late for the first node.

    `--parallel` 是这条线同时握几格：它落的正是 `policies.parallel` 那一格（`cycle policy` 写的
    是同一个键），所以开局这一次挑到几格、以后每一次收尾补几格，取的是同一个数。不给就用缺省
    （一次一格）。

    Refuses while another cycle is still active, unless `--force`. The alternative -- two active
    cycles -- makes the next bare `next` an `ask_user`, which an unattended run cannot answer, and
    the cycle it opens would very likely pick the node the old one was working on.

    The whole body runs under the ledger lock: the pick must not read the DAG while another
    `cycle start` is mid-claim, or both lines pin the same node (the pick's "ready" excludes the
    nodes a cycle holds only if the other line's claim is already on disk).
    """
    pipeline = _load_pipeline()

    tags = _parse_cycle_tags(tags)
    _require_tags(tags, 'pf cycle start')
    # 命令行那一格过 `cycle policy` 用的同一个解析口：正整数落盘，`off` 落回缺省。
    stated = _parse_policy_count('parallel', parallel) if parallel is not None else None
    with _ledger_lock():
        project = _resolve_project_root(Path.cwd())
        # 开局钉几格与收尾补几格是同一件事：`parallel` 说这条线同时握几个节点。这一次挑选的
        # `limit` 与开局钉几格都是它 —— `cycle_policy` 是拿它的唯一来源，命令行给了就先问它一次，
        # 没给就是这条新线落盘后的那个缺省值。
        stated_or_default = {'parallel': stated} if stated is not None else {}
        cap = cycle_policy({'policies': stated_or_default})['parallel']
        ranked = pipeline.ranked_ready_nodes(project, limit=cap, tags=tags)
        if not ranked:
            blocked = pipeline.tag_blocked_ready_nodes(project, tags)
            if blocked:
                # 图里有活，只是不归这条线管。这句话要让人一眼看出差在哪儿：换一套 --tags 就行。
                print('Error: --tags ' + ','.join(tags) + ' 这个过滤集里没有 ready 节点；'
                      '图里这些就绪节点被 tag 挡在外面：\n'
                      + '\n'.join(pipeline.tag_blocked_lines(blocked))
                      + '\n把 --tags 改宽一点（没有 tag 的节点在任何范围里都在）。', file=sys.stderr)
                sys.exit(2)
            print('Error: 图里没有 ready 节点 —— 没有可执行的活。先让 pf-route 建好图，再开机。',
                  file=sys.stderr)
            sys.exit(2)
        node = ranked[0]
        cmd_cycle_create(cycle_id, title, force=force, tags=tags)
        state = _read_state()
        cycle = _find_cycle(state, cycle_id)
        if stated is not None:
            cycle.setdefault('policies', {})['parallel'] = stated
        holding = [{'id': n['id'], 'since': cycle['created_at']} for n in ranked[:cap]]
        _set_holding(cycle, holding)
        _write_state(state)
    # 名单是按这条线的 `parallel` 取的，所以「后面还排着多少」这次读不出来 —— 不报，也不猜。
    print(f'STARTED: cycle "{cycle_id}" → {node.get("title", node["id"])}'
          + (f'  ({len(holding)} node(s) pinned)' if len(holding) > 1 else ''))


# ── the group's queries ──────────────────────────────────────────────────────

def cmd_cycle_list() -> None:
    state = _read_state()
    if not state['cycles']:
        print('No cycles found.')
        return
    print(f'{"ID":<22} {"Title":<34} {"Status":<12} {"Steps":<6} {"Node":<10}')
    print('-' * 92)
    for c in state['cycles']:
        holding = _holding_ids(c)
        node = holding[0][:8] if holding else '-'
        if len(holding) > 1:
            node += f'+{len(holding) - 1}'
        print(f'{c["id"]:<22} {c.get("title","?"):<34} '
              f'{c.get("status","?"):<12} {len(c.get("path") or []):<6} {node:<10}')


def cmd_cycle_current() -> None:
    """现在要紧的那几条线：每一条 `active` 的，**以及每一条手上有节点的**。

    第二条是这里存在的原因：一条不在服务中的线照样握着它没收尾的节点 —— 那正是人必须看见的
    状态（活停在半路，而这条线不会自己往前走）。以前只列 active，于是停住的线在这份读数里根本
    不存在。

    一条线可以同时握着多个节点（`parallel` 策略）：这份读数把每一个都列出来，各自的认领时刻也
    就在每一行上。
    """
    state = _read_state()
    lines = [c for c in state['cycles']
             if c.get('status') == 'active' or _holding_ids(c)]
    if not lines:
        print('No active cycles.')
        return
    for c in lines:
        # ● 在服务中，○ 不在 —— 一眼能看出这条线会不会自己往前走。
        mark = '●' if c.get('status') == 'active' else '○'
        policy = ', '.join(f'{k}={v}' for k, v in cycle_policy(c).items())
        print(f'{mark} {c["id"]} — {c.get("title","")}  [{c.get("status", "?")}]')
        print(f'  Policy: {policy}  Steps walked: {len(c.get("path") or [])}')
        holding = _holding_of(c)
        if holding:
            for item in holding:
                print(f'  Node: {item["id"]}  (since {item.get("since") or "?"})')
        else:
            print('  Node: (none in hand)')


def cmd_cycle_show(cycle_id: str, node_id: str) -> None:
    """One node's standing in this cycle: the signal on disk and the cycle's own hand."""
    state = _read_state()
    cycle = _find_cycle(state, cycle_id)
    item = next((h for h in _holding_of(cycle) if h['id'] == node_id), None)
    if item:
        print(f'cycle {cycle_id}: holding {node_id} since {item.get("since") or "?"}')
    else:
        step = next((s for s in reversed(cycle.get('path') or [])
                     if s.get('node') == node_id), None)
        if step:
            print(f'cycle {cycle_id}: walked {node_id} → {step.get("result")} '
                  f'({step.get("started_at")} .. {step.get("ended_at")})')
        else:
            print(f'cycle {cycle_id}: nothing about {node_id} (not in hand, not in the path)')
    sig = _read_signal(cycle_id, node_id)
    if sig:
        print(f'  signal: {sig.get("status")}  {sig.get("summary","")}')
        if sig.get('outputs'):
            print(f'  outputs: {json.dumps(sig["outputs"], ensure_ascii=False)}')
    else:
        print('  signal: (none)')


# ── the group's writes ───────────────────────────────────────────────────────

def cmd_cycle_hold(cycle_id: str, node_id: str) -> None:
    """Pin the cycle on a node. This is how work is claimed: another cycle's pick skips it.

    The claim is exclusive ACROSS cycles, and the account is where that is enforced: two lines can
    reach for the same node in the overlap window between their closes and opens, and without this
    check both pins would land -- two sessions would then boot onto one node, and no close would
    ever be able to settle it (`cycle close` reads ONE holder). The lock makes the scan-then-write
    one atomic step.

    一条线同时可以握多个节点（`parallel` 策略），所以往已经有节点的这条线上再 pin 一个不是错误
    —— 这一笔就是「再开一格」；同一个 id 重 pin 照旧放行（重启语义）。
    """
    with _ledger_lock():
        state = _read_state()
        cycle = _find_cycle(state, cycle_id)
        holder = next((c for c in state['cycles']
                       if c['id'] != cycle_id and node_id in _holding_ids(c)), None)
        if holder is not None:
            print(f'Error: node "{node_id}" is already pinned by cycle "{holder["id"]}" '
                  f'(status {holder.get("status")}).'
                  f' -- one node belongs to one cycle at a time, whatever that line\'s status. '
                  f'Close it there first, or pick another node.', file=sys.stderr)
            sys.exit(1)
        now = _now()
        holding = _holding_of(cycle)
        if not any(item['id'] == node_id for item in holding):
            holding.append({'id': node_id, 'since': now})
        _set_holding(cycle, holding)
        cycle['updated_at'] = now
        _write_state(state)
    print(f'CURRENT_NODE: {cycle_id} → {node_id}')


def cmd_cycle_retry(cycle_id: str, node_id: str, summary: str | None,
                    drop: bool = False) -> None:
    """Take a node's ticket out of the way so its run can be retried.

    Retrying a node whose run came back not delivered needs this: without it the old terminal signal
    stays on disk, so the next reader calls it finished again -- an infinite retry that never retries.

    默认把条子重写成 `pending`（重试留下的记号，读者照红灯等）。`--drop` 连条子都不留：重试真正
    起步的那一步 —— 只要还有一张 `pending` 条子，`next` 就永远答 `wait`，一个字节都推不动
    （这条卡住过一条线）。
    """
    with _ledger_lock():
        state = _read_state()
        _find_cycle(state, cycle_id)
        sp = _signal_path(cycle_id, node_id)
        remove = _shared('pf_shared_jsonio', 'jsonio.py').unlink_with_retry
        if drop:
            if sp.exists():
                remove(sp)
            # 会话那条记号也一并抹掉：留着它，重试根本派不出去（派发会把这一格跳过，见 nextcmd）。
            remove(_launch_dir(cycle_id, node_id) / 'started')
            print(f'Signal dropped (no ticket left): {sp}')
            return
        if sp.exists():
            remove(sp)
        p = _write_signal(cycle_id, node_id, 'pending',
                          summary or 'Node restarted (previous signal cleared)', {})
    print(f'Signal cleared and rewritten as pending: {p}')


def cmd_cycle_close(cycle_id: str, node_id: str, result: str) -> None:
    """Record the step and let the node go.

    The node leaves the cycle's hand here, which is what lets the next pick take it (or the close
    command open the next one). 这条线上握着的其它节点不动。The DAG status is a separate writer's
    job -- the conductor runs `dag-manage status` itself -- so this only owns the cycle's own record.
    """
    if result not in VALID_NODE_RESULTS:
        print(f'Error: --result must be one of {sorted(VALID_NODE_RESULTS)}', file=sys.stderr)
        sys.exit(1)
    with _ledger_lock():
        state = _read_state()
        cycle = _find_cycle(state, cycle_id)
        holding = _holding_of(cycle)
        item = next((h for h in holding if h['id'] == node_id), None)
        if item is None:
            names = ', '.join(h['id'][:8] for h in holding) or 'nothing'
            print(f'Error: cycle "{cycle_id}" is not holding {node_id}'
                  + f' (it holds {names})'
                  + '; nothing was changed.', file=sys.stderr)
            sys.exit(1)

        now = _now()
        step = {
            'node': node_id,
            'started_at': item.get('since'),
            'ended_at': now,
            'result': result,
        }
        cycle.setdefault('path', []).append(step)
        _set_holding(cycle, [h for h in holding if h['id'] != node_id])
        cycle['updated_at'] = now
        _write_state(state)
    print(f'NODE_CLOSED: {cycle_id} → {node_id} {result} '
          f'(path is now {len(cycle["path"])} step(s))')


def cmd_cycle_status(cycle_id: str, status: str) -> None:
    """把一条线放进或拿出服务。

    记录里只落 `active` / `inactive`：一条线停下来的原因写在停手那一刻的 reason 里，状态只回答
    「它该不该往前走」。这条命令也是人把一条线重新拉起来的那只手 —— 工具自己永远不会写 `active`。
    """
    if status not in CYCLE_VALID_STATUSES:
        print(f'Error: cycle status must be one of {sorted(CYCLE_VALID_STATUSES)}', file=sys.stderr)
        sys.exit(1)

    with _ledger_lock():
        state = _read_state()
        cycle = _find_cycle(state, cycle_id)
        cycle['status'] = status
        cycle['updated_at'] = _now()
        _write_state(state)
    print(f'CYCLE_STATUS: "{cycle_id}" → {status}')


def cmd_cycle_rename(cycle_id: str, title: str) -> None:
    with _ledger_lock():
        state = _read_state()
        cycle = _find_cycle(state, cycle_id)
        cycle['title'] = title
        cycle['updated_at'] = _now()
        _write_state(state)
    print(f'RENAMED: "{cycle_id}" → "{title}"')


# ── the launch dir ───────────────────────────────────────────────────────────

def _launch_dir(cycle_id: str, node_id: str) -> Path:
    """`.pf/meta/launch/<cycle>/<node>/` —— 派发那一格的落点（prompt / claim / started 都在里面）。"""
    return meta_dir_for(None) / 'launch' / cycle_id / node_id

