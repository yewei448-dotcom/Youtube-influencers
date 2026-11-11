"""Presentation-only emphasis for creator identities in native editable tables."""
import hashlib

NAME_COLOR = '#6244AF'
PINNED_NAME_COLOR = '#240050'


def style_creator_names(frame, view='creators', *, pinned=False):
    """Style only the read-only identity column; never change row data or index.

    Streamlit's native grid honors these three Styler properties for disabled
    columns. Duplicate labels cannot be styled by Pandas, so keep that table's
    original display rather than resetting its business identities.
    """
    if (frame.empty or '频道名称' not in frame.columns
            or not frame.index.is_unique or not frame.columns.is_unique):
        return frame
    uuid = 'wb_names_' + hashlib.sha256(str(view).encode('utf-8')).hexdigest()[:12]
    return frame.style.set_uuid(uuid).set_properties(
        subset=['频道名称'],
        # Native pinned cells paint at 60% opacity; compensate for readable purple.
        **{'color': PINNED_NAME_COLOR if pinned else NAME_COLOR, 'font-weight': '700'})
