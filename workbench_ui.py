"""Shared navigation and presentation for the existing workbench pages."""
from pathlib import Path

import streamlit as st
from workbench_theme import sync_native_theme

PAGES = [
    ('find', '找达人', ':material/person_search:'),
    ('resources', '达人资源', ':material/groups:'),
    ('contacts', '建联库', ':material/handshake:'),
    ('analysis', '数据分析', ':material/monitoring:'),
    ('reminders', '待办提醒', ':material/notifications_active:'),
]

_NAV_GROUPS = (
    ('发现与资源', ('find', 'resources')),
    ('合作管理', ('contacts', 'reminders')),
    ('经营复盘', ('analysis',)),
)


def _render_theme():
    """Load the glass presentation without changing widget or navigation state."""
    try:
        css = Path(__file__).with_name('workbench_theme.css').read_text(encoding='utf-8')
    except OSError:
        return
    st.markdown('<style>' + css + '</style>', unsafe_allow_html=True)


def _select_page(page):
    if page == 'contacts' and st.session_state.get('workbench_page') != page:
        st.session_state.pop('contact_editor_snapshot', None)
        st.session_state['contact_editor_version'] = st.session_state.get('contact_editor_version', 0) + 1
        st.session_state.pop('recipient_snapshot', None)
        st.session_state.pop('_compose_selection', None)
        st.session_state.pop('_compose_batch', None)
        st.session_state['recipient_version'] = st.session_state.get('recipient_version', 0) + 1
    st.session_state['workbench_page'] = page


def render_navigation():
    sync_native_theme(Path(__file__).resolve().parent)
    _render_theme()
    page = st.session_state.get('workbench_page', 'find')
    if page not in {p[0] for p in PAGES}:
        page = 'find'; st.session_state['workbench_page'] = page
    with st.sidebar:
        st.markdown('''<div class="wb-brand">
          <span class="wb-brand-mark" aria-hidden="true"><svg width="21" height="21" viewBox="0 0 24 24" fill="none">
            <rect x="3" y="3" width="7" height="7" rx="1.5" fill="currentColor"/>
            <rect x="14" y="3" width="7" height="7" rx="1.5" stroke="currentColor" stroke-width="1.5"/>
            <rect x="3" y="14" width="7" height="7" rx="1.5" stroke="currentColor" stroke-width="1.5"/>
            <rect x="14" y="14" width="7" height="7" rx="1.5" fill="currentColor" opacity=".55"/>
          </svg></span>
          <div class="wb-wordmark"><strong>达人合作工作台</strong><span>工作空间</span></div>
        </div>''', unsafe_allow_html=True)
        for group, page_ids in _NAV_GROUPS:
            st.markdown('<div class="wb-nav-section">' + group + '</div>', unsafe_allow_html=True)
            for key, label, icon in PAGES:
                if key not in page_ids:
                    continue
                st.button(label, icon=icon, key='nav_' + key, use_container_width=True,
                          type='primary' if key == page else 'secondary',
                          on_click=_select_page, args=(key,))
        st.divider()
        st.markdown('<div class="wb-sidebar-footer"><span class="wb-symbol" aria-hidden="true">workspaces</span><span>发现 · 建联 · 复盘</span></div>', unsafe_allow_html=True)
    label = next(p[1] for p in PAGES if p[0] == page)
    st.markdown('<div class="wb-breadcrumb"><span>WORKSPACE</span><span class="wb-breadcrumb-divider">/</span><span class="wb-breadcrumb-current">' + label + '</span></div>', unsafe_allow_html=True)
    st.title(label)
    st.markdown('<div class="wb-header-divider" aria-hidden="true"></div>', unsafe_allow_html=True)
    return page


def render_filters(prefix='mining'):
    with st.container(key='wb_filters_' + prefix):
        st.markdown('<div class="wb-inline-label"><span class="wb-symbol" aria-hidden="true">tune</span><span>频道范围</span></div>', unsafe_allow_html=True)
        c1, c2, c3 = st.columns(3)
        min_subs = c1.number_input('最小粉丝数', 0, 100_000_000, 5000, step=1000, key=prefix+'_min_subs')
        max_subs = c2.number_input('最大粉丝数', 0, 100_000_000, 100000, step=10000, key=prefix+'_max_subs')
        active_days = c3.number_input('近 N 天活跃（0=不检查）', 0, 365, 30, step=1,
                                      key=prefix+'_active_days', help='0 表示不检查活跃度，速度更快')
        a, b, c = st.columns(3)
        english_only = a.checkbox('仅英语频道', True, key=prefix+'_english')
        exclude_brand = b.checkbox('排除竞品官方号', True, key=prefix+'_brands')
        has_email = c.checkbox('仅显示有邮箱', False, key=prefix+'_email')
        with st.expander('更多质量条件', expanded=False, icon=':material/filter_alt:'):
            a, b = st.columns(2)
            min_avg_views = a.number_input('近6条平均播放 ≥', 0, 100_000_000, 0, step=500,
                                            key=prefix+'_avg', help='0=不筛；按近期6条视频计算均播')
            min_er = b.number_input('互动率 ≥ %', 0.0, 100.0, 0.0, step=.5,
                                     key=prefix+'_er', help='沿用原口径：近6条均播÷粉丝数')
    return min_subs, max_subs, english_only, exclude_brand, active_days, min_avg_views, min_er, has_email
