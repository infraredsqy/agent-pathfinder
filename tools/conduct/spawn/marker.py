#!/usr/bin/env python3
"""断链降级标记的正本：它落在哪、长什么样、怎么写、谁在什么条件下写。

一次失败意味着这条链不会再自己往前走。**标记存在 = 链现在是断的** —— 它是人工进来接管的
唯一入口，所以它必须显眼：写不进盘、静默降级都是这条判据失效的形态，也正是这个模块存在的
理由。调用点收成两处：python 侧（`handoff.py` 的条子写不下去那一拍）直接用下面这三个
函数；两份 spawner 共用的那一层（`spawn/spawn-common.ps1`）按同一份形状自己写 —— 那条例外与它的
理由写在 `写法` 那一条里。

    落点    marker_path(project, cycle) —— `<project>/.pf/meta/CONDUCT-CALLBACK-FAILED-<cycle>.json`
            按线分文件：并行=2 时两条线并排跑，本线成功只清本线的标记，不抹别线的断链记号。
    形状    record(cycle, phase, reason, attempts=, workspace=, extra=) —— 一份，写它的
            每一条路都长出同一个形状；宿主自己的事实（dsh 的 api_base、zcode 的 db）走 `extra`
            那一个槽，漂移不再落在字段上。`detected_at` 是这个记录自己的时刻，取
            `tools/shared/clock.py` 的微秒形状（`clock.now()`）—— 时刻的形状只有那一处正本。
    写法    write(path, payload) —— `tools/shared/jsonio.py` 的原子写：临时文件 + 同卷改名 +
            对「读方此刻正开着目标文件」的重试。全树写 `.pf/` 记录都走这条原语。
            **一处例外**：spawner 那一层（`spawn/spawn-common.ps1`）用纯 PowerShell 写同一份形状。
            理由是物理上的 —— spawner 跑起来的那台机器、那个时刻可能没有 python，而 PowerShell
            没法 import 这个模块，这条写入又恰恰是链断掉之后唯一留给人的入口。两份实现的形状由
            `conduct/tests/cases_spawn_shell.py` 的 `TestTheSpawnerShell` 钉住（同一组键、同一句
            人话、同一个时刻形状）。
    谁写    失败的那一拍自己：spawner 在自己的失败点上写（`spawn/spawn-common.ps1` 那一层自己写盘，
            形状照这份），条子写不下去的那一拍由 `handoff.py` 写。
            **一处失败只落一条**：第一次的那条是根因，后面的不再覆盖它。
    谁清    spawner 成功路径（`spawn/spawn-common.ps1`）：删文件没有形状可言，而成功那一拍不该
            因为清不掉而失败（清不掉不许挡派发成功），所以清不经过这里。
    谁删    人。接管完、确认无碍，人自己删掉本文件。

`.pf` 这个名字与 `meta/` 这一层从 `tools/shared/ledger.py` 借（`meta_dir_for`）：派发侧不再自
拼第二份，名字一改，标记跟着记录一起走。

命令行是给人排障、演练用的那一面：`python marker.py write --path …` 按这份形状手写一条标记。
spawner 不再调它（见上面那条例外）；python 侧直接用 `marker_path` / `record` / `write`。

Loaded by PATH, never by name, like every other file in this directory: the callers are themselves
loaded by path, and a by-name import would hand the second load the first load's modules.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONDUCT = HERE.parent                      # the conduct tool directory

# 引导：装载器也按路径取，所以这一段只能每个文件自己带（见 `shared/loader.py`）。
SHARED = CONDUCT.parent / 'shared'

#: 标记文件名的唯一拼法。一条线一个文件，`{cycle}` 就是 spawner 递过来的那个 cycle id。
MARKER_NAME = 'CONDUCT-CALLBACK-FAILED-{cycle}.json'

#: 标记自己写下的那句人话：链断在哪、怎么接管、什么时候可以删。写它的每一条路都用这一条 ——
#: 同一件事只有一份说法。
DEFAULT_NOTE = ('链条断在这里：本拍没能把下一棒开起来，管线不会再自己往前走。手动接管是 '
                '`pf next --after <node_id>`（收尾那一拍）或 `pf dispatch <cycle>`（派发那一拍）；'
                '确认无碍后删掉本文件。')

_LOADER = None


def _loader():
    """`tools/shared/loader.py`: the one loader, for the modules this tree keeps outside `shared/`."""
    global _LOADER
    if _LOADER is None:
        spec = importlib.util.spec_from_file_location('pf_shared_loader', SHARED / 'loader.py')
        _LOADER = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_LOADER)
    return _LOADER


def _ledger():
    """`tools/shared/ledger.py`: `.pf/` 与它底下的 `meta/` —— 正本一处。"""
    return _loader().load_shared('ledger')


def _clock():
    """`tools/shared/clock.py`: 记录里的时刻 —— 形状与「现在」都取这一处正本。"""
    return _loader().load_shared('clock')


def marker_path(project, cycle: str) -> Path:
    """这条线的降级标记落在哪：`<project>/.pf/meta/CONDUCT-CALLBACK-FAILED-<cycle>.json`。

    全树拼这条路径只有这里一处：`argv_for` 把它的结果作为 `-MarkerPath` 交给 spawner，
    `chain-spawn.py` 从它取报错里那句「标记在哪」。
    """
    return _ledger().meta_dir_for(project) / MARKER_NAME.format(cycle=cycle)


def record(cycle: str, phase: str, reason: str, *, attempts: int = 0, workspace='',
           extra=None) -> dict:
    """标记在盘上的那份记录，一处长成。

    `phase` 是断在哪一步（spawner 自己的步骤名），`reason` 是那一拍的原话，`attempts` 是那一步
    数过的尝试次数（没数就是 0），`workspace` 是这条线的项目根，`extra` 是宿主自己的事实
    （dsh 的 api_base、zcode 的 db）：它们对一个来排障的人是线索，所以留一个槽，而不是让两份
    spawner 各长一套字段。`note` 是这份形状固定的那句话（`DEFAULT_NOTE`）。
    """
    return {
        'cycle_id': str(cycle),
        'phase': str(phase),
        'attempts': int(attempts),
        'reason': str(reason),
        'workspace': str(workspace),
        'extra': dict(extra or {}),
        'detected_at': _clock().now(),
        'note': DEFAULT_NOTE,
    }


def write(path, payload: dict) -> Path:
    """把一份标记落盘：`jsonio` 的原子写（临时文件 + 同卷改名 + 重试）。

    裸 `os.replace` 在 Windows 上会撞上「读方此刻正开着目标文件」而 PermissionError，而这条
    路径恰恰是那条不许丢的写入 —— 所以它和全树别的 `.pf/` 记录走同一条原语。写失败让异常出去
    （`PermissionError` 是 `OSError` 的一种）：这一拍要不要吞、怎么说话，是调用方的判断。
    """
    path = Path(path)
    _loader().load_shared('jsonio').atomic_write_json(path, payload)
    return path


def _extra_pairs(values) -> dict:
    """`KEY=VALUE` 那一串（spawner 递宿主事实的写法）解析成一个对象。"""
    pairs = {}
    for item in values or []:
        key, _, value = str(item).partition('=')
        if key.strip():
            pairs[key.strip()] = value
    return pairs


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(
        prog='marker.py',
        description='写一条断链降级标记：形状、落点与重试都在这一处，spawner 只递事实。')
    verbs = parser.add_subparsers(dest='verb', required=True)
    writer = verbs.add_parser('write', help='写一条标记（已存在的那条不覆盖由调用方判断）')
    writer.add_argument('--path', required=True, help='标记的完整路径（argv_for 从 marker_path 取）')
    writer.add_argument('--cycle', required=True, help='cycle id（记进 cycle_id）')
    writer.add_argument('--phase', required=True, help='断在哪一步')
    writer.add_argument('--reason', required=True, help='那一拍的原话')
    writer.add_argument('--attempts', type=int, default=0, help='那一步数过的尝试次数（没数就是 0）')
    writer.add_argument('--workspace', default=None, help='项目根（缺省从标记路径推）')
    writer.add_argument('--extra', action='append', default=[], metavar='KEY=VALUE',
                        help='宿主自己的事实，可给多次')
    args = parser.parse_args(argv)

    # 标记落点定下了项目根（`<project>/.pf/meta/…`），只有给了 -WorkspacePath 时才用它自己那一份。
    workspace = args.workspace or Path(args.path).parent.parent.parent
    payload = record(args.cycle, args.phase, args.reason, attempts=args.attempts,
                     workspace=workspace, extra=_extra_pairs(args.extra))
    try:
        write(args.path, payload)
    except OSError as e:
        print(f'写降级标记失败（{args.path}）：{e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())