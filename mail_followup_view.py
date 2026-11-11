"""Account-bound history, quotation and reply actions for the contact workspace."""
from copy import deepcopy
from datetime import datetime
from io import BytesIO
import hashlib
import json
from threading import Lock
from uuid import uuid4

import pandas as pd
import streamlit as st

import email_module
from mail_accounts import get_store, AccountError
from template_store import get_template_store, TemplateError
from mail_compose_view import text, render_resend_selection
from negotiation_view import render_quote_selection, markdown_text, clear_negotiation_draft
from reminder_center import reply_worker, sync_caption
from workbench_io import csv_transaction, write_csv_atomic, read_csv_file
from cooperation_history_view import render_cooperation_reference

_REPLY_SUBMISSION_LOCK = Lock()
_SUBMITTED_REPLY_IDS = set()


def source_fingerprint(row):
    fields = ('邮箱', '合作产品', '合作类型', '回信次数', '最近回信Message-ID',
              '对方报价', '报价币种', '报价明细', '最近回信时间', '回信原文')
    data = {field: text(row.get(field, '')) for field in fields}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def prepare_reply(context, account_id, purpose, amount, account_store=None, template_store=None):
    """Resolve the exact source account/product and freeze a reply without connecting."""
    accounts, templates = account_store or get_store(), template_store or get_template_store()
    row = dict(context['row'])
    source_account = text(row.get('account_id')) or text(row.get('发件账号ID'))
    if not account_id or not source_account or source_account != account_id:
        raise ValueError('这条邀约不属于当前邮箱，请切换到原发件账号。')
    account = accounts.get_account(account_id)
    if not account or not account['enabled'] or not account['smtp_ready']:
        raise ValueError('原发件邮箱未启用或尚未通过发信验证。')
    product = text(row.get('合作产品'))
    form = text(context['form'])
    currency = text(context.get('currency'))
    if currency not in ('USD', 'EUR', 'GBP', 'CAD', 'AUD', 'INR', 'JPY', 'KRW', 'TWD', 'CNY'):
        raise ValueError('请先确认达人报价的币种。')
    if amount is None or float(amount) <= 0:
        raise ValueError('请填写本次准备对外提出的金额。')
    video = next((text(row.get(c)) for c in ('代表视频链接', '视频链接', '链接') if text(row.get(c))), '')
    rendered = templates.render(product, form, purpose=purpose, values={
        '达人名':context['name'], '发件人姓名':account.get('sender_name') or account['name'],
        '发件邮箱':account['email'], '产品名':product, '合作形式':form,
        '视频链接':video, '对外金额':amount, '币种':currency})
    parent = text(row.get('最近回信Message-ID')) or text(row.get('邮件Message-ID'))
    references = ' '.join(dict.fromkeys((text(row.get('References'))+' '+text(row.get('邮件Message-ID'))+' '+parent).split()))
    return {'account_id':account_id, 'account_revision':account['revision'], 'from_email':account['email'],
            'sender_name':account.get('sender_name') or account['name'], 'cc':list(account.get('cc') or []),
            'name':context['name'], 'to_email':email_module.clean_email(context['email']),
            'product':product, 'coop_type':form, 'purpose':purpose, 'amount':float(amount), 'currency':currency,
            'subject':rendered['subject'], 'body':rendered['body'], 'template_version':rendered['version'],
            'template_key':rendered['template_key'], 'source_key':email_module._invitation_row_key(row, account_id),
            'source_fingerprint':source_fingerprint(row), 'seen_count':row.get('回信次数',0),
            'invitation_id':text(row.get('邀约ID')) or str(uuid4()), 'contact_id':text(row.get('联系人ID')),
            'message_id':'<'+str(uuid4())+'@'+account['email'].rsplit('@',1)[-1]+'>',
            'in_reply_to':parent, 'references':references, 'submitted':False}


