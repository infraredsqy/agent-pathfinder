#!/usr/bin/env python3
"""`migrate`: take one project's ledger from `.nav/` to `.pf/`, driver and all.

    python migrate-ledger.py [--project DIR] [--mode commit|local] [--dry-run] [--force]

Run it once per project, on a quiet workspace. It exists because the ledger's directory name and the
git attribute that names its merge driver both changed: a project that adopted the tool under the
old names keeps a `.nav/` full of records and a `merge.nav-ledger.driver` in its own git config.

What it changes
---------------
    .nav/                     ->  .pf/            (git mv when the project's git tracks the record)
    <project>/.gitattributes  the stale `nav/**` / `.nav/**` lines go; the ledger's own
                              `.gitattributes` owns those rules now
    <clone>/info/exclude      the old `# nav 工作系统…` block goes; `init` writes the new mark
    git config                `merge.nav-ledger.*` unset
    then `init` runs:         `.pf/.gitignore`, `.pf/.gitattributes` (attribute `merge=pf`),
                              `.pf/AGENTS.md`, the exclude line, and the `merge.pf` registration

What it refuses
---------------
No `.nav/`, an existing `.pf/`, a ledger with uncommitted changes (`--force` overrides the last one).
Linked worktrees are reported, never touched: each keeps a `.nav/` of its own, and only a checkout
run inside that worktree can bring it forward.
"""

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

OLD, NEW = '.nav', '.pf'
OLD_DRIVER = 'merge.nav-ledger'
OLD_MARK = '# nav 工作系统'
#: The rules that lived in the project's own `.gitattributes` before the ledger carried them. Both
#: layouts' spellings go, so a project that skipped a generation still ends up clean.
STALE_ATTR_PREFIXES = ('nav/', '.nav/')

HERE = Path(__file__).resolve().parent
STATE_MANAGE = HERE / 'pipeline' / 'state-manage.py'


def say(verb: str, what: str) -> None:
    print(f'{verb:<14}{what}')


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(['git', *args], cwd=str(root), capture_output=True, text=True,
                          encoding='utf-8', errors='replace')


def is_repo(root: Path) -> bool:
    return git(root, 'rev-parse', '--git-dir').returncode == 0


def is_tracked(root: Path) -> bool:
    return bool(git(root, 'ls-files', '--', OLD).stdout.strip())


def linked_worktrees(root: Path) -> list[str]:
    """Every worktree of this repository except `root` itself."""
    out = git(root, 'worktree', 'list', '--porcelain').stdout
    if not out:
        return []
    others, current = [], None
    for line in out.splitlines():
        if line.startswith('worktree '):
            current = line[len('worktree '):].strip()
        elif line.startswith('branch ') and current:
            if Path(current).resolve() != root.resolve():
                others.append(f'{current}  [{line[len("branch "):].strip()}]')
    return others


def drop_stale_attributes(root: Path) -> int:
    """Take the ledger's own merge/EOL rules out of the project's `.gitattributes`.

    They belong to `.pf/.gitattributes` now, where the patterns are relative to the ledger itself.
    Only the lines whose pattern starts with an old ledger path are dropped, and a block of comments
    left behind by an earlier pass goes with them.
    """
    path = root / '.gitattributes'
    if not path.is_file():
        return 0
    kept, dropped, pending = [], 0, []
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        stripped = line.strip()
        if stripped.startswith('#') and any(p in stripped for p in STALE_ATTR_PREFIXES):
            pending.append(line)                      # a comment that introduces a stale rule
            continue
        if stripped and not stripped.startswith('#') and stripped.split()[0].startswith(STALE_ATTR_PREFIXES):
            dropped += 1
            pending = []
            continue
        kept.extend(pending)
        pending = []
        kept.append(line)
    if dropped:
        path.write_text('\n'.join(kept) + ('\n' if kept else ''), encoding='utf-8')
    return dropped


def drop_old_exclude(root: Path) -> bool:
    """Remove the `# nav 工作系统…` block from the clone's `info/exclude`, mark and pattern alike."""
    common = git(root, 'rev-parse', '--git-common-dir').stdout.strip()
    if not common:
        return False
    gitdir = Path(common)
    if not gitdir.is_absolute():
        gitdir = (root / gitdir).resolve()
    path = gitdir / 'info' / 'exclude'
    if not path.is_file():
        return False
    lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    kept, skipped, hit = [], False, False
    for line in lines:
        if skipped:
            skipped = False
            hit = True
            continue
        if line.strip() == OLD_MARK:
            skipped = True
            continue
        kept.append(line)
    if hit:
        while kept and not kept[-1].strip():
            kept.pop()
        path.write_text('\n'.join(kept) + ('\n' if kept else ''), encoding='utf-8')
    return hit


def unset_old_driver(root: Path) -> bool:
    if not is_repo(root):
        return False
    had = bool(git(root, 'config', '--get', f'{OLD_DRIVER}.driver').stdout.strip())
    git(root, 'config', '--unset-all', f'{OLD_DRIVER}.name')
    git(root, 'config', '--unset-all', f'{OLD_DRIVER}.driver')
    return had


