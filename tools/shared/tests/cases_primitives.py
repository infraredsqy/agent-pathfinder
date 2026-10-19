"""The two primitives under `tools/shared/`: the bytes and the reading of one JSON record, and the
shape of a moment inside a record."""

from __future__ import annotations

import json
import os
import time
import unittest
from datetime import datetime, timezone

from shared_env import SharedCase, _shared

__all__ = ['TestTheClockShapes', 'TestTheReaderAndWriter']


class TestTheReaderAndWriter(SharedCase):
    """`tools/shared/jsonio.py`: the read's three outcomes, and the bytes one write lands."""

    def test_a_record_that_is_not_there_reads_as_none(self):
        """A missing file gives `None`: the caller takes it as its own default, and only a corrupt
        read is a separate story.

        What this holds off: treating "not written yet" as "corrupt". That would make a tool on its
        first run go red over a perfectly normal absent record, while the genuinely corrupt ones
        drown in that false alarm.
        """
        jsonio = _shared('pf_shared_jsonio', 'jsonio.py')

        self.assertIsNone(jsonio.read_json_strict(self.root / 'nothing.json', 'nothing.json'))

    def test_the_two_read_failures_each_keep_their_own_wording(self):
        """Not-UTF-8 and bad JSON each give the source's own sentence (`JsonReadError`, recognizing
        `not valid UTF-8` and `parse failure`).

        What this holds off: folding the two failures into one sentence. Once folded, reading a
        record with the wrong encoding and reading one with broken syntax look exactly alike, and a
        human can no longer tell whether to fix the file or fix the tool.
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
        """`json_bytes`'s shape is byte for byte what `atomic_write_json` lands: indent two spaces,
        non-ASCII text passed through as it is, the `sort_keys` switch, the platform line separator,
        no trailing newline.

        What this holds off: the merge driver's one dependency. It reads these bytes to decide
        whether a write would change the file, so one byte too many or too few makes every merge
        look like a change and every node's close out writes the file once more; escaping the
        non-ASCII text would stop the record being the copy a human can read directly.
        """
        jsonio = _shared('pf_shared_jsonio', 'jsonio.py')
        record = {'title': 'Café billing page', 'z': 1, 'a': [1, 2]}
        separator = os.linesep.encode('utf-8')

        payload = jsonio.json_bytes(record, sort_keys=True)

        self.assertEqual(record, json.loads(payload.decode('utf-8')))
        self.assertIn('Café'.encode('utf-8'), payload, 'the non-ASCII text was escaped')
        self.assertTrue(payload.split(separator)[1].startswith(b'  "'),
                        'the indent is not two spaces')
        self.assertNotIn(b'\n', payload.replace(separator, b''),
                         'the newline is not the platform one')
        self.assertFalse(payload.endswith(b'\n'), 'the record has one newline too many at the end')
        self.assertLess(payload.index(b'"a"'), payload.index(b'"z"'),
                        'sort_keys did not take effect')

        target = self.root / 'deep' / 'record.json'
        jsonio.atomic_write_json(target, record, sort_keys=True)
        self.assertEqual(payload, target.read_bytes(),
                         'sorted branch: the bytes on disk are not the ones json_bytes computed')

        jsonio.atomic_write_json(target, record)
        self.assertEqual(jsonio.json_bytes(record), target.read_bytes(),
                         'unsorted branch: the bytes on disk are not the ones json_bytes computed')

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
    """`tools/shared/clock.py`: the two moment shapes, each parsed back by its own reader."""

    def test_each_now_parses_back_with_its_own_format(self):
        """Each "now" parses back with its own `ISO_FMT` / `ISO_FMT_SECONDS` through `strptime`, and
        the two shapes differ from each other (the microsecond one carries a fraction, the seconds
        one does not).

        What this holds off: the shape and the reader being changed apart. Give the seconds one a
        fraction too (or push the microsecond one back to seconds) and writing looks perfectly
        normal, while values already on disk stop parsing -- the drift shows only on the reading
        side, and only some time later.
        """
        clock = _shared('pf_shared_clock', 'clock.py')

        micro = clock.now()
        seconds = clock.now_seconds()

        self.assertIsNotNone(datetime.strptime(micro, clock.ISO_FMT))
        self.assertIsNotNone(datetime.strptime(seconds, clock.ISO_FMT_SECONDS))
        self.assertIn('.', micro, 'the microsecond shape carries no fraction')
        self.assertNotIn('.', seconds, 'the seconds shape carries a fraction')
        with self.assertRaises(ValueError,
                               msg='the seconds shape was taken by the microsecond reader: '
                                   'the two shapes have blended into one'):
            datetime.strptime(seconds, clock.ISO_FMT)

    def test_the_moment_is_utc_in_both_shapes(self):
        """Both "now"s land on UTC, and inside the short stretch around the call.

        What this holds off: falling back to local time. Once one of a record's two moments is
        written in the local zone and the other in UTC, comparing them and taking the interval still
        run, only hours off -- the order in which parallel claims were made silently comes out
        reversed.
        """
        clock = _shared('pf_shared_clock', 'clock.py')

        for shape, read in ((clock.ISO_FMT, clock.now),
                            (clock.ISO_FMT_SECONDS, clock.now_seconds)):
            with self.subTest(shape=shape):
                before = datetime.now(timezone.utc)
                parsed = datetime.strptime(read(), shape).replace(tzinfo=timezone.utc)
                after = datetime.now(timezone.utc)
                # The seconds shape drops the fraction, so the window's floor is the whole second.
                floor = before.replace(microsecond=0)

                self.assertLessEqual(floor, parsed, 'does this moment fall before the call?')
                self.assertLessEqual(
                    parsed, after, 'this moment is not on UTC: it falls outside the call window')