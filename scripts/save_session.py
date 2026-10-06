#!/usr/bin/env python3
"""Verify quotations and save a new Obsidian reading note with a separate AI cursor."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo
from epub_chapters import last_sentence

MARKER = '<!-- VERIFIED_QUOTES -->'


def safe_name(value, max_bytes=96):
    value = re.sub(r'[\x00-\x1f/\\:*?"<>|\[\]#^]', '-', str(value)).strip(' .')
    value = value.encode('utf-8')[:max_bytes].decode('utf-8', errors='ignore').rstrip(' .')
    if not value or value in ('.', '..'):
        raise ValueError('Empty or invalid book title')
    return value


def output_folder(config):
    if not config.get('vault_path'):
        raise ValueError('Set vault_path in config.local.json before saving to Obsidian')
    vault = Path(config['vault_path']).expanduser().resolve()
    if not vault.is_dir():
        raise ValueError('Configured Obsidian vault does not exist')
    subfolder = Path(config['output_subfolder'])
    if subfolder.is_absolute() or '..' in subfolder.parts:
        raise ValueError('Output must be a folder inside the configured vault')
    folder = (vault / subfolder).resolve()
    if folder != vault and vault not in folder.parents:
        raise ValueError('Output folder symlink escapes the configured vault')
    return folder


def atomic_json(path, data):
    fd, tmp = tempfile.mkstemp(prefix='.reading-tmp-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def create_note(path, content):
    fd, tmp = tempfile.mkstemp(prefix='.note-tmp-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        # A complete file becomes visible in one exclusive operation; never overwrite.
        os.link(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def verified_quotes(reading, quotes):
    lines = ['## 精句摘录', '']
    if not isinstance(quotes, list):
        raise ValueError('quotes.json must contain an array')
    if not quotes:
        return '\n'.join(lines + ['本次未选取精句。'])
    for q in quotes:
        text, position = q['text'], q['start_offset']
        if not isinstance(text, str) or not text.strip() or not isinstance(position, int):
            raise ValueError('A quote must have nonempty text and an integer absolute start_offset')
        matches = [s for s in reading['segments'] if s['member'] == q['member']
                   and s['start_offset'] <= position
                   and position + len(text) <= s['end_offset']]
        if len(matches) != 1:
            raise ValueError('Quote location is outside the read range or ambiguous')
        segment = matches[0]
        relative = position - segment['start_offset']
        if segment['text'][relative:relative + len(text)] != text:
            raise ValueError('Quote text does not match the source at the recorded position')
        lines.extend(['> ' + line for line in text.splitlines()])
        lines += ['', '**出处：** ' + segment.get('title', q['member']) +
                  '；`' + q['member'] + '`；正文偏移 ' + str(position) + '。']
        if q.get('reason'):
            lines += ['**选择理由（AI）：** ' + str(q['reason'])]
        lines += ['']
    return '\n'.join(lines).rstrip()


def validate_reading(reading):
    required = ['asset_id', 'title', 'source_sha256', 'source_quality', 'format', 'planned_minutes',
                'estimated_minutes', 'start_locator', 'end_locator', 'segments',
                'reading_range', 'starting_chapter_title']
    if any(k not in reading for k in required):
        raise ValueError('Incomplete reading session')
    asset_id = reading['asset_id']
    if not isinstance(asset_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', asset_id):
        raise ValueError('asset_id is not safe for a state filename')
    if not re.fullmatch(r'[0-9a-f]{64}', reading['source_sha256']):
        raise ValueError('source_sha256 must be a SHA-256 digest')
    if reading['format'] not in ('epub', 'pdf', 'ui'):
        raise ValueError('Unsupported reading format')
    if not reading['segments'] or reading['estimated_minutes'] <= 0:
        raise ValueError('No reading content')
    for s in reading['segments']:
        if not isinstance(s['text'], str) or s['start_offset'] < 0 or s['end_offset'] - s['start_offset'] != len(s['text']):
            raise ValueError('Segment text length does not match source offsets')
    if not reading['reading_range'].strip() or not reading['starting_chapter_title'].strip():
        raise ValueError('Verify actual chapter range and starting title before saving')


def save(config, reading, body, quotes):
    validate_reading(reading)
    if body.count(MARKER) != 1 or '{{' in body:
        raise ValueError('Body must be completed, with exactly one VERIFIED_QUOTES marker')
    body = body.replace(MARKER, verified_quotes(reading, quotes))
    identity = {k: reading[k] for k in ('asset_id', 'source_sha256', 'format', 'start_locator', 'end_locator')}
    identity['segment_sha256'] = hashlib.sha256(
        json.dumps(reading['segments'], sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    session_id = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    folder = output_folder(config)
    folder.mkdir(parents=True, exist_ok=True)
    state_dir = folder / '.reading-state'
    if state_dir.is_symlink():
        raise ValueError('State directory must not be a symlink')
    state_dir.mkdir(exist_ok=True)
    book_state = state_dir / (reading['asset_id'] + '.json')
    receipt = state_dir / ('session-' + session_id + '.json')
    for path in (book_state, receipt, state_dir / '.lock'):
        if path.is_symlink():
            raise ValueError('State files must not be symlinks')
    with (state_dir / '.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another reading save is active; retry after it completes')
        if receipt.exists():
            recorded = json.loads(receipt.read_text(encoding='utf-8'))
            actual = Path(recorded['note_path'])
            if actual.resolve().parent != folder or actual.is_symlink() or not actual.is_file():
                raise ValueError('Existing receipt points to a missing or unsafe note; preserve state')
            # Returning an old session must never move the latest AI cursor backwards.
            return {'status': 'already_saved', 'note_path': str(actual), 'session_id': session_id,
                    'obsidian_uri': 'obsidian://open?path=' + quote(str(actual), safe='')}
        current = json.loads(book_state.read_text(encoding='utf-8')) if book_state.exists() else None
        expected = reading.get('resume_from_session_id')
        if (current and current['session_id'] not in (expected, session_id)) or (not current and expected):
            raise ValueError('AI cursor changed or disappeared; reload state before saving')
        zone = ZoneInfo(config.get('timezone', 'Asia/Shanghai'))
        created = reading.get('created_at') or dt.datetime.now(dt.timezone.utc).isoformat()
        timestamp = dt.datetime.fromisoformat(created).astimezone(zone)
        local_time = timestamp.isoformat(timespec='seconds')
        filename = timestamp.strftime('%Y-%m-%d_%H-%M-%S') + '-' + safe_name(reading['title']) + '-' + safe_name(reading['starting_chapter_title']) + '.md'
        note = folder / filename
        if current and current['session_id'] == session_id:
            note = Path(current['note_path'])
            if note.resolve().parent != folder or note.is_symlink():
                raise ValueError('Recovery note points outside the configured folder')
        elif note.exists() or note.is_symlink():
            same_session = (not note.is_symlink() and note.is_file() and
                            ('<!-- ebook-reading-session: ' + session_id + ' -->') in note.read_text(encoding='utf-8'))
            if same_session:
                pass
            else:
                filename = timestamp.strftime('%Y-%m-%d_%H-%M-%S.%f') + '-' + safe_name(reading['title']) + '-' + safe_name(reading['starting_chapter_title']) + '.md'
                note = folder / filename
        uri = 'obsidian://open?path=' + quote(str(note), safe='')
        properties = {'book': reading['title'], 'author': reading.get('author'),
                      'asset_id': reading['asset_id'], 'created': local_time,
                      'session_id': session_id, 'mode': 'ai-equivalent',
                      'scope': 'session', 'scope_mode': reading.get('scope_mode', 'time-equivalent'),
                      'human_reading_confirmed': False,
                      'planned_minutes': reading['planned_minutes'],
                      'estimated_reading_minutes': round(reading['estimated_minutes'], 2),
                      'books_progress_snapshot': reading.get('books_progress_snapshot'),
                      'source_quality': reading['source_quality'], 'source_sha256': reading['source_sha256'],
                      'start_locator': json.dumps(reading['start_locator'], ensure_ascii=False),
                      'end_locator': json.dumps(reading['end_locator'], ensure_ascii=False),
                      'tags': ['读书摘要', 'AI代读']}
        tail, complete = last_sentence(reading['segments'])
        if reading.get('last_sentence') not in (None, tail):
            raise ValueError('Recorded final sentence differs from the actual read text')
        tail_label = '本次最后一句' if complete else '本次末尾原文（句中或非句子处截止）'
        opening = '**本次阅读范围：** ' + reading['reading_range'] + '\n\n**' + tail_label + '：**\n\n'
        opening += '\n'.join('> ' + line for line in tail.splitlines()) + '\n\n'
        next_cursor = reading.get('next_cursor')
        if reading.get('at_book_end'):
            next_text = '已到全书末尾。'
        elif next_cursor:
            next_text = '从 `' + str(next_cursor.get('member', next_cursor.get('page', '已记录位置'))) + '` 的位置 ' + str(next_cursor.get('offset', '见定位记录')) + ' 继续。'
        else:
            next_text = '下次依据上方末尾原文重新核对位置。'
        progress = reading.get('books_progress_snapshot')
        progress_text = ('未知' if progress is None else str(round(progress * 100, 2)) + '%')
        records = '\n\n---\n\n<details>\n<summary>阅读记录与下次接续</summary>\n\n'
        mode_label = {'specified-chapters': '指定章节', 'next-chapters': '接续章节', 'time-equivalent': '按时间估算阅读量'}.get(properties['scope_mode'], properties['scope_mode'])
        records += '- 整理时间：' + local_time + '\n- 模式：' + mode_label
        records += '\n- 估算阅读量：' + str(properties['estimated_reading_minutes']) + ' 分钟\n'
        records += '- Books 进度快照：' + progress_text + '\n- AI 代读不代表本人已读。\n'
        records += '- 下次接续：' + next_text + '\n- 来源：' + reading['source_quality'] + '\n\n'
        records += '### 技术定位记录\n\n```json\n' + json.dumps(properties, ensure_ascii=False, indent=2) + '\n```\n\n</details>\n'
        content = opening + body.strip() + records + '\n<!-- ebook-reading-session: ' + session_id + ' -->\n'
        if note.exists() or note.is_symlink():
            if note.is_symlink() or note.read_text(encoding='utf-8') != content:
                raise ValueError('A note already exists with different contents; do not overwrite')
        else:
            create_note(note, content)
        if note.read_text(encoding='utf-8') != content:
            raise ValueError('Saved note read-back verification failed')
        new_state = {'asset_id': reading['asset_id'], 'title': reading['title'], 'session_id': session_id,
                     'source_sha256': reading['source_sha256'], 'created_at': created,
                     'note_path': str(note), 'books_progress_snapshot': reading.get('books_progress_snapshot'),
                     'start_locator': reading['start_locator'], 'end_locator': reading['end_locator'],
                     'reading_range': reading['reading_range'], 'starting_chapter_title': reading['starting_chapter_title'],
                     'last_sentence': tail,
                     'next_cursor': reading.get('next_cursor'), 'at_book_end': reading.get('at_book_end', False)}
        # Receipt follows cursor update. A retry after a crash here can recover the same note.
        atomic_json(book_state, new_state)
        atomic_json(receipt, {'session_id': session_id, 'note_path': str(note),
                              'content_sha256': hashlib.sha256(content.encode('utf-8')).hexdigest()})
        return {'status': 'saved', 'note_path': str(note), 'session_id': session_id,
                'state_path': str(book_state), 'obsidian_uri': uri}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--reading', type=Path, required=True)
    p.add_argument('--body', type=Path, required=True)
    p.add_argument('--quotes', type=Path, required=True)
    args = p.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8'))
    local_config = args.config.parent / 'config.local.json'
    if args.config.name == 'config.json' and local_config.exists():
        config.update(json.loads(local_config.read_text(encoding='utf-8')))
    result = save(config,
                  json.loads(args.reading.read_text(encoding='utf-8')),
                  args.body.read_text(encoding='utf-8'),
                  json.loads(args.quotes.read_text(encoding='utf-8')))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
