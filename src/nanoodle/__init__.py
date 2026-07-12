"""nanoodle — re-execute nanoodle (https://nanoodle.com) workflow saves against NanoGPT.

Zero runtime dependencies (Python >= 3.9, stdlib only).

    from nanoodle import Workflow
    wf = Workflow.load("noodle-graph.json", api_key="...")
    result = wf.run({"Text": "a cozy ramen shop"})
    result["Image"].save("out.png")
"""

from .errors import NanoodleError, RunError, UnsupportedNodeError
from .iodef import InputSpec, OutputSpec, SettingSpec
from .media import MediaRef, media_from_file
from .share import decode_share_fragment, decode_share_url, is_share_ref
from .workflow import NodeRun, RunResult, Workflow
from .x402 import parse_nano_invoice

__version__ = "0.2.0"

__all__ = [
    "Workflow", "RunResult", "NodeRun",
    "MediaRef", "media_from_file",
    "NanoodleError", "UnsupportedNodeError", "RunError",
    "InputSpec", "OutputSpec", "SettingSpec",
    "decode_share_url", "decode_share_fragment", "is_share_ref",
    "parse_nano_invoice",
    "__version__",
]
