"""Explicit recipient selection and immutable, account-bound mail preparation."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
from threading import Lock
from uuid import uuid4

import pandas as pd
import streamlit as st
from resource_table_style import style_creator_names

import email_module
from mail_accounts import get_store, current_account_id, AccountError
from template_store import get_template_store, TemplateError

_SUBMISSION_LOCK = Lock()
_SUBMITTED_BATCHES = set()


def text(value):
    value = str(value or '').strip()
    return '' if value.lower() in ('nan', 'none', '<na>') else value


def _identity(row):
    fields = ('联系人ID', '频道名称', '邮箱', '主页链接', '合作产品', '合作类型',
              '代表视频链接', '视频链接', '链接')
    return {field: text(row.get(field, '')) for field in fields}


def _fingerprint(row):
    return hashlib.sha256(json.dumps(_identity(row), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def selection_signature(rows):
    rows = rows.to_dict('records') if isinstance(rows, pd.DataFrame) else list(rows)
    return tuple(sorted(_fingerprint(row) for row in rows))


def _invalidate_compose_selection():
    for key in ('_compose_selection', '_compose_batch', '_compose_options', 'compose_sender',
                'compose_repeat', 'compose_preview_person'):
        st.session_state.pop(key, None)


def _protected_shared_columns(columns):
    fixed = {'发送状态', '对方报价', '回信次数', '回信状态', '已处理回信次数', '回信处理时间'}
    return [column for column in columns if column in fixed or str(column).startswith(('报价', '最近回信', '邮件Message'))]


def account_contact_statuses(contacts, mail_log):
    """Only exact contact + product + form links; never join by creator email."""
    rows = mail_log.to_dict('records') if isinstance(mail_log, pd.DataFrame) else list(mail_log)
    latest = {}
    for index, row in enumerate(rows):
        key = tuple(text(row.get(field)) for field in ('联系人ID', '合作产品', '合作类型'))
        if not all(key):
            continue
        try:
            stamp = datetime.fromisoformat(text(row.get('发送时间')).replace('Z', '+00:00'))
            stamp = (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp()
        except (ValueError, TypeError, OverflowError):
            stamp = float('-inf')
        if key not in latest or (stamp, index) > latest[key][0]:
            status = ' · '.join(text(row.get(field)) for field in ('状态', '回信状态') if text(row.get(field)))
            latest[key] = ((stamp, index), status or '已有记录，状态待核')
    records = contacts.to_dict('records') if isinstance(contacts, pd.DataFrame) else list(contacts)
    return [latest.get(tuple(text(row.get(field)) for field in ('联系人ID', '合作产品', '合作类型')),
                       (None, '暂无精确关联记录'))[1] for row in records]


def prepare_batch(rows, account_id, account_store=None, template_store=None, form_override='',
                  purpose='首次邀约', reuse_snapshot=False):
    """No network: freeze identities, exact templates and rendered messages."""
    accounts = account_store or get_store()
    templates = template_store or get_template_store()
    account = accounts.get_account(account_id)
    if not account or not account.get('enabled') or not account.get('smtp_ready'):
        raise ValueError('请先选择已启用且通过发信验证的邮箱。')
    rows = rows.to_dict('records') if isinstance(rows, pd.DataFrame) else list(rows)
    batch_id, valid, blocked, seen = str(uuid4()), [], [], set()
    sender = account.get('sender_name') or account.get('name') or ''
    for source in rows:
        source = dict(source)
        contact_id = text(source.get('联系人ID'))
        name = text(source.get('频道名称')) or 'there'
        product = text(source.get('合作产品'))
        form = form_override or text(source.get('合作类型'))
        address = email_module.clean_email(text(source.get('邮箱')))
        identity = contact_id or text(source.get('发送记录ID')) or text(source.get('邀约ID')) or str(uuid4())
        token = (identity, product, form)
        if token in seen:
            continue
        seen.add(token)
        reason = ''
        row_account = text(source.get('account_id') or source.get('发件账号ID'))
        if reuse_snapshot and row_account != account_id:
            reason = '原邮件账号归属不明确或不属于所选账号，不能跨账号重发'
        elif not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', address):
            reason = '邮箱待补充或格式不正确'
        elif not product or not form or form == '议价回复':
            reason = '合作产品或本轮合作形式待确认'
        video = next((text(source.get(c)) for c in ('代表视频链接', '视频链接', '链接')
                      if text(source.get(c))), '')
        rendered = None
        if not reason:
            try:
                if reuse_snapshot and text(source.get('邮件主题')) and text(source.get('邮件正文')):
                    rendered = {'subject': text(source['邮件主题']), 'body': str(source['邮件正文']),
                                'version': source.get('模板版本', ''), 'template_key': source.get('模板键', ''),
                                'snapshot': True}
                else:
                    rendered = templates.render(product, form, purpose=purpose, values={
                        '达人名': name, '视频链接': video, '发件人姓名': sender,
                        '发件邮箱': account['email'], '产品名': product, '合作形式': form})
            except (TemplateError, ValueError) as error:
                reason = str(error)
        if reason:
            blocked.append({'达人': name, '产品': product, '合作形式': form, '原因': reason})
            continue
        valid.append({'contact_id': contact_id, 'name': name, 'to_email': address, 'product': product,
                      'coop_type': form, 'subject': rendered['subject'], 'body': rendered['body'],
                      'template_version': rendered.get('version', ''), 'template_key': rendered.get('template_key', ''),
                      'snapshot': bool(rendered.get('snapshot')), 'source': _identity(source),
                      'source_fingerprint': _fingerprint(source),
                      'invitation_id': str(uuid4()), 'message_id': '<'+str(uuid4())+'@'+account['email'].rsplit('@',1)[-1]+'>',
                      'original_invitation_id': text(source.get('邀约ID'))})
    return {'id': batch_id, 'account_id': account_id, 'account_revision': account.get('revision'),
            'from_email': account['email'], 'sender_name': sender, 'cc': list(account.get('cc') or []),
            'prepared_at': datetime.now(timezone.utc).isoformat(), 'rows': valid, 'blocked': blocked,
            'purpose': purpose, 'submitted': False, 'selection_signature': selection_signature(rows)}


def validate_batch(batch, current_contacts=None, account_store=None, template_store=None):
    """Revalidate before commit; submitted messages always use the frozen snapshot."""
    accounts, templates = account_store or get_store(), template_store or get_template_store()
    if batch.get('submitted'):
        raise ValueError('本批邮件已提交，请查看发送记录，不要重复发送。')
    account = accounts.get_account(batch['account_id'])
    if (not account or not account.get('enabled') or not account.get('smtp_ready')
            or account.get('revision') != batch['account_revision'] or account['email'] != batch['from_email']):
        raise ValueError('发件账号设置或验证状态已变化，请重新生成预览。')
    latest = None
    if current_contacts is not None:
        current_contacts = current_contacts() if callable(current_contacts) else current_contacts
        current = current_contacts.to_dict('records') if isinstance(current_contacts,pd.DataFrame) else current_contacts
        latest = {text(row.get('联系人ID')): row for row in current}
    for row in batch['rows']:
        if latest is not None and row['contact_id']:
            found = latest.get(row['contact_id'])
            if found is None or _fingerprint(found) != row['source_fingerprint']:
                raise ValueError('勾选达人的资料已变化，请刷新名单并重新预览。')
        if not row.get('snapshot'):
            template = templates.get_template(row['product'],row['coop_type'],purpose=batch['purpose'])
            if (not template or not template.get('active', True)
                    or template['version'] != row['template_version']
                    or template.get('template_key', '') != row['template_key']):
                raise ValueError('邮件模板已变化，请重新生成预览。')
    return True


def _receipt_matches(receipt, batch, row):
    return (isinstance(receipt, dict) and receipt.get('account_id') == batch['account_id']
            and text(receipt.get('to_email')).casefold() == row['to_email'].casefold()
            and receipt.get('from_email') == batch['from_email']
            and receipt.get('message_id') == row['message_id']
            and receipt.get('invitation_id') == row['invitation_id']
            and str(receipt.get('account_revision', '')) == str(batch['account_revision'])
            and str(receipt.get('template_version', '')) == str(row['template_version'])
            and receipt.get('template_key', '') == row['template_key']
            and list(receipt.get('cc') or []) == batch['cc'])


def _result(name, status, message):
    return {'达人': name, '结果': status, '说明': message}


def execute_batch(batch, send=None, current_contacts=None, account_store=None, template_store=None):
    """Call only after explicit user confirmation; never use a page's active mailbox."""
    with _SUBMISSION_LOCK:
        identity = (batch.get('account_id'), batch.get('id'))
        if batch.get('submitted') or identity in _SUBMITTED_BATCHES:
            raise ValueError('本批已经提交，不能重复发送。')
        if not batch.get('id') or not batch.get('rows'):
            raise ValueError('本批没有可发送的邮件。')
        validate_batch(batch, current_contacts, account_store, template_store)
        batch['submitted'] = True
        _SUBMITTED_BATCHES.add(identity)
    send = send or email_module.send_and_log
    results = []
    frozen_rows = deepcopy(batch['rows'])
    for index, row in enumerate(frozen_rows):
        try:
            # Each message checks live metadata again; the SMTP boundary also
            # checks expected_account_revision against its own credentials read.
            validate_batch(dict(batch, submitted=False, rows=[row]), current_contacts,
                           account_store, template_store)
        except (ValueError, TemplateError, AccountError):
            results.extend(_result(remaining['name'], '未发送', '账号、模板或达人资料已变化，请重新生成预览。')
                           for remaining in frozen_rows[index:])
            break
        try:
            receipt = send(row['to_email'],row['subject'],row['body'],account_id=batch['account_id'],
                 name=row['name'],product=row['product'],coop_type=row['coop_type'],
                 contact_id=row['contact_id'],invitation_id=row['invitation_id'],
                 message_id=row['message_id'],template_version=row['template_version'],
                 template_key=row['template_key'],purpose=batch['purpose'],cc=list(batch['cc']),
                 expected_account_revision=batch['account_revision'])
            if _receipt_matches(receipt, batch, row) and receipt.get('status') == '成功':
                results.append(_result(row['name'], '成功', '已保存到本批发件账号的发送记录。'))
            elif _receipt_matches(receipt, batch, row) and receipt.get('status') == '失败':
                results.append(_result(row['name'], '失败', '没有确认发送成功，请查看所选邮箱的记录。'))
            else:
                results.append(_result(row['name'], '发送待核', '尚未取得一致的成功回执，请先核对该邮箱发件箱与发送记录，不要直接重发。'))
                if not _receipt_matches(receipt, batch, row):
                    results.extend(_result(remaining['name'], '未发送', '前一封回执无法核对，已停止本批后续发送。')
                                   for remaining in frozen_rows[index + 1:])
                    break
        except Exception as error:
            # SMTP may have accepted a message before a connection/log failure.
            receipt = getattr(error, 'send_result', None)
            status = '失败' if _receipt_matches(receipt, batch, row) and receipt.get('status') == '失败' else '发送待核'
            results.append(_result(row['name'], status, '请核对所选邮箱发件箱与发送记录；本批不会自动重试。'))
    batch['results'] = results
    return results


