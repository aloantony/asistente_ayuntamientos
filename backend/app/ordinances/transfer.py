"""Selective corpus bundles. Planning is read-only; applying never overwrites data."""
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from app.municipalities.models import Municipality
from app.ordinances.coverage import CYL_PROVINCES
from app.ordinances.import_service import is_official_source_url
from app.ordinances.models import OfficialLegalSource, Ordinance, OrdinanceLegalChunk
from app.ordinances.schemas import OrdinanceCreate

FIELDS = ('title', 'topic', 'subtopic', 'ordinance_type', 'summary', 'source_url',
          'official_bulletin', 'bulletin_number', 'approval_date', 'publication_date',
          'effective_date', 'status', 'curation_status', 'text_content', 'legal_review_notes')
CHUNK_FIELDS = ('chunk_index', 'heading', 'citation', 'text', 'source_url', 'source_locator', 'review_status')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str,
                                     separators=(',', ':')).encode()).hexdigest()


def record_data(ordinance):
    return json.loads(json.dumps({key: getattr(ordinance, key) for key in FIELDS}, default=str))


def export_bundle(db: Session):
    sources = list(db.scalars(select(OfficialLegalSource).where(OfficialLegalSource.status == 'active')))
    rows = db.scalars(select(Ordinance).options(selectinload(Ordinance.municipality),
                                              selectinload(Ordinance.legal_chunks)).order_by(Ordinance.id))
    records, excluded = [], Counter()
    seen = set()
    for ordinance in rows:
        code = ordinance.municipality.ine_code or ''
        reason = None
        if not re.fullmatch(r'[0-9]{5}', code) or code[:2] not in CYL_PROVINCES:
            reason = 'invalid_or_outside_cyl_identity'
        elif ordinance.curation_status != 'approved' or ordinance.status == 'archived':
            reason = 'not_approved_or_archived'
        elif ordinance.document_id is not None:
            reason = 'linked_private_document'
        elif not ordinance.source_url or not is_official_source_url(ordinance.source_url, sources):
            reason = 'source_not_allowlisted'
        elif not (ordinance.text_content or '').strip():
            reason = 'missing_extracted_text'
        chunks = [{key: getattr(chunk, key) for key in CHUNK_FIELDS}
                  for chunk in sorted(ordinance.legal_chunks, key=lambda c: c.chunk_index)
                  if chunk.review_status == 'approved' and chunk.text.strip()]
        if not reason and not chunks:
            reason = 'missing_reviewed_chunks'
        if not reason and any(c['source_url'] and not is_official_source_url(c['source_url'], sources) for c in chunks):
            reason = 'chunk_source_not_allowlisted'
        key = (code, ordinance.source_url)
        if not reason and key in seen:
            reason = 'duplicate_identity_url'
        if reason:
            excluded[reason] += 1
            continue
        seen.add(key)
        records.append({'ine_code': code, 'municipality_name': ordinance.municipality.name,
                        'ordinance': record_data(ordinance), 'chunks': chunks})
    return {'format': 'miconcejo-reviewed-corpus-v1',
            'exported_at': datetime.now(timezone.utc).isoformat(),
            'records_sha256': digest(records), 'excluded': dict(excluded), 'records': records}


