"""`tools/shared/` 的两件原语：一条 JSON 记录的字节与读法，以及记录里的时刻形状。"""

from __future__ import annotations

import json
import os
import time
import unittest
from datetime import datetime, timezone

from shared_env import SharedCase, _shared

__all__ = ['TestTheClockShapes', 'TestTheReaderAndWriter']


class TestTheReaderAndWriter(SharedCase):
    """`tools/shared/jsonio.py`：读的三个结果，和一次写落下的那些字节。"""

    def test_a_record_that_is_not_there_reads_as_none(self):
        """缺文件时给 `None`：调用方拿它当自己的缺省，读坏了才另说。

        挡的是把「还没写过」当成「读坏了」：那会让第一次运行的工具在一条正常的空记录上判红，
        而真正的坏记录反而淹在这条假故障里。
        """
        jsonio = _shared('pf_shared_jsonio', 'jsonio.py')

        self.assertIsNone(jsonio.read_json_strict(self.root / 'nothing.json', 'nothing.json'))

    def test_the_two_read_failures_each_keep_their_own_wording(self):
        """非 UTF-8 与坏 JSON 各给正本那句话（`JsonReadError`，认 `not valid UTF-8` 与 `parse
        failure`）。

        挡的是把两种失败合成一句：合成之后，读到一份编码不对的记录与读到一份语法坏掉的记录长得
        一模一样，人再也分不出该去改文件还是改工具。
        """
        jsonio = _shared('pf_shared_jsonio', 'jsonio.py')
        not_utf8_path = self.root / 'bytes.json'
        not_utf8_path.write_bytes(b'\xff\xfe not utf-8 at all')
        not_json_path = self.root / 'text.json'
        not_json_path.write_text('{not json', encoding='utf-8')

        with self.assertRaises(jsonio.JsonReadError) as not_utf8:
            jsonio.read_json_strict(not_utf8_path, 'bytes.json')
        with self.assertRaises(jsonio.JsonReadError) as not_json:
            jsonio.read_json_strict(not_json_path, 'text.json')

        self.assertIn('bytes.json is not valid UTF-8', str(not_utf8.exception))
        self.assertIn('text.json parse failure', str(not_json.exception))

    def test_one_records_bytes_are_the_bytes_that_land_on_disk(self):
        """`json_bytes` 的形状与 `atomic_write_json` 落盘的字节逐字相同：缩进两格、中文原样、
        `sort_keys` 开关、平台行分隔、末尾没有换行。

        挡的是合并驱动那条依赖：它拿这几个字节判断「这一笔写会不会改动文件」，算出来的多一个字节
        或少一个字节，每次合并就都像改过，于是每个节点收口都要多写一次文件；中文被转义则让记录
        不再是人能直接读的那一份。
        """
        jsonio = _shared('pf_shared_jsonio', 'jsonio.py')
        record = {'标题': '计费页', 'z': 1, 'a': [1, 2]}
        separator = os.linesep.encode('utf-8')

        payload = jsonio.json_bytes(record, sort_keys=True)

        self.assertEqual(record, json.loads(payload.decode('utf-8')))
        self.assertIn('计费页'.encode('utf-8'), payload, '中文被转义了')
        self.assertTrue(payload.split(separator)[1].startswith(b'  "'), '缩进不是两格')
        self.assertNotIn(b'\n', payload.replace(separator, b''), '换行不是平台那一种')
        self.assertFalse(payload.endswith(b'\n'), '记录末尾多了一个换行')
        self.assertLess(payload.index(b'"a"'), payload.index(b'"z"'), 'sort_keys 没有生效')

        target = self.root / 'deep' / 'record.json'
        jsonio.atomic_write_json(target, record, sort_keys=True)
        self.assertEqual(payload, target.read_bytes(),
                         '排过序那一支：落盘的字节与 json_bytes 算出来的不是同一份')

        jsonio.atomic_write_json(target, record)
        self.assertEqual(jsonio.json_bytes(record), target.read_bytes(),
                         '不排序那一支：落盘的字节与 json_bytes 算出来的不是同一份')

    def test_a_write_a_reader_never_lets_go_fails_bounded_and_leaves_no_temp(self):
        sb = self.make()
        jsonio = _shared('pf_shared_jsonio', 'jsonio.py')
        target = sb.root / '.pf' / 'meta' / 'held.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"stale": true}', encoding='utf-8')
        held = open(target, 'r', encoding='utf-8')
        self.addCleanup(held.close)

        started = time.monotonic()
        with self.assertRaises(PermissionError):
            jsonio.atomic_write_json(target, {'fresh': True})
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 3.0, 'the retry is bounded, it does not hang')
        self.assertEqual([], sorted(p.name for p in target.parent.iterdir() if p.suffix == '.tmp'),
                         'a failed write leaves its temp file behind')
        self.assertEqual({'stale': True}, json.loads(target.read_text(encoding='utf-8')))


class TestTheClockShapes(SharedCase):
    """`tools/shared/clock.py`：两种时刻形状，各自能被自己的读者解析回来。"""

    def test_each_now_parses_back_with_its_own_format(self):
        """两种「现在」各能被自己的 `ISO_FMT` / `ISO_FMT_SECONDS` 用 `strptime` 解析回来，且两种
        形状互不相同（微秒那条带小数、秒级那条没有）。

        挡的是形状与读者分开改：把秒级那条也带上微秒（或把微秒那条退回秒级），写的时候一切正常，
        而盘上已有的值从此解析不了 —— 漂移只在读的那一头、隔一段时间才现形。
        """
        clock = _shared('pf_shared_clock', 'clock.py')

        micro = clock.now()
        seconds = clock.now_seconds()

        self.assertIsNotNone(datetime.strptime(micro, clock.ISO_FMT))
        self.assertIsNotNone(datetime.strptime(seconds, clock.ISO_FMT_SECONDS))
        self.assertIn('.', micro, '微秒那条形状里没有小数')
        self.assertNotIn('.', seconds, '秒级那条形状里带了小数')
        with self.assertRaises(ValueError, msg='秒级那条被微秒读者收下了：两种形状混成了一种'):
            datetime.strptime(seconds, clock.ISO_FMT)

    def test_the_moment_is_utc_in_both_shapes(self):
        """两种「现在」都落在 UTC 上，且就在调用前后那一小段里。

        挡的是退回本机时间：记录里两枚时刻一旦一枚按本机时区写、一枚按 UTC 写，比大小、算间隔都
        还跑得通，只是差了几个小时 —— 并行认领的先后顺序会静默地反掉。
        """
        clock = _shared('pf_shared_clock', 'clock.py')

        for shape, read in ((clock.ISO_FMT, clock.now),
                            (clock.ISO_FMT_SECONDS, clock.now_seconds)):
            with self.subTest(shape=shape):
                before = datetime.now(timezone.utc)
                parsed = datetime.strptime(read(), shape).replace(tzinfo=timezone.utc)
                after = datetime.now(timezone.utc)
                # 秒级那条把小数截掉，窗口的下沿按整秒算。
                floor = before.replace(microsecond=0)

                self.assertLessEqual(floor, parsed, '这个时刻落在调用之前？')
                self.assertLessEqual(parsed, after, '这个时刻不在 UTC 上：它落在调用窗口之外')