"""Carry existing editor edits across one presentation-layout upgrade.

Only ordinary session metadata is written. Widget state and original data are
never changed; the new editor receives a stable, seeded display-data copy.
"""
from copy import deepcopy
import hashlib
import json
import math
import re

import pandas as pd
import streamlit as st
from streamlit import dataframe_util


_CACHE_KEY = '_wb_editor_layout_seeds'
_MAX_ENTRIES = 16
_MAX_CHANGES = 4096
_MAX_SEED_BYTES = 256 * 1024


def _source_hash(frame):
    payload = dataframe_util.convert_pandas_df_to_arrow_bytes(frame)
    return hashlib.sha256(payload).hexdigest()


def _position(value, row_count):
    if type(value) is int:
        position = value
    elif isinstance(value, str) and re.fullmatch(r'0|[1-9][0-9]*', value):
        position = int(value)
    else:
        return None
    return position if 0 <= position < row_count else None


def _safe_scalar(value):
    if value is None or type(value) in (bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if isinstance(value, str) and len(value) <= 4096:
        try:
            value.encode('utf-8')
            return True
        except UnicodeError:
            return False
    return False


def _read_changes(frame, state, allowed):
    if not isinstance(state, dict) or not isinstance(state.get('edited_rows'), dict):
        return []
    changes = []
    for row, cells in state['edited_rows'].items():
        position = _position(row, len(frame))
        if position is None or not isinstance(cells, dict):
            continue
        for column, value in cells.items():
            if column not in allowed or not _safe_scalar(value):
                continue
            if pd.api.types.is_bool_dtype(frame[column].dtype) and type(value) is not bool:
                continue
            changes.append((position, column, deepcopy(value)))
            if len(changes) > _MAX_CHANGES:
                return []
    if len(json.dumps(changes, ensure_ascii=False).encode('utf-8')) > _MAX_SEED_BYTES:
        return []
    return changes


def seed_editor_layout(frame, key, editable_columns, revision='number-first-v1'):
    """Return a display copy retaining valid pre-upgrade, fixed-row edits.

    A source change permanently invalidates that key/revision's previous seed.
    It never re-imports stale widget edits after an invalidation. The caller's
    key must already distinguish batch, context and original row identity/order.
    """
    if not isinstance(frame, pd.DataFrame):
        return frame.copy() if hasattr(frame, 'copy') else frame
    display = frame.copy(deep=True)
    try:
        if not isinstance(key, str) or not key or not isinstance(revision, str) or not revision or not frame.columns.is_unique:
            return display
        allowed = {column for column in editable_columns if isinstance(column, str) and column in frame.columns}
        source_hash = _source_hash(frame)
        identity = hashlib.sha256(json.dumps([revision, key]).encode('utf-8')).hexdigest()
        existing = st.session_state.get(_CACHE_KEY, {})
        cache = dict(existing) if isinstance(existing, dict) else {}
        entry = cache.pop(identity, None)
        if not isinstance(entry, dict):
            entry = {'source_hash': source_hash,
                     'changes': _read_changes(frame, st.session_state.get(key), allowed)}
        elif entry.get('source_hash') != source_hash:
            # A new backend value is authoritative. Record an empty seed now;
            # do not import this editor's old diff on any subsequent render.
            entry = {'source_hash': source_hash, 'changes': []}
        cache[identity] = entry
        while len(cache) > _MAX_ENTRIES:
            cache.pop(next(iter(cache)))
        st.session_state[_CACHE_KEY] = cache
        for position, column, value in entry.get('changes', []):
            if column in allowed and _position(position, len(frame)) is not None:
                display.iat[position, frame.columns.get_loc(column)] = deepcopy(value)
        return display
    except Exception:
        # No fallback reads, logging of row data, or mutation of widget state.
        return frame.copy(deep=True)
