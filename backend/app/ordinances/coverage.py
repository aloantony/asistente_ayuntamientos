"""Observed regional corpus coverage; never equates inventory with completeness."""
from datetime import datetime, timezone

from sqlalchemy import case, distinct, func, select
from sqlalchemy.orm import Session

from app.municipalities.models import Municipality
from app.ordinances.models import Ordinance, OrdinanceLegalChunk

CYL_PROVINCES = {'05': 'Ávila', '09': 'Burgos', '24': 'León', '34': 'Palencia',
                 '37': 'Salamanca', '40': 'Segovia', '42': 'Soria', '47': 'Valladolid', '49': 'Zamora'}


def build_cyl_coverage(db: Session) -> dict:
    prefix = func.substr(Municipality.ine_code, 1, 2)
    valid_code = Municipality.ine_code.op('~')('^[0-9]{5}$')
    has_text = select(OrdinanceLegalChunk.id).where(
        OrdinanceLegalChunk.ordinance_id == Ordinance.id,
        OrdinanceLegalChunk.review_status == 'approved',
        func.length(func.trim(OrdinanceLegalChunk.text)) > 0,
    ).exists()
    rows = db.execute(select(
        prefix.label('code'), func.count(Ordinance.id).label('ordinances'),
        func.count(distinct(Ordinance.municipality_id)).label('municipalities'),
        func.sum(case((has_text, 1), else_=0)).label('with_reviewed_text'),
        func.sum(case((Ordinance.status == 'unknown', 1), else_=0)).label('unknown_validity'),
        func.max(Ordinance.publication_date).label('latest_publication'),
    ).join(Municipality, Municipality.id == Ordinance.municipality_id).where(
        valid_code, prefix.in_(CYL_PROVINCES), Ordinance.curation_status == 'approved',
        Ordinance.status != 'archived',
    ).group_by(prefix)).mappings()
    by_code = {row['code']: dict(row) for row in rows}
    return {
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'reviewed_internal_corpus', 'official_source_coverage': 'not_verified',
        'provinces': [dict(by_code.get(code, dict(code=code, ordinances=0, municipalities=0,
            with_reviewed_text=0, unknown_validity=0, latest_publication=None)), name=name)
            for code, name in CYL_PROVINCES.items()],
        'notice': 'Los recuentos describen la biblioteca revisada disponible. No prueban cobertura completa ni vigencia jurídica; cero resultados no demuestra inexistencia de ordenanzas.',
    }
