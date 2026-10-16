"""`dag-manage.py` 的命令面：前缀怎么裁决、spec 落点从哪来、便条删不掉时怎么收场。

CLI 那几条起真进程（一次一条命令，temp 项目）；进程内那几条直接加载模块调 `cmd_*`，
用来盯住「导入不绑项目」和「便条是 best effort」这两件不看输出看不出来的事。

跑法（两种都行）：

    python -m unittest discover -s dag/tests -t .      （cwd 在 tools/）
    python dag/tests/test_cli.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

DAG = Path(__file__).resolve().parent.parent / 'dag-manage.py'
TOOLS = DAG.parent.parent
#: UTC、微秒、字面 `Z`。这里写死，不读 `clock.ISO_FMT`：读它的话，正本退回秒级时断言会跟着
#: 一起退回，那正是这条用例要拦住的漂移。
SHAPE = r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z'


def load_clock():
    """`tools/shared/clock.py`：记录时间戳形状的正本。"""
    spec = importlib.util.spec_from_file_location('pf_shared_clock_case',
                                                  TOOLS / 'shared' / 'clock.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


clock = load_clock()

#: 前缀能唯一对上 / 对不上的两枚 id，和一枚与前两者都不撞的：裁决在一个前缀上就看得出。
A = 'aaaaaaaa-1111-4111-8111-111111111111'
B = 'aaaaaaaa-2222-4222-8222-222222222222'
OTHER = 'bbbbbbbb-3333-4333-8333-333333333333'


def child_env() -> dict:
    """每个子进程都按 UTF-8 出去；顺手把宿主的 session 变量摘掉，让「有没有便条」由用例说了算。"""
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    env.pop('DSH_SESSION_ID', None)
    return env


def run(project: Path, *argv):
    proc = subprocess.run([sys.executable, '-X', 'utf8', str(DAG), *[str(a) for a in argv]],
                          cwd=str(project), capture_output=True, text=True,
                          encoding='utf-8', errors='replace', env=child_env())
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def load_module():
    """`dag-manage.py` 的一份新拷贝，按路径加载 —— 每个用例一份，绑定互不串味。"""
    spec = importlib.util.spec_from_file_location('dag_manage_case', DAG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ProjectCase(unittest.TestCase):
    """一个 init 过的空项目：记录目录摆好，图可以直接写、也可以走真命令长出来。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='dag-cli-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        ledger = self.root / '.pf'
        ledger.mkdir(parents=True, exist_ok=True)
        (ledger / '.gitignore').write_text('meta/\n', encoding='utf-8')
        (ledger / '.gitattributes').write_text('** -text\n', encoding='utf-8')

    def write_graph(self, nodes: dict):
        (self.root / '.pf' / 'ideas.json').write_text(
            json.dumps({'nodes': nodes}, ensure_ascii=False), encoding='utf-8')

    def graph(self) -> dict:
        return json.loads((self.root / '.pf' / 'ideas.json').read_text(encoding='utf-8'))

    def add(self, title: str) -> str:
        code, out, err = run(self.root, 'add', title)
        self.assertEqual(0, code, err or out)
        return out.split(':', 1)[1].split()[0]


class TestTheHelpVerb(ProjectCase):
    """`help` 与 `--help` 是同一份正文：动词表就是这个 parser 里的那一份。"""

    def test_help_prints_the_parser_help(self):
        verb, flag = run(self.root, 'help'), run(self.root, '--help')

        self.assertEqual(0, verb[0], verb[2])
        self.assertEqual(0, flag[0], flag[2])
        self.assertEqual(flag[1], verb[1])
        self.assertIn('validate', verb[1], '动词表没印出来')

    def test_the_verb_lists_itself(self):
        self.assertIn('help', run(self.root, 'help')[1])


