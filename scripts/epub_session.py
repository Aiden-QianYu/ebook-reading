#!/usr/bin/env python3
"""Extract a located, resumable EPUB reading segment. Python standard library only."""
import argparse
import datetime as dt
import hashlib
import json
import posixpath
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

CJK = r'[\u3400-\u9fff\uf900-\ufaff]'
TOKEN = re.compile(CJK + r"|[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*")
BLOCK = {'p', 'div', 'section', 'li', 'blockquote', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
SKIP = {'script', 'style', 'head', 'svg'}


def local(tag):
    return tag.rsplit('}', 1)[-1]


def norm(text):
    return re.sub(r'\s+', ' ', text or '')


class Publication:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.z = None if self.path.is_dir() else zipfile.ZipFile(self.path)
        try:
            container = ET.fromstring(self.read('META-INF/container.xml'))
            rootfiles = [x for x in container.iter() if local(x.tag) == 'rootfile']
            self.opf_member = self.safe_member(rootfiles[0].attrib['full-path'])
            opf_bytes = self.read(self.opf_member)
            self.opf = ET.fromstring(opf_bytes)
            manifest_node = next(x for x in self.opf if local(x.tag) == 'manifest')
            spine = next(x for x in self.opf if local(x.tag) == 'spine')
            self.manifest = {x.attrib['id']: x.attrib for x in manifest_node}
            self.docs = []
            digest = hashlib.sha256(opf_bytes)
            for ref in spine:
                if local(ref.tag) != 'itemref' or ref.attrib.get('linear') == 'no':
                    continue
                item = self.manifest[ref.attrib['idref']]
                if item.get('media-type') not in ('application/xhtml+xml', 'text/html'):
                    raise ValueError('Spine contains non-XHTML content; use original page: ' + item['href'])
                member = self.href(item['href'])
                raw = self.read(member)
                digest.update(member.encode('utf-8') + b'\0' + raw)
                node = ET.fromstring(raw)
                text, spans, starts, complex_tags = self.render(node)
                headings = [norm(''.join(x.itertext())).strip() for x in node.iter()
                            if local(x.tag) in ('h1', 'h2', 'h3')]
                self.docs.append({'member': member, 'title': next((h for h in headings if h), member),
                                  'node': node, 'text': text, 'spans': spans, 'starts': starts,
                                  'complex_tags': complex_tags})
            if not self.docs:
                raise ValueError('No readable linear XHTML spine')
            if len({d['member'] for d in self.docs}) != len(self.docs):
                raise ValueError('Repeated spine member requires original-page location handling')
            self.navigation_sources = []
            for item in self.manifest.values():
                kind = ('nav' if 'nav' in item.get('properties', '').split() else
                        'ncx' if item.get('media-type') == 'application/x-dtbncx+xml' else None)
                if kind:
                    member = self.href(item['href'])
                    raw = self.read(member)
                    self.navigation_sources.append((kind, member, raw))
                    digest.update(member.encode('utf-8') + b'\0' + raw)
            self.fingerprint = digest.hexdigest()
        finally:
            if self.z:
                self.z.close()

    def safe_member(self, member):
        if '\\' in member or member.startswith('/'):
            raise ValueError('Invalid EPUB member path')
        member = posixpath.normpath(member)
        if member in ('.', '..') or member.startswith('../'):
            raise ValueError('EPUB member escapes publication')
        return member

    def href(self, href):
        uri = urlsplit(href)
        if uri.scheme or uri.netloc:
            raise ValueError('Remote spine content unsupported')
        return self.safe_member(posixpath.join(posixpath.dirname(self.opf_member), unquote(uri.path)))

    def read(self, member):
        member = self.safe_member(member)
        if self.z:
            return self.z.read(member)
        path = (self.path / member).resolve()
        if self.path not in path.parents:
            raise ValueError('EPUB member symlink escapes publication')
        return path.read_bytes()

    @staticmethod
    def render(root):
        pieces, spans, starts, complex_tags = [], {}, {}, set()
        length = 0

        def append(value):
            nonlocal length
            pieces.append(value)
            length += len(value)

        def record(node, slot, raw):
            spans[(id(node), slot)] = (length, raw or '')
            append(norm(raw))

        def walk(node):
            tag = local(node.tag)
            starts[id(node)] = length
            if tag in ('img', 'svg', 'math', 'table'):
                complex_tags.add(tag)
            if tag in SKIP:
                return
            record(node, 'text', node.text)
            for child in node:
                walk(child)
                record(child, 'tail', child.tail)
            if tag in BLOCK or tag == 'br':
                append('\n')

        body = next((x for x in root.iter() if local(x.tag) == 'body'), root)
        walk(body)
        return ''.join(pieces), spans, starts, sorted(complex_tags)

    def member_index(self, member):
        matches = [i for i, d in enumerate(self.docs) if d['member'] == member]
        if len(matches) != 1:
            raise ValueError('Member is not uniquely in linear spine: ' + str(member))
        return matches[0]

    def cfi_location(self, cfi):
        if not cfi.startswith('epubcfi(') or not cfi.endswith(')'):
            raise ValueError('Expected epubcfi(...)')
        parts = split_range(cfi[8:-1])
        if len(parts) not in (1, 3):
            raise ValueError('Unsupported CFI range')
        point = parts[0] + (parts[1] if len(parts) == 3 else '')
        if point.count('!') != 1:
            raise ValueError('Only one package-to-content CFI indirection supported')
        package, content = point.split('!')
        ref, slot, char = walk_path(self.opf, package, allow_spine_idref=True)
        if slot or char or local(ref.tag) != 'itemref':
            raise ValueError('CFI does not resolve to an OPF itemref')
        index = self.member_index(self.href(self.manifest[ref.attrib['idref']]['href']))
        doc = self.docs[index]
        node, slot, char = walk_path(doc['node'], content)
        if slot:
            span = doc['spans'].get((id(node), slot))
            if span is None:
                raise ValueError('CFI points outside extracted body text')
            base, raw = span
            encoded = raw.encode('utf-16-le')
            if char * 2 > len(encoded):
                raise ValueError('CFI character offset beyond text')
            prefix = encoded[:char * 2].decode('utf-16-le')
            offset = base + len(norm(prefix))
        else:
            if id(node) not in doc['starts']:
                raise ValueError('CFI element is outside extracted body')
            offset = doc['starts'][id(node)]
        return index, offset


def split_range(value):
    depth, escaped, parts, start = 0, False, [], 0
    for i, ch in enumerate(value):
        if escaped:
            escaped = False
        elif ch == '^':
            escaped = True
        elif ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
        elif ch == ',' and depth == 0:
            parts.append(value[start:i])
            start = i + 1
        if depth < 0:
            raise ValueError('Malformed CFI assertion')
    if depth or escaped:
        raise ValueError('Malformed CFI assertion')
    return parts + [value[start:]]


def walk_path(root, value, allow_spine_idref=False):
    char = 0
    offset = re.search(r':(\d+)$', value)
    if offset:
        char = int(offset.group(1))
        value = value[:offset.start()]
    steps = list(re.finditer(r'/([1-9]\d*)(?:\[((?:\^.|[^\]^])*)\])?', value))
    if not steps or ''.join(m.group(0) for m in steps) != value:
        raise ValueError('Unsupported CFI extension; use a unique visible text anchor')
    node, slot = root, None
    for i, step in enumerate(steps):
        number, assertion = int(step.group(1)), step.group(2)
        children = list(node)
        if number % 2:
            if i != len(steps) - 1 or assertion is not None:
                raise ValueError('Unsupported CFI text assertion or intermediate text node')
            index = (number - 1) // 2
            if index > len(children):
                raise ValueError('CFI text node does not exist')
            if index == 0:
                slot = 'text'
            else:
                node, slot = children[index - 1], 'tail'
        else:
            index = number // 2 - 1
            if index >= len(children):
                raise ValueError('CFI element does not exist')
            node = children[index]
            if assertion is not None:
                expected = re.sub(r'\^(.)', r'\1', assertion)
                actual = node.attrib.get('id')
                # Observed Books compatibility: absent itemref @id, assertion equals @idref.
                if actual is None and allow_spine_idref and local(node.tag) == 'itemref':
                    actual = node.attrib.get('idref')
                if actual != expected:
                    raise ValueError('CFI ID assertion mismatch; re-establish anchor')
    if offset and slot is None:
        raise ValueError('Character offset requires a text node')
    return node, slot, char


def stats(text, zh_rate, en_rate):
    han = len(re.findall(CJK, text))
    words = sum(1 for m in TOKEN.finditer(text) if not re.fullmatch(CJK, m.group()))
    return {'cjk_chars': han, 'english_words': words,
            'estimated_minutes': han / zh_rate + words / en_rate}


def cutoff(text, minutes, zh_rate, en_rate):
    elapsed, end = 0, 0
    for m in TOKEN.finditer(text):
        cost = 1 / (zh_rate if re.fullmatch(CJK, m.group()) else en_rate)
        if elapsed + cost > minutes:
            break
        elapsed += cost
        end = m.end()
    else:
        return len(text)
    if end == 0:
        raise ValueError('Budget too small for even one reading unit')
    # Prefer a nearby sentence/paragraph ending while bounding the added content.
    after = re.search(r'[\n。！？.!?]', text[end:])
    if after:
        proposed = end + after.end()
        if stats(text[:proposed], zh_rate, en_rate)['estimated_minutes'] <= minutes * 1.10:
            return proposed
    before = list(re.finditer(r'[\n。！？.!?]', text[:end]))
    if before:
        proposed = before[-1].end()
        if stats(text[:proposed], zh_rate, en_rate)['estimated_minutes'] >= minutes * 0.85:
            return proposed
    return end


def build_session(pub, book, index, offset, minutes, zh_rate, en_rate, located_via, previous=None, through_member=None):
    if not 0 <= index < len(pub.docs) or not 0 <= offset <= len(pub.docs[index]['text']):
        raise ValueError('Cursor is outside source bounds')
    pieces, ranges, length = [], [], 0
    stop_index = pub.member_index(through_member) if through_member else len(pub.docs) - 1
    if stop_index < index:
        raise ValueError('End member precedes start location')
    for i in range(index, stop_index + 1):
        doc = pub.docs[i]
        start = offset if i == index else 0
        piece = doc['text'][start:]
        pieces.append(piece)
        ranges.append((i, start, length, length + len(piece)))
        length += len(piece)
    combined = ''.join(pieces)
    if not combined.strip() or not TOKEN.search(combined):
        raise ValueError('No remaining readable text (book ended or non-text content)')
    end = cutoff(combined, minutes, zh_rate, en_rate)
    segments = []
    for i, start, left, right in ranges:
        if left >= end:
            break
        take = min(end, right) - left
        if take <= 0:
            continue
        doc = pub.docs[i]
        segments.append({'member': doc['member'], 'title': doc['title'], 'spine_index': i,
                         'start_offset': start, 'end_offset': start + take,
                         'text': doc['text'][start:start + take], 'complex_tags': doc['complex_tags']})
    last = segments[-1]
    next_index, next_offset = last['spine_index'], last['end_offset']
    if next_offset == len(pub.docs[next_index]['text']) and next_index + 1 < len(pub.docs):
        next_index, next_offset = next_index + 1, 0
    measured = stats(combined[:end], zh_rate, en_rate)
    return {'schema_version': 1, 'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'format': 'epub', 'asset_id': book['asset_id'],
            'title': book.get('title'), 'author': book.get('author'), 'source_path': str(pub.path),
            'source_sha256': pub.fingerprint, 'source_quality': 'local EPUB text extraction',
            'books_progress_snapshot': book.get('reading_progress'), 'located_via': located_via,
            'planned_minutes': minutes, 'cjk_chars_per_minute': zh_rate,
            'english_words_per_minute': en_rate, **measured,
            'start_locator': {'member': segments[0]['member'], 'offset': segments[0]['start_offset']},
            'end_locator': {'member': last['member'], 'offset': last['end_offset']},
            'segments': segments, 'resume_from_session_id': previous,
            'next_cursor': {'format': 'epub', 'member': pub.docs[next_index]['member'], 'offset': next_offset},
            'at_book_end': next_index == len(pub.docs) - 1 and next_offset == len(pub.docs[-1]['text'])}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['inspect', 'index', 'locate', 'slice'])
    p.add_argument('--book-json', type=Path, required=True)
    group = p.add_mutually_exclusive_group()
    group.add_argument('--cfi')
    group.add_argument('--anchor')
    group.add_argument('--resume-state', type=Path)
    group.add_argument('--from-start', action='store_true')
    p.add_argument('--member')
    p.add_argument('--through-member', help='Stop no later than the end of this spine member')
    p.add_argument('--chapter-selection', type=Path, help='JSON chapter_outline_ids for the index action')
    p.add_argument('--chapter-map', type=Path)
    chapter_mode = p.add_mutually_exclusive_group()
    chapter_mode.add_argument('--chapters', help='Complete chapter numbers, e.g. 3-5,8')
    chapter_mode.add_argument('--next-chapters', type=int, help='Continue N chapters from current cursor')
    p.add_argument('--verified-position', action='store_true', help='Only attest after UI/user text verification')
    p.add_argument('--previous-session')
    p.add_argument('--minutes', type=float, default=30)
    p.add_argument('--zh-rate', type=float, default=400)
    p.add_argument('--en-rate', type=float, default=200)
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    if min(args.minutes, args.zh_rate, args.en_rate) <= 0:
        raise ValueError('Budget and rates must be positive')
    data = json.loads(args.book_json.read_text(encoding='utf-8'))
    book = data.get('book', data)
    if not book.get('path') or Path(book['path']).suffix.lower() != '.epub':
        raise ValueError('This helper requires a local EPUB; use the PDF/UI route otherwise')
    pub = Publication(book['path'])
    from epub_chapters import outline, make_index, chapter_session, annotate_time_session
    chapter_map = json.loads(args.chapter_map.read_text(encoding='utf-8')) if args.chapter_map else None
    if args.action == 'inspect':
        result = {'source_sha256': pub.fingerprint, 'spine': [
            {k: d[k] for k in ('member', 'title', 'complex_tags')} | {'text_length': len(d['text'])}
            for d in pub.docs], 'outline': outline(pub)}
    elif args.action == 'index':
        if not args.chapter_selection:
            raise ValueError('Index action requires a verified chapter-level outline selection')
        result = make_index(pub, json.loads(args.chapter_selection.read_text(encoding='utf-8')), book)
    elif args.action == 'slice' and args.chapters:
        if chapter_map is None:
            raise ValueError('Chapter mode requires --chapter-map')
        if args.cfi or args.anchor or args.resume_state or args.member or args.from_start:
            raise ValueError('Explicit chapters start at their TOC boundaries; use --previous-session for saved-state concurrency')
        result = chapter_session(pub, book, chapter_map, requested=args.chapters,
                                 previous=args.previous_session, zh_rate=args.zh_rate, en_rate=args.en_rate)
    else:
        previous = args.previous_session
        if args.cfi:
            if args.action == 'slice' and not args.verified_position:
                raise ValueError('Verify current position first; locate can show CFI context')
            index, offset = pub.cfi_location(args.cfi)
            via = 'CFI matched to verified current position' if args.verified_position else 'CFI candidate, unverified'
        elif args.resume_state:
            state = json.loads(args.resume_state.read_text(encoding='utf-8'))
            if state.get('source_sha256') != pub.fingerprint or state.get('asset_id') != book['asset_id']:
                raise ValueError('Source version or book identity changed; establish a new anchor')
            cursor = state.get('next_cursor') or {}
            if cursor.get('format') != 'epub':
                raise ValueError('No resumable EPUB cursor; establish current position')
            index, offset = pub.member_index(cursor['member']), cursor['offset']
            previous, via = state['session_id'], 'saved AI cursor'
        elif args.anchor:
            matches = []
            for i, doc in enumerate(pub.docs):
                if args.member and doc['member'] != args.member:
                    continue
                for m in re.finditer(re.escape(args.anchor), doc['text']):
                    matches.append((i, m.start()))
            if len(matches) != 1:
                raise ValueError('Anchor must occur exactly once; found ' + str(len(matches)))
            (index, offset), via = matches[0], 'unique user/UI text anchor'
        elif args.member:
            index, offset, via = pub.member_index(args.member), 0, 'explicit chapter start'
        elif args.from_start:
            index, offset, via = 0, 0, 'explicit book start'
        else:
            raise ValueError('Choose a verified CFI, unique anchor, chapter start, book start, or saved AI cursor')
        if args.action == 'locate':
            doc = pub.docs[index]
            result = {'member': doc['member'], 'title': doc['title'], 'offset': offset,
                      'located_via': via, 'context_before': doc['text'][max(0, offset - 80):offset],
                      'context_after': doc['text'][offset:offset + 160]}
        else:
            if args.next_chapters is not None:
                if chapter_map is None:
                    raise ValueError('Relative chapter mode requires --chapter-map')
                result = chapter_session(pub, book, chapter_map, count=args.next_chapters,
                                         cursor={'member': pub.docs[index]['member'], 'offset': offset},
                                         previous=previous, zh_rate=args.zh_rate, en_rate=args.en_rate)
            else:
                result = build_session(pub, book, index, offset, args.minutes, args.zh_rate, args.en_rate,
                                       via, previous, args.through_member)
                if chapter_map is not None:
                    result = annotate_time_session(pub, result, chapter_map)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + '\n', encoding='utf-8')
        print(json.dumps({'output': str(args.output.resolve()),
                          'estimated_minutes': result.get('estimated_minutes'),
                          'segments': len(result.get('segments', []))}, ensure_ascii=False))
    else:
        print(output)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, IndexError, ET.ParseError, zipfile.BadZipFile) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