def backup(root: Path, ledger: Path) -> Path:
    """An untracked ledger has no history to fall back on, so copy it before the rename."""
    stamp = time.strftime('%Y%m%d-%H%M%S')
    dest = root / f'{OLD}.bak-{stamp}'
    shutil.copytree(ledger, dest, symlinks=True)
    before = sum(1 for _ in ledger.rglob('*'))
    after = sum(1 for _ in dest.rglob('*'))
    if before != after:
        print(f'备份对不上：{before} 个条目 vs {after} 个。先别继续，删掉 {dest} 再查一遍。',
              file=sys.stderr)
        sys.exit(1)
    return dest


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--project', default=None, help='项目根（缺省：从 cwd 往上找 .git）')
    ap.add_argument('--mode', choices=('commit', 'local'), default=None,
                    help='记录跟项目一起进版本库（commit）还是只留本机（local）；缺省按现状')
    ap.add_argument('--dry-run', action='store_true', help='只打印计划，不落笔')
    ap.add_argument('--force', action='store_true', help='记录目录有未提交改动时也继续')
    args = ap.parse_args(argv)

    root = Path(args.project).expanduser().resolve() if args.project else Path.cwd().resolve()
    ledger = root / OLD
    target = root / NEW

    if not ledger.is_dir():
        hint = f'（旧的 `nav/` 布局先手工改名成 {OLD}/，再跑我）' if (root / 'nav').is_dir() else ''
        print(f'这里没有 {OLD}/：{root}{hint}', file=sys.stderr)
        return 1
    if target.exists():
        print(f'{NEW}/ 已经在了：{root}。这次迁移没什么可做的。', file=sys.stderr)
        return 1

    tracked = is_tracked(root)
    mode = args.mode or ('commit' if tracked else 'local')
    dirty = git(root, 'status', '--porcelain', '--', OLD).stdout.strip() if is_repo(root) else ''
    if dirty and not args.force:
        print(f'{OLD}/ 里有未提交的改动，先收干净（或 --force）：\n{dirty}', file=sys.stderr)
        return 1
    worktrees = linked_worktrees(root)

    say('项目根', str(root))
    say('记录目录', f'{OLD}/ -> {NEW}/（{"git mv，项目在跟踪它" if tracked else "文件系统改名，项目不跟踪它"}）')
    say('档位', '跟项目一起进版本库' if mode == 'commit' else '只留本机')
    say('清理', f'项目 .gitattributes 的 {" / ".join(STALE_ATTR_PREFIXES)} 行、info/exclude 的旧记号、'
                f'{OLD_DRIVER}.* 注册')
    say('随后', '跑一次 init：写 .pf/.gitignore、.pf/.gitattributes、.pf/AGENTS.md，登记 merge.pf')
    if worktrees:
        say('注意', f'这个仓库还有 {len(worktrees)} 个链接工作树，它们的记录目录要各自跟上：')
        for wt in worktrees:
            say('', f'  {wt}')

    if args.dry_run:
        say('dry-run', '一个字节都没写。')
        return 0

    if not tracked:
        dest = backup(root, ledger)
        say('备份', str(dest))
    if tracked and not (ledger / '.git').exists():
        done = git(root, 'mv', OLD, NEW)
        if done.returncode != 0:
            print(f'git mv 没成：{done.stderr.strip()}', file=sys.stderr)
            return 1
    else:
        ledger.rename(target)
    say('改名', f'{OLD}/ -> {NEW}/')

    dropped = drop_stale_attributes(root)
    if dropped:
        say('属性', f'项目 .gitattributes 摘掉 {dropped} 条旧规则')
    if drop_old_exclude(root):
        say('忽略行', '拔掉 info/exclude 里的旧记号')
    if unset_old_driver(root):
        say('驱动', f'抹掉 {OLD_DRIVER}.* 注册')

    init_argv = [sys.executable, '-X', 'utf8', str(STATE_MANAGE), 'init', '--project', str(root),
                 '--commit' if mode == 'commit' else '--local']
    if not is_repo(root):
        init_argv.append('--no-own-repo')
    done = subprocess.run(init_argv, capture_output=True, text=True, encoding='utf-8', errors='replace')
    print(done.stdout.strip())
    if done.returncode != 0:
        print(done.stderr.strip(), file=sys.stderr)
        return done.returncode

    if worktrees:
        print('\n工作树还没跟上——进每个链接工作树里跑一次同一条命令，或者让它自己 merge/rebase 主树：')
        for wt in worktrees:
            print(f'  git -C "{wt.split("  [")[0]}" status')
    if not tracked:
        print(f'\n确认没问题之后再删备份：{root / (OLD + ".bak-")}*')
    print('\n提交这次改名（项目跟踪记录时 git mv 已经 staged）：')
    print('  git add -A && git commit -m "refactor: move the record from .nav/ to .pf/"')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))