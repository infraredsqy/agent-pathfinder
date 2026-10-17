"""这台机器与这个项目的设置：出厂默认 → 全局覆盖 → 项目覆盖，越靠后越赢。

值只住三个地方，别处不许再写第二份：

    出厂默认    `CONFIG_SPEC` —— 唯一一处定义（改默认值只改这一张表）
    全局覆盖    `~/.pf/config.json`（跟人／机器走，不进任何项目的版本库）
    项目覆盖    `<项目>/.pf/config.json`（随项目进版本库；`init` 摆一个空的 `{}`）

**两层覆盖都可以不在**：缺文件读作"没改过默认值"，一切照出厂默认走。全局那一层谁都不自动生成
—— 只有人真想改全局默认时才由 `pf config set --global` 写出来；项目那一层由 `init` 写一个空
对象，位置一眼可见，而空对象读作"没有覆盖"。`list` 印每一键的有效值和它来自哪一层，`get` 只
印值（技能读的就是它）。

`worktree_mode` 的缺省不是一个值而是一个条件 —— git 项目 `fork`、其余 `off` —— 所以它由
`default_of()` 算出来，表里那一格留 `None`。四个键同时也是 `CONFIG_SPEC` 的形状：`set` 与
`get` 的候选表直接取自它，盘上读到的值按同一张表校验，读不懂就当场报出来而不是悄悄落回默认。
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

from .store import (
    LEDGER_NAME,
    _atomic_write,
    _ledger_lock,
    _read_json,
    ledger_dir_for,
)

#: 覆盖层的文件名，两层同名。
CONFIG_NAME = 'config.json'

#: 三个键的唯一定义处：出厂默认、形状、一句话说明。`kind` 是 `parse_value` 唯一的分派依据 ——
#: 加一个键就是加这一格。
CONFIG_SPEC = {
    'max_agents': {
        'kind': 'count',
        'default': 3,
        'summary': '一个会话里同时在飞的子 agent 上限',
    },
    'test_tier': {
        'kind': 'choice',
        'values': ('full', 'minimal'),
        'default': 'full',
        'summary': '这个项目怎么验：full 跑全套件／minimal 只跑项目口径那几条检查',
    },
    'worktree_mode': {
        'kind': 'choice',
        'values': ('off', 'fork', 'reuse'),
        'default': None,
        'summary': '一个节点要不要另开一棵 worktree：git 项目缺省 fork，其余缺省 off',
    },
    'issue_repair': {
        'kind': 'choice',
        'values': ('on', 'off'),
        'default': 'on',
        'summary': '一条线越过它自己的维护阈值时，要不要自动插一个还债的节点',
    },
}

#: 覆盖层的名字，从低到高：先出厂默认，再全局，最后项目。
LAYERS = ('global', 'project')

#: 有效值是从哪一层来的，给人读的写法。
LAYER_LABELS = {
    'builtin': '出厂默认',
    'global': f'全局 ~/{LEDGER_NAME}/{CONFIG_NAME}',
    'project': f'项目 {LEDGER_NAME}/{CONFIG_NAME}',
}


# ── 落点 ──────────────────────────────────────────────────────────────────────
# 项目那一层从 `ledger_dir_for` 走，不在别处拼 `.pf/` 底下的路径（那条纪律见 store）。
# 全局那一层是家目录里同样形状的一份：`~/.pf/config.json`。

def global_config_path() -> Path:
    """全局覆盖的落点。`PF_HOME_DIR` 只给测试指到沙箱（同 `ZCODE_DATA_BASE_DIR` 的先例）。"""
    home = os.environ.get('PF_HOME_DIR') or str(Path.home())
    return Path(home).expanduser() / LEDGER_NAME / CONFIG_NAME


def project_config_path(project: str | None = None) -> Path:
    return ledger_dir_for(project) / CONFIG_NAME


def layer_path(layer: str, project: str | None = None) -> Path:
    return global_config_path() if layer == 'global' else project_config_path(project)


def project_root_of(project: str | None = None) -> Path:
    """这个项目的根：记录目录的上一级。`worktree_mode` 的缺省要问它是不是 git 检出。"""
    return ledger_dir_for(project).parent


# ── 读一层 ────────────────────────────────────────────────────────────────────

#: 每种 kind 一处规则：给它一个值（盘上读到的那个 JSON 值，或命令行给的那串字），合法就交出配置
#: 域里的值，不合法给 None。读盘与写盘两侧都从这里过 —— 加一个 kind、改一个 kind 的边界，动的是
#: 这一节；`CONFIG_SPEC` 每一格的 `kind` 是唯一的分派依据。
def _parse_count(spec: dict, value):
    """`count`：正整数。布尔不算数；命令行那串 `"3"` 与盘上那个 3 一样，都算出 3。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            return None
    return value if isinstance(value, int) and value >= 1 else None


def _parse_choice(spec: dict, value):
    """`choice`：这一格自己的候选表里的词。"""
    return value if value in spec['values'] else None


_KINDS = {'count': _parse_count, 'choice': _parse_choice}


def parse_value(spec: dict, value):
    """这一个键的一个值：按 `spec['kind']` 的规则解析并校验，不合法给 None。

    两侧共用同一份：`read_layer` 拿它校验盘上读到的值，`cmd_config_set` 拿它校验命令行给的值。
    各写一份的下场是「写得进、读不出」（或反过来）—— 一个 kind 的边界只在这里定义一次。
    """
    return _KINDS[spec['kind']](spec, value)


