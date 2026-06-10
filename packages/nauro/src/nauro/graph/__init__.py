"""HTML rendering for the decision graph.

``render_html`` turns a graph payload (built by
``nauro_core.build_graph_payload``) into one self-contained, read-only HTML
document: inline CSS and JS, no external requests, no third-party assets. The
payload is embedded as a single JSON block the page reads at load time.
"""

from nauro.graph.html_render import render_html

# The default basename for the rendered graph artifact. The graph command writes
# it into the store directory; sync excludes it because its generation timestamp
# changes every run, so its sha never settles.
DEFAULT_GRAPH_FILENAME = "nauro-graph.html"

__all__ = ["DEFAULT_GRAPH_FILENAME", "render_html"]