def _render_batch(scope, batch, load_library=None, require_recontact=False):
    st.caption('发件人：'+batch['sender_name']+' <'+batch['from_email']+'>')
    st.caption('抄送：'+(', '.join(batch['cc']) if batch['cc'] else '无'))
    if batch['blocked']:
        st.warning(f"{len(batch['blocked'])} 条未就绪，本次不会发送；资料仍保留。")
        st.dataframe(pd.DataFrame(batch['blocked']),hide_index=True,use_container_width=True)
    if not batch['rows']:
        return
    summary = [{'达人':r['name'],'收件邮箱':r['to_email'],'产品':r['product'],'合作形式':r['coop_type'],
                '主题':r['subject'],'模板版本':str(r['template_version'])} for r in batch['rows']]
    st.dataframe(pd.DataFrame(summary),hide_index=True,use_container_width=True)
    chosen = st.selectbox('逐封检查主题和正文',range(len(batch['rows'])),
                          format_func=lambda i:batch['rows'][i]['name']+' · '+batch['rows'][i]['product'],
                          key=scope+'_preview_person')
    st.text(batch['rows'][chosen]['subject'])
    st.text(batch['rows'][chosen]['body'])
    repeated = True
    if require_recontact:
        repeated = st.checkbox('我已核对已有联系记录，本次确认为新的合作或跟进',key=scope+'_repeat')
    if batch.get('submitted'):
        st.info('本批已执行；不自动重发未完成项。')
        st.dataframe(pd.DataFrame(batch.get('results',[])),hide_index=True,use_container_width=True)
        if st.button('查看该邮箱的发送记录',key=scope+'_view_records'):
            st.session_state['_pending_mail_active'] = batch['account_id']
            st.session_state['_mail_follow_notice'] = '已切换到本批实际发件账号，请打开“邮件跟进”。'
            st.rerun()
        return
    if st.button('确认发送 '+str(len(batch['rows']))+' 封',type='primary',key=scope+'_commit',disabled=not repeated):
        try:
            with st.spinner('正在使用已确认的发件账号发送...'):
                results=execute_batch(batch,current_contacts=load_library)
        except (ValueError,TemplateError,AccountError) as error:
            st.error(str(error));return
        st.dataframe(pd.DataFrame(results),hide_index=True,use_container_width=True)
        if all(result['结果'] == '成功' for result in results):
            st.success('本批邮件已发送并保存记录。')
        else:
            st.warning('本批包含未发送、失败或待核项，请按结果核对；不会自动重试。')


