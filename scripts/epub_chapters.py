"""Resolve EPUB navigation chapters and exact content ranges, including in-file anchors."""
import datetime as dt
import posixpath
import re
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit


def local(tag):
    return tag.rsplit('}', 1)[-1]


def number_in_title(title):
    match = re.match(r'^\s*(?:第\s*([0-9零〇一二两三四五六七八九十百千]+)\s*[章回]|chapter\s+(\d+)\b|(\d+)[\s.、:：])', title, re.I)
    if not match:
        return None
    value = next(x for x in match.groups() if x is not None)
    if value.isdigit():
        return int(value)
    digits = dict(zip('零〇一二两三四五六七八九', [0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9]))
    units, total, digit = {'十': 10, '百': 100, '千': 1000}, 0, 0
    for char in value:
        if char in digits:
            digit = digits[char]
        else:
            total += (digit or 1) * units[char]
            digit = 0
    return total + digit


def position(pub, locator):
    index = pub.member_index(locator['member'])
    offset = locator['offset']
    if not isinstance(offset, int) or not 0 <= offset <= len(pub.docs[index]['text']):
        raise ValueError('Chapter locator is outside source bounds')
    return sum(len(d['text']) for d in pub.docs[:index]) + offset


def resolve_href(pub, source_member, href):
    uri = urlsplit(href)
    if uri.scheme or uri.netloc:
        raise ValueError('Remote navigation target unsupported')
    member = pub.safe_member(posixpath.join(posixpath.dirname(source_member), unquote(uri.path)))
    doc = pub.docs[pub.member_index(member)]
    offset = 0
    if uri.fragment:
        nodes = [x for x in doc['node'].iter() if x.attrib.get('id') == unquote(uri.fragment)]
        if len(nodes) != 1 or id(nodes[0]) not in doc['starts']:
            raise ValueError('Navigation anchor is not uniquely in extracted body')
        offset = doc['starts'][id(nodes[0])]
    return {'member': member, 'offset': offset}


def outline(pub):
    entries, warnings = [], []
    sources = [s for s in pub.navigation_sources if s[0] == 'nav'] or pub.navigation_sources

    def add(title, href, depth, source, parent):
        entry = {'outline_id': 'toc-' + str(len(entries) + 1), 'title': title.strip(),
                 'depth': depth, 'parent_id': parent, 'source': source, 'href': href}
        try:
            entry['start'] = resolve_href(pub, source, href)
        except ValueError as exc:
            entry['start'] = None
            entry['warning'] = str(exc)
        entries.append(entry)
        return entry['outline_id']

    for kind, source, raw in sources:
        root = ET.fromstring(raw)
        if kind == 'nav':
            nav = next((x for x in root.iter() if local(x.tag) == 'nav' and any(
                local(k) == 'type' and 'toc' in v.split() for k, v in x.attrib.items())), None)
            if nav is None:
                warnings.append('No EPUB 3 toc nav found in ' + source)
                continue

            def walk_list(node, depth=1, parent=None):
                for child in node:
                    if local(child.tag) == 'ol':
                        walk_list(child, depth, parent)
                    elif local(child.tag) == 'li':
                        link = next((x for x in child if local(x.tag) == 'a'), None)
                        heading = next((x for x in child if local(x.tag) in ('a', 'span')), None)
                        current = parent
                        if link is not None and link.attrib.get('href'):
                            current = add(' '.join(''.join(link.itertext()).split()), link.attrib['href'], depth, source, parent)
                        elif heading is not None:
                            warnings.append('Unlinked navigation container: ' + ''.join(heading.itertext()).strip())
                        for nested in child:
                            if local(nested.tag) == 'ol':
                                walk_list(nested, depth + 1, current)
            walk_list(nav)
        else:
            navmap = next((x for x in root.iter() if local(x.tag) == 'navMap'), None)
            if navmap is None:
                continue

            def walk_ncx(node, depth=1, parent=None):
                for point in node:
                    if local(point.tag) != 'navPoint':
                        continue
                    label = next((x for x in point if local(x.tag) == 'navLabel'), None)
                    content = next((x for x in point if local(x.tag) == 'content'), None)
                    current = parent
                    if label is not None and content is not None:
                        current = add(' '.join(''.join(label.itertext()).split()), content.attrib['src'], depth, source, parent)
                    walk_ncx(point, depth + 1, current)
            walk_ncx(navmap)
    if not any(e['start'] for e in entries):
        entries = []
        for doc in pub.docs:
            for node in doc['node'].iter():
                tag = local(node.tag)
                if tag in ('h1', 'h2', 'h3') and id(node) in doc['starts']:
                    entries.append({'outline_id': 'heading-' + str(len(entries) + 1),
                        'title': ' '.join(''.join(node.itertext()).split()), 'depth': int(tag[-1]),
                        'parent_id': None, 'source': 'XHTML headings (verify chapter level)',
                        'start': {'member': doc['member'], 'offset': doc['starts'][id(node)]}})
        warnings.append('TOC unavailable: headings are candidates, not automatically chapters')
    return {'entries': entries, 'warnings': warnings}


