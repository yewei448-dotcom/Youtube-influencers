"""Presentation adapters for discovery decisions, with unchanged business CSVs."""
import hashlib
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from contact_store import canonical_channel
from resource_decisions import DecisionStore, channel_identity, dedupe_channels
from resource_review_view import TAG_OPTIONS, render_tag_actions, review_version
from cooperation_history import build_history
from resource_views import render_resource_views
from cooperation_history_view import render_cooperation_reference

TAG_COLUMN = "__wb_review_tag"
TAG_PLACEHOLDER = "选择标签"
LABEL_COLUMN = "__wb_review_labels"
COOP_COLUMN = "__wb_cooperation_summary"
DERIVED_COLUMNS = (TAG_COLUMN, LABEL_COLUMN, COOP_COLUMN)


def text(value):
    if value is None:return ""
    try:
        if pd.isna(value):return ""
    except (TypeError,ValueError):pass
    return str(value).strip()


def decision_store():
    return DecisionStore(root=Path.cwd())


def context(product, form, english_only=None, account_id=None):
    return {"product":product, "coop_type":form, "language":"en" if english_only else "",
            "account_id":account_id or ""}


def editor_suffix(frame, ctx, prefix):
    ids=[channel_identity(row) or text(row.get("主页链接") or row.get("频道链接")) or str(i)
         for i,row in frame.iterrows()]
    identity=json.dumps([ctx,ids],ensure_ascii=False,sort_keys=True)
    return str(review_version(prefix))+"_"+hashlib.sha256(identity.encode()).hexdigest()[:12]


def clean_review_columns(frame):
    return frame.drop(columns=[c for c in DERIVED_COLUMNS if c in frame],errors="ignore").copy()


def decorate(frame, ctx, cooperation=None):
    result=frame.copy()
    store=decision_store()
    by_channel={}
    for record in store.matching_records(**ctx):
        by_channel.setdefault(record["channel_id"],[]).append(record)
    result[TAG_COLUMN]=TAG_PLACEHOLDER
    labels=[]
    summaries=[]
    for _,row in result.iterrows():
        labels.append("；".join(record["reason_label"] for record in by_channel.get(channel_identity(row),[])))
        if cooperation is not None:
            summary=cooperation.channel_summary(row,product=ctx["product"],form=ctx["coop_type"])
            count=summary.get("record_count",0)
            summaries.append(str(count)+" 条本地记录" if count else "暂无已关联记录")
    result[LABEL_COLUMN]=labels
    if cooperation is not None:result[COOP_COLUMN]=summaries
    return result


def discovery_frame(raw, ctx, prefix, cooperation=None):
    raw=pd.DataFrame(raw) if not isinstance(raw,pd.DataFrame) else raw.copy()
    merged=dedupe_channels(raw)
    include=st.checkbox("显示已处理达人",value=False,key=prefix+"_show_processed",
                        help="默认排除当前条件下已打标签的频道；可临时显示以复核或恢复。")
    shown,hidden=decision_store().filter_frame(merged,include_processed=include,**ctx)
    excluded=len(hidden) if not include else sum(bool(decision_store().active_for(row,**ctx)) for _,row in merged.iterrows())
    st.caption("本批 "+str(len(raw))+" 条来源，合并为 "+str(len(merged))+" 个频道；"+
               ("其中 "+str(excluded)+" 个已有生效标签。" if include else
                "展示 "+str(len(shown))+" 个，已排除 "+str(excluded)+" 个。"))
    st.caption("在频道旁的“打标签”列选择类型，再确认范围；换关键词或做相似推荐都会沿用频道标签。")
    if shown.empty:
        st.info("当前没有可展示的推荐。已有标签的频道可打开“显示已处理达人”复核，或到“人工筛选池”恢复。")
    shown = decorate(shown,ctx,cooperation)
    return render_resource_views(shown,prefix,cooperation=cooperation,product=ctx["product"],form=ctx["coop_type"])


def review_columns():
    return {TAG_COLUMN:st.column_config.SelectboxColumn("打标签",options=[TAG_PLACEHOLDER]+TAG_OPTIONS,width="medium",
                help="在这一行选择标签类型，再确认生效范围；不会删除联系人、邮件或合作记录。"),
            LABEL_COLUMN:st.column_config.TextColumn("生效标签"),
            COOP_COLUMN:st.column_config.TextColumn("合作记录")}


def render_review_controls(edited, ctx, prefix, selected=None, monitor_callback=None, actor=""):
    pending=[]
    if TAG_COLUMN in edited:
        for _,row in edited.iterrows():
            reason=text(row.get(TAG_COLUMN))
            if reason in TAG_OPTIONS:
                pending.append({"row":clean_review_columns(pd.DataFrame([row])).iloc[0].to_dict(),
                                "requested_reason":reason})
    selected=clean_review_columns(selected) if selected is not None else None
    render_tag_actions(clean_review_columns(edited),prefix=prefix,pending_tags=pending,
        selected=selected,monitor_callback=monitor_callback,store=decision_store(),actor=actor,**ctx)


