import pytest
from sqlalchemy import select, func, delete
from app.municipalities.models import Municipality
from app.ordinances.models import Ordinance, OfficialLegalSource, OrdinanceLegalChunk
from app.ordinances.transfer import export_bundle, plan_transfer, apply_transfer


def test_selective_transfer_is_repeatable_and_does_not_overwrite(db):
    municipality=Municipality(autonomous_community='Castilla y León',name='Demo',province='Burgos',ine_code='09001')
    source=OfficialLegalSource(name='BOP',base_url='https://bop.example.es',domain='bop.example.es',source_type='bop')
    db.add_all([municipality,source]);db.flush()
    ordinance=Ordinance(municipality_id=municipality.id,title='Ordenanza',topic='agua',ordinance_type='ordinance',
        source_url='https://bop.example.es/1.pdf',curation_status='approved',status='unknown',text_content='Texto íntegro sintético')
    db.add(ordinance);db.flush()
    db.add(OrdinanceLegalChunk(ordinance_id=ordinance.id,chunk_index=0,text='Texto íntegro sintético',review_status='approved'))
    db.commit()
    bundle=export_bundle(db)
    assert len(bundle['records'])==1
    assert plan_transfer(db,bundle)['counts']=={'unchanged':1}
    # An isolated fixture simulates an empty target; no production records touched.
    db.execute(delete(Ordinance).where(Ordinance.id==ordinance.id));db.commit()
    assert plan_transfer(db,bundle)['counts']=={'create':1}
    assert db.scalar(select(func.count()).select_from(Ordinance))==0
    apply_transfer(db,bundle);db.commit()
    apply_transfer(db,bundle);db.commit()
    assert db.scalar(select(func.count()).select_from(Ordinance))==1
    imported=db.scalar(select(Ordinance))
    assert imported.status=='unknown'
    assert db.scalar(select(OrdinanceLegalChunk)).embedding_status=='pending'
    imported.title='Revisión local';db.commit()
    assert plan_transfer(db,bundle)['counts']=={'conflict':1}
    with pytest.raises(ValueError,match='unresolved'):
        apply_transfer(db,bundle)
    db.rollback()
    assert db.scalar(select(Ordinance)).title=='Revisión local'
    bundle['records'][0]['ordinance']['title']='Alteración'
    with pytest.raises(ValueError,match='checksum'):
        plan_transfer(db,bundle)


@pytest.mark.parametrize("change", ["missing", "text", "review"])
def test_transfer_detects_changed_or_missing_destination_chunks(db, change):
    municipality = Municipality(autonomous_community="Castilla y León", name="Demo", province="Burgos", ine_code="09001")
    source = OfficialLegalSource(name="BOP", base_url="https://bop.example.es", domain="bop.example.es", source_type="bop")
    db.add_all([municipality, source]); db.flush()
    ordinance = Ordinance(municipality_id=municipality.id, title="Ordenanza", topic="agua", ordinance_type="ordinance",
        source_url="https://bop.example.es/1.pdf", curation_status="approved", status="unknown", text_content="Texto sintético")
    db.add(ordinance); db.flush()
    chunk = OrdinanceLegalChunk(ordinance_id=ordinance.id, chunk_index=0, text="Texto sintético", review_status="approved")
    db.add(chunk); db.commit()
    bundle = export_bundle(db)
    if change == "missing": db.delete(chunk)
    elif change == "text": chunk.text = "Revisión local del fragmento"
    else: chunk.review_status = "pending_review"
    db.commit(); db.expire_all()
    assert plan_transfer(db, bundle)["counts"] == {"conflict": 1}
    with pytest.raises(ValueError, match="unresolved"):
        apply_transfer(db, bundle)
    db.rollback()
