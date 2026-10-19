"""`init`: the one setup act, and the only thing that ever writes these two files.

A project either commits its `.pf/` or keeps that directory to itself. Which one it is gets decided
here, once, and every other command just reads what is on disk -- a `.pf/` the project's git tracks
is the committed kind, anything else is the local kind. Nothing else records the choice, so there is
no second place for it to drift out of step with reality.

What init writes:

    .pf/.gitignore       `meta/ worktrees/ temp/` -- the tool's own machinery. Both modes want that
                          ignored: the committed kind needs it out of the project's history, the
                          own-repo kind needs it out of the ledger's own.
    .pf/.gitattributes   no EOL conversion on the record files, and the entry-wise merge for the
                          four that must not be merged line by line
    .pf/AGENTS.md        how this directory is to be used. The tool owns it, not the host: every
                          project that runs pf gets the same discipline, on any harness

and, for `--local`, one line in the repository's `info/exclude`. That file belongs to the clone and
never travels, so no teammate ever sees a line about a directory that does not exist for them.
`--commit` takes the line back out.

`--own-repo` gives `.pf/` a repository of its own, which buys local history and -- once it is given
a remote -- the trip between machines. That `git init` is the only git command this file runs, and it
is a setup act by construction: no runtime command ever touches a repository.
"""

import os
import subprocess
import sys
from pathlib import Path

from .store import LEDGER_NAME, _ledger, _resolve_project_root

#: `.pf/` 底下三样机械空间。提交档下它替项目仓库挡住这三样，自成仓库时替那份仓库挡。
GITIGNORE_BODY = """\
# 这套工具自己的机械空间：派发落点、worktree、节点跑出来的临时件。
meta/
worktrees/
temp/
"""

#: 模式相对 `.pf/`，所以同一份文件在两种档位下都对：提交档下它管的是 `<项目>/.pf/…`，
#: 自成仓库时管的是那份仓库的根。
GITATTRIBUTES_BODY = """\
# 记录不进 EOL 转换：工具在 Windows 上写 CRLF，各机的 core.autocrlf 不该改动它。
** -text

# 记录按条目合并，不按行；驱动实现在 tools/conduct/ledger-merge.py，
# 命令注册在每台机器的 git config（pf register）。
ideas.json merge=pf
state.json merge=pf
instructions.json merge=pf
signals/** merge=pf
"""

#: `.pf/` 自己的使用约定，由 `init` 写入，跟着项目走：谁跑 pf 都看见同一份纪律，
#: 不必为它在每次工具调用上挂一个钩子。要改它，先问用户。
LEDGER_AGENTS_BODY = """\
# .pf 使用约定

> 本文件（`.pf/AGENTS.md`）不得超过 100 行。
> 本文件由这套工具在 `init` 时写入并维护。**要改它，先问用户**——他说了「可以」，再动手。

本目录是 pf 管线的记录库：`ideas.json`、`state.json`、`instructions.json`、
`signals/` 是管线的记录，`nodes/<uuid>/` 是每一格（DAG 节点）的 spec 与交付物。

- 记录只经 `pf` 读写（`pf dag …`、`pf issue …`、`pf next`）；禁止手编 JSON，也不要直接 read `ideas.json` 原文——用工具的 list / plan 输出。
- 属于某一格的过程产物（探针、原始输出、自证脚本）放 `.pf/nodes/<uuid>/temp/`，临时不入库，别当交付物提交。
- 其他一切临时产物放工作区根的 `.tmp/`。
- `meta/`、`worktrees/`、`nodes/<id>/temp/` 由 `.gitignore` 挡在版本库外；别把这些里面的东西手工搬出去提交。
"""

#: 写进 `info/exclude` 的那两行的记号。按记号收割：种一次是它，拔一次也是它。
EXCLUDE_MARK = '# pf 工作系统：这套记录只留本机，不进团队库'

#: 这套工具树里 `pf` 的位置：`<tools>/conduct/conduct_state/` 往上三级就是 `<tools>`。
TOOLS = Path(__file__).resolve().parent.parent.parent


