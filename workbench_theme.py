"""Apply the project's native theme without restarting a running workbench.

Streamlit 1.50 sends customTheme on every full script run. Its public
st.set_option only supports client settings, so this narrow compatibility
adapter uses the installed config layer for registered theme.* keys only.
It never reparses or changes server, network, credentials or other settings.
The TOML remains the source of truth for subsequent normal server starts.
"""
from copy import deepcopy
import hashlib
from pathlib import Path

import streamlit as st
from streamlit import config
import toml


def sync_native_theme(project_root):
    path = Path(project_root) / '.streamlit' / 'config.toml'
    try:
        source = path.read_text(encoding='utf-8')
        theme = toml.loads(source).get('theme', {})
        if not isinstance(theme, dict) or theme.get('base') not in ('light', 'dark'):
            return False
        desired = {}
        for key, value in theme.items():
            if key == 'sidebar' and isinstance(value, dict):
                desired.update({'theme.sidebar.' + name: val for name, val in value.items()})
            elif not isinstance(value, dict):
                desired['theme.' + key] = value
            else:
                return False
        revision = hashlib.sha256(source.encode('utf-8')).hexdigest()
        with config._config_lock:
            options = config.get_config_options()
            if any(key not in options for key in desired):
                return False
            changes = {key: val for key, val in desired.items() if options[key].value != val}
            if not changes:
                return True
            # A failed or externally reset theme must not cause a rerun loop.
            if st.session_state.get('_wb_native_theme_attempt') == revision:
                return False
            st.session_state['_wb_native_theme_attempt'] = revision
            previous = {key: (deepcopy(options[key].value), options[key].where_defined) for key in changes}
            try:
                for key, value in changes.items():
                    config.set_option(key, value, where_defined=str(path))
            except Exception:
                for key, (value, origin) in previous.items():
                    config.set_option(key, value, where_defined=origin)
                return False
    except (OSError, ValueError, AttributeError):
        return False
    # Finish this entire run: an early st.rerun removes widgets not yet rendered.
    # The next ordinary full run sends the native theme without clearing them.
    return True