def validate_reply(draft, account_id, current_rows, account_store=None, template_store=None):
    accounts, templates = account_store or get_store(), template_store or get_template_store()
    if draft.get('submitted'):
        raise ValueError('此回复已提交，请先检查发送记录，不要重复发送。')
    if draft['account_id'] != account_id:
        raise ValueError('当前邮箱已经改变，请重新选择原邀约并生成回复。')
    account = accounts.get_account(account_id)
    if (not account or not account['enabled'] or not account['smtp_ready']
            or account['revision'] != draft['account_revision'] or account['email'] != draft['from_email']):
        raise ValueError('发件设置已经改变，请重新生成回复。')
    template = templates.get_template(draft['product'],draft['coop_type'],purpose=draft['purpose'])
    if not template or not template.get('active') or template['version'] != draft['template_version']:
        raise ValueError('模板已修改，请重新生成回复。')
    rows = current_rows.to_dict('records') if isinstance(current_rows,pd.DataFrame) else current_rows
    source = next((r for r in rows if email_module._invitation_row_key(r,account_id)==draft['source_key']),None)
    if source is None or source_fingerprint(source)!=draft['source_fingerprint']:
        raise ValueError('这条邀约或最新回信、报价已经改变，请刷新后重新核对。')
    if not draft.get('subject','').strip() or not draft.get('body','').strip():
        raise ValueError('邮件主题和正文不能为空。')
    return True


def send_reply(draft, send=None, mark=None):
    # A copied confirmation is still the same submission. Claim its stable
    # account/message identity before any SMTP work, including uncertain sends.
    with _REPLY_SUBMISSION_LOCK:
        identity = (text(draft.get('account_id')), text(draft.get('message_id')))
        if not all(identity):
            raise ValueError('回复缺少提交身份，请重新生成预览。')
        if draft.get('submitted') or identity in _SUBMITTED_REPLY_IDS:
            raise ValueError('这封回复已提交，请勿重复发送。')
        draft['submitted']=True
        _SUBMITTED_REPLY_IDS.add(identity)
    send, mark = send or email_module.send_and_log, mark or email_module.mark_replies_handled
    receipt=send(draft['to_email'],draft['subject'],draft['body'], account_id=draft['account_id'],
        name=draft['name'],product=draft['product'],coop_type=draft['coop_type'],
        contact_id=draft['contact_id'],invitation_id=draft['invitation_id'],message_id=draft['message_id'],
        template_version=draft['template_version'],template_key=draft['template_key'],purpose=draft['purpose'],
        in_reply_to=draft['in_reply_to'],references=draft['references'],cc=draft['cc'],
        expected_account_revision=draft['account_revision'])
    if not isinstance(receipt,dict) or receipt.get('status')!='成功':
        raise ValueError('发送结果尚未确认，请先核对所选邮箱的记录，不要重复发送。')
    expected={'account_id':draft['account_id'],'from_email':draft['from_email'],
              'to_email':draft['to_email'],'message_id':draft['message_id'],'invitation_id':draft['invitation_id']}
    if any(text(receipt.get(key))!=text(value) for key,value in expected.items()):
        raise ValueError('发送回执与预览不一致，请先核对邮箱和记录，未标记原邀约已回复。')
    try:
        mark([draft['source_key']],status='已回复',seen_counts={draft['source_key']:draft['seen_count']},
             account_id=draft['account_id'])
        draft['handled']=True
    except Exception:
        draft['handled']=False
    return receipt