class TestThePrefixVerdict(ProjectCase):
    """同一个前缀在每个动词上走到同一个裁决：对得上就照那一格办，对不上就停。"""

    def setUp(self):
        super().setUp()
        self.write_graph({
            A: {'title': '一格', 'status': 'todo', 'dependencies': []},
            B: {'title': '另一格', 'status': 'todo', 'dependencies': []},
            OTHER: {'title': '第三格', 'status': 'todo', 'dependencies': []},
        })

    def test_an_ambiguous_prefix_stops_every_verb(self):
        for argv in (['rm', 'aaaaaaaa'],
                     ['undepend', 'aaaaaaaa', OTHER],
                     ['depend', 'aaaaaaaa', '--on', OTHER],
                     ['depend', OTHER, '--on', 'aaaaaaaa'],
                     ['status', 'aaaaaaaa', 'completed'],
                     ['update', 'aaaaaaaa', '--title', 'x'],
                     ['revive', 'aaaaaaaa'],
                     ['info', 'aaaaaaaa'],
                     ['context', 'aaaaaaaa'],
                     ['open', 'aaaaaaaa']):
            with self.subTest(argv=argv):
                code, out, err = run(self.root, *argv)
                self.assertEqual(1, code, out or err)
                self.assertIn('matches multiple nodes', err)

    def test_a_unique_prefix_reaches_the_node_and_moves_it(self):
        code, out, err = run(self.root, 'rm', 'bbbbbbbb')
        self.assertEqual(0, code, err or out)
        self.assertIn(f'Removed {OTHER}', out, '报的该是记录里那一格的 id')
        self.assertNotIn(OTHER, self.graph()['nodes'])

    def test_a_unique_prefix_records_the_full_id_on_the_edge(self):
        code, out, err = run(self.root, 'depend', 'bbbbbbbb', '--on', 'aaaaaaaa-1111')
        self.assertEqual(0, code, err or out)
        self.assertEqual([A], self.graph()['nodes'][OTHER]['dependencies'])

    def test_a_unique_prefix_takes_the_edge_back_out(self):
        code, out, err = run(self.root, 'depend', OTHER, '--on', A)
        self.assertEqual(0, code, err or out)

        code, out, err = run(self.root, 'undepend', 'bbbbbbbb', 'aaaaaaaa-1111')

        self.assertEqual(0, code, err or out)
        self.assertEqual([], self.graph()['nodes'][OTHER]['dependencies'])

    def test_a_dangling_edge_comes_out_by_its_stored_id_or_a_prefix_of_it(self):
        """图上没有的那个节点仍然能在边上点着名：修悬空引用是这条命令的活。"""
        self.write_graph({
            OTHER: {'title': '第三格', 'status': 'todo',
                    'dependencies': ['dddddddd-4444-4444-8444-444444444444']},
        })

        code, out, err = run(self.root, 'undepend', 'bbbbbbbb', 'dddddddd')

        self.assertEqual(0, code, err or out)
        self.assertEqual([], self.graph()['nodes'][OTHER]['dependencies'])

    def test_removing_a_node_takes_its_id_out_of_every_other_node(self):
        code, out, err = run(self.root, 'depend', OTHER, '--on', A)
        self.assertEqual(0, code, err or out)

        code, out, err = run(self.root, 'rm', 'aaaaaaaa-1111', '--force')

        self.assertEqual(0, code, err or out)
        self.assertEqual([], self.graph()['nodes'][OTHER]['dependencies'],
                         '被删掉的 id 还挂在别人的依赖里')

    def test_a_prefix_that_matches_nobody_is_not_found(self):
        code, out, err = run(self.root, 'info', 'cccccccc')
        self.assertEqual(1, code)
        self.assertIn('not found', err)


