"""nanoodle — re-execute nanoodle (https://nanoodle.io) workflow saves against NanoGPT.

Zero runtime dependencies (Python >= 3.9, stdlib only).

    from nanoodle import Workflow
    wf = Workflow.load("noodle-graph.json", api_key="...")
    result = wf.run({"Text": "a cozy ramen shop"})
    result["Image"].save("out.png")
"""

from .errors import NanoodleError, RunError, UnsupportedNodeError
from .iodef import InputSpec, OutputSpec, SettingSpec
from .media import MediaRef, media_from_file
from .workflow import NodeRun, RunResult, Workflow

__version__ = "0.1.1"

__all__ = [
    "Workflow", "RunResult", "NodeRun",
    "MediaRef", "media_from_file",
    "NanoodleError", "UnsupportedNodeError", "RunError",
    "InputSpec", "OutputSpec", "SettingSpec",
    "__version__",
]