def driver_command() -> str:
    """登记进 `merge.pf.driver` 的那条命令。

    首选 PATH 上的 `pf`：这条命令不带绝对路径，checkout 搬到别处、换一台机器，项目里已经登记的
    那一行照样有效，不必逐项目重来。PATH 上还没有 `pf` 时退回绝对形式，并请人去补 PATH——补好
    之后重跑一次 `pf register`，登记的那行就跟位置无关了。
    """
    for entry in os.environ.get('PATH', '').split(os.pathsep):
        if entry and (Path(entry) / 'pf').is_file():
            return 'pf merge-ledger %O %A %B %P'
    return f'"{sys.executable}" -X utf8 "{TOOLS / "bin" / "pf.py"}" merge-ledger %O %A %B %P'


def register_driver(project: Path) -> str:
    """把一个项目的合并驱动登记好，返回登记的那条命令。"""
    command = driver_command()
    _git(project, 'config', 'merge.pf.name', 'pf ledger three-way merge')
    _git(project, 'config', 'merge.pf.driver', command)
    return command


def cmd_register(project: str | None = None) -> None:
    """`register`：登记合并驱动，并把登记的那条命令打出来。

    `init` 里已经顺手做了这件事；这个动词在这里，是为了让一个已经 init 过的项目能单独把驱动补齐
    （换机器、换 checkout 位置之后重跑它一次就够），也让 `pf register` 有个可转发的落点。
    """
    root = Path(project).expanduser().resolve() if project else _resolve_project_root(Path.cwd())
    print(register_driver(root))


def _git(project: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(['git', *args], cwd=str(project), capture_output=True, text=True,
                          encoding='utf-8', errors='replace')


def _exclude_file(project: Path) -> tuple[Path, str] | None:
    """这个项目的仓库忽略清单（`info/exclude`）与要写进去的那条模式。

    不是 git 项目、或者 git 不在手上时返回 None：那种项目没有能被扫进去的地方，`--local`
    的忽略行也就无从谈起。模式带上项目相对仓库根的前缀——项目可能是仓库里的一个子目录。
    """
    if _git(project, 'rev-parse', '--git-dir').returncode != 0:
        return None
    common = _git(project, 'rev-parse', '--git-common-dir').stdout.strip()
    prefix = _git(project, 'rev-parse', '--show-prefix').stdout.strip()
    if not common:
        return None
    gitdir = Path(common)
    if not gitdir.is_absolute():
        gitdir = (project / gitdir).resolve()
    return gitdir / 'info' / 'exclude', f'/{prefix}{LEDGER_NAME}/'


def _write_exclude(path: Path, pattern: str | None) -> str:
    """种下或拔掉 `info/exclude` 里那两行，返回一句话说明做了什么。

    按记号收割而不是按模式收割：模式随项目在仓库里的位置变（子目录项目），记号不变。
    """
    lines = path.read_text(encoding='utf-8', errors='replace').splitlines() if path.exists() else []
    kept, skipped = [], False
    for line in lines:
        if skipped:
            skipped = False
            continue
        if line.strip() == EXCLUDE_MARK:
            skipped = True
            continue
        kept.append(line)
    had = len(kept) != len(lines)
    if pattern is not None:
        while kept and not kept[-1].strip():
            kept.pop()
        if kept:
            kept.append('')
        kept += [EXCLUDE_MARK, pattern]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(kept) + '\n', encoding='utf-8')
        return f'已写进 {path}：{pattern}' + ('（换了新的模式）' if had else '')
    if not had:
        return f'{path} 里本来就没有 pf 的忽略行。'
    path.write_text('\n'.join(kept) + ('\n' if kept else ''), encoding='utf-8')
    return f'已从 {path} 拔掉 pf 的忽略行。'