class TestTheDependencyVerdict(ProjectCase):
    """要摘的那条边也走三档裁决：唯一对得上就摘，多义列出候选，认不出来就说认不出来。"""

    #: 两条写在边上、图上却没有的 id，共用一个前缀 —— 边上那一侧的「多义」只可能从这儿来。
    D1 = 'dddddddd-1111-4111-8111-111111111111'
    D2 = 'dddddddd-2222-4222-8222-222222222222'

    def setUp(self):
        super().setUp()
        self.write_graph({
            A: {'title': '一格', 'status': 'todo', 'dependencies': []},
            OTHER: {'title': '第三格', 'status': 'todo', 'dependencies': [self.D1, self.D2]},
        })

    def deps(self) -> list:
        return self.graph()['nodes'][OTHER]['dependencies']

    def test_an_ambiguous_dependency_prefix_is_refused_and_the_edges_stay(self):
        code, out, err = run(self.root, 'undepend', OTHER, 'dddddddd')

        self.assertEqual(1, code, out or err)
        self.assertIn('matches multiple dependencies', err)
        self.assertIn(self.D1, err, '候选要列出来')
        self.assertEqual([self.D1, self.D2], self.deps(), '多义时一条边都不该被摘掉')

    def test_a_dependency_that_names_nobody_is_refused(self):
        code, out, err = run(self.root, 'undepend', OTHER, 'ffffffff')

        self.assertEqual(1, code, out or err)
        self.assertIn('not found', err)
        self.assertEqual([self.D1, self.D2], self.deps())

    def test_a_dependency_that_is_absent_while_the_node_is_here_is_a_no_op(self):
        """管线插维护节点时会摘一条常常不存在的边（`maintenance.insertion` 第 3 步），
        那一步要的正是「没有这条边」的安静收场。"""
        code, out, err = run(self.root, 'undepend', OTHER, A)

        self.assertEqual(0, code, err or out)
        self.assertEqual([self.D1, self.D2], self.deps())


class TestTheSpecLanding(ProjectCase):
    """`add` 报的与 `open` 印的是同一个落点，而这个落点由管线那一处给出。"""

    def pipeline_nodes(self, mod):
        """管线那边算落点的那个模块 —— dag 侧问的就是这一份（同一个名字、同一次按路径取）。"""
        pkg = mod._loader().load_by_path('pf_dag_conduct_impl',
                                         mod.CONDUCT_IMPL / '__init__.py',
                                         package_dir=mod.CONDUCT_IMPL, register=True, reuse=True)
        return pkg.nodes

    def test_add_and_open_print_one_path(self):
        code, out, err = run(self.root, 'add', '一格')
        self.assertEqual(0, code, err or out)
        uid = out.split(':', 1)[1].split()[0]
        reported = out.splitlines()[1].split(': ', 1)[1]

        code, opened, err = run(self.root, 'open', uid)

        self.assertEqual(0, code, err or opened)
        self.assertEqual(reported, opened)
        self.assertTrue(opened.replace('\\', '/').endswith(f'.pf/nodes/{uid}/spec.md'), opened)

    def test_both_verbs_take_the_path_from_the_pipeline(self):
        """把管线那一处换成哨兵：`add` 与 `open` 印出来的都得是哨兵。

        dag 侧若自己拼一次 `nodes/<uid>/spec.md`（不落点是哪儿），哨兵就印不出来 —— 这条用例是
        那条回落的红线，抓的是「落点从哪来」这件事本身，与路径长什么样无关。
        """
        nodes = self.pipeline_nodes(load_module())
        original = nodes.node_spec_path
        self.addCleanup(setattr, nodes, 'node_spec_path', original)
        sentinel = 'SENTINEL/where-the-pipeline-says/spec.md'
        nodes.node_spec_path = lambda project, uid: sentinel

        mod = load_module()
        mod.configure(self.root)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {'DSH_SESSION_ID': ''}):
            with contextlib.redirect_stdout(out):
                mod.cmd_add(mod.argparse.Namespace(
                    title='一格', description=[], desc=None, depends_on=None,
                    mode=None, weight=None, tags=None))
        printed = out.getvalue()
        self.assertIn(sentinel, printed, f'`add` 没问管线的落点：{printed!r}')
        uid = printed.split(':', 1)[1].split()[0]

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mod.cmd_open(mod.argparse.Namespace(uuid=uid))

        self.assertEqual(sentinel, out.getvalue().strip(), '`open` 没问管线的落点')