def make_index(pub, selection, book):
    navigation = outline(pub)
    chosen = selection.get('chapter_outline_ids', []) if isinstance(selection, dict) else selection
    if not chosen or len(set(chosen)) != len(chosen):
        raise ValueError('Select unique outline IDs at the actual chapter level')
    by_id = {e['outline_id']: e for e in navigation['entries']}
    selected = [by_id[x] for x in chosen]
    if any(not e['start'] for e in selected):
        raise ValueError('Selected chapter has an unresolved navigation target')
    starts = [position(pub, e['start']) for e in selected]
    if starts != sorted(starts) or len(set(starts)) != len(starts):
        raise ValueError('Chapters must be in source order, with distinct starting positions')
    chapters = []
    final = {'member': pub.docs[-1]['member'], 'offset': len(pub.docs[-1]['text'])}
    for i, entry in enumerate(selected):
        candidates = [e['start'] for e in navigation['entries'] if e.get('start')
                      and e['depth'] <= entry['depth'] and position(pub, e['start']) > starts[i]]
        if i + 1 < len(selected):
            candidates.append(selected[i + 1]['start'])
        end = min(candidates, key=lambda x: position(pub, x)) if candidates else final
        number = number_in_title(entry['title'])
        chapters.append({'ordinal': i + 1, 'number': number,
                         'number_basis': 'printed-title' if number is not None else 'toc-order',
                         'title': entry['title'], 'outline_id': entry['outline_id'],
                         'start': entry['start'], 'end': end})
    return {'asset_id': book['asset_id'], 'source_sha256': pub.fingerprint, 'chapters': chapters,
            'warnings': navigation['warnings']}


def validate_index(pub, index, book):
    if index.get('source_sha256') != pub.fingerprint or index.get('asset_id') != book['asset_id']:
        raise ValueError('Chapter index belongs to a different source version or book')
    chapters = index['chapters']
    if not chapters:
        raise ValueError('Chapter index is empty')
    previous = -1
    for chapter in chapters:
        start, end = position(pub, chapter['start']), position(pub, chapter['end'])
        if not start < end or start < previous:
            raise ValueError('Chapter ranges are invalid or overlap')
        previous = end
    return chapters


def segments_between(pub, start, end, chapter=None):
    low, high = position(pub, start), position(pub, end)
    segments, base = [], 0
    for i, doc in enumerate(pub.docs):
        left, right = max(0, low - base), min(len(doc['text']), high - base)
        if right > left:
            segments.append({'member': doc['member'], 'title': doc['title'], 'spine_index': i,
                             'start_offset': left, 'end_offset': right, 'text': doc['text'][left:right],
                             'complex_tags': doc['complex_tags']})
            if chapter:
                segments[-1]['chapter_title'] = chapter['title']
        base += len(doc['text'])
    return segments


