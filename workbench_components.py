"""Small, shared presentation elements; no business or network access."""
from html import escape
import streamlit as st

_PATHS = {
    'search': '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>',
    'filter': '<path d="M4 7h16M7 12h10M10 17h4"/>',
    'users': '<circle cx="9" cy="8" r="3"/><path d="M3 20v-2a6 6 0 0 1 12 0v2M16 5a3 3 0 0 1 0 6M18 14a5 5 0 0 1 3 4v2"/>',
    'library': '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 8h8M8 12h8M8 16h5"/>',
    'insights': '<path d="M4 19h16M7 15v-4M12 15V5M17 15V8"/>',
}


def section_heading(title, *, eyebrow='', caption='', icon='library'):
    path = _PATHS.get(icon, _PATHS['library'])
    svg = '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+path+'</svg>'
    html = '<div class="wb-section-heading"><span class="wb-section-icon">'+svg+'</span><div>'
    if eyebrow:
        html += '<div class="wb-eyebrow">'+escape(eyebrow)+'</div>'
    html += '<strong>'+escape(title)+'</strong>'
    if caption:
        html += '<p>'+escape(caption)+'</p>'
    st.markdown(html+'</div></div>', unsafe_allow_html=True)
