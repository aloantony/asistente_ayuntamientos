import json
import pytest
from conftest import headers_for
from sqlalchemy import select, func
from app.assistant import tools
from app.tasks.models import MunicipalTask, MunicipalTaskEvent
from test_assistant import arm_confirmed_conversation_mutation


def test_human_task_tool_confirm_execute_and_replay(db, make_user, make_organization, grant_permissions):
    user, org = make_user(), make_organization()
    grant_permissions(user, org, ['assistant.use','tasks.create','tasks.view'])
    payload = {'organization_id':org.id,'title':'Revisar alumbrado'}
    conversation,message,authorization=arm_confirmed_conversation_mutation(db,user,'create_municipal_task',payload)
    context=tools.ToolContext(conversation_id=conversation.id,user_message_id=message.id)
    first=tools.execute_tool(db,user,'create_municipal_task',payload,context,authorization=authorization)
    assert first.ok, first.content
    second=tools.execute_tool(db,user,'create_municipal_task',payload,context,authorization=authorization)
    assert first.content == second.content
    entity=json.loads(first.content)['entity_id']
    assert db.scalar(select(func.count()).select_from(MunicipalTask).where(MunicipalTask.id==entity))==1
    assert db.scalar(select(func.count()).select_from(MunicipalTaskEvent).where(MunicipalTaskEvent.task_id==entity))==1
    read=tools.execute_tool(db,user,'get_municipal_record',{'domain':'tasks','entity_id':entity})
    assert read.ok and json.loads(read.content)['events'][0]['event_type']=='created'
    listing=tools.execute_tool(db,user,'list_municipal_records',{'domain':'tasks','organization_id':org.id})
    assert listing.ok and json.loads(listing.content)['total']==1


def test_municipal_tools_cannot_read_another_tenant(db, make_user, make_organization, grant_permissions):
    user, own, other = make_user(), make_organization(), make_organization()
    grant_permissions(user,own,['assistant.use','tasks.view','tasks.create'])
    task=MunicipalTask(organization_id=other.id,title='Private task')
    db.add(task); db.commit()
    result=tools.execute_tool(db,user,'get_municipal_record',{'domain':'tasks','entity_id':task.id})
    assert not result.ok and 'Private task' not in result.content
    result=tools.execute_tool(db,user,'list_municipal_records',{'domain':'tasks','organization_id':other.id})
    assert not result.ok and 'Private task' not in result.content


def test_changed_record_invalidates_confirmed_edit(db, make_user, make_organization, grant_permissions):
    user,org=make_user(),make_organization()
    grant_permissions(user,org,['assistant.use','tasks.view','tasks.edit'])
    task=MunicipalTask(organization_id=org.id,title='Original')
    db.add(task);db.commit()
    payload={'entity_id':task.id,'changes':{'title':'Approved edit'}}
    conversation,message,authorization=arm_confirmed_conversation_mutation(db,user,'update_municipal_task',payload)
    task.title='Concurrent edit';db.commit()
    result=tools.execute_tool(db,user,'update_municipal_task',payload,
        tools.ToolContext(conversation_id=conversation.id,user_message_id=message.id),authorization=authorization)
    assert not result.ok
    db.refresh(task)
    assert task.title=='Concurrent edit'