def plan_transfer(db: Session, bundle: dict):
    if bundle.get('format') != 'miconcejo-reviewed-corpus-v1' or digest(bundle.get('records')) != bundle.get('records_sha256'):
        raise ValueError('Bundle format or checksum mismatch')
    municipalities = {}
    for municipality in db.scalars(select(Municipality)):
        municipalities.setdefault(municipality.ine_code, []).append(municipality)
    existing = {}
    for ordinance in db.scalars(select(Ordinance).options(selectinload(Ordinance.municipality), selectinload(Ordinance.legal_chunks))):
        existing.setdefault((ordinance.municipality.ine_code, ordinance.source_url), []).append(ordinance)
    sources = list(db.scalars(select(OfficialLegalSource).where(OfficialLegalSource.status == 'active')))
    results, seen = [], set()
    for record in bundle['records']:
        code, data = record['ine_code'], record['ordinance']
        if set(data) != set(FIELDS) or data['curation_status'] != 'approved' or not re.fullmatch(r'[0-9]{5}', code) or code[:2] not in CYL_PROVINCES:
            raise ValueError('Invalid selective corpus record')
        OrdinanceCreate.model_validate(dict(data, municipality_id=1))
        key = (code, data['source_url'])
        if key in seen:
            raise ValueError('Duplicate identity and URL in bundle')
        seen.add(key)
        if not record['chunks'] or any(set(c) != set(CHUNK_FIELDS) or c['review_status'] != 'approved' or not isinstance(c['text'], str) or not c['text'].strip() for c in record['chunks']):
            raise ValueError('Invalid reviewed chunks')
        indexes = [c['chunk_index'] for c in record['chunks']]
        if any(not isinstance(i, int) or isinstance(i, bool) or i < 0 for i in indexes) or len(set(indexes)) != len(indexes):
            raise ValueError('Invalid chunk indexes')
        candidates = municipalities.get(code, [])
        matches = existing.get(key, [])
        outcome = 'create'
        if len(candidates) != 1 or candidates[0].status != 'active':
            outcome = 'municipality_missing_or_ambiguous'
        elif not data['source_url'] or not is_official_source_url(data['source_url'], sources) or any(c['source_url'] and not is_official_source_url(c['source_url'], sources) for c in record['chunks']):
            outcome = 'source_not_allowlisted'
        elif matches:
            same_content = len(matches) == 1 and digest(record_data(matches[0])) == digest(data)
            target_chunks = ([{key: getattr(chunk, key) for key in CHUNK_FIELDS}
                              for chunk in sorted(matches[0].legal_chunks, key=lambda c: c.chunk_index)]
                             if len(matches) == 1 else [])
            same_chunks = digest(target_chunks) == digest(sorted(record['chunks'], key=lambda c: c['chunk_index']))
            outcome = 'unchanged' if same_content and same_chunks else 'conflict'
        results.append({'ine_code': code, 'source_url': data['source_url'], 'outcome': outcome,
                        'municipality_id': candidates[0].id if len(candidates) == 1 else None})
    return {'bundle_sha256': bundle['records_sha256'], 'counts': dict(Counter(r['outcome'] for r in results)), 'items': results}


def apply_transfer(db: Session, bundle: dict):
    """Caller owns commit. Abort the entire bundle if any prerequisite is unresolved."""
    db.execute(text("SET LOCAL lock_timeout = '5s'"))
    db.execute(text("LOCK TABLE ordinances, ordinance_legal_chunks, municipalities, official_legal_sources IN SHARE ROW EXCLUSIVE MODE"))
    plan = plan_transfer(db, bundle)
    if any(item['outcome'] not in ('create', 'unchanged') for item in plan['items']):
        raise ValueError('Transfer has unresolved identities, sources or content conflicts')
    for record, item in zip(bundle['records'], plan['items']):
        if item['outcome'] == 'unchanged':
            continue
        data = OrdinanceCreate.model_validate(dict(record['ordinance'], municipality_id=item['municipality_id']))
        ordinance = Ordinance(**data.model_dump(exclude={'notes'}), extraction_status='selective_transfer',
                              notes=f"Selective transfer bundle SHA-256: {bundle['records_sha256']}")
        db.add(ordinance)
        db.flush()
        for chunk in record['chunks']:
            if set(chunk) != set(CHUNK_FIELDS) or chunk['review_status'] != 'approved' or not chunk['text'].strip():
                raise ValueError('Invalid reviewed chunk')
            db.add(OrdinanceLegalChunk(ordinance_id=ordinance.id, **chunk,
                                      embedding_status='pending'))
    db.flush()
    return plan


def main():
    import argparse
    import os
    from pathlib import Path
    from app.main import app  # Register the complete ORM model graph.
    from app.db.session import SessionLocal

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['export', 'plan', 'apply'])
    parser.add_argument('--bundle', required=True, type=Path)
    arguments = parser.parse_args()
    with SessionLocal() as db:
        if arguments.operation != 'apply':
            db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        if arguments.operation == 'export':
            bundle = export_bundle(db)
            fd = os.open(arguments.bundle, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(bundle, stream, ensure_ascii=False, default=str)
            print(json.dumps({'records': len(bundle['records']), 'excluded': bundle['excluded'],
                              'sha256': bundle['records_sha256']}))
        else:
            bundle = json.loads(arguments.bundle.read_text())
            report = apply_transfer(db, bundle) if arguments.operation == 'apply' else plan_transfer(db, bundle)
            if arguments.operation == 'apply':
                db.commit()
            print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