def _shape_error(key: str, value, where: Path) -> None:
    print(f'Error: {where} 的 {key} 不是一个合法的值：{value!r}', file=sys.stderr)
    sys.exit(1)


def _checked(key: str, value, where: Path):
    """盘上读到的一个值，按 `CONFIG_SPEC` 校验后交出来。

    读不懂就当场停下：一个写坏的设置悄悄落回默认，等于这一项看起来"没配过"——而它其实配错了。
    """
    checked = parse_value(CONFIG_SPEC[key], value)
    if checked is None:
        _shape_error(key, value, where)
    return checked


def read_layer(layer: str, project: str | None = None) -> dict:
    """一层覆盖里写下的键。文件不在就是空的（正常态）；写坏了就吵一句。"""
    path = layer_path(layer, project)
    data = _read_json(path, str(path))
    if data is None:
        return {}
    if not isinstance(data, dict):
        print(f'Error: {path} 要是一个 JSON 对象（键 → 值）', file=sys.stderr)
        sys.exit(1)
    out = {}
    for key, value in data.items():
        if key not in CONFIG_SPEC:
            print(f'Warning: {path} 里的 {key!r} 不是可配的键，已跳过（可配的：'
                  f'{", ".join(sorted(CONFIG_SPEC))}）', file=sys.stderr)
            continue
        out[key] = _checked(key, value, path)
    return out


def default_of(key: str, project: str | None = None):
    spec = CONFIG_SPEC[key]
    if spec['default'] is not None:
        return spec['default']
    return 'fork' if (project_root_of(project) / '.git').exists() else 'off'


def resolve_config(project: str | None = None) -> dict:
    """有效值：出厂默认 → 全局 → 项目，后一层盖前一层。"""
    out = {key: default_of(key, project) for key in CONFIG_SPEC}
    for layer in LAYERS:
        out.update(read_layer(layer, project))
    return out


def config_sources(project: str | None = None) -> dict:
    """每一键的有效值来自哪一层：`builtin` / `global` / `project`。"""
    out = {key: 'builtin' for key in CONFIG_SPEC}
    for layer in LAYERS:
        for key in read_layer(layer, project):
            out[key] = layer
    return out


# ── 写一层 ────────────────────────────────────────────────────────────────────
# 项目那一层跟着记录目录一起上锁：它和 state.json 一样是整份读-改-写，两条线同时 `config set`
# 会丢一笔。全局那一层没有共享锁可言（它不属于任何一个项目），照 `_atomic_write` 的原子替换走。

def _parse_for_write(key: str, value: str):
    """命令行那一侧的一个值：走同一个 kind 的规则，拒绝的话由这一侧说出来。"""
    spec = CONFIG_SPEC[key]
    stored = parse_value(spec, value)
    if stored is None:
        if spec['kind'] == 'count':
            print(f'Error: {key} 要一个正整数，读到的是 {value!r}', file=sys.stderr)
        else:
            print(f'Error: {key} 要 {list(spec["values"])} 里的一个，读到的是 {value!r}',
                  file=sys.stderr)
        sys.exit(1)
    return stored


def write_value(key: str, value, layer: str, project: str | None = None) -> None:
    """把一层的某一个键写进去（`value is None` ＝ 拿掉这一层的覆盖）。

    项目那一层走 `_ledger_lock`，它自己就是那道闸：没 init 过的 `.pf/` 什么都不许写。全局那一层
    属于这台机器而不属于任何项目，没有共享的锁可言 —— `nullcontext` 让两条路写出同一个形状。
    """
    path = layer_path(layer, project)
    with (_ledger_lock(project) if layer == 'project' else contextlib.nullcontext()):
        data = _read_json(path, str(path)) if path.exists() else {}
        if not isinstance(data, dict):
            data = {}
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
        _atomic_write(path, data)


# ── 命令 ──────────────────────────────────────────────────────────────────────

def cmd_config_list(project: str | None = None) -> None:
    effective = resolve_config(project)
    sources = config_sources(project)
    width = max(len(key) for key in CONFIG_SPEC)
    for key in CONFIG_SPEC:
        print(f'{key.ljust(width)} = {effective[key]}（{LAYER_LABELS[sources[key]]}）')


def cmd_config_get(key: str, project: str | None = None) -> None:
    """只印值：调用方（技能、脚本）拿到的就是这一行。"""
    print(resolve_config(project)[key])


def cmd_config_set(key: str, value: str, layer: str = 'project',
                   project: str | None = None) -> None:
    stored = _parse_for_write(key, value)
    write_value(key, stored, layer, project)
    print(f'CONFIG: {key} = {stored}（写进 {LAYER_LABELS[layer]}）')
    _report_effective(key, project)


def cmd_config_unset(key: str, layer: str = 'project', project: str | None = None) -> None:
    write_value(key, None, layer, project)
    print(f'CONFIG: {key} 的覆盖已从 {LAYER_LABELS[layer]} 拿掉')
    _report_effective(key, project)


def _report_effective(key: str, project: str | None) -> None:
    """写完补一句有效值：写的是全局、项目另有一层覆盖时，那一句就是全部的差别。"""
    effective = resolve_config(project)[key]
    source = config_sources(project)[key]
    print(f'CONFIG: 有效值 {key} = {effective}（来自 {LAYER_LABELS[source]}）')