def render_compose(selected, load_library):
    accounts = get_store()
    usable = [a for a in accounts.list_accounts() if a['enabled'] and a['smtp_ready']]
    st.markdown('**为本轮所选达人准备邮件**')
    st.caption('已选 '+str(len(selected))+' 名；接下来选择本次发件邮箱，逐封核对后才发送。')
    if not usable:
        st.info('请先在“管理发件账号”添加并验证邮箱。已选达人仍保留。');return
    by_id={a['id']:a for a in usable}
    if st.session_state.get('compose_sender') not in [None]+list(by_id):
        st.session_state.pop('compose_sender',None)
    sender=st.selectbox('本次发件邮箱',list(by_id),index=None,placeholder='请选择本次发件邮箱',
                        format_func=lambda x:by_id[x]['name']+' · '+by_id[x]['email'],key='compose_sender')
    form=st.selectbox('本轮合作形式',['使用达人已保存的形式']+email_module.COOP_TYPES,key='compose_form')
    signature=(sender,form,selection_signature(selected))
    if st.session_state.get('_compose_options')!=signature:
        st.session_state['_compose_options']=signature
        st.session_state.pop('_compose_batch',None)
        st.session_state.pop('compose_repeat',None)
    history=False
    if sender:
        try:
            log=email_module.load_mail_log(sender)
        except (ValueError,AccountError):
            st.warning('所选邮箱的往来记录暂时无法核对，请先检查发送记录，再准备本轮邮件。')
            st.session_state.pop('_compose_batch',None)
            return
        if not log.empty:
            emails={text(r.get('邮箱')).lower() for r in selected}
            history=log.get('邮箱',pd.Series(dtype=str)).astype(str).str.lower().isin(emails).any()
        if history:st.warning('所选达人与这个邮箱已有往来，请核对是否为本轮再次合作。')
    if st.button('生成邮件预览',key='compose_preview',disabled=sender is None):
        try:
            st.session_state['_compose_batch']=prepare_batch(selected,sender,
                form_override='' if form=='使用达人已保存的形式' else form)
        except (ValueError,TemplateError,AccountError) as error:st.error(str(error))
    batch=st.session_state.get('_compose_batch')
    if batch:
        _render_batch('compose',batch,load_library=load_library,require_recontact=bool(history))


