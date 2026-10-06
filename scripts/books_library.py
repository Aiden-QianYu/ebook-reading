#!/usr/bin/env python3
"""Read only the observed Apple Books library schema; never update Books."""
import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path


def connect(path):
    con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    con.execute('PRAGMA query_only=ON')
    con.row_factory = sqlite3.Row
    return con


def schema(con, table):
    return {row[1] for row in con.execute('PRAGMA table_info("' + table + '")')}


def read_library(root, asset_id=None):
    paths = sorted((root / 'BKLibrary').glob('*.sqlite'))
    if len(paths) != 1:
        raise ValueError('Expected one BKLibrary database; inspect schema before choosing: ' + str(paths))
    path = paths[0]
    con = connect(path)
    try:
        required = {'ZASSETID', 'ZTITLE', 'ZREADINGPROGRESS', 'ZPATH'}
        columns = schema(con, 'ZBKLIBRARYASSET')
        if not required <= columns:
            raise ValueError('Books schema changed; missing columns: ' + str(required - columns))
        selected = ['ZASSETID', 'ZTITLE', 'ZREADINGPROGRESS', 'ZPATH']
        selected += [x for x in ('ZAUTHOR', 'ZPAGECOUNT', 'ZISFINISHED', 'ZLASTOPENDATE') if x in columns]
        query = 'SELECT ' + ','.join(selected) + ' FROM ZBKLIBRARYASSET'
        if asset_id:
            query += ' WHERE ZASSETID=?'
        rows = con.execute(query, (asset_id,) if asset_id else ()).fetchall()
    finally:
        con.close()
    books = []
    for row in rows:
        r = dict(row)
        progress = r['ZREADINGPROGRESS']
        valid = isinstance(progress, (int, float)) and 0 <= progress <= 1
        local = r['ZPATH']
        books.append({
            'asset_id': r['ZASSETID'], 'title': r['ZTITLE'], 'author': r.get('ZAUTHOR'),
            'reading_progress': progress if valid else None,
            'progress_percent': round(progress * 100, 2) if valid else None,
            'page_count_raw': r.get('ZPAGECOUNT'),
            'finished_raw': r.get('ZISFINISHED'),
            'path': local,
            'path_exists': Path(local).exists() if local else False,
            'format': Path(local).suffix.lower().lstrip('.') if local else None,
        })
    if asset_id and len(books) != 1:
        raise ValueError('Selected asset_id must match exactly one book')
    annotation_paths = sorted((root / 'AEAnnotation').glob('*.sqlite'))
    warnings = []
    if len(annotation_paths) == 1:
        try:
            ac = connect(annotation_paths[0])
            try:
                columns = schema(ac, 'ZAEANNOTATION')
                needed = {'ZANNOTATIONASSETID', 'ZANNOTATIONTYPE', 'ZANNOTATIONLOCATION',
                          'ZANNOTATIONMODIFICATIONDATE', 'ZANNOTATIONDELETED'}
                if not needed <= columns:
                    raise ValueError('Annotation schema changed')
                for book in books:
                    row = ac.execute(
                        'SELECT ZANNOTATIONLOCATION,ZANNOTATIONMODIFICATIONDATE '
                        'FROM ZAEANNOTATION WHERE ZANNOTATIONASSETID=? AND ZANNOTATIONTYPE=3 '
                        'AND ZANNOTATIONDELETED=0 ORDER BY ZANNOTATIONMODIFICATIONDATE DESC LIMIT 1',
                        (book['asset_id'],)).fetchone()
                    book['location_candidate'] = ({'location': row[0], 'modified_cf_absolute_time': row[1],
                        'source': 'AEAnnotation type 3 (internal schema)',
                        'verified_current_position': False} if row else None)
            finally:
                ac.close()
        except (OSError, sqlite3.Error, ValueError) as exc:
            warnings.append(str(exc))
    else:
        warnings.append('Annotation database not uniquely available; current position unverified')
    result = {'observed_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'source': str(path), 'warnings': warnings, 'books': books}
    if asset_id:
        result['book'] = books[0]
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--documents', type=Path, default=Path.home() / 'Library/Containers/com.apple.iBooksX/Data/Documents')
    p.add_argument('--asset-id')
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    result = read_library(args.documents, args.asset_id)
    data = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(data + '\n', encoding='utf-8')
        print(json.dumps({'output': str(args.output.resolve()), 'book_count': len(result['books'])}, ensure_ascii=False))
    else:
        print(data)


if __name__ == '__main__':
    try:
        main()
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