def chapter_session(pub, book, chapter_index, requested=None, count=None, cursor=None,
                    previous=None, zh_rate=400, en_rate=200):
    from epub_session import stats
    chapters = validate_index(pub, chapter_index, book)
    selected = []
    if requested is not None:
        numbers = []
        for token in requested.split(','):
            token = token.strip()
            if re.fullmatch(r'\d+-\d+', token):
                first, last = map(int, token.split('-'))
                if first > last:
                    raise ValueError('Chapter range is reversed')
                numbers.extend(range(first, last + 1))
            elif token.isdigit():
                numbers.append(int(token))
            else:
                raise ValueError('Use chapter numbers such as 3-5,8')
        for number in sorted(set(numbers)):
            matches = [c for c in chapters if (c['number'] if c['number'] is not None else c['ordinal']) == number]
            if len(matches) != 1:
                raise ValueError('Chapter number is missing or ambiguous: ' + str(number))
            selected.append(matches[0])
        selected.sort(key=lambda c: position(pub, c['start']))
        mode = 'specified-chapters'
    else:
        if not isinstance(count, int) or count <= 0 or cursor is None:
            raise ValueError('Relative chapters require a positive count and a verified/resumable cursor')
        at = position(pub, cursor)
        remaining = []
        for c in chapters:
            if position(pub, c['end']) <= at:
                continue
            start = cursor if at > position(pub, c['start']) else c['start']
            if not any(s['text'].strip() for s in segments_between(pub, start, c['end'])):
                continue
            remaining.append(c)
        selected = remaining[:count]
        mode = 'next-chapters'
    if not selected:
        raise ValueError('No remaining selected chapters')
    segments, chapter_ranges = [], []
    for i, chapter in enumerate(selected):
        start = chapter['start']
        if count is not None and i == 0 and position(pub, cursor) > position(pub, start):
            start = cursor
        segments += segments_between(pub, start, chapter['end'], chapter)
        chapter_ranges.append({**chapter, 'actual_start': start, 'actual_end': chapter['end'],
                               'partial_start': position(pub, start) > position(pub, chapter['start']),
                               'partial_end': False})
    text = '\n'.join(s['text'] for s in segments)
    measured = stats(text, zh_rate, en_rate)
    if not text.strip() or measured['estimated_minutes'] <= 0:
        raise ValueError('Selected chapter ranges have no readable text')
    end = selected[-1]['end']
    reading = {'schema_version': 2, 'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
               'format': 'epub', 'asset_id': book['asset_id'], 'title': book.get('title'),
               'author': book.get('author'), 'source_path': str(pub.path), 'source_sha256': pub.fingerprint,
               'source_quality': 'local EPUB text extraction', 'books_progress_snapshot': book.get('reading_progress'),
               'scope_mode': mode, 'planned_minutes': None, 'requested_chapter_count': count,
               'actual_chapter_count': len(selected), 'cjk_chars_per_minute': zh_rate,
               'english_words_per_minute': en_rate, **measured, 'segments': segments,
               'start_locator': {'member': segments[0]['member'], 'offset': segments[0]['start_offset']},
               'end_locator': {'member': segments[-1]['member'], 'offset': segments[-1]['end_offset']},
               'next_cursor': {'format': 'epub', **end}, 'resume_from_session_id': previous,
               'at_book_end': position(pub, end) == sum(len(d['text']) for d in pub.docs),
               'chapter_ranges': chapter_ranges}
    return add_display_fields(reading)


def annotate_time_session(pub, reading, chapter_index):
    chapters = validate_index(pub, chapter_index, reading)
    low, high = position(pub, reading['start_locator']), position(pub, reading['end_locator'])
    ranges = []
    for c in chapters:
        left, right = position(pub, c['start']), position(pub, c['end'])
        if left < high and right > low:
            ranges.append({**c, 'partial_start': low > left, 'partial_end': high < right})
    if not ranges or low < position(pub, ranges[0]['start']) or high > position(pub, ranges[-1]['end']):
        raise ValueError('Timed content extends outside the chapter index; include/verify the relevant TOC sections')
    reading['chapter_ranges'] = ranges
    reading['scope_mode'] = 'time-equivalent'
    return add_display_fields(reading)


def chapter_label(c):
    prefix = ('第' + str(c['number']) + '章') if c['number'] is not None else ('目录第' + str(c['ordinal']) + '节（未编号）')
    return prefix + '《' + c['title'] + '》'


def last_sentence(segments):
    trailing = next((s['text'].strip() for s in reversed(segments) if s['text'].strip()), '')
    if not trailing:
        raise ValueError('Reading contains no terminal text')
    # A last visible heading or a cut sentence is preserved as a tail fragment, never completed.
    paragraph = trailing.splitlines()[-1].strip()
    sentences = re.findall(r'[^。！？.!?]+(?:[。！？.!?]+[”’"」』）)]*|$)', paragraph)
    result = sentences[-1].strip() if sentences else paragraph
    complete = bool(re.search(r'[。！？.!?][”’"」』）)]*$', result))
    return result, complete


def add_display_fields(reading):
    ranges = reading['chapter_ranges']
    contiguous = all(b['ordinal'] == a['ordinal'] + 1 for a, b in zip(ranges, ranges[1:]))
    if len(ranges) == 1:
        display = chapter_label(ranges[0])
    elif contiguous:
        display = chapter_label(ranges[0]) + ' 至 ' + chapter_label(ranges[-1])
    else:
        display = '、'.join(chapter_label(c) for c in ranges) + '（非连续章节）'
    notes = []
    if ranges[0].get('partial_start'):
        notes.append('从起始章中途续读')
    if ranges[-1].get('partial_end'):
        notes.append('终止章尚未读完')
    reading['reading_range'] = display + ('；' + '，'.join(notes) if notes else '')
    reading['starting_chapter_title'] = ranges[0]['title']
    reading['last_sentence'], reading['last_sentence_complete'] = last_sentence(reading['segments'])
    return reading