def render_contact_workspace(load_library, save_library_full, save_to_library, active_account_id=None):
    """Shared contact resources; progress is read from the selected mailbox's log."""
    templates=get_template_store()
    st.caption('达人资料共用，邮件往来按发件邮箱分别保留。先勾选达人，再选择本次发件邮箱。')
    with st.expander('手动添加达人',expanded=False):
        manual=st.data_editor(pd.DataFrame([{'频道名称':'','主页链接':'','邮箱':'',
            '合作产品':templates.products()[0] if templates.products() else '', '合作类型':'加链接'}]),
            num_rows='dynamic',key='recipient_manual',hide_index=True,use_container_width=True,
            column_config={'合作产品':st.column_config.SelectboxColumn(options=templates.products()),
                           '合作类型':st.column_config.SelectboxColumn(options=email_module.COOP_TYPES)})
        if st.button('加入联系人名单',key='recipient_manual_add'):
            valid=manual[manual['频道名称'].fillna('').str.strip().ne('') | manual['主页链接'].fillna('').str.strip().ne('')]
            if valid.empty:st.info('请填写频道名称或主页。')
            else:
                save_to_library(valid)
                st.session_state.pop('recipient_snapshot',None)
                _invalidate_compose_selection()
                st.rerun()
    if st.button('刷新联系人名单',key='recipient_refresh'):
        st.session_state.pop('recipient_snapshot',None)
        _invalidate_compose_selection()
        st.session_state['recipient_version']=st.session_state.get('recipient_version',0)+1
    if 'recipient_snapshot' not in st.session_state:
        st.session_state['recipient_snapshot']=load_library().copy(deep=True)
    original=st.session_state['recipient_snapshot'].copy(deep=True)
    if original.empty:
        st.info('联系人名单为空，可从找达人或达人自动推送加入。');return
    columns=[c for c in ['联系人ID','频道名称','主页链接','代表视频链接','邮箱','合作产品','合作类型',
                         '国家','地区','近6条均播','平均播放量','VPH','备注'] if c in original]
    shown=original[columns].copy();shown.insert(0,'选择',False)
    active_account_id = active_account_id or current_account_id()
    status_column = '当前查看账号 · 最近往来'
    if active_account_id:
        try:
            shown[status_column] = account_contact_statuses(original, email_module.load_mail_log(active_account_id))
            st.caption('最近往来来自当前查看账号；不改变接下来为本轮选择的发件邮箱。')
        except (ValueError, AccountError):
            shown[status_column] = '当前账号记录读取未完成'
    else:
        shown[status_column] = '尚未选择查看账号'
    chosen=st.data_editor(style_creator_names(shown, "contacts"),hide_index=True,use_container_width=True,
        key='recipient_select_'+str(st.session_state.get('recipient_version',0)),
        disabled=columns+[status_column],column_config={'联系人ID':None,'选择':st.column_config.CheckboxColumn(),
            '主页链接':st.column_config.LinkColumn('主页',display_text='打开主页'),
            '代表视频链接':st.column_config.LinkColumn('视频',display_text='打开视频')})
    selected=original.loc[chosen['选择'].fillna(False).astype(bool)].copy()
    signature = selection_signature(selected)
    if st.session_state.get('_recipient_selection_signature') != signature:
        had_preview = bool(st.session_state.get('_compose_selection') or st.session_state.get('_compose_batch'))
        _invalidate_compose_selection()
        st.session_state['_recipient_selection_signature'] = signature
        if had_preview:
            st.info('勾选名单已变化，旧邮件预览已失效；请重新为当前勾选达人准备邮件。')
    st.caption('已勾选 '+str(len(selected))+' 名达人。名单保持当前快照，新增或修改资料后请刷新。')
    if st.button('为勾选达人准备邮件',key='recipient_prepare',type='primary',disabled=selected.empty):
        st.session_state['_compose_selection']=selected.to_dict('records')
        for key in ('_compose_batch','_compose_options','compose_sender','compose_repeat'):
            st.session_state.pop(key,None)
    if st.session_state.get('_compose_selection'):
        with st.container(border=True):render_compose(st.session_state['_compose_selection'],load_library)
    with st.expander('编辑达人资料和合作信息',expanded=False):
        st.caption('保留全部原字段。发送、回信和报价列来自旧共享表，仅供查看，不代表当前邮箱；新的邮件状态由各账号发送记录维护。')
        editable=original.copy(deep=True)
        editable['删除']=False
        protected = _protected_shared_columns(editable.columns)
        configs = {column: st.column_config.TextColumn('旧共享 · '+column+'（只读）') for column in protected}
        configs.update({'联系人ID':None,'合作产品':st.column_config.SelectboxColumn(options=list(dict.fromkeys(templates.products()+original.get('合作产品',pd.Series(dtype=str)).astype(str).tolist()))),
                '合作类型':st.column_config.SelectboxColumn(options=email_module.COOP_TYPES),
                '删除':st.column_config.CheckboxColumn()})
        edited=st.data_editor(editable,key='recipient_edit_'+str(st.session_state.get('recipient_version',0)),
            hide_index=True,use_container_width=True,disabled=['联系人ID']+protected,column_config=configs)
        confirm_delete=st.checkbox('如勾选删除，确认移除对应联系人资料；邮件历史仍保留',key='recipient_delete_confirm')
        if st.button('保存联系人修改',key='recipient_save'):
            if edited['删除'].fillna(False).any() and not confirm_delete:
                st.warning('请先确认删除勾选，或取消删除。')
            else:
                # Preserve protected legacy fields even if a stale/client editor
                # payload attempts to change a disabled column.
                safe_edited = edited.copy(deep=True)
                baseline = {text(row.get('联系人ID')): row for row in editable.to_dict('records')}
                for index, row in safe_edited.iterrows():
                    prior = baseline.get(text(row.get('联系人ID')))
                    if prior is not None:
                        for column in protected:
                            safe_edited.at[index, column] = prior[column]
                result=save_library_full(safe_edited,before=editable)
                st.session_state.pop('recipient_snapshot',None)
                _invalidate_compose_selection()
                st.session_state['recipient_version']=st.session_state.get('recipient_version',0)+1
                st.success('资料已保存，现有邮件和报价保留。')
                st.rerun()


def render_resend_selection(selected, account_id):
    """A resend is a new invitation within the original account, with its own preview."""
    if selected.empty:
        st.session_state.pop('_resend_batch',None)
        st.session_state.pop('_resend_options',None)
        return
    ids=[email_module._invitation_row_key(r) for _,r in selected.iterrows()]
    signature=(account_id,tuple(ids),selection_signature(selected))
    if st.session_state.get('_resend_options')!=signature:
        st.session_state['_resend_options']=signature
        st.session_state.pop('_resend_batch',None)
        st.session_state.pop('resend_confirm_repeat',None)
        st.session_state.pop('resend_confirm_preview_person',None)
    if st.button('为勾选记录准备重发',key='resend_prepare'):
        try:st.session_state['_resend_batch']=prepare_batch(selected,account_id,reuse_snapshot=True)
        except (ValueError,TemplateError,AccountError) as error:st.error(str(error))
    batch=st.session_state.get('_resend_batch')
    if batch:_render_batch('resend_confirm',batch,require_recontact=True)