class TestTheNodeTimestamp(ProjectCase):
    """新建节点写下的时刻：UTC、微秒、字面 `Z` —— 正本 `tools/shared/clock.py` 那一份。"""

    def test_a_new_node_carries_the_tree_timestamp_shape(self):
        uid = self.add('一格')

        code, out, err = run(self.root, 'info', uid, '--verbose')

        self.assertEqual(0, code, err or out)
        row = json.loads(out)
        for key in ('created_at', 'updated_at'):
            with self.subTest(key=key):
                value = row[key]
                self.assertRegex(value, SHAPE, f'{key} 的时间戳形状不对：{value!r}')
                self.assertIsNotNone(datetime.strptime(value, clock.ISO_FMT),
                                     f'正本 {clock.ISO_FMT!r} 认不出 {value!r}')


class TestTheModuleBindsNothingAtImport(ProjectCase):
    """路径由调用方说一句：导入这份文件不绑任何一个项目。"""

    def test_a_fresh_module_points_at_no_project(self):
        mod = load_module()

        self.assertIsNone(mod.LEDGER_DIR)
        self.assertIsNone(mod.IDEAS_JSON)

    def test_a_command_without_a_project_says_so(self):
        mod = load_module()
        err = io.StringIO()

        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            mod.cmd_list(mod.argparse.Namespace(status=None, ready=False, verbose=False,
                                                limit=0, tags=None))

        self.assertEqual(1, caught.exception.code)
        self.assertIn('configure', err.getvalue())

    def test_configure_reads_the_project_the_caller_named(self):
        """cwd 落在别处：名字点给谁，就读谁 —— 这个用例进程的 cwd 是 tools/，两个 temp 项目都不在那儿。"""
        other = Path(tempfile.mkdtemp(prefix='dag-cli-other-'))
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        (other / '.pf').mkdir(parents=True, exist_ok=True)
        (other / '.pf' / 'ideas.json').write_text(
            json.dumps({'nodes': {A: {'title': '别处的', 'status': 'todo',
                                      'dependencies': []}}}), encoding='utf-8')
        self.write_graph({B: {'title': '这边的', 'status': 'todo', 'dependencies': []}})
        mod = load_module()

        mod.configure(other)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mod.cmd_list(mod.argparse.Namespace(status=None, ready=False, verbose=False,
                                                limit=0, tags=None))

        listed = json.loads(out.getvalue())
        self.assertEqual([A], [e['id'] for e in listed])


class TestTheSessionNoteIsBestEffort(ProjectCase):
    """便条是管道物：写不成、删不成，都只警告一行，图上那一笔照旧落地。"""

    def boom(self):
        """一份加载就炸的 session-note：模块里那两个动作都从这里过。"""
        class Broken:
            def drop(self, project, node_id):
                raise OSError('便条加载不了')

            def append(self, project, node_id, role, session):
                raise OSError('便条加载不了')

        return Broken()

    def test_a_note_that_cannot_be_removed_does_not_fail_rm(self):
        self.write_graph({A: {'title': '一格', 'status': 'todo', 'dependencies': []}})
        mod = load_module()
        mod.configure(self.root)
        mod._session_note = self.boom
        err = io.StringIO()

        with contextlib.redirect_stderr(err):
            mod.cmd_rm(mod.argparse.Namespace(uuid=A[:8], force=False))

        self.assertNotIn(A, self.graph()['nodes'], '节点该已经从图上删掉')
        self.assertIn('Warning', err.getvalue())

    def test_a_note_that_cannot_be_written_does_not_fail_add(self):
        mod = load_module()
        mod.configure(self.root)
        mod._session_note = self.boom
        args = mod.argparse.Namespace(title='一格', description=[], desc=None, depends_on=None,
                                      mode=None, weight=None, tags=None)
        err = io.StringIO()

        with mock.patch.dict(os.environ, {'DSH_SESSION_ID': 'session-x'}):
            with contextlib.redirect_stderr(err):
                mod.cmd_add(args)

        self.assertEqual(1, len(self.graph()['nodes']), '节点该已经落图')
        self.assertIn('Warning', err.getvalue())


if __name__ == '__main__':
    unittest.main(verbosity=2)