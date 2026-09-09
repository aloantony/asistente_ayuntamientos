from app.municipalities.models import Municipality
from app.ordinances.models import Ordinance
from app.ordinances.coverage import build_cyl_coverage
from conftest import headers_for


def test_coverage_uses_valid_ine_not_province_spelling(db):
    valid=Municipality(autonomous_community='Castilla y León',name='Municipio real de prueba',province='Leon',ine_code='24001')
    fake=Municipality(autonomous_community='Castilla y León',name='Demostración',province='León',ine_code='oa-demo')
    db.add_all([valid,fake]);db.flush()
    for municipality in (valid,fake):
        db.add(Ordinance(municipality_id=municipality.id,title='Texto',topic='agua',ordinance_type='ordinance',curation_status='approved'))
    db.commit()
    report=build_cyl_coverage(db)
    assert len(report['provinces'])==9
    leon=next(p for p in report['provinces'] if p['code']=='24')
    assert leon['name']=='León' and leon['ordinances']==1
    assert leon['unknown_validity']==1 and leon['with_reviewed_text']==0
    assert report['official_source_coverage']=='not_verified'
    assert next(p for p in report['provinces'] if p['code']=='05')['ordinances']==0


def test_coverage_requires_library_permission(client, make_user):
    assert client.get('/ordinances/coverage/cyl',headers=headers_for(make_user())).status_code==403