def render_record_lookup(rows, effect_frame, history_frame, library_frame, product="", form="", prefix="lookup"):
    if rows is None or rows.empty:return
    frame=clean_review_columns(rows).copy()
    # Never use a channel name or mailbox address as the selector's identity.
    choices=[]
    seen=set()
    for i,row in frame.iterrows():
        identity=channel_identity(row) or text(row.get("主页链接") or row.get("频道链接"))
        effective_product=product or text(row.get("合作产品"))
        effective_form=form or text(row.get("合作类型"))
        key_identity=json.dumps([identity,effective_product,effective_form],ensure_ascii=False)
        if not identity or key_identity in seen:continue
        seen.add(key_identity);choices.append((key_identity,row.to_dict()))
    if not choices:return
    by_id=dict(choices)
    signature=hashlib.sha256(json.dumps([product,form,list(by_id)],ensure_ascii=False).encode()).hexdigest()[:12]
    key=prefix+"_"+signature
    with st.expander("查看达人合作记录",expanded=False):
        selected=st.selectbox("选择达人查看历史",options=[None]+list(by_id),key=key,
            format_func=lambda value:"请选择达人" if value is None else
            text(by_id[value].get("频道名称") or by_id[value].get("达人名"))+" · "+
            (product or text(by_id[value].get("合作产品")))+" / "+(form or text(by_id[value].get("合作类型")))+" · "+
            (channel_identity(by_id[value]) or text(by_id[value].get("主页链接") or by_id[value].get("频道链接"))))
        if selected:
            render_cooperation_reference(by_id[selected],effect_frame,history_frame,library_frame,
                product=product or text(by_id[selected].get("合作产品")),
                form=form or text(by_id[selected].get("合作类型")),context_key=key+"_reference",expanded=True)


def render_source_videos(frame):
    source_fields=[field for field in ("来源视频","代表视频集合") if field in frame]
    if not source_fields:return
    videos,seen=[],set()
    for _,row in frame.iterrows():
        for field in source_fields:
            try:items=json.loads(text(row.get(field))) if isinstance(row.get(field),str) else row.get(field)
            except (ValueError,TypeError):continue
            if not isinstance(items,list):continue
            for video in items:
                if isinstance(video,dict):
                    url=text(video.get("链接") or video.get("url") or video.get("视频链接"))
                    title=text(video.get("标题") or video.get("title") or video.get("视频标题"))
                else:url,title=text(video),""
                key=(channel_identity(row) or text(row.get("主页链接") or row.get("频道链接")),url)
                if url and key not in seen:
                    seen.add(key);videos.append({"频道":text(row.get("频道名称")),"视频":title,"视频链接":url})
    if videos:
        with st.expander("查看同频道的其他来源视频",expanded=False):
            st.dataframe(pd.DataFrame(videos),hide_index=True,use_container_width=True,
                column_config={"视频链接":st.column_config.LinkColumn("视频链接",display_text="观看视频")})


def merge_history(existing, incoming):
    """Append new channel resources and retain originals, including unknown fields."""
    if existing is None:existing=pd.DataFrame()
    if incoming is None or incoming.empty:return existing.copy()
    columns=list(dict.fromkeys(list(existing.columns)+list(incoming.columns)+["来源词","来源视频"]))
    records=existing.fillna("").to_dict("records")
    def identity(row):
        cid=channel_identity(row)
        if cid:return "id:"+cid
        url,_=canonical_channel(row.get("主页链接",row.get("频道链接","")))
        return "url:"+url if url else ""
    index={}
    for i,row in enumerate(records):
        key=identity(row)
        if key:index.setdefault(key,i)
    def items(value):
        if isinstance(value,(list,tuple)):return list(value)
        if not text(value):return []
        try:
            result=json.loads(text(value))
            if isinstance(result,list):return result
        except (ValueError,TypeError):pass
        return [text(value)]
    for row in incoming.fillna("").to_dict("records"):
        key=identity(row)
        if key and key in index:target=records[index[key]]
        else:
            target=dict(row);records.append(target)
            if key:index[key]=len(records)-1
        for field,value in row.items():
            if not text(target.get(field)) and text(value):target[field]=value
        keywords=list(dict.fromkeys(items(target.get("来源词"))+items(row.get("来源词"))+
            [v.strip() for v in text(target.get("命中关键词")).split("|") if v.strip()]+
            [v.strip() for v in text(row.get("命中关键词")).split("|") if v.strip()]))
        videos=items(target.get("来源视频"))+items(row.get("来源视频"))
        for source in (target,row):
            link=text(source.get("代表视频链接") or source.get("视频链接"))
            title=text(source.get("代表视频标题") or source.get("视频标题"))
            if link:
                value={"链接":link,"标题":title}
                if value not in videos:videos.append(value)
        if keywords:
            target["来源词"]=json.dumps(keywords,ensure_ascii=False)
            target["命中关键词"]=" | ".join(keywords)
        if videos:
            unique,seen=[],set()
            for video in videos:
                marker=json.dumps(video,ensure_ascii=False,sort_keys=True,default=str)
                if marker not in seen:seen.add(marker);unique.append(video)
            target["来源视频"]=json.dumps(unique,ensure_ascii=False)
    return pd.DataFrame(records,columns=columns)