def _untrack(project: Path, ledger: Path) -> str:
    """本地档：已经进了项目仓库的 `.pf/` 要从 index 里退出来，文件留在盘上。

    不退的话档位会撒谎——盘上看着是本地档（忽略行在、下一发不带它），而项目仓库仍把整个
    目录带走。忽略行管不了已经 track 的文件，这一条只有 index 说了算。
    """
    if _git(project, 'ls-files', '--', LEDGER_NAME).stdout.strip() == '':
        return '项目仓库本来就没 track `.pf/`，index 没动。'
    done = _git(project, 'rm', '-r', '-q', '--cached', '--', LEDGER_NAME)
    if done.returncode != 0:
        return f'Warning: `git rm --cached` 没成：{done.stderr.strip()}'
    return '已把 `.pf/` 从项目仓库的 index 里退出来（文件都还在盘上，随你提交这一发）。'


def cmd_init(project: str | None = None, mode: str = 'local', own_repo: bool = True) -> None:
    """把 `.pf/` 摆成一个能用的证据目录，并定下它是进项目库还是只留本机。

    幂等：两枚文件每次都照常量重写，忽略行照着模式种或拔。已经 init 过的项目重跑一次没有副
    作用，换档也只是再跑一次 —— `--local` 与 `--commit` 各自把 `info/exclude` 那一行安顿好。
    """
    root = Path(project).expanduser().resolve() if project else _resolve_project_root(Path.cwd())
    ledger = _ledger().ledger_dir_for(root)
    own_repo = own_repo and mode == 'local'
    ledger.mkdir(parents=True, exist_ok=True)
    (ledger / '.gitignore').write_text(GITIGNORE_BODY, encoding='utf-8')
    (ledger / '.gitattributes').write_text(GITATTRIBUTES_BODY, encoding='utf-8')
    (ledger / 'AGENTS.md').write_text(LEDGER_AGENTS_BODY, encoding='utf-8')
    # 设置文件：摆一个空对象，位置一眼可见；空对象读作"全按出厂默认"。已经有内容时一个字都不动
    # —— init 是幂等的，而这一份是人／agent 会往里写东西的，重跑一次不能把人写下的覆盖清掉。
    config_file = _ledger().config_path_of(ledger)
    made_config = not config_file.exists()
    if made_config:
        config_file.write_text('{}\n', encoding='utf-8')

    print(f'记录目录：{ledger}')
    print(f'模式：{"只留本机（.pf/ 不进团队库）" if mode == "local" else "跟项目一起进版本库"}')
    print(f'设置文件：{config_file}'
          f'（{"新摆的空对象" if made_config else "已经在，没动它"}；'
          f'空＝全按出厂默认，要改就用 `pf config set`）')

    exclude = _exclude_file(root)
    if exclude is None:
        print('这个项目不是一个 git 检出：忽略行无从谈起，整套 pull / push 也都不适用。')
    else:
        path, pattern = exclude
        print(_write_exclude(path, pattern if mode == 'local' else None))
        if mode == 'local':
            print(_untrack(root, ledger))

    if own_repo and not (ledger / '.git').exists():
        done = _git(ledger, 'init', '--quiet')
        if done.returncode == 0:
            print(f'已给 {ledger} 开一份自己的仓库：本地历史从这里开始，'
                  f'给它接一个远端之后，它也能自己走另一台机器。')
        else:
            print(f'Warning: `git init` 没成：{done.stderr.strip()}', file=sys.stderr)
    elif own_repo:
        print(f'{ledger} 已经有一份自己的仓库了，没动它。')

    if mode == 'local':
        print('提醒：`.pf/` 是被忽略的，`git clean -xdf` 会把它连里面的历史一起删掉。'
              '接一个远端是唯一能救回来的办法。')

    if _git(root, 'rev-parse', '--git-dir').returncode == 0:
        command = register_driver(root)
        print(f'合并驱动已登记：merge.pf.driver = {command}')
        if not command.startswith('pf '):
            print('  注：`pf` 还不在 PATH 上，先登记了绝对形式。把它所在的 bin 目录放进 PATH，'
                  '再重跑一次 `pf register`，这行就跟位置无关了。')
