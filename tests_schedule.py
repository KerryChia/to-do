"""Offline schedule coverage using only the bundled synthetic calendar."""
from datetime import datetime
from pathlib import Path
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch

import course_schedule as calendar

FIXTURE = Path(__file__).parent / 'fixtures' / 'sample-schedule.ics'


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.schedule = calendar.Schedule(self.temp.name)
        self.content = FIXTURE.read_text(encoding='utf-8')

    def load(self):
        return self.schedule.import_ics(self.content)

    def test_empty_defaults_have_no_bundled_courses_or_resources(self):
        self.assertEqual(calendar.COURSES, {})
        self.assertEqual(calendar.RESOURCES, {})
        self.assertEqual(self.schedule.snapshot(), dict(events=[], source='', sha256='',
                                                       timezone='Asia/Shanghai', resources={}, courses=[]))
        self.assertFalse(self.schedule.path.exists())

    def test_import_persistence_hash_idempotency_and_generic_courses(self):
        self.assertEqual(self.load(), dict(ok=True, count=8, unchanged=False))
        self.assertEqual(self.load(), dict(ok=True, count=8, unchanged=True))
        snapshot = self.schedule.snapshot()
        self.assertEqual(snapshot['courses'], ['Sample Computing', 'Sample Seminar'])
        self.assertEqual(snapshot['resources'], {})
        self.assertEqual(snapshot['sha256'], hashlib.sha256(self.content.encode()).hexdigest())
        self.assertEqual(calendar.Schedule(self.temp.name).snapshot(), snapshot)
        self.assertEqual(len({e['id'] for e in snapshot['events']}), 8)
        self.assertFalse(self.schedule.path.with_suffix('.tmp').exists())

    def test_ics_timezone_folding_escaping_alarm_and_deduplication(self):
        events = calendar.parse_ics(self.content)
        self.assertEqual(events[1]['title'], 'Sample Seminar')
        self.assertEqual(events[1]['start'], '2026-09-08T16:00')
        self.assertEqual(events[1]['end'], '2026-09-08T17:30')
        self.assertEqual(events[1]['location'], 'Sample Hall, Room B; Level 1')
        event = self.content.split('BEGIN:VEVENT', 1)[1].split('END:VEVENT', 1)[0]
        duplicated = self.content.replace('END:VCALENDAR', 'BEGIN:VEVENT' + event + 'END:VEVENT\nEND:VCALENDAR')
        self.assertEqual(calendar.parse_ics(duplicated), events)
        self.assertEqual(events, sorted(events, key=lambda e: (e['start'], e['id'])))

    def test_optional_generic_mapping_still_works(self):
        with patch.dict(calendar.COURSES, {'Sample Computing': 'Sample Course'}, clear=True):
            events = calendar.parse_ics(self.content)
        self.assertEqual(events[0]['category'], 'Sample Course')
        self.assertEqual(events[0]['title'], 'Sample Computing')

    def test_invalid_import_preserves_saved_calendar(self):
        self.load()
        before = self.schedule.path.read_bytes()
        cases = [('', 'invalid_calendar_count'), ('x' * 2_000_001, 'invalid_calendar_size'),
                 (None, 'invalid_calendar_size'),
                 (self.content.replace('Asia/Shanghai', 'Europe/London'), 'unsupported_calendar_timezone'),
                 (self.content.replace('SUMMARY:Sample Computing', 'RRULE:FREQ=WEEKLY\nSUMMARY:Sample Computing', 1), 'expand_recurrence_before_import'),
                 (self.content.replace('20260907T103000', '20260907T080000'), 'invalid_calendar_event')]
        for content, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                self.schedule.import_ics(content)
            self.assertEqual(self.schedule.path.read_bytes(), before)

    def test_inference_explicit_dates_relative_weeks_next_class_and_lab(self):
        self.load()
        for phrase, source, due in [
            ('下周交', '2026-09-08T16:00', '2026-09-15T16:00'),
            ('September 15', '2026-09-08T16:00', '2026-09-15T16:00'),
            ('9月15日', '2026-09-08T16:00', '2026-09-15T16:00'),
            ('2026/9/15', '2026-09-08T08:00:00+00:00', '2026-09-15T16:00')]:
            with self.subTest(phrase=phrase):
                result = self.schedule.resolve('Sample Seminar', phrase, source)
                self.assertEqual(result['due'], due)
                self.assertTrue(result['inferred'])
                self.assertIn('资料日期', result['basis'])
                self.assertTrue(result['eventId'].startswith('class-'))
        for phrase, due in [('下次课', '2026-09-09T09:00'),
                            ('next class Lab', '2026-09-09T18:00'),
                            ('下周', '2026-09-16T09:00'),
                            ('下周上机', '2026-09-16T18:00'),
                            ('第2周', '2026-09-14T09:00'),
                            ('第二周', '2026-09-14T09:00')]:
            with self.subTest(phrase=phrase):
                self.assertEqual(self.schedule.resolve('Sample Computing', phrase, '2026-09-09T08:00')['due'], due)
        result = self.schedule.resolve('Sample Computing', 'next week', now=datetime(2026, 9, 9, 8, tzinfo=calendar.CHINA))
        self.assertEqual(result['due'], '2026-09-14T09:00')
        self.assertIn('未提供资料日期', result['basis'])

    def test_inference_does_not_guess_missing_courses_dates_or_year(self):
        self.load()
        for category, phrase in [('Unknown Course', '下周'), ('Sample Seminar', '下下周'),
                                 ('Sample Seminar', '9月30日'), ('Sample Seminar', '稍后通知'),
                                 ('Sample Seminar', '第0周')]:
            result = self.schedule.resolve(category, phrase, '2026-09-08T16:00')
            self.assertEqual(result['due'], '')
            self.assertFalse(result['inferred'])
        data = json.loads(self.schedule.path.read_text(encoding='utf-8'))
        data['events'].append({**data['events'][1], 'id': 'class-next-year', 'start': '2027-09-15T16:00', 'end': '2027-09-15T17:30'})
        self.schedule.path.write_text(json.dumps(data), encoding='utf-8')
        self.assertFalse(self.schedule.resolve('Sample Seminar', '9月15日')['inferred'])
        self.assertEqual(self.schedule.resolve('Sample Seminar', '2027年9月15日')['due'], '2027-09-15T16:00')

    def test_task_inference_preserves_explicit_completed_and_suggested_tasks(self):
        self.load()
        task = dict(title='Sample assignment', category='Sample Seminar', due='', completed=False,
                    kind='assignment', sourceTiming='下周', notes='Fixture evidence', firedKey='old')
        inferred = self.schedule.infer_task(task, '2026-09-08T16:00')
        self.assertEqual(inferred['due'], '2026-09-15T16:00')
        self.assertEqual(inferred['firedKey'], '')
        self.assertIn('[课表推算]', inferred['notes'])
        self.assertEqual(task['due'], '')
        for changes in [dict(due='2026-09-10T12:00'), dict(completed=True), dict(kind='suggestion'), dict(category='Unknown Course')]:
            original = {**task, **changes}
            self.assertEqual(self.schedule.infer_task(original, '2026-09-08T16:00'), original)


if __name__ == '__main__':
    unittest.main(verbosity=2)