def _download(log, account_id):
    if log.empty:return
    buf=BytesIO();log.to_excel(buf,index=False,engine='openpyxl')
    st.download_button('下载邮件记录 Excel',data=buf.getvalue(),file_name='邮件记录_'+datetime.now().strftime('%Y%m%d')+'.xlsx',
        mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',key='mail_download_'+str(account_id))


def _save_edits(before, edited, account_id):
    conflicts=0
    path=email_module.mail_log_path(account_id)
    handled=[]
    with csv_transaction(path):
        latest=email_module.load_mail_log(account_id)
        for field in ('对方报价','回信状态','回信次数'):
            if field not in latest:latest[field]=''
        keys=latest.apply(lambda r:email_module._invitation_row_key(r,account_id),axis=1)
        for i,old in before.iterrows():
            row_key=email_module._invitation_row_key(old,account_id)
            indices=latest.index[keys==row_key].tolist()
            if len(indices)!=1:conflicts+=1;continue
            target=indices[0]
            for field in ('对方报价','回信状态'):
                old_value,new_value=text(old.get(field)),text(edited.at[i,field])
                if old_value==new_value:continue
                if text(latest.at[target,field])!=old_value:
                    conflicts+=1;continue
                if field=='回信状态' and new_value in email_module.HANDLED_REPLY_STATUSES:
                    handled.append((row_key,new_value,old.get('回信次数',0)))
                elif field=='回信状态' and text(latest.at[target,'回信次数'])!=text(old.get('回信次数')):
                    conflicts+=1
                else:latest.at[target,field]=new_value
        write_csv_atomic(latest,path)
    for row_key,status,count in handled:
        email_module.mark_replies_handled([row_key],status=status,seen_counts={row_key:count},account_id=account_id)
    return conflicts


def render_mail_history(account_id):
    st.subheader('发送记录')
    log=email_module.load_mail_log(account_id)
    if not account_id:
        st.info('历史邮件完整保留，目前只读。请在“管理发件账号”连接原邮箱，验证后确认历史归属；确认前不会同步或发送。')
        if not log.empty:st.dataframe(log,hide_index=True,use_container_width=True)
        _download(log,'legacy');return log
    sync_caption(account_id)
    a,b=st.columns(2)
    if a.button('同步回信',key='sync_reply_btn',use_container_width=True,
                disabled=reply_worker(account_id).status().get('read_only',False)):
        reply_worker(account_id).start(force=True);st.info('正在后台检查此邮箱的回信，完成后刷新列表。')
    b.button('刷新记录与报价',key='negotiation_refresh_view',use_container_width=True)
    if log.empty:st.info('此邮箱还没有发送记录。');return log
    replied=log.get('回信状态',pd.Series('',index=log.index)).fillna('').astype(str).str.startswith('已回信').sum()
    st.caption('当前邮箱共 '+str(len(log))+' 条发送记录；已回信 '+str(replied)+' 条。')
    shown=log.copy()
    for field in ('对方报价','回信状态'):
        if field not in shown:shown[field]=''
    shown.insert(0,'重发',False)
    edited=st.data_editor(shown,hide_index=True,use_container_width=True,key='mail_editor_'+account_id,
        disabled=[c for c in shown if c not in ('重发','对方报价','回信状态')],
        column_config={'重发':st.column_config.CheckboxColumn(), '邮件正文':None,'模板键':None,
                       'account_id':None,'发件账号ID':None})
    if st.button('保存报价/状态修改',key='mail_save_edit'):
        conflicts=_save_edits(shown,edited,account_id)
        if conflicts:st.warning('部分记录已有新变化，已保留最新值，请刷新核对。')
        else:st.success('当前邮箱的报价和状态已保存。')
    render_resend_selection(edited[edited['重发'].fillna(False).astype(bool)],account_id)
    _download(log,account_id)
    return log


def render_negotiation(log,account_id,load_history,load_library,load_effect,save_effect,refresh_metrics=None,cooperation_records=None):
    st.subheader('💰 议价')
    if not account_id:
        st.caption('确认历史归属后，这里的报价、议价和回复将跟随所选邮箱。');return
    context=render_quote_selection(log,load_history(),load_library(),refresh_metrics=refresh_metrics)
    if context is None:return
    row=context['row'];evaluation=context['evaluation']
    render_cooperation_reference(row,cooperation_records() if cooperation_records else read_csv_file("合作效果.csv"),load_history(),load_library(),
        product=text(row.get('合作产品')),form=context['form'],
        context_key='negotiation_cooperation_'+context['id'])
    st.info(markdown_text(evaluation['说明']))
    suggested=evaluation.get('最佳价格')
    if suggested is not None:st.caption('内部建议价：'+context['currency']+' '+str(suggested)+'；不会自动发送。')
    amount=st.number_input('本次对外提出的金额（'+(context['currency'] or '币种待确认')+'）',min_value=0.0,
        value=float(suggested if suggested is not None else context['offer'] or 0),step=5.0,
        key='nego_amount_'+context['id'])
    st.caption('可以按对方报价接受，也可以填写你决定对外提出的金额后准备议价邮件。')
    a,b=st.columns(2)
    purpose=None
    if a.button('接受（按对方报价）',key='nego_accept',disabled=not context['can_accept']):purpose='接受报价'
    if b.button('准备议价回复',key='nego_bargain',disabled=amount<=0 or not context.get('currency')):purpose='议价回复'
    if purpose:
        clear_negotiation_draft()
        try:
            st.session_state['nego_draft']=prepare_reply(context,account_id,purpose,
                context['offer'] if purpose=='接受报价' else amount)
        except (ValueError,TemplateError,AccountError) as error:st.error(str(error))
    draft=st.session_state.get('nego_draft')
    if draft and draft['account_id']==account_id:
        with st.container(border=True):
            st.markdown('**回复预览 · 发件邮箱已锁定为原邀约账号**')
            st.caption(draft['sender_name']+' <'+draft['from_email']+'> → '+draft['to_email'])
            st.caption(draft['product']+' · '+draft['coop_type']+' · '+draft['purpose'])
            if not draft.get('submitted'):
                subject=st.text_input('主题',value=draft['subject'],key='nego_subject_edit')
                body=st.text_area('正文',value=draft['body'],height=280,key='nego_body_edit')
                if st.button('核对并准备发送',key='nego_send_btn'):
                    frozen=deepcopy(draft);frozen['subject']=subject;frozen['body']=body
                    try:
                        validate_reply(frozen,account_id,email_module.load_mail_log(account_id))
                        st.session_state['nego_confirmation']=frozen
                    except (ValueError,TemplateError,AccountError) as error:st.error(str(error))
                confirmed=st.session_state.get('nego_confirmation')
                if confirmed:
                    if confirmed['subject']!=subject or confirmed['body']!=body:
                        st.session_state.pop('nego_confirmation',None)
                        st.caption('内容已变化，请重新核对。')
                    else:
                        st.warning('确认后将使用 '+confirmed['from_email']+' 回复 '+confirmed['to_email'])
                        if st.button('确认发送此回复',key='nego_commit',type='primary'):
                            try:
                                validate_reply(confirmed,account_id,email_module.load_mail_log(account_id))
                                send_reply(confirmed)
                                draft['submitted']=True
                                st.success('回复已发送，已保存此邮箱的邮件记录。')
                                if not confirmed.get('handled'):st.warning('处理状态未保存，请手动核对待办。')
                            except (ValueError,TemplateError,AccountError) as error:st.error(str(error))
                            except Exception:
                                draft['submitted']=True
                                st.warning('发送未完成或结果待核，请先检查此邮箱发件箱和记录，不要直接重发。')
                            if confirmed.get('submitted'):draft['submitted']=True
                            st.session_state.pop('nego_confirmation',None)
            else:st.info('此回复已提交；请检查发送记录。')
    with st.expander('记录已达成的合作',expanded=False):
        st.caption('合作效果与交付记录共用，保存时附上本次发件邮箱和原邀约，方便追溯。')
        agreed=st.number_input('实际合作金额（USD）',min_value=0.0,value=float(context['offer'] or 0),
            key='coop_amount_'+context['id'])
        date=st.date_input('合作日期',value=datetime.now().date(),key='coop_date_'+context['id'])
        due=st.date_input('约定交付日期（未约定可留空）',value=None,key='coop_due_'+context['id'])
        if st.button('标记已合作',key='mark_coop',disabled=agreed<=0 or context['currency']!='USD'):
            source_key=email_module._invitation_row_key(row,account_id)
            effects=load_effect()
            same=(effects.get('原邀约记录键',pd.Series('',index=effects.index)).fillna('')==source_key)
            if same.any():st.info('这条邀约已记录合作；修改金额或交付请到数据分析。')
            else:
                account=get_store().get_account(account_id)
                new={'合作ID':str(uuid4()),'达人名':context['name'],'邮箱':context['email'],
                     '合作产品':text(row.get('合作产品')),'合作类型':context['form'],
                     '合作日期':str(date),'合作金额':agreed,'约定交付日期':str(due) if due else '',
                     '交付状态':'待交付','发件账号ID':account_id,'实际发件地址':account['email'],
                     '邀约ID':text(row.get('邀约ID')),'原邀约记录键':source_key,
                     '记录时间':datetime.now().strftime('%Y-%m-%d %H:%M')}
                save_effect(pd.concat([effects,pd.DataFrame([new])],ignore_index=True))
                email_module.mark_replies_handled([source_key],status='已合作',
                    seen_counts={source_key:row.get('回信次数',0)},account_id=account_id)
                st.success('合作已记录，交付提醒保留。')


def render_followup(account_id,load_history,load_library,load_effect,save_effect,refresh_metrics=None,cooperation_records=None):
    log=render_mail_history(account_id)
    render_negotiation(log,account_id,load_history,load_library,load_effect,save_effect,refresh_metrics,cooperation_records)
