"""Desktop application scaffolding for PYeyedata."""

from .models import DatasetRevision, RunRecord, RunSnapshot, SelectionSpec
from .bridge import PipelineBridge, PipelineOutcome, PipelinePhase, PipelineStatus
from .session import AnalysisSession
from .forms import BandListEditor, TypedFieldSpec, TypedFieldWidget, TypedParameterForm
from .main_window import MainWindow
from .tasks import CancellationToken, PipelineTaskWorker, TaskManager
from .widgets import DataFrameTableModel, DataFrameTableView

__all__ = [
    "DatasetRevision",
    "RunRecord",
    "RunSnapshot",
    "SelectionSpec",
    "PipelineBridge",
    "PipelineOutcome",
    "PipelinePhase",
    "PipelineStatus",
    "AnalysisSession",
    "TypedFieldSpec",
    "TypedFieldWidget",
    "TypedParameterForm",
    "BandListEditor",
    "MainWindow",
    "CancellationToken",
    "PipelineTaskWorker",
    "TaskManager",
    "DataFrameTableModel",
    "DataFrameTableView",
]
