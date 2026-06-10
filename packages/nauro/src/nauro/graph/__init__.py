"""HTML rendering for the decision graph.

``render_html`` turns a graph payload (built by
``nauro_core.build_graph_payload``) into one self-contained, read-only HTML
document: inline CSS and JS, no external requests, no third-party assets. The
payload is embedded as a single JSON block the page reads at load time.
"""

from nauro.graph.html_render import render_html

__all__ = ["render_html"]
