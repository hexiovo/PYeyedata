"""PYeyedata: local eye-tracking data analysis."""

from .app_info import APP_NAME, APP_VERSION, PRD_VERSION

from .contracts import (
    AnalysisBundle,
    AvailabilityStatus,
    CONTRACT_VERSION,
    ContractError,
    CoordinateMetadata,
    EyeCoordinateMetadata,
    DesignSpec,
    MetricAvailability,
    MetricSpec,
    NormalizedDataset,
    ParameterSet,
    ResultRow,
    StatisticsResult,
    SIGNAL_DERIVED_COLUMNS,
    SIGNAL_TEMPLATE_COLUMNS,
)
from .io import (
    CsvOptions,
    DataImportError,
    ImportedTable,
    SignalImportOptions,
    normalize_aois,
    normalize_performance,
    normalize_segments,
    normalize_signals,
    read_table,
)
from .processing import (
    ProcessingConfig,
    ProcessingError,
    ProcessingResult,
    process_signals,
    time_weighted_mean,
)
from .eye_streams import EyeSelectionConfig, EyeStreamError, derive_eye_streams
from .events import (
    EventDetectionConfig,
    EventDetectionError,
    EventDetectionResult,
    detect_events,
)
from .execution_cache import ExecutionCache, CACHE_KEY_SCHEMA_VERSION, CACHE_AUDIT_SCHEMA_VERSION
from .store import (
    ArchiveError,
    ArchiveLimits,
    PyeaArchive,
    dataframe_from_csv_member,
    dataframe_to_csv_member,
    read_pyea,
    write_pyea,
)
from .pipeline import (
    AnalysisConfig,
    AnalysisPreflight,
    LoadedAnalysis,
    PipelineError,
    PIPELINE_SCHEMA_VERSION,
    get_analysis_schema,
    load_analysis,
    preflight_analysis,
    run_analysis,
    save_analysis,
)

__all__ = [
    "APP_NAME", "APP_VERSION", "PRD_VERSION", "AnalysisBundle", "ArchiveError", "ArchiveLimits", "AvailabilityStatus", "CONTRACT_VERSION",
    "ContractError", "CoordinateMetadata", "CsvOptions", "DataImportError", "DesignSpec",
    "EyeSelectionConfig", "EyeStreamError", "EventDetectionConfig", "EventDetectionError",
    "EventDetectionResult",
    "EyeCoordinateMetadata", "SIGNAL_DERIVED_COLUMNS", "SIGNAL_TEMPLATE_COLUMNS",
    "ImportedTable", "MetricAvailability", "MetricSpec", "NormalizedDataset", "ParameterSet",
    "ProcessingConfig", "ProcessingError", "ProcessingResult", "PyeaArchive", "ResultRow",
    "SignalImportOptions", "StatisticsResult", "dataframe_from_csv_member", "dataframe_to_csv_member",
    "normalize_aois", "normalize_performance", "normalize_segments", "normalize_signals",
    "process_signals", "read_pyea", "read_table", "time_weighted_mean", "write_pyea",
    "derive_eye_streams", "detect_events",
    "AnalysisConfig", "AnalysisPreflight", "LoadedAnalysis", "PipelineError",
    "PIPELINE_SCHEMA_VERSION", "get_analysis_schema", "load_analysis",
    "preflight_analysis", "run_analysis", "save_analysis",
    "ExecutionCache", "CACHE_KEY_SCHEMA_VERSION", "CACHE_AUDIT_SCHEMA_VERSION",
]